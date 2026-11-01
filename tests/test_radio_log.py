"""The Radio Log: what it keeps, what survives a restart, what an export holds.

What is pinned here: entries and the changes made to them after the fact
(timings, ratings) come back after a restart; the file stays bounded; a bad
line costs one entry, not the log; switched off, nothing touches the disk;
warnings are recorded and INFO is not, so questions are never doubled as
events; a reader asking for changes gets the late timings of an old entry;
and an export never contains an API key.
"""

import json
import logging
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ai_race_engineer.config import AppConfig
from ai_race_engineer.llm import COMMS_FAILURE
from ai_race_engineer.radio_log import FILE_NAME, RadioLog
from ai_race_engineer.web_ui import create_app

PANEL = "127.0.0.1:9420"
EL_KEY = "sk_test_elevenlabs_secret_key"
OR_KEY = "sk-or-test-openrouter-secret-key"


def _lines(path):
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── Storage ─────────────────────────────────────────────────────────────

def test_entries_and_their_later_changes_survive_a_restart(tmp_path):
    path = tmp_path / FILE_NAME
    log = RadioLog(path)
    entry = log.add({"kind": "exchange", "question": "How's my fuel?", "answer": "Eight laps."})
    log.patch(entry["id"], {"timings": {"total": 2400.0}})
    log.patch(entry["id"], {"feedback": {"rating": "down", "note": "wrong number"}})

    reloaded = RadioLog(path).get(entry["id"])

    assert reloaded["question"] == "How's my fuel?"
    assert reloaded["timings"] == {"total": 2400.0}
    assert reloaded["feedback"] == {"rating": "down", "note": "wrong number"}


def test_the_log_keeps_the_newest_and_the_file_stays_bounded(tmp_path):
    path = tmp_path / FILE_NAME
    log = RadioLog(path, keep=100)
    for n in range(450):
        log.add({"kind": "exchange", "question": f"q{n}"})

    kept = [e["question"] for e in log.entries()]
    assert kept[0] == "q350" and kept[-1] == "q449" and len(kept) == 100
    assert len(_lines(path)) <= 200, "never compacted"
    assert [e["question"] for e in RadioLog(path, keep=100).entries()] == kept


def test_a_corrupt_line_costs_that_line_not_the_log(tmp_path):
    path = tmp_path / FILE_NAME
    log = RadioLog(path)
    log.add({"kind": "exchange", "question": "first"})
    with open(path, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    log.add({"kind": "exchange", "question": "second"})

    assert [e["question"] for e in RadioLog(path).entries()] == ["first", "second"]


def test_switched_off_nothing_touches_the_disk(tmp_path):
    path = tmp_path / FILE_NAME
    log = RadioLog(path, enabled=False)
    log.add({"kind": "exchange", "question": "kept in memory"})

    assert not path.exists()
    assert [e["question"] for e in log.entries()] == ["kept in memory"]


def test_switching_on_keeps_what_was_logged_meanwhile(tmp_path):
    path = tmp_path / FILE_NAME
    RadioLog(path).add({"kind": "exchange", "question": "yesterday"})
    log = RadioLog(path, enabled=False)
    log.add({"kind": "exchange", "question": "while off"})

    log.configure(enabled=True)

    assert [e["question"] for e in RadioLog(path).entries()] == ["yesterday", "while off"]


def test_clear_deletes_the_file_and_tells_readers(tmp_path):
    path = tmp_path / FILE_NAME
    log = RadioLog(path)
    log.add({"kind": "exchange", "question": "gone"})
    seen = log.rev

    log.clear()

    assert not path.exists()
    assert log.entries() == []
    assert log.cleared_rev > seen, "a reader would keep showing the cleared entries"
    log.add({"kind": "exchange", "question": "fresh"})
    assert [e["question"] for e in RadioLog(path).entries()] == ["fresh"]


def test_a_reader_gets_the_late_timings_of_an_old_entry_and_nothing_else(tmp_path):
    """The panel polls for changes. Timings are patched in after speech
    finishes, by which time the panel has already seen the entry."""
    log = RadioLog(tmp_path / FILE_NAME)
    old = log.add({"kind": "exchange", "question": "old"})
    log.add({"kind": "exchange", "question": "unchanged"})
    seen = log.rev

    log.patch(old["id"], {"timings": {"total": 1800.0}})

    changed = log.entries(since_rev=seen)
    assert [e["question"] for e in changed] == ["old"]
    assert changed[0]["timings"] == {"total": 1800.0}


# ── Warnings ────────────────────────────────────────────────────────────

def test_warnings_are_recorded_and_info_is_not(tmp_path):
    """Questions and answers are logged at INFO. Recording INFO would put
    every exchange in the log twice."""
    log = RadioLog(tmp_path / FILE_NAME)
    source = logging.getLogger("race-engineer.test-source")
    # Open the logger itself to INFO, or the record never reaches the
    # handler and this passes whatever the handler's own level is.
    previous = source.level
    source.setLevel(logging.DEBUG)
    handler = log.handler()
    source.addHandler(handler)
    try:
        source.info("Question: %r", "how's my fuel")
        source.warning("iRacing disconnected")
    finally:
        source.removeHandler(handler)
        source.setLevel(previous)

    events = log.entries()
    assert [(e["kind"], e["level"], e["message"]) for e in events] == [
        ("event", "warning", "iRacing disconnected")]


def test_a_repeated_warning_is_counted_not_repeated(tmp_path):
    """A warning every poll would push a race's exchanges out of the log."""
    log = RadioLog(tmp_path / FILE_NAME)
    for _ in range(50):
        log.add_event("warning", "race-engineer", "iRacing disconnected")

    events = log.entries()
    assert len(events) == 1 and events[0]["count"] == 50


# ── Through the app ─────────────────────────────────────────────────────

def _config(tmp_path, **extra):
    data = {
        "telemetry": {"source": "simulated"},
        "elevenlabs": {"api_key": EL_KEY, "cache_speech": False},
        "llm": {"openrouter": {"api_key": OR_KEY}},
    }
    data.update(extra)
    return AppConfig(data, path=tmp_path / "config.json")


def _client(config, export_dir):
    app = create_app(config)
    app.state.export_dir = export_dir
    return TestClient(app, base_url=f"http://{PANEL}")


def _log_exchange(client, question="How's my fuel?", answer="Eight laps."):
    orch = client.app.state.orchestrator
    entry = orch._log_exchange(question, answer, "text", route="deterministic")
    orch._finish_log_entry()
    return entry


def test_an_exchange_records_what_it_takes_to_debug_it(tmp_path, monkeypatch):
    import asyncio

    config = _config(tmp_path)
    client = _client(config, tmp_path / "out")
    orch = client.app.state.orchestrator
    monkeypatch.setattr(orch.llm, "generate", lambda q, s: COMMS_FAILURE)

    asyncio.run(orch.ask("How's my fuel?", speak=False))

    entry = client.get("/api/log").json()["entries"][-1]
    assert entry["kind"] == "exchange" and entry["type"] == "text"
    assert entry["failed"] is True
    assert entry["model"] == "openrouter/anthropic/claude-haiku-5.5"
    assert entry["profile"] == "default"
    assert entry["session_kind"]
    assert "route" in entry
    assert entry["timings"]["total"] >= 0, "timings are patched in after the turn"


def test_feedback_is_validated(tmp_path):
    client = _client(_config(tmp_path), tmp_path / "out")
    entry = _log_exchange(client)

    assert client.post(f"/api/log/{entry['id']}/feedback", json={"rating": "meh"}).status_code == 400
    assert client.post("/api/log/0123456789ab/feedback", json={"rating": "up"}).status_code == 404
    saved = client.post(f"/api/log/{entry['id']}/feedback",
                        json={"rating": "down", "note": "x" * 900}).json()["entry"]
    assert saved["feedback"]["rating"] == "down"
    assert len(saved["feedback"]["note"]) == 500


def test_an_export_never_contains_an_api_key(tmp_path):
    config = _config(tmp_path, system_prompt="Be terse.", driver_callsign="Bart")
    (tmp_path / "aire.log").write_text("WARNING something\n", encoding="utf-8")
    client = _client(config, tmp_path / "out")
    _log_exchange(client)

    result = client.post("/api/log/export", json={"include_aire_log": True}).json()

    with zipfile.ZipFile(result["path"]) as bundle:
        names = set(bundle.namelist())
        everything = "".join(bundle.read(n).decode("utf-8") for n in names)
        about = json.loads(bundle.read("about.json"))
    assert {"about.json", "radio-log.json", "radio-log.txt", "aire.log"} <= names
    assert EL_KEY not in everything and OR_KEY not in everything
    assert "Be terse." not in everything, "the prompt text is reported as custom, not quoted"
    assert about["settings"]["custom_system_prompt"] is True
    assert about["settings"]["llm_model"] == "anthropic/claude-haiku-5.5"


def test_an_export_holds_exactly_what_was_asked_for(tmp_path):
    config = _config(tmp_path)
    (tmp_path / "diagnostic-trace.jsonl").write_text("{}\n", encoding="utf-8")
    client = _client(config, tmp_path / "out")
    first = _log_exchange(client, "first")
    _log_exchange(client, "second")

    plain = client.post("/api/log/export", json={"ids": [first["id"]]}).json()
    with zipfile.ZipFile(plain["path"]) as bundle:
        assert [e["question"] for e in json.loads(bundle.read("radio-log.json"))] == ["first"]
        assert "diagnostic-trace.jsonl" not in bundle.namelist()

    traced = client.post("/api/log/export",
                         json={"ids": [first["id"]], "include_trace": True}).json()
    with zipfile.ZipFile(traced["path"]) as bundle:
        assert "diagnostic-trace.jsonl" in bundle.namelist()


@pytest.mark.parametrize("name", ["../config.json", "config.json",
                                  "AIRE-feedback-20261101-120000.zip/../x",
                                  "AIRE-feedback-20261101-120000.zip"])
def test_reveal_opens_only_an_export_this_app_wrote(tmp_path, name):
    """Every name here points at a file that exists, so a check that only
    asked whether the path was there would open it."""
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (out / "config.json").write_text("{}", encoding="utf-8")
    (out / "AIRE-feedback-20261101-120000.zip").mkdir()       # a directory, not a bundle
    (out / "x").write_text("", encoding="utf-8")
    client = _client(_config(tmp_path), out)
    assert client.post("/api/log/export/reveal", json={"name": name}).status_code == 404


def test_the_old_exchange_log_length_is_migrated_away(tmp_path):
    config = AppConfig({"memory_log": 20}, path=tmp_path / "config.json")
    assert "memory_log" not in config.to_dict()
    assert config.radio_log == {"enabled": True, "keep": 1000, "retention_days": 30}


# ── Retention and clearing older entries ────────────────────────────────

def _aged_file(path, *ages_days):
    """A log file holding one entry per age, oldest first."""
    now = datetime.now(timezone.utc)
    with open(path, "w", encoding="utf-8") as f:
        for n, days in enumerate(ages_days):
            stamp = (now - timedelta(days=days)).isoformat()
            f.write(json.dumps({"op": "add", "id": f"{n:012x}", "timestamp": stamp,
                                "kind": "exchange", "question": f"{days}d"}) + "\n")


def test_prune_removes_only_what_is_older_and_tells_readers(tmp_path):
    path = tmp_path / FILE_NAME
    _aged_file(path, 10, 2, 0)
    log = RadioLog(path, retention_days=0)
    seen = log.rev

    assert log.prune(timedelta(days=7)) == 1

    assert [e["question"] for e in log.entries()] == ["2d", "0d"]
    assert [e["question"] for e in RadioLog(path, retention_days=0).entries()] == ["2d", "0d"]
    assert log.cleared_rev > seen, "the panel would keep showing the pruned entry"


def test_retention_applies_on_load(tmp_path):
    path = tmp_path / FILE_NAME
    _aged_file(path, 40, 0)
    assert [e["question"] for e in RadioLog(path, retention_days=30).entries()] == ["0d"]


def test_no_time_limit_keeps_old_entries(tmp_path):
    path = tmp_path / FILE_NAME
    _aged_file(path, 40, 0)
    assert [e["question"] for e in RadioLog(path, retention_days=0).entries()] == ["40d", "0d"]


@pytest.mark.parametrize("bad", [-5, "x", 3, True, None])
def test_an_odd_retention_value_means_thirty_days(tmp_path, bad):
    path = tmp_path / FILE_NAME
    _aged_file(path, 40, 20, 0)
    assert [e["question"] for e in RadioLog(path, retention_days=bad).entries()] == ["20d", "0d"]


def test_clearing_older_than_through_the_app(tmp_path):
    config = _config(tmp_path, radio_log={"retention_days": 0})
    _aged_file(tmp_path / FILE_NAME, 10, 2, 0)
    client = _client(config, tmp_path / "out")

    assert client.post("/api/log/clear", json={"older_than_hours": 3}).status_code == 400
    assert client.post("/api/log/clear", json={"older_than_days": 7}).status_code == 200, \
        "an unknown field is ignored, which here means clear everything"


def test_clearing_older_than_a_week_and_than_an_hour(tmp_path):
    """The shortest choice is the one a log a day old can actually use."""
    config = _config(tmp_path, radio_log={"retention_days": 0})
    path = tmp_path / FILE_NAME
    _aged_file(path, 10, 2, 0)
    now = datetime.now(timezone.utc)
    with open(path, "a", encoding="utf-8") as f:
        for n, minutes in ((10, 120), (11, 10)):
            f.write(json.dumps({"op": "add", "id": f"{n:012x}", "kind": "exchange",
                                "question": f"{minutes}m",
                                "timestamp": (now - timedelta(minutes=minutes)).isoformat()}) + "\n")
    client = _client(config, tmp_path / "out")

    def questions():
        return [e["question"] for e in client.get("/api/log").json()["entries"]
                if e["kind"] == "exchange"]

    assert client.post("/api/log/clear", json={"older_than_hours": 168}).json()["removed"] == 1
    assert questions() == ["2d", "0d", "120m", "10m"]
    # "120m" sits after the fresh "0d" in the file: removed all the same.
    assert client.post("/api/log/clear", json={"older_than_hours": 1}).json()["removed"] == 2
    assert questions() == ["0d", "10m"]


# ── Deleting a note ─────────────────────────────────────────────────────

def test_deleting_a_note_keeps_the_rating(tmp_path):
    client = _client(_config(tmp_path), tmp_path / "out")
    entry = _log_exchange(client)
    url = f"/api/log/{entry['id']}/feedback"
    client.post(url, json={"rating": "down", "note": "misheard me"})

    kept = client.post(url, json={"rating": "down", "note": ""}).json()["entry"]
    assert kept["feedback"]["rating"] == "down" and kept["feedback"]["note"] == ""

    gone = client.post(url, json={"rating": None, "note": ""}).json()["entry"]
    assert gone["feedback"] is None
