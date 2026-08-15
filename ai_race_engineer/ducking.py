"""Audio ducking (Windows only).

Uses the WASAPI per-application volume API (via pycaw) to drop another
app's volume while the race engineer is talking, then restore it.

Everything here degrades to a no-op when pycaw isn't installed or we're
not on Windows, so the rest of the pipeline never has to care.
"""

import logging
import os
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("race-engineer")

try:
    from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

    HAS_PYCAW = True
except Exception:  # pycaw missing, or not on Windows
    AudioUtilities = ISimpleAudioVolume = None
    HAS_PYCAW = False


class AudioDuck:
    """Duck another application's audio session while the AI responds."""

    def __init__(self, config):
        self._config = config
        self._settings = getattr(config, "ducking", config) or {}
        self._available = os.name == "nt" and HAS_PYCAW
        # Exact session-volume handles captured at duck() time. One process
        # can own several WASAPI sessions with different levels, so a PID is
        # not a unique key and cannot restore them correctly.
        self._saved: List[Tuple[Any, float]] = []

    @property
    def available(self) -> bool:
        return self._available

    # Kept as an alias: older code referenced `.supported`.
    @property
    def supported(self) -> bool:
        return self._available

    @property
    def process_name(self) -> str:
        return self._settings.get("process_name", "CrewChiefV4.exe")

    def list_sessions(self) -> list:
        """Processes you could duck, for the settings dropdown.

        Windows lists an app here only once it has played something, so an
        app that has been quiet since launch will not appear. The field
        accepts a typed name for that case, and Refresh picks it up once it
        has made a sound.
        """
        if not self._available:
            return []

        entries: Dict[str, dict] = {}

        try:
            for session in AudioUtilities.GetAllSessions():
                if session.Process and session.Process.name():
                    name = session.Process.name()
                    entries[name.lower()] = {"id": name, "name": name}
        except Exception as exc:
            logger.debug("Could not enumerate audio sessions: %s", exc)

        # Enumerating every running process was tried and made this worse:
        # 112 entries of background services to find one app in. Audio
        # sessions are both short and exactly the set that can be ducked.
        # An app missing from the list has simply not made a sound yet —
        # the field takes a typed name, which works once it does.

        # Ducking ourselves would only mute the engineer.
        entries.pop("aire.exe", None)
        entries.pop("airaceengineer.exe", None)

        return sorted(entries.values(), key=lambda e: e["name"].lower())

    def duck(self) -> bool:
        """Reduce the target application's volume. True if anything changed."""
        if not self._available:
            return False

        # Never overwrite a baseline that still needs restoring. Capturing an
        # already-ducked level here would make the reduction permanent.
        if self._saved:
            self.un_duck()
            if self._saved:
                logger.warning("Previous audio level could not be restored; skipping duck")
                return False

        target = self.process_name.lower()
        try:
            level = max(0.0, min(1.0, float(
                self._settings.get("duck_level", 0.15))))
        except (TypeError, ValueError):
            level = 0.15
        self._saved = []

        try:
            sessions = AudioUtilities.GetAllSessions()
        except Exception as exc:
            logger.debug("Ducking failed to enumerate sessions: %s", exc)
            return False

        for session in sessions:
            if not session.Process or session.Process.name().lower() != target:
                continue
            try:
                volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                previous = float(volume.GetMasterVolume())
                # Ducking must never make a quiet or muted target louder.
                if previous <= level:
                    continue
                volume.SetMasterVolume(level, None)
                self._saved.append((volume, previous))
            except Exception as exc:
                logger.debug("Could not duck pid %s: %s", session.ProcessId, exc)

        if self._saved:
            logger.debug("Ducked %s (%d session(s))", self.process_name, len(self._saved))
        return bool(self._saved)

    def un_duck(self) -> bool:
        """Restore the volumes captured by the last duck()."""
        if not self._available or not self._saved:
            return False

        restored = False
        pending = self._saved
        self._saved = []
        for volume, previous in pending:
            try:
                volume.SetMasterVolume(previous, None)
                restored = True
            except Exception as exc:
                # Keep it for a later retry; silently discarding the only copy
                # of the original level is how an app stays permanently low.
                self._saved.append((volume, previous))
                logger.debug("Could not restore an audio session: %s", exc)

        return restored and not self._saved
