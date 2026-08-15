"""Vocabulary hints for speech recognition.

Scribe accepts a `keyterms` list that biases transcription toward words you
expect to hear. The driver says surnames and car numbers that no general
model has a prior for — "where's Kowalski?" is a coin flip cold, and a
near-miss there is worse than useless because the engineer then can't find
the car in the running order.

Everything here comes from the live session, so the vocabulary follows
whoever is actually on track.

Cost note: ElevenLabs bills a 20% surcharge and a 20-second minimum billable
duration once a request carries more than 100 keyterms. Radio clips run two
to five seconds, so crossing that line would multiply the bill several times
over for a marginal accuracy gain. MAX_KEYTERMS stays under it deliberately.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .telemetry import TelemetrySnapshot

# Below ElevenLabs' 100-term surcharge threshold, with room to spare.
MAX_KEYTERMS = 80

# The API rejects these outright.
_ILLEGAL = re.compile(r"[<>{}\[\]\\]")
_MAX_CHARS = 50
_MAX_WORDS = 5

# Words a general-purpose model has no reason to expect from someone shouting
# over a wheelbase, but which change the meaning of the question entirely.
RACING_VOCABULARY = [
    "undercut", "overcut", "box this lap", "splash and dash", "pit window",
    "stint", "delta", "apex", "marbles", "dirty air", "tow", "slipstream",
    "understeer", "oversteer", "lock up", "flat spot", "graining",
    "blistering", "degradation", "deg", "fuel save", "lift and coast",
    "double stint", "out lap", "in lap", "safety car", "full course yellow",
    "black flag", "blue flag", "drive through", "stop and go",
    "fast repair", "tear off", "wing angle", "brake bias", "diff",
    "traction control", "anti roll bar", "camber", "toe", "ride height",
]


def _clean(term: str) -> Optional[str]:
    """Normalise one term, or drop it if the API would reject it."""
    term = _ILLEGAL.sub("", str(term or "")).strip()
    if not term:
        return None
    if len(term) > _MAX_CHARS or len(term.split()) > _MAX_WORDS:
        return None
    return term


# Name particles belong to the surname: "van der Beek" is asked for as
# "van der Beek" or "Beek", never as "Beek" alone in a Dutch field.
_PARTICLES = {"van", "von", "der", "den", "de", "di", "da", "del", "della",
              "la", "le", "el", "al", "bin", "mac", "mc", "st"}


def _surname(name: str) -> Optional[str]:
    """The part of a driver's name someone actually says on the radio.

    iRacing publishes a full name as UserName and a "Surname, F." form as
    AbbrevName; nobody asks for either in full. Biasing toward the surname
    alone is the useful hint, and it leaves budget for more drivers.
    """
    cleaned = str(name or "").replace(".", " ").replace(",", " ")
    parts = [p for p in cleaned.split() if len(p) > 1]
    if not parts:
        return None

    # Walk back over any particles so the whole surname is kept.
    end = len(parts)
    start = end - 1
    while start > 0 and parts[start - 1].lower() in _PARTICLES:
        start -= 1
    surname = " ".join(parts[start:end])
    return surname if len(surname) > 2 else None


def build(snapshot: Optional["TelemetrySnapshot"],
          limit: int = MAX_KEYTERMS) -> List[str]:
    """Recognition hints for the cars on track, closest rivals first.

    Ordered by who the driver is most likely to ask about, because the list
    is truncated to stay inside the cost threshold: the cars either side,
    then the leaders, then everyone else.
    """
    terms: List[str] = []
    seen = set()

    def add(value: Optional[str]) -> None:
        cleaned = _clean(value) if value else None
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            terms.append(cleaned)

    if snapshot is not None:
        car, track = snapshot.car, snapshot.track

        # The two cars you are actually racing matter most.
        for rival in (car.ahead, car.behind):
            if rival is not None and rival.known:
                add(_surname(rival.name))
                if rival.car_number:
                    add(f"car {rival.car_number}")

        # Then the order, leader first — "how far ahead is the leader" and
        # "where's P3" are the next most likely questions.
        for rival in track.field:
            if rival.is_player:
                continue
            add(_surname(rival.name))
            if rival.car_number:
                add(f"car {rival.car_number}")

        add(track.track_name)

    # Racing vocabulary fills whatever budget the field left over.
    for word in RACING_VOCABULARY:
        if len(terms) >= limit:
            break
        add(word)

    return terms[:limit]
