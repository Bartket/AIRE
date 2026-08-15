"""Desktop application shell.

Runs the FastAPI server on a background thread and presents the settings
panel in a native window (pywebview / Edge WebView2 on Windows), with a
system tray icon so the app can keep listening while iRacing is fullscreen.

Closing the window hides it to the tray; quitting is done from the tray
menu, so a stray click never kills push-to-talk mid-race.
"""

import logging
import socket
import threading
import time
from typing import Optional

from .resources import ICON_PATH

logger = logging.getLogger("race-engineer")

APP_TITLE = "AIRE"


class DesktopUnavailable(RuntimeError):
    """pywebview (or its platform backend) could not be loaded."""


# ── Server thread ───────────────────────────────────────────────────


class ServerThread(threading.Thread):
    """Runs uvicorn in the background so the GUI owns the main thread."""

    def __init__(self, app, host: str, port: int, log_level: str = "warning"):
        super().__init__(daemon=True, name="uvicorn")
        import uvicorn

        self._server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level=log_level)
        )
        self.host = host
        self.port = port

    def run(self) -> None:
        self._server.run()

    def stop(self, timeout: float = 5.0) -> None:
        self._server.should_exit = True
        self.join(timeout=timeout)

    def wait_until_ready(self, timeout: float = 30.0) -> bool:
        """Poll the port until uvicorn accepts connections."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_alive():
                return False
            try:
                with socket.create_connection((self.host, self.port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.1)
        return False


# ── Tray icon ───────────────────────────────────────────────────────


def _load_icon_image():
    """Icon for the tray: the committed .ico, or a drawn fallback."""
    from PIL import Image

    if ICON_PATH.exists():
        return Image.open(ICON_PATH)

    # Bundled asset missing — draw something rather than crash the tray.
    from PIL import ImageDraw

    img = Image.new("RGBA", (64, 64), (24, 26, 32, 255))
    draw = ImageDraw.Draw(img)
    for row in range(4):
        for col in range(4):
            if (row + col) % 2 == 0:
                draw.rectangle([16 + col * 8, 16 + row * 8, 24 + col * 8, 24 + row * 8],
                               fill=(242, 244, 248, 255))
    return img


class Tray:
    """System tray icon. Every failure here is non-fatal by design."""

    def __init__(self, controller: "DesktopApp"):
        self._controller = controller
        self._icon = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        try:
            import pystray
        except Exception as exc:
            logger.info("Tray icon unavailable: %s", exc)
            return False

        try:
            menu = pystray.Menu(
                pystray.MenuItem("Open settings", self._on_show, default=True),
                pystray.MenuItem("Hide window", self._on_hide),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._on_quit),
            )
            self._icon = pystray.Icon(
                "ai_race_engineer", _load_icon_image(), APP_TITLE, menu
            )
        except Exception as exc:
            logger.warning("Could not build tray icon: %s", exc)
            return False

        self._thread = threading.Thread(target=self._run, daemon=True, name="tray")
        self._thread.start()
        return True

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception as exc:
            logger.warning("Tray icon stopped: %s", exc)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    # pystray passes (icon, item) to callbacks.
    def _on_show(self, *_args) -> None:
        self._controller.show_window()

    def _on_hide(self, *_args) -> None:
        self._controller.hide_window()

    def _on_quit(self, *_args) -> None:
        self._controller.quit()


# ── Application ─────────────────────────────────────────────────────


class DesktopApp:
    """Owns the server thread, the native window, and the tray icon."""

    def __init__(self, config, host: str = "127.0.0.1", port: int = 9420,
                 use_tray: bool = True, log_level: str = "warning",
                 start_minimized: bool = False):
        self.config = config
        self.host = host
        self.port = port
        self.use_tray = use_tray
        self.log_level = log_level
        # Launch straight to the tray, for starting the app before the sim
        # rather than having a settings window appear over the desktop.
        self.start_minimized = start_minimized

        self._window = None
        self._server: Optional[ServerThread] = None
        self._tray: Optional[Tray] = None
        self._tray_active = False
        self._quitting = False

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def run(self) -> int:
        try:
            import webview
        except Exception as exc:
            raise DesktopUnavailable(
                f"pywebview could not be loaded ({exc}). "
                "Install it with: pip install pywebview"
            ) from exc

        from .web_ui import create_app

        web_app = create_app(self.config, bind_host=self.host)
        # Lets /api/window/show raise this window when a second launch is
        # attempted, instead of starting a rival copy.
        web_app.state.desktop = self
        self._server = ServerThread(web_app, self.host, self.port, self.log_level)
        self._server.start()

        if not self._server.wait_until_ready():
            self._server.stop()
            raise DesktopUnavailable(
                f"The web server did not start on {self.host}:{self.port} "
                "(is the port already in use?)"
            )
        logger.info("Server ready at %s", self.url)

        if self.use_tray:
            self._tray = Tray(self)
            self._tray_active = self._tray.start()
            if not self._tray_active:
                logger.info("Running without a tray icon — closing the window will quit")

        # Without a tray there would be no way to get the window back.
        minimized = self.start_minimized and self._tray_active
        if self.start_minimized and not minimized:
            logger.warning("Cannot start minimized without a tray icon — showing the window")

        options = dict(width=1180, height=860, min_size=(900, 600))
        try:
            self._window = webview.create_window(
                APP_TITLE, self.url, hidden=minimized, **options)
        except TypeError:
            # Older pywebview builds have no `hidden` argument; open the
            # window and hide it once the GUI loop is up instead.
            self._window = webview.create_window(APP_TITLE, self.url, **options)
            if minimized:
                threading.Timer(0.5, self.hide_window).start()
        # With a tray available, the close button hides rather than quits.
        self._window.events.closing += self._on_closing

        try:
            webview.start(icon=str(ICON_PATH) if ICON_PATH.exists() else None)
        except TypeError:
            # Older pywebview builds don't accept the icon argument.
            webview.start()
        finally:
            self._shutdown()
        return 0

    # ── Window control (called from the tray thread) ─────────────────

    def _on_closing(self) -> bool:
        """Return False to cancel the close and hide to tray instead."""
        if self._quitting or not self._tray_active:
            return True
        self.hide_window()
        logger.info("Window hidden — the engineer is still listening (tray icon)")
        return False

    def show_window(self) -> None:
        if self._window is None:
            return
        try:
            self._window.show()
            self._window.restore()
        except Exception as exc:
            logger.debug("Could not show window: %s", exc)

    def hide_window(self) -> None:
        if self._window is None:
            return
        try:
            self._window.hide()
        except Exception as exc:
            logger.debug("Could not hide window: %s", exc)

    def quit(self) -> None:
        """Tear the app down: destroying the window unblocks webview.start()."""
        self._quitting = True
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception as exc:
                logger.debug("Could not destroy window: %s", exc)

    def _shutdown(self) -> None:
        if self._tray is not None:
            self._tray.stop()
        if self._server is not None:
            self._server.stop()
        self.config.maybe_save()
        logger.info("Desktop app stopped")


def run_desktop(config, host: str = "127.0.0.1", port: int = 9420,
                use_tray: bool = True, log_level: str = "warning",
                start_minimized: bool = False) -> int:
    return DesktopApp(config, host, port, use_tray, log_level, start_minimized).run()
