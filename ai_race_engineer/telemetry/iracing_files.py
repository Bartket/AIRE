"""Files iRacing keeps on disk that the SDK does not publish.

`Documents\\iRacing\\fueldata.ini` is iRacing's own fuel history: what each
car has used a lap on each track, as recorded by the sim. It looks like

    [porsche9922cup]
    bathurst=10,2.44,2.43,2.47,2.44,2.40,2.44,2.34,2.41,2.43,2.45	;

A section per car — the session YAML's `CarPath`, spaces as backslashes — and
a key per track — `WeekendInfo:TrackName`, the same — holding a count and ten
slots, -1 where empty. The figures are **kilograms per lap**: checked against
the litres measured in 53 recorded sessions at the car's 0.75 kg/L (Porsche
Cup at Bathurst 2.34-2.47 kg against 3.12-3.30 L; Formula iR-04 at Monza
0.93-0.97 kg against 1.25-1.29 L).

The format is not documented. Anything that does not parse is no history,
never a guess.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("race-engineer")


def documents_dir() -> Path:
    """The user's Documents folder, wherever Windows has put it.

    OneDrive and folder redirection move it, so `~/Documents` is only the
    fallback. Asked of the shell with SHGetKnownFolderPath, whose argument
    and return types are declared: undeclared, ctypes truncates 64-bit
    pointers, which is how an earlier priority call silently did nothing.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            # FOLDERID_Documents, {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            folder = _GUID(0xFDD39AD0, 0x238F, 0x46AF,
                           (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85,
                                                0x48, 0x03, 0x69, 0xC7))
            shell = ctypes.windll.shell32.SHGetKnownFolderPath
            shell.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD,
                              wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
            shell.restype = ctypes.c_long
            free = ctypes.windll.ole32.CoTaskMemFree
            free.argtypes = [ctypes.c_void_p]
            free.restype = None
            path = ctypes.c_wchar_p()
            if shell(ctypes.byref(folder), 0, None, ctypes.byref(path)) == 0:
                try:
                    if path.value:
                        return Path(path.value)
                finally:
                    free(ctypes.cast(path, ctypes.c_void_p))
        except Exception as exc:                      # noqa: BLE001
            logger.debug("SHGetKnownFolderPath failed: %s", exc)
    return Path(os.path.expanduser("~")) / "Documents"


def fuel_data_path() -> Path:
    return documents_dir() / "iRacing" / "fueldata.ini"


def _key(name: str) -> str:
    """A YAML name as fueldata.ini spells it: spaces become backslashes."""
    return "\\".join(str(name or "").strip().lower().split())


def fuel_history(path: Path, car_path: str, track_name: str) -> List[float]:
    """iRacing's recorded burn for this car on this track, kg per lap.

    Empty when the file, the car or the track is not there, or a line does
    not parse. Only positive figures are kept; -1 marks an empty slot.
    """
    car, track = _key(car_path), _key(track_name)
    if not car or not track:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    section = None
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != car or "=" not in line:
            continue
        name, _, values = line.partition("=")
        if name.strip().lower() != track:
            continue
        figures: List[float] = []
        for field in values.split(",")[1:]:
            try:
                value = float(field)
            except ValueError:
                return []
            if value > 0:
                figures.append(value)
        return figures
    return []
