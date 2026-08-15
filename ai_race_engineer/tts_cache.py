"""On-disk cache of synthesized speech.

Speech is the largest single cost in the pipeline — roughly 46% of what a
question costs — and it is billed per character every time, even when the
engineer says something it has said a hundred times before. Refusals are
word-for-word identical by design ("I've got no reading on your brakes"),
and within a session the same question tends to produce the same answer.
Every repeat served from here is free.

What is cached is the raw PCM from ElevenLabs, before the radio effect and
volume are applied. Those are cheap local DSP and the driver changes them
often; re-rendering them costs nothing, whereas re-synthesizing does.

The cache lives beside config.json rather than in a temp directory, because
the phrases worth caching repeat across sessions, not just within one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("race-engineer")

#: Raw PCM is 48 KB per second at 24 kHz mono, so a five-second answer is
#: about 240 KB. The default holds several hundred phrases.
DEFAULT_MAX_MB = 128


def cache_key(
    text: str,
    voice_id: str,
    model: str,
    settings: Dict[str, Any],
    request_options: Optional[Dict[str, Any]] = None,
) -> str:
    """Identify audio by everything that changes how it sounds.

    Voice settings are part of the key: the same words at stability 0.4 and
    0.75 are different recordings, and serving the wrong one would make a
    settings change look like it did nothing.
    """
    payload = json.dumps(
        {
            "text": (text or "").strip(),
            "voice": voice_id or "",
            "model": model or "",
            "settings": {k: settings.get(k) for k in sorted(settings or {})},
            "request": {
                k: request_options.get(k) for k in sorted(request_options or {})
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class TTSCache:
    """Least-recently-used store of PCM keyed by phrase and voice."""

    def __init__(self, directory: Path, max_mb: int = DEFAULT_MAX_MB, enabled: bool = True):
        self._dir = Path(directory)
        self._max_bytes = max(0, int(max_mb)) * 1024 * 1024
        self._enabled = bool(enabled) and self._max_bytes > 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.bytes_served = 0

        if self._enabled:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("Could not create the speech cache at %s: %s", self._dir, exc)
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get(self, key: str) -> Optional[bytes]:
        """Return cached audio, or None. Never raises — a miss is not an error."""
        if not self._enabled:
            return None
        path = self._dir / f"{key}.pcm"
        try:
            data = path.read_bytes()
        except OSError:
            with self._lock:
                self.misses += 1
            return None

        with self._lock:
            self.hits += 1
            self.bytes_served += len(data)
        # Touch it so eviction knows this phrase is still in use.
        try:
            path.touch()
        except OSError:
            pass
        return data

    def put(self, key: str, pcm: bytes) -> None:
        """Store audio. A failure here costs a cache miss, nothing more."""
        if not self._enabled or not pcm:
            return
        path = self._dir / f"{key}.pcm"
        tmp = path.with_suffix(".tmp")
        try:
            # Write then rename, so a crash mid-write cannot leave a
            # truncated file that would later be served as audio.
            tmp.write_bytes(pcm)
            tmp.replace(path)
        except OSError as exc:
            logger.debug("Could not cache speech: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self._evict()

    def _evict(self) -> None:
        """Drop the least recently used entries until back under the cap."""
        try:
            files = sorted(self._dir.glob("*.pcm"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        total = 0
        sizes = []
        for f in files:
            try:
                size = f.stat().st_size
            except OSError:
                continue
            sizes.append((f, size))
            total += size
        for f, size in sizes:
            if total <= self._max_bytes:
                break
            try:
                f.unlink()
                total -= size
            except OSError:
                pass

    def clear(self) -> int:
        """Remove every entry. Returns how many were deleted."""
        if not self._dir.exists():
            return 0
        removed = 0
        for f in self._dir.glob("*.pcm"):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        with self._lock:
            self.hits = self.misses = self.bytes_served = 0
        return removed

    def delete(self, key: str) -> bool:
        """Remove one synthesized phrase so it can be generated again."""
        if not self._enabled:
            return False
        try:
            (self._dir / f"{key}.pcm").unlink()
            return True
        except OSError:
            return False

    def stats(self) -> Dict[str, Any]:
        """Hit rate and size, for the settings panel."""
        with self._lock:
            hits, misses, served = self.hits, self.misses, self.bytes_served
        entries = 0
        size = 0
        try:
            for f in self._dir.glob("*.pcm"):
                entries += 1
                size += f.stat().st_size
        except OSError:
            pass
        asked = hits + misses
        return {
            "enabled": self._enabled,
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / asked, 3) if asked else 0.0,
            "entries": entries,
            "size_mb": round(size / (1024 * 1024), 1),
            "max_mb": round(self._max_bytes / (1024 * 1024)),
            # 24 kHz, 16-bit mono: two bytes a sample.
            "seconds_served": round(served / (24000 * 2), 1),
        }


# One instance shared by the engineer and the settings previews, so a phrase
# auditioned in the panel is already cached when the engineer says it, and
# the hit counters describe the whole app rather than one caller.
_shared: Optional[TTSCache] = None
_shared_key: Optional[tuple] = None


def shared_cache(directory: Path, max_mb: int, enabled: bool) -> TTSCache:
    """Return the process-wide cache, rebuilding it if the settings changed."""
    global _shared, _shared_key
    key = (str(directory), int(max_mb), bool(enabled))
    if _shared is None or _shared_key != key:
        _shared = TTSCache(directory, max_mb, enabled)
        _shared_key = key
    return _shared
