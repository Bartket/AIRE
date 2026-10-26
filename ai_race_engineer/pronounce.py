"""Respell names for the voice, without changing what the engineer knows.

ElevenLabs is not deterministic: the same text can come back clean once and
slurred the next time. That is why the artefacts are intermittent rather
than a fixed mispronunciation — the model is uncertain about the token, and
uncertainty shows up as instability. Feeding it a spelling it is sure of
removes the uncertainty rather than papering over it.

Applied at the speech boundary only. The model still reasons about "Circuit
de Spa-Francorchamps", the exchange log still records it, and only the audio
request sees "Spa". Nothing upstream can tell the difference.

Two layers (folding runs first, so the table matches accented text too):

1. **A table**, for the cases worth getting exactly right. Track names are a
   closed set and they are what the engineer says most, so they are worth
   spelling out by hand.
2. **Diacritic folding**, for everything else. Hungarian, Polish, Icelandic
   and Nordic names are the ones that break, and their accented characters
   are what the voice is least sure of. Folding to ASCII does not make the
   pronunciation *correct* — "Kovacs" is not how Kovács sounds — but it is
   stable, and a name said the same plain way every time beats one that
   slurs at random.

Phoneme tags would be more precise, but ElevenLabs only supports them on
some models and not on turbo v2.5, which is what this ships with. Alias
substitution works on every model, which is why this is spelling rather
than IPA.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Dict, Optional

logger = logging.getLogger("race-engineer")

# Tracks the voice reliably mangles, and what an engineer would say anyway.
# Real race engineers do not say "Circuit de Spa-Francorchamps" on the
# radio — they say "Spa". Shortening is both more natural and more stable.
#
# **No articles in the values.** "the Nurburgring" here produces "the the
# Nurburgring" the moment the sentence already had one. Names whose only
# problem is an umlaut need no entry at all — folding handles those.
#
# **Keys are iRacing's own strings, folded.** Text is folded before it is
# matched, so "Autódromo José Carlos Pace" needs an ASCII key. The original
# table was written from memory and a third of it never fired. The first
# group is TrackDisplayName copied from real session dumps; the second is
# the Data API track name, which matched TrackDisplayName for most tracks
# sampled but not all (Barcelona gains a hyphen, Watkins Glen loses
# "International") — confirm against a live session before trusting one.
_TRACKS: Dict[str, str] = {
    # Seen in session YAML.
    "circuit de spa-francorchamps": "Spa",
    "circuit de barcelona-catalunya": "Barcelona",
    "circuit des 24 heures du mans": "Le Mans",
    "mount panorama circuit": "Bathurst",
    "charlotte motor speedway": "Charlotte",
    # The combined Nürburgring reports its layout as the display name.
    "gesamtstrecke vln": "Nordschleife",
    "gesamtstrecke short w/out arena": "Nordschleife",
    # Data API names only.
    "autodromo nazionale monza": "Monza",
    "autodromo jose carlos pace": "Interlagos",
    "autodromo internazionale enzo e dino ferrari": "Imola",
    "autodromo internazionale del mugello": "Mugello",
    "autodromo hermanos rodriguez": "Mexico City",
    "algarve international circuit": "Portimao",
    "circuit zandvoort": "Zandvoort",
    "circuit park zandvoort": "Zandvoort",
    "hockenheimring baden-wurttemberg": "Hockenheim",
    "nurburgring grand-prix-strecke": "Nurburgring GP",
    "circuit de nevers magny-cours": "Magny-Cours",
    "circuito de jerez - angel nieto": "Jerez",
    "misano world circuit marco simoncelli": "Misano",
    "motorsport arena oschersleben": "Oschersleben",
    "suzuka international racing course": "Suzuka",
    "mobility resort motegi": "Motegi",
    "canadian tire motorsports park": "Mosport",
    "donington park racing circuit": "Donington",
    "sandown international motor raceway": "Sandown",
    "weathertech raceway at laguna seca": "Laguna Seca",
    "michigan international speedway": "Michigan",
    "world wide technology raceway (gateway)": "Gateway",
    "echopark speedway (atlanta)": "Atlanta",
    # How the model shortens the long forms on its own.
    "spa-francorchamps": "Spa",
    "barcelona-catalunya": "Barcelona",
}

_SMALL_NUMBERS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety")


def _integer_words(value: int) -> str:
    """English cardinal words for the range race telemetry can plausibly use."""
    if value < 20:
        return _SMALL_NUMBERS[value]
    if value < 100:
        tens, remainder = divmod(value, 10)
        return _TENS[tens] + (f" {_SMALL_NUMBERS[remainder]}" if remainder else "")
    if value < 1000:
        hundreds, remainder = divmod(value, 100)
        return (f"{_SMALL_NUMBERS[hundreds]} hundred"
                + (f" {_integer_words(remainder)}" if remainder else ""))
    for size, name in ((1_000_000_000, "billion"),
                       (1_000_000, "million"), (1_000, "thousand")):
        if value >= size:
            count, remainder = divmod(value, size)
            return (f"{_integer_words(count)} {name}"
                    + (f" {_integer_words(remainder)}" if remainder else ""))
    return " ".join(_SMALL_NUMBERS[int(ch)] for ch in str(value))


def _digits_words(digits: str) -> str:
    return " ".join(_SMALL_NUMBERS[int(ch)] for ch in digits)


def _number_words(sign: str, whole: str, fraction: Optional[str] = None) -> str:
    digits = whole.replace(",", "")
    if len(digits) > 1 and digits.startswith("0"):
        words = _digits_words(digits)
    else:
        try:
            value = int(digits)
            words = (_integer_words(value) if value <= 999_999_999_999
                     else _digits_words(digits))
        except ValueError:
            return sign + whole + (f".{fraction}" if fraction else "")
    if fraction is not None:
        words += " point " + _digits_words(fraction)
    if sign == "-":
        words = "minus " + words
    elif sign == "+":
        words = "plus " + words
    return words


_POSITION = re.compile(r"(?<!\w)[Pp]\s*(\d{1,3})(?!\w)")
_CAR_NUMBER = re.compile(r"(?:(?<!\w)car\s*)?#\s*(\d+)(?!\w)", re.IGNORECASE)
# A unit after a lap time is absorbed: the words already end in "seconds".
_LAP_TIME = re.compile(r"(?<!\w)(\d+):([0-5]\d)(?:\.(\d+))?"
                       r"(?:\s*(?:s|secs?|seconds))?(?!\w)", re.IGNORECASE)
_NUMBER = re.compile(r"(?<!\w)([+-]?)(\d[\d,]*)(?:\.(\d+))?(?!\w)")


def for_speech(text: str) -> str:
    """Turn display notation into unambiguous words before synthesis.

    Some answers come straight from the calculators and never pass through
    the model's "write for the ear" instruction.  Keeping this at the same
    boundary as name pronunciation makes every route obey the same contract,
    while the exchange log retains the compact original text.
    """
    if not text:
        return text

    out = str(text)

    def _lap_time(m):
        minutes = int(m.group(1))
        seconds = int(m.group(2))
        second_words = _integer_words(seconds)
        if m.group(3):
            second_words += " point " + _digits_words(m.group(3))
        return (f"{_integer_words(minutes)} minute"
                f"{'s' if minutes != 1 else ''} {second_words} seconds")

    # Before the units: "1:23.4s" would otherwise gain a second "seconds".
    out = _LAP_TIME.sub(_lap_time, out)
    out = re.sub(r"\bkm\s*/\s*h\b", "kilometres per hour", out, flags=re.IGNORECASE)
    out = re.sub(r"\bmph\b", "miles per hour", out, flags=re.IGNORECASE)
    out = re.sub(r"\bm\s*/\s*s\b", "metres per second", out, flags=re.IGNORECASE)
    out = re.sub(r"(?<=\d)\s*°\s*C\b", " degrees Celsius", out,
                 flags=re.IGNORECASE)
    out = re.sub(r"(?<=\d)\s*°\s*F\b", " degrees Fahrenheit", out,
                 flags=re.IGNORECASE)
    out = re.sub(r"(?<=\d)\s*%", " percent", out)
    # A standalone number only: the 3 in "GT3s" is a class, not a time.
    out = re.sub(r"(?<!\w)(\d+(?:[.,]\d+)*)\s*s\b", r"\1 seconds", out,
                 flags=re.IGNORECASE)
    out = re.sub(r"\bk\s*Pa\b", "kilopascals", out, flags=re.IGNORECASE)
    out = re.sub(r"\bPSI\b", "P S I", out, flags=re.IGNORECASE)
    out = re.sub(r"\bRPM\b", "revolutions per minute", out, flags=re.IGNORECASE)
    for acronym in ("ABS", "DRS", "ERS", "GTP", "TCR"):
        letters = " ".join(acronym)
        out = re.sub(rf"\b{acronym}\b", letters, out, flags=re.IGNORECASE)
    out = re.sub(r"\bGT\s*([1-9])(s?)\b",
                 lambda m: f"G T {_integer_words(int(m.group(1)))}{m.group(2)}",
                 out, flags=re.IGNORECASE)

    out = _POSITION.sub(lambda m: f"position {_integer_words(int(m.group(1)))}", out)

    def _car(m):
        digits = m.group(1)
        number = (_digits_words(digits) if len(digits) > 1 and digits.startswith("0")
                  else _integer_words(int(digits)))
        return f"car {number}"

    out = _CAR_NUMBER.sub(_car, out)
    out = _NUMBER.sub(lambda m: _number_words(m.group(1), m.group(2), m.group(3)), out)

    # Common display abbreviations occasionally survive weaker model replies.
    for short, full in (("FL", "front left"), ("FR", "front right"),
                        ("RL", "rear left"), ("RR", "rear right")):
        out = re.sub(rf"\b{short}\b", full, out)
    return out


def fold(text: str) -> str:
    """Strip accents to their base letters, keeping everything else.

    Deliberately crude. The goal is a spelling the voice is certain of, not
    a faithful transliteration — an accent it half-recognises is exactly
    what produces the drawn-out consonants.

    Public because looking a driver up has the same problem from the other
    end: speech recognition hears "Kovacs" and the roster holds "Kovács",
    and one folding rule for both keeps them agreeing.
    """
    # Characters that do not decompose need naming; NFD leaves them alone.
    special = {"ß": "ss", "æ": "ae", "ø": "o", "å": "a", "þ": "th", "ð": "d",
               "Æ": "Ae", "Ø": "O", "Å": "A", "Þ": "Th", "Ð": "D",
               "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i", "œ": "oe"}
    text = "".join(special.get(ch, ch) for ch in text)
    return "".join(ch for ch in unicodedata.normalize("NFD", text)
                   if not unicodedata.combining(ch))


def build_table(overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The built-in table with the driver's own entries layered on top."""
    table = dict(_TRACKS)
    for key, value in (overrides or {}).items():
        # Folded because the text is folded before matching.
        key = fold(str(key).strip().lower())
        if key:
            table[key] = str(value)
    return table


# What an engineer puts between two names for the same place: punctuation,
# the few words used to gloss one with the other, and at most one word of
# anything else — which is nearly always the driver being addressed ("Spa,
# Bart, Spa"). Bounded deliberately: two mentions with a clause between them
# are two real mentions of the track, and collapsing those would eat the
# sentence around them.
_CONNECTORS = re.compile(r"\b(?:or|aka|that is|in full|also known as)\b",
                         re.IGNORECASE)
_FILL = (r"(?:[\s,;:.()\-–—]|\bor\b|\baka\b|\bthat is\b"
         r"|\bin full\b|\balso known as\b)*")
_BRIDGE = _FILL + r"(?:\w+" + _FILL + r")?"


def _collapse_repeats(text: str, values) -> str:
    """Drop a substitution that has become a repeat of the one beside it.

    Several spellings collapse to the same word on purpose — "Circuit de
    Spa-Francorchamps" and "Spa-Francorchamps" are both "Spa" — so an answer
    that offers both forms, which is a natural thing to say, comes out as
    "you're at Spa, Spa to you and me". The voice was blamed for that; the
    table wrote it.

    Only values this pass actually produced are collapsed. Repeated words
    the model chose itself are its own and are left alone.
    """
    for value in values:
        if not value.strip():
            continue
        v = re.escape(value)
        pattern = re.compile(rf"\b({v})\b({_BRIDGE})\b{v}\b", re.IGNORECASE)

        def _keep_one(m):
            # The first occurrence's own casing, not the table's: it may be
            # starting a sentence, where "car 44" should stay "Car 44".
            head, bridge = m.group(1), m.group(2)
            # A bridge of punctuation is scaffolding between two names and
            # goes with the one being dropped. A bridge with a word in it is
            # the driver being spoken to, and that stays.
            if re.search(r"\w", _CONNECTORS.sub(" ", bridge)):
                return head + bridge.rstrip(" ,;:.()-–—")
            return head

        previous = None
        while previous != text:
            previous = text
            text = pattern.sub(_keep_one, text)
    return text


def apply(text: str, overrides: Optional[Dict[str, str]] = None) -> str:
    """Rewrite `text` into something the voice says the same way every time.

    Longest phrases first, so "circuit de spa-francorchamps" wins over a
    bare "spa" that would otherwise match inside it and leave the rest
    stranded.
    """
    if not text:
        return text
    # Fold first: iRacing sends "Autódromo José Carlos Pace", and a table
    # matched before folding needs every accent spelled exactly right.
    folded = fold(text)
    if folded != text:
        logger.debug("Folded accents for speech: %r -> %r", text, folded)
    text = folded
    table = build_table(overrides)
    if not table:
        return text

    # One alternation, longest first, applied in a single sweep. Running the
    # rules one after another re-scans each replacement with every later
    # rule: "Circuit de Spa-Francorchamps" became "Spa" and then a separate
    # "spa" entry rewrote *that*. A single pass consumes the text as it
    # goes, so nothing a rule produces can be matched again.
    # Lookarounds rather than \b, which never matches after a key ending in
    # punctuation — "World Wide Technology Raceway (Gateway)".
    phrases = sorted(table, key=len, reverse=True)
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(p) for p in phrases)
                         + r")(?!\w)", re.IGNORECASE)
    produced = set()

    def _replace(m):
        value = table[m.group(0).lower()]
        produced.add(value)
        # Keep the sentence reading properly when a substitution lands at
        # the start of one: "car 7 is ahead" should be "Car 7 is ahead".
        before = text[:m.start()].rstrip()
        if not before or before[-1] in ".!?":
            return value[:1].upper() + value[1:]
        return value

    # Values are folded too: a driver's own spelling may carry accents.
    return fold(_collapse_repeats(pattern.sub(_replace, text), produced))

# ── Names the voice is likely to fight ──────────────────────────────
# iRacing has hundreds of thousands of drivers. Nothing curated can cover
# that, so risky names are detected rather than listed, and the fallback is
# the car number — which is what a real engineer says half the time anyway
# ("the 44 car is closing"). That is what makes guessing safe here: a false
# positive costs nothing, because the alternative phrasing is also correct.

# Letter pairs common in Hungarian, Polish, Czech and Nordic surnames and
# rare or absent in English. Not a language detector — a list of the things
# an English-trained voice has least evidence for.
# No "ee": it is one of the commonest pairs in English, and it made a rival
# called Green turn "Green flag" into "Car 12 flag".
_AWKWARD = ("sz", "cz", "rz", "zs", "dz", "gy", "ny", "ty", "cs", "szcz",
            "kj", "hj", "sj", "tj", "vj", "zh", "shch", "ij", "aa",
            "oe", "yj", "wsk", "rzy", "chr", "kkr", "ttr")

_VOWELS = set("aeiouy")


def looks_hard(name: str) -> bool:
    """Whether a name is likely to come out unstable.

    Two signals, both language-agnostic enough to explain:

    * an awkward letter pair, from the list above
    * four or more consonants in a row, which English words essentially
      never contain and which give the voice nothing to anchor on

    Deliberately generous. Saying "the 44 car" when the name would have
    been fine is invisible on a race radio; slurring a name is not.
    """
    folded = fold(str(name or "")).lower()
    letters = re.sub(r"[^a-z ]", "", folded)
    if not letters.strip():
        return False
    for word in letters.split():
        if len(word) >= 12:
            return True
        if any(pair in word for pair in _AWKWARD):
            return True
        run = 0
        for ch in word:
            run = 0 if ch in _VOWELS else run + 1
            if run >= 4:
                return True
    return False


def auto_table(drivers, policy: str = "auto") -> Dict[str, str]:
    """Build respellings from the roster, so nothing needs curating.

    `drivers` is anything with `name` and `car_number` — the rivals on the
    current snapshot. Names judged risky are spoken as their car number.

    Policies: "auto" substitutes only the risky ones, "numbers" always
    substitutes, "names" never does.
    """
    policy = str(policy or "auto").lower()
    if policy == "names":
        return {}
    table: Dict[str, str] = {}
    # A surname two cars share — the driver's own included — would name
    # whichever came last. The full name still distinguishes them.
    surnames: Dict[str, int] = {}
    for driver in drivers or []:
        parts = (getattr(driver, "name", "") or "").split()
        if len(parts) > 1:
            key = fold(parts[-1].lower())
            surnames[key] = surnames.get(key, 0) + 1
    for driver in drivers or []:
        # The driver is in this list too, and they are the one being spoken
        # to — "car 44, you're P4" is not something an engineer says. Their
        # own name has its own lever, the callsign setting, which is a
        # spelling they chose rather than one guessed for them.
        if getattr(driver, "is_player", False):
            continue
        name = (getattr(driver, "name", "") or "").strip()
        number = (getattr(driver, "car_number", "") or "").strip()
        if not name or not number:
            continue
        if policy != "numbers" and not looks_hard(name):
            continue
        spoken = f"car {number}"
        table[name.lower()] = spoken
        # The surname alone is what the engineer usually says, and it is
        # normally the half that breaks.
        parts = name.split()
        if (len(parts) > 1 and looks_hard(parts[-1])
                and surnames[fold(parts[-1].lower())] == 1):
            table[parts[-1].lower()] = spoken
    return table
