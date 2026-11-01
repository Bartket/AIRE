"""Driver profiles: named sets of the per-person settings, kept on disk.

One file per profile under `<config dir>/profiles/<id>.json`, holding
`{"name": ..., "initials": ..., "color": ..., "settings": {...}}`. What
`settings` covers is decided by `config._PROFILE_KEYS`, not here — this
module only reads and writes files. Name, initials and colour describe the
profile itself, so they sit beside the settings rather than among them.

config.json still holds every live value, the active profile's included,
so a build that predates profiles reads it unchanged. The active profile's
file is rewritten on every save; see `AppConfig.save`.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("race-engineer")

DEFAULT_ID = "default"
DEFAULT_NAME = "Default"

#: Badge colours, by name. The panel turns each into a shade per theme, which
#: a stored hex could not do — a pale colour chosen in dark mode would leave
#: the initials unreadable in light mode. First is the brand orange.
COLORS = ("orange", "red", "amber", "green", "teal", "blue", "violet", "pink", "slate")

# Checked before any path is built from an id. The id arrives in a URL, and
# "../config" would otherwise name config.json itself.
_VALID_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_MAX_NAME = 40


class ProfileError(ValueError):
    """A profile request that cannot be carried out, with a reason to show."""


def valid_id(profile_id: str) -> bool:
    return bool(_VALID_ID.match(profile_id or ""))


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:32].strip("-") or "driver"


def clean_name(name: Any) -> str:
    name = " ".join(str(name or "").split())[:_MAX_NAME]
    if not name:
        raise ProfileError("A profile needs a name.")
    return name


def clean_initials(initials: Any) -> str:
    """Up to two letters or digits, upper-cased. Blank means "from the name"."""
    return "".join(c for c in str(initials or "") if c.isalnum())[:2].upper()


def initials_for(name: str) -> str:
    """Two words give their first letters, one word its first two."""
    words = [w for w in (clean_initials(w) or "" for w in name.split()) if w]
    if not words:
        return "?"
    return (words[0][0] + words[1][0]) if len(words) > 1 else words[0][:2]


def clean_color(color: Any) -> str:
    if color not in COLORS:
        raise ProfileError(f"Not a profile colour: {color!r}.")
    return color


class ProfileStore:
    """The profiles directory. Every method takes and returns plain data."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def _path(self, profile_id: str) -> Path:
        if not valid_id(profile_id):
            raise ProfileError(f"Not a profile id: {profile_id!r}")
        return self.dir / f"{profile_id}.json"

    def exists(self, profile_id: str) -> bool:
        return valid_id(profile_id) and self._path(profile_id).is_file()

    def _load(self, profile_id: str) -> Optional[Dict[str, Any]]:
        """The file as stored, or None when it is missing or unreadable."""
        path = self._path(profile_id)
        if not path.is_file():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("settings"), dict):
                raise ValueError("profile must hold a 'settings' object")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Could not read profile %s (%s); skipping it", path, exc)
            return None
        return data

    def read(self, profile_id: str) -> Optional[Dict[str, Any]]:
        """The profile with its badge filled in, or None."""
        data = self._load(profile_id)
        if data is None:
            return None
        name = str(data.get("name") or "").strip() or profile_id
        color = data.get("color")
        return {
            "id": profile_id,
            "name": name,
            "initials": clean_initials(data.get("initials")) or initials_for(name),
            # A hand-edited file with a colour this build does not know
            # falls back rather than failing to list.
            "color": color if color in COLORS else COLORS[0],
            "settings": data["settings"],
        }

    def list(self) -> List[Dict[str, str]]:
        if not self.dir.is_dir():
            return []
        found = []
        for path in sorted(self.dir.glob("*.json")):
            if not valid_id(path.stem):
                continue
            profile = self.read(path.stem)
            if profile is not None:
                found.append({k: profile[k] for k in ("id", "name", "initials", "color")})
        return sorted(found, key=lambda p: p["name"].lower())

    def write(self, profile_id: str, settings: Dict[str, Any],
              name: Optional[str] = None, initials: Optional[str] = None,
              color: Optional[str] = None) -> None:
        """Write atomically.

        Anything left as None keeps what is on disk. `AppConfig.save` calls
        this with the settings alone on every save, so without that a save
        would reset the driver's badge.
        """
        path = self._path(profile_id)
        existing = self._load(profile_id) or {}
        if name is None:
            name = existing.get("name") or (
                DEFAULT_NAME if profile_id == DEFAULT_ID else profile_id)
        if initials is None:
            initials = existing.get("initials", "")
        if color is None:
            color = existing.get("color")
        data: Dict[str, Any] = {"name": name}
        # Blank initials are not stored, so a rename re-derives them.
        if initials:
            data["initials"] = initials
        if color:
            data["color"] = color
        data["settings"] = settings
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)

    def _name_taken(self, name: str, profile_id: Optional[str] = None) -> bool:
        return any(p["name"].lower() == name.lower() and p["id"] != profile_id
                   for p in self.list())

    def create(self, name: str, settings: Dict[str, Any],
               initials: Any = "", color: Any = None) -> str:
        name = clean_name(name)
        if self._name_taken(name):
            raise ProfileError(f"There is already a profile called {name}.")
        if color is None:
            # The first colour nobody has, so two drivers are not both orange.
            used = {p["color"] for p in self.list()}
            color = next((c for c in COLORS if c not in used), COLORS[0])
        color = clean_color(color)
        base = _slug(name)
        profile_id, n = base, 2
        while self._path(profile_id).exists():
            profile_id = f"{base}-{n}"
            n += 1
        self.write(profile_id, settings, name, clean_initials(initials), color)
        return profile_id

    def edit(self, profile_id: str, name: Any, initials: Any, color: Any) -> None:
        name = clean_name(name)
        profile = self._load(profile_id)
        if profile is None:
            raise ProfileError(f"No profile {profile_id!r}.")
        if self._name_taken(name, profile_id):
            raise ProfileError(f"There is already a profile called {name}.")
        self.write(profile_id, profile["settings"], name,
                   clean_initials(initials), clean_color(color))

    def delete(self, profile_id: str) -> None:
        path = self._path(profile_id)
        if not path.is_file():
            raise ProfileError(f"No profile {profile_id!r}.")
        path.unlink()
