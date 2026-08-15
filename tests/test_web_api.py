"""The HTTP surface the settings panel drives.

520 lines of it had no direct test, which is how a cross-site reset and a
reset that ate the pronunciation table both survived a full audit of the
code they live in. Each of these is a five-line TestClient assertion.

What is pinned here: keys never leave the process, posting the panel's own
blanked view back does not wipe them, a reset keeps what was typed by hand,
a busy pipeline says so instead of swallowing the question, the voice
preview speaks through the same respellings as the engineer, and the
diagnostic endpoints go through the adapter interface rather than the
iRacing reader's privates.
"""

import pytest
from fastapi.testclient import TestClient

from ai_race_engineer import tts as tts_module
from ai_race_engineer.config import AppConfig
from ai_race_engineer.orchestrator import Orchestrator
from ai_race_engineer.telemetry import TelemetryAdapter
from ai_race_engineer.web_ui import create_app

PANEL = "127.0.0.1:9420"

ELEVENLABS_KEY = "sk_test_elevenlabs_key"
OPENROUTER_KEY = "sk-or-test-openrouter-key"


def _config(tmp_path, **overrides):
    data = {
        "telemetry": {"source": "simulated"},
        "elevenlabs": {
            "api_key": ELEVENLABS_KEY,
            # No API in these tests, so a cache would only serve stale audio
            # between them.
            "cache_speech": False,
            "pronunciation": {"Kowalski": "Ko-VAL-ski"},
        },
        "llm": {"openrouter": {"api_key": OPENROUTER_KEY}},
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return AppConfig(data, path=tmp_path / "config.json")


def _client(config, orchestrator=None):
    app = create_app(config, orchestrator)
    # The guard refuses anything not addressed to loopback, and TestClient
    # defaults to Host "testserver".
    return TestClient(app, base_url=f"http://{PANEL}")


# ── Secrets ─────────────────────────────────────────────────────────────

def test_a_stored_key_never_leaves_the_process(tmp_path):
    """The panel needs to know a key is set, and nothing more than that."""
    client = _client(_config(tmp_path))

    body = client.get("/api/config").json()

    assert ELEVENLABS_KEY not in client.get("/api/config").text
    assert OPENROUTER_KEY not in client.get("/api/config").text
    assert body["elevenlabs"]["api_key"] == ""
    assert body["elevenlabs"]["api_key_set"] is True
    assert body["llm"]["openrouter"]["api_key"] == ""
    assert body["llm"]["openrouter"]["api_key_set"] is True


def test_saving_the_panels_blanked_view_keeps_the_stored_keys(tmp_path):
    """The panel posts back what it was given, and it was given blanks.

    Taking those literally would log the driver out of both providers every
    time they touched a slider.
    """
    config = _config(tmp_path)
    client = _client(config)
    view = client.get("/api/config").json()
    view["radio_effect"]["noise"] = 0.42

    assert client.post("/api/config", json=view).status_code == 200

    assert config.elevenlabs_key() == ELEVENLABS_KEY
    assert config.llm_api_key("openrouter") == OPENROUTER_KEY
    assert config.radio_effect["noise"] == 0.42


def test_a_key_typed_into_the_panel_is_stored(tmp_path):
    """The mirror case: a non-blank secret must still be taken."""
    config = _config(tmp_path)
    client = _client(config)

    client.post("/api/config", json={"elevenlabs": {"api_key": "sk_test_new"}})

    assert config.elevenlabs_key() == "sk_test_new"


# ── Restore defaults ────────────────────────────────────────────────────

def test_restoring_defaults_keeps_the_hand_typed_pronunciations(tmp_path):
    """A respelling table is built the way the favourites list is: by ear.

    The reset dialog promises the keys, the voice and the binding survive,
    and a driver reads that as "nothing I typed by hand is lost". This was
    the one thing typed by hand that a reset silently wiped.
    """
    config = _config(tmp_path)
    client = _client(config)

    assert client.post("/api/config/reset").status_code == 200

    assert config.elevenlabs["pronunciation"] == {"Kowalski": "Ko-VAL-ski"}
    assert config.elevenlabs_key() == ELEVENLABS_KEY
    assert config.llm_api_key("openrouter") == OPENROUTER_KEY


def test_resetting_the_voice_section_alone_keeps_the_pronunciations(tmp_path):
    """The likelier route: the voice sliders are what people reset."""
    config = _config(tmp_path, elevenlabs={"voice_settings": {"stability": 0.1},
                                           "favourite_voices": ["v1"],
                                           "tts_voice_id": "v1"})
    client = _client(config)

    response = client.post("/api/config/reset/elevenlabs")

    assert response.status_code == 200
    assert config.elevenlabs["pronunciation"] == {"Kowalski": "Ko-VAL-ski"}
    assert config.elevenlabs["favourite_voices"] == ["v1"]
    assert config.elevenlabs["tts_voice_id"] == "v1"
    assert config.elevenlabs["voice_settings"]["stability"] == 0.75, \
        "the reset did not actually run"


def test_resetting_an_unknown_section_is_a_404(tmp_path):
    client = _client(_config(tmp_path))

    assert client.post("/api/config/reset/not_a_section").status_code == 404


def test_a_reset_puts_the_radio_effect_back(tmp_path):
    """The setting the button exists for."""
    config = _config(tmp_path, radio_effect={"noise": 0.99})
    client = _client(config)

    body = client.post("/api/config/reset/radio_effect").json()

    assert body["ok"] is True
    assert config.radio_effect["noise"] == 0.08


# ── Ask ─────────────────────────────────────────────────────────────────

def test_a_busy_pipeline_is_a_409_not_a_silent_success(tmp_path):
    """200 with an error body reads as success to the panel's fetch wrapper.

    It cleared the question box, opened the exchange log and showed nothing:
    the typed question was gone and no answer ever arrived.
    """
    config = _config(tmp_path)
    orch = Orchestrator(config)
    client = _client(config, orch)

    assert orch._busy.acquire(blocking=False), "fixture needs the lock free"
    try:
        response = client.post("/api/ask", json={"question": "How's my fuel?"})
    finally:
        orch._busy.release()

    assert response.status_code == 409
    assert "busy" in response.json()["detail"].lower()


def test_an_empty_question_is_a_400(tmp_path):
    client = _client(_config(tmp_path))

    assert client.post("/api/ask", json={"question": "   "}).status_code == 400


# ── Voice preview ───────────────────────────────────────────────────────

class _FakeHTTP:
    """Stands in for the TTS client's httpx session."""

    def __init__(self, *args, **kwargs):
        self.payloads = []
        self.closed = 0

    def post(self, url, json=None, params=None, headers=None):
        self.payloads.append(json)
        return _FakeResponse(b"\x11\x22" * 20000)

    def close(self):
        self.closed += 1

    def get(self, *args, **kwargs):
        raise AssertionError("the preview should not query the account")


class _FakeResponse:
    def __init__(self, content):
        self.content = content
        self.status_code = 200

    def raise_for_status(self):
        return None


@pytest.fixture
def fake_elevenlabs(monkeypatch):
    sent = _FakeHTTP()
    monkeypatch.setattr(tts_module.httpx, "Client", lambda **kwargs: sent)
    return sent


def test_the_voice_preview_speaks_the_drivers_own_respellings(tmp_path,
                                                              fake_elevenlabs):
    """Auditioning a voice has to exercise the table it will speak through.

    Without it you preview a name, it sounds fine, and the engineer says
    something else on track — and the two clients key the speech cache
    differently, so the preview never saves the cost of the real answer.
    """
    client = _client(_config(tmp_path))

    response = client.post("/api/tts/preview", json={"text": "Kowalski is ahead"})

    assert response.status_code == 200
    assert fake_elevenlabs.payloads, "nothing was synthesized"
    assert "Ko-VAL-ski" in fake_elevenlabs.payloads[0]["text"]


def test_the_preview_releases_its_http_client(tmp_path, fake_elevenlabs):
    """Built per request, so an abandoned one holds its connection open."""
    client = _client(_config(tmp_path))

    client.post("/api/tts/preview", json={"text": "Radio check."})

    assert fake_elevenlabs.closed == 1


def test_the_preview_and_the_engineer_send_the_same_text(tmp_path,
                                                         fake_elevenlabs):
    """Same text, same voice, same settings — so they share a cache entry."""
    config = _config(tmp_path)
    client = _client(config)
    line = "Kowalski is one point four ahead"

    client.post("/api/tts/preview", json={"text": line})
    Orchestrator(config).tts.synthesize(line)

    assert len(fake_elevenlabs.payloads) == 2
    assert fake_elevenlabs.payloads[0] == fake_elevenlabs.payloads[1]


# ── Telemetry diagnostics ───────────────────────────────────────────────

class _StubAdapter(TelemetryAdapter):
    """A telemetry source that is not the iRacing reader."""

    name = "stub"

    def __init__(self):
        self.asked = []

    def is_connected(self) -> bool:
        return True

    def get_telemetry_snapshot(self):
        return None

    def raw_channels(self, names=()):
        self.asked.append(("raw", list(names)))
        return {"available": True, "values": {"Speed": 61.1}}

    def list_channels(self, match=""):
        self.asked.append(("list", match))
        return {"available": True, "channels": {"Speed": 61.1}}

    def session_yaml(self, key=""):
        self.asked.append(("yaml", key))
        return {"available": True, "blocks": {"WeekendInfo": ["TrackName"]}}


def test_the_diagnostic_endpoints_go_through_the_adapter(tmp_path):
    """They used to read `_ir`, `_session_type` and pyirsdk's own internals.

    Anything but the iRacing reader answered `{"available": false}`, so a
    second backend would satisfy the interface and still break the panel —
    quietly, which is the worst way for a diagnostic to fail.
    """
    config = _config(tmp_path)
    orch = Orchestrator(config)
    stub = _StubAdapter()
    orch.telemetry = stub
    client = _client(config, orch)

    raw = client.get("/api/telemetry/raw?channels=Speed,RPM").json()
    listed = client.get("/api/telemetry/channels?match=speed").json()
    session = client.get("/api/telemetry/session?key=WeekendInfo").json()

    assert raw["values"] == {"Speed": 61.1}
    assert listed["channels"] == {"Speed": 61.1}
    assert session["blocks"] == {"WeekendInfo": ["TrackName"]}
    assert stub.asked == [("raw", ["Speed", "RPM"]),
                          ("list", "speed"),
                          ("yaml", "WeekendInfo")]


def test_a_source_without_raw_channels_says_which_source_it_was(tmp_path):
    """The simulator has no channels to dump; it has to say so, by name."""
    config = _config(tmp_path)
    client = _client(config)

    body = client.get("/api/telemetry/raw").json()

    assert body["available"] is False
    assert "simulated" in body["reason"]


def test_a_reset_keeps_settings_this_build_does_not_know_about(tmp_path):
    """`_deep_merge` preserves unknown keys so a config written by a newer
    build survives being read by an older one. Reset assigned the defaults
    wholesale and quietly opted out of that."""
    config = _config(tmp_path)
    config.update({"radio_effect": {"noise": 0.99, "future_knob": 7}})
    client = _client(config)

    client.post("/api/config/reset/radio_effect")

    assert config.radio_effect["noise"] == 0.08, "the reset did not run"
    assert config.radio_effect["future_knob"] == 7


def test_the_preview_and_the_playback_hear_the_same_radio(tmp_path):
    """Both paths read this section, and their fallbacks disagreed — 0.85
    against 0.75 for intensity — so a config missing the key made the tuning
    preview sound unlike the thing being tuned."""
    from ai_race_engineer.orchestrator import Orchestrator

    config = AppConfig({"telemetry": {"source": "simulated"},
                        "radio_effect": {"low_cut": 400}},
                       path=tmp_path / "config.json")
    # A section with keys missing entirely, as an older config file has.
    del config.radio_effect["intensity"]
    orch = Orchestrator(config)

    settings = config.radio_effect_settings()

    assert settings["intensity"] == 0.85          # what ships, from one place
    assert settings["low_cut"] == 400.0
    assert orch.config.radio_effect_settings() == settings


def test_a_reset_that_cannot_be_written_says_so(tmp_path, monkeypatch):
    """`/api/config` already reported a failed write; the reset pair did not.

    Silently, the panel showed defaults, the process ran on them, and the
    next launch read the old file back.
    """
    config = _config(tmp_path, radio_effect={"noise": 0.99})
    client = _client(config)

    def refuse():
        raise OSError("Read-only file system")

    monkeypatch.setattr(config, "save", refuse)

    for path in ("/api/config/reset", "/api/config/reset/radio_effect"):
        response = client.post(path)
        assert response.status_code == 500
        assert "Read-only" in response.json()["detail"]


def test_the_static_mount_asks_about_the_directory_it_mounts(tmp_path):
    """It was conditioned on the panel's directory and mounted the parent."""
    import inspect

    from ai_race_engineer import web_ui

    source = inspect.getsource(web_ui.create_app)

    assert "if STATIC_DIR.exists():" in source
    assert "if UI_DIR.exists():" not in source
