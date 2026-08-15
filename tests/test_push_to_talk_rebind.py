"""Switching push-to-talk between keyboard and wheel must actually switch it.

Both of these reached a real setup: the panel showed the new input while the
old one was still the thing listening, and the only way out was to re-bind.
"""

import pytest

from ai_race_engineer.config import AppConfig
from ai_race_engineer.orchestrator import Orchestrator


def _orchestrator(binding):
    config = AppConfig({
        "push_to_button": binding,
        # Keeps the adapter off iRacing and out of the test.
        "telemetry": {"source": "simulated"},
    })
    return Orchestrator(config)


def test_switching_from_an_absent_wheel_to_a_key_rebinds():
    """The guard used to ask the *outgoing* binding whether it was available.

    A wheel that is unplugged — or was never bound — answers no, which
    skipped the rebind entirely. Switching to a keyboard key then did
    nothing at all until the app was restarted.
    """
    orch = _orchestrator({"type": "controller", "device_name": "Absent wheel",
                          "button": 3})
    assert orch.push_to_button.available is False, "fixture needs an absent wheel"

    orch.config.update({"push_to_button": {"type": "keyboard", "key": "f8"}})
    orch.apply_config()

    assert orch.push_to_button.description == "keyboard:F8"
    assert orch.status()["hotkey"] == "keyboard:F8"


def test_switching_from_a_key_to_the_wheel_rebinds():
    """The mirror case, which is how it was first noticed."""
    orch = _orchestrator({"type": "keyboard", "key": "shift"})
    assert orch.push_to_button.description == "keyboard:Shift"

    orch.config.update({"push_to_button": {"type": "controller",
                                           "device_name": "Fanatec CSL",
                                           "button": 3}})
    orch.apply_config()

    assert orch.push_to_button.description == "controller:Fanatec CSL button 3"


def test_a_failed_rebind_is_reported():
    """Selecting the wheel without binding a button leaves nothing listening.

    That is a fair thing to require, but it was only written to the log, so
    the panel showed no error and push-to-talk was simply dead.
    """
    orch = _orchestrator({"type": "keyboard", "key": "shift"})
    orch._running = True

    # Wheel selected, no button bound.
    orch.config.update({"push_to_button": {"type": "controller", "button": -1,
                                           "device_guid": "", "device_name": ""}})
    orch.apply_config()

    assert orch.status()["error"], "a dead push-to-talk must reach the status bar"
    assert "button" in orch.status()["error"].lower()


class _FakeHub:
    """Enough of ControllerHub to drive a button press without a wheel."""

    def __init__(self, device):
        self.device = device
        self.subscribers = []
        self.error = None

    def start(self):
        return True

    def devices(self):
        return [self.device] if self.device is not None else []

    def subscribe(self, callback):
        self.subscribers.append(callback)

    def unsubscribe(self, callback):
        if callback in self.subscribers:
            self.subscribers.remove(callback)

    def press(self, button, pressed=True):
        for callback in list(self.subscribers):
            callback(self.device.guid, self.device.name, self.device.index,
                     button, pressed)


def test_switching_to_the_wheel_leaves_the_wheel_actually_listening(monkeypatch):
    """The switch has to end with the wheel bound, not merely selected.

    Asserting on `description` alone would pass while nothing was subscribed
    to the hub — which is the difference between the panel saying it is bound
    and the button doing something.
    """
    from ai_race_engineer import controller as controller_mod

    wheel = controller_mod.ControllerInfo(
        index=0, name="Fanatec CSL DD", guid="030000000f0e", buttons=32)
    hub = _FakeHub(wheel)
    monkeypatch.setattr(controller_mod.ControllerHub, "instance",
                        staticmethod(lambda: hub))

    orch = _orchestrator({"type": "keyboard", "key": "shift"})
    orch._running = True
    presses = []
    monkeypatch.setattr(orch, "_on_button_down", lambda: presses.append(True))

    # Exactly the reported flow: the wheel binding is already stored from an
    # earlier session, and only the type changes.
    orch.config.update({"push_to_button": {
        "type": "controller", "device_guid": wheel.guid,
        "device_name": wheel.name, "device_index": 0, "button": 7}})
    orch.apply_config()

    assert hub.subscribers, "nothing subscribed to the controller hub"
    assert orch.status()["hotkey_available"] is True
    assert orch.status()["error"] is None

    hub.press(7)
    assert presses, "the bound wheel button did not reach the orchestrator"


def test_a_wheel_appearing_after_startup_activates_the_saved_binding(monkeypatch):
    """The wheel can enumerate after the app's two-second startup wait.

    The status check then found it and showed green, but startup had already
    abandoned the subscription, so the button produced no blip, recording or
    transcription despite looking available.
    """
    from ai_race_engineer import controller as controller_mod
    from ai_race_engineer.input_handler import PushToButton

    wheel = controller_mod.ControllerInfo(
        index=3, name="MOZA R25 Ultra True Torque Base Base",
        guid="030000004d4f5a41", buttons=128)
    hub = _FakeHub(None)
    monkeypatch.setattr(controller_mod.ControllerHub, "instance",
                        staticmethod(lambda: hub))

    button = PushToButton({
        "type": "controller", "device_guid": wheel.guid,
        "device_name": wheel.name, "device_index": 0, "button": 24})
    presses = []
    releases = []
    button.on_press = lambda: presses.append(True)
    button.on_release = lambda: releases.append(True)

    button.start()
    assert hub.subscribers, "listener abandoned a temporarily absent wheel"
    assert button.available is False

    hub.device = wheel
    assert button.available is True
    hub.press(24, True)
    hub.press(24, False)
    assert presses and releases


def test_controller_status_cannot_be_green_without_a_live_subscription(monkeypatch):
    from ai_race_engineer import controller as controller_mod
    from ai_race_engineer.input_handler import PushToButton

    wheel = controller_mod.ControllerInfo(
        index=0, name="MOZA R25", guid="moza", buttons=128)
    hub = _FakeHub(wheel)
    monkeypatch.setattr(controller_mod.ControllerHub, "instance",
                        staticmethod(lambda: hub))
    button = PushToButton({"type": "controller", "device_guid": "moza",
                           "device_name": "MOZA R25", "button": 24})

    assert button.available is False
    button.start()
    assert button.available is True


def test_an_unchanged_binding_is_left_alone():
    """Saving unrelated settings must not tear down a working listener."""
    orch = _orchestrator({"type": "keyboard", "key": "shift"})
    before = orch.push_to_button

    orch.config.update({"radio_effect": {"noise": 0.2}})
    orch.apply_config()

    assert orch.push_to_button is before
