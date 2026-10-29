"""CLI entry point for AI Race Engineer."""

import argparse
import asyncio
import logging
import os
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import perf, single_instance

logger = logging.getLogger("race-engineer")

APP_HOST = "127.0.0.1"
APP_PORT = 9420  # "9420" on a phone keypad → "AI RACE"

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LOG_FILE_BYTES = 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aire",
        description="AIRE — AI Race Engineer: spoken answers over race radio",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument("--config", help="Path to config.json")
    parser.add_argument("--host", default=APP_HOST, help=f"Web UI host (default {APP_HOST})")
    parser.add_argument("--port", type=int, default=APP_PORT, help=f"Web UI port (default {APP_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser on start")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="Run as a desktop app in a native window (default for the packaged build)",
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help="Force browser/server mode even in the packaged build",
    )
    parser.add_argument("--no-tray", action="store_true", help="Desktop mode: skip the tray icon")
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="Desktop mode: start hidden in the tray, no window on launch",
    )
    parser.add_argument(
        "--provider",
        choices=["openrouter", "local"],
        help="Override the configured LLM provider for this run",
    )
    parser.add_argument("--model", help="Override the LLM model for this run")
    parser.add_argument(
        "--telemetry",
        choices=["auto", "irsdk", "simulated"],
        help="Override the telemetry source for this run",
    )
    parser.add_argument(
        "--ask",
        metavar="QUESTION",
        help="Ask one question from the terminal and exit (no web server)",
    )
    parser.add_argument("--speak", action="store_true", help="With --ask, also speak the answer")
    parser.add_argument("--check", action="store_true", help="Check LLM connectivity and exit")
    parser.add_argument("--simulate", action="store_true", help="Force the simulated telemetry source")
    # Accepted and deliberately not read: it means "open the settings UI",
    # which is what happens with no flags at all. Removing it would break
    # anyone whose shortcut still passes it.
    parser.add_argument("--setup", action="store_true", help="Open the settings UI (same as no flags)")
    return parser


def _load_config(args):
    from .config import AppConfig

    config = AppConfig.load(args.config)

    # CLI overrides are session-only; they are never written back to disk.
    patch = {}
    if args.provider:
        patch.setdefault("llm", {})["provider"] = args.provider
    if args.model:
        provider = args.provider or config.llm_provider
        patch.setdefault("llm", {})[provider] = {"model": args.model}
    if args.simulate:
        patch["telemetry"] = {"source": "simulated"}
    elif args.telemetry:
        patch["telemetry"] = {"source": args.telemetry}
    if patch:
        config.update(patch)
        config.mark_clean()  # override only, don't persist
    return config


def _run_check(config) -> int:
    from .llm import LLMClient, LLMError

    settings = config.llm_settings()
    print(f"Provider : {settings['provider']}")
    print(f"Endpoint : {settings['endpoint']}")
    print(f"Model    : {settings['model']}")
    if settings["api_key"]:
        key_state = "set"
    else:
        key_state = "MISSING" if settings["provider"] == "openrouter" else "none (not required)"
    print(f"API key  : {key_state}")
    try:
        result = LLMClient(config).test_connection()
    except LLMError as exc:
        print(f"\nFAILED: {exc}")
        return 1
    print(f"\nOK — model replied: {result['reply']!r}")
    return 0


def _run_ask(config, question: str, speak: bool) -> int:
    from .orchestrator import Orchestrator

    async def run() -> int:
        orch = Orchestrator(config)
        try:
            snapshot = orch.get_snapshot()
            if snapshot is not None:
                print(f"[telemetry: {orch.telemetry.name}, lap {snapshot.track.current_lap}]")
            result = await orch.ask(question, speak=speak, kind="text")
            if "error" in result:
                print(f"FAILED: {result['error']}")
                return 1
            print(f"\nEngineer: {result['answer']}")
            return 0
        finally:
            # Never started, so this is only about letting go: without it
            # the iRacing memory map stays open until the process exits.
            await orch.stop()

    return asyncio.run(run())


def _serve(config, args) -> int:
    import uvicorn

    from .web_ui import create_app

    app = create_app(config, bind_host=args.host)
    url = f"http://{args.host}:{args.port}/"

    if not args.no_browser:
        # Fires after uvicorn has had a moment to bind the port.
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    settings = config.llm_settings()
    print(f"AIRE running at {url}")
    print(f"  LLM       : {settings['provider']} / {settings['model']}")
    print(f"  Telemetry : {config.telemetry.get('source', 'auto')}")
    print(f"  Config    : {config.path}")
    print("Press Ctrl+C to stop.")

    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="debug" if args.verbose else "warning",
        )
    except KeyboardInterrupt:
        pass
    return 0


def _log_to_file(path: Path) -> None:
    """Keep warnings and errors where the packaged build can find them.

    AIRE.exe is built without a console, so it has no stderr, and every
    warning and error went nowhere in the build drivers actually run —
    including the ones written to be loud because they once failed in
    silence. WARNING and up only: questions and answers are logged at INFO,
    and the exchange log is promised to stay in memory.

    Never raises. A log that cannot be opened is not a reason not to start.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=_LOG_FILE_BYTES, backupCount=1, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not open the log file %s: %s", path, exc)
        return
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(handler)


def _stand_in_for_a_missing_console() -> None:
    """Give a process with no console somewhere to write.

    AIRE.exe is built without a console, so `sys.stdout` and `sys.stderr`
    are None, and uvicorn calls `sys.stdout.isatty()` while it configures
    logging. `--server` died there at startup — exit code 1, nothing logged.
    Desktop mode only survived because importing pywebview happens to make
    this same substitution first, which is not something to stand on.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def main() -> int:
    _stand_in_for_a_missing_console()
    args = build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=_LOG_FORMAT,
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        # httpx logs every request at INFO, which drowns out our own output.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    config = _load_config(args)

    # One-shot commands talk to nothing and can run alongside a live app.
    if not (args.check or args.ask):
        if single_instance.find_running(args.host, args.port):
            raised = single_instance.show_existing(args.host, args.port)
            logger.info("AIRE is already running on %s:%s", args.host, args.port)
            print(f"\nAIRE is already running on {args.host}:{args.port}.")
            print("Raised its window." if raised
                  else f"Open http://{args.host}:{args.port}/ to reach it.")
            return 0
        # Only the long-running app writes the file: a one-shot run beside a
        # live one would share it, and rotation fails on Windows while the
        # other process holds it open.
        _log_to_file(config.path.parent / "aire.log")

    # Yield to the sim before anything heavy starts.
    perf.lower_priority(config.performance.get("low_priority", True))

    if args.check:
        return _run_check(config)
    if args.ask:
        return _run_ask(config, args.ask, args.speak)
    if _want_desktop(args):
        return _run_desktop(config, args)
    return _serve(config, args)


def _want_desktop(args) -> bool:
    from .resources import is_frozen

    if args.server:
        return False
    # The packaged .exe is a desktop app unless told otherwise.
    return args.desktop or is_frozen()


def _run_desktop(config, args) -> int:
    from .desktop import DesktopUnavailable, run_desktop

    settings = config.llm_settings()
    print("AIRE — desktop mode")
    print(f"  LLM       : {settings['provider']} / {settings['model']}")
    print(f"  Config    : {config.path}")

    try:
        return run_desktop(
            config,
            host=args.host,
            port=args.port,
            # --no-tray is a per-run override of the saved preference.
            use_tray=(not args.no_tray) and config.desktop.get("hide_to_tray", True),
            log_level="debug" if args.verbose else "warning",
            start_minimized=args.minimized,
        )
    except DesktopUnavailable as exc:
        logger.error("%s", exc)
        print(f"\nFalling back to browser mode: {exc}")
        return _serve(config, args)


if __name__ == "__main__":
    sys.exit(main())
