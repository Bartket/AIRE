"""Audio ducking must always restore the exact Windows mixer state."""

from ai_race_engineer import ducking


class _Process:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _Volume:
    def __init__(self, level):
        self.level = level
        self.set_to = []

    def GetMasterVolume(self):
        return self.level

    def SetMasterVolume(self, level, _context):
        self.level = level
        self.set_to.append(level)


class _Control:
    def __init__(self, volume):
        self.volume = volume

    def QueryInterface(self, _interface):
        return self.volume


class _Session:
    def __init__(self, pid, level, name="CrewChiefV4.exe"):
        self.ProcessId = pid
        self.Process = _Process(name)
        self.volume = _Volume(level)
        self._ctl = _Control(self.volume)


class _Utilities:
    sessions = []

    @classmethod
    def GetAllSessions(cls):
        return list(cls.sessions)


def _duck(monkeypatch, sessions, level=0.15):
    monkeypatch.setattr(ducking, "AudioUtilities", _Utilities)
    monkeypatch.setattr(ducking, "ISimpleAudioVolume", object())
    _Utilities.sessions = sessions
    control = ducking.AudioDuck({"process_name": "CrewChiefV4.exe",
                                 "duck_level": level})
    control._available = True
    return control


def test_ducking_never_turns_up_an_already_quiet_target(monkeypatch):
    session = _Session(10, 0.10)
    control = _duck(monkeypatch, [session], level=0.15)

    assert control.duck() is False
    assert session.volume.level == 0.10
    assert session.volume.set_to == []


def test_each_audio_session_restores_its_own_level_even_with_one_pid(monkeypatch):
    first = _Session(10, 0.80)
    second = _Session(10, 0.45)
    control = _duck(monkeypatch, [first, second])

    assert control.duck() is True
    assert first.volume.level == second.volume.level == 0.15
    assert control.un_duck() is True
    assert first.volume.level == 0.80
    assert second.volume.level == 0.45


def test_restore_does_not_depend_on_enumerating_the_sessions_again(monkeypatch):
    session = _Session(10, 0.80)
    control = _duck(monkeypatch, [session])
    assert control.duck() is True

    def unavailable():
        raise RuntimeError("Windows session list unavailable")

    monkeypatch.setattr(_Utilities, "GetAllSessions", unavailable)
    assert control.un_duck() is True
    assert session.volume.level == 0.80


def test_a_failed_restore_is_kept_for_retry(monkeypatch):
    session = _Session(10, 0.80)
    control = _duck(monkeypatch, [session])
    assert control.duck() is True

    original = session.volume.SetMasterVolume
    attempts = 0

    def fail_once(level, context):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient mixer error")
        original(level, context)

    session.volume.SetMasterVolume = fail_once
    assert control.un_duck() is False
    assert control._saved, "original level was discarded after a failed restore"
    assert control.un_duck() is True
    assert session.volume.level == 0.80
