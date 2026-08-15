"""Push-to-talk capture via keyboard (pynput) or a sim wheel button."""

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("race-engineer")

try:
    from pynput import keyboard

    HAS_PYNPUT = True
except Exception as _exc:  # no display / unsupported platform
    keyboard = None
    HAS_PYNPUT = False
    logger.debug("pynput unavailable: %s", _exc)


class InputUnavailable(RuntimeError):
    """Raised when a global hotkey listener cannot be installed."""


# Modifier keys report left/right variants, but nobody binds "the right ctrl
# specifically" — they bind ctrl. Both sides collapse to one name so a
# combination works whichever hand you use.
_MODIFIER_ALIASES = {
    "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "shift_l": "shift", "shift_r": "shift",
    "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "cmd_l": "cmd", "cmd_r": "cmd",
}
MODIFIERS = ("ctrl", "shift", "alt", "cmd")

# Long-standing bindings from when the UI offered a fixed list.
_LEGACY_NAMES = {
    "lshift": "shift", "rshift": "shift",
    "lctrl": "ctrl", "rctrl": "ctrl",
    "lalt": "alt", "ralt": "alt",
}


def key_name(key: Any) -> Optional[str]:
    """A stable name for a pynput key, or None if it cannot be identified.

    Letters and digits arrive as KeyCode with a char; named keys as Key
    members. Both are reduced to lower-case strings so a binding can be
    stored, compared and shown to the driver as the same thing.
    """
    if key is None:
        return None

    # With ctrl held, pynput reports the control character rather than the
    # letter — ctrl+r arrives as "\x12", which is invisible and unusable as
    # a binding. The virtual key code still identifies the physical key.
    char = getattr(key, "char", None)
    vk = getattr(key, "vk", None)
    if char and ord(char[0]) < 32:
        if vk is not None and 0x30 <= vk <= 0x5A:      # 0-9 and A-Z
            return chr(vk).lower()
        # Fall back to the classic mapping: ctrl+A is 0x01.
        return chr(ord(char[0]) + 64).lower()
    if char:
        return char.lower()
    name = getattr(key, "name", None)
    if name:
        return _MODIFIER_ALIASES.get(name, name).lower()
    # Numpad and media keys can arrive as a bare virtual key code.
    return f"vk{vk}" if vk is not None else None


def parse_binding(spec: str) -> List[str]:
    """"ctrl+shift+r" -> ["ctrl", "shift", "r"], modifiers first."""
    parts = [p.strip().lower() for p in str(spec or "").split("+") if p.strip()]
    parts = [_LEGACY_NAMES.get(p, p) for p in parts]
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return sorted(out, key=lambda p: (p not in MODIFIERS, out.index(p)))


def describe_binding(spec: str) -> str:
    """How the binding should read on screen."""
    parts = parse_binding(spec)
    if not parts:
        return "unset"
    pretty = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "cmd": "Cmd",
              "space": "Space", "enter": "Enter", "tab": "Tab", "esc": "Esc"}
    return " + ".join(pretty.get(p, p.upper() if len(p) == 1 else p.title())
                      for p in parts)


class PushToButton:
    """Listen for the configured push-to-talk button.

    Callers can either poll `is_pressed()` or register `on_press` /
    `on_release` callbacks (used by the orchestrator for hold-to-record).
    """

    def __init__(self, config):
        # Accept either the whole AppConfig or just its push_to_button dict.
        self._settings = getattr(config, "push_to_button", config) or {}
        self._listener = None
        self._pressed = False
        self._target = None
        self._controller_bound = False
        self._controller_device: Optional[int] = None
        self._controller_button: int = -1
        self.on_press: Optional[Callable[[], None]] = None
        self.on_release: Optional[Callable[[], None]] = None

    # ── Lifecycle ───────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """Whether push-to-talk can actually fire right now.

        For a wheel this has to mean the device is present, not merely that
        the library imported. A bound wheel that is switched off reported
        green, which is the worst way to find out — you press the button
        mid-session and nothing happens.
        """
        if (self._settings.get("type") or "keyboard").lower() == "controller":
            from .controller import HAS_PYGAME, ControllerHub, resolve_binding

            if not HAS_PYGAME or not self._controller_bound:
                return False
            try:
                hub = ControllerHub.instance()
                return resolve_binding(hub, self._binding()) is not None
            except Exception as exc:
                logger.debug("Could not check the controller binding: %s", exc)
                return False
        return HAS_PYNPUT

    @property
    def description(self) -> str:
        btn_type = (self._settings.get("type") or "keyboard").lower()
        if btn_type == "controller":
            return f"controller:{self._binding().describe()}"
        return f"keyboard:{describe_binding(self._settings.get('key', 'shift'))}"

    def _binding(self):
        from .controller import ButtonBinding

        return ButtonBinding(
            guid=self._settings.get("device_guid", "") or "",
            name=self._settings.get("device_name", "") or "",
            button=int(self._settings.get("button", -1)),
            index=int(self._settings.get("device_index", -1)),
        )

    def start(self) -> None:
        """Start listening. Raises InputUnavailable if no backend can run."""
        if self._listener is not None or self._controller_bound:
            return

        btn_type = (self._settings.get("type") or "keyboard").lower()
        key_or_name = self._settings.get("key") or "lshift"

        if btn_type == "controller":
            # Handled first: it needs no pynput, so a wheel binding works
            # even on a machine where global keyboard hooks are unavailable.
            self._listen_controller()
        elif not HAS_PYNPUT:
            raise InputUnavailable(
                "Global hotkeys unavailable (pynput could not load — needs a desktop session)"
            )
        elif btn_type == "keyboard":
            self._listen_keyboard(key_or_name)
        else:
            raise InputUnavailable(f"Unknown push-to-button type: {btn_type}")

        logger.info("Push-to-talk listening on %s", self.description)

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        if self._controller_bound:
            from .controller import ControllerHub

            ControllerHub.instance().unsubscribe(self._on_controller_event)
            self._controller_bound = False
        self._pressed = False

    def is_pressed(self) -> bool:
        return self._pressed

    # ── Simulation hooks (used by --simulate and the web UI) ─────────

    def press(self) -> None:
        self._set_pressed(True)

    def release(self) -> None:
        self._set_pressed(False)

    def _set_pressed(self, pressed: bool) -> None:
        if pressed == self._pressed:
            return
        self._pressed = pressed
        callback = self.on_press if pressed else self.on_release
        if callback is None:
            return
        try:
            callback()
        except Exception:
            logger.exception("push-to-talk callback failed")

    # ── Backends ────────────────────────────────────────────────────

    def _listen_keyboard(self, spec: str) -> None:
        """Listen for a key or a combination such as ctrl+shift+r.

        Combinations need the set of currently-held keys, not just the last
        event: "ctrl+r" fires when r goes down *while* ctrl is already down,
        and releases as soon as either is let go.
        """
        wanted = parse_binding(spec)
        if not wanted:
            logger.warning("No push-to-talk key bound — falling back to shift")
            wanted = ["shift"]
        self._wanted: Set[str] = set(wanted)
        self._held: Set[str] = set()

        def on_press(key):
            name = key_name(key)
            if name is None:
                return
            self._held.add(name)
            if self._wanted <= self._held:
                self._set_pressed(True)

        def on_release(key):
            name = key_name(key)
            if name is None:
                return
            self._held.discard(name)
            # Letting go of any part of the combination ends the transmission.
            if name in self._wanted:
                self._set_pressed(False)

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.daemon = True
        self._listener.start()

    def capture_next_key(self, timeout: float = 15.0) -> Optional[str]:
        """Block until a key or combination is pressed, and return it.

        Runs against a listener of its own so it works whether or not
        push-to-talk is currently bound to anything.
        """
        if not HAS_PYNPUT:
            return None
        import threading

        event = threading.Event()
        keys: Set[str] = set()

        def on_press(key):
            name = key_name(key)
            if name is None:
                return
            keys.add(name)
            if name not in MODIFIERS:
                event.set()

        def on_release(key):
            # Releasing a lone modifier ends the capture: someone binding
            # "shift" on its own never presses anything else.
            if keys and not event.is_set():
                name = key_name(key)
                if name in MODIFIERS and keys <= set(MODIFIERS):
                    event.set()

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.daemon = True
        listener.start()
        try:
            if not event.wait(timeout):
                return None
        finally:
            listener.stop()
        return "+".join(parse_binding("+".join(keys))) if keys else None

    def _listen_controller(self) -> None:
        """Subscribe to the shared controller hub for one wheel button."""
        from .controller import ControllerHub, resolve_binding

        binding = self._binding()
        if not binding.configured:
            raise InputUnavailable(
                "No wheel button bound yet — use 'Bind a button' in the settings panel"
            )

        hub = ControllerHub.instance()
        if not hub.start():
            raise InputUnavailable(hub.error or "Controller support unavailable")

        device_index = resolve_binding(hub, binding)
        self._controller_device = device_index
        self._controller_button = binding.button
        hub.subscribe(self._on_controller_event)
        self._controller_bound = True
        if device_index is None:
            names = ", ".join(d.name for d in hub.devices()) or "none detected"
            logger.warning(
                "Controller '%s' is not connected yet (connected: %s); waiting for it",
                binding.name or binding.guid, names)

    def _on_controller_event(self, guid, name, index: int,
                             button: int, pressed: bool) -> None:
        if button != self._controller_button:
            return
        binding = self._binding()
        matches = (
            bool(binding.guid and guid and binding.guid.lower() == guid.lower())
            or bool(binding.name and name and binding.name == name)
            or (not binding.guid and not binding.name and binding.index == index)
        )
        if not matches:
            return
        # The index can change when a wheel is unplugged or appears after app
        # startup. The stable GUID/name decides identity; the index is only
        # where that device happens to live now.
        self._controller_device = index
        self._set_pressed(pressed)
