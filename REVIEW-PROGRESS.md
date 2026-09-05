# Review implementation — progress

Working branch: `review/now-phase`
Source: [AGENT-REVIEW.md](AGENT-REVIEW.md) — findings F1–F12, to-do items 1–29.

**How to resume:** read the *Next up* line, then the notes under that item. Every
completed item gets its own commit, so `git log --oneline main..HEAD` is the other
half of this file. Run `uv run pytest tests/ -q` before continuing — the tree
should be green between items.

**Next up:** item 2 (F2) — the calculator word-budget test.

**Baseline:** 365 tests passing at `f1e7254`.

---

## Now — correctness and cost

- [x] **1 · F1 — check `pit_commands.enabled` before calling the classifier**
  `orchestrator._handle_pit_command` runs the LLM interpreter before testing
  `enabled`, so the shipped default (disabled) still pays a model round trip for
  anything `might_be_pit_instruction` matches, and can answer a fuel question
  with a pit-command refusal.
  *Plan:* early return when disabled and the text is not the canonical
  `pit stop ...` form. Explicit commands keep the "pit commands are off" message —
  that path is pure regex and costs nothing.
  *Done:* guard sits immediately after `explicit = is_pit_command(...)`, so
  nothing below it runs while the feature is off. The later `if not enabled`
  became unreachable and was removed with it.
  *Tests:* `test_a_disabled_feature_never_reaches_the_interpreter` and
  `test_an_enabled_feature_still_interprets_natural_phrasing` in
  `tests/test_pit_commands.py`. The second is the pair that stops the shortcut
  being widened into one that breaks natural pit instructions.
  *Bug-put-back:* confirmed — reverting the guard fails 2 tests. 367 passing.

- [ ] **2 · F2 — word-budget assertion over every calculator**
  A test walking every `_TOOLS` entry across a snapshot matrix, asserting `spoken`
  stays inside the budget while `finished` is false.
  *Expected to fail on `fuel_plan` when first written (76 words measured). That
  failure is the finding* — item 3 is what makes it pass. Do not soften the
  threshold to make it green.

- [ ] **3 · F2 — split calculator output into headline + clauses**
  `fuel_plan` and `pit_window` build `spoken` from a headline plus a `clauses`
  dict; the terminal short-circuit in `llm.py` speaks the headline and only the
  clauses the question asked for.
  *Invariant to keep:* no clause is ever rewritten by the model — selection only.
  Structured fields must not move; plenty downstream reads them.

- [ ] **4 · F7 — apply the focus filter only when tools are enabled**
  With `use_tools: off`, or after a provider rejected tools, the pace focus group
  strips `Ahead on track` and the running order from a question about the car ahead.
  *Plan:* one-line guard in `prompt_builder.build_user_turn`.

- [ ] **5 · F3 — stage timings in the diagnostic trace and `status()`**
  Per-stage milliseconds through one exchange: stt, pit_classifier, llm, tts, dsp,
  duck, playback, plus `first_audio_ms` and `total_ms`.
  *Out of scope here:* P50/P95 over ≥20 real questions — that needs a live session
  and is the verification step, not the change.

- [ ] **6 · F5 — log OpenRouter cache counters into the trace**
  Read `usage.prompt_tokens_details.cached_tokens` and `cache_write_tokens` off
  every response, accumulate per exchange, surface in the trace record.
  *Expectation to check live:* zero on `anthropic/claude-haiku-4.5`, because the
  ~1,340-token static prefix is below that model's 4,096-token minimum. Confirming
  that zero is what closes or reopens item 27.

---

## Next — the voice experience (not started)

- [ ] **7 · F4 — interrupt on push-to-talk during playback**
  Stop audio, un-duck, start recording; mark the exchange `interrupted`. Guards:
  ~250 ms minimum playback before an interrupt counts, and **never** cancel a
  pending pit-command read-back's confirmation window.
- [ ] **8 · F3 — stream TTS over a persistent WebSocket**, cache miss only. High risk.
- [ ] **9 · F3 — carry DSP filter state across streamed chunks**, A/B against buffered. High risk.
- [ ] **10 · F4 — feed `[the driver cut off your previous answer]` as an operator note**
- [ ] **11 · F3 — deadline budget on the model call**, fall back to the deterministic answer

## Then — evaluation (not started)

- [ ] **12 · F9 — commit a synthetic graded corpus** (no real names, no captured radio)
- [ ] **13 · F9 — run `grade_record` over it in CI**
- [ ] **14 · F9/F6 — add route, tool-selection, refusal-direction, word-budget and TTS-char metrics**
- [ ] **15 · F9/F3 — latency-budget grading** (needs item 5)
- [ ] **16 · F9 — `make eval-live`**, opt-in, never in CI

## Then — routing and memory (not started)

- [ ] **17 · F6 — report deterministic-route coverage from the corpus**
- [ ] **18 · F6 — filler-stripping + alias table in front of the exact-match router** (no model, no embeddings)
- [ ] **19 · F6 — decide `race_control_report`:** add a tool schema or document why it is deterministic-only
- [ ] **20 · F8 — age-stamp replayed history turns**

## Then — proactive calls (not started)

- [ ] **21 · F10 — event detector over the telemetry poll** (edge-triggered, hysteresis, dedup, segment-scoped)
- [ ] **22 · F10 — deterministic announcement templates**, no LLM in the path
- [ ] **23 · F10 — announcement scheduler** (respects `_busy`, drops stale, per-stint budget, off by default)
- [ ] **24 · F10 — panel toggles**, matching the pit-commands posture

## Later — strategic (not started)

- [ ] **25 · F12 — adapter capability manifest**, read by the prompt *and* the calculators
- [ ] **26 · F12/F9 — capability profile per eval record**
- [ ] **27 · F5 — decide prompt caching** once item 6 has a live reading
- [ ] **28 · F11 — route user-facing strings through one lookup**; README note on `language`
- [ ] **29 · TODO 3 — Le Mans Ultimate reader**, on top of item 25

---

## Notes for whoever picks this up

- Nothing here has run in a live iRacing session. Items 5 and 6 exist to produce
  the measurements the rest of the plan assumes; treat their numbers as unverified
  until a race has been driven with the trace on.
- `telemetry.diagnostic_trace` is off by default and stays off — it records driver
  names and spoken radio. The new timing and cache fields ride inside it, so they
  inherit that posture.
- The repo rule holds throughout: prefer a structural fix to a stronger prompt
  instruction. Items 3, 4 and 20 all have prompt-shaped alternatives that are
  deliberately not being taken.
