"""Keep every test out of the developer's real config folder.

Many tests build `AppConfig(data)` with no path, which resolves to
`~/.config/AIRE/config.json` — and everything that lives beside it: the
Radio Log, profiles, the speech cache. With the Radio Log on by default, a
test run wrote exchanges into the real one. Pointing AIRE_CONFIG at the
test's own temporary folder makes "no path" mean "somewhere disposable".
Tests that set AIRE_CONFIG themselves still win; monkeypatch applies last.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRE_CONFIG", str(tmp_path / "aire-config" / "config.json"))
