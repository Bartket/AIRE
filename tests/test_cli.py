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
