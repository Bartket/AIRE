"""What an engineer calls a car on the radio.

Nobody says "the McLaren 720S GT3 EVO is closing" — it is "the McLaren".
iRacing's own short name does not get there either: CarScreenNameShort is
still "BMW M4 G82 GT4" or "NASCAR Toyota Camry". And "the Mustang" or "the
Aston" cannot be derived from the name by rule, so this is a table.

**Keyed by CarID, never by name.** The names drift — the session YAML says
"Mclaren 570s GT4" where the Data API says "McLaren 570S GT4", "Porsche 991
RSR" where it says "Porsche 911 RSR" — while the IDs matched on every car
compared. A name key would fail silently, as half the track table did.
IDs are from the iRacing Data API car list, cross-checked against CarID in
real session dumps.

Only the multi-make classes have entries. In a spec series every car is the
same, and the name is dropped altogether (`single_make`).
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

_NICKNAMES: Dict[int, str] = {
    # GT3
    194: "Acura",           # Acura NSX GT3 EVO 22
    206: "Aston",           # Aston Martin Vantage GT3 EVO
    176: "Audi",            # Audi R8 LMS EVO II GT3
    132: "BMW",             # BMW M4 GT3 EVO
    184: "Corvette",        # Chevrolet Corvette Z06 GT3.R
    173: "Ferrari",         # Ferrari 296 GT3
    144: "Ferrari",         # Ferrari 488 GT3 Evo 2020
    185: "Mustang",         # Ford Mustang GT3
    133: "Lambo",           # Lamborghini Huracán GT3 EVO
    188: "McLaren",         # McLaren 720S GT3 EVO
    156: "Mercedes",        # Mercedes-AMG GT3 2020
    169: "Porsche",         # Porsche 911 GT3 R (992)
    # GT4
    150: "Aston",           # Aston Martin Vantage GT4
    122: "BMW",             # BMW M4 F82 GT4 - 2018
    189: "BMW",             # BMW M4 G82 GT4 Evo
    204: "Mustang",         # Ford Mustang GT4
    135: "McLaren",         # McLaren 570S GT4
    157: "Mercedes",        # Mercedes-AMG GT4
    119: "Porsche",         # Porsche 718 Cayman GT4 Clubsport MR
    # GTP / LMH
    170: "Acura",           # Acura ARX-06 GTP
    212: "Valkyrie",        # Aston Martin Valkyrie AMR-LMH
    159: "BMW",             # BMW M Hybrid V8 (Evo)
    168: "Cadillac",        # Cadillac V-Series.R GTP
    196: "Ferrari",         # Ferrari 499P
    174: "Porsche",         # Porsche 963 GTP
    # GTE
    109: "BMW",             # BMW M8 GTE
    127: "Corvette",        # Chevrolet Corvette C8.R GTE
    93: "Ferrari",          # Ferrari 488 GTE
    92: "Ford GT",          # Ford GTE
    102: "Porsche",         # Porsche 911 RSR
    # TCR
    210: "Audi",            # Audi RS3 LMS Gen2 TCR
    147: "Honda",           # Honda Civic Type R TCR
    146: "Hyundai",         # Hyundai Elantra N TCR
    153: "Hyundai",         # Hyundai Veloster N TCR
    # NASCAR Cup Next Gen, where the make is what everyone says
    139: "Chevy",           # NASCAR Cup Series Next Gen Chevrolet Camaro ZL1
    140: "Ford",            # NASCAR Cup Series Next Gen Ford Mustang
    141: "Toyota",          # NASCAR Cup Series Next Gen Toyota Camry
    # Supercars
    192: "Camaro",          # Supercars Chevrolet Camaro Gen 3
    190: "Mustang",         # Supercars Ford Mustang Gen 3
}


def single_make(field: Iterable) -> bool:
    """Every car in the field is the same model, so naming it says nothing.

    False when there is no field to judge by — an unknown is not a spec
    series, and dropping the name on a guess would hide real information.
    """
    ids = {getattr(r, "car_id", None) for r in field or []}
    return len(ids) == 1 and None not in ids


def spoken_model(car, field: Optional[Iterable] = None) -> str:
    """The name an engineer would use for `car`, or iRacing's own if none.

    A nickname another model in the field also answers to is ambiguous —
    a GT3 and a GT4 Mustang in one multiclass race are both "the Mustang" —
    so it falls back to iRacing's name, which tells them apart.
    """
    fallback = (getattr(car, "car_model_short", "")
                or getattr(car, "car_model", "") or "")
    car_id = getattr(car, "car_id", None)
    nickname = _NICKNAMES.get(car_id)
    if not nickname:
        return fallback
    for other in field or []:
        other_id = getattr(other, "car_id", None)
        if other_id != car_id and _NICKNAMES.get(other_id) == nickname:
            return fallback
    return nickname
