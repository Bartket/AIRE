"""Replay diagnostic traces and grade the radio behaviors that must not regress."""

import argparse
import json
import re
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import race_math
from .config import AppConfig
from .llm import _REASONING_EFFORT, COMMS_FAILURE, LLMClient, NO_ANSWER
from .telemetry import TelemetrySnapshot, session_kind
from .telemetry.simulated import SimulatedTelemetryAdapter
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
    # What made the radio sound read off a spreadsheet: a time said to the
    # hundredth ("1.51 seconds"), and a rival named the way the roster
    # abbreviates them ("M. Rossi"). Neither helps a driver at speed.
    # In a race that is anything finer than a tenth. In practice and
    # qualifying the hundredths separate two drivers and are said on
    # purpose (race_math.lap_digits), so only thousandths fail there.
    in_race = session_kind(str(snapshot.get("session_type") or "")) == "race"
    if not finished and in_race and re.search(r"\b\d+\.\d{2,}\s*seconds\b", spoken):
        failures.append("race-speed answer gives a time to the hundredth")
    if not finished and not in_race and re.search(r"\b\d+\.\d{3,}\s*seconds\b", spoken):
        failures.append("answer gives a time to the thousandth")
    if re.search(r"\b[A-Z]\. [A-Z][a-z]", spoken):
        failures.append("rival named by initial instead of surname")
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


# The model check, as a command rather than a memory. Each probe is a question
# no deterministic route answers, so it reaches the model, and each check is a
# failure that reached a real session. A model is judged on pass^k — every one
# of k runs clean — because the driver hears every answer, not the best of k.
_SPA_CORNERS = ("la source", "eau rouge", "raidillon", "kemmel", "les combes",
                "malmedy", "rivage", "pouhon", "fagnes", "campus", "stavelot",
                "blanchimont", "bus stop")

Check = Callable[[str, str], bool]


def _used_a_calculator(answer: str, route: str) -> bool:
    return route.startswith("tool:") or route == "deterministic"


def _no_figures(answer: str, route: str) -> bool:
    return not re.search(r"\d", answer)


def _no_corner_name(answer: str, route: str) -> bool:
    return not any(corner in answer.lower() for corner in _SPA_CORNERS)


_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six",
                 "seven", "eight", "nine")


def _says_rossis_margin(answer: str, route: str) -> bool:
    """The calculator's tenths, in digits or words — not a subtraction of its own."""
    spoken = race_math.dispatch("pace_compare", {"target": "Rossi"},
                                _floor_snapshot())["spoken"]
    tenths = int(re.search(r"(\d) tenths", spoken).group(1))
    low = answer.lower()
    return f"{tenths} tenths" in low or f"{_NUMBER_WORDS[tenths]} tenths" in low


def _floor_snapshot() -> TelemetrySnapshot:
    """The simulated race, with what the real reader cannot see taken out.

    The fixture publishes brake temperatures and live tyre figures; iRacing
    publishes neither out on track. Left in, "how hot are my brakes" would
    be answered from data no driver's engineer will ever have.
    """
    snapshot = SimulatedTelemetryAdapter().get_telemetry_snapshot()
    car = snapshot.car
    car.brake_fl_temp = car.brake_fr_temp = None
    car.brake_rl_temp = car.brake_rr_temp = None
    car.tyre_data_live = False
    return snapshot


_FLOOR_PROBES: List[tuple] = [
    ("Which corner am I losing time in?",
     [("names no corner", _no_corner_name)]),
    # Rossi holds the fastest lap, so the block carries the margin already
    # worked out: answering from it is right, as long as the figure is.
    ("How much quicker is Rossi than me per lap?",
     [("says the calculated margin", _says_rossis_margin)]),
    # Silva's best lap is nowhere in the block, so an answer without a
    # calculator can only be invented.
    ("How much quicker is Silva than me per lap?",
     [("calls a calculator", _used_a_calculator)]),
    # Not "the gap to the leader": the running order states that one, and
    # reading it out is not arithmetic. Silva's gap has to be subtracted.
    ("How far am I behind Silva?",
     [("calls a calculator", _used_a_calculator)]),
    ("How hot are my brakes?",
     [("invents no reading", _no_figures)]),
    ("How are my tyres looking?",
     [("reads out no stale figures", _no_figures)]),
]


def floor_check(config: AppConfig, runs: int,
                reasoning_effort: Optional[str]) -> List[Dict[str, Any]]:
    """Ask every probe `runs` times from a clean history; record what it cost."""
    client = LLMClient(config, reasoning_effort=reasoning_effort)
    results = []
    try:
        for question, checks in _FLOOR_PROBES:
            for run in range(1, runs + 1):
                snapshot = _floor_snapshot()
                client.reset_history()
                client.reset_usage()
                started = time.perf_counter()
                answer = client.generate(question, snapshot)
                elapsed_ms = (time.perf_counter() - started) * 1000
                route = client.last_route
                failures = [name for name, check in checks
                            if not check(answer, route)]
                if not answer or answer in (NO_ANSWER, COMMS_FAILURE):
                    failures.append("answers at all")
                failures += grade_record(
                    {"question": question, "route": route,
                     "snapshot": snapshot.to_dict()}, answer)
                results.append({
                    "question": question, "run": run, "answer": answer,
                    "route": route, "failures": failures,
                    "llm_ms": round(elapsed_ms), "usage": client.last_usage,
                })
    finally:
        client.close()
    return results


def _report_floor(results: List[Dict[str, Any]], runs: int) -> int:
    failed_probes = 0
    for question, _ in _FLOOR_PROBES:
        mine = [r for r in results if r["question"] == question]
        clean = sum(1 for r in mine if not r["failures"])
        verdict = "PASS" if clean == len(mine) else "FAIL"
        failed_probes += verdict == "FAIL"
        print(f"{verdict} {clean}/{len(mine)}  {question}")
        for r in mine:
            mark = "  " if not r["failures"] else "x "
            print(f"   {mark}[{r['route']}, {r['llm_ms']} ms] {r['answer']}")
            for failure in r["failures"]:
                print(f"       - {failure}")

    def spread(values: List[float]) -> str:
        if not values:
            return "n/a"
        ordered = sorted(values)
        p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
        return f"median {statistics.median(ordered):.0f}, p95 {p95:.0f}"

    print(f"\nllm ms: {spread([r['llm_ms'] for r in results])}")
    for key in ("completion", "calls"):
        values = [r["usage"][key] for r in results if key in r["usage"]]
        print(f"{key} per question: {spread(values)}")
    print(f"pass^{runs}: {len(_FLOOR_PROBES) - failed_probes}/{len(_FLOOR_PROBES)} probes")
    return 1 if failed_probes else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Grade or replay an AIRE diagnostic trace")
    parser.add_argument("trace", type=Path, nargs="?")
    parser.add_argument("--config")
    parser.add_argument(
        "--replay-model", action="store_true",
        help="Call the configured LLM for each trace entry; this incurs API cost",
    )
    parser.add_argument(
        "--floor", type=int, metavar="K",
        help="Ask every model-check probe K times on simulated telemetry; "
             "this incurs API cost",
    )
    parser.add_argument(
        "--effort", default="app",
        help="With --floor: reasoning effort to send (none, low, medium, high), "
             "'default' to send none, or 'app' for what the app sends",
    )
    args = parser.parse_args(argv)
    if args.floor:
        config = AppConfig.load(args.config)
        effort = {"app": _REASONING_EFFORT,
                  "default": None}.get(args.effort, args.effort)
        settings = config.llm_settings()
        print(f"Model check: {settings['model']} via {settings['provider']}, "
              f"reasoning effort {effort or 'model default'}, "
              f"{len(_FLOOR_PROBES)} probes x {args.floor} runs. "
              "This calls the model and costs money.\n")
        return _report_floor(floor_check(config, args.floor, effort), args.floor)
    if args.trace is None:
        parser.error("a trace file is required unless --floor is given")
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
