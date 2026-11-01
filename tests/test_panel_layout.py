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
    for control_id in ("diagnostic-trace", "pit-commands", "pit-confirm"):
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


README = PANEL.parents[3] / "README.md"
# The ones the user asked to keep at full length: pit-command risk, and every
# note about what a setting costs. Everything else says what a control does in
# a line, and links to the README for why.
_HINT_WORDS = 25


def _github_slug(heading: str) -> str:
    """The anchor GitHub gives a README heading."""
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s", "-", text)


def test_every_more_link_lands_on_a_readme_heading():
    """A "More" link is the hint's other half. A renamed heading would leave
    it opening the top of the README with nothing to say it broke."""
    panel = PANEL.read_text(encoding="utf-8")
    slugs = {_github_slug(line.lstrip("#"))
             for line in README.read_text(encoding="utf-8").splitlines()
             if line.startswith("#")}
    anchors = set(re.findall(r"docs\('([^']+)'\)", panel))
    assert anchors, "no README links found — has docs() been renamed?"
    assert not anchors - slugs, f"links to headings that do not exist: {anchors - slugs}"


def test_hints_stay_a_line_long():
    """The panel ran to about sixty hint paragraphs, most of them 20-40 words,
    and read as a wall of text. A hint says what the control does; the
    README says why. Only the pit-command risk and cost notes, marked
    hint-keep, may run longer."""
    panel = PANEL.read_text(encoding="utf-8")
    long = []
    for m in re.finditer(r'<p class="(hint[^"]*)"[^>]*>(.*?)</p>', panel, re.S):
        if "hint-keep" in m.group(1):
            continue
        words = re.sub(r"\{\{.*?\}\}|<[^>]+>", " ", m.group(2)).split()
        if len(words) > _HINT_WORDS:
            long.append(f"{len(words)} words: {' '.join(words)[:80]}")
    assert not long, "\n".join(long)


def test_sections_sit_on_the_tab_their_subject_belongs_to():
    """Radio effect is audio processing, the speech cache and Radio Log settings are
    app housekeeping, and pit commands are not telemetry plumbing."""
    panel = PANEL.read_text(encoding="utf-8")
    sections = dict((title, tab) for tab, title in re.findall(
        r'<section class="settings-section" v-show="tab === \'(\w+)\'">\s*<h2>(.*?)</h2>',
        panel))
    assert sections["Radio Effect"] == "audio"
    assert sections["Speech Cache"] == "app"
    assert sections["Radio Log"] == "app"
    assert sections["Appearance"] == "app"
    assert sections["Pit Commands"] == "race"
    assert 'v-model="theme"' in panel.split('<h2>Appearance</h2>')[1].split("</section>")[0]


def _js_method(name: str) -> str:
    app = APP_JS.read_text(encoding="utf-8")
    start = app.index(f"    {name}() {{")
    return app[start:app.index("\n    },\n", start)]


def test_the_status_bar_reads_what_is_running_not_what_is_saved():
    """The status bar once said "no-hotkey" next to a key that worked: it
    reported a startup failure recorded once, while the badge beside it read
    the binding live. Config may hold an unsaved change, so the bar reads
    `status` only — the push-to-talk pill above all."""
    for method in ("engineState", "statusChecks"):
        body = _js_method(method)
        assert "this.status" in body, method
        assert not re.search(r"\bconfig\??\.|\bkeyLabel\b", body), method
    assert re.search(r"\bs\.hotkey\b(?!_)", _js_method("statusChecks"))


def _status_bar(panel: str) -> str:
    """The status block, by its own markers. It used to be sliced up to the
    tab bar's </nav>, which stopped working when the bar moved below it in
    the sidebar: the slice came back empty."""
    start = panel.index('<div class="status-bar"')
    bar = panel[start:panel.index("<!-- /status-bar -->", start)]
    assert "status-pill" in bar and "engine-state" in bar, "status block is empty"
    return bar


def test_status_pills_take_you_to_the_tab_that_fixes_them():
    panel = PANEL.read_text(encoding="utf-8")
    bar = _status_bar(panel)
    assert re.search(r'<button[^>]*class="status-pill"[^>]*\n?[^>]*@click="selectTab\(check\.tab\)"', bar, re.S)


def test_the_status_bar_speaks_plainly():
    """"STT", "TTS" and "irsdk" are this codebase's words, not the driver's —
    and STT and TTS were two red dots for one missing ElevenLabs key."""
    panel = PANEL.read_text(encoding="utf-8")
    bar = _status_bar(panel)
    texts = re.findall(r"text: [`']([^`']*)[`']", _js_method("statusChecks"))
    labels = re.findall(r"label: '([^']*)'", _js_method("engineState"))
    assert texts and labels
    for shown in [bar, *texts, *labels]:
        assert not re.search(r"\b(STT|TTS|irsdk)\b", shown), shown


def test_the_panel_asks_in_its_own_dialogs_not_the_browsers():
    """A browser confirm box in the desktop window is drawn by the webview
    and titled "127.0.0.1:9420 says" — a web page's prompt, in an app. The
    profile delete and Restore all settings both used one."""
    for source in (APP_JS, PANEL):
        text = source.read_text(encoding="utf-8")
        found = re.findall(r"\b(?:window\.)?(?:confirm|alert|prompt)\(", text)
        assert not found, f"{source.name}: {found}"
    assert "confirmDialog(" in APP_JS.read_text(encoding="utf-8")


def test_every_tell_tale_has_an_icon_and_a_name():
    """The status checks are icon tiles. An icon missing from the sprite
    draws an empty tile that says nothing, and a tile with no accessible
    name is a blank button to a screen reader."""
    panel = PANEL.read_text(encoding="utf-8")
    body = _js_method("statusChecks")
    icons_block = body[body.index("const icons = {"):body.index("};", body.index("const icons = {"))]
    named = set(re.findall(r"'([\w-]+)'", icons_block)) - {"controller"}
    assert named, "no icons named in statusChecks()"
    symbols = set(re.findall(r'<symbol id="i-([\w-]+)"', panel))
    assert not named - symbols, f"icons missing from the sprite: {named - symbols}"

    bar = _status_bar(panel)
    assert ':aria-label="check.text"' in bar
    assert "check.icon" in bar


def test_the_pit_confirm_switch_reads_on_as_skip():
    """The switch says "Skip confirmation", so on must store
    confirm_pit_stop_commands = false. Bound straight to the setting, the
    words and the behaviour would say opposite things about the one control
    that changes the car without asking."""
    panel = PANEL.read_text(encoding="utf-8")
    toggle = re.search(r'<input type="checkbox" id="pit-confirm"[^>]*>', panel).group(0)
    assert 'v-model="skipPitConfirm"' in toggle, toggle
    assert "Skip confirmation" in panel.split('id="pit-confirm"')[1][:300]

    app = APP_JS.read_text(encoding="utf-8")
    block = app[app.index("    skipPitConfirm: {"):app.index("    },", app.index("    skipPitConfirm: {"))]
    assert "confirm_pit_stop_commands === false" in block
    assert "confirm_pit_stop_commands = !skip" in block


def test_the_radio_log_is_a_page_with_search_and_export():
    """It replaced a popup that showed twenty lines and could not be searched,
    kept or sent anywhere."""
    panel = PANEL.read_text(encoding="utf-8")
    page = panel[panel.index('v-show="tab === \'log\'"'):]
    page = page[:page.index("</section>")]
    assert 'type="search"' in page and 'v-model="logQuery"' in page
    assert '@click="openExport"' in page
    assert "rate(e, 'down')" in page, "no way to mark a bad answer"
    assert "showExchangeLog" not in panel, "the old popup is back"
    app = APP_JS.read_text(encoding="utf-8")
    assert "{ id: 'log'" in app
    # The call itself: a comment mentioning the parameter passed this once.
    assert "api(`/api/log?since_rev=${this.logRev}`)" in app, \
        "the poll fetches the whole log again"


def test_each_api_key_field_says_where_to_get_a_key():
    panel = PANEL.read_text(encoding="utf-8")
    llm = panel[panel.index('v-model="activeProvider.api_key"'):]
    llm = llm[:llm.index("</div>")]
    assert "https://openrouter.ai/settings/keys" in llm
    assert "https://openrouter.ai/docs/api_reference/authentication" in llm

    el = panel[panel.index('v-model="config.elevenlabs.api_key"'):]
    el = el[:el.index("</div>")]
    assert "https://elevenlabs.io/app/developers/api-keys" in el
    assert "https://elevenlabs.io/docs/api-reference/authentication" in el


def test_a_note_can_be_deleted_without_losing_the_rating():
    panel = PANEL.read_text(encoding="utf-8")
    assert 'aria-label="Delete note"' in panel and '@click="deleteNote(e)"' in panel
    body = _js_method_async("deleteNote")
    assert "this.sendFeedback(entry, this.rating(entry), '')" in body


def test_testing_a_connection_saves_nothing():
    """Test connection saved every setting first, so a test was also an
    unannounced Save of whatever else had been changed."""
    for name in ("testLLM", "testElevenLabs", "loadModels"):
        assert "saveConfig" not in _js_method_async(name), name


def test_the_pit_tell_tale_is_always_shown_and_green_when_on():
    body = _js_method("statusChecks")
    pit = body[body.index("if (!s.pit_commands_enabled)"):body.index("const icons")]
    assert "text: 'Pit commands off', level: 'idle'" in pit
    assert "text: 'Pit commands on', level: 'ok'" in pit


def _js_method_async(name: str) -> str:
    app = APP_JS.read_text(encoding="utf-8")
    start = app.index(f"    async {name}(")
    return app[start:app.index("\n    },\n", start)]


def test_esc_closes_whichever_dialog_is_on_top():
    """Handled on each dialog, Esc only worked once something inside it had
    focus — after a click on the page behind, never."""
    app = APP_JS.read_text(encoding="utf-8")
    handler = app[app.index("this._onKeyDown = (e) => {"):]
    handler = handler[:handler.index("\n    };")]
    for closes in ("this.closeDialog(null)", "this.exportOpen = false",
                   "this.showProfiles = false", "this.showAbout = false"):
        assert closes in handler, closes


def test_connection_results_are_toasts_not_panel_text():
    """A result left on the page described settings that may since have
    changed. A toast says it once and goes."""
    panel = PANEL.read_text(encoding="utf-8")
    assert "llmTestResult" not in panel and "elevenLabsTest" not in panel
    for name in ("testLLM", "testElevenLabs"):
        assert "this.notify(" in _js_method_async(name), name


def test_the_clear_dialog_always_lists_every_choice():
    """It used to leave out any choice that would remove nothing, so a fresh
    log offered only "Everything" and the rest looked missing."""
    app = APP_JS.read_text(encoding="utf-8")
    choices = app[app.index("    clearChoices() {"):]
    choices = choices[:choices.index("\n    },\n")]
    for label in ("Older than an hour", "Older than a day", "Older than a week",
                  "Older than 30 days", "Everything"):
        assert f"label: '{label}'" in choices, label
    returned = choices[choices.index("return ["):]
    assert ".filter(" not in returned, "choices are filtered out again"
    panel = PANEL.read_text(encoding="utf-8")
    assert ':disabled="!c.count"' in panel, "an empty choice must show disabled"


def test_a_toast_is_coloured_by_what_happened():
    css = CSS.read_text(encoding="utf-8")
    for kind in ("success", "error", "info"):
        rule = re.search(rf"\.notification\.{kind}\s*\{{([^}}]*)\}}", css)
        assert rule and "background" in rule.group(1), kind
    success = re.search(r"\.notification\.success\s*\{([^}]*)\}", css).group(1)
    base = re.search(r"\.notification\s*\{([^}]*)\}", css).group(1)
    background = lambda rule: re.search(r"background:\s*([^;]+);", rule).group(1)
    assert background(success) != background(base), "success looks like any other toast"
    assert "className = `notification ${kind}`" in _js_method_notify()


def _js_method_notify() -> str:
    app = APP_JS.read_text(encoding="utf-8")
    start = app.index("    notify(message")
    return app[start:app.index("\n    },\n", start)]
