"""The driver stating their own fuel burn, on the radio.

"Asiam Ayborn two and a half liters per lap" is a real transcription from a
race: the driver saying what the car was using, the start of the sentence
misheard. It went to the fuel calculator as a question and was ignored.
"""

import asyncio

import pytest

from ai_race_engineer.config import AppConfig
from ai_race_engineer.fuel_statement import parse_stated_burn
from ai_race_engineer.orchestrator import Orchestrator


@pytest.mark.parametrize("said, heard", [
    ("Asiam Ayborn two and a half liters per lap", (2.5, "litres")),
    ("Plan on 3.2 litres a lap", (3.2, "litres")),
    ("I'm using 2.4 kilos a lap", (2.4, "kilograms")),
    ("I am on two point five litres per lap", (2.5, "litres")),
    ("three and three quarters litres a lap", (3.75, "litres")),
    ("plan on 3.2 a lap", (3.2, None)),
    ("fuel per lap is 0.85", (0.85, None)),
    ("my fuel consumption is 3.1 litres", (3.1, "litres")),
])
def test_a_stated_burn_is_heard(said, heard):
    assert parse_stated_burn(said) == heard


@pytest.mark.parametrize("said", [
    "What is my fuel consumption per lap?",          # the question, not a figure
    "how much fuel do I burn per lap",
    "save 0.2 litres a lap",                         # a saving target
    "I'm using more than 3 litres a lap",            # a comparison
    "pit stop add 20 litres",                        # a pit command
    "losing three tenths a lap",                     # not fuel at all
    "two a lap",                                     # a bare number, no fuel word
    "I'm doing 1:47 a lap",
])
def test_anything_else_is_not_a_stated_burn(said):
    assert parse_stated_burn(said) is None


class _Holding:
    """Telemetry that records what it was told and has no sim."""

    name = "test"

    def __init__(self, accept=True):
        self.accept = accept
        self.stated = []

    def set_stated_fuel_burn(self, value, unit=None):
        self.stated.append((value, unit))
        return value if self.accept else None

    def is_connected(self):
        return False

    def get_telemetry_snapshot(self):
        return None

    def shutdown(self):
        pass


def _ask(tmp_path, monkeypatch, question, telemetry):
    orch = Orchestrator(AppConfig({"telemetry": {"source": "simulated"}},
                                  path=tmp_path / "config.json"))
    orch.telemetry = telemetry
    monkeypatch.setattr(
        orch.llm, "generate",
        lambda *args: (_ for _ in ()).throw(AssertionError("reached the model")))

    async def directly(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", directly)
    return asyncio.run(orch.ask(question, speak=False))


def test_the_burn_is_held_and_read_back_without_the_model(tmp_path, monkeypatch):
    telemetry = _Holding()
    entry = _ask(tmp_path, monkeypatch,
                 "Asiam Ayborn two and a half liters per lap", telemetry)
    assert telemetry.stated == [(2.5, "litres")]
    assert entry["answer"] == ("Copy, planning on 2.5 litres a lap until I've "
                               "measured two laps of my own.")


def test_an_implausible_burn_is_asked_again(tmp_path, monkeypatch):
    entry = _ask(tmp_path, monkeypatch, "plan on 250 litres a lap",
                 _Holding(accept=False))
    assert entry["answer"].startswith("Say again")
