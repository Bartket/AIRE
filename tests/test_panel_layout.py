"""Structural checks on the settings page.

The panel has no JS tests, and a broken layout is invisible to every other
check in this repo — the app starts, the API answers, the settings save.
It just looks wrong, and only to whoever opens the tab.

    uv run pytest tests/ -q
"""

import re
from pathlib import Path

PANEL = (Path(__file__).resolve().parent.parent / "ai_race_engineer"
         / "static" / "ui" / "panel.html")
_RANGE_ROW = re.compile(r'<div class="range-row">.*?</div>', re.S)


def _rows():
    return _RANGE_ROW.findall(PANEL.read_text(encoding="utf-8"))


def test_no_help_text_inside_a_slider_row():
    """A hint paragraph inside .range-row collapses the slider to nothing.

    .range-row is display:flex and the input is flex:1 with min-width:0,
    so a third child takes the width and the track shrinks to zero — the
    thumb is left floating on its own with the value stranded at the far
    right. Five sliders shipped like that: temperature, radio intensity,
    low cut, high cut and duck level.

    The hint belongs after the row, as a sibling, like every other field.
    """
    broken = []
    for row in _rows():
        if 'class="hint"' in row:
            name = re.search(r'v-model[.\w]*="([^"]+)"', row)
            broken.append(name.group(1) if name else row[:60])
    assert not broken, f"hint inside .range-row collapses these sliders: {broken}"


def test_adjacent_toggle_hints_stay_with_their_controls():
    """Standalone hints inherit the previous control's bottom margin.

    That leaves a large gap below their own toggle and no gap before the next
    one, making the diagnostic and pit-command explanations look swapped.
    """
    panel = PANEL.read_text(encoding="utf-8")
    for control_id in ("diagnostic-trace", "pit-commands"):
        grouped = re.search(
            rf'<div class="form-group form-toggle with-hint">'
            rf'.*?id="{control_id}".*?<p class="hint">.*?</p>\s*</div>',
            panel,
            re.S,
        )
        assert grouped, f"{control_id} hint is not grouped with its toggle"

    css = CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.form-toggle\.with-hint\s*\{([^}]*)\}", css)
    assert rule and "display: grid" in rule.group(1)
    hint_rule = re.search(r"\.form-toggle\.with-hint \.hint\s*\{([^}]*)\}", css)
    assert hint_rule and "grid-column: 2" in hint_rule.group(1)


def test_every_slider_still_shows_its_value():
    """The readout is the only way to know where a slider is sitting."""
    missing = [r for r in _rows() if "range-value" not in r]
    assert not missing, f"{len(missing)} slider rows have no value readout"


def test_sliders_are_present_and_each_row_has_exactly_one():
    rows = _rows()
    assert len(rows) >= 13, f"only found {len(rows)} slider rows — did the page change?"
    for row in rows:
        sliders = row.count('type="range"')
        assert sliders == 1, f"row with {sliders} sliders"


# ── Voice favourites ────────────────────────────────────────────────

CSS = PANEL.parent / "style.css"
APP_JS = PANEL.parent / "app.js"


def test_the_voice_card_absorbs_free_space_around_its_buttons():
    """.voice-card is justify-content: space-between. With one button that
    puts the info left and the button right; adding the favourite star
    makes three children, and space-between would strand the star in the
    middle of the card. .voice-info must take flex:1 so the two buttons
    stay together on the right.

    Same failure as the hint inside .range-row — a new flex child taking
    space nobody budgeted for.
    """
    css = CSS.read_text(encoding="utf-8")
    info = re.search(r"\.voice-info\s*\{([^}]*)\}", css)
    assert info, ".voice-info rule is gone"
    assert "flex: 1" in info.group(1), "star would sit between info and preview"


def test_favourites_survive_a_settings_reset():
    """Favourites are found by ear across many previews, each costing a
    synthesis. A reset that wiped them would be worse than not having
    them."""
    from ai_race_engineer.config import AppConfig
    assert ("elevenlabs", "favourite_voices") in AppConfig._PROTECTED


def test_favourites_default_to_an_empty_list():
    from ai_race_engineer.config import _DEFAULT_CONFIG
    assert _DEFAULT_CONFIG["elevenlabs"]["favourite_voices"] == []


def test_favourite_toggle_tolerates_a_config_without_the_key():
    """A config.json written before this shipped has no array, and pushing
    onto undefined throws — which would break starring for every existing
    install rather than only the first click."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "Array.isArray(el.favourite_voices)" in js


def test_every_performance_setting_is_reachable_from_the_panel():
    """A setting that exists only in config.json cannot be found.

    CPU affinity shipped with a default, a reserve count and a whole
    ctypes implementation, and no way to turn it on short of editing JSON
    — so in practice it was off for everyone, including whoever wanted to
    measure whether it helps. It has since been deleted rather than fixed,
    but the guard stands: the next setting added here needs a control.
    """
    from ai_race_engineer.config import _DEFAULT_CONFIG

    panel = PANEL.read_text(encoding="utf-8")
    # Whole-word. When there were three affinity keys, one was a prefix of
    # another, and a plain substring test found "cpu_affinity" inside
    # "cpu_affinity_reserve" and passed with the checkbox deleted. It did.
    missing = [key for key in _DEFAULT_CONFIG["performance"]
               if not re.search(rf"config\.performance\.{key}\b", panel)]
    assert not missing, f"no control in the panel for: {missing}"


def test_a_long_option_cannot_stretch_a_tab():
    """A <select> sizes itself to its widest <option>, and the audio device
    lists are whatever Windows calls the hardware. One real name was 86
    characters, so the Audio tab rendered wider than every other tab and
    switching to it looked like the panel resizing itself.

    Same family as the grid-column fix on .settings-content: content
    deciding the width of a container that should be deciding it.
    """
    css = CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.form-group select\s*\{([^}]*)\}", css)
    assert rule, ".form-group select rule is gone"
    assert "max-width" in rule.group(1), \
        "a long device name will stretch the Audio tab again"


def test_a_device_name_is_fit_to_put_in_a_dropdown():
    """Windows hands PortAudio some names as unexpanded resource
    references, with a literal newline in the middle. Those reached the
    dropdown verbatim."""
    from ai_race_engineer.audio_handler import _device_name

    raw = ("Headset (@System32\\drivers\\bthhfenum.sys,#2;"
           "%1 Hands-Free%0\r\n;(QuietMax 7))")
    cleaned = _device_name(raw)
    assert cleaned == "Headset (QuietMax 7)", cleaned
    assert "\n" not in cleaned and "\r" not in cleaned

    # A name the driver got right is left alone apart from stray spacing.
    assert _device_name("Microphone (2 - Studio Condenser)") == \
        "Microphone (2 - Studio Condenser)"
    assert _device_name("  Speakers  (Desk  Monitors) ") == "Speakers (Desk Monitors)"
    assert _device_name("") == ""

    # And it is actually on the path the dropdown reads. Without this the
    # test passes with the call deleted, which is how it was written first.
    import inspect
    from ai_race_engineer.audio_handler import _list_devices
    assert "_device_name(" in inspect.getsource(_list_devices), \
        "device names reach the dropdown unfiltered"


def test_the_page_column_is_sized_by_the_page_not_by_the_tab():
    """body.settings-panel is a column flex container, and .app-container
    centres itself with `margin: 0 auto`. An auto cross-axis margin turns
    OFF `align-items: stretch`, so without an explicit width the container
    is sized to its content — and every tab is then as wide as its own
    widest line.

    Measured in the built app at 1180px: five tabs reached 1141px and Audio
    came out at 1135, which is the panel visibly changing size on click.
    Two earlier fixes went at what the content was doing; the content was
    never what decided the width.
    """
    css = CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.app-container\s*\{(.*?)\}", css, re.S)
    assert rule, ".app-container rule is gone"
    body = rule.group(1)
    assert re.search(r"^\s*width:\s*100%", body, re.M), \
        "container will size to the widest tab again"
    # The pairing is the whole point: width without the cap loses the
    # centred column, the cap without width is this bug.
    assert "max-width" in body and "margin: 0 auto" in body


def test_an_unsaved_input_type_change_is_called_out():
    """Switching Type edits local state only — nothing binds until Save.

    The panel renders the wheel section, stored button and all, the instant
    the picker changes, so it reads as switched while the keyboard is still
    the thing listening. That was reported as the wheel binding not working.
    The warning has to compare against the *live* binding from the status
    bar, not against config, because config is the thing that already moved.
    """
    panel = PANEL.read_text(encoding="utf-8")
    app = APP_JS.read_text(encoding="utf-8")

    assert "inputTypeNotApplied" in panel, "no unsaved-change warning on the Type picker"
    assert "inputTypeNotApplied()" in app
    # Derived from status.hotkey — the only field that says what is bound now.
    assert "this.status?.hotkey" in app
    assert "status.hotkey" in panel, "the warning must name what is still listening"
