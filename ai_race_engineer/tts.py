"""Text-to-speech via the ElevenLabs API."""

import logging
from typing import Any, Dict, List, Optional

import httpx

from . import pronounce
from .tts_cache import TTSCache, cache_key

logger = logging.getLogger("race-engineer")

# ElevenLabs stock voice ("Rachel"), used only when no voice has been picked
# yet so a fresh install can still produce audio.
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

# Tiers whose API refuses Voice Library voices, even when the voice owner
# has allowed free users. Confirmed against a live free account: HTTP 402
# "Free users cannot use library voices via the API."
LIBRARY_BLOCKED_TIERS = {"free"}

# Raw 16-bit mono PCM: no decoder needed, and it feeds the radio effect and
# sounddevice directly.
PCM_SAMPLE_RATE = 24000
_OUTPUT_FORMAT = f"pcm_{PCM_SAMPLE_RATE}"
_MIN_PCM_BYTES = PCM_SAMPLE_RATE * 2 // 10  # one tenth of a second


class TTSError(RuntimeError):
    """Speech synthesis failed."""


def _validate_pcm(pcm: bytes) -> None:
    """Reject transport-level failures before they can be played or cached."""
    if len(pcm) < _MIN_PCM_BYTES:
        raise TTSError("ElevenLabs TTS returned truncated audio")
    if len(pcm) % 2:
        raise TTSError("ElevenLabs TTS returned malformed PCM audio")
    if not any(pcm):
        raise TTSError("ElevenLabs TTS returned silent audio")


class TTS:
    """ElevenLabs text-to-speech synthesis."""

    def __init__(
        self,
        api_key: str,
        model: str = "eleven_turbo_v2_5",
        voice_id: str = "",
        voice_name: str = "",
        voice_settings: Optional[Dict[str, Any]] = None,
        cache: Optional["TTSCache"] = None,
        pronunciation: Optional[Dict[str, str]] = None,
        language: str = "en",
    ):
        self._api_key = api_key
        self._model = model
        self._voice_id = voice_id or DEFAULT_VOICE_ID
        self._voice_name = voice_name
        language = str(language or "").strip().lower().replace("_", "-")
        primary_language = language.split("-", 1)[0]
        self._language = (primary_language
                          if len(primary_language) == 2 and primary_language.isalpha()
                          else "")
        self._voice_settings = dict(voice_settings or {})
        self._cache = cache
        # Driver's own respellings, layered over the built-in table.
        self._pronunciation = dict(pronunciation or {})
        # Built from the current grid each answer; see auto_pronunciation.
        self._auto_pronunciation: Dict[str, str] = {}
        self._subscription: Optional[Dict[str, Any]] = None
        self._client = httpx.Client(timeout=30, base_url="https://api.elevenlabs.io/v1")

    def set_auto_pronunciation(self, table: Dict[str, str]) -> None:
        """Respellings derived from who is actually on track right now.

        Kept apart from the driver's own table so the two can be reasoned
        about separately: this one is rebuilt every session and is only ever
        a guess, theirs is deliberate and always wins.
        """
        self._auto_pronunciation = dict(table or {})

    def _build_voice_settings(self) -> Dict[str, Any]:
        """Assemble the voice_settings payload.

        stability and similarity_boost are supported everywhere. The rest
        are only sent when they differ from the neutral default, so a model
        that does not support them never sees them.
        """
        cfg = self._voice_settings
        settings: Dict[str, Any] = {
            "stability": _clamp(cfg.get("stability", 0.5), 0.0, 1.0, 0.5),
            "similarity_boost": _clamp(cfg.get("similarity_boost", 0.75), 0.0, 1.0, 0.75),
        }

        style = _clamp(cfg.get("style", 0.0), 0.0, 1.0, 0.0)
        if style > 0:
            settings["style"] = style

        if cfg.get("use_speaker_boost"):
            settings["use_speaker_boost"] = True

        speed = _clamp(cfg.get("speed", 1.0), 0.7, 1.2, 1.0)
        if abs(speed - 1.0) > 1e-3:
            settings["speed"] = speed

        return settings

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def sample_rate(self) -> int:
        return PCM_SAMPLE_RATE

    @property
    def voice_id(self) -> str:
        return self._voice_id

    @property
    def voice_name(self) -> str:
        return self._voice_name

    def _headers(self) -> Dict[str, str]:
        return {"xi-api-key": self._api_key}

    def synthesize(self, text: str, regenerate: bool = False) -> bytes:
        """Synthesize speech, returning raw 16-bit mono PCM at 24 kHz."""
        if not self._api_key:
            raise TTSError("No ElevenLabs TTS API key configured")
        if not text.strip():
            raise TTSError("Nothing to synthesize")

        # Respell before anything else touches the text: the cache should
        # key on what is actually sent, so two answers that sound identical
        # share an entry, and a table change invalidates the old audio.
        # Roster-derived rules first, the driver's own on top, so an
        # explicit spelling always beats the automatic car-number fallback.
        table = dict(self._auto_pronunciation)
        table.update(self._pronunciation)
        text = pronounce.for_speech(pronounce.apply(text, table))

        settings = self._build_voice_settings()
        request_options: Dict[str, Any] = {"apply_text_normalization": "on"}
        if self._language:
            request_options["language_code"] = self._language
        key = None
        if self._cache is not None and self._cache.enabled:
            key = cache_key(
                text, self._voice_id, self._model, settings, request_options)
            if regenerate:
                self._cache.delete(key)
            else:
                cached = self._cache.get(key)
                if cached is not None:
                    try:
                        _validate_pcm(cached)
                    except TTSError:
                        logger.warning("Discarding malformed cached speech")
                        self._cache.delete(key)
                    else:
                        logger.debug("Speech served from cache (%d bytes)", len(cached))
                        return cached

        payload = {
            "model_id": self._model,
            "text": text,
            "voice_settings": settings,
            **request_options,
        }

        try:
            resp = self._client.post(
                f"/text-to-speech/{self._voice_id}",
                json=payload,
                params={"output_format": _OUTPUT_FORMAT},
                headers=self._headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TTSError(
                f"ElevenLabs TTS returned {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise TTSError(f"Could not reach ElevenLabs TTS: {exc}") from exc

        pcm = resp.content
        _validate_pcm(pcm)

        if key is not None:
            self._cache.put(key, pcm)
        return pcm

    def subscription(self) -> Dict[str, Any]:
        """Account subscription info, cached for the life of this client."""
        if self._subscription is None:
            try:
                resp = self._client.get("/user/subscription", headers=self._headers())
                resp.raise_for_status()
                self._subscription = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.debug("Could not read subscription: %s", exc)
                self._subscription = {}
        return self._subscription

    @property
    def tier(self) -> str:
        return str(self.subscription().get("tier") or "")

    def list_voices(self) -> List[Dict[str, Any]]:
        """List voices, flagging any this account cannot actually use.

        `/v1/voices` returns everything in the account, including Voice
        Library entries that the API refuses to synthesize on the free
        tier — so listing it verbatim offers voices that fail at 402.
        """
        if not self._api_key:
            raise TTSError("No ElevenLabs TTS API key configured")
        try:
            resp = self._client.get("/voices", headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise TTSError(
                f"ElevenLabs returned {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise TTSError(f"Could not reach ElevenLabs: {exc}") from exc
        except ValueError as exc:
            raise TTSError(f"Malformed voices response: {exc}") from exc

        tier = self.tier
        voices: List[Dict[str, Any]] = []
        for v in data.get("voices", []):
            if not v.get("voice_id"):
                continue
            # ElevenLabs describes each voice with labels — gender, accent and
            # age among them. Passing them through lets the picker filter on
            # something more useful than a name.
            labels = v.get("labels") or {}
            # A non-null `sharing` block means the voice came from the Voice
            # Library. Its `free_users_allowed` flag is the *owner's*
            # permission and does not override the API's tier gate.
            is_library = v.get("sharing") is not None
            blocked = is_library and tier in LIBRARY_BLOCKED_TIERS
            voices.append(
                {
                    "id": v["voice_id"],
                    "name": v.get("name", ""),
                    "description": v.get("description") or "",
                    "category": v.get("category") or "",
                    "gender": (labels.get("gender") or "").lower(),
                    "accent": (labels.get("accent") or "").lower(),
                    "age": (labels.get("age") or "").replace("_", " ").lower(),
                    "useCase": (labels.get("use_case") or "").replace("_", " ").lower(),
                    "previewUrl": v.get("preview_url") or "",
                    "isLibrary": is_library,
                    "available": not blocked,
                    "restriction": "Needs a paid plan" if blocked else "",
                }
            )
        return voices

    def set_voice(self, voice_id: str, voice_name: str = "") -> str:
        """Switch TTS voice by ID; returns the resolved voice name."""
        self._voice_id = voice_id or DEFAULT_VOICE_ID
        self._voice_name = voice_name
        if voice_name or not self._api_key:
            return self._voice_name
        try:
            resp = self._client.get(f"/voices/{self._voice_id}", headers=self._headers())
            resp.raise_for_status()
            self._voice_name = resp.json().get("name", "")
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("Could not resolve voice name for %s: %s", voice_id, exc)
            self._voice_name = ""
        return self._voice_name

    def close(self) -> None:
        self._client.close()


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    """Clamp to range, falling back to `default` — not to `low` — when the
    value is missing or unparseable, so a corrupt config does not silently
    pin a setting to its minimum."""
    try:
        return max(low, min(float(value), high))
    except (TypeError, ValueError):
        return default
