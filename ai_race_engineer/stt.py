"""Speech-to-text via the ElevenLabs Scribe API."""

import logging
import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import httpx

logger = logging.getLogger("race-engineer")

_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"


class STTError(RuntimeError):
    """Transcription failed."""


@dataclass(frozen=True)
class Transcription:
    text: str
    language_code: str = ""
    language_probability: Optional[float] = None
    word_logprobs: Tuple[float, ...] = ()

    @property
    def mean_word_logprob(self) -> Optional[float]:
        if not self.word_logprobs:
            return None
        return round(sum(self.word_logprobs) / len(self.word_logprobs), 3)


_RACING_WORDS = {
    "abs", "ahead", "behind", "brake", "car", "catch", "class", "close",
    "closing", "damage", "delta", "engine", "faster", "flag", "fuel", "gain",
    "gap", "incident", "joker", "lap", "leader", "pace", "penalty", "pit",
    "position", "push", "race", "rain", "repair", "sector", "slower", "stint",
    "strategy", "temperature", "track", "tyre", "tire", "weather", "wet",
}


def should_repeat(result: Transcription) -> bool:
    """Reject only an uncertain transcript that also looks unlike race radio.

    Confidence on its own can be low for a surname; vocabulary on its own can
    reject ordinary conversation. Requiring both signals means a clear
    off-topic question still reaches the engineer and a noisy but recognisable
    racing request still gets answered.
    """
    words = set(re.findall(r"[a-z0-9]+", result.text.lower()))
    racing = bool(words & _RACING_WORDS) or any(phrase in result.text.lower() for phrase in (
        "where am i", "how am i doing", "what am i driving", "who is in front",
        "who is next", "how much longer", "can i make it", "am i going to make it",
        "say again", "repeat that", "repeat last", "what did you say", "come again",
        "try that again", "that was gibberish", "voice glitched", "audio glitched",
        "bad audio", "glitched out",
    ))
    if racing:
        return False
    mean = result.mean_word_logprob
    low_words = mean is not None and mean < -1.0
    low_language = (result.language_probability is not None
                    and result.language_probability < 0.55)
    return low_words or low_language


class STT:
    """ElevenLabs Scribe speech-to-text."""

    def __init__(self, api_key: str, model: str = "scribe_v1", language: str = "en"):
        self._api_key = api_key
        self._model = model
        self._language = language
        self._client = httpx.Client(timeout=30)

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def transcribe_raw_result(
        self, audio_bytes: bytes, filename: str = "audio.wav",
        keyterms: Optional[Sequence[str]] = None,
    ) -> Transcription:
        """Transcribe raw WAV bytes and retain the structured response."""
        return self._post_result(audio_bytes, filename, keyterms)

    def _post_result(self, audio_bytes: bytes, filename: str,
                     keyterms: Optional[Sequence[str]] = None) -> Transcription:
        if not self._api_key:
            raise STTError("No ElevenLabs STT API key configured")

        try:
            resp = self._client.post(
                _STT_URL,
                headers={"xi-api-key": self._api_key},
                files={"file": (filename, audio_bytes, "audio/wav")},
                data=self._form(keyterms),
            )
            resp.raise_for_status()
            result = resp.json()
        except httpx.HTTPStatusError as exc:
            raise STTError(
                f"ElevenLabs STT returned {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise STTError(f"Could not reach ElevenLabs STT: {exc}") from exc
        except ValueError as exc:
            raise STTError(f"Malformed STT response: {exc}") from exc

        return self._parse_result(result)

    def _form(self, keyterms: Optional[Sequence[str]]) -> dict:
        """Multipart fields for one transcription request.

        `keyterms` must go out as a repeated form field — httpx does that
        for a list value. A JSON-encoded array is rejected as one oversized
        keyword ("All keywords must be less than 50 characters"), and a
        comma-joined string is accepted but treated as a single keyword,
        which silently stops biasing once the list passes 50 characters.
        """
        data: dict = {
            "model_id": self._model,
            "language_code": self._language,
        }
        if keyterms:
            data["keyterms"] = list(keyterms)
            logger.debug("STT biased toward %d keyterms", len(keyterms))
        return data

    @staticmethod
    def _parse_result(result: dict) -> Transcription:
        """Keep text plus the confidence evidence used by the radio guard."""
        text = (result.get("text") or "").strip()
        if not text:
            # Older/segmented responses put the text on individual segments.
            segments = result.get("segments") or []
            text = " ".join(s.get("text", "").strip() for s in segments).strip()
        logprobs = []
        for word in result.get("words") or []:
            if word.get("type") != "word":
                continue
            try:
                logprobs.append(float(word["logprob"]))
            except (KeyError, TypeError, ValueError):
                continue
        try:
            probability = float(result["language_probability"])
        except (KeyError, TypeError, ValueError):
            probability = None
        return Transcription(
            text=text,
            language_code=str(result.get("language_code") or ""),
            language_probability=probability,
            word_logprobs=tuple(logprobs),
        )

    def close(self) -> None:
        self._client.close()
