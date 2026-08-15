"""Game controller / sim wheel input via SDL (pygame).

Sim wheels — Moza, Fanatec, Simucube, Logitech — present as DirectInput
devices with a lot of buttons (a Moza R25 base reports 128). SDL reads
them all, so any wheel or button-box button can drive push-to-talk.

Everything goes through a single `ControllerHub`. SDL's joystick state
must be pumped from one place; two threads polling it independently is
the classic way to get missed or duplicated button events.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("race-engineer")

# Must be set before pygame is imported, or SDL tries to open a window.
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
# Keep reading the wheel while the sim has focus — without this SDL stops
# reporting buttons whenever our window is in the background, which is the
# normal case during a race.
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")

try:
    import pygame

    HAS_PYGAME = True
except Exception as _exc:  # pragma: no cover - depends on the host
    pygame = None
    HAS_PYGAME = False
    logger.debug("pygame unavailable: %s", _exc)

POLL_HZ = 120


@dataclass
class ControllerInfo:
    index: int
    name: str
    guid: str
    buttons: int
    axes: int = 0
    hats: int = 0

    def to_dict(self) -> Dict[str, object]:
        return {
            "index": self.index,
            "name": self.name,
            "guid": self.guid,
            "buttons": self.buttons,
            "axes": self.axes,
            "hats": self.hats,
        }


@dataclass
class ButtonBinding:
    """Which physical button drives push-to-talk."""

    guid: str = ""
    name: str = ""
    button: int = -1
    index: int = -1  # fallback when the GUID is not known

    @property
    def configured(self) -> bool:
        return self.button >= 0 and bool(self.guid or self.name or self.index >= 0)

    def describe(self) -> str:
        if not self.configured:
            return "unbound"
        return f"{self.name or self.guid or f'device {self.index}'} button {self.button}"


class ControllerHub:
    """Owns the SDL joystick subsystem and polls it on one thread."""

    _instance: Optional["ControllerHub"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._joysticks: Dict[int, "pygame.joystick.Joystick"] = {}
        self._states: Dict[int, List[bool]] = {}
        self._error: Optional[str] = None

        # Edge subscribers: called with (guid, name, device_index, button, pressed)
        self._subscribers: List[Callable[[str, str, int, int, bool], None]] = []
        # Set while a bind capture is in progress.
        self._capture: Optional[Tuple[threading.Event, List[ButtonBinding]]] = None

    # ── Lifecycle ───────────────────────────────────────────────────

    @classmethod
    def instance(cls) -> "ControllerHub":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @property
    def available(self) -> bool:
        return HAS_PYGAME

    @property
    def error(self) -> Optional[str]:
        return self._error

    def start(self) -> bool:
        """Start polling. Returns False if SDL is unavailable."""
        if not HAS_PYGAME:
            self._error = "pygame is not installed (pip install pygame-ce)"
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="controller-hub"
            )
            self._thread.start()
        # Give the thread a moment to enumerate before callers ask for devices.
        for _ in range(40):
            if self._joysticks or self._error:
                break
            time.sleep(0.05)
        return self._error is None

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    # ── Polling ─────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            pygame.init()
            pygame.joystick.init()
        except Exception as exc:
            self._error = f"Could not initialise SDL: {exc}"
            logger.warning("%s", self._error)
            return

        self._rescan()
        interval = 1.0 / POLL_HZ

        while not self._stop.is_set():
            try:
                pygame.event.pump()
                for event in pygame.event.get():
                    if event.type in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                        self._rescan()
                self._poll_buttons()
            except Exception as exc:
                logger.debug("Controller poll failed: %s", exc)
            time.sleep(interval)

        try:
            pygame.joystick.quit()
            pygame.quit()
        except Exception:
            pass

    def _rescan(self) -> None:
        """(Re)enumerate devices — called on hot-plug."""
        try:
            count = pygame.joystick.get_count()
        except Exception as exc:
            self._error = f"Could not enumerate controllers: {exc}"
            return

        joysticks: Dict[int, "pygame.joystick.Joystick"] = {}
        states: Dict[int, List[bool]] = {}
        for i in range(count):
            try:
                joy = pygame.joystick.Joystick(i)
                joysticks[i] = joy
                states[i] = [False] * joy.get_numbuttons()
            except Exception as exc:
                logger.debug("Could not open joystick %d: %s", i, exc)

        with self._lock:
            self._joysticks = joysticks
            self._states = states
        logger.info(
            "Controllers detected: %s",
            ", ".join(f"{j.get_name()} ({j.get_numbuttons()} buttons)"
                      for j in joysticks.values()) or "none",
        )

    def _poll_buttons(self) -> None:
        with self._lock:
            joysticks = dict(self._joysticks)
            states = self._states

        for index, joy in joysticks.items():
            previous = states.get(index)
            if previous is None:
                continue
            for button in range(min(len(previous), joy.get_numbuttons())):
                try:
                    now = bool(joy.get_button(button))
                except Exception:
                    continue
                if now == previous[button]:
                    continue
                previous[button] = now
                self._dispatch(index, joy, button, now)

    def _dispatch(self, index: int, joy, button: int, pressed: bool) -> None:
        guid = _safe_guid(joy)
        name = joy.get_name()

        # A bind capture consumes the first press it sees.
        capture = self._capture
        if capture is not None and pressed:
            event, result = capture
            result.append(ButtonBinding(guid=guid, name=name, button=button, index=index))
            event.set()
            return

        for callback in list(self._subscribers):
            try:
                callback(guid, name, index, button, pressed)
            except Exception:
                logger.exception("Controller subscriber failed")

    # ── Public API ──────────────────────────────────────────────────

    def devices(self) -> List[ControllerInfo]:
        self.start()
        with self._lock:
            joysticks = dict(self._joysticks)
        infos = []
        for index, joy in joysticks.items():
            try:
                infos.append(
                    ControllerInfo(
                        index=index,
                        name=joy.get_name(),
                        guid=_safe_guid(joy),
                        buttons=joy.get_numbuttons(),
                        axes=joy.get_numaxes(),
                        hats=joy.get_numhats(),
                    )
                )
            except Exception:
                continue
        return infos

    def subscribe(self, callback: Callable[[str, str, int, int, bool], None]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def capture_next_press(self, timeout: float = 15.0) -> Optional[ButtonBinding]:
        """Block until a button is pressed, and report which one it was.

        This is how the UI binds a button: the user presses the one they
        want instead of looking up an index.
        """
        if not self.start():
            return None
        event = threading.Event()
        result: List[ButtonBinding] = []
        self._capture = (event, result)
        try:
            event.wait(timeout=timeout)
        finally:
            self._capture = None
        return result[0] if result else None


def _safe_guid(joy) -> str:
    try:
        return joy.get_guid()
    except Exception:
        return ""


def resolve_binding(hub: ControllerHub, binding: ButtonBinding) -> Optional[int]:
    """Find the live device index for a stored binding.

    Matches on GUID first so the binding survives replugging into another
    USB port, then on name, then on the raw index.
    """
    devices = hub.devices()
    if not devices:
        return None
    if binding.guid:
        for dev in devices:
            if dev.guid and dev.guid == binding.guid:
                return dev.index
    if binding.name:
        for dev in devices:
            if dev.name == binding.name:
                return dev.index
    if 0 <= binding.index < len(devices):
        return binding.index
    return None
