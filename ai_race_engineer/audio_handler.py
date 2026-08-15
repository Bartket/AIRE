"""Audio recording and playback."""

import io
import logging
import re
import threading
import wave
from typing import List, Optional

logger = logging.getLogger("race-engineer")

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is a hard requirement in practice
    np = None

try:
    import sounddevice as sd
except (ImportError, OSError):
    # No PortAudio (headless box, missing system lib) — the app still boots,
    # audio calls just report themselves as unavailable.
    sd = None


class AudioUnavailable(RuntimeError):
    """Raised when audio I/O is requested but no backend is present."""


def audio_available() -> bool:
    return sd is not None and np is not None


def _require_audio() -> None:
    if np is None:
        raise AudioUnavailable("numpy is not installed")
    if sd is None:
        raise AudioUnavailable("Audio unavailable: sounddevice/PortAudio not installed")


def _device_name(raw: str) -> str:
    """A device name fit to put in a dropdown.

    Windows hands PortAudio some names as unexpanded resource references —
    "Headset (@System32\\drivers\\bthhfenum.sys,#2;%1 Hands-Free%0\r\n;(MODEL))"
    is one real example, 86 characters with a newline in the middle of it.
    Rendered in a <select> that has no width limit, one of those stretches
    the whole tab.

    The readable part is the model in the trailing parentheses, so keep
    that and drop the reference. Anything that does not match is only
    stripped of stray whitespace — guessing further would mean renaming
    devices the driver named correctly.
    """
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    # "Prefix (@resource...;(Model))" -> "Prefix (Model)"
    match = re.fullmatch(r"(.*?)\s*\(@[^()]*;\s*\((.+)\)\)", name)
    if match:
        return f"{match.group(1)} ({match.group(2)})".strip()
    return name


def _list_devices(kind: str) -> List[dict]:
    """kind is 'input' or 'output'."""
    if sd is None:
        return []
    key = f"max_{kind}_channels"
    try:
        devices = sd.query_devices()
    except Exception as exc:
        logger.warning("Could not query audio devices: %s", exc)
        return []
    return [
        {"id": i, "name": _device_name(d["name"]), "channels": d[key]}
        for i, d in enumerate(devices)
        if d.get(key, 0) > 0
    ]


class AudioRecorder:
    """Record microphone audio, either for a fixed time or press-and-hold."""

    def __init__(
        self,
        sample_rate: int = 48000,
        channels: int = 1,
        input_device_id: Optional[int] = None,
    ):
        self._samplerate = sample_rate
        self._channels = channels
        self._device_id = input_device_id
        self._stream = None
        self._frames: List["np.ndarray"] = []
        self._lock = threading.Lock()

    @property
    def samplerate(self) -> int:
        return self._samplerate

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def get_input_devices(self) -> List[dict]:
        return _list_devices("input")

    def select_device(self, input_device_id: Optional[int]) -> None:
        self._device_id = input_device_id

    # ── Press-and-hold capture ──────────────────────────────────────

    def start(self) -> None:
        """Begin capturing until `stop()` is called."""
        _require_audio()
        if self._stream is not None:
            return
        with self._lock:
            self._frames = []

        def callback(indata, _frames, _time, status):
            if status:
                logger.debug("Input stream status: %s", status)
            with self._lock:
                self._frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=self._samplerate,
            channels=self._channels,
            device=self._device_id,
            dtype="float32",
            callback=callback,
        )
        self._stream.start()

    def stop(self) -> Optional[bytes]:
        """Stop capturing and return the recording as WAV bytes."""
        if self._stream is None:
            return None
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None

        with self._lock:
            frames, self._frames = self._frames, []

        if not frames:
            return None
        audio = np.concatenate(frames, axis=0).flatten()
        return self.to_wav_bytes(audio)

    def cancel(self) -> None:
        """Abandon an in-progress recording."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._lock:
            self._frames = []

    def to_wav_bytes(self, audio: "np.ndarray") -> bytes:
        """Encode float32 samples in [-1, 1] as a 16-bit mono WAV."""
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(self._channels)
            wf.setsampwidth(2)
            wf.setframerate(self._samplerate)
            wf.writeframes(pcm.tobytes())
        return buf.getvalue()


class AudioPlayer:
    """Play audio from raw PCM, WAV bytes, or a numpy array."""

    def __init__(self, output_device_id: Optional[int] = None):
        self._device_id = output_device_id

    def get_output_devices(self) -> List[dict]:
        return _list_devices("output")

    def select_device(self, output_device_id: Optional[int]) -> None:
        self._device_id = output_device_id

    def play_array(self, audio: "np.ndarray", sample_rate: int) -> None:
        """Play float samples in [-1, 1], blocking until finished."""
        _require_audio()
        sd.play(np.clip(audio, -1.0, 1.0), samplerate=sample_rate, device=self._device_id)
        sd.wait()

    def play_async(self, audio: "np.ndarray", sample_rate: int) -> None:
        """Start playback and return immediately.

        Used for push-to-talk blips: waiting would delay the recording the
        cue is announcing.
        """
        _require_audio()
        sd.play(np.clip(audio, -1.0, 1.0), samplerate=sample_rate, device=self._device_id)

    def play_beep(self, closing: bool, volume: float = 0.35) -> None:
        """Play a push-to-talk blip. Never raises — a cue is not worth a crash."""
        if volume <= 0:
            return
        try:
            beep = make_ptt_beep(closing) * float(np.clip(volume, 0.0, 1.0))
            self.play_async(beep, BEEP_SAMPLE_RATE)
        except Exception as exc:
            logger.debug("Could not play push-to-talk beep: %s", exc)


def pcm_to_float(pcm_bytes: bytes) -> "np.ndarray":
    """Convert signed 16-bit little-endian PCM to float32 in [-1, 1]."""
    if np is None:
        raise AudioUnavailable("numpy is not installed")
    # An odd trailing byte would make frombuffer raise; drop it.
    if len(pcm_bytes) % 2:
        pcm_bytes = pcm_bytes[:-1]
    return np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0


#: Above 1.0 is a real boost. Beyond ~2x the limiter is doing so much work
#: that it starts to audibly squash, so that is the ceiling.
MAX_VOLUME = 2.0

#: Level above which the limiter starts easing peaks toward full scale.
_LIMIT_THRESHOLD = 0.70


# ── Push-to-talk blips ──────────────────────────────────────────────
#
# Real pit radio gives the driver two audible cues: a short tone as the
# mic keys open, and a "roger beep" as it closes. Without them you cannot
# tell whether the system heard you without looking at the screen — which
# is the one thing you cannot do mid-corner.

BEEP_SAMPLE_RATE = 24000


def _tone(freq: float, ms: float, sr: int, attack_ms: float = 4.0,
          release_ms: float = 12.0) -> "np.ndarray":
    """A single sine burst with click-free edges."""
    n = max(1, int(sr * ms / 1000))
    t = np.arange(n) / sr
    wave = np.sin(2 * np.pi * freq * t)

    # Raised-cosine attack/release: a hard edge would click.
    env = np.ones(n)
    a = min(int(sr * attack_ms / 1000), n // 2)
    r = min(int(sr * release_ms / 1000), n // 2)
    if a:
        env[:a] = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, a))
    if r:
        env[-r:] = 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, r))
    return (wave * env).astype(np.float32)


def make_ptt_beep(closing: bool = False, sr: int = BEEP_SAMPLE_RATE) -> "np.ndarray":
    """Mic-open cue, or the two-tone roger beep used when releasing.

    Frequencies sit inside the 300–3000 Hz comms band so the cue sounds
    like it came over the same radio as the engineer.
    """
    if not closing:
        # Opening: one clean mid tone, deliberately short.
        return _tone(1180, 85, sr) * 0.9

    # Closing: the classic descending "over" pair.
    gap = np.zeros(int(sr * 0.012), dtype=np.float32)
    return np.concatenate([_tone(1180, 55, sr), gap, _tone(880, 70, sr)]) * 0.9


def apply_volume(audio: "np.ndarray", volume: float) -> "np.ndarray":
    """Scale playback gain. 1.0 is unchanged, 0.0 silent, up to 2.0 boosts.

    Above unity a soft limiter eases peaks toward full scale instead of
    letting them clip, so boosting raises perceived loudness rather than
    just adding distortion.
    """
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        return audio
    volume = float(np.clip(volume, 0.0, MAX_VOLUME))

    if abs(volume - 1.0) < 1e-3:
        return audio
    if volume <= 1.0:
        return audio * volume

    out = np.asarray(audio, dtype=np.float32) * volume
    magnitude = np.abs(out)
    over = magnitude > _LIMIT_THRESHOLD
    if over.any():
        headroom = 1.0 - _LIMIT_THRESHOLD
        excess = magnitude[over] - _LIMIT_THRESHOLD
        # tanh approaches 1 asymptotically, so this never exceeds full scale.
        out[over] = np.sign(out[over]) * (
            _LIMIT_THRESHOLD + headroom * np.tanh(excess / headroom)
        )
    return out


def float_to_pcm(audio: "np.ndarray") -> bytes:
    """Convert float32 samples in [-1, 1] to signed 16-bit PCM bytes."""
    if np is None:
        raise AudioUnavailable("numpy is not installed")
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def wav_bytes_from_pcm(pcm_bytes: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw 16-bit PCM in a WAV container (used to serve UI previews)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()
