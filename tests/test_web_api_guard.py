"""The local API must only answer the panel running on this machine.

Two holes, both reachable from any web page the driver happened to have
open while AIRE was running.

A cross-site HTML form post is a CORS "simple request": no preflight, no
consent, and the browser sends it happily. Every endpoint taking no request
body was therefore reachable from anywhere — `/api/config/reset` wiped a
tuned radio effect mid-session, `/api/speech_cache/clear` threw away speech
that costs money to synthesize again. The JSON endpoints escaped only
because FastAPI insists on a JSON body, which is luck rather than a guard.

And nothing checked `Host`, so a name an attacker owns, resolved to
127.0.0.1, made their page same-origin with this API.
"""

import pytest
from fastapi.testclient import TestClient

from ai_race_engineer.config import AppConfig
from ai_race_engineer.web_ui import create_app

PANEL = "127.0.0.1:9420"


def _client(tmp_path, bind_host="127.0.0.1"):
    """An app plus config, with the radio effect tuned away from its default.

    The reset endpoints are the ones under test, so the fixture has to hold
    a value a reset would visibly destroy.
    """
    config = AppConfig(
        {"telemetry": {"source": "simulated"}, "radio_effect": {"noise": 0.99}},
        path=tmp_path / "config.json",
    )
    app = create_app(config, bind_host=bind_host)
    # TestClient defaults to Host "testserver", which is exactly what the
    # guard exists to refuse.
    return TestClient(app, base_url=f"http://{PANEL}"), config


# ── Cross-site form posts ───────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/config/reset",
    "/api/config/reset/radio_effect",
    "/api/speech_cache/clear",
    "/api/llm/test",
    "/api/window/show",
])
def test_a_cross_site_form_post_is_refused(tmp_path, path):
    """The parameterless endpoints, which took no body to reach."""
    client, _ = _client(tmp_path)

    response = client.post(path, data={"anything": "1"},
                           headers={"Origin": "https://evil.example"})

    assert response.status_code == 403


def test_a_refused_reset_leaves_the_settings_alone(tmp_path):
    """The status code is not the point; the settings surviving is."""
    client, config = _client(tmp_path)

    client.post("/api/config/reset", data={"x": "1"},
                headers={"Origin": "https://evil.example"})

    assert config.radio_effect["noise"] == 0.99


def test_the_panels_own_post_still_works(tmp_path):
    """Same-origin is the case that must not regress: Origin matches Host."""
    client, config = _client(tmp_path)

    response = client.post("/api/config/reset/radio_effect",
                           headers={"Origin": f"http://{PANEL}"})

    assert response.status_code == 200
    assert config.radio_effect["noise"] != 0.99, "the reset did not run"


def test_a_client_that_sends_no_origin_still_works(tmp_path):
    """`single_instance.show_existing()` is an httpx POST, not a browser.

    No web page can make an Origin-less state-changing request, so absence
    is safe to allow — and refusing it would break raising the window when
    the app is launched a second time.
    """
    client, _ = _client(tmp_path)

    response = client.post("/api/window/show")

    assert response.status_code == 200


def test_a_null_origin_is_refused(tmp_path):
    """A sandboxed iframe posts `Origin: null`, which matches no host."""
    client, config = _client(tmp_path)

    response = client.post("/api/config/reset", headers={"Origin": "null"})

    assert response.status_code == 403
    assert config.radio_effect["noise"] == 0.99


# ── DNS rebinding ───────────────────────────────────────────────────────

@pytest.mark.parametrize("host", [
    "attacker.example",
    # Owned by whoever registers it, and free to resolve to 127.0.0.1 —
    # which is why this cannot be a prefix match on "127.".
    "127.0.0.1.attacker.example",
    "aire.local",
])
def test_a_foreign_host_header_is_refused(tmp_path, host):
    client, _ = _client(tmp_path)

    response = client.get("/api/status", headers={"Host": host})

    assert response.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:9420", "localhost:9420",
                                  "127.0.0.1", "[::1]:9420"])
def test_loopback_hosts_are_answered(tmp_path, host):
    """However the driver reached the panel, it has to keep working."""
    client, _ = _client(tmp_path)

    response = client.get("/api/health", headers={"Host": host})

    assert response.status_code == 200


def test_binding_to_a_public_address_keeps_the_cross_site_guard(tmp_path):
    """`--host 0.0.0.0` is a deliberate choice, so Host is no longer known.

    The cross-site guard does not depend on knowing it, and must survive.
    """
    client, config = _client(tmp_path, bind_host="0.0.0.0")

    assert client.get("/api/health", headers={"Host": "aire.lan:9420"}).status_code == 200
    refused = client.post("/api/config/reset",
                          headers={"Origin": "https://evil.example"})
    assert refused.status_code == 403
    assert config.radio_effect["noise"] == 0.99
