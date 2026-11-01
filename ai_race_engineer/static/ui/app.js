// AIRE — settings panel + Radio Log.
const { createApp } = Vue;

// Small fetch wrapper: throws with the server's error detail attached.
async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || body.error || detail;
    } catch (e) { /* non-JSON error body */ }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

// A failed connection test carries the reason, which takes longer to read
// than "Settings saved".
const TEST_FAILURE_MS = 9000;

createApp({
  data() {
    return {
      // UI state
      showAbout: false,
      loading: true,
      saving: false,
      asking: false,
      testingLLM: false,
      loadingModels: false,
      version: '0.2.0',

      // Server-backed config. Replaced wholesale by loadConfig().
      config: null,

      // Reference data
      status: {},
      telemetry: null,
      telemetryAgeMs: null,
      // Radio Log: entries oldest first, merged in from /api/log?since_rev=.
      logEntries: [],
      logRev: 0,
      logQuery: '',
      logFilter: 'all',
      logOpen: {},              // id -> details expanded
      noteEditing: null,
      noteText: '',
      traceAvailable: false,
      exportOpen: false,
      exportAireLog: true,
      exportTrace: false,
      exporting: false,
      exportResult: null,
      clearOpen: false,
      clearChoice: 'all',
      clearing: false,
      inputDevices: [],
      outputDevices: [],
      duckingDevices: [],
      voices: [],
      voiceFavesOnly: false,
      voicesOpen: false,
      voiceFilter: '',
      voiceGender: 'all',
      cacheStats: null,
      // Scored against the failure modes this app actually hits: inventing
      // corner names, doing arithmetic in its head, ignoring the tools,
      // rambling past one sentence. All of these passed every probe; Haiku
      // 5.5 was re-checked over five runs, and Haiku 4.5 is clean only since
      // the fix for a model falling silent after its own tool call.
      recommendedModels: [
        'anthropic/claude-haiku-5.5',
        'anthropic/claude-haiku-4.5',
        'google/gemini-2.5-flash',
      ],
      loadingVoices: false,
      voiceTier: '',
      voiceUsable: 0,
      voiceBlocked: 0,
      models: [],
      modelListOpen: false,
      duckListOpen: false,
      // "auto" follows the OS; the other two are an explicit override.
      theme: localStorage.getItem('aire-theme') || 'auto',
      loadingDevices: false,
      controllers: [],
      binding: false,

      // Ask box
      question: '',
      speakAnswer: true,
      testingElevenLabs: false,
      previewing: false,

      // What was last loaded from or saved to the server, to tell an edit
      // from the saved state. Set by loadConfig() and syncSaved().
      savedJson: '',

      // Driver profiles
      profiles: [],
      activeProfile: '',
      profileMenuOpen: false,
      showProfiles: false,
      profileColors: [],
      newProfileName: '',
      newProfileColor: 'orange',
      editing: null,          // { id, name, initials, color } while editing a badge
      switchingProfile: false,
      switchingTo: null,

      // The open confirmation, if any; see confirmDialog().
      dialog: null,

      tab: 'home',
      tabs: [
        { id: 'home',     label: 'Home',     icon: 'house',
          blurb: 'Live state, quick questions and recent radio.' },
        { id: 'race',     label: 'Race',     icon: 'flag',
          blurb: 'Telemetry source, units and pit commands.' },
        { id: 'engineer', label: 'Engineer', icon: 'headset',
          blurb: 'The model behind the engineer, and how it talks to you.' },
        { id: 'voice',    label: 'Voice',    icon: 'audio-lines',
          blurb: 'The engineer’s voice, its delivery and how it says names.' },
        { id: 'audio',    label: 'Audio',    icon: 'volume-2',
          blurb: 'Devices, volume, the radio effect and ducking.' },
        { id: 'input',    label: 'Input',    icon: 'keyboard',
          blurb: 'Push-to-talk: the button, and how it records.' },
        { id: 'app',      label: 'App',      icon: 'settings',
          blurb: 'Appearance, window, speech cache and defaults.' },
        { id: 'log',      label: 'Radio Log', icon: 'scroll-text',
          blurb: 'Every question, answer and warning. Rate answers, then export them for a bug report.' },
      ],

      bindingKey: false,
      pollHandle: null,
      telemetryHandle: null,
    };
  },

  computed: {

    // The switch reads "skip confirmation", so on is the risky choice, like
    // the pit-commands switch above it. The stored setting stays
    // confirm_pit_stop_commands (true = confirm), so no config needs moving.
    skipPitConfirm: {
      get() { return this.config?.pit_commands?.confirm_pit_stop_commands === false; },
      set(skip) { this.config.pit_commands.confirm_pit_stop_commands = !skip; },
    },

    currentTab() {
      return this.tabs.find((t) => t.id === this.tab) || this.tabs[0];
    },

    dirty() {
      return !!this.config && !!this.savedJson
        && JSON.stringify(this.config) !== this.savedJson;
    },

    // Settings changed since the last save, counted per field.
    unsavedCount() {
      if (!this.dirty) return 0;
      const flat = (obj, prefix = '', out = {}) => {
        for (const [k, v] of Object.entries(obj || {})) {
          if (v && typeof v === 'object' && !Array.isArray(v)) flat(v, `${prefix}${k}.`, out);
          else out[prefix + k] = JSON.stringify(v);
        }
        return out;
      };
      const now = flat(this.config);
      const saved = flat(JSON.parse(this.savedJson));
      const keys = new Set([...Object.keys(now), ...Object.keys(saved)]);
      return [...keys].filter((k) => now[k] !== saved[k]).length;
    },

    recentRadio() {
      return this.logEntries.filter((e) => e.kind === 'exchange').slice(-5).reverse();
    },

    // The chips above the log, each with how many entries it would show.
    logFilters() {
      const all = this.logEntries;
      const count = (test) => all.filter(test).length;
      return [
        { id: 'all', label: 'All', count: all.length },
        { id: 'voice', label: 'Voice', count: count((e) => e.type === 'voice') },
        { id: 'text', label: 'Typed', count: count((e) => e.type === 'text') },
        { id: 'rated', label: 'Rated', count: count((e) => this.rating(e) || e.feedback?.note) },
        { id: 'problems', label: 'Problems', count: count((e) => this.isProblem(e)) },
      ];
    },

    // Newest first, narrowed by the chip and the search box.
    filteredLog() {
      const q = this.logQuery.trim().toLowerCase();
      const keep = {
        all: () => true,
        voice: (e) => e.type === 'voice',
        text: (e) => e.type === 'text',
        rated: (e) => this.rating(e) || e.feedback?.note,
        problems: (e) => this.isProblem(e),
      }[this.logFilter] || (() => true);
      return this.logEntries.filter((e) => {
        if (!keep(e)) return false;
        if (!q) return true;
        return [e.question, e.answer, e.message, e.source, e.feedback?.note, e.route]
          .some((f) => (f || '').toLowerCase().includes(q));
      }).reverse();
    },

    // Every choice, always, with what it would remove. Counted here from the
    // entries the panel holds, which are the entries the log holds.
    clearChoices() {
      const now = Date.now();
      const older = (hours) => this.logEntries
        .filter((e) => now - new Date(e.timestamp).getTime() > hours * 36e5).length;
      return [
        { key: '1', hours: 1, label: 'Older than an hour', count: older(1) },
        { key: '24', hours: 24, label: 'Older than a day', count: older(24) },
        { key: '168', hours: 168, label: 'Older than a week', count: older(168) },
        { key: '720', hours: 720, label: 'Older than 30 days', count: older(720) },
        { key: 'all', hours: null, label: 'Everything', count: this.logEntries.length },
      ];
    },

    clearCount() {
      return this.clearChoices.find((c) => c.key === this.clearChoice)?.count || 0;
    },

    logDays() {
      const days = [];
      const today = new Date().toDateString();
      const yesterday = new Date(Date.now() - 864e5).toDateString();
      for (const e of this.filteredLog) {
        const d = new Date(e.timestamp);
        const key = d.toDateString();
        let day = days[days.length - 1];
        if (!day || day.key !== key) {
          const label = key === today ? 'Today' : key === yesterday ? 'Yesterday'
            : d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' });
          day = { key, label, entries: [] };
          days.push(day);
        }
        day.entries.push(e);
      }
      return days;
    },

    activeProfileName() {
      return this.profileName(this.activeProfile) || 'Default';
    },

    activeBadge() {
      const found = this.profiles.find((p) => p.id === this.activeProfile);
      return found || { initials: this.derivedInitials(this.activeProfileName), color: 'orange' };
    },

    editPreview() {
      if (!this.editing) return '';
      return this.cleanInitials(this.editing.initials) || this.derivedInitials(this.editing.name);
    },

    // Flags voice settings sitting outside ElevenLabs' documented ranges.
    // Artefacts — a held "sssss", a swallowed word — nearly always trace
    // back to these rather than to the machine.
    // Typing narrows the list; a long process list is unusable otherwise.
    filteredDuckProcesses() {
      const q = (this.config?.ducking?.process_name || '').toLowerCase().trim();
      const all = this.duckingDevices || [];
      if (!q) return all.slice(0, 40);
      return all.filter((d) => d.name.toLowerCase().includes(q)).slice(0, 40);
    },

    // A voice library can run to dozens of entries once you add from
    // ElevenLabs; scrolling for one is not a picker.
    favouriteIds() {
      return (this.config?.elevenlabs?.favourite_voices) || [];
    },

    filteredVoices() {
      const q = (this.voiceFilter || '').toLowerCase().trim();
      const g = this.voiceGender;
      const faves = this.favouriteIds;
      const matched = this.voices.filter((v) => {
        if (this.voiceFavesOnly && !faves.includes(v.id)) return false;
        if (g !== 'all' && (v.gender || '') !== g) return false;
        if (!q) return true;
        return [v.name, v.description, v.category, v.accent, v.age, v.useCase]
          .some((f) => (f || '').toLowerCase().includes(q));
      });
      // Starred first, otherwise the order the API gave us. Sorting by name
      // as well would move voices around under the cursor on every star.
      return matched.slice().sort(
        (a, b) => (faves.includes(b.id) ? 1 : 0) - (faves.includes(a.id) ? 1 : 0));
    },

    modelIsRecommended() {
      const m = (this.activeProvider && this.activeProvider.model) || '';
      return this.recommendedModels.includes(m.trim());
    },

    // Render the stored binding the way the backend describes it.
    keyLabel() {
      const spec = this.config?.push_to_button?.key || '';
      if (!spec) return 'unset';
      const pretty = { ctrl: 'Ctrl', shift: 'Shift', alt: 'Alt', cmd: 'Cmd',
                       lshift: 'Shift', rshift: 'Shift', lctrl: 'Ctrl',
                       rctrl: 'Ctrl', lalt: 'Alt', ralt: 'Alt',
                       space: 'Space', enter: 'Enter', tab: 'Tab', esc: 'Esc' };
      return spec.split('+').map((p) => {
        const k = p.trim().toLowerCase();
        return pretty[k] || (k.length === 1 ? k.toUpperCase() : k.charAt(0).toUpperCase() + k.slice(1));
      }).join(' + ');
    },

    // What is actually listening right now, as opposed to what the Type
    // picker is showing. The status bar reports the live binding as
    // "keyboard:Shift" or "controller:Wheel button 7".
    liveInputType() {
      const hotkey = this.status?.hotkey || '';
      return hotkey.split(':')[0] || '';
    },

    // Changing Type only edits local state — nothing is bound until Save.
    // The panel immediately renders the wheel section complete with the
    // stored button, so it looks switched while the key is still the thing
    // listening. Push-to-talk that reads as live and is not is the one bit
    // of this panel that must never be ambiguous.
    inputTypeNotApplied() {
      const picked = this.config?.push_to_button?.type;
      if (!picked || !this.liveInputType) return false;
      return picked !== this.liveInputType;
    },

    voiceWarnings() {
      const v = this.config && this.config.elevenlabs && this.config.elevenlabs.voice_settings;
      if (!v) return [];
      const out = [];
      if (v.stability < 0.3) out.push('Stability under 0.3 — unstable.');
      if (v.speed > 1.1) out.push('Speed over 1.1 — slurring.');
      if (v.speed < 0.9) out.push('Speed under 0.9 — drags.');
      if (v.similarity_boost < 0.5) out.push('Similarity under 0.5 — the voice drifts.');
      // Style is the one that actually mangles the audio, and ElevenLabs
      // says to leave it at zero. Calling it "adds latency" undersold it
      // badly enough that it sat at maximum through a live race.
      if (v.style >= 0.5) out.push('Style is high — this is the usual cause of artefacts. Set it to 0.');
      else if (v.style > 0) out.push('Style above 0 — a known source of artefacts.');
      return out;
    },
    // The settings block for whichever provider is currently selected.
    activeProvider() {
      if (!this.config) return null;
      return this.config.llm[this.config.llm.provider];
    },
    providerLabel() {
      return this.config && this.config.llm.provider === 'openrouter'
        ? 'OpenRouter' : 'Local (Ollama / LM Studio)';
    },
    apiKeyPlaceholder() {
      if (!this.activeProvider) return '';
      return this.activeProvider.api_key_set
        ? 'Saved — leave blank to keep' : 'sk-or-v1-…';
    },
    // The status bar. Everything here reads `status` — what is running now —
    // and never config, which may hold an unsaved change: a bar built from
    // config once said "no-hotkey" next to a key that worked.
    engineState() {
      const s = this.status;
      if (!s.running) {
        return s.error
          ? { label: 'Stopped', level: 'bad', pulse: false, title: s.error }
          : { label: 'Stopped', level: 'idle', pulse: false, title: 'The engineer is not running.' };
      }
      const states = {
        listening: { label: 'Listening', level: 'ok', pulse: false, title: 'Waiting for push-to-talk.' },
        recording: { label: 'Recording', level: 'bad', pulse: true, title: 'Your mic is open.' },
        thinking: { label: 'Thinking', level: 'setup', pulse: true, title: 'Working out the answer.' },
        speaking: { label: 'Speaking', level: 'info', pulse: false, title: 'The engineer is on the radio.' },
      };
      return states[s.state] || states.listening;
    },
    statusChecks() {
      const s = this.status;
      const checks = [];

      const model = String(s.llm_model || '');
      checks.push(s.llm_ready
        ? { id: 'model', text: model.split('/').pop() || 'Model', level: 'ok',
            title: `${s.llm_provider} · ${model}`, tab: 'engineer' }
        : { id: 'model', text: 'Add an OpenRouter key', level: 'setup',
            title: 'The engineer needs an LLM key to answer.', tab: 'engineer' });

      const why = s.telemetry_error ? ` ${s.telemetry_error}` : '';
      if (s.telemetry_source === 'simulated') {
        checks.push({ id: 'telemetry', text: 'Simulated telemetry', level: 'idle', tab: 'race',
          title: (s.telemetry_requested === 'simulated'
            ? 'Simulated telemetry selected.'
            : 'iRacing was not found, so simulated telemetry is running.') + why });
      } else if (s.telemetry_connected) {
        checks.push({ id: 'telemetry', text: 'iRacing connected', level: 'ok', tab: 'race',
          title: 'Live iRacing telemetry.' });
      } else {
        checks.push({ id: 'telemetry', text: 'Waiting for iRacing', level: 'setup', tab: 'race',
          title: 'Start iRacing and get in a session.' + why });
      }

      if (s.stt_configured && s.tts_configured) {
        checks.push({ id: 'voice', text: 'Voice ready', level: 'ok', tab: 'voice',
          title: 'Speech recognition and the engineer’s voice are set up.' });
      } else if (!s.stt_configured && !s.tts_configured) {
        checks.push({ id: 'voice', text: 'Add an ElevenLabs key', level: 'setup', tab: 'voice',
          title: 'Needed to hear you and to speak.' });
      } else {
        checks.push({ id: 'voice', text: 'Voice partly set up', level: 'setup', tab: 'voice',
          title: s.stt_configured ? 'The engineer’s voice is not set up.'
                                  : 'Speech recognition is not set up.' });
      }

      const hotkey = String(s.hotkey || '');
      const [kind, ...rest] = hotkey.split(':');
      const binding = rest.join(':');
      if (s.hotkey_available === false) {
        checks.push({ id: 'ptt', text: 'Push-to-talk not connected', level: 'bad', tab: 'input',
          title: `${binding || hotkey} — device not detected.` });
      } else if (!binding || binding === 'unbound') {
        checks.push({ id: 'ptt', text: 'Bind push-to-talk', level: 'setup', tab: 'input',
          title: 'Nothing is bound to push-to-talk yet.' });
      } else {
        // "<device> button <n>": the button is what you press, and a long
        // device name cut off by the pill hid exactly that part.
        const wheel = kind === 'controller' && binding.match(/^(.*) button (\d+)$/);
        checks.push({ id: 'ptt', level: 'ok', tab: 'input',
          text: `Push-to-talk: ${wheel ? `button ${wheel[2]}` : binding}`,
          title: `${kind === 'controller' ? 'Wheel or button box' : 'Keyboard'}: ${binding}` });
      }

      // Always shown, so whether pit commands are on is never a question:
      // green on, grey off, amber only while one waits for confirm.
      if (!s.pit_commands_enabled) {
        checks.push({ id: 'pit', text: 'Pit commands off', level: 'idle', tab: 'race',
          title: 'Turn them on under Race → Pit Commands.' });
      } else if (s.pit_command_pending) {
        checks.push({ id: 'pit', text: 'Pit command waiting for confirm', level: 'setup',
          pulse: true, tab: 'race', title: 'Say confirm to set it, or cancel.' });
      } else {
        checks.push({ id: 'pit', text: 'Pit commands on', level: 'ok', tab: 'race',
          title: 'Pit commands can change your next stop.' });
      }

      // What each tell-tale shows. Push-to-talk shows the device it is
      // bound to, so a wheel binding reads as a wheel at a glance.
      const icons = {
        model: 'brain',
        telemetry: 'gauge',
        voice: 'audio-lines',
        ptt: kind === 'controller' ? 'gamepad-2' : 'keyboard',
        pit: 'wrench',
      };
      return checks.map((check) => ({ ...check, icon: icons[check.id] }));
    },
    // OpenRouter returns hundreds of models; filter against what is typed
    // and cap the list so the dropdown stays usable.
    filteredModels() {
      if (!this.models.length) return [];
      const typed = (this.activeProvider && this.activeProvider.model || '').toLowerCase().trim();
      const matches = typed
        ? this.models.filter(m => m.id.toLowerCase().includes(typed)
                               || (m.name || '').toLowerCase().includes(typed))
        : this.models;
      return matches.slice(0, 60);
    },
    boundDescription() {
      const p = this.config && this.config.push_to_button;
      if (!p || p.button < 0) return 'Nothing bound yet.';
      return `${p.device_name || p.device_guid || 'device'} — button ${p.button}`;
    },
  },

  methods: {
    docs(anchor) {
      // The panel explains what a setting does; the README explains why it
      // is there and how to tell whether it worked. Hints that tried to do
      // both ran to sixty words against a twenty-word median, which is a
      // wall of text next to the control you are trying to use.
      //
      // Points at the repo rather than at a bundled copy so the link is
      // right for the build the reader has, not the one they downloaded.
      const base = 'https://github.com/Bartket/AIRE';
      return anchor ? `${base}#${anchor}` : base;
    },
    applyVoicePreset(name) {
      // Three points on one axis: consistency versus expression. Every
      // preset keeps style at 0, which ElevenLabs recommends unconditionally.
      const presets = {
        stable:     { stability: 0.75, similarity_boost: 0.85, style: 0, speed: 1.0 },
        balanced:   { stability: 0.55, similarity_boost: 0.75, style: 0, speed: 1.05 },
        expressive: { stability: 0.40, similarity_boost: 0.70, style: 0, speed: 1.05 },
      };
      const preset = presets[name];
      if (!preset || !this.config.elevenlabs) return;
      Object.assign(this.config.elevenlabs.voice_settings, preset);
    },
    async init() {
      this.applyTabFromUrl();
      window.addEventListener('hashchange', () => this.applyTabFromUrl());
      await this.loadConfig();
      await Promise.all([
        this.loadProfiles(),
        this.refreshStatus(),
        this.loadDevices(),
        this.refreshCache(),
        this.loadLog(),
        this.loadControllers(),
        this.refreshTelemetry(),
      ]);
      this.loading = false;
      // Telemetry moves fast, status and the log do not — poll them apart
      // so the readout stays live without hammering the rest.
      this.telemetryHandle = setInterval(() => {
        if (this.tab === 'home') this.refreshTelemetry();
      }, 400);
      this.pollHandle = setInterval(() => {
        this.refreshStatus();
        this.loadLog();
      }, 2000);
    },

    async loadConfig() {
      try {
        this.config = await api('/api/config');
        this.savedJson = JSON.stringify(this.config);
      } catch (error) {
        this.notify(`Failed to load config: ${error.message}`, 'error');
      }
    },

    // Record one setting as saved, after an action that saved it on its
    // own. Without this a starred voice or a bound key would show in the
    // unsaved bar, and Discard would put back what was just saved.
    syncSaved(section, ...keys) {
      if (!this.savedJson) return;
      const saved = JSON.parse(this.savedJson);
      for (const key of keys) {
        saved[section][key] = JSON.parse(JSON.stringify(this.config[section][key]));
      }
      this.savedJson = JSON.stringify(saved);
    },

    // Take one section from the server after the server changed it — a
    // bind or a reset — leaving unsaved edits elsewhere alone. Reloading
    // the whole config threw those away without a word.
    async reloadSection(section) {
      const fresh = await api('/api/config');
      this.config[section] = fresh[section];
      const saved = JSON.parse(this.savedJson || '{}');
      saved[section] = fresh[section];
      this.savedJson = JSON.stringify(saved);
    },

    async discardChanges() {
      await this.loadConfig();
    },

    // ── Driver profiles ────────────────────────────────────────────

    async loadProfiles() {
      try {
        this.takeProfiles(await api('/api/profiles'));
      } catch (error) {
        this.notify(`Could not list profiles: ${error.message}`, 'error');
      }
    },

    takeProfiles(result) {
      this.profiles = result.profiles;
      this.activeProfile = result.active;
      if (result.colors) this.profileColors = result.colors;
    },

    profileName(id) {
      const found = this.profiles.find((p) => p.id === id);
      return found ? found.name : '';
    },

    // Mirrors profiles.clean_initials / initials_for, for the live preview
    // only; what is shown once saved is what the server stored.
    cleanInitials(text) {
      return Array.from(String(text || '')).filter((c) => /[\p{L}\p{N}]/u.test(c))
        .slice(0, 2).join('').toUpperCase();
    },

    derivedInitials(name) {
      const words = String(name || '').split(/\s+/).map((w) => this.cleanInitials(w)).filter(Boolean);
      if (!words.length) return '?';
      return words.length > 1 ? words[0][0] + words[1][0] : words[0].slice(0, 2);
    },

    // The first colour no profile has, as the server would pick.
    freeColor() {
      const used = new Set(this.profiles.map((p) => p.color));
      return this.profileColors.find((c) => !used.has(c)) || this.profileColors[0] || 'orange';
    },

    toggleProfileMenu() {
      if (this.profileMenuOpen) {
        this.closeProfileMenu(false);
        return;
      }
      this.profileMenuOpen = true;
      // Focus the current driver, so arrow keys start from where you are.
      this.$nextTick(() => {
        const menu = this.$refs.profileMenu;
        (menu?.querySelector('.profile-menu-item.active') || menu?.querySelector('button'))?.focus();
      });
    },

    closeProfileMenu(refocus) {
      if (!this.profileMenuOpen) return;
      this.profileMenuOpen = false;
      if (refocus) this.$nextTick(() => this.$refs.profileButton?.focus());
    },

    moveMenuFocus(step) {
      const items = Array.from(this.$refs.profileMenu?.querySelectorAll('button:not(:disabled)') || []);
      if (!items.length) return;
      const at = items.indexOf(document.activeElement);
      items[(at + step + items.length) % items.length].focus();
    },

    openProfiles(focusNew) {
      this.profileMenuOpen = false;
      this.editing = null;
      this.newProfileColor = this.freeColor();
      this.showProfiles = true;
      if (focusNew) this.$nextTick(() => this.$refs.newProfileInput?.focus());
    },

    async requestSwitch(id) {
      this.profileMenuOpen = false;
      if (id === this.activeProfile || this.switchingProfile) return;
      if (this.dirty) {
        const choice = await this.confirmDialog({
          title: 'Unsaved changes',
          message: `Save your changes to ${this.activeProfileName} before switching to ${this.profileName(id)}?`,
          actions: [
            { label: 'Discard', value: 'discard' },
            { label: 'Save and switch', value: 'save', kind: 'primary' },
          ],
        });
        if (!choice) return;
        if (choice === 'save') {
          await this.saveConfig();
          if (this.dirty) return;   // the save failed and said so; stay put
        }
      }
      await this.switchProfile(id);
    },

    // One switch at a time. Clicking through drivers quickly started a switch
    // per click, each ending in its own toast, and the last to finish won.
    async switchProfile(id) {
      if (this.switchingProfile) return;
      this.switchingProfile = true;
      this.switchingTo = id;
      try {
        this.takeProfiles(await api(`/api/profiles/${encodeURIComponent(id)}/activate`,
                                    { method: 'POST' }));
        await this.loadConfig();
          await this.refreshStatus();
        this.notify(`Switched to ${this.activeProfileName}`);
      } catch (error) {
        this.notify(error.message, 'error');
      } finally {
        this.switchingProfile = false;
        this.switchingTo = null;
      }
    },

    async createProfile() {
      const name = this.newProfileName.trim();
      if (!name) return;
      try {
        this.takeProfiles(await api('/api/profiles', {
          method: 'POST', body: JSON.stringify({ name, color: this.newProfileColor }),
        }));
        this.newProfileName = '';
        this.newProfileColor = this.freeColor();
        this.notify(`Added ${name}`);
      } catch (error) {
        this.notify(error.message, 'error');
      }
    },

    startEdit(profile) {
      // Initials typed by hand are shown; derived ones stay a placeholder,
      // so renaming the profile still updates them.
      const derived = this.derivedInitials(profile.name);
      this.editing = {
        id: profile.id,
        name: profile.name,
        initials: profile.initials === derived ? '' : profile.initials,
        color: profile.color,
      };
    },

    async saveEdit() {
      const edit = this.editing;
      if (!edit || !edit.name.trim()) return;
      try {
        this.takeProfiles(await api(`/api/profiles/${encodeURIComponent(edit.id)}/edit`, {
          method: 'POST',
          body: JSON.stringify({ name: edit.name, initials: edit.initials, color: edit.color }),
        }));
        this.editing = null;
      } catch (error) {
        this.notify(error.message, 'error');
      }
    },

    async deleteProfile(profile) {
      const ok = await this.confirmDialog({
        title: `Delete ${profile.name}?`,
        message: 'Their voice, persona and other driver settings are removed from this PC.',
        actions: [{ label: 'Delete', value: true, kind: 'danger' }],
      });
      if (!ok) return;
      try {
        this.takeProfiles(await api(`/api/profiles/${encodeURIComponent(profile.id)}/delete`,
                                    { method: 'POST' }));
        this.notify(`Deleted ${profile.name}`);
      } catch (error) {
        this.notify(error.message, 'error');
      }
    },

    // An in-panel replacement for the browser's confirm box. That one is drawn by the
    // webview, titled with the server's address — "127.0.0.1:9420 says" —
    // which reads like a web page, not the app. Resolves to the chosen
    // action's value, or null for Cancel, Esc or a click outside.
    confirmDialog({ title, message, actions }) {
      if (this._dialogResolve) this._dialogResolve(null);
      this.dialog = { title, message, actions };
      this.$nextTick(() => this.$refs.dialogCancel?.focus());
      return new Promise((resolve) => { this._dialogResolve = resolve; });
    },

    closeDialog(value) {
      const resolve = this._dialogResolve;
      this._dialogResolve = null;
      this.dialog = null;
      if (resolve) resolve(value);
    },

    async saveConfig() {
      this.saving = true;
      try {
        await api('/api/config', {
          method: 'POST',
          body: JSON.stringify(this.config),
        });
        await this.loadConfig();
        await this.refreshStatus();
        this.notify('Settings saved');
      } catch (error) {
        this.notify(`Failed to save: ${error.message}`, 'error');
      } finally {
        this.saving = false;
      }
    },

    async refreshStatus() {
      try {
        this.status = await api('/api/status');
      } catch (error) { /* the panel stays usable if polling blips */ }
      // Switched from somewhere else (a second window). Follow it, unless
      // that would throw away edits — then the bar still offers the save.
      const live = this.status.profile;
      if (live && this.activeProfile && live !== this.activeProfile
          && !this.switchingProfile) {
        await this.loadProfiles();
        if (!this.dirty) {
          await this.loadConfig();
            }
      }
    },

    async refreshTelemetry() {
      try {
        const result = await api('/api/telemetry');
        this.telemetry = result.telemetry;
        this.telemetryAgeMs = result.age_ms;
      } catch (error) { /* ignore transient blips */ }
    },

    async loadDevices() {
      this.loadingDevices = true;
      try {
      const [input, output, ducking] = await Promise.all([
        api('/api/devices/input').catch(() => []),
        api('/api/devices/output').catch(() => []),
        api('/api/ducking_devices').catch(() => []),
      ]);
      this.inputDevices = input;
      this.outputDevices = output;
      this.duckingDevices = ducking;
      } finally {
        this.loadingDevices = false;
      }
    },

    // Only what changed since the last look: the log runs to a thousand
    // entries, and the whole of it every two seconds was most of the poll.
    async loadLog() {
      try {
        const result = await api(`/api/log?since_rev=${this.logRev}`);
        if (result.reset) this.logEntries = [];
        if (result.entries.length) {
          const at = new Map(this.logEntries.map((e, i) => [e.id, i]));
          const merged = this.logEntries.slice();
          for (const entry of result.entries) {
            if (at.has(entry.id)) merged[at.get(entry.id)] = entry;
            else merged.push(entry);
          }
          // The log drops its oldest past "Entries kept"; so does this copy.
          this.logEntries = merged.slice(Math.max(0, merged.length - result.count));
        } else if (this.logEntries.length > result.count) {
          this.logEntries = this.logEntries.slice(this.logEntries.length - result.count);
        }
        this.logRev = result.rev;
        this.traceAvailable = result.trace_available;
      } catch (error) { /* the panel stays usable if polling blips */ }
    },

    rating(entry) {
      return entry.feedback?.rating || null;
    },

    isProblem(entry) {
      if (entry.kind === 'event') return entry.level === 'warning' || entry.level === 'error'
        || entry.level === 'critical';
      return !!(entry.failed || entry.route === 'stt_guard' || this.rating(entry) === 'down');
    },

    routeLabel(route) {
      if (route === 'deterministic') return 'telemetry';
      if (route === 'model') return 'model';
      if (route === 'stt_guard') return 'say again';
      if (route?.startsWith('tool:')) return `calculator: ${route.slice(5).replace(/_/g, ' ')}`;
      if (route?.startsWith('pit_command:')) return `pit: ${route.slice(12)}`;
      return route;
    },

    seconds(entry) {
      const total = entry.timings?.total;
      if (typeof total !== 'number') return '';
      return total < 1000 ? `${Math.round(total)} ms` : `${(total / 1000).toFixed(1)} s`;
    },

    detailRows(entry) {
      const rows = [];
      const t = entry.timings || {};
      const stages = Object.entries(t).filter(([k]) => k !== 'total')
        .map(([k, v]) => `${k.replace(/_/g, ' ')} ${Math.round(v)} ms`);
      if (stages.length) rows.push(['Timings', stages.join(' · ')]);
      if (typeof t.total === 'number') rows.push(['Total', `${Math.round(t.total)} ms`]);
      const tr = entry.transcription;
      if (tr) {
        const bits = [];
        if (typeof tr.mean_word_logprob === 'number') bits.push(`word confidence ${tr.mean_word_logprob.toFixed(2)}`);
        if (typeof tr.language_probability === 'number') bits.push(`${tr.language_code || 'language'} ${Math.round(tr.language_probability * 100)}%`);
        if (bits.length) rows.push(['Heard', bits.join(' · ')]);
      }
      if (entry.route) rows.push(['Answered by', entry.route]);
      if (entry.model) rows.push(['Model', entry.model]);
      if (entry.usage) rows.push(['Tokens', Object.entries(entry.usage).map(([k, v]) => `${k} ${v}`).join(' · ')]);
      if (typeof entry.speech_cached === 'boolean') rows.push(['Speech', entry.speech_cached ? 'replayed from cache' : 'synthesized']);
      if (entry.session_type || entry.session_kind) rows.push(['Session', `${entry.session_type || '(none)'} → ${entry.session_kind || '?'}`]);
      if (entry.profile) rows.push(['Profile', this.profileName(entry.profile) || entry.profile]);
      return rows;
    },

    toggleLogOpen(id) {
      this.logOpen = { ...this.logOpen, [id]: !this.logOpen[id] };
    },

    copyEntry(entry) {
      const lines = [`${new Date(entry.timestamp).toLocaleString()}  ${entry.type}`,
        `You: ${entry.question}`, `Engineer: ${entry.answer}`,
        ...this.detailRows(entry).map(([k, v]) => `${k}: ${v}`)];
      if (entry.feedback?.note) lines.push(`Note: ${entry.feedback.note}`);
      this.copyText(lines.join('\n'));
    },

    // Optimistic: the row changes at once, and goes back if the save fails.
    async sendFeedback(entry, rating, note) {
      const before = entry.feedback;
      entry.feedback = (rating || note) ? { rating, note } : null;
      try {
        const result = await api(`/api/log/${entry.id}/feedback`, {
          method: 'POST', body: JSON.stringify({ rating, note }),
        });
        Object.assign(entry, result.entry);
      } catch (error) {
        entry.feedback = before;
        this.notify(`Could not save the rating: ${error.message}`, 'error');
      }
    },

    rate(entry, rating) {
      const next = this.rating(entry) === rating ? null : rating;
      this.sendFeedback(entry, next, entry.feedback?.note || '');
    },

    editNote(entry) {
      this.noteEditing = entry.id;
      this.noteText = entry.feedback?.note || '';
      this.$nextTick(() => {
        const input = this.$refs[`note-${entry.id}`];
        (Array.isArray(input) ? input[0] : input)?.focus();
      });
    },

    async saveNote(entry) {
      this.noteEditing = null;
      await this.sendFeedback(entry, this.rating(entry), this.noteText.trim());
    },

    // The rating stays; only the words go.
    async deleteNote(entry) {
      this.noteEditing = null;
      await this.sendFeedback(entry, this.rating(entry), '');
    },

    clearLog() {
      // The narrowest choice that removes something; everything otherwise.
      const first = this.clearChoices.find((c) => c.count);
      this.clearChoice = first ? first.key : 'all';
      this.clearOpen = true;
    },

    async runClear() {
      const choice = this.clearChoices.find((c) => c.key === this.clearChoice);
      if (!choice || !choice.count) return;
      this.clearing = true;
      try {
        const result = await api('/api/log/clear', {
          method: 'POST',
          body: JSON.stringify(choice.hours ? { older_than_hours: choice.hours } : {}),
        });
        this.clearOpen = false;
        this.logEntries = [];
        this.logRev = 0;
        await this.loadLog();
        this.notify(`Cleared ${result.removed} ${result.removed === 1 ? 'entry' : 'entries'}`);
      } catch (error) {
        this.notify(`Could not clear the log: ${error.message}`, 'error');
      } finally {
        this.clearing = false;
      }
    },

    openExport() {
      this.exportResult = null;
      this.exportTrace = false;
      this.exportOpen = true;
    },

    async runExport() {
      this.exporting = true;
      try {
        this.exportResult = await api('/api/log/export', {
          method: 'POST',
          body: JSON.stringify({
            ids: this.filteredLog.map((e) => e.id),
            include_aire_log: this.exportAireLog,
            include_trace: this.exportTrace,
          }),
        });
      } catch (error) {
        this.notify(`Export failed: ${error.message}`, 'error');
      } finally {
        this.exporting = false;
      }
    },

    async revealExport() {
      try {
        const result = await api('/api/log/export/reveal', {
          method: 'POST', body: JSON.stringify({ name: this.exportResult.name }),
        });
        if (!result.ok) this.copyText(result.path);
      } catch (error) {
        this.notify(`Could not open the folder: ${error.message}`, 'error');
      }
    },

    // ── Controller / sim wheel ─────────────────────────────────────

    async loadControllers() {
      try {
        const result = await api('/api/controllers');
        this.controllers = result.devices || [];
        if (!result.available) {
          this.notify(result.error || 'Controller support unavailable', 'error');
        }
      } catch (error) {
        this.notify(`Could not list controllers: ${error.message}`, 'error');
      }
    },

    // Waits for the next physical button press and stores it server-side.
    // Mirrors the wheel binding: the key is captured globally by the
    // backend, so any key or combination works, not a list we chose for you.
    // ElevenLabs names read "Roger - Laid-Back, Casual, Resonant": the part
    // before the dash identifies the voice, the rest describes it. Splitting
    // them stops a long name eating the card and leaves room for the traits.
    voiceName(voice) {
      return (voice.name || '').split(' - ')[0].trim() || voice.name || '';
    },

    voiceMeta(voice) {
      const tail = (voice.name || '').split(' - ').slice(1).join(' - ').trim();
      const bits = [voice.gender, voice.age, voice.accent].filter(Boolean);
      const traits = bits.join(' · ');
      return [traits, tail || voice.description || voice.category || '']
        .filter(Boolean).join(' — ');
    },

    async resetAll() {
      const ok = await this.confirmDialog({
        title: 'Restore all settings?',
        message: 'Every setting goes back to its default. Your API keys, chosen voice, '
          + 'favourites and push-to-talk binding are kept.',
        actions: [{ label: 'Restore', value: true, kind: 'danger' }],
      });
      if (!ok) return;
      try {
        const r = await api('/api/config/reset', { method: 'POST' });
        await this.loadConfig();
        this.notify(`Restored ${r.sections.length} sections to defaults`);
      } catch (error) {
        this.notify(`Could not reset: ${error.message}`, 'error');
      }
    },

    async resetSection(section) {
      try {
        await api(`/api/config/reset/${section}`, { method: 'POST' });
        await this.reloadSection(section);
        this.notify('Restored default settings');
      } catch (error) {
        this.notify(`Could not reset: ${error.message}`, 'error');
      }
    },

    async refreshCache() {
      try {
        this.cacheStats = await api('/api/speech_cache');
      } catch (error) {
        this.cacheStats = null;
      }
    },

    async clearCache() {
      try {
        const r = await api('/api/speech_cache/clear', { method: 'POST' });
        this.notify(`Cleared ${r.removed} cached phrases`);
        await this.refreshCache();
      } catch (error) {
        this.notify(`Could not clear the cache: ${error.message}`, 'error');
      }
    },

    async bindKey() {
      if (this.bindingKey) return;
      this.bindingKey = true;
      try {
        const result = await api('/api/keyboard/bind', {
          method: 'POST',
          body: JSON.stringify({ timeout: 15 }),
        });
        if (result.ok) {
          await this.reloadSection('push_to_button');
          await this.refreshStatus();
          this.notify(`Bound to ${result.label}`);
        } else if (result.cancelled) {
          this.notify('Binding cancelled', 'info');
        } else {
          this.notify(result.error || 'No key detected', 'error');
        }
      } catch (error) {
        this.notify(`Bind failed: ${error.message}`, 'error');
      } finally {
        this.bindingKey = false;
      }
    },

    async bindController() {
      if (this.binding) return;
      this.binding = true;
      try {
        const result = await api('/api/controllers/bind', {
          method: 'POST',
          body: JSON.stringify({ timeout: 15 }),
        });
        if (result.ok) {
          await this.reloadSection('push_to_button');
          await this.refreshStatus();
          this.notify(`Bound to ${result.description}`);
        } else if (result.cancelled) {
          this.notify('Binding cancelled', 'info');
        } else {
          this.notify(result.error || 'No button detected', 'error');
        }
      } catch (error) {
        this.notify(`Bind failed: ${error.message}`, 'error');
      } finally {
        this.binding = false;
      }
    },

    // ── Combobox pickers ───────────────────────────────────────────
    // These replace <datalist>. A native datalist popup is owned by the
    // browser and gets dismissed whenever Vue patches the DOM, so the
    // telemetry poll was closing it a few hundred ms after it opened.

    pickModel(id) {
      this.activeProvider.model = id;
      this.modelListOpen = false;
    },

    selectTab(id) {
      // Tabs differ a lot in height. Landing halfway down a short tab
      // because the previous one was long feels like the page moved.
      this.tab = id;
      window.scrollTo({ top: 0, behavior: 'instant' });
      // Mirror the tab into the URL so a tab can be linked to and reopened.
      // replaceState rather than the hash directly: setting location.hash
      // pushes a history entry, so Back would walk every tab you touched
      // instead of leaving the page.
      try {
        history.replaceState(null, '', `#${id}`);
      } catch (error) {
        /* file:// and some embedded webviews refuse replaceState */
      }
    },

    // Open the tab named in the URL, if it names a real one. Anything else
    // is ignored rather than left showing a blank page.
    applyTabFromUrl() {
      const wanted = (window.location.hash || '').replace(/^#/, '');
      if (wanted && this.tabs.some((t) => t.id === wanted)) this.tab = wanted;
    },

    applyTheme() {
      const root = document.documentElement;
      if (this.theme === 'auto') {
        root.removeAttribute('data-theme');   // let the media query decide
      } else {
        root.setAttribute('data-theme', this.theme);
      }
      localStorage.setItem('aire-theme', this.theme);
    },

    pickDuckProcess(name) {
      this.config.ducking.process_name = name;
      this.duckListOpen = false;
    },

    closeCombos(event) {
      if (!event.target.closest || !event.target.closest('.combo')) {
        this.modelListOpen = false;
        this.duckListOpen = false;
      }
      if (!event.target.closest || !event.target.closest('.profile-switch')) {
        this.profileMenuOpen = false;
      }
    },

    // ── LLM ────────────────────────────────────────────────────────

    // What the provider fields show, saved or not, for the server to try.
    // A blank key stays blank: the server reads that as "the saved key".
    llmTrialBody() {
      return JSON.stringify({ llm: this.config.llm });
    },


    async loadModels() {
      this.loadingModels = true;
      try {
        // Asks the provider being shown, without saving it: browsing a
        // catalogue is not a decision about settings.
        const result = await api('/api/llm/models', { method: 'POST', body: this.llmTrialBody() });
        this.models = result.models;
        this.modelListOpen = true;
        this.notify(`Loaded ${this.models.length} models`);
      } catch (error) {
        this.notify(`Could not list models: ${error.message}`, 'error');
      } finally {
        this.loadingModels = false;
      }
    },

    // Tries the settings on screen and saves nothing. It used to save
    // everything first, so a test was also an unannounced Save.
    async testLLM() {
      this.testingLLM = true;
      try {
        const result = await api('/api/llm/test', { method: 'POST', body: this.llmTrialBody() });
        this.notify(`${result.model} replied: “${result.reply}”`);
      } catch (error) {
        this.notify(`Connection test failed: ${error.message}`, 'error', TEST_FAILURE_MS);
      } finally {
        this.testingLLM = false;
      }
    },

    async testElevenLabs() {
      this.testingElevenLabs = true;
      try {
        const el = this.config.elevenlabs;
        const result = await api('/api/elevenlabs/test', {
          method: 'POST',
          body: JSON.stringify({ elevenlabs: {
            api_key: el.api_key, stt_model: el.stt_model,
            tts_model: el.tts_model, tts_voice_id: el.tts_voice_id,
          } }),
        });
        // One toast: every check passing in a line, or the first failure
        // with its reason — the one that tells you which permission is missing.
        const failed = result.checks.find((c) => !c.ok);
        if (failed) {
          const summary = result.checks.map((c) => `${c.ok ? '✓' : '✗'} ${c.name}`).join(' · ');
          this.notify(`ElevenLabs: ${summary}. ${failed.name}: ${failed.detail}`,
                      'error', TEST_FAILURE_MS);
        } else {
          const heard = result.checks.find((c) => c.name === 'Recognition');
          this.notify(`ElevenLabs works: voices, speech and recognition (${heard.detail}).`);
        }
      } catch (error) {
        this.notify(`Connection test failed: ${error.message}`, 'error', TEST_FAILURE_MS);
      } finally {
        this.testingElevenLabs = false;
      }
    },

    // ── Pipeline ───────────────────────────────────────────────────

    async ask() {
      const text = this.question.trim();
      if (!text || this.asking) return;
      this.asking = true;
      try {
        await api('/api/ask', {
          method: 'POST',
          body: JSON.stringify({ question: text, speak: this.speakAnswer }),
        });
        this.question = '';
        // The answer lands in Recent Radio, just below the box.
        await this.loadLog();
      } catch (error) {
        this.notify(`Ask failed: ${error.message}`, 'error');
      } finally {
        this.asking = false;
      }
    },

    async testBeep(closing) {
      try {
        await api('/api/beep', {
          method: 'POST',
          body: JSON.stringify({
            closing,
            volume: this.config.audio.ptt_beep_volume,
          }),
        });
      } catch (error) {
        this.notify(`Could not play blip: ${error.message}`, 'error');
      }
    },

    // ── Voices ─────────────────────────────────────────────────────

    async loadVoices() {
      this.loadingVoices = true;
      try {
        const result = await api('/api/voices');
        this.voices = result.voices;
        this.voiceTier = result.tier || '';
        this.voiceUsable = result.usable || 0;
        this.voiceBlocked = result.blocked || 0;
        this.voicesOpen = true;
        const suffix = this.voiceBlocked
          ? ` (${this.voiceBlocked} need a paid plan)` : '';
        this.notify(`Loaded ${this.voices.length} voices${suffix}`);
      } catch (error) {
        this.notify(`Could not load voices: ${error.message}`, 'error');
      } finally {
        this.loadingVoices = false;
      }
    },

    isFavourite(voice) {
      return this.favouriteIds.includes(voice.id);
    },

    async toggleFavourite(voice) {
      const el = this.config && this.config.elevenlabs;
      if (!el) return;
      // A config written before this setting existed has no array yet.
      if (!Array.isArray(el.favourite_voices)) el.favourite_voices = [];
      const at = el.favourite_voices.indexOf(voice.id);
      if (at === -1) el.favourite_voices.push(voice.id);
      else el.favourite_voices.splice(at, 1);
      // Save immediately: starring is a decision, and losing it because the
      // window was closed without hitting Save would be the whole point of
      // the feature gone.
      try {
        await api('/api/config', {
          method: 'POST',
          body: JSON.stringify({ elevenlabs: { favourite_voices: el.favourite_voices } }),
        });
        this.syncSaved('elevenlabs', 'favourite_voices');
      } catch (error) {
        this.notify(`Could not save favourites: ${error.message}`, 'error');
      }
    },

    async selectVoice(voice) {
      // Blocked voices fail at synthesis time with a 402, so stop here.
      if (voice.available === false) {
        this.notify(`${voice.name} — ${voice.restriction}`, 'error');
        return;
      }
      try {
        await api('/api/voices/select', {
          method: 'POST',
          body: JSON.stringify({ voiceId: voice.id, voiceName: voice.name }),
        });
        this.config.elevenlabs.tts_voice_id = voice.id;
        this.config.elevenlabs.tts_voice_name = voice.name;
        this.syncSaved('elevenlabs', 'tts_voice_id', 'tts_voice_name');
        // Deliberately leaves the list open: picking a voice is usually the
        // middle of comparing several, and closing it forced a reopen to
        // try the next one. "Hide list" is the only thing that closes it.
        this.notify(`Voice set to ${this.voiceName(voice)}`);
      } catch (error) {
        this.notify(`Could not select voice: ${error.message}`, 'error');
      }
    },

    // Play a sample through (or around) the current radio settings.
    async previewRadio(withEffect = true) {
      if (this.previewing) return;
      this.previewing = true;
      try {
        await this.saveConfig();
        await this.playSample({
          radio: withEffect !== false,
          text: 'Box this lap, box this lap. Fuel is marginal, and the front left is going away.',
        });
      } catch (error) {
        this.notify(`Preview failed: ${error.message}`, 'error');
      } finally {
        this.previewing = false;
      }
    },

    async playSample(payload) {
      const response = await fetch('/api/tts/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        let detail = response.statusText;
        try { detail = (await response.json()).detail || detail; } catch (e) { /* */ }
        throw new Error(detail);
      }
      const blob = await response.blob();
      const audio = new Audio(URL.createObjectURL(blob));
      await audio.play();
    },

    async previewVoice(voice) {
      if (voice.available === false) {
        this.notify(`${voice.name} — ${voice.restriction}`, 'error');
        return;
      }
      try {
        const response = await fetch('/api/tts/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ voiceId: voice.id }),
        });
        if (!response.ok) throw new Error(await response.text());
        const blob = await response.blob();
        const audio = new Audio(URL.createObjectURL(blob));
        audio.play();
      } catch (error) {
        this.notify('Preview failed — check your ElevenLabs key', 'error');
      }
    },

    // ── Helpers ────────────────────────────────────────────────────

    // Toasts stack in one corner. Each used to be positioned on its own at
    // the same spot, so a quick run of them drew on top of each other.
    notify(message, type = 'success', ms = 3200) {
      let stack = document.querySelector('.toasts');
      if (!stack) {
        stack = document.createElement('div');
        stack.className = 'toasts';
        stack.setAttribute('role', 'status');
        document.body.appendChild(stack);
      }
      // success green, error red, info neutral — with an icon as well, so
      // the state is not carried by colour alone.
      const kind = ['success', 'error', 'info'].includes(type) ? type : 'info';
      const className = `notification ${kind}`;
      // The same message again restarts the one showing rather than adding a copy.
      let el = Array.from(stack.children).find(
        (t) => t.textContent === message && t.className === className);
      if (el) {
        clearTimeout(el._timer);
      } else {
        el = document.createElement('div');
        el.className = className;
        const icon = { success: 'check', error: 'triangle-alert', info: 'info' }[kind];
        el.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#i-${icon}"/></svg>`;
        const text = document.createElement('span');
        text.textContent = message;
        el.appendChild(text);
        stack.appendChild(el);
        while (stack.children.length > 3) stack.firstElementChild.remove();
      }
      el._timer = setTimeout(() => {
        el.classList.add('fade-out');
        setTimeout(() => el.remove(), 300);
      }, ms);
    },

    formatTime(timestamp) {
      return new Date(timestamp).toLocaleTimeString([], {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
    },

    round(value, digits = 1) {
      return typeof value === 'number' ? value.toFixed(digits) : value;
    },

    async copyText(text) {
      try {
        await navigator.clipboard.writeText(text);
        this.notify('Copied to clipboard');
      } catch (error) {
        this.notify('Could not copy', 'error');
      }
    },
  },

  mounted() {
    this.applyTheme();
    this.init();
    this._onDocClick = (e) => this.closeCombos(e);
    document.addEventListener('click', this._onDocClick);

    // A <select> keeps focus after you pick from it, and Chromium counts a
    // click on one as keyboard-like — so the focus ring sat around every
    // dropdown you had touched. Drop focus when the change came from a
    // pointer, and leave it alone when it came from the keyboard.
    this._pointerPick = false;
    this._onPointerDown = (e) => {
      this._pointerPick = e.target && e.target.tagName === 'SELECT';
    };
    this._onKeyDown = (e) => {
      this._pointerPick = false;
      // Esc calls off a wheel bind. A key bind needs nothing here: the
      // backend's global listener sees the same Esc and cancels itself.
      if (e.key === 'Escape' && this.binding) {
        api('/api/controllers/bind/cancel', { method: 'POST' }).catch(() => {});
      }
      // Esc closes the dialog on top, wherever focus is. Handled on the
      // dialogs themselves it only worked once something inside had focus,
      // which after a click on the page behind was never.
      if (e.key === 'Escape') {
        if (this.dialog) this.closeDialog(null);
        else if (this.clearOpen) this.clearOpen = false;
        else if (this.exportOpen) this.exportOpen = false;
        else if (this.showProfiles && this.editing) this.editing = null;   // one step back
        else if (this.showProfiles) this.showProfiles = false;
        else if (this.showAbout) this.showAbout = false;
      }
    };
    this._onChange = (e) => {
      if (this._pointerPick && e.target && e.target.tagName === 'SELECT') {
        e.target.blur();
      }
      this._pointerPick = false;
    };
    document.addEventListener('pointerdown', this._onPointerDown, true);
    document.addEventListener('keydown', this._onKeyDown, true);
    document.addEventListener('change', this._onChange, true);
  },

  unmounted() {
    document.removeEventListener('click', this._onDocClick);
    document.removeEventListener('pointerdown', this._onPointerDown, true);
    document.removeEventListener('keydown', this._onKeyDown, true);
    document.removeEventListener('change', this._onChange, true);
    if (this.pollHandle) clearInterval(this.pollHandle);
    if (this.telemetryHandle) clearInterval(this.telemetryHandle);
  },
}).mount('#app');
