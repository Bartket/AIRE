"""The command line, which had no tests and holds two easy traps.

A `--provider`/`--model`/`--telemetry` override is for one run and must never
reach config.json, and a one-shot `--ask` builds a whole Orchestrator — so it
also has to put the telemetry source back down when it is finished.
"""

from ai_race_engineer import cli
from ai_race_engineer.config import AppConfig


def _args(**overrides):
    argv = []
    for name, value in overrides.items():
        argv += [f"--{name.replace('_', '-')}"] if value is True else [
            f"--{name.replace('_', '-')}", str(value)]
    return cli.build_parser().parse_args(argv)


def test_a_session_only_override_is_never_written_to_disk(tmp_path, monkeypatch):
    """It applies to the run, not to the file the driver tuned by hand."""
    path = tmp_path / "config.json"
    monkeypatch.setenv("AIRE_CONFIG", str(path))

    config = cli._load_config(_args(config=str(path), model="test/model"))

    assert config.llm_settings()["model"] == "test/model"
    assert config.dirty is False, "the override would be saved on exit"
    assert not path.exists()


def test_setup_is_still_accepted_and_still_means_the_default(tmp_path):
    """It is declared and deliberately not read. Anyone whose shortcut passes
    it must not get an argument error."""
    args = cli.build_parser().parse_args(["--setup"])

    assert args.setup is True
    assert cli._want_desktop(args) is False      # not frozen, no --desktop


def test_a_one_shot_ask_releases_the_telemetry_source(tmp_path, monkeypatch):
    """The reader holds an open handle on iRacing's shared memory. Left
    unstopped it stays open until the process exits — which for `--ask`
    is immediately, but only by luck."""
    monkeypatch.setenv("AIRE_CONFIG", str(tmp_path / "config.json"))
    config = AppConfig({"telemetry": {"source": "simulated"}},
                       path=tmp_path / "config.json")
    shutdowns = []

    from ai_race_engineer import orchestrator as orchestrator_module

    real_init = orchestrator_module.Orchestrator.__init__

    def spy_init(self, cfg):
        real_init(self, cfg)
        original = self.telemetry.shutdown
        self.telemetry.shutdown = lambda: (shutdowns.append(True), original())[1]
        self.llm.generate = lambda *args: "Eight laps left in it."

    monkeypatch.setattr(orchestrator_module.Orchestrator, "__init__", spy_init)

    assert cli._run_ask(config, "How's my fuel?", speak=False) == 0
    assert shutdowns == [True]


def test_forgetting_unsaved_changes_goes_through_the_lock(tmp_path):
    """`_load_config` poked `config._dirty` directly, from outside the class
    and around the lock every other write to that flag takes.

    Asserting on the resulting flag alone cannot catch this — both spellings
    leave it False. What changed is that the CLI no longer reaches inside, so
    that is what is asserted.
    """
    import inspect

    assert "_dirty" not in inspect.getsource(cli), \
        "the CLI is writing a config private again"

    config = AppConfig({}, path=tmp_path / "config.json")
    config.update({"language": "pl"})
    assert config.dirty is True

    config.mark_clean()

    assert config.dirty is False
    config.maybe_save()
    assert not (tmp_path / "config.json").exists()


def test_the_log_file_keeps_failures_and_never_the_radio(tmp_path):
    """The packaged build has no console, so without a file every warning
    and error it raised went nowhere. The file must not become a transcript:
    questions and answers are INFO, and the exchange log is promised to stay
    in memory."""
    import logging

    path = tmp_path / "AIRE" / "aire.log"
    root = logging.getLogger()
    before = list(root.handlers)
    cli._log_to_file(path)
    added = [h for h in root.handlers if h not in before]
    try:
        log = logging.getLogger("race-engineer")
        previous_level = log.level
        log.setLevel(logging.INFO)
        try:
            log.info("Question: %r", "how far behind Kowalski am I")
            log.warning("Could not lower process priority: access denied")
        finally:
            log.setLevel(previous_level)
        for handler in added:
            handler.flush()
    finally:
        for handler in added:
            root.removeHandler(handler)
            handler.close()

    written = path.read_text(encoding="utf-8")
    assert "Could not lower process priority" in written
    assert "Kowalski" not in written


# Starts the way AIRE.exe does, with no console, and runs `main()` as far as
# uvicorn building its Config — which is where it died. A fresh interpreter,
# because nulling stdout inside pytest would take the test runner with it.
_CONSOLELESS = r'''
import sys
result_path, config_path = sys.argv[1], sys.argv[2]
sys.stdout = sys.stderr = None

import uvicorn
from ai_race_engineer import cli, perf, single_instance

def build_and_stop(app, **kwargs):
    uvicorn.Config(app, host=kwargs["host"], port=kwargs["port"],
                   log_level=kwargs["log_level"])
    return None

uvicorn.run = build_and_stop
single_instance.find_running = lambda host, port: False
perf.lower_priority = lambda enabled=True: False
sys.argv = ["aire", "--server", "--no-browser", "--config", config_path]
try:
    outcome = f"returned {cli.main()}"
except BaseException as exc:
    outcome = f"{type(exc).__name__}: {exc}"
with open(result_path, "w", encoding="utf-8") as f:
    f.write(outcome)
'''


def test_the_server_starts_without_a_console(tmp_path):
    """`AIRE.exe --server` exited with code 1 and wrote nothing anywhere.

    uvicorn asks `sys.stdout.isatty()`, and a build without a console has no
    stdout. Desktop mode got away with it only because pywebview, imported
    first, quietly substitutes one.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.json"
    config.write_text('{"telemetry": {"source": "simulated"}}', encoding="utf-8")
    result = tmp_path / "result.txt"

    subprocess.run(
        [sys.executable, "-c", _CONSOLELESS, str(result), str(config)],
        cwd=root, timeout=60, check=False,
        env={**os.environ, "PYTHONPATH": str(root)},
    )

    assert result.read_text(encoding="utf-8") == "returned 0"
