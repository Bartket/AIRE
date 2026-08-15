"""Build the third-party notice file from the locked runtime environment."""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REQUIREMENTS = ROOT / "requirements.txt"
DEFAULT_OUTPUT = ROOT / "THIRD_PARTY_NOTICES.txt"
VUE_LICENSE = ROOT / "ai_race_engineer" / "static" / "vendor" / "VUE-LICENSE.txt"


def _runtime_distributions(requirements_path: Path):
    """Return each installed distribution selected by this platform's markers."""
    selected = {}
    for raw in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        key = canonicalize_name(requirement.name)
        if key in selected:
            continue
        selected[key] = metadata.distribution(requirement.name)
    return [selected[key] for key in sorted(selected)]


def _license_files(distribution):
    """Read license/notice files recorded in an installed distribution."""
    found = []
    for relative in distribution.files or []:
        name = Path(str(relative)).name.lower()
        if not name.startswith(("license", "licence", "copying", "notice")):
            continue
        path = Path(distribution.locate_file(relative))
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            found.append((str(relative).replace("\\", "/"), text))
    return sorted(found)


def _metadata_license(distribution) -> str:
    value = (distribution.metadata.get("License-Expression")
             or distribution.metadata.get("License") or "Not declared")
    return " ".join(value.split())


def render_notices(requirements_path: Path = DEFAULT_REQUIREMENTS) -> str:
    sections = [
        "AIRE THIRD-PARTY NOTICES",
        "========================",
        "",
        "This file covers the runtime packages selected by requirements.txt on",
        "the platform that built this copy of AIRE. Package license texts below",
        "are reproduced from the installed distributions.",
        "",
    ]
    for distribution in _runtime_distributions(requirements_path):
        name = distribution.metadata.get("Name") or "Unknown package"
        homepage = (distribution.metadata.get("Project-URL")
                    or distribution.metadata.get("Home-page") or "")
        sections.extend([
            "-" * 78,
            f"{name} {distribution.version}",
            f"Declared license: {_metadata_license(distribution)}",
        ])
        if homepage:
            sections.append(f"Project: {homepage}")
        files = _license_files(distribution)
        if not files:
            sections.append("No separate license file was installed by this distribution.")
        for relative, text in files:
            sections.extend(["", f"[{relative}]", text])
        sections.append("")

    sections.extend([
        "-" * 78,
        "Vue.js 3.5.41",
        "Project: https://vuejs.org/",
        "Declared license: MIT",
        "",
        "[VUE-LICENSE.txt]",
        VUE_LICENSE.read_text(encoding="utf-8").strip(),
        "",
    ])
    return "\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.write_text(render_notices(args.requirements), encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
