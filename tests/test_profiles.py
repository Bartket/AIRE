"""Driver profiles: per-person settings that switch, and machine settings
that stay put.

What is pinned here: switching restores each driver's own values without
leaking the last driver's into them, nothing shared (keys, devices, the
wheel binding, the model) moves, a save lands in the active profile's file,
an id from the URL cannot name a file outside the profiles directory, and a
switch waits while the engineer is mid-answer or a pit command is pending.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_race_engineer.config import _DEFAULT_CONFIG, _PROFILE_KEYS, AppConfig
from ai_race_engineer.web_ui import create_app

PANEL = "127.0.0.1:9420"
EL_KEY = "sk_test_elevenlabs_key"
OR_KEY = "sk-or-test-openrouter-key"


def _config(tmp_path):
    return AppConfig({
        "telemetry": {"source": "simulated", "units": "metric"},
        "elevenlabs": {
            "api_key": EL_KEY,
            "cache_speech": False,
            "tts_voice_id": "voice-a",
            "pronunciation": {"Kowalski": "Ko-VAL-ski"},
        },
        "llm": {"openrouter": {"api_key": OR_KEY, "model": "anthropic/claude-haiku-5.5"}},
        "audio": {"input_device": 3, "output_device": 5},
        "push_to_button": {"type": "controller", "device_guid": "wheel-guid", "button": 7},
        "driver_callsign": "Alpha",
    }, path=tmp_path / "config.json")


def _client(config):
    return TestClient(create_app(config), base_url=f"http://{PANEL}")


def _new_profile(client, name):
    response = client.post("/api/profiles", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _activate(client, profile_id):
    return client.post(f"/api/profiles/{profile_id}/activate")


def _profile_file(config, profile_id):
    path = config.path.parent / "profiles" / f"{profile_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ── Switching ───────────────────────────────────────────────────────────

def test_switching_back_and_forth_restores_each_drivers_settings(tmp_path):
    config = _config(tmp_path)
    client = _client(config)
    b = _new_profile(client, "Driver B")

    assert _activate(client, b).status_code == 200
    client.post("/api/config", json={
        "driver_callsign": "Bravo",
        "radio_effect": {"intensity": 0.3},
        "telemetry": {"units": "imperial"},
    })

    assert _activate(client, "default").status_code == 200
    assert config.driver_callsign == "Alpha"
    assert config.radio_effect["intensity"] == _DEFAULT_CONFIG["radio_effect"]["intensity"]
    assert config.telemetry["units"] == "metric"

    assert _activate(client, b).status_code == 200
    assert config.driver_callsign == "Bravo"
    assert config.radio_effect["intensity"] == 0.3
    assert config.telemetry["units"] == "imperial"


def test_one_drivers_pronunciations_do_not_leak_into_anothers(tmp_path):
    """A dict only gains keys in a deep merge, so applying a profile with
    `update()` kept every respelling of the driver switched away from."""
    config = _config(tmp_path)
    client = _client(config)
    config.profiles().write(
        "b", {"elevenlabs": {"pronunciation": {"Nowak": "NO-vak"}}}, "Driver B")
    b = "b"
    _activate(client, b)
    assert config.elevenlabs["pronunciation"] == {"Nowak": "NO-vak"}

    _activate(client, "default")
    assert config.elevenlabs["pronunciation"] == {"Kowalski": "Ko-VAL-ski"}

    _activate(client, b)
    assert config.elevenlabs["pronunciation"] == {"Nowak": "NO-vak"}


def test_a_setting_missing_from_a_profile_takes_the_shipped_value(tmp_path):
    """A profile written before a setting existed must not inherit whatever
    the previous driver had for it."""
    config = _config(tmp_path)
    client = _client(config)
    client.post("/api/config", json={"audio": {"min_reply_seconds": 4.0},
                                     "radio_effect": {"noise": 0.5}})
    config.profiles().write("old", {"driver_callsign": "Old",
                                    "radio_effect": {"intensity": 0.2}}, "Old")

    assert _activate(client, "old").status_code == 200

    assert config.driver_callsign == "Old"
    assert config.audio["min_reply_seconds"] == _DEFAULT_CONFIG["audio"]["min_reply_seconds"]
    assert config.radio_effect["intensity"] == 0.2
    assert config.radio_effect["noise"] == _DEFAULT_CONFIG["radio_effect"]["noise"]


def test_switching_leaves_shared_settings_alone(tmp_path):
    config = _config(tmp_path)
    client = _client(config)
    b = _new_profile(client, "Driver B")
    config.profiles().write(b, {"driver_callsign": "Bravo"})

    assert _activate(client, b).status_code == 200

    assert config.elevenlabs_key() == EL_KEY
    assert config.llm_api_key("openrouter") == OR_KEY
    assert config.llm["openrouter"]["model"] == "anthropic/claude-haiku-5.5"
    assert config.audio["input_device"] == 3
    assert config.audio["output_device"] == 5
    ptb = config.push_to_button
    assert (ptb["type"], ptb["device_guid"], ptb["button"]) == ("controller", "wheel-guid", 7)
    assert config.telemetry["source"] == "simulated"


def test_a_new_profile_starts_on_the_shipped_settings(tmp_path):
    """Not a copy of whoever is driving: a new driver gets a clean setup,
    and the one being copied keeps theirs."""
    config = _config(tmp_path)
    client = _client(config)
    b = _new_profile(client, "Driver B")

    settings = _profile_file(config, b)["settings"]
    assert settings["elevenlabs"]["tts_voice_id"] == ""
    assert settings["elevenlabs"]["pronunciation"] == {}
    assert settings["driver_callsign"] == ""
    assert settings["telemetry"]["units"] == "auto"
    # An unedited prompt is left out, so the profile tracks the shipped one.
    assert "system_prompt" not in settings

    assert config.active_profile == "default", "creating must not switch"
    assert config.elevenlabs["tts_voice_id"] == "voice-a"
    assert config.driver_callsign == "Alpha"


def test_a_profile_without_a_voice_still_speaks():
    """A profile on the shipped settings has no voice chosen. The voice
    must fall back to the default rather than ask ElevenLabs for nothing."""
    from ai_race_engineer.tts import DEFAULT_VOICE_ID, TTS
    assert TTS("key", voice_id="").voice_id == DEFAULT_VOICE_ID


# ── Saving ──────────────────────────────────────────────────────────────

def test_a_settings_save_lands_in_the_active_profile(tmp_path):
    config = _config(tmp_path)
    client = _client(config)

    client.post("/api/config", json={"driver_callsign": "Ace"})

    saved = _profile_file(config, "default")
    assert saved["name"] == "Default"
    assert saved["settings"]["driver_callsign"] == "Ace"
    # Machine settings are not copied into a person's profile.
    assert "api_key" not in json.dumps(saved)
    assert "input_device" not in saved["settings"].get("audio", {})


def test_a_stale_panel_view_cannot_change_the_active_profile(tmp_path):
    """The panel posts back the whole config it loaded, active profile and
    all. A view loaded before a switch would move the settings into the
    driver switched away from."""
    config = _config(tmp_path)
    client = _client(config)
    stale = client.get("/api/config").json()
    b = _new_profile(client, "Driver B")
    _activate(client, b)

    client.post("/api/config", json=stale)

    assert config.active_profile == b


def test_first_run_lists_the_existing_settings_as_default(tmp_path):
    config = _config(tmp_path)
    before = config.to_dict()
    client = _client(config)

    listing = client.get("/api/profiles").json()

    assert listing["active"] == "default"
    assert listing["profiles"] == [
        {"id": "default", "name": "Default", "initials": "DE", "color": "orange"}]
    assert config.to_dict() == before


def test_the_default_prompt_is_not_frozen_into_a_profile(tmp_path):
    """Same rule as config.json: an unedited prompt tracks the shipped one.
    Both ways a profile file gets written: a save, and the first listing on
    an install that has not saved since profiles arrived. The listing path
    froze the prompt until this was checked in the running app."""
    config = _config(tmp_path)
    _client(config).get("/api/profiles")
    assert "system_prompt" not in _profile_file(config, "default")["settings"]

    config.save()
    assert "system_prompt" not in _profile_file(config, "default")["settings"]

    config.update({"system_prompt": "Be terse."})
    config.save()
    assert _profile_file(config, "default")["settings"]["system_prompt"] == "Be terse."


# ── Ids and management ──────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["..%2Fconfig", "a%2Fb", "Default", "-x", "x" * 41])
def test_an_id_cannot_name_a_file_outside_the_profiles_directory(tmp_path, bad):
    config = _config(tmp_path)
    client = _client(config)
    client.get("/api/profiles")
    before = sorted(p.name for p in tmp_path.rglob("*"))

    for action in ("activate", "edit", "delete"):
        response = client.post(f"/api/profiles/{bad}/{action}",
                               json={"name": "X", "color": "teal"})
        assert response.status_code in (400, 404), (action, response.status_code)

    assert sorted(p.name for p in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("bad", ["../config", "..\\config", "sub/x", "", "Default"])
def test_the_store_refuses_an_id_that_is_not_a_plain_name(tmp_path, bad):
    """The guard that matters is here, not in the router. Routing happens
    to keep a "/" out of a URL segment, which is why the HTTP test above
    passed with validation deleted; this one does not."""
    from ai_race_engineer.profiles import ProfileError, ProfileStore

    store = ProfileStore(tmp_path / "profiles")
    for call in (lambda: store.write(bad, {"x": 1}, "X"),
                 lambda: store.read(bad),
                 lambda: store.delete(bad)):
        with pytest.raises(ProfileError):
            call()
    assert not (tmp_path / "config.json").exists()
    assert not store.exists(bad)


def test_the_active_profile_cannot_be_deleted(tmp_path):
    """Which also covers the last one: it is always the active one."""
    config = _config(tmp_path)
    client = _client(config)
    client.get("/api/profiles")

    assert client.post("/api/profiles/default/delete").status_code == 409
    assert (config.path.parent / "profiles" / "default.json").is_file()


def _listed(response, profile_id):
    return next(p for p in response.json()["profiles"] if p["id"] == profile_id)


def test_rename_and_delete(tmp_path):
    config = _config(tmp_path)
    client = _client(config)
    b = _new_profile(client, "Driver B")

    renamed = client.post(f"/api/profiles/{b}/edit", json={"name": "Bea", "color": "teal"})
    assert _listed(renamed, b)["name"] == "Bea"
    assert client.post("/api/profiles", json={"name": "bea"}).status_code == 400

    after = client.post(f"/api/profiles/{b}/delete").json()
    assert [p["id"] for p in after["profiles"]] == ["default"]


# ── Badge: initials and colour ──────────────────────────────────────────

def test_a_badge_survives_the_save_that_follows_it(tmp_path):
    """Every settings save rewrites the active profile's file with the
    settings alone. A write that only kept the name would reset the badge
    the first time the driver touched a slider."""
    config = _config(tmp_path)
    client = _client(config)
    client.get("/api/profiles")

    edited = client.post("/api/profiles/default/edit",
                         json={"name": "Bartosz", "initials": "bk", "color": "teal"})
    assert edited.status_code == 200, edited.text

    client.post("/api/config", json={"radio_effect": {"noise": 0.3}})
    config.save()

    badge = _listed(client.get("/api/profiles"), "default")
    assert (badge["initials"], badge["color"]) == ("BK", "teal")
    assert _profile_file(config, "default")["settings"]["radio_effect"]["noise"] == 0.3


def test_a_colour_outside_the_palette_is_refused(tmp_path):
    client = _client(_config(tmp_path))
    client.get("/api/profiles")
    for color in ("#ff00ff", "chartreuse", None):
        response = client.post("/api/profiles/default/edit",
                               json={"name": "Default", "color": color})
        assert response.status_code == 400, color
    assert client.post("/api/profiles", json={"name": "B", "color": "#fff"}).status_code == 400


@pytest.mark.parametrize("typed, shown", [
    ("bkx", "BK"), ("b.k", "BK"), ("  z ", "Z"), ("7", "7"), ("łó", "ŁÓ"),
    ("", "MV"), ("!!", "MV"),
])
def test_initials_are_two_letters_and_blank_comes_from_the_name(tmp_path, typed, shown):
    client = _client(_config(tmp_path))
    b = _new_profile(client, "Max Verde")
    response = client.post(f"/api/profiles/{b}/edit",
                           json={"name": "Max Verde", "initials": typed, "color": "blue"})
    assert _listed(response, b)["initials"] == shown


def test_a_new_profile_gets_a_colour_nobody_else_has(tmp_path):
    """Two orange "DR" badges cannot be told apart at a glance."""
    client = _client(_config(tmp_path))
    client.get("/api/profiles")
    b = _new_profile(client, "Driver B")
    c = _new_profile(client, "Driver C")
    colors = {p["id"]: p["color"] for p in client.get("/api/profiles").json()["profiles"]}
    assert len(set(colors.values())) == 3, colors
    assert colors[b] != "orange" and colors[c] != "orange"


def test_the_panel_draws_its_swatches_from_the_list_the_server_checks():
    from ai_race_engineer.profiles import COLORS
    css = (Path(__file__).resolve().parent.parent / "ai_race_engineer" / "static"
           / "ui" / "style.css").read_text(encoding="utf-8")
    # The base rule, at the start of a line: the light-theme overrides name
    # every colour too, so a plain substring passed with a base rule gone.
    missing = [c for c in COLORS if not re.search(rf"^\.badge-{c}\s*\{{", css, re.M)]
    assert not missing, f"no badge colour in style.css for: {missing}"


# ── Timing ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("state", ["recording", "thinking", "speaking"])
def test_a_switch_waits_while_the_engineer_is_busy(tmp_path, state):
    config = _config(tmp_path)
    client = _client(config)
    b = _new_profile(client, "Driver B")
    client.app.state.orchestrator._status = state

    response = _activate(client, b)

    assert response.status_code == 409
    assert config.active_profile == "default"


def test_a_switch_waits_while_a_pit_command_is_pending(tmp_path):
    config = _config(tmp_path)
    client = _client(config)
    b = _new_profile(client, "Driver B")
    client.app.state.orchestrator._pending_pit_command = object()

    assert _activate(client, b).status_code == 409
    assert config.active_profile == "default"


def test_a_cross_site_switch_is_refused(tmp_path):
    config = _config(tmp_path)
    client = _client(config)
    b = _new_profile(client, "Driver B")

    response = client.post(f"/api/profiles/{b}/activate",
                           headers={"Origin": "https://evil.example"})

    assert response.status_code == 403
    assert config.active_profile == "default"


# ── The table itself ────────────────────────────────────────────────────

def test_every_profile_setting_is_a_shipped_setting():
    """A path that does not exist would be silently skipped forever."""
    for path in _PROFILE_KEYS:
        node = _DEFAULT_CONFIG
        for key in path:
            assert isinstance(node, dict) and key in node, path
            node = node[key]


def test_no_secret_or_machine_setting_is_in_a_profile():
    flat = {".".join(p) for p in _PROFILE_KEYS}
    for shared in ("elevenlabs.api_key", "llm", "audio.input_device",
                   "audio.output_device", "push_to_button.key",
                   "push_to_button.button", "push_to_button.device_guid",
                   "active_profile"):
        assert not any(f == shared or f.startswith(shared + ".") or shared.startswith(f + ".")
                       for f in flat), shared
