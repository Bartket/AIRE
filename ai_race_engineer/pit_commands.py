"""Strict, deterministic parsing for commands that change iRacing pit service.

These commands deliberately do not go through the LLM.  A write intent must
start with ``pit stop`` and match the small grammar below in full; partially
understanding a sentence and silently applying the understood half would be
worse than refusing it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class PitCommand:
    """One pyirsdk PitCommandMode name and its integer parameter."""

    mode: str
    value: int = 0


@dataclass(frozen=True)
class PitTireCompound:
    """One compound name and its zero-based iRacing tire index."""

    name: str
    index: int


@dataclass(frozen=True)
class PitCommandPlan:
    """The exact writes held while the driver confirms their read-back."""

    commands: Tuple[PitCommand, ...]
    summary: str


class PitCommandError(ValueError):
    """A proposed pit request was not exact enough to apply."""


_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUMBER_WORDS = set(_ONES) | set(_TENS) | {"hundred", "point"}
_NUMBER_TOKEN = (
    "(?:" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
    + r"|\d+(?:\.\d+)?)"
)
_MEASUREMENT = re.compile(
    rf"\b({_NUMBER_TOKEN}(?:[ -]+{_NUMBER_TOKEN})*)\s+(?:us\s+)?"
    r"(litres?|liters?|l|gallons?|gal|kpa|psi)\b")
_LITRES_PER_US_GALLON = 3.785411784
_KPA_PER_PSI = 6.894757293


def _number(text: str) -> float:
    text = text.strip().replace("-", " ")
    try:
        value = float(text)
    except ValueError:
        words = text.split()
        if not words or any(word not in _NUMBER_WORDS for word in words):
            raise PitCommandError(f"I couldn't read the number in '{text}'.")
        if words.count("point") > 1:
            raise PitCommandError(f"I couldn't read the number in '{text}'.")
        whole_words, dot, decimal_words = " ".join(words).partition(" point ")
        whole = 0
        group = 0
        for word in whole_words.split():
            if word in _ONES:
                group += _ONES[word]
            elif word in _TENS:
                group += _TENS[word]
            elif word == "hundred":
                group = max(1, group) * 100
        whole += group
        if dot:
            if not decimal_words or any(word not in _ONES for word in decimal_words.split()):
                raise PitCommandError(f"I couldn't read the number in '{text}'.")
            digits = "".join(str(_ONES[word]) for word in decimal_words.split())
            value = whole + int(digits) / (10 ** len(digits))
        else:
            value = float(whole)
    if not math.isfinite(value):
        raise PitCommandError("That number is not valid for a pit command.")
    return value


def _rounded(value: float) -> int:
    """Round a positive SDK parameter normally, not with bankers' rounding."""
    return int(math.floor(value + 0.5))


def _amount(text: str, kind: str) -> int:
    match = re.fullmatch(
        r"(.+?)\s+(litres?|liters?|l|gallons?|gal|kpa|psi)", text.strip())
    if not match:
        unit = "litres or US gallons" if kind == "fuel" else "kPa or PSI"
        raise PitCommandError(f"Say the amount in {unit}.")
    value = _number(match.group(1))
    unit = match.group(2)
    if value <= 0:
        raise PitCommandError("Pit command amounts must be greater than zero.")

    if kind == "fuel":
        if unit in ("gallon", "gallons", "gal"):
            value *= _LITRES_PER_US_GALLON
        elif unit not in ("litre", "litres", "liter", "liters", "l"):
            raise PitCommandError("Say fuel in litres or US gallons.")
        result = _rounded(value)
        # Rounding is what makes this dangerous: 0.4 litres becomes 0, and
        # PitCommand("fuel", 0) does not mean "no fuel" to iRacing — it means
        # "refuel to the amount already set". The read-back would say "0
        # litres" while the sim did something else entirely, which is exactly
        # the mismatch every other check here exists to prevent.
        if result < 1:
            raise PitCommandError("Say a fuel amount of at least one litre.")
        if result > 1000:
            raise PitCommandError("That fuel amount is outside the supported range.")
        return result

    if unit == "psi":
        value *= _KPA_PER_PSI
    elif unit != "kpa":
        raise PitCommandError("Say tyre pressure in kPa or PSI.")
    result = _rounded(value)
    if result > 1000:
        raise PitCommandError("That tyre pressure is outside the supported range.")
    return result


def _join(parts: List[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _normalise(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"\btires?\b", lambda m: "tyre" if m.group(0) == "tire" else "tyres", text)
    text = re.sub(r"\bwindshield\b", "windscreen", text)
    text = re.sub(r"\btear[ -]?off\b", "tearoff", text)
    text = re.sub(r"[^a-z0-9.,\s-]", " ", text)
    return " ".join(text.split())


def _compound_key(text: str) -> str:
    return " ".join(_normalise(text).replace("-", " ").split())


def _compound(
    requested: str, available: Sequence[PitTireCompound]
) -> PitTireCompound:
    if not available:
        raise PitCommandError(
            "I haven't got the tyre types available for this car.")
    matches = [compound for compound in available
               if _compound_key(compound.name) == _compound_key(requested)]
    if len(matches) == 1:
        return matches[0]
    names = _join([compound.name.lower() for compound in available])
    raise PitCommandError(
        f"That tyre type is not available here; available types are {names}.")


def is_pit_command(text: str) -> bool:
    """Whether text uses the deliberate write-command prefix."""
    return bool(re.match(r"^pit\s*stop\b", _normalise(text)))


def might_be_pit_instruction(text: str) -> bool:
    """Whether a natural utterance is worth classifying as a pit write.

    This is only a cost gate before the LLM classifier, never the decision to
    write. It stays broad around action words and radio shorthand while
    avoiding a second model call for ordinary fuel and tyre questions.
    """
    normal = _normalise(text)
    if is_pit_command(normal):
        return True
    if re.match(
        r"^(?:should|what|which|how|when|why|do i|do we|am i|are we|"
        r"have i|has|is|are|could(?! you\b)|would(?! you\b)|will(?! you\b))\b",
        normal,
    ):
        return False
    service = re.search(
        r"\b(?:fuel|refuel|refuell?ing|litres?|liters?|gallons?|gal|tyres?|"
        r"boots|wet|wets|rain|"
        r"rains|dry|drys|slicks|soft|softs|hard|hards|qualifying|"
        r"all[ -]purpose|left front|front left|right front|front right|"
        r"left rear|rear left|right rear|rear right|all four|tearoff|"
        r"windscreen|repair|everything)\b",
        normal,
    )
    if not service:
        return False
    action = re.search(
        r"\b(?:box|pit|stop|add|set|enable|re[ -]?enable|turn|refuel|change|"
        r"take|fit|put|give|clear|request|remove|leave|load|want|new|round|"
        r"please)\b",
        normal,
    )
    if action:
        return True
    # Normal pit-radio shorthand often omits the verb entirely.
    amount = re.search(
        r"\b(?:\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|"
        r"nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
        r"eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|"
        r"ninety|hundred)\b",
        normal,
    )
    compound_and_tyre = re.search(
        r"\b(?:wet|wets|dry|drys|soft|softs|hard|hards|qualifying|"
        r"all[ -]purpose)\b.*\btyres?\b",
        normal,
    )
    tyre_and_fuel = re.search(r"\btyres?\b", normal) and re.search(
        r"\b(?:fuel|litres?|liters?|gallons?|gal)\b", normal)
    one_word_compound = normal in {
        "wets", "rains", "drys", "slicks", "softs", "hards"}
    return bool(amount or compound_and_tyre or tyre_and_fuel or one_word_compound)


def pit_amounts_match(source: str, canonical: str) -> bool:
    """Ensure the interpreter preserved every explicit amount and unit.

    The model is allowed to understand phrasing, not to do arithmetic. This
    catches invented fuel amounts and apparently helpful gallon/litre or
    PSI/kPa conversions before the canonical command can become a proposal.
    """
    units = {
        "litre": "litres", "litres": "litres",
        "liter": "litres", "liters": "litres", "l": "litres",
        "gallon": "gallons", "gallons": "gallons", "gal": "gallons",
        "kpa": "kpa", "psi": "psi",
    }

    def measurements(text: str) -> List[Tuple[float, str]]:
        found = []
        for match in _MEASUREMENT.finditer(_normalise(text)):
            try:
                found.append((_number(match.group(1)), units[match.group(2)]))
            except PitCommandError:
                return []
        return found

    return sorted(measurements(source)) == sorted(measurements(canonical))


def parse_pit_command(
    text: str, tire_compounds: Sequence[PitTireCompound] = ()
) -> Optional[PitCommandPlan]:
    """Parse one complete ``pit stop ...`` request, or return None.

    Components may be separated by ``and``, ``plus`` or commas.  The exact
    quantities sent to the SDK are also the quantities in ``summary``.
    """
    normal = _normalise(text)
    match = re.match(r"^pit\s*stop\s+(.+)$", normal)
    if not match:
        return None
    body = match.group(1)
    if re.search(r"\b(?:no|not|dont|without|except)\b|\bdon\s+t\b", body):
        raise PitCommandError("I won't apply a pit command containing a negation.")

    pieces = [piece.strip() for piece in re.split(r"\s*(?:,|\band\b|\bplus\b)\s*", body)
              if piece.strip()]
    if not pieces:
        raise PitCommandError("Say exactly what to change after 'pit stop'.")

    commands: List[PitCommand] = []
    summaries: List[str] = []
    clear_modes = {
        "clear all": ("clear", "clear all service"),
        "clear everything": ("clear", "clear all service"),
        "clear tyres": ("clear_tires", "clear tyres"),
        "clear all tyres": ("clear_tires", "clear tyres"),
        "clear fuel": ("clear_fuel", "clear fuel"),
        "clear tearoff": ("clear_ws", "clear windscreen tear-off"),
        "clear windscreen": ("clear_ws", "clear windscreen tear-off"),
        "clear fast repair": ("clear_fr", "clear fast repair"),
    }
    corner_modes = {
        "left front": ("lf", "left front tyre"),
        "right front": ("rf", "right front tyre"),
        "left rear": ("lr", "left rear tyre"),
        "right rear": ("rr", "right rear tyre"),
    }

    for piece in pieces:
        if piece in clear_modes:
            mode, label = clear_modes[piece]
            commands.append(PitCommand(mode))
            summaries.append(label)
            continue

        if re.fullmatch(r"(?:(?:take|add|set|change)\s+)?(?:a\s+)?(?:tearoff|windscreen)", piece):
            commands.append(PitCommand("ws"))
            summaries.append("windscreen tear-off")
            continue
        if re.fullmatch(r"(?:(?:take|add|set|request)\s+)?(?:a\s+)?fast repair", piece):
            commands.append(PitCommand("fr"))
            summaries.append("fast repair")
            continue

        tyre = re.fullmatch(
            r"(?:(?:change|take|fit|set)\s+)?(?:all(?:\s+four)?|four)\s+tyres"
            r"(?:\s+at\s+(.+))?", piece)
        if tyre:
            pressure = _amount(tyre.group(1), "pressure") if tyre.group(1) else 0
            commands.extend(PitCommand(mode, pressure) for mode in ("lf", "rf", "lr", "rr"))
            label = "four tyres"
            if pressure:
                label += f" at {pressure} kPa"
            summaries.append(label)
            continue

        corner = re.fullmatch(
            r"(?:(?:change|take|fit|set)\s+)?"
            r"(left front|right front|left rear|right rear)\s+tyre"
            r"(?:\s+at\s+(.+))?", piece)
        if corner:
            mode, label = corner_modes[corner.group(1)]
            pressure = _amount(corner.group(2), "pressure") if corner.group(2) else 0
            commands.append(PitCommand(mode, pressure))
            if pressure:
                label += f" at {pressure} kPa"
            summaries.append(label)
            continue

        compound = re.fullmatch(
            r"(?:(?:change|take|fit|set)\s+(?:to\s+)?)?(.+?)\s+tyres", piece)
        if not compound:
            compound = re.fullmatch(
                r"(?:(?:change|set)\s+)?tyre\s+(?:compound|type)"
                r"(?:\s+to)?\s+(.+)", piece)
        if compound:
            selected = _compound(compound.group(1), tire_compounds)
            commands.append(PitCommand("tc", selected.index))
            summaries.append(f"{selected.name.lower()} tyres")
            continue

        fuel_existing = re.fullmatch(
            r"(?:(?:add|set|enable|re[ -]?enable|turn)\s+)?"
            r"(?:fuel|refuel|refuell?ing)(?:\s+(?:back\s+)?on)?",
            piece,
        )
        if fuel_existing:
            commands.append(PitCommand("fuel"))
            summaries.append("fuel at the current amount")
            continue

        fuel = re.fullmatch(r"(?:(?:add|set)\s+)?fuel(?:\s+to)?\s+(.+)", piece)
        if fuel:
            litres = _amount(fuel.group(1), "fuel")
            commands.append(PitCommand("fuel", litres))
            summaries.append(f"{litres} litre{'s' if litres != 1 else ''} of fuel")
            continue
        # "Four tyres and thirty litres" is the natural short radio form.
        if re.fullmatch(r".+\s+(?:litres?|liters?|l|gallons?|gal)", piece):
            litres = _amount(piece, "fuel")
            commands.append(PitCommand("fuel", litres))
            summaries.append(f"{litres} litre{'s' if litres != 1 else ''} of fuel")
            continue

        raise PitCommandError(
            f"I won't change anything because I couldn't read '{piece}'.")

    if len(commands) > 12:
        raise PitCommandError("That pit command contains too many changes.")
    return PitCommandPlan(tuple(commands), _join(summaries))
