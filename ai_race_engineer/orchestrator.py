"""Orchestrator: the loop that connects push-to-talk → STT → LLM → TTS → speakers."""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from .audio_handler import (
    AudioPlayer,
    AudioRecorder,
    AudioUnavailable,
    apply_volume,
    audio_available,
    pcm_to_float,
)
from . import keyterms
from .config import AppConfig
from .ducking import AudioDuck
from .input_handler import InputUnavailable, PushToButton
from .llm import COMMS_FAILURE, NO_ANSWER, LLMClient
from .pit_commands import (PitCommandError, PitCommandPlan,
                           is_pit_command, might_be_pit_instruction,
                           parse_pit_command, pit_amounts_match)
from .prompt_builder import PromptBuilder
from .stt import STT, STTError, Transcription, should_repeat
from .telemetry import TelemetrySnapshot, create_telemetry_adapter
from .text import normalise_question
from . import pronounce
from .tts import TTS, TTSError

logger = logging.getLogger("race-engineer")

# Ignore accidental taps of the push-to-talk button.
_MIN_RECORDING_SECONDS = 0.35
_TRACE_SECONDS = 60.0
_TRACE_MAX_BYTES = 8 * 1024 * 1024
_PIT_CONFIRM_SECONDS = 30.0
_REPEAT_REQUESTS = {
    "say again", "say that again", "repeat", "repeat that", "repeat last",
    "repeat the last one", "what did you say", "come again",
    "can you repeat that", "could you repeat that",
    "try that again", "that was gibberish", "you sounded like gibberish",
    "voice glitched", "your voice glitched", "audio glitched", "bad audio",
    "you glitched out",
}


def _is_repeat_request(text: str) -> bool:
    return normalise_question(text) in _REPEAT_REQUESTS


def _irsdk_available() -> bool:
    """Whether the iRacing SDK could be imported at all (Windows only)."""
    try:
        from .telemetry.irsdk_reader import HAS_IRSDK

        return bool(HAS_IRSDK)
    except Exception:
        return False


class Orchestrator:
    """Owns the components and runs one push-to-talk exchange at a time."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._busy = threading.Lock()
        self._press_started_at: Optional[float] = None
        # Guards the press marker alone. Two threads end a recording — the
        # button listener on release, and the cutoff timer — and the marker
        # is what decides which of them owns the hand-off. Read and cleared
        # without this, both could pass the guard on the same press, both
        # would release `_busy`, and the spare release frees a lock a later
        # press is holding.
        self._press_lock = threading.Lock()
        # Watchdog that ends a recording that has run too long.
        self._cutoff: Optional[threading.Timer] = None
        self._status = "stopped"
        self._last_error: Optional[str] = None
        self._last_spoken_text: Optional[str] = None
        # Kept apart from _last_error so a hotkey failure can be cleared by a
        # later successful rebind without wiping an unrelated error.
        self._hotkey_error: Optional[str] = None
        self._snapshot: Optional[TelemetrySnapshot] = None
        self._snapshot_at: float = 0.0
        self._session_key: Optional[tuple] = None
        self._pending_pit_command: Optional[PitCommandPlan] = None
        self._pending_pit_command_at: float = 0.0
        self._trace_samples: Deque[Dict[str, Any]] = deque(
            maxlen=self._trace_maxlen())
        self._trace_enabled = bool(config.telemetry.get("diagnostic_trace", False))
        self._trace_path = config.path.parent / "diagnostic-trace.jsonl"

        el = config.elevenlabs
        audio_cfg = config.audio

        # Components
        self._telemetry_source = config.telemetry.get("source", "auto")
        self._fuel_units = config.telemetry.get("fuel_units", "auto")
        self._units = config.telemetry.get("units", "auto")
        self.telemetry = create_telemetry_adapter(
            self._telemetry_source, fuel_units=self._fuel_units,
            units=self._units)
        self.audio_player = AudioPlayer(audio_cfg.get("output_device"))
        self.audio_recorder = AudioRecorder(
            sample_rate=audio_cfg.get("sample_rate", 48000),
            input_device_id=audio_cfg.get("input_device"),
        )
        self.stt = STT(
            config.elevenlabs_key(),
            model=el.get("stt_model", "scribe_v1"),
            language=config.get("language", "en"),
        )
        self.tts = TTS(
            config.elevenlabs_key(),
            model=el.get("tts_model", "eleven_turbo_v2_5"),
            voice_id=el.get("tts_voice_id", ""),
            voice_name=el.get("tts_voice_name", ""),
            language=config.get("language", "en"),
            voice_settings=el.get("voice_settings"),
            pronunciation=el.get("pronunciation"),
            cache=config.speech_cache(),
        )
        self.prompt_builder = PromptBuilder(config)
        self.llm = LLMClient(config, self.prompt_builder)
        self.push_to_button = PushToButton(config)
        self.push_to_button.on_press = self._on_button_down
        self.push_to_button.on_release = self._on_button_up
        self.ducking = AudioDuck(config)

        # Rolling log of exchanges, surfaced by the web UI.
        self._history: Deque[Dict[str, Any]] = deque(maxlen=max(1, config.get("memory_log", 20)))

    # ── Settings read live, never cached ────────────────────────────

    def _poll_interval(self) -> float:
        """How often to refresh the snapshot, in seconds.

        A method rather than a stored value because the panel can change it
        mid-session, and a bad value must not take the poll loop down: this
        is read on every iteration now.
        """
        try:
            return max(0.02, float(self.config.telemetry.get("poll_interval", 0.1)))
        except (TypeError, ValueError):
            return 0.1

    def _trace_maxlen(self) -> int:
        """Samples needed to cover `_TRACE_SECONDS` at the current poll rate."""
        return max(10, int(_TRACE_SECONDS / self._poll_interval()) + 2)

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the push-to-talk listener and the telemetry poll loop."""
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._status = "listening"

        try:
            self.push_to_button.start()
        except InputUnavailable as exc:
            # Not fatal: the web UI's "Ask" box still drives the full pipeline.
            #
            # Deliberately does NOT touch _status. That field says what the
            # pipeline is doing — listening, recording, thinking — and writing
            # a hotkey condition into it produced a status bar that read
            # "no-hotkey" next to a working hotkey. A sim wheel is enumerated
            # asynchronously, so failing here and binding a second later is
            # normal; the hotkey badge reads the live binding and was right
            # all along, while this string was a snapshot that never updated.
            self._last_error = self._hotkey_error = str(exc)
            logger.warning("%s — use the web UI to ask questions instead", exc)

        if not audio_available():
            logger.warning("Audio I/O unavailable — voice capture and playback are disabled")

        self._task = asyncio.create_task(self._telemetry_loop())
        logger.info(
            "Orchestrator started (telemetry=%s, llm=%s/%s, ptt=%s)",
            self.telemetry.name,
            self.llm.provider,
            self.llm.model,
            self.push_to_button.description,
        )

    async def stop(self) -> None:
        self._running = False
        self._status = "stopped"
        self._clear_pending_pit_command()
        self.push_to_button.stop()
        # A pending cutoff would otherwise fire into a stopped orchestrator.
        self._cancel_cutoff()
        self._press_started_at = None
        self.audio_recorder.cancel()
        # Belt-and-suspenders restoration if shutdown lands between duck()
        # and the playback finally block.
        self.ducking.un_duck()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.telemetry.shutdown()
        logger.info("Orchestrator stopped")

    async def _telemetry_loop(self) -> None:
        """Keep the latest snapshot warm so questions answer without a stall."""
        while self._running:
            # Read per iteration, not once before the loop: read once, a new
            # poll interval saved in the panel showed there and did nothing
            # until the app was restarted.
            interval = self._poll_interval()
            try:
                self._snapshot = await asyncio.to_thread(self.telemetry.get_telemetry_snapshot)
                self._snapshot_at = time.monotonic()
                self._adopt_session(self._snapshot)
                self._remember_trace_sample(self._snapshot)
            except Exception as exc:
                logger.debug("Telemetry poll failed: %s", exc)
                self._snapshot = None
            await asyncio.sleep(interval)

    # ── Status for the web UI ───────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    def get_snapshot(self) -> Optional[TelemetrySnapshot]:
        snapshot = self._snapshot
        if snapshot is None:
            # Not started, or first poll hasn't landed yet.
            try:
                snapshot = self.telemetry.get_telemetry_snapshot()
            except Exception:
                snapshot = None
        return snapshot

    async def snapshot_now(self) -> Optional[TelemetrySnapshot]:
        """`get_snapshot()` for callers already on the event loop.

        The warm path is an attribute read and costs nothing. The cold one —
        before the first poll lands, or right after a source swap — is a
        blocking SDK read, and every other call into the reader goes through
        a thread for exactly that reason.
        """
        snapshot = self._snapshot
        if snapshot is not None:
            return snapshot
        return await asyncio.to_thread(self.get_snapshot)

    def _adopt_session(self, snapshot: Optional[TelemetrySnapshot]) -> None:
        """Keep model dialogue inside the session where it was spoken."""
        if snapshot is None:
            return
        track = snapshot.track
        key = (
            track.subsession_id,
            track.session_id,
            track.session_num,
            track.session_generation,
        )
        if self._session_key is None:
            self._session_key = key
            return
        if key == self._session_key:
            return
        logger.info("Session segment changed — clearing conversational memory")
        self._session_key = key
        self._clear_pending_pit_command()
        self.llm.reset_history()

    def snapshot_age_ms(self) -> Optional[float]:
        """Age of the cached telemetry reading, in milliseconds."""
        if self._snapshot is None or not self._snapshot_at:
            return None
        return (time.monotonic() - self._snapshot_at) * 1000

    def status(self) -> Dict[str, Any]:
        settings = self.config.llm_settings()
        return {
            "running": self._running,
            "state": self._status,
            "error": self._last_error,
            # What was asked for vs what is actually running — they differ
            # when "auto" falls back, or iRacing simply is not running.
            "telemetry_requested": self._telemetry_source,
            "telemetry_source": self.telemetry.name,
            "telemetry_connected": self.telemetry.is_connected(),
            "irsdk_available": _irsdk_available(),
            "telemetry_error": getattr(self.telemetry, "last_error", None),
            "llm_provider": settings["provider"],
            "llm_model": settings["model"],
            "llm_key_set": bool(settings["api_key"]),
            # A local server needs no key, so "configured" is not "has a key".
            "llm_ready": bool(settings["api_key"]) or settings["provider"] != "openrouter",
            "stt_configured": self.stt.configured,
            "tts_configured": self.tts.configured,
            "audio_available": audio_available(),
            "hotkey_available": self.push_to_button.available,
            "hotkey": self.push_to_button.description,
            "diagnostic_trace": self._trace_enabled,
            "diagnostic_trace_path": str(self._trace_path) if self._trace_enabled else None,
            "pit_commands_enabled": (
                self.config.get("pit_commands", {}).get("enabled") is True),
            "pit_command_pending": self._pending_pit_command is not None,
        }

    def history(self) -> List[Dict[str, Any]]:
        return list(self._history)

    def _log_exchange(self, question: str, answer: str, kind: str = "voice") -> Dict[str, Any]:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": kind,
            "question": question,
            "answer": answer,
        }
        self._history.append(entry)
        return entry

    def _remember_trace_sample(self, snapshot: Optional[TelemetrySnapshot]) -> None:
        """Keep one bounded minute of the readings behind strategy answers."""
        if not self._trace_enabled or snapshot is None:
            return
        car, track = snapshot.car, snapshot.track

        def rival(value):
            if value is None:
                return None
            return {
                "name": value.name,
                "car_number": value.car_number,
                "gap_seconds": value.gap,
                "gap_metres": value.gap_metres,
                "timing_gap_seconds": value.trend_gap,
            }

        self._trace_samples.append({
            "monotonic": round(time.monotonic(), 3),
            "session_num": track.session_num,
            "session_type": track.session_type,
            "session_state": track.session_state,
            "lap": track.current_lap,
            "lap_progress": round(car.lap_progress, 5),
            "speed": round(car.speed, 2),
            "fuel_level": car.fuel_level,
            "fuel_unit": car.fuel_unit,
            "fuel_per_lap": car.fuel_per_lap,
            "fuel_burn_samples": car.fuel_burn_samples,
            "fuel_burn_min": car.fuel_burn_min,
            "fuel_burn_max": car.fuel_burn_max,
            "ahead": rival(car.ahead),
            "behind": rival(car.behind),
            "ahead_trend": {
                "status": car.closing_ahead_status,
                "change_seconds": car.closing_ahead_change,
                "mini_sectors": car.closing_ahead_segments,
            },
            "behind_trend": {
                "status": car.closing_behind_status,
                "change_seconds": car.closing_behind_change,
                "mini_sectors": car.closing_behind_segments,
            },
        })

    def _write_diagnostic_trace(
        self, question: str, answer: str, kind: str,
        snapshot: Optional[TelemetrySnapshot] = None,
        transcription: Optional[Transcription] = None,
        route: Optional[str] = None,
    ) -> None:
        """Append one exchange and its preceding minute, rotating at 8 MB."""
        if not self._trace_enabled:
            return
        now = time.monotonic()
        samples = []
        for stored in self._trace_samples:
            sample = dict(stored)
            sample["age_seconds"] = round(now - sample.pop("monotonic"), 2)
            samples.append(sample)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": kind,
            "question": question,
            "answer": answer,
            "route": route,
            "transcription": (None if transcription is None else {
                "language_code": transcription.language_code,
                "language_probability": transcription.language_probability,
                "mean_word_logprob": transcription.mean_word_logprob,
            }),
            "snapshot": None if snapshot is None else snapshot.to_dict(),
            "samples": samples,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            self._trace_path.parent.mkdir(parents=True, exist_ok=True)
            if (self._trace_path.exists()
                    and self._trace_path.stat().st_size + len(line.encode("utf-8"))
                    > _TRACE_MAX_BYTES):
                self._trace_path.replace(Path(str(self._trace_path) + ".1"))
            with open(self._trace_path, "a", encoding="utf-8") as trace:
                trace.write(line)
        except OSError as exc:
            logger.warning("Could not write diagnostic telemetry trace: %s", exc)

    # ── Push-to-talk handling ───────────────────────────────────────

    @property
    def _toggle_mode(self) -> bool:
        """Press once to open the channel, once more to close it."""
        return str(self.config.push_to_button.get("mode", "hold")).lower() == "toggle"

    def _on_button_down(self) -> None:
        """Called from the listener thread when the button goes down."""
        # In toggle mode a press while recording is the sign-off, not a
        # second question.
        if self._toggle_mode and self._press_started_at is not None:
            self._finish_recording()
            return

        if not audio_available():
            logger.warning("Push-to-talk pressed but audio capture is unavailable")
            return
        if not self._busy.acquire(blocking=False):
            logger.debug("Still handling the previous question — ignoring press")
            return
        try:
            self.audio_recorder.start()
            with self._press_lock:
                self._press_started_at = time.monotonic()
            self._status = "recording"
            self._beep(closing=False)
            self._arm_cutoff()
            logger.info("Recording…%s", " (press again to send)" if self._toggle_mode else "")
        except (AudioUnavailable, Exception) as exc:
            logger.error("Could not start recording: %s", exc)
            with self._press_lock:
                self._press_started_at = None
            self._busy.release()

    def _on_button_up(self) -> None:
        """Called on release. In toggle mode the release means nothing."""
        if self._toggle_mode:
            return
        self._finish_recording()

    def _arm_cutoff(self) -> None:
        """Stop recording by itself after the configured limit.

        Without this a stuck button — or a forgotten second press in toggle
        mode — records until the disk fills, then sends a stint of audio to
        be transcribed at a cost per minute.
        """
        self._cancel_cutoff()
        try:
            limit = float(self.config.audio.get("max_record_seconds", 30.0))
        except (TypeError, ValueError):
            limit = 30.0
        if limit <= 0:
            return
        self._cutoff = threading.Timer(limit, self._on_cutoff)
        self._cutoff.daemon = True
        self._cutoff.start()

    def _cancel_cutoff(self) -> None:
        timer, self._cutoff = self._cutoff, None
        if timer is not None:
            timer.cancel()

    def _on_cutoff(self) -> None:
        """The recording ran past its limit — send what we have."""
        if self._press_started_at is None:
            return
        logger.info("Recording hit the %.0fs limit — sending what we have",
                    float(self.config.audio.get("max_record_seconds", 30.0)))
        self._finish_recording()

    def _finish_recording(self) -> None:
        """Stop recording and hand the audio to the pipeline.

        Claiming the press marker is the whole point of the lock: a release
        and an expiring cutoff can land on the same press, and `cancel()`
        cannot stop a timer that is already inside its callback. Whichever
        thread takes the marker owns the recording and the `_busy` release;
        the other finds None and leaves.
        """
        self._cancel_cutoff()
        with self._press_lock:
            started, self._press_started_at = self._press_started_at, None
        if started is None:
            return
        held = time.monotonic() - started

        try:
            wav_bytes = self.audio_recorder.stop()
        except Exception as exc:
            logger.error("Could not stop recording: %s", exc)
            self._status = "listening"
            self._busy.release()
            return

        self._beep(closing=True)

        if held < _MIN_RECORDING_SECONDS or not wav_bytes:
            logger.info("Recording too short (%.2fs) — discarded", held)
            self._status = "listening"
            self._busy.release()
            return

        # Hop back onto the event loop; the lock is released by the task.
        if self._loop is None or not self._loop.is_running():
            self._busy.release()
            return
        asyncio.run_coroutine_threadsafe(self._handle_recording(wav_bytes), self._loop)

    def _beep(self, closing: bool) -> None:
        """Radio blip marking the mic opening or closing."""
        audio_cfg = self.config.audio
        if not audio_cfg.get("ptt_beep", True) or not audio_available():
            return
        self.audio_player.play_beep(closing, float(audio_cfg.get("ptt_beep_volume", 0.35)))

    async def _handle_recording(self, wav_bytes: bytes) -> None:
        try:
            # Bias recognition toward the drivers and numbers actually on
            # track, so a surname the model has never seen still lands.
            # Never let hint-building break transcription: a malformed term
            # would otherwise take push-to-talk down entirely.
            try:
                hints = keyterms.build(await self.snapshot_now())
            except Exception:
                logger.exception("Could not build speech keyterms")
                hints = None
            try:
                transcription = await asyncio.to_thread(
                    self.stt.transcribe_raw_result, wav_bytes, "audio.wav", hints)
            except STTError:
                if not hints:
                    raise
                # The API rejects the whole request if one term is invalid.
                # Falling back unbiased still gets the driver an answer.
                logger.warning("Transcription rejected the keyterms; retrying without")
                transcription = await asyncio.to_thread(
                    self.stt.transcribe_raw_result, wav_bytes, "audio.wav", None)
        except STTError as exc:
            logger.error("Transcription failed: %s", exc)
            self._last_error = str(exc)
            self._status = "listening"
            self._busy.release()
            return
        except Exception:
            logger.exception("Transcription crashed")
            self._status = "listening"
            self._busy.release()
            return

        try:
            question = transcription.text
            if not question.strip():
                logger.info("Nothing was said — skipping")
                return
            if should_repeat(transcription):
                logger.info(
                    "Low-confidence off-domain transcript rejected: %r (word logprob=%s, "
                    "language probability=%s)", question,
                    transcription.mean_word_logprob,
                    transcription.language_probability,
                )
                self._log_exchange(question, NO_ANSWER, "voice")
                self._write_diagnostic_trace(
                    question, NO_ANSWER, "voice", await self.snapshot_now(),
                    transcription, "stt_guard")
                if self.tts.configured and audio_available():
                    self._status = "speaking"
                    await self._speak(NO_ANSWER)
                return
            logger.info("Question: %r", question)
            await self.ask(question, speak=True, kind="voice", _hold_lock=True,
                           _transcription=transcription)
        finally:
            self._status = "listening"
            self._busy.release()

    # ── The pipeline ────────────────────────────────────────────────

    async def ask(
        self,
        question: str,
        speak: bool = True,
        kind: str = "text",
        _hold_lock: bool = False,
        _transcription: Optional[Transcription] = None,
    ) -> Dict[str, Any]:
        """Answer a question end-to-end. Also the web UI's entry point."""
        acquired = False
        if not _hold_lock:
            acquired = self._busy.acquire(blocking=False)
            if not acquired:
                return {"error": "Busy handling another question"}

        try:
            self._status = "thinking"
            if _is_repeat_request(question):
                # Deliberately returns before `_handle_pit_command`, so a
                # pending command survives a "say again" — and deliberately
                # does not touch `_pending_pit_command_at`, so the 30 s it
                # has left does not grow.
                #
                # This is the one turn that carries no new intent: what it
                # re-speaks *is* the read-back, so "confirm" is still the
                # word that follows it. Clearing here would answer the
                # read-back with "no pit command is waiting", which is a
                # worse failure than the one it would prevent — a repeat
                # that was itself misheard leaves the write armed for the
                # remainder of a window that is already running out.
                previous = self._last_spoken_text
                answer = previous or "I've got nothing to repeat yet."
                entry = self._log_exchange(question, answer, kind)
                self._write_diagnostic_trace(
                    question, answer, kind, None, _transcription, "repeat")
                logger.info("Repeat request: %r", answer)
                self._last_error = None
                if speak and self.tts.configured and audio_available():
                    self._status = "speaking"
                    await self._speak(answer, regenerate=previous is not None)
                elif speak:
                    logger.info("Not speaking: TTS key or audio device unavailable")
                return entry

            snapshot = await self.snapshot_now()
            self._adopt_session(snapshot)
            self._remember_trace_sample(snapshot)

            pit_result = await self._handle_pit_command(question)
            if pit_result is not None:
                answer, route = pit_result
                entry = self._log_exchange(question, answer, kind)
                self._write_diagnostic_trace(
                    question, answer, kind, snapshot, _transcription, route)
                logger.info("Pit command route %s: %r", route, answer)
                self._last_error = None
                if speak and self.tts.configured and audio_available():
                    self._status = "speaking"
                    await self._speak(answer)
                elif speak:
                    logger.info("Not speaking: TTS key or audio device unavailable")
                return entry

            answer = await asyncio.to_thread(self.llm.generate, question, snapshot)
            entry = self._log_exchange(question, answer, kind)
            self._write_diagnostic_trace(
                question, answer, kind, snapshot, _transcription,
                self.llm.last_route)
            logger.info("Answer: %r", answer)

            # generate() never raises, so the fallback text is the only signal
            # that the model call failed — surface it to the status bar.
            if answer == COMMS_FAILURE:
                self._last_error = "LLM call failed — check the provider settings"
            else:
                self._last_error = None

            if speak and self.tts.configured and audio_available():
                self._status = "speaking"
                await self._speak(answer)
            elif speak:
                logger.info("Not speaking: TTS key or audio device unavailable")

            return entry
        finally:
            self._status = "listening" if self._running else "stopped"
            if acquired:
                self._busy.release()

    def _clear_pending_pit_command(self) -> None:
        self._pending_pit_command = None
        self._pending_pit_command_at = 0.0

    async def _handle_pit_command(
        self, question: str
    ) -> Optional[tuple[str, str]]:
        """Interpret, propose, or commit a pit write behind confirmation."""
        control = normalise_question(question)
        pending = self._pending_pit_command
        if pending is not None and (
                time.monotonic() - self._pending_pit_command_at
                > _PIT_CONFIRM_SECONDS):
            self._clear_pending_pit_command()
            pending = None

        enabled = self.config.get("pit_commands", {}).get("enabled") is True
        if control == "confirm":
            if pending is None:
                return "No pit command is waiting for confirmation.", "pit_command:none"
            self._clear_pending_pit_command()
            if not enabled:
                return "Pit commands were disabled; nothing was changed.", "pit_command:disabled"
            ready, reason = self.telemetry.pit_commands_ready()
            if not ready:
                return f"I couldn't set the pit stop: {reason}.", "pit_command:not_ready"
            applied, reason = await asyncio.to_thread(
                self.telemetry.apply_pit_commands, pending.commands)
            if not applied:
                return f"I couldn't verify the pit stop: {reason}.", "pit_command:unverified"
            return "Pit stop set.", "pit_command:committed"

        if control == "cancel" and pending is not None:
            self._clear_pending_pit_command()
            return "Pit command cancelled; nothing was changed.", "pit_command:cancelled"

        explicit = is_pit_command(question)
        if not explicit and not might_be_pit_instruction(question):
            # Confirmation is single-use and must immediately follow its
            # read-back. Any other turn makes the held write unreachable —
            # except a repeat request, which never reaches here (see `ask`)
            # because re-speaking the read-back is not another turn.
            if pending is not None:
                self._clear_pending_pit_command()
            return None

        self._clear_pending_pit_command()
        compounds = self.telemetry.pit_tire_compounds()
        command_text = question
        plan = None
        if explicit:
            try:
                plan = parse_pit_command(command_text, compounds)
            except PitCommandError:
                # The prefix proves write intent, but the rest may still be
                # ordinary human phrasing rather than the canonical grammar.
                pass
        if plan is None:
            interpretation = await asyncio.to_thread(
                self.llm.interpret_pit_command,
                question,
                [compound.name for compound in compounds],
            )
            if interpretation.kind == "not_command":
                return None
            if interpretation.kind == "ambiguous":
                return (
                    "I couldn't safely interpret that pit request; nothing was changed.",
                    "pit_command:ambiguous",
                )
            if interpretation.kind != "command":
                return (
                    "I couldn't interpret that pit request; nothing was changed.",
                    "pit_command:interpretation_failed",
                )
            command_text = interpretation.command
            if not pit_amounts_match(question, command_text):
                return (
                    "I couldn't safely preserve the pit amounts; nothing was changed.",
                    "pit_command:amount_mismatch",
                )
            try:
                plan = parse_pit_command(command_text, compounds)
            except PitCommandError as exc:
                return str(exc), "pit_command:rejected"
        if plan is None:
            return "I couldn't safely interpret that pit request.", "pit_command:rejected"
        if not enabled:
            return (
                "Pit commands are off; enable the at-your-own-risk setting first.",
                "pit_command:disabled",
            )
        ready, reason = self.telemetry.pit_commands_ready()
        if not ready:
            return f"I couldn't arm that command: {reason}.", "pit_command:not_ready"

        self._pending_pit_command = plan
        self._pending_pit_command_at = time.monotonic()
        return (
            f"Confirm: {plan.summary}; say confirm to commit.",
            "pit_command:confirmation",
        )

    async def _speak(self, text: str, regenerate: bool = False) -> bool:
        """Synthesize, apply the radio effect, duck other audio, and play."""
        self._refresh_spoken_names()
        try:
            pcm = await asyncio.to_thread(self.tts.synthesize, text, regenerate)
        except TTSError as exc:
            logger.error("Speech synthesis failed: %s", exc)
            self._last_error = str(exc)
            return False

        self._last_spoken_text = text

        audio = pcm_to_float(pcm)
        audio = self._apply_radio_effect(audio, self.tts.sample_rate)
        audio = apply_volume(audio, self.config.audio.get("volume", 1.0))

        # Off the loop like everything either side of it: duck() enumerates
        # every WASAPI session and queries an interface per match, and doing
        # that inline stalls the telemetry poll and every HTTP handler at the
        # exact moment the driver is being spoken to.
        ducked = False
        if self.config.ducking.get("enabled", True):
            ducked = await asyncio.to_thread(self.ducking.duck)
        try:
            await asyncio.to_thread(self.audio_player.play_array, audio, self.tts.sample_rate)
        except Exception as exc:
            logger.error("Playback failed: %s", exc)
            self._last_error = f"Playback failed: {exc}"
            return False
        finally:
            if ducked:
                await asyncio.to_thread(self.ducking.un_duck)
        return True

    def _refresh_spoken_names(self) -> None:
        """Rebuild the roster-derived respellings for the grid on track.

        iRacing has hundreds of thousands of drivers, so nothing curated
        can cover the names that break the voice. This derives them from
        whoever is actually in this session instead, every time — a name
        that looks risky is spoken as its car number, which is what a real
        engineer says half the time regardless.

        Cheap enough to redo per answer (a few dozen names), and doing it
        per answer means it cannot go stale when the field changes.
        """
        snapshot = self._snapshot
        field = getattr(getattr(snapshot, "track", None), "field", None) or []
        policy = self.config.elevenlabs.get("name_policy", "auto")
        try:
            self.tts.set_auto_pronunciation(pronounce.auto_table(field, policy))
        except Exception:                             # noqa: BLE001
            # A respelling is a nicety; never let it stop the radio.
            logger.debug("Could not build spoken-name table", exc_info=True)

    def _apply_radio_effect(self, audio, sample_rate: int):
        """Band-pass + saturation so it sounds like team radio, not a podcast."""
        settings = self.config.radio_effect
        if not settings.get("enabled", True):
            return audio
        try:
            from .radio_effect import apply_radio_effect
        except ImportError as exc:  # scipy missing
            logger.debug("Radio effect unavailable: %s", exc)
            return audio
        try:
            return apply_radio_effect(
                audio, sr=sample_rate, **self.config.radio_effect_settings())
        except Exception as exc:
            logger.warning("Radio effect failed, playing dry: %s", exc)
            return audio

    # ── Runtime reconfiguration (called after the UI saves) ──────────

    def apply_config(self) -> None:
        """Re-read config into the live components without a restart."""
        cfg = self.config
        el = cfg.elevenlabs
        if cfg.get("pit_commands", {}).get("enabled") is not True:
            self._clear_pending_pit_command()

        self.stt = STT(
            cfg.elevenlabs_key(),
            model=el.get("stt_model", "scribe_v1"),
            language=cfg.get("language", "en"),
        )
        self.tts = TTS(
            cfg.elevenlabs_key(),
            model=el.get("tts_model", "eleven_turbo_v2_5"),
            voice_id=el.get("tts_voice_id", ""),
            voice_name=el.get("tts_voice_name", ""),
            language=cfg.get("language", "en"),
            voice_settings=el.get("voice_settings"),
            pronunciation=el.get("pronunciation"),
            # Was missing here: every settings save rebuilt the client
            # without a cache, so speech silently stopped being reused
            # for the rest of the run and every repeat was paid for again.
            cache=cfg.speech_cache(),
        )
        self.audio_recorder.select_device(cfg.audio.get("input_device"))
        self.audio_player.select_device(cfg.audio.get("output_device"))

        # Swap the telemetry adapter when anything it was built with changes.
        # Without this a change in the UI silently did nothing until the app
        # was restarted — which cost an evening the first time round.
        new_source = cfg.telemetry.get("source", "auto")
        new_fuel_units = cfg.telemetry.get("fuel_units", "auto")
        new_units = cfg.telemetry.get("units", "auto")
        self._trace_enabled = bool(cfg.telemetry.get("diagnostic_trace", False))
        self._trace_path = cfg.path.parent / "diagnostic-trace.jsonl"
        if not self._trace_enabled:
            self._trace_samples.clear()

        # Both deques fix their length at construction, so a saved change to
        # either setting used to show in the panel and do nothing until the
        # app was restarted. Rebuilt around the existing contents, keeping the
        # newest — a longer trace is not worth throwing the current one away.
        history_len = max(1, cfg.get("memory_log", 20))
        if history_len != self._history.maxlen:
            self._history = deque(self._history, maxlen=history_len)
        trace_len = self._trace_maxlen()
        if trace_len != self._trace_samples.maxlen:
            self._trace_samples = deque(self._trace_samples, maxlen=trace_len)
        if (new_source != self._telemetry_source
                or new_fuel_units != self._fuel_units
                or new_units != self._units):
            previous = self.telemetry
            self.telemetry = create_telemetry_adapter(
                new_source, fuel_units=new_fuel_units, units=new_units)
            self._telemetry_source = new_source
            self._fuel_units = new_fuel_units
            self._units = new_units
            self._snapshot = None  # drop readings from the old source
            self._session_key = None
            self.llm.reset_history()
            try:
                previous.shutdown()
            except Exception as exc:
                logger.debug("Could not shut down previous telemetry source: %s", exc)
            logger.info("Telemetry source switched to %s (%s)", new_source, self.telemetry.name)

        # Rebind the hotkey if it changed. The comparison is against what the
        # live object is actually listening to, never against config, because
        # `description` is the only thing that knows which of the two it is.
        #
        # This used to be guarded on the *outgoing* binding being `available`,
        # which asks the wrong object the wrong question: a wheel that was
        # unplugged, switched off or never bound answers no, and the rebind
        # was then skipped entirely — so switching back to a keyboard key did
        # nothing at all until the app was restarted, while the settings panel
        # happily showed the key.
        replacement = PushToButton(cfg)
        if replacement.description != self.push_to_button.description:
            self.push_to_button.stop()
            replacement.on_press = self._on_button_down
            replacement.on_release = self._on_button_up
            self.push_to_button = replacement
            if self._running:
                try:
                    replacement.start()
                except InputUnavailable as exc:
                    # Only logged before. Selecting the wheel without binding a
                    # button is a fair thing to refuse, but it left push-to-talk
                    # dead with nothing on screen saying so.
                    logger.warning("Could not rebind hotkey: %s", exc)
                    self._last_error = self._hotkey_error = str(exc)
                else:
                    # Clear the failure the previous binding left behind, or
                    # correcting it would still read as broken until restart.
                    if self._last_error == self._hotkey_error:
                        self._last_error = None
                    self._hotkey_error = None

        logger.info("Configuration reloaded")
