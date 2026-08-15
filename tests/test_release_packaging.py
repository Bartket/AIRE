"""Release assets must remain self-contained and legally distributable."""

import re
import runpy
from pathlib import Path

import ai_race_engineer
from ai_race_engineer.resources import ICON_PATH, STATIC_DIR, UI_DIR


ROOT = Path(__file__).resolve().parent.parent


def test_the_iracing_sdk_is_not_vendored_at_the_repo_root():
    """A copy of pyirsdk here shadows the dependency the lock file pins.

    There was one: byte-identical to upstream, but it imported on every
    platform because its only third-party import is PyYAML, which arrives
    with `uvicorn[standard]`. `pyirsdk` is declared for Windows only, and
    that marker is what makes "auto" fall back to simulated telemetry
    elsewhere — so the copy defeated the fallback, and `--ask` off Windows
    answered "no telemetry" instead of using the simulator. It also meant
    the SDK actually running was invisible to `uv.lock` and to Dependabot.
    """
    assert not (ROOT / "irsdk.py").exists(), (
        "irsdk.py at the repo root shadows the pinned pyirsdk dependency; "
        "let the dependency provide it")


def test_versions_match_the_release_metadata():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    app_js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    version_info = (ROOT / "file_version_info.txt").read_text(encoding="utf-8")
    project_version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)

    assert ai_race_engineer.__version__ == project_version
    assert f"version: '{project_version}'" in app_js
    assert f"ProductVersion', '{project_version}'" in version_info


def test_packaged_assets_are_inside_the_python_package():
    package = ROOT / "ai_race_engineer"
    assert STATIC_DIR.is_relative_to(package)
    assert UI_DIR.joinpath("panel.html").is_file()
    assert ICON_PATH.is_file()


def test_settings_ui_has_no_remote_runtime_scripts():
    panel = (UI_DIR / "panel.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', panel)

    assert scripts
    assert all(src.startswith("/static/") for src in scripts)
    assert (STATIC_DIR / "vendor" / "vue-3.5.41.global.prod.js").is_file()
    assert (STATIC_DIR / "vendor" / "VUE-LICENSE.txt").is_file()
    assert "npm integrity" in (STATIC_DIR / "vendor" / "README.md").read_text()


def test_windows_build_copies_visible_notices_beside_the_executable():
    build = (ROOT / "build_windows.ps1").read_text(encoding="utf-8")
    for name in ("LICENSE", "README.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.txt"):
        assert f"dist\\AIRE\\{name}" in build
    assert "AIRE-v$version-win64.zip" in build
    assert "SHA256SUMS.txt" in build


def test_notice_generator_includes_vue_and_runtime_packages():
    script = runpy.run_path(str(ROOT / "tools" / "generate_third_party_notices.py"))
    notices = script["render_notices"]()
    assert "Vue.js 3.5.41" in notices
    assert "fastapi " in notices
    assert "MIT License" in notices
