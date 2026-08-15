"""Pit writes are opt-in, exact, confirmed, and checked after broadcast."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_race_engineer.config import AppConfig, _DEFAULT_CONFIG
from ai_race_engineer.llm import PitCommandInterpretation
from ai_race_engineer.orchestrator import Orchestrator
from ai_race_engineer.pit_commands import (PitCommand, PitCommandError,
                                           PitTireCompound,
                                           might_be_pit_instruction,
                                           parse_pit_command,
                                           pit_amounts_match)
from ai_race_engineer.telemetry import (CarTelemetry, TelemetryAdapter,
                                        TelemetrySnapshot, TrackConfig)


def test_a_combined_command_is_parsed_to_exact_sdk_units():
    plan = parse_pit_command(
        "Pit stop all tires at 21 PSI, fuel 8 gallons, tear-off and fast repair")

    assert plan is not None
    assert plan.commands == (
        PitCommand("lf", 145), PitCommand("rf", 145),
        PitCommand("lr", 145), PitCommand("rr", 145),
        PitCommand("fuel", 30), PitCommand("ws"), PitCommand("fr"),
    )
    assert plan.summary == (
        "four tyres at 145 kPa, 30 litres of fuel, "
        "windscreen tear-off and fast repair")


def test_spoken_numbers_and_every_clear_mode_are_supported():
    assert parse_pit_command("pit stop four tyres and thirty litres").summary == \
        "four tyres and 30 litres of fuel"
    plan = parse_pit_command(
        "pit stop clear all, clear tyres, clear fuel, clear tearoff and clear fast repair")
    assert [command.mode for command in plan.commands] == [
        "clear", "clear_tires", "clear_fuel", "clear_ws", "clear_fr"]


def test_refuelling_can_be_reenabled_without_changing_its_existing_amount():
    for request in (
        "pit stop fuel",
        "pit stop add fuel",
        "pit stop enable refuelling",
        "pit stop re-enable refuel",
        "pit stop turn refueling back on",
    ):
        plan = parse_pit_command(request)
        assert plan.commands == (PitCommand("fuel", 0),)
        assert plan.summary == "fuel at the current amount"


def test_named_compounds_use_the_live_zero_based_mapping():
    compounds = (
        PitTireCompound("Dry", 0),
        PitTireCompound("Wet", 1),
        PitTireCompound("All-Purpose", 2),
    )
    wet = parse_pit_command("pit stop wet tires", compounds)
    all_purpose = parse_pit_command(
        "pit stop set tyre type to all purpose", compounds)

    assert wet.commands == (PitCommand("tc", 1),)
    assert wet.summary == "wet tyres"
    assert all_purpose.commands == (PitCommand("tc", 2),)
    assert all_purpose.summary == "all-purpose tyres"


def test_compound_commands_refuse_missing_or_unmatched_live_mappings():
    try:
        parse_pit_command("pit stop wet tyres")
    except PitCommandError as exc:
        assert "haven't got the tyre types" in str(exc)
    else:
        raise AssertionError("compound command worked without a live mapping")

    try:
        parse_pit_command(
            "pit stop soft tyres",
            (PitTireCompound("Dry", 0), PitTireCompound("Wet", 1)))
    except PitCommandError as exc:
        assert "available types are dry and wet" in str(exc)
    else:
        raise AssertionError("unavailable compound was accepted")


def test_natural_pit_candidate_gate_is_broad_only_around_write_language():
    for request in (
        "Box me for wets and thirty litres",
        "Can you put me on softs?",
        "Four tyres and 30 litres",
        "Wets",
        "No fuel at the stop",
        "Clear everything",
        "Enable refuelling",
        "Re-enable refuel",
        "Turn refueling back on",
        "Refuel",
        "New boots all round",
        "Change the front left",
        "Slicks",
    ):
        assert might_be_pit_instruction(request), request
    for question in (
        "How much fuel do I need?",
        "How are my tyres?",
        "Am I good to the end?",
    ):
        assert not might_be_pit_instruction(question), question


def test_natural_interpreter_cannot_invent_or_convert_amounts():
    assert pit_amounts_match(
        "Give me tyres at twenty-one PSI and thirty litres",
        "pit stop fuel thirty litres and four tyres at twenty-one psi",
    )
    assert not pit_amounts_match(
        "Give me 8 gallons", "pit stop fuel 30 litres")
    assert not pit_amounts_match(
        "Give me fuel to the end", "pit stop fuel 30 litres")
    assert not pit_amounts_match(
        "Give me 30 litres and tyres at 145 kPa", "pit stop fuel 30 litres")


def test_a_question_or_partly_understood_command_can_never_become_a_write():
    assert parse_pit_command("How much fuel should I add at my pit stop?") is None
    for unsafe in (
        "pit stop four tyres and maybe fuel",
        "pit stop four tyres without fuel",
        "pit stop fuel thirty",
    ):
        try:
            parse_pit_command(unsafe)
        except PitCommandError:
            pass
        else:
            raise AssertionError(f"unsafe command was accepted: {unsafe}")


class _CommandTelemetry(TelemetryAdapter):
    name = "irsdk"

    def __init__(self):
        self.applied = []

    def is_connected(self):
        return True

    def get_telemetry_snapshot(self):
        return TelemetrySnapshot(CarTelemetry(), TrackConfig(session_num=0))

    def pit_commands_ready(self):
        return True, ""

    def pit_tire_compounds(self):
        return (PitTireCompound("Dry", 0), PitTireCompound("Wet", 1))

    def apply_pit_commands(self, commands):
        self.applied.append(tuple(commands))
        return True, ""


def _orchestrator(tmp_path, enabled=True):
    config = AppConfig({
        "telemetry": {"source": "simulated"},
        "pit_commands": {"enabled": enabled},
    }, path=tmp_path / "config.json")
    orch = Orchestrator(config)
    orch.telemetry = _CommandTelemetry()
    return orch


def test_first_turn_only_reads_back_and_second_exact_turn_commits(tmp_path, monkeypatch):
    """The regression guard: removing confirmation makes the first assert fail."""
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        orch.llm, "generate",
        lambda *args: (_ for _ in ()).throw(AssertionError("command reached the LLM")))
    async def directly(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", directly)

    proposal = asyncio.run(orch.ask(
        "pit stop four tyres and thirty litres", speak=False))
    assert proposal["answer"] == \
        "Confirm: four tyres and 30 litres of fuel; say confirm to commit."
    assert orch.telemetry.applied == []

    committed = asyncio.run(orch.ask("confirm", speak=False))
    assert committed["answer"] == "Pit stop set."
    assert len(orch.telemetry.applied) == 1


def test_named_compound_is_read_back_before_it_is_written(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path)
    async def directly(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", directly)

    proposal = asyncio.run(orch.ask("pit stop wet tyres", speak=False))
    assert proposal["answer"] == \
        "Confirm: wet tyres; say confirm to commit."
    assert orch.telemetry.applied == []

    committed = asyncio.run(orch.ask("confirm", speak=False))
    assert committed["answer"] == "Pit stop set."
    assert orch.telemetry.applied == [(PitCommand("tc", 1),)]

    # Confirmation is consumed, so a duplicate transmission cannot repeat it.
    again = asyncio.run(orch.ask("confirm", speak=False))
    assert again["answer"] == "No pit command is waiting for confirmation."
    assert len(orch.telemetry.applied) == 1


def test_natural_language_is_interpreted_then_read_back_before_write(
    tmp_path, monkeypatch
):
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        orch.llm,
        "interpret_pit_command",
        lambda *_args: PitCommandInterpretation(
            "command", "pit stop wet tyres and fuel thirty litres and tearoff"),
    )
    monkeypatch.setattr(
        orch.llm, "generate",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("a pit instruction became an ordinary answer")))
    async def directly(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", directly)

    proposal = asyncio.run(orch.ask(
        "Box me for wets, add thirty litres and take the tear-off",
        speak=False,
    ))
    assert proposal["answer"] == (
        "Confirm: wet tyres, 30 litres of fuel and windscreen tear-off; "
        "say confirm to commit.")
    assert orch.telemetry.applied == []

    asyncio.run(orch.ask("confirm", speak=False))
    assert orch.telemetry.applied == [(
        PitCommand("tc", 1), PitCommand("fuel", 30), PitCommand("ws"))]


def test_pit_stop_prefix_does_not_force_the_driver_to_use_canonical_grammar(
    tmp_path, monkeypatch
):
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        orch.llm,
        "interpret_pit_command",
        lambda *_args: PitCommandInterpretation(
            "command", "pit stop wet tyres and clear fuel"),
    )
    async def directly(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", directly)

    proposal = asyncio.run(orch.ask(
        "Pit stop, put me on wets please, but don't add fuel", speak=False))
    assert proposal["answer"] == (
        "Confirm: wet tyres and clear fuel; say confirm to commit.")
    assert orch.telemetry.applied == []


def test_natural_request_can_reenable_the_existing_fuel_amount(
    tmp_path, monkeypatch
):
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        orch.llm,
        "interpret_pit_command",
        lambda *_args: PitCommandInterpretation("command", "pit stop fuel"),
    )
    async def directly(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", directly)

    proposal = asyncio.run(orch.ask("Turn refuelling back on", speak=False))
    assert proposal["answer"] == (
        "Confirm: fuel at the current amount; "
        "say confirm to commit.")
    assert orch.telemetry.applied == []

    asyncio.run(orch.ask("confirm", speak=False))
    assert orch.telemetry.applied == [(PitCommand("fuel", 0),)]


def test_advice_about_pit_service_remains_a_read_only_question(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        orch.llm,
        "interpret_pit_command",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("an obvious advice question reached the command classifier")),
    )
    monkeypatch.setattr(orch.llm, "generate", lambda *_args: "Stay on dry tyres.")
    async def directly(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", directly)

    answer = asyncio.run(orch.ask("Should I take wet tyres?", speak=False))
    assert answer["answer"] == "Stay on dry tyres."
    assert orch.telemetry.applied == []
    assert orch._pending_pit_command is None


def test_changed_or_invented_amounts_fail_before_readback(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        orch.llm,
        "interpret_pit_command",
        lambda *_args: PitCommandInterpretation(
            "command", "pit stop fuel 30 litres"),
    )
    async def directly(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", directly)

    answer = asyncio.run(orch.ask("Add 8 gallons at the stop", speak=False))
    assert "couldn't safely preserve the pit amounts" in answer["answer"]
    assert orch.telemetry.applied == []
    assert orch._pending_pit_command is None


def test_ambiguous_natural_pit_request_fails_closed(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        orch.llm,
        "interpret_pit_command",
        lambda *_args: PitCommandInterpretation("ambiguous"),
    )
    async def directly(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", directly)

    answer = asyncio.run(orch.ask("Put some fuel in at the stop", speak=False))
    assert "couldn't safely interpret" in answer["answer"]
    assert orch.telemetry.applied == []
    assert orch._pending_pit_command is None


def test_llm_pit_interpreter_returns_only_validated_json(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path)
    captured = {}

    def chat_raw(messages, max_tokens=None, tools=None, temperature=None):
        captured["messages"] = messages
        captured["temperature"] = temperature
        return {
            "content": '```json\n{"kind":"command","command":"pit stop wet tyres"}\n```'
        }

    monkeypatch.setattr(orch.llm, "_chat_raw", chat_raw)
    result = orch.llm.interpret_pit_command(
        "Can you put me on wets?", ["Dry", "Wet"])

    assert result == PitCommandInterpretation("command", "pit stop wet tyres")
    assert captured["temperature"] == 0.0
    assert "Available tyre types for this car: Dry, Wet" in \
        captured["messages"][0]["content"]

    monkeypatch.setattr(
        orch.llm, "_chat_raw", lambda *_args, **_kwargs: {"content": "not json"})
    assert orch.llm.interpret_pit_command("Wets", ["Wet"]).kind == "error"


def test_any_intervening_question_cancels_the_held_write(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path)
    asyncio.run(orch.ask("pit stop fast repair", speak=False))
    monkeypatch.setattr(orch.llm, "generate", lambda *args: "Ordinary answer.")
    async def directly(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", directly)

    answer = asyncio.run(orch.ask("How much fuel do I need?", speak=False))
    assert answer["answer"] == "Ordinary answer."
    confirm = asyncio.run(orch.ask("confirm", speak=False))
    assert confirm["answer"] == "No pit command is waiting for confirmation."
    assert orch.telemetry.applied == []


def test_confirmation_expires_and_a_session_change_destroys_it(tmp_path):
    orch = _orchestrator(tmp_path)
    asyncio.run(orch.ask("pit stop fast repair", speak=False))
    orch._pending_pit_command_at = time.monotonic() - 31
    expired = asyncio.run(orch.ask("confirm", speak=False))
    assert expired["answer"] == "No pit command is waiting for confirmation."

    asyncio.run(orch.ask("pit stop fast repair", speak=False))
    orch._adopt_session(TelemetrySnapshot(
        CarTelemetry(), TrackConfig(session_num=1)))
    changed = asyncio.run(orch.ask("confirm", speak=False))
    assert changed["answer"] == "No pit command is waiting for confirmation."
    assert orch.telemetry.applied == []


def test_commands_ship_off_and_the_ui_names_the_risk(tmp_path):
    assert _DEFAULT_CONFIG["pit_commands"]["enabled"] is False
    orch = _orchestrator(tmp_path, enabled=False)
    result = asyncio.run(orch.ask("pit stop clear all", speak=False))
    assert "at-your-own-risk setting" in result["answer"]
    assert orch.telemetry.applied == []

    # A hand-edited JSON string must not become truthy and silently opt in.
    string_false = _orchestrator(tmp_path, enabled="false")
    result = asyncio.run(string_false.ask("pit stop clear all", speak=False))
    assert "at-your-own-risk setting" in result["answer"]
    assert string_false.telemetry.applied == []

    panel = (Path(__file__).resolve().parent.parent
             / "ai_race_engineer" / "static" / "ui" / "panel.html").read_text(encoding="utf-8")
    assert 'v-model="config.pit_commands.enabled"' in panel
    assert "at my own risk" in panel
    assert "Speak naturally" in panel
    assert "<strong>confirm</strong>" in panel
    assert "result-bad" not in panel[panel.index('id="pit-commands"'):]
    assert "confirmPitCommands" not in panel
    app = (Path(__file__).resolve().parent.parent
           / "ai_race_engineer" / "static" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "confirmPitCommands" not in app


def test_reader_uses_runtime_enums_and_verifies_the_result(monkeypatch):
    from ai_race_engineer.telemetry import irsdk_reader

    reader = irsdk_reader.iRacingTelemetryReader()
    reader._started = True
    values = {
        "IsOnTrack": True,
        "PitSvFlags": 0,
        "PitSvFuel": 22.0,
        "PitSvLFP": 0.0,
        "PitSvRFP": 0.0,
        "PitSvLRP": 0.0,
        "PitSvRRP": 0.0,
        "PitSvTireCompound": 1,
        "DriverInfo": {
            "DriverTires": [
                {"TireIndex": 0, "TireCompoundType": "Dry"},
                {"TireIndex": 1, "TireCompoundType": "Wet"},
            ],
        },
    }
    flags = SimpleNamespace(
        lf_tire_change=1, rf_tire_change=2, lr_tire_change=4,
        rr_tire_change=8, fuel_fill=16, windshield_tearoff=32,
        fast_repair=64)
    modes = SimpleNamespace(
        clear=100, ws=101, fuel=102, lf=103, rf=104, lr=105, rr=106,
        clear_tires=107, fr=108, clear_ws=109, clear_fr=110,
        clear_fuel=111, tc=112)
    monkeypatch.setattr(
        irsdk_reader, "irsdk",
        SimpleNamespace(PitSvFlags=flags, PitCommandMode=modes))

    class IR:
        is_initialized = True
        is_connected = True

        def pit_command(self, mode, value):
            if mode == modes.clear:
                values["PitSvFlags"] = 0
                return True
            if mode == modes.clear_tires:
                values["PitSvFlags"] &= ~(
                    flags.lf_tire_change | flags.rf_tire_change
                    | flags.lr_tire_change | flags.rr_tire_change)
                return True
            clears = {
                modes.clear_ws: flags.windshield_tearoff,
                modes.clear_fr: flags.fast_repair,
                modes.clear_fuel: flags.fuel_fill,
            }
            if mode in clears:
                values["PitSvFlags"] &= ~clears[mode]
                return True
            if mode == modes.tc:
                values["PitSvTireCompound"] = value
                # Selecting a compound can select all four tyre services too.
                values["PitSvFlags"] |= (
                    flags.lf_tire_change | flags.rf_tire_change
                    | flags.lr_tire_change | flags.rr_tire_change)
                return True
            mapping = {
                modes.lf: (flags.lf_tire_change, "PitSvLFP"),
                modes.rf: (flags.rf_tire_change, "PitSvRFP"),
                modes.lr: (flags.lr_tire_change, "PitSvLRP"),
                modes.rr: (flags.rr_tire_change, "PitSvRRP"),
                modes.fuel: (flags.fuel_fill, "PitSvFuel"),
                modes.ws: (flags.windshield_tearoff, None),
                modes.fr: (flags.fast_repair, None),
            }
            bit, channel = mapping[mode]
            values["PitSvFlags"] |= bit
            if channel and value:
                values[channel] = float(value)
            return True

    reader._ir = IR()
    monkeypatch.setattr(reader, "_get", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(irsdk_reader.time, "sleep", lambda _seconds: None)

    ready, reason = reader.pit_commands_ready()
    assert ready, reason
    assert reader.pit_tire_compounds() == (
        PitTireCompound("Dry", 0), PitTireCompound("Wet", 1))
    applied, reason = reader.apply_pit_commands([PitCommand("fuel", 0)])
    assert applied, reason
    assert values["PitSvFlags"] == flags.fuel_fill
    assert values["PitSvFuel"] == 22.0

    values["PitSvFlags"] = 0
    # Index zero is a real compound, not an omitted parameter.
    applied, reason = reader.apply_pit_commands([PitCommand("tc", 0)])
    assert applied, reason
    assert values["PitSvTireCompound"] == 0

    values["PitSvFlags"] = 0
    applied, reason = reader.apply_pit_commands([
        PitCommand("lf", 145), PitCommand("fuel", 30),
        PitCommand("ws"), PitCommand("fr")])
    assert applied, reason
    assert values["PitSvFlags"] == (
        flags.lf_tire_change | flags.fuel_fill
        | flags.windshield_tearoff | flags.fast_repair)
    assert values["PitSvLFP"] == 145.0
    assert values["PitSvFuel"] == 30.0

    applied, reason = reader.apply_pit_commands([
        PitCommand("clear_tires"), PitCommand("clear_fuel"),
        PitCommand("clear_ws"), PitCommand("clear_fr")])
    assert applied, reason
    assert values["PitSvFlags"] == 0

    # A queued Windows broadcast is not success if iRacing never reflects it.
    reader._ir.pit_command = lambda _mode, _value: True
    applied, reason = reader.apply_pit_commands([PitCommand("fuel", 25)])
    assert applied is False
    assert "did not report" in reason


def test_reader_refuses_to_broadcast_without_readback_state(monkeypatch):
    from ai_race_engineer.telemetry import irsdk_reader

    reader = irsdk_reader.iRacingTelemetryReader()
    reader._started = True

    class IR:
        is_initialized = True
        is_connected = True

        def pit_command(self, *_args):
            raise AssertionError("unverifiable write was broadcast")

    reader._ir = IR()
    monkeypatch.setattr(
        reader, "_get",
        lambda key, default=None: True if key == "IsOnTrack" else default)
    ready, _ = reader.pit_commands_ready()
    assert ready is False
    applied, _ = reader.apply_pit_commands([PitCommand("fuel", 30)])
    assert applied is False


def test_a_repeat_request_re_reads_the_confirmation_and_keeps_it_armed(tmp_path):
    """"Say again" is the one turn that carries no new intent.

    What it re-speaks *is* the read-back, so "confirm" is still the word that
    follows it. Clearing the pending write here would answer the read-back
    with "no pit command is waiting", which is the worse failure.
    """
    orch = _orchestrator(tmp_path)
    proposal = asyncio.run(orch.ask("pit stop fast repair", speak=False))
    armed_at = orch._pending_pit_command_at
    # These tests never reach the speaker — no TTS key, no audio device — so
    # stand in for what a spoken read-back would have left behind.
    orch._last_spoken_text = proposal["answer"]

    repeat = asyncio.run(orch.ask("Say again?", speak=False))

    assert repeat["answer"] == proposal["answer"]
    assert orch._pending_pit_command is not None
    # The window it has left must not grow: a repeat is not a new read-back.
    assert orch._pending_pit_command_at == armed_at

    committed = asyncio.run(orch.ask("confirm", speak=False))
    assert committed["answer"] == "Pit stop set."
    assert len(orch.telemetry.applied) == 1


def test_a_repeat_cannot_outlive_the_confirmation_window(tmp_path):
    """Which is what bounds the risk of the repeat itself being misheard."""
    orch = _orchestrator(tmp_path)
    asyncio.run(orch.ask("pit stop fast repair", speak=False))
    orch._pending_pit_command_at = time.monotonic() - 31

    asyncio.run(orch.ask("say again", speak=False))
    expired = asyncio.run(orch.ask("confirm", speak=False))

    assert expired["answer"] == "No pit command is waiting for confirmation."
    assert orch.telemetry.applied == []


def test_a_fuel_amount_that_rounds_to_zero_is_refused():
    """`PitCommand("fuel", 0)` does not mean "no fuel" to iRacing.

    It means "refuel to the amount already set", so 0.4 litres rounding to 0
    would read back as "0 litres" while the sim did something else — the
    exact mismatch every other check in here exists to prevent.
    """
    for request in ("pit stop fuel 0.4 litres", "pit stop fuel 0.1 gallons"):
        with pytest.raises(PitCommandError, match="at least one litre"):
            parse_pit_command(request)

    # The deliberate "keep the existing amount" form still works, and still
    # says so out loud.
    assert parse_pit_command("pit stop fuel").commands == (PitCommand("fuel", 0),)
    # And one litre is a real request.
    assert parse_pit_command("pit stop fuel 1 litre").commands == \
        (PitCommand("fuel", 1),)
