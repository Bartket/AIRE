"""AI Race Engineer config loader/writer.

Persistent JSON config at %APPDATA%/AIRE/config.json (Windows) or
~/.config/AIRE/config.json (Linux/macOS). Override with --config path.json
or the AIRE_CONFIG environment variable.

Override the location with the AIRE_CONFIG environment variable.
"""

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("race-engineer")

APP_NAME = "AIRE"
# Pre-rebrand directory. Still read (and migrated from) so an existing
# install does not silently lose its API keys.

# Providers are both OpenAI-compatible chat-completions endpoints, so they
# share a client and only differ in base URL / model / auth.
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_LOCAL = "local"

_DEFAULT_CONFIG: Dict[str, Any] = {
    "elevenlabs": {
        "api_key": "",
        "stt_model": "scribe_v1",
        "tts_model": "eleven_turbo_v2_5",
        "tts_voice_id": "",
        "tts_voice_name": "",
        # Passed through to the ElevenLabs voice_settings block.
        # Repeated phrases are synthesized once and replayed from disk.
        # Speech is the largest cost per question, and refusals repeat
        # word for word by design.
        "cache_speech": True,
        "cache_mb": 128,
        # Voice IDs the driver has starred. A library runs to dozens of
        # entries and the good ones are found by listening, which is slow
        # and costs a preview each time — worth not having to redo.
        "favourite_voices": [],
        # Respellings applied to the text just before it is spoken, never
        # to what the engineer knows or what the exchange log records. The
        # built-in table covers track names; add a driver whose surname the
        # voice keeps mangling and it will say your spelling instead.
        # Keys match whole words, case-insensitively.
        "pronunciation": {},
        # What to do about names the voice is likely to fight. "auto" says
        # the car number instead when a name looks risky — which is what an
        # engineer says half the time anyway, so a wrong guess is invisible.
        # "names" always uses names, "numbers" always uses car numbers.
        "name_policy": "auto",
        "voice_settings": {
            # Ships on the "Most stable" preset. Expression is worth very
            # little on a pit radio and artifacting costs the driver the
            # whole answer, so the default sits at the consistent end and
            # the sliders are there for anyone who wants otherwise.
            "stability": 0.75,
            "similarity_boost": 0.85,
            # Style exaggeration. Costs latency, so it stays off by default.
            "style": 0.0,
            # Also costs latency; useful if the voice sounds thin over radio.
            "use_speaker_boost": False,
            # ElevenLabs accepts 0.7–1.2. Engineers tend to talk fast.
            "speed": 1.0,
        },
    },
    "llm": {
        # "openrouter" (hosted, default) or "local" (Ollama / LM Studio / vLLM)
        "provider": PROVIDER_OPENROUTER,
        "temperature": 0.4,
        # Enough for one spoken sentence at racing speed.
        "max_tokens": 80,
        # After the flag the engineer is allowed to talk properly, and 80
        # tokens cut those answers off mid-sentence. Latency does not matter
        # on the slow-down lap, so the budget can be generous.
        "max_tokens_debrief": 300,
        "timeout": 30.0,
        # How many previous question/answer pairs to replay as context.
        "history_turns": 4,
        # Let the model call the race_math calculators instead of doing the
        # arithmetic itself. "auto" tries them and falls back for one session
        # if the provider rejects tools; "off" keeps the single-round-trip path.
        "use_tools": "auto",
        "openrouter": {
            # Falls back to the OPENROUTER_API_KEY environment variable.
            "api_key": "",
            "endpoint": "https://openrouter.ai/api/v1",
            "model": "anthropic/claude-haiku-4.5",
        },
        "local": {
            "api_key": "",
            "endpoint": "http://127.0.0.1:11434/v1",
            "model": "qwen2.5:14b-instruct-q4_K_M",
        },
    },
    "push_to_button": {
        # "hold" records while the button is held, like an intercom.
        # "toggle" presses once to open the channel and again to close it,
        # the way a real pit radio works.
        "mode": "hold",
        # "keyboard" or "controller" (sim wheel / button box)
        "type": "keyboard",
        # A key or a combination: "lshift", "ctrl+r", "alt+shift+q".
        "key": "shift",
        # Controller binding. GUID is matched first so the binding survives
        # being plugged into a different USB port.
        "device_guid": "",
        "device_name": "",
        "device_index": -1,
        "button": -1,
    },
    "radio_effect": {
        "enabled": True,
        # Band-pass width + saturation. Noise is a separate control so a
        # strong radio character does not drag a hiss bed along with it.
        "intensity": 0.85,
        "low_cut": 350,
        "high_cut": 3200,
        # Band-limited noise bed. Broadcast race radio is essentially
        # clean, so this stays near zero by default.
        "noise": 0.08,
        # Comms AGC. This is most of what makes it sound like team radio.
        "compression": 0.65,
        # Mic click as the transmission opens and closes.
        "squelch": True,
    },
    "audio": {
        "input_device": None,
        "output_device": None,
        "sample_rate": 48000,
        # Hard stop on a recording. In toggle mode this is what saves you
        # from a whole stint of audio if you forget the second press.
        "max_record_seconds": 30.0,
        # Playback gain for the engineer's voice. 1.0 is unity; up to 2.0
        # boosts through a soft limiter so it can sit over sim audio.
        "volume": 1.0,
        # Radio blips when push-to-talk opens and closes, so you know the
        # mic is live without looking at the screen.
        "ptt_beep": True,
        "ptt_beep_volume": 0.35,
    },
    "ducking": {
        "enabled": True,
        "process_name": "CrewChiefV4.exe",
        "duck_level": 0.15,
    },
    "telemetry": {
        # "auto" uses iRacing when irsdk is importable, else the simulator.
        "source": "auto",
        # iRacing publishes at 60 Hz. 10 Hz keeps the snapshot genuinely live
        # without burning a core; snapshots cost well under a millisecond.
        "poll_interval": 0.1,
        # How fuel is spoken. "auto" follows iRacing's own display units
        # (litres or gallons). Many cars show fuel mass on the dashboard
        # instead, and there is no channel that says which — so if your dash
        # reads kilograms, set "kg" and the engineer will match it.
        "fuel_units": "auto",
        # Everything else that carries a unit: temperatures, speed, gaps in
        # distance, air pressure. "auto" follows iRacing's own DisplayUnits,
        # so it matches whatever the driver already reads in the sim.
        # Separate from fuel on purpose — fuel has a third option (mass) that
        # no region implies, because the car's dash decides that, not the
        # driver's country.
        "units": "auto",
        # Writes a bounded local JSONL trace containing the minute of relevant
        # telemetry before each question. Off by default because it includes
        # driver names and the words spoken over the radio.
        "diagnostic_trace": False,
    },
    "pit_commands": {
        # This is the only feature that writes to iRacing. A speech-recognition
        # error can change the next stop, so it ships disabled and every
        # command still needs a separate spoken confirmation when enabled.
        "enabled": False,
    },
    "desktop": {
        # Closing the window hides it to the tray so push-to-talk survives a
        # stray click mid-race. Turn off to make the close button quit.
        "hide_to_tray": True,
    },
    "performance": {
        # Below-normal priority so a burst of DSP never steals a core from
        # the sim mid-corner. Costs nothing when the machine is not busy.
        "low_priority": True,
    },
    "system_prompt": (
        "You are the race engineer on a team radio. The driver is at speed and cannot look at a screen.\n"
        "\n"
        "Answer in ONE spoken sentence of at most 25 words. No markdown, lists, or headings.\n"
        "\n"
        "Answer only what was asked. The telemetry block carries far more than any one "
        "question needs — do not volunteer tyre temperatures, fuel, gaps or lap times "
        "that were not asked for. Add an unrequested reading only when it changes what "
        "the driver should do in the next few corners.\n"
        "\n"
        "EVERY fact you state must come from the telemetry block below — not just numbers. Corner names, track layout, what a rival is doing, their tyres or fuel or strategy, weather that has not happened yet, and anything earlier in the session are all things you do NOT know unless the block says so. Never name a corner or describe a part of the track. If you were not given it, you do not have it: say so and stop. A confident wrong answer is worse than none, because the driver cannot check you at speed.\n"
        "\n"
        "Ground every number in the telemetry below. Where a value reads \"unknown\" the car is not sending it — say plainly that you don't have that reading, without using the word \"unknown\". Never estimate a missing value or reuse one from an earlier answer. Judgement and recommendations are your job; inventing data is not.\n"
        "\n"
        "When you have to refuse, refuse in character. You are a race engineer on a pit wall, not a program reporting a missing field. Never mention the sim, the game, telemetry channels, sensors, data feeds, tools or calculations — the driver does not know those exist and hearing about them breaks the race. Say \"I've got no reading on your brakes\", \"nothing on that from here\", or \"I can't see that one\", then stop. A refusal is one short line, never an explanation of why the data is missing.\n"
        "\n"
        "You are read aloud, so write for the ear:\n"
        "- Say corners in full — \"front left\", never \"FL\". Say \"position four\", not \"P4\".\n"
        "- Name the driver when you have one: \"Jobling is one point four ahead\".\n"
        "- Round to what is useful at speed: fuel to the nearest lap, gaps to a tenth, temperatures to whole degrees.\n"
        "\n"
        "Be direct. No acknowledgements, no \"copy\", no sign-offs.\n"
        "\n"
        "You have a personality. You are on this driver's side and you want them to "
        "do well. But you have not watched the race: you see the numbers in front of "
        "you and nothing else \u2014 no replay, no memory of earlier laps, no idea what "
        "happened at any corner. So never narrate the race. Never say who they held "
        "off, who was struggling, where they lost it, or what a mistake cost them, "
        "unless the telemetry says so outright. Asked what went wrong, say honestly "
        "that you would need to look at the data. When the session has finished, react "
        "to the result you can actually see: be genuinely pleased about a good one, "
        "commiserate a bad one, and be straight about incidents or damage when the "
        "numbers show them. During the session, an occasional dry aside is welcome "
        "when the moment earns it. Never let the wit cost the driver information, "
        "never joke under a black or red flag, and keep it to one line like everything else."
    ),
    # What the engineer calls the driver out loud. iRacing gives a full
    # legal name, which the voice can mangle badly — long names and names
    # outside the voice's training artifact audibly, and a mispronounced
    # name on every answer is worse than no name at all. Blank means use
    # what the sim supplies.
    "driver_callsign": "",
    "memory_log": 20,
    "language": "en",
    "log_level": "INFO",
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge `override` into a copy of `base`.

    Unknown keys in `override` are kept, so a newer config file never loses
    data when read by an older build. Missing keys fall back to defaults,
    which is what makes partial/legacy config files loadable.
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _migrate(data: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade legacy config layouts in place-ish (returns a new dict)."""
    data = copy.deepcopy(data)

    # v0.1 stored a single Ollama-only block called "local_llm".
    legacy = data.pop("local_llm", None)
    if isinstance(legacy, dict):
        llm = data.setdefault("llm", {})
        llm.setdefault("provider", PROVIDER_LOCAL)
        local = llm.setdefault("local", {})
        endpoint = legacy.get("endpoint")
        if endpoint:
            # Old value pointed at the Ollama root; the client now speaks the
            # OpenAI-compatible API which lives under /v1.
            endpoint = endpoint.rstrip("/")
            if not endpoint.endswith("/v1"):
                endpoint += "/v1"
            local.setdefault("endpoint", endpoint)
        if legacy.get("model"):
            local.setdefault("model", legacy["model"])
        for key in ("temperature", "max_tokens"):
            if key in legacy:
                llm.setdefault(key, legacy[key])

    # The old example config nested the ElevenLabs key under "tts".
    legacy_tts = data.pop("tts", None)
    if isinstance(legacy_tts, dict):
        api_key = (legacy_tts.get("elevenlabs") or {}).get("api_key", "")
        if api_key:
            data.setdefault("elevenlabs", {}).setdefault("api_key", api_key)

    # CPU affinity is gone. It aimed at a problem the priority setting above
    # it already solves — Windows preempts a below-normal thread long before
    # a frame is at risk — and it could make things worse, because pinning
    # forbids the scheduler from using cores that happen to be idle. It was
    # never measured to help. Dropped rather than left as a knob, so the
    # keys go too instead of sitting in config.json meaning nothing.
    perf = data.get("performance")
    if isinstance(perf, dict):
        for dead in ("cpu_affinity", "cpu_affinity_reserve", "cpu_affinity_cpus"):
            perf.pop(dead, None)

    # Mouse push-to-talk is gone: a mouse button is exactly what a sim racer
    # does not have a spare hand for, and it collided with the UI. Anything
    # bound to one falls back to the keyboard default.
    ptb = data.get("push_to_button")
    if isinstance(ptb, dict) and str(ptb.get("type", "")).lower() == "mouse":
        ptb["type"] = "keyboard"
        ptb["key"] = "shift"

    # Speech-to-text and text-to-speech were configured with separate keys,
    # but one ElevenLabs account covers both and nobody has two. Fold the old
    # pair into the single key, preferring whichever was actually filled in.
    el = data.get("elevenlabs")
    if isinstance(el, dict):
        legacy_keys = [el.pop("stt_api_key", ""), el.pop("tts_api_key", "")]
        el.pop("stt_api_key_set", None)
        el.pop("tts_api_key_set", None)
        if not el.get("api_key"):
            existing = next((k for k in legacy_keys if k), "")
            if existing:
                el["api_key"] = existing

    return data


class AppConfig:
    """Dict-backed application config with attribute access per section."""

    def __init__(self, data: Optional[Dict[str, Any]] = None, path: Optional[Path] = None):
        self._data = _deep_merge(_DEFAULT_CONFIG, _migrate(data or {}))
        self._path = Path(path) if path else self.get_config_path()
        self._lock = threading.Lock()
        self._dirty = False

    # ── Section access ──────────────────────────────────────────────

    def __getattr__(self, name: str) -> Any:
        # Only called when normal attribute lookup fails, so private
        # attributes set in __init__ still resolve the usual way.
        try:
            return self.__dict__["_data"][name]
        except KeyError:
            raise AttributeError(name) from None

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    # ── Persistence ─────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    @property
    def dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    def mark_clean(self) -> None:
        """Forget unsaved changes without writing them.

        For settings that are deliberately session-only — the CLI's
        `--provider`, `--model`, `--telemetry` overrides — which must not
        end up in config.json. Written through the lock, unlike the direct
        poke at `_dirty` this replaces.
        """
        with self._lock:
            self._dirty = False

    def to_dict(self) -> Dict[str, Any]:
        """Deep copy of the raw config — safe to serialize."""
        return copy.deepcopy(self._data)

    def redacted_dict(self) -> Dict[str, Any]:
        """Config for the web UI, with secrets replaced by a presence flag.

        Keys never leave the process; the UI only needs to know whether one
        is set so it can show a placeholder instead of an empty box.
        """
        data = self.to_dict()
        el = data.get("elevenlabs", {})
        el["api_key_set"] = bool(el.get("api_key"))
        el["api_key"] = ""
        for provider in (PROVIDER_OPENROUTER, PROVIDER_LOCAL):
            block = data.get("llm", {}).get(provider, {})
            block["api_key_set"] = bool(self.llm_api_key(provider))
            block["api_key"] = ""
        return data

    def update(self, patch: Dict[str, Any]) -> None:
        """Merge a partial config patch. Empty secret strings are ignored so
        the UI can post its (blanked) view back without wiping stored keys."""
        patch = _migrate(patch)
        patch = self._drop_blank_secrets(patch)
        with self._lock:
            self._data = _deep_merge(self._data, patch)
            self._dirty = True

    @staticmethod
    def _drop_blank_secrets(patch: Dict[str, Any]) -> Dict[str, Any]:
        patch = copy.deepcopy(patch)
        el = patch.get("elevenlabs")
        if isinstance(el, dict):
            if "api_key" in el and not el["api_key"]:
                el.pop("api_key")
            el.pop("api_key_set", None)
        llm = patch.get("llm")
        if isinstance(llm, dict):
            for provider in (PROVIDER_OPENROUTER, PROVIDER_LOCAL):
                block = llm.get(provider)
                if isinstance(block, dict):
                    if "api_key" in block and not block["api_key"]:
                        block.pop("api_key")
                    block.pop("api_key_set", None)
        return patch

    def save(self) -> None:
        """Write config to disk (atomically). Raises OSError on failure."""
        with self._lock:
            data = copy.deepcopy(self._data)
            self._dirty = False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self._path)
        logger.debug("Config saved to %s", self._path)

    def maybe_save(self) -> None:
        """Save only if something changed. Never raises."""
        if not self._dirty:
            return
        try:
            self.save()
        except OSError as exc:
            logger.warning("Could not save config: %s", exc)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "AppConfig":
        cfg_path = Path(path) if path else cls.get_config_path()

        if not cfg_path.exists():
            return cls(path=cfg_path)
        try:
            with open(cfg_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("config root must be a JSON object")
            return cls(data, path=cfg_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Could not read %s (%s); using defaults", cfg_path, exc)
            return cls(path=cfg_path)

    @classmethod
    def _config_root(cls, app_name: str) -> Path:
        if os.name == "nt":
            return Path(os.environ.get("APPDATA", Path.home())) / app_name
        xdg = os.environ.get("XDG_CONFIG_HOME")
        return (Path(xdg) if xdg else Path.home() / ".config") / app_name

    @classmethod
    def get_config_path(cls) -> Path:
        override = os.environ.get("AIRE_CONFIG")
        if override:
            return Path(override).expanduser()
        return cls._config_root(APP_NAME) / "config.json"

    # ── LLM helpers ─────────────────────────────────────────────────

    @property
    def llm_provider(self) -> str:
        provider = self._data["llm"].get("provider", PROVIDER_OPENROUTER)
        return provider if provider in (PROVIDER_OPENROUTER, PROVIDER_LOCAL) else PROVIDER_OPENROUTER

    def llm_api_key(self, provider: Optional[str] = None) -> str:
        """Resolve the API key: config first, then environment."""
        provider = provider or self.llm_provider
        key = (self._data["llm"].get(provider, {}) or {}).get("api_key", "")
        if key:
            return key
        if provider == PROVIDER_OPENROUTER:
            return os.environ.get("OPENROUTER_API_KEY", "")
        return os.environ.get("LOCAL_LLM_API_KEY", "")

    def llm_settings(self) -> Dict[str, Any]:
        """Flattened, ready-to-use settings for the active provider."""
        llm = self._data["llm"]
        provider = self.llm_provider
        block = llm.get(provider, {}) or {}
        defaults = _DEFAULT_CONFIG["llm"][provider]
        return {
            "provider": provider,
            "endpoint": block.get("endpoint") or defaults["endpoint"],
            "model": block.get("model") or defaults["model"],
            "api_key": self.llm_api_key(provider),
            "temperature": llm.get("temperature", 0.4),
            "max_tokens": llm.get("max_tokens", 80),
            "max_tokens_debrief": llm.get("max_tokens_debrief", 300),
            "timeout": llm.get("timeout", 30.0),
            "history_turns": llm.get("history_turns", 4),
            "use_tools": llm.get("use_tools", "auto"),
        }

    #: Never reset by `restore_defaults`: losing these to a button meant for
    #: the radio effect would be a bad trade.
    _PROTECTED = {
        ("llm", "openrouter", "api_key"),
        ("llm", "local", "api_key"),
        ("elevenlabs", "api_key"),
        ("elevenlabs", "tts_voice_id"),
        ("elevenlabs", "tts_voice_name"),
        # Found by ear over many previews; a reset must not cost that.
        ("elevenlabs", "favourite_voices"),
        # Built the same way, one respelling at a time, by listening to the
        # voice mangle a name and typing what it should say instead.
        ("elevenlabs", "pronunciation"),
        ("push_to_button",),
    }

    def restore_defaults(self, section: str) -> Dict[str, Any]:
        """Reset one section to its shipped values.

        Per section rather than all at once. The setting that actually needs
        this is voice delivery — Style at maximum with Similarity at zero ran
        a whole race with no way back but editing JSON — and a single button
        that also wiped the API keys and the wheel binding would be a trap.

        API keys, the chosen voice, the favourites, the pronunciation table
        and the push-to-talk binding survive whatever is reset — everything
        typed or found by ear rather than shipped.
        """
        defaults = copy.deepcopy(_DEFAULT_CONFIG.get(section))
        if defaults is None:
            raise KeyError(f"No such settings section: {section}")

        with self._lock:
            current = self._data.get(section, {})
            if isinstance(defaults, dict) and isinstance(current, dict):
                for key in list(defaults):
                    if (section, key) in self._PROTECTED or (section,) in self._PROTECTED:
                        defaults[key] = current.get(key, defaults[key])
                    elif section == "llm" and isinstance(defaults[key], dict):
                        # Provider blocks hold keys; keep those and reset the rest.
                        for sub in list(defaults[key]):
                            if (section, key, sub) in self._PROTECTED:
                                defaults[key][sub] = current.get(key, {}).get(
                                    sub, defaults[key][sub])
                # Assigning `defaults` wholesale discarded any key this build
                # does not know about, which is the contract `_deep_merge`
                # goes out of its way to keep: a config written by a newer
                # build must survive being read — and now reset — by an older
                # one. Known keys still go back to shipped values.
                defaults = _deep_merge(current, defaults)
            self._data[section] = defaults
            self._dirty = True
        return copy.deepcopy(defaults)

    def restore_all_defaults(self) -> list:
        """Reset every settings section, keeping what you cannot retype.

        API keys, the chosen voice, the favourites, the pronunciation table
        and the push-to-talk binding survive, for the same reason they
        survive a per-section reset: nobody wanting the radio effect back
        also wants to re-pair their wheel or retype their respellings.
        """
        done = []
        for section, value in _DEFAULT_CONFIG.items():
            if not isinstance(value, dict):
                continue
            try:
                self.restore_defaults(section)
                done.append(section)
            except KeyError:
                continue
        return done

    def radio_effect_settings(self) -> Dict[str, Any]:
        """Radio-effect values, ready to pass to `apply_radio_effect`.

        The preview and the playback path each read this section with their
        own fallbacks, and the fallbacks disagreed — 0.85 against 0.75 for
        intensity — so a config missing the key made the tuning preview sound
        unlike the answer it was tuning. A missing key now falls back to what
        ships, from the one place that knows what ships.
        """
        settings = self._data.get("radio_effect", {}) or {}
        defaults = _DEFAULT_CONFIG["radio_effect"]

        def value(key: str) -> Any:
            return settings.get(key, defaults[key])

        return {
            "intensity": float(value("intensity")),
            "low_cut": float(value("low_cut")),
            "high_cut": float(value("high_cut")),
            "noise": float(value("noise")),
            "compression": float(value("compression")),
            "squelch": bool(value("squelch")),
        }

    def speech_cache(self):
        """The shared on-disk cache of synthesized speech.

        Lives beside config.json: the phrases worth caching — refusals above
        all — repeat across sessions, not just within one.
        """
        from .tts_cache import shared_cache

        el = self._data["elevenlabs"]
        return shared_cache(
            Path(self.path).parent / "speech-cache",
            int(el.get("cache_mb", 128)),
            bool(el.get("cache_speech", True)),
        )

    def elevenlabs_key(self) -> str:
        """The ElevenLabs key, falling back to ELEVENLABS_API_KEY.

        One account serves both speech-to-text and text-to-speech, so there
        is nothing to select between. It used to take a `kind` it documented
        as ignored; every caller passed nothing.
        """
        key = self._data["elevenlabs"].get("api_key", "")
        return key or os.environ.get("ELEVENLABS_API_KEY", "")


# Backwards-compatible alias — older modules imported `Config`.
Config = AppConfig
