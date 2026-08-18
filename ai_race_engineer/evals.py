"""Replay diagnostic traces and grade the radio behaviors that must not regress."""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import AppConfig
from .llm import LLMClient, NO_ANSWER
from .telemetry import TelemetrySnapshot, session_kind
from .text import normalise_question


def load_trace(path: Path) -> List[Dict[str, Any]]:
    records = []
    with open(path, encoding="utf-8") as source:
        for number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: {exc}") from exc
    return records


def grade_record(record: Dict[str, Any], answer: Optional[str] = None) -> List[str]:
    """Deterministic safety/concision checks over one recorded exchange."""
    question = str(record.get("question") or "")
    spoken = str(answer if answer is not None else record.get("answer") or "")
    q = normalise_question(question)
    low = spoken.lower()
    failures = []
    snapshot = record.get("snapshot") or {}

    if record.get("route") == "stt_guard" and spoken != NO_ANSWER:
        failures.append("STT guard did not use the fixed say-again response")

    movement = any(word in q for word in ("gaining", "catching", "closing")) \
        and any(word in q for word in ("ahead", "front", "behind"))
    if movement:
        if "per second" in low or "contact in" in low:
            failures.append("adjacent trend invented a physical rate/contact time")
        if "lap" in low and "mini-sector" not in low:
            failures.append("adjacent trend described its evidence as laps")
        if record.get("route") not in (None, "deterministic"):
            failures.append("common adjacent-trend intent did not use deterministic routing")

    position_phrases = {
        "position", "position status", "position update", "my position",
        "current position", "race position", "what is my position",
        "whats my position", "where am i",
    }
    if q in position_phrases:
        for side in ("ahead", "behind"):
            rival = snapshot.get(side)
            if isinstance(rival, dict) and rival.get("gap") is not None:
                name = rival.get("name") or rival.get("car_number")
                if name and str(name).lower() not in low:
                    failures.append(f"position answer omitted the car {side}")
        if record.get("route") not in (None, "deterministic"):
            failures.append("position intent did not use deterministic routing")

    best = snapshot.get("fuel_margin_best")
    conservative = snapshot.get("fuel_margin_conservative")
    if best is not None and conservative is not None and best >= 0 > conservative:
        if "marginal" not in low:
            failures.append("fuel range spans the finish but answer is not marginal")
        if "pit now" in low or "stop now" in low:
            failures.append("marginal fuel was turned into an immediate stop")

    # Qualifying is a lap allowance inside a window, not a stretch of time
    # to keep trying in. The engineer read the session clock as the driver's
    # budget and offered eight more minutes of running to a driver who had
    # two laps left, so an answer about how much running is left may not be
    # built from the clock where iRacing published an allowance.
    if session_kind(str(snapshot.get("session_type") or "")) == "qualify":
        asking_how_long = any(word in q for word in (
            "how long", "how much time", "time left", "how many laps",
            "laps left", "time remaining"))
        if asking_how_long:
            # The trace records the route but not which tools ran, so this
            # grades the answer rather than how it was reached: a session
            # with a published allowance answered purely in minutes is the
            # clock being read as the budget, whatever produced it.
            if (snapshot.get("laps_total") and "minute" in low
                    and "lap" not in low):
                failures.append("qualifying budget answered from the clock, "
                                "not the published lap allowance")

    finished = bool(snapshot.get("finished"))
    if not finished and len(spoken.split()) > 45:
        failures.append("race-speed answer exceeds 45 words")
    return failures


def evaluate(records: Iterable[Dict[str, Any]], config: AppConfig,
             replay_model: bool = False) -> List[Dict[str, Any]]:
    """Grade stored answers, or deliberately pay to replay them through the model."""
    client = LLMClient(config) if replay_model else None
    results = []
    session_key = None
    for index, record in enumerate(records, start=1):
        raw = record.get("snapshot")
        snapshot = TelemetrySnapshot.from_dict(raw) if isinstance(raw, dict) else None
        if client is not None and snapshot is not None:
            key = (snapshot.track.subsession_id, snapshot.track.session_id,
                   snapshot.track.session_num, snapshot.track.session_generation)
            if session_key is not None and key != session_key:
                client.reset_history()
            session_key = key
        answer = (client.generate(str(record.get("question") or ""), snapshot)
                  if client is not None else str(record.get("answer") or ""))
        results.append({
            "index": index,
            "question": record.get("question"),
            "answer": answer,
            "failures": grade_record(record, answer),
        })
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Grade or replay an AIRE diagnostic trace")
    parser.add_argument("trace", type=Path)
    parser.add_argument("--config")
    parser.add_argument(
        "--replay-model", action="store_true",
        help="Call the configured LLM for each trace entry; this incurs API cost",
    )
    args = parser.parse_args(argv)
    try:
        records = load_trace(args.trace)
    except (OSError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 2
    results = evaluate(records, AppConfig.load(args.config), args.replay_model)
    failed = 0
    for result in results:
        if result["failures"]:
            failed += 1
            print(f"FAIL {result['index']}: {result['question']}")
            for failure in result["failures"]:
                print(f"  - {failure}")
        else:
            print(f"PASS {result['index']}: {result['question']}")
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
