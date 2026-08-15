"""FastAPI web UI: settings panel, exchange log, and test endpoints."""

import asyncio
import ipaddress
import logging
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .audio_handler import AudioPlayer, AudioRecorder, audio_available, wav_bytes_from_pcm
from .config import AppConfig
from .llm import LLMClient, LLMError
from .orchestrator import Orchestrator
from .resources import STATIC_DIR, UI_DIR
from .tts import TTS, TTSError

logger = logging.getLogger("race-engineer")

# Requests that only read. Everything else can change settings, spend money
# at a provider, or write to the sim, so it has to prove where it came from.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Default bind address, mirrored from the CLI. Kept as a literal rather than
#: imported so this module does not depend on the command line.
DEFAULT_BIND_HOST = "127.0.0.1"


def _host_only(netloc: str) -> str:
    """The host out of a `Host`/`Origin` value, without port or brackets."""
    netloc = netloc.strip().lower()
    if netloc.startswith("["):                    # [::1]:9420
        return netloc.partition("]")[0].lstrip("[")
    return netloc.partition(":")[0]


def _is_loopback(hostname: str) -> bool:
    """Whether a hostname is unambiguously this machine.

    Parsed as an address rather than matched on a prefix: "127.0.0.1" is
    loopback, and "127.0.0.1.example.com" is a domain an attacker can own
    and point at 127.0.0.1, which is the whole DNS-rebinding trick.
    """
    if hostname in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _install_local_guard(app: FastAPI, bind_host: str) -> None:
    """Refuse requests that did not come from the panel on this machine.

    Two separate holes, one middleware.

    A page on any site can post a plain HTML form to this API. That is a
    CORS "simple request" — no preflight, no consent — so the endpoints
    taking no request body were reachable from anywhere: `/api/config/reset`
    wiped the driver's settings, `/api/speech_cache/clear` threw away speech
    that costs money to synthesize again. The JSON endpoints were already
    safe, but only as a side effect of FastAPI insisting on a JSON body.
    A browser always sends `Origin` on a state-changing request, so the
    check is that it matches where the request was addressed. Its absence
    means a non-browser client — the second-launch probe, curl — which no
    web page can impersonate.

    Separately, nothing checked `Host`, so an attacker-controlled name
    resolved to 127.0.0.1 made their page same-origin with this API and
    every guard above became moot. Only loopback names are answered.

    That last check is skipped when the app was deliberately bound to a
    non-loopback address: `--host 0.0.0.0` is someone asking to reach this
    from another machine, and the README already says not to.
    """
    check_host = _is_loopback(_host_only(bind_host))

    def refuse(reason: str, detail: str) -> JSONResponse:
        logger.warning("Refused a request: %s", reason)
        return JSONResponse({"detail": detail}, status_code=403)

    @app.middleware("http")
    async def guard_local_only(request: Request, call_next):
        host = request.headers.get("host", "").strip().lower()
        if check_host and not _is_loopback(_host_only(host)):
            return refuse(
                f"Host header {host!r} is not this machine",
                "AIRE only answers requests addressed to localhost.")

        if request.method not in _SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).netloc.lower() != host:
                return refuse(
                    f"cross-origin {request.method} from {origin!r} to {request.url.path}",
                    "Cross-site requests to AIRE are refused.")

        return await call_next(request)


def create_app(config: AppConfig, orchestrator: Optional[Orchestrator] = None,
               bind_host: str = DEFAULT_BIND_HOST) -> FastAPI:
    """Build the FastAPI app.

    If no orchestrator is supplied one is created and started with the app,
    so `uvicorn ai_race_engineer.web_ui:app`-style usage works too.

    `bind_host` is the address uvicorn was told to listen on; it decides
    whether the Host check applies. See `_install_local_guard`.
    """
    orch = orchestrator or Orchestrator(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await orch.start()
        try:
            yield
        finally:
            await orch.stop()
            config.maybe_save()

    app = FastAPI(title="AIRE", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.orchestrator = orch
    _install_local_guard(app, bind_host)

    # Guarded on the directory actually being mounted. Asking whether the
    # panel exists and then mounting its parent is a question about the wrong
    # path: StaticFiles raises at mount time if the directory is missing.
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── UI ──────────────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        panel = UI_DIR / "panel.html"
        if not panel.exists():
            return JSONResponse(
                {"error": f"UI not found at {panel}"}, status_code=500
            )
        return FileResponse(panel)

    # ── Config ──────────────────────────────────────────────────────

    @app.get("/api/config")
    async def get_config() -> Dict[str, Any]:
        return config.redacted_dict()

    @app.post("/api/config")
    async def save_config(patch: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        config.update(patch)
        _save(config)
        orch.apply_config()
        return {"status": "ok", "path": str(config.path), "config": config.redacted_dict()}

    # ── Status, telemetry, exchange log ─────────────────────────────

    @app.get("/api/status")
    async def status() -> Dict[str, Any]:
        return orch.status()

    @app.get("/api/telemetry")
    async def telemetry() -> Dict[str, Any]:
        snapshot = await orch.snapshot_now()
        if snapshot is None:
            return {"connected": False, "telemetry": None, "age_ms": None}
        return {
            "connected": True,
            "telemetry": snapshot.to_dict(),
            # How stale the reading is, so "not live" is diagnosable.
            "age_ms": orch.snapshot_age_ms(),
            "read_ms": getattr(orch.telemetry, "last_snapshot_ms", None),
        }

    @app.get("/api/telemetry/raw")
    async def telemetry_raw(channels: str = "") -> Dict[str, Any]:
        """Dump raw sim channels, for diagnosing wrong or missing values.

        `?channels=Speed,LapLastLapTime` narrows it; otherwise the adapter's
        own curated set — everything it consumes — is returned.
        """
        wanted = [c.strip() for c in channels.split(",") if c.strip()]
        return await asyncio.to_thread(orch.telemetry.raw_channels, wanted)

    @app.get("/api/telemetry/channels")
    async def telemetry_channels(match: str = "") -> Dict[str, Any]:
        """Every channel the sim is publishing, with its current value.

        The reader consumes a hand-picked subset; this lists the whole set so
        new fields (damage, incidents, standings) can be chosen from what the
        sim actually exposes rather than from memory. `?match=damage` filters.
        """
        return await asyncio.to_thread(orch.telemetry.list_channels, match)

    @app.get("/api/telemetry/session")
    async def telemetry_session(key: str = "") -> Dict[str, Any]:
        """Dump the sim's session-info YAML.

        Live channels are only half the picture — session setup, weather
        configuration and any forecast live in this blob, not in the telemetry
        vars. `?key=WeekendInfo` narrows it; no key lists the top-level keys.
        """
        return await asyncio.to_thread(orch.telemetry.session_yaml, key)

    @app.get("/api/log")
    async def exchange_log() -> List[Dict[str, Any]]:
        return orch.history()

    # ── Ask (the pipeline, driven from the browser) ─────────────────

    @app.post("/api/ask")
    async def ask(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        question = (payload.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "A 'question' is required")
        speak = bool(payload.get("speak", True))
        result = await orch.ask(question, speak=speak, kind="text")
        # A busy pipeline answered 200 with an error body, which the panel's
        # fetch wrapper reads as success: it cleared the question box, opened
        # the exchange log and showed nothing. The typed question was gone and
        # no answer ever appeared. 409 is the state conflict this actually is.
        if "error" in result:
            raise HTTPException(409, result["error"])
        return result

    # ── LLM ─────────────────────────────────────────────────────────

    @app.get("/api/llm/models")
    async def llm_models() -> Dict[str, Any]:
        """Live model catalogue from the active provider."""
        # Closed explicitly: these clients are built per request, and an
        # abandoned one holds its TLS connection until the garbage collector
        # happens to run.
        try:
            with closing(LLMClient(config)) as client:
                return {"models": client.list_models()}
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/llm/test")
    async def llm_test() -> Dict[str, Any]:
        """Round-trip a tiny prompt to validate provider + key + model."""
        try:
            with closing(LLMClient(config)) as client:
                return client.test_connection()
        except LLMError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    # ── Audio devices ───────────────────────────────────────────────

    @app.get("/api/devices/input")
    async def input_devices() -> List[Dict[str, Any]]:
        return AudioRecorder().get_input_devices()

    @app.get("/api/devices/output")
    async def output_devices() -> List[Dict[str, Any]]:
        return AudioPlayer().get_output_devices()

    @app.post("/api/window/show")
    async def window_show() -> Dict[str, Any]:
        """Bring the desktop window up. Used by a second launch attempt."""
        controller = getattr(app.state, "desktop", None)
        if controller is None:
            return {"ok": False, "reason": "not running as a desktop app"}
        try:
            controller.show_window()
            return {"ok": True}
        except Exception as exc:
            logger.warning("Could not raise the window: %s", exc)
            return {"ok": False, "reason": str(exc)}

    @app.get("/api/ducking_devices")
    async def ducking_devices() -> List[Dict[str, Any]]:
        return orch.ducking.list_sessions()

    # ── Controllers / sim wheel ─────────────────────────────────────

    @app.get("/api/controllers")
    async def controllers() -> Dict[str, Any]:
        from .controller import ControllerHub

        hub = ControllerHub.instance()
        hub.start()
        return {
            "available": hub.available,
            "error": hub.error,
            "devices": [d.to_dict() for d in hub.devices()],
        }

    @app.post("/api/config/reset")
    async def reset_all() -> Dict[str, Any]:
        """Restore every section. Keys and the wheel binding are preserved."""
        sections = config.restore_all_defaults()
        _save(config)
        orch.apply_config()
        return {"ok": True, "sections": sections}

    @app.post("/api/config/reset/{section}")
    async def reset_section(section: str) -> Dict[str, Any]:
        """Restore one settings section. Never touches keys or the binding."""
        try:
            restored = config.restore_defaults(section)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        _save(config)
        orch.apply_config()
        return {"ok": True, "section": section, "settings": restored}

    @app.get("/api/speech_cache")
    async def speech_cache_stats() -> Dict[str, Any]:
        """Hit rate and size of the synthesized-speech cache."""
        return config.speech_cache().stats()

    @app.post("/api/speech_cache/clear")
    async def speech_cache_clear() -> Dict[str, Any]:
        """Drop every cached phrase. Used after changing voice settings."""
        removed = config.speech_cache().clear()
        return {"ok": True, "removed": removed}

    @app.post("/api/keyboard/bind")
    async def bind_key(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        """Wait for the next key or combination and bind push-to-talk to it."""
        from .input_handler import HAS_PYNPUT, describe_binding

        if not HAS_PYNPUT:
            raise HTTPException(503, "Global hotkeys unavailable on this machine")

        timeout = float(payload.get("timeout", 15.0))
        spec = await asyncio.to_thread(orch.push_to_button.capture_next_key, timeout)
        if not spec:
            return {"ok": False, "error": f"No key pressed within {timeout:.0f}s"}

        config.update({"push_to_button": {"type": "keyboard", "key": spec}})
        _save(config)
        orch.apply_config()
        return {"ok": True, "key": spec, "label": describe_binding(spec)}

    @app.post("/api/controllers/bind")
    async def bind_controller(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        """Wait for the next wheel button press and bind push-to-talk to it."""
        from .controller import ControllerHub

        timeout = float(payload.get("timeout", 15.0))
        hub = ControllerHub.instance()
        if not hub.start():
            raise HTTPException(503, hub.error or "Controller support unavailable")

        binding = await asyncio.to_thread(hub.capture_next_press, timeout)
        if binding is None:
            return {"ok": False, "error": f"No button pressed within {timeout:.0f}s"}

        config.update(
            {
                "push_to_button": {
                    "type": "controller",
                    "device_guid": binding.guid,
                    "device_name": binding.name,
                    "device_index": binding.index,
                    "button": binding.button,
                }
            }
        )
        _save(config)
        orch.apply_config()

        return {
            "ok": True,
            "device_name": binding.name,
            "device_guid": binding.guid,
            "button": binding.button,
            "description": binding.describe(),
        }

    # ── Voices ──────────────────────────────────────────────────────

    @app.get("/api/voices")
    async def voices() -> Dict[str, Any]:
        with closing(_tts(config)) as client:
            try:
                listed = client.list_voices()
            except TTSError as exc:
                raise HTTPException(502, str(exc)) from exc
            usable = sum(1 for v in listed if v["available"])
            return {
                "voices": listed,
                "tier": client.tier,
                "usable": usable,
                "blocked": len(listed) - usable,
            }

    @app.post("/api/voices/select")
    async def select_voice(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        voice_id = (payload.get("voiceId") or payload.get("voice_id") or "").strip()
        if not voice_id:
            raise HTTPException(400, "A 'voiceId' is required")
        name = payload.get("voiceName") or payload.get("name") or ""

        # Refuse a voice this account cannot synthesize, rather than letting
        # it fail with a 402 mid-session.
        with closing(_tts(config)) as client:
            try:
                match = next((v for v in client.list_voices() if v["id"] == voice_id), None)
            except TTSError:
                match = None  # cannot verify — allow, and let synthesis report it
            if match is not None and not match["available"]:
                raise HTTPException(
                    403,
                    f"'{match['name']}' is a Voice Library voice and your "
                    f"{client.tier or 'current'} plan cannot use it via the API.",
                )

        config.update(
            {"elevenlabs": {"tts_voice_id": voice_id, "tts_voice_name": name}}
        )
        config.maybe_save()
        orch.apply_config()
        return {"status": "ok", "voice_id": voice_id, "voice_name": orch.tts.voice_name}

    @app.post("/api/tts/preview")
    async def tts_preview(payload: Dict[str, Any] = Body(...)) -> Response:
        """Synthesize a sample and return playable WAV audio.

        Pass `radio: true` to hear it through the current radio-effect
        settings — the only practical way to tune them.
        """
        text = payload.get("text") or "Box this lap, box this lap. Fuel is marginal."
        voice_id = payload.get("voiceId") or payload.get("voice_id") or ""
        with closing(_tts(config)) as tts:
            if voice_id:
                tts.set_voice(voice_id)
            try:
                pcm = tts.synthesize(text)
            except TTSError as exc:
                raise HTTPException(502, str(exc)) from exc

            if payload.get("radio"):
                pcm = _apply_radio(config, pcm, tts.sample_rate)

            return Response(
                content=wav_bytes_from_pcm(pcm, tts.sample_rate),
                media_type="audio/wav",
            )

    @app.post("/api/beep")
    async def beep(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        """Play a push-to-talk blip through the app's own output device."""
        if not audio_available():
            raise HTTPException(503, "Audio output unavailable")
        closing = bool(payload.get("closing"))
        volume = float(payload.get("volume", config.audio.get("ptt_beep_volume", 0.35)))
        await asyncio.to_thread(orch.audio_player.play_beep, closing, volume)
        return {"ok": True, "closing": closing}

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        return {"status": "ok", "audio": audio_available(), "config": str(config.path)}

    return app


def _save(config: AppConfig) -> None:
    """Write the config, turning a failed write into a 500 the panel shows.

    Every endpoint that saves needs this. Two of them — the reset pair — did
    not have it, so a read-only config file made the settings appear to reset
    and silently come back on the next launch.
    """
    try:
        config.save()
    except OSError as exc:
        raise HTTPException(500, f"Could not write {config.path}: {exc}") from exc


def _apply_radio(config: AppConfig, pcm: bytes, sample_rate: int) -> bytes:
    """Run PCM through the configured radio effect, for UI previews."""
    from .audio_handler import apply_volume, float_to_pcm, pcm_to_float
    from .radio_effect import apply_radio_effect

    # Same settings the engineer is played through, from the same place —
    # a preview that quietly differs from playback is worse than none, since
    # tuning it is the only reason to press the button.
    processed = apply_radio_effect(
        pcm_to_float(pcm), sr=sample_rate, **config.radio_effect_settings())
    # Match what playback will actually sound like.
    processed = apply_volume(processed, config.audio.get("volume", 1.0))
    return float_to_pcm(processed)


def _tts(config: AppConfig) -> TTS:
    el = config.elevenlabs
    return TTS(
        config.elevenlabs_key(),
        model=el.get("tts_model", "eleven_turbo_v2_5"),
        voice_id=el.get("tts_voice_id", ""),
        voice_name=el.get("tts_voice_name", ""),
        language=config.get("language", "en"),
        voice_settings=el.get("voice_settings"),
        # Same table the orchestrator speaks through. Without it, auditioning
        # a voice bypassed the exact mechanism the table exists for — the
        # preview said the name correctly and the engineer said it wrong —
        # and the two clients keyed the speech cache differently, so a
        # preview never saved the cost of the real answer.
        #
        # The roster-derived respellings are deliberately not applied: they
        # are rebuilt per answer from whoever is on track, and there is no
        # snapshot here to build them from.
        pronunciation=el.get("pronunciation"),
        cache=config.speech_cache(),
    )
