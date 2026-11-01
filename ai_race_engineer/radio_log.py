"""The Radio Log: every exchange and warning, searchable and exportable.

One timeline of two kinds of entry:

- `exchange` — a question and its answer, with what it takes to debug one:
  how it was answered (telemetry, a calculator, the model), how long each
  stage took, how sure speech recognition was, whether the speech was
  replayed from the cache, and any thumbs up or down the driver gave it.
- `event` — a WARNING or ERROR from anywhere in the app: the lines that go
  to aire.log, where nobody in the app could see them.

Kept on disk beside config.json so feedback can be gathered across a few
sessions and sent in one export. The file is append-only JSONL — an `add`
record per entry and a `patch` record per later change (timings arrive after
speech finishes; feedback whenever the driver gives it) — so an exchange
costs one short append rather than a rewrite. It is compacted once it holds
twice the entries kept.

Switched off, the log is memory only and nothing is read or written.
"""

import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Its own name, so the warning handler can ignore what this module says about
# itself — recording "could not write the log" by writing to the log loops.
logger = logging.getLogger("race-engineer.radio-log")

FILE_NAME = "radio-log.jsonl"
DEFAULT_KEEP = 1000
MIN_KEEP, MAX_KEEP = 100, 5000

#: How long an entry is kept, in days; 0 keeps it until the entry cap pushes
#: it out. A fixed set, so a hand-edited config cannot ask for a negative age.
RETENTION_CHOICES = (0, 1, 7, 30, 90)
DEFAULT_RETENTION_DAYS = 30
#: Retention is checked as entries arrive, at most this often.
_RETENTION_CHECK_S = 3600.0

#: A warning repeated within this many seconds is counted on the entry it
#: repeats, not added again. An iRacing reconnect warning every poll would
#: otherwise push a race's worth of exchanges out of a 1,000-entry log.
_REPEAT_WINDOW_S = 60.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_id(entry_id: str) -> bool:
    return isinstance(entry_id, str) and len(entry_id) == 12 and all(
        c in "0123456789abcdef" for c in entry_id)


class RadioLog:
    """Entries in memory, mirrored to a JSONL file when enabled.

    Every change bumps `rev`, and each entry carries the `rev` it last
    changed at, so a reader can ask for only what changed since it last
    looked. Thread-safe: exchanges arrive from worker threads and warnings
    from anywhere.
    """

    def __init__(self, path: Path, enabled: bool = True, keep: int = DEFAULT_KEEP,
                 retention_days: Any = DEFAULT_RETENTION_DAYS):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._entries: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._rev = 0
        self._cleared_rev = 0    # the rev of the last clear(), for readers
        self._lines = 0          # records in the file, for compaction
        self._enabled = False
        self._keep = self._clamp(keep)
        self._retention_days = self._retention(retention_days)
        self._retention_checked = 0.0
        self.configure(enabled=enabled, keep=keep, retention_days=retention_days)

    # ── Settings ────────────────────────────────────────────────────

    @staticmethod
    def _clamp(keep: Any) -> int:
        try:
            return max(MIN_KEEP, min(MAX_KEEP, int(keep)))
        except (TypeError, ValueError):
            return DEFAULT_KEEP

    @staticmethod
    def _retention(days: Any) -> int:
        return days if days in RETENTION_CHOICES and not isinstance(days, bool) \
            else DEFAULT_RETENTION_DAYS

    @property
    def path(self) -> Path:
        return self._path

    @property
    def enabled(self) -> bool:
        return self._enabled

    def configure(self, enabled: bool, keep: Any = DEFAULT_KEEP,
                  path: Optional[Path] = None,
                  retention_days: Any = DEFAULT_RETENTION_DAYS) -> None:
        """Apply settings live. Switching on reads what the file already
        holds and adds anything kept in memory meanwhile; switching off
        leaves the file alone until it is cleared."""
        with self._lock:
            if path is not None and Path(path) != self._path:
                self._path = Path(path)
                self._enabled = False       # re-read from the new place
            self._keep = self._clamp(keep)
            self._retention_days = self._retention(retention_days)
            if enabled and not self._enabled:
                in_memory = list(self._entries.values())
                self._entries.clear()
                self._load()
                for entry in in_memory:
                    self._entries.setdefault(entry["id"], entry)
                self._enabled = True
                self._trim()
                self._compact()
            elif not enabled:
                self._enabled = False
                self._trim()
            else:
                self._trim()
            self._apply_retention()

    # ── Reading ─────────────────────────────────────────────────────

    @property
    def rev(self) -> int:
        return self._rev

    @property
    def cleared_rev(self) -> int:
        """A reader that last looked before this must start again."""
        return self._cleared_rev

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self, since_rev: int = 0) -> List[Dict[str, Any]]:
        """Copies of the entries changed after `since_rev`, oldest first."""
        with self._lock:
            return [dict(e) for e in self._entries.values() if e["rev"] > since_rev]

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._entries.get(entry_id)
            return dict(entry) if entry else None

    # ── Writing ─────────────────────────────────────────────────────

    def add(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """Add an entry and return it (a live reference, for `patch`)."""
        with self._lock:
            entry_id = secrets.token_hex(6)
            while entry_id in self._entries:
                entry_id = secrets.token_hex(6)
            self._rev += 1
            entry = {"id": entry_id, "timestamp": _now(), **fields, "rev": self._rev}
            self._entries[entry_id] = entry
            self._append({"op": "add", **{k: v for k, v in entry.items() if k != "rev"}})
            self._trim()
            if time.monotonic() - self._retention_checked > _RETENTION_CHECK_S:
                self._apply_retention()
            return entry

    def patch(self, entry_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return None
            self._rev += 1
            entry.update(fields)
            entry["rev"] = self._rev
            self._append({"op": "patch", "id": entry_id, "fields": fields})
            return dict(entry)

    def add_event(self, level: str, source: str, message: str) -> None:
        with self._lock:
            last = next(reversed(self._entries.values()), None)
            if (last is not None and last.get("kind") == "event"
                    and last.get("message") == message and last.get("level") == level
                    and self._age_seconds(last.get("last_at") or last["timestamp"])
                    < _REPEAT_WINDOW_S):
                self.patch(last["id"], {"count": last.get("count", 1) + 1, "last_at": _now()})
                return
            self.add({"kind": "event", "level": level, "source": source, "message": message})

    @staticmethod
    def _age_seconds(timestamp: str) -> float:
        try:
            then = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            return float("inf")
        return (datetime.now(timezone.utc) - then).total_seconds()

    def prune(self, older_than: timedelta) -> int:
        """Remove entries older than `older_than`; return how many went.

        Every entry is checked, not just the oldest few: entries are added
        in time order, but a file merged in when the log is switched back on
        need not be, and stopping at the first young entry would keep older
        ones behind it.
        """
        cutoff = datetime.now(timezone.utc) - older_than
        with self._lock:
            stale = [entry_id for entry_id, entry in self._entries.items()
                     if self._older(entry, cutoff)]
            for entry_id in stale:
                del self._entries[entry_id]
            if stale:
                # Readers hold entries this removed; make them start again.
                self._rev += 1
                self._cleared_rev = self._rev
                self._compact()
            return len(stale)

    @staticmethod
    def _older(entry: Dict[str, Any], cutoff: datetime) -> bool:
        try:
            stamp = datetime.fromisoformat(entry["timestamp"])
        except (KeyError, TypeError, ValueError):
            return True                       # undatable: let it go
        if stamp.tzinfo is None:              # hand-edited; read as UTC
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp < cutoff

    def _apply_retention(self) -> None:
        self._retention_checked = time.monotonic()
        if self._retention_days:
            self.prune(timedelta(days=self._retention_days))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._rev += 1
            self._cleared_rev = self._rev
            self._lines = 0
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Could not delete %s: %s", self._path, exc)

    # ── Disk ────────────────────────────────────────────────────────

    def _trim(self) -> None:
        while len(self._entries) > self._keep:
            self._entries.popitem(last=False)

    def _load(self) -> None:
        self._lines = 0
        if not self._path.is_file():
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as exc:
            logger.warning("Could not read %s: %s", self._path, exc)
            return
        skipped = 0
        for line in lines:
            if not line.strip():
                continue
            self._lines += 1
            try:
                record = json.loads(line)
                op = record.pop("op")
                if op == "add" and valid_id(record.get("id")):
                    self._rev += 1
                    record["rev"] = self._rev
                    self._entries[record["id"]] = record
                elif op == "patch" and record.get("id") in self._entries:
                    self._rev += 1
                    entry = self._entries[record["id"]]
                    entry.update(record.get("fields") or {})
                    entry["rev"] = self._rev
            except (ValueError, KeyError, TypeError, AttributeError):
                skipped += 1
        if skipped:
            logger.warning("Skipped %d unreadable line(s) in %s", skipped, self._path)

    def _append(self, record: Dict[str, Any]) -> None:
        if not self._enabled:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._lines += 1
        except OSError as exc:
            logger.warning("Could not write %s: %s", self._path, exc)
            return
        if self._lines > 2 * self._keep:
            self._compact()

    def _compact(self) -> None:
        """Rewrite the file as one `add` per entry kept, atomically."""
        if not self._enabled:
            return
        if not self._entries and not self._path.exists():
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for entry in self._entries.values():
                    record = {"op": "add", **{k: v for k, v in entry.items() if k != "rev"}}
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            tmp.replace(self._path)
            self._lines = len(self._entries)
        except OSError as exc:
            logger.warning("Could not compact %s: %s", self._path, exc)

    # ── Warnings and errors ─────────────────────────────────────────

    def handler(self) -> logging.Handler:
        return _EventHandler(self)


class _EventHandler(logging.Handler):
    """Records WARNING and above as `event` entries. INFO never: questions and
    answers are logged at INFO, and they are already exchanges."""

    def __init__(self, radio_log: RadioLog):
        super().__init__(level=logging.WARNING)
        self._log = radio_log

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == logger.name:
            return
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                message += f" ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
            self._log.add_event(record.levelname.lower(), record.name, message)
        except Exception:
            # A log handler must never take down the code that logged.
            pass
