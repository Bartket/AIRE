"""The feedback bundle: a .zip a driver can attach to an issue or send on.

Holds the Radio Log entries they chose (as JSON and as readable text), the
facts needed to reproduce an answer, and optionally aire.log and the
diagnostic trace. Never an API key: `about.json` is built from an explicit
list of settings, not from the config minus its secrets, so a setting added
later with a key in it cannot ride along by default.
"""

import json
import platform
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import __version__

NAME_PATTERN = re.compile(r"^AIRE-feedback-\d{8}-\d{6}(-\d+)?\.zip$")


def export_dir(config_dir: Path) -> Path:
    """Downloads, where people look for a file they just saved; beside the
    config when there is no Downloads folder."""
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path(config_dir) / "exports"


def about(config) -> Dict[str, Any]:
    """App, platform and the settings that shape an answer. No keys, no
    prompt text, no names: a custom prompt or callsign is reported as set,
    not quoted."""
    llm = config.llm_settings()
    el = config.elevenlabs
    telemetry = config.telemetry
    pit = config.get("pit_commands", {}) or {}
    radio = config.get("radio_effect", {}) or {}
    ptb = config.push_to_button
    from .config import _DEFAULT_CONFIG

    return {
        "app_version": __version__,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "settings": {
            "llm_provider": llm["provider"],
            "llm_model": llm["model"],
            "use_tools": llm["use_tools"],
            "history_turns": llm["history_turns"],
            "max_tokens": llm["max_tokens"],
            "stt_model": el.get("stt_model"),
            "tts_model": el.get("tts_model"),
            "voice_name": el.get("tts_voice_name") or "(default)",
            "voice_settings": el.get("voice_settings"),
            "name_policy": el.get("name_policy"),
            "telemetry_source": telemetry.get("source"),
            "units": telemetry.get("units"),
            "fuel_units": telemetry.get("fuel_units"),
            "pit_commands": pit.get("enabled") is True,
            "pit_stop_confirmation": pit.get("confirm_pit_stop_commands") is not False,
            "radio_effect": radio.get("enabled") is True,
            "push_to_talk_mode": ptb.get("mode"),
            "push_to_talk_type": ptb.get("type"),
            "custom_system_prompt":
                config.get("system_prompt") != _DEFAULT_CONFIG["system_prompt"],
            "callsign_set": bool(config.get("driver_callsign")),
        },
    }


def _local(timestamp: str) -> str:
    try:
        return datetime.fromisoformat(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(timestamp)


def as_text(entries: Iterable[Dict[str, Any]]) -> str:
    """The entries as a reader would want them pasted into an issue."""
    lines: List[str] = []
    for e in entries:
        when = _local(e.get("timestamp", ""))
        if e.get("kind") == "event":
            count = f" (x{e['count']})" if e.get("count", 1) > 1 else ""
            lines.append(f"{when}  {str(e.get('level', '')).upper():<7} "
                         f"{e.get('source', '')}: {e.get('message', '')}{count}")
            continue
        tags = [e.get("route") or "", e.get("session_kind") or ""]
        total = (e.get("timings") or {}).get("total")
        if isinstance(total, (int, float)):
            tags.insert(1, f"{total / 1000:.1f} s")
        if e.get("speech_cached"):
            tags.append("cached speech")
        if e.get("failed"):
            tags.append("FAILED")
        tag = " · ".join(t for t in tags if t)
        lines.append(f"{when}  {e.get('type', '')}" + (f"  [{tag}]" if tag else ""))
        lines.append(f"  You:      {e.get('question', '')}")
        lines.append(f"  Engineer: {e.get('answer', '')}")
        feedback = e.get("feedback") or {}
        if feedback.get("rating") or feedback.get("note"):
            mark = {"up": "+1", "down": "-1"}.get(feedback.get("rating"), "")
            note = f' "{feedback["note"]}"' if feedback.get("note") else ""
            lines.append(f"  Feedback: {mark}{note}".rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_bundle(config, entries: List[Dict[str, Any]], out_dir: Path,
                 include_aire_log: bool = True, include_trace: bool = False,
                 now: Optional[datetime] = None) -> Path:
    """Write the bundle and return its path. Raises OSError if it cannot."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"AIRE-feedback-{stamp}.zip"
    n = 2
    while path.exists():
        path = out_dir / f"AIRE-feedback-{stamp}-{n}.zip"
        n += 1

    clean = [{k: v for k, v in e.items() if k != "rev"} for e in entries]
    info = about(config)
    info["exported"] = {"entries": len(clean),
                        "first": clean[0]["timestamp"] if clean else None,
                        "last": clean[-1]["timestamp"] if clean else None}
    config_dir = Path(config.path).parent
    tmp = path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("about.json", json.dumps(info, indent=2, ensure_ascii=False))
        bundle.writestr("radio-log.json", json.dumps(clean, indent=2, ensure_ascii=False))
        bundle.writestr("radio-log.txt", as_text(clean))
        if include_aire_log and (config_dir / "aire.log").is_file():
            bundle.write(config_dir / "aire.log", "aire.log")
        trace = config_dir / "diagnostic-trace.jsonl"
        if include_trace and trace.is_file():
            bundle.write(trace, "diagnostic-trace.jsonl")
    tmp.replace(path)
    return path
