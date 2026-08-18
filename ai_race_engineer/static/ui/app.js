// AIRE — settings panel + exchange log.
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

createApp({
  data() {
    return {
      // UI state
      showExchangeLog: false,
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
      exchangeLog: [],
      inputDevices: [],
      outputDevices: [],
      duckingDevices: [],
      voices: [],
      voiceFavesOnly: false,
      // Edited as rows so a half-typed entry cannot clobber the
      // config object mid-keystroke.
      pronunciationRows: [],
      voicesOpen: false,
      voiceFilter: '',
      voiceGender: 'all',
      cacheStats: null,
      // Scored against the failure modes this app actually hits: inventing
      // corner names, doing arithmetic in its head, ignoring the tools,
      // rambling past one sentence. Both of these passed every probe.
      recommendedModels: [
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
      llmTestResult: null,
      previewing: false,

      tab: 'race',
      tabs: [
        { id: 'race',     label: 'Race' },
        { id: 'engineer', label: 'Engineer' },
        { id: 'voice',    label: 'Voice' },
        { id: 'audio',    label: 'Audio' },
        { id: 'input',    label: 'Input' },
        { id: 'app',      label: 'App' },
      ],

      bindingKey: false,
      pollHandle: null,
      telemetryHandle: null,
    };
  },

  computed: {
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
    statusLabel() {
      if (!this.status.running) return 'stopped';
      return this.status.state || 'listening';
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
      this.loadPronunciation();
      await Promise.all([
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
        if (this.tab === 'race') this.refreshTelemetry();
      }, 400);
      this.pollHandle = setInterval(() => {
        this.refreshStatus();
        this.loadLog();
      }, 2000);
    },

    async loadConfig() {
      try {
        this.config = await api('/api/config');
      } catch (error) {
        this.notify(`Failed to load config: ${error.message}`, 'error');
      }
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

    async loadLog() {
      try {
        this.exchangeLog = await api('/api/log');
      } catch (error) { /* ignore */ }
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
      if (!window.confirm(
        'Restore every setting to its default?\n\n'
        + 'Your API keys, chosen voice, favourites, pronunciations and '
        + 'push-to-talk binding are kept.')) return;
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
        await this.loadConfig();
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
          await this.loadConfig();
          await this.refreshStatus();
          this.notify(`Bound to ${result.label}`);
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
          await this.loadConfig();
          await this.refreshStatus();
          this.notify(`Bound to ${result.description}`);
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
    },

    // ── LLM ────────────────────────────────────────────────────────

    async loadModels() {
      this.loadingModels = true;
      try {
        // Save first so the server queries the provider we're looking at.
        await this.saveConfig();
        const result = await api('/api/llm/models');
        this.models = result.models;
        this.modelListOpen = true;
        this.notify(`Loaded ${this.models.length} models`);
      } catch (error) {
        this.notify(`Could not list models: ${error.message}`, 'error');
      } finally {
        this.loadingModels = false;
      }
    },

    async testLLM() {
      this.testingLLM = true;
      this.llmTestResult = null;
      try {
        await this.saveConfig();
        const result = await api('/api/llm/test', { method: 'POST' });
        this.llmTestResult = { ok: true, text: `${result.model} replied: “${result.reply}”` };
      } catch (error) {
        this.llmTestResult = { ok: false, text: error.message };
      } finally {
        this.testingLLM = false;
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
        await this.loadLog();
        this.showExchangeLog = true;
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

    addPronunciation() {
      this.pronunciationRows.push({ from: '', to: '' });
    },

    removePronunciation(i) {
      this.pronunciationRows.splice(i, 1);
      this.savePronunciation();
    },

    loadPronunciation() {
      const table = (this.config?.elevenlabs?.pronunciation) || {};
      this.pronunciationRows = Object.entries(table)
        .map(([from, to]) => ({ from, to }));
    },

    async savePronunciation() {
      const el = this.config && this.config.elevenlabs;
      if (!el) return;
      const table = {};
      for (const row of this.pronunciationRows) {
        const from = (row.from || '').trim();
        const to = (row.to || '').trim();
        // Skip half-finished rows rather than saving a rule that would
        // replace a name with nothing.
        if (from && to) table[from] = to;
      }
      el.pronunciation = table;
      try {
        await api('/api/config', {
          method: 'POST',
          body: JSON.stringify({ elevenlabs: { pronunciation: table } }),
        });
      } catch (error) {
        this.notify(`Could not save pronunciations: ${error.message}`, 'error');
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

    notify(message, type = 'success') {
      const el = document.createElement('div');
      el.className = type === 'error' ? 'notification error' : 'notification';
      el.textContent = message;
      document.body.appendChild(el);
      setTimeout(() => {
        el.classList.add('fade-out');
        setTimeout(() => el.remove(), 300);
      }, 3200);
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
    this._onKeyDown = () => { this._pointerPick = false; };
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
