"""The driver telling the engineer what the car burns a lap.

"Plan on 3.2 litres a lap", "I'm doing two and a half liters per lap". A
strict pattern, not the model, for the same reason pit commands are: it is a
number the engineer will plan every fuel call on, and reading it back is the
only check the driver gets.

Anchored on the end of the sentence — a number, a unit, "a lap" — because
that is the part speech recognition gets right. The start is where it slips:
"I'm on two and a half litres per lap" arrived as "Asiam Ayborn two and a
half liters per lap", and was ignored.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# The pit grammar's number reading, shared so "two point five" means the same
# thing to both.
from .pit_commands import (_NUMBER_TOKEN, PitCommandError, _normalise,
                           _number)

_UNITS = {
    "litre": "litres", "litres": "litres", "liter": "litres", "liters": "litres",
    "l": "litres",
    "kilo": "kilograms", "kilos": "kilograms", "kilogram": "kilograms",
    "kilograms": "kilograms", "kg": "kilograms", "kgs": "kilograms",
    "gallon": "gallons", "gallons": "gallons", "gal": "gallons",
}
_FRACTIONS = {"half": 0.5, "quarter": 0.25, "three quarters": 0.75}

_FIGURE = (
    rf"(?P<number>{_NUMBER_TOKEN}(?:[ -]+{_NUMBER_TOKEN})*)"
    r"(?:\s+and\s+(?:a\s+)?(?P<fraction>three quarters|half|quarter))?"
    r"(?:\s+(?P<unit>" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r"))?")
# "… 3.2 litres a lap", and "fuel per lap is 3.2", the figure last.
_STATEMENTS = (
    re.compile(rf"\b{_FIGURE}(?:\s+of\s+fuel)?\s+(?:per|a|an|each|every)\s+lap\b"),
    re.compile(r"\b(?:fuel\s+)?(?:per\s+lap|consumption|usage|burn)\s+"
               rf"(?:is|of|at)\s+(?:about\s+)?{_FIGURE}\s*$"),
)

# A question about the burn is not a statement of it, and a comparison or a
# saving target is not the figure to plan on.
_QUESTION = re.compile(
    r"^(?:what|how|why|when|which|is|are|do|does|did|am|can|could|should|"
    r"will|would|if)\b")
_NOT_A_FIGURE = re.compile(
    r"\b(?:more than|less than|over|under|above|below|save|saving|"
    r"extra|faster|slower)\s+(?:about\s+)?$")
_FUEL_WORDS = re.compile(r"\b(?:fuel|burn|burning|using|use|consumption|"
                         r"usage|plan|planning|doing)\b")


def parse_stated_burn(text: str) -> Optional[Tuple[float, Optional[str]]]:
    """(figure, unit) when the driver states their burn, else None.

    The unit is "litres", "kilograms" or "gallons", or None when none was
    said — then it is whatever the driver's fuel is reported in. A bare
    number counts only beside a fuel word: "plan on 3.2 a lap" is a burn,
    "two a lap" on its own could be anything.
    """
    if "?" in str(text or ""):
        return None
    normal = _normalise(text)
    if not normal or _QUESTION.match(normal):
        return None
    match = next((m for m in (pattern.search(normal) for pattern in _STATEMENTS)
                  if m is not None), None)
    if match is None or _NOT_A_FIGURE.search(normal[:match.start()]):
        return None
    unit = _UNITS.get(match.group("unit") or "")
    if unit is None and not _FUEL_WORDS.search(normal):
        return None
    try:
        value = _number(match.group("number"))
    except PitCommandError:
        return None
    value += _FRACTIONS.get(match.group("fraction") or "", 0.0)
    return (value, unit) if value > 0 else None
