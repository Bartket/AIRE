# AIRE — Agent and Voice-Agent Architecture Review

**Reviewed:** v0.2.0 (commit `f1e7254`), 5 September 2026
**Scope:** application functionality, and specifically how the race-engineer logic
works — routing, prompting, calculators, the voice pipeline, and the write path.
**Method:** full read of `orchestrator.py`, `llm.py`, `prompt_builder.py`,
`race_math.py`, `pit_commands.py`, `stt.py`, `tts.py`, `keyterms.py`, `evals.py`,
`config.py`, `telemetry/`; measurement of calculator output against the shipped
budgets; comparison against current agent and voice-agent practice (sources at the end).

> Everything below was derived by reading and executing this repository offline.
> **Nothing here was tested in a live iRacing session**, so where a finding depends
> on live behaviour it says so. Two findings were confirmed by running the shipped
> code (F2, F1) and are marked **measured**.

---

## Executive summary

AIRE is an unusually disciplined piece of engineering for its category. The
central design decision — that **the model chooses words and Python chooses
numbers** — is correct, is enforced structurally rather than by instruction, and
is the reason the engineer is trustworthy at all. The refusal paths, the unit
handling, the session classifier, the "zero is not missing" discipline and the
fail-closed pit-command grammar are all better than what the category typically
ships. `AGENTS.md` is the best artefact in the repo: it encodes failures that
reached a real race, and most of them are now structural rather than prompted.

The findings below are therefore not about the engineer being wrong. They are
about **three seams where the architecture stops being enforced**, and one about
a planned optimisation that cannot work as specified.

**The five that matter most:**

1. **The pit-command classifier fires when the feature is switched off.**
   `_handle_pit_command` calls the LLM interpreter *before* it checks
   `pit_commands.enabled`. On the shipped default (disabled), an utterance like
   *"give me an update on fuel"* costs an extra model round trip, adds one to
   two seconds of latency, and — if the classifier misreads it — answers a plain
   information question with *"I couldn't safely interpret that pit request."*
   for a feature the driver never turned on. **(F1, measured)**

2. **Terminal-tool answers bypass every length rule in the system.** When a
   calculator in `_TERMINAL_TOOLS` is the sole call, its `spoken` string is
   returned verbatim. A shipped `fuel_plan` result was measured at **42 words in
   a lap-limited race and 76 words in a timed one** — against a 25-word system
   prompt limit and a 45-word eval failure threshold. That is roughly 30 seconds
   of radio at speed, and about **5× the average TTS character cost** on the most
   frequently asked question in the app. **(F2, measured)**

3. **There is no latency instrumentation anywhere in the pipeline, and nothing
   streams.** Not one `perf_counter` across STT → LLM → TTS → DSP → playback.
   The README's "a couple of seconds" is an estimate, not a measurement.
   Playback is `sd.play()` + `sd.wait()` and `stream` is hardcoded `False`, so
   the driver hears nothing until the entire answer has been synthesised. For a
   voice agent this is the single highest-leverage thing left un-instrumented. **(F3)**

4. **A push-to-talk press while the engineer is speaking is silently dropped.**
   No beep, no cancellation, no acknowledgement — the `_busy` lock refuses it and
   logs at debug. Current voice-agent practice treats interruption handling as
   the single feature most responsible for an agent sounding human rather than
   mechanical, and push-to-talk makes it *trivially* safe to implement, because
   there is no turn-detection problem to solve. **(F4)**

5. **`TODO.md` item 2 — prompt caching on Claude — cannot fire on the current
   default model.** Claude Haiku 4.5 requires a **4,096-token** minimum cacheable
   prefix. The static prefix here is the ~1,340 tokens the TODO itself measures.
   The markers would be accepted, `cached_tokens` would stay at zero, and the
   work would look done while saving nothing. The item is not wrong about the
   headroom — it is wrong about the model it would run on. **(F5)**

Beyond those: the deterministic router is exact-string matching over roughly
forty phrases with no coverage measurement (F6); the prompt's focus filter can
remove the only line that answers the question when tool calling is off (F7);
conversation history replays old numbers with no age attached (F8); `evals.py`
is a good instrument that nothing ever runs (F9); the engineer never initiates,
which is a defensible choice but leaves the largest functional gap against
CrewChief and DRE (F10); and every calculator's spoken output is hardcoded
English, which quietly makes the `language` setting a dead end (F11).

**What not to do** is at the end and matters as much as the to-do list. Three
popular 2026 voice-agent moves — VAD barge-in, speech-to-speech models, and a
question queue — would each make AIRE worse, for reasons specific to this app.

---

## 1. How the engineer actually works today

Reading the code, a spoken question can be answered through **five** different
routes, only one of which is "ask the model". This is not documented anywhere,
and it is the most important thing to hold in your head when reasoning about the
findings.

```
                         push-to-talk release
                                  │
                     ┌────────────▼────────────┐
                     │ STT (+ session keyterms)│
                     └────────────┬────────────┘
                                  │
             ┌────────────────────▼────────────────────┐
             │ should_repeat()  → ROUTE 1: stt_guard   │  fixed string, no model
             └────────────────────┬────────────────────┘
                                  │
             ┌────────────────────▼────────────────────┐
             │ _is_repeat_request() → ROUTE 2: repeat  │  replays _last_spoken_text
             └────────────────────┬────────────────────┘
                                  │
             ┌────────────────────▼────────────────────┐
             │ _handle_pit_command() → ROUTE 3: pit    │  ← F1 lives here
             │   confirm / cancel / classify / arm     │    (1 extra LLM call)
             └────────────────────┬────────────────────┘
                                  │
             ┌────────────────────▼────────────────────┐
             │ _direct_telemetry_answer()              │
             │        → ROUTE 4: deterministic         │  ← F6: exact strings
             │   ~40 exact normalised phrases          │    calculator text verbatim
             └────────────────────┬────────────────────┘
                                  │
             ┌────────────────────▼────────────────────┐
             │ LLMClient.generate()  → ROUTE 5: model  │
             │   tools on  → _chat_with_tools          │
             │     single terminal tool → verbatim ────┼──► ← F2: no word budget
             │     otherwise → second model round trip │
             │   tools off → summary_lines in prompt   │
             └────────────────────┬────────────────────┘
                                  │
                     TTS → radio DSP → volume → duck → blocking playback
                                                          ↑
                                                     F3 / F4 live here
```

Routes 1, 2, 4 and the terminal-tool branch of 5 all return **text no model ever
wrote**. That is the design working exactly as intended — and it is also why the
persona, the word budget, the debrief mode and the language setting silently do
not apply to a large share of real answers. Nothing in the code or docs states
that tradeoff, so it is easy to reason about the engineer as though every answer
passed through the prompt. Most interesting ones do not.

---

## 2. Findings

Each finding states what it is, how it was established, why it matters at 200 km/h,
and what to do. Severity is about the driver, not about the code.

### F1 — The pit-command classifier runs even when pit commands are disabled

**Severity: high. Measured.**

`orchestrator._handle_pit_command` checks `enabled` *after* it has already spent
a model call:

```python
explicit = is_pit_command(question)
if not explicit and not might_be_pit_instruction(question):
    return None                       # cheap gate — good
...
if plan is None:
    interpretation = await asyncio.to_thread(     # ← LLM call happens here
        self.llm.interpret_pit_command, question, [...])
    if interpretation.kind == "not_command":
        return None
    ...
if not enabled:                                   # ← check happens here
    return ("Pit commands are off; enable the at-your-own-risk setting first.",
            "pit_command:disabled")
```

`might_be_pit_instruction` is deliberately broad. Running the shipped gate over
plausible radio phrasings:

| Utterance | Gate | Consequence with the feature **off** |
| --- | --- | --- |
| `give me an update on fuel` | **True** | extra LLM call, then falls through to a second one |
| `update on tyre wear please` | **True** | same |
| `box this lap for fuel` | **True** | same (this one is at least arguably a command) |
| `check my fuel` | False | fine |
| `remind me how much fuel is left` | False | fine |

Three things follow, and they compound:

- **Cost.** A question that trips the gate makes *three* LLM calls in the tool-enabled
  path: classifier, then the tool-call turn, then the answer turn. At the README's
  measured ~$0.0053 of LLM spend per question, that is roughly a 50% increase on
  the affected turns for a feature the driver has switched off.
- **Latency.** One extra full round trip, serialised, before the pipeline even
  begins answering. On the 30-second timeout that is unbounded in the worst case.
- **Correctness.** If the classifier returns `ambiguous` for an information
  request, the driver hears *"I couldn't safely interpret that pit request;
  nothing was changed."* — a refusal about a feature that is off, in response to a
  question about fuel. That is exactly the failure mode `AGENTS.md` warns about:
  a refusal the driver could have had answered, which teaches them to stop asking.

**Fix.** Move the `enabled` test above the classifier. When the feature is off,
only the canonical `pit stop ...` form should produce the "pit commands are off"
message — that path is a pure regex and costs nothing. Everything else should
return `None` and let the ordinary engineer answer conversationally.

```python
enabled = self.config.get("pit_commands", {}).get("enabled") is True
explicit = is_pit_command(question)
if not enabled and not explicit:
    return None            # no classifier, no cost, no spurious refusal
```

**Verify:** a test asserting `llm.interpret_pit_command` is never called when
`pit_commands.enabled` is false, and that `"give me an update on fuel"` reaches
the ordinary answer path. Put the bug back and confirm the test fails.

---

### F2 — Terminal-tool answers bypass the word budget, the persona and the debrief

**Severity: high. Measured.**

```python
_TERMINAL_TOOLS = {"fuel_plan", "pit_window", "position_report",
                   "adjacent_gap_trend", "car_resources"}
...
if len(calls) == 1 and name in _TERMINAL_TOOLS:
    return result.get("spoken", "")
```

The reasoning in the comment is sound — asking the model to paraphrase a finished
calculation reintroduced exactly the errors Python removed. But the consequence
is that a calculator's `spoken` string becomes a *final spoken answer* while
being written as an *exhaustive report*. Running the shipped `fuel_plan` against
a short-on-fuel car:

| Scenario | Words | Approx. spoken | Approx. TTS chars |
| --- | --- | --- | --- |
| System prompt limit | 25 | ~10 s | ~140 |
| README measured average answer | ~14 | ~6 s | **78** |
| `evals.grade_record` failure threshold | 45 | ~18 s | ~250 |
| `fuel_plan`, lap-limited race, short on fuel | **42** | ~17 s | ~215 |
| `fuel_plan`, timed race, short, last lap high | **59** | ~24 s | ~300 |
| `fuel_plan`, `target_minutes`, timed, short | **76** | ~30 s | ~385 |

The 76-word case, verbatim:

> *"We're short on fuel — about 3.2 laps in the tank at 3.70 litres a lap, and 14
> laps to run. That's more than you can save. One fuel stop is required. The first
> window is open and closes on lap 22. That's 14 laps at your current pace — an
> estimate from the clock, not a lap count. Last lap used 5.00 litres, well above
> your average — that pace will not reach the flag."*

Every clause in that is *true and useful*. The problem is that all of them arrive
at once, unprompted by the question, while the driver is braking. Four separate
consequences:

- **The 25-word rule does not apply to the most-asked question in the app.**
  `_direct_telemetry_answer` routes *"how's my fuel"*, *"fuel status"* and six
  other phrasings straight to `fuel_plan().spoken`, and the tool path routes the
  rest there too.
- **`grade_record` already flags this** — "race-speed answer exceeds 45 words" —
  but nothing runs it against a corpus (see F9), so the guard has never fired.
- **TTS is billed per character and is 46% of the bill.** A 385-character answer
  is ~5× the measured 78-character average. On a fuel-critical stint, where the
  question gets asked repeatedly, this is the largest single controllable cost in
  the app.
- **Persona, name, debrief mode and language never apply.** After the flag, a
  terminal `position_report` says *"You finished P4."* — correct, but not the
  warm, named, two-sentence debrief `DEBRIEF_PROMPT` was written to produce.

**Fix — keep the guarantee, drop the monologue.** The invariant worth preserving
is "the model must not rewrite a number or a verdict". That does not require
sending the whole report. Change calculators to return the sentence *plus* its
parts, and let the model *select* rather than rephrase:

```python
return _yes(
    spoken=headline,                       # "Fuel is short by 3.2 litres."
    clauses={                              # each already radio-safe wording
        "verdict":  "That's more than you can save.",
        "stops":    "One fuel stop is required.",
        "window":   "The first window is open and closes on lap 22.",
        "estimate": "That's 14 laps at your current pace, an estimate from the clock.",
        "last_lap": "Last lap used 5.00 litres, well above your average.",
    },
    ...
)
```

Then either (a) keep the terminal short-circuit but return
`headline` only, with the remaining clauses folded in **only when the question
asked for them**; or (b) drop the short-circuit for these tools and instruct the
model: *"You may speak these clauses verbatim, in any order, or omit them. You
may not alter, merge or recompute any of them."* Option (a) is cheaper and needs
no model behaviour; option (b) restores persona and debrief. They compose — (a)
during a session, (b) after the flag.

Either way, add a hard assertion: **no calculator's `spoken` may exceed the
configured word budget while `finished` is false.** That is a unit test over
every calculator against a matrix of snapshots, and it costs nothing to run.

**Verify:** the calculator word-budget test, plus F9's corpus run showing zero
"exceeds 45 words" failures.

---

### F3 — Nothing in the pipeline is measured, and nothing streams

**Severity: high.**

A grep for `perf_counter`, `elapsed`, `latency` or `duration_ms` across
`ai_race_engineer/` returns only comments and an unrelated DSP parameter. There
is no timing on transcription, no timing on the model call, no timing on
synthesis, no timing on playback, and no field in the diagnostic trace to hold
one. The trace records *what* was said and the minute of telemetry behind it —
excellent for correctness — but nothing about *when*.

The pipeline is also fully sequential and fully buffered:

- `_chat_raw` sends `"stream": False`.
- `tts.synthesize` returns complete PCM before anything downstream runs.
- The radio effect and volume are applied to the whole buffer.
- `AudioPlayer.play_array` is `sd.play(...)` then `sd.wait()`.

So the driver's time-to-first-audio is the **sum** of: STT round trip, plus
(optionally) the pit classifier round trip, plus one or two model round trips,
plus full TTS synthesis, plus DSP, plus the ducking enumeration. On a
tool-calling question that is four network round trips before a single sample
plays. The LLM timeout is 30 seconds with no deadline budget — there is no point
at which the engineer gives up and says something deterministic instead.

Two things to do, in order:

**First, measure.** Add stage timings to the diagnostic trace and to the status
payload. This is a small change and it makes every later decision evidence-based
rather than argued:

```python
"timings_ms": {
    "stt": ..., "pit_classifier": ..., "llm_first_token": ...,
    "llm_total": ..., "tool_rounds": n, "tts": ..., "dsp": ...,
    "duck": ..., "playback_start": ...,   # ← time-to-first-audio
    "total": ...,
}
```

Then report P50 and P95 per route. The repo's own rule applies: *three samples is
not a measurement.* Until this exists, "answers take a couple of seconds" in the
README is a guess.

**Second, stream the speech.** This is where the wall-clock actually is. Current
practice puts ElevenLabs Flash v2.5 at roughly 50–75 ms model time-to-first-byte
and ~290 ms P50 end-to-end TTFA in independent tests, and a persistent WebSocket
saves a further 50–100 ms per turn against reopening HTTP. Against a full
synthesis of a 40–76 word answer, that is the difference between the driver
hearing the first word almost immediately and waiting for the whole sentence.

Streaming has one real cost here, and it must be designed for: **the radio effect
is a band-pass plus compression, and applying it per chunk will not sound
identical to applying it to the whole buffer.** Filter state has to be carried
across chunks (`scipy.signal.lfilter` with `zi`), and the AGC needs a lookahead
or a fixed target rather than a per-buffer normalisation. Do this as a separate,
verified change with an A/B listen — do not fold it into the streaming work.

**Also worth doing at the same time:** the **speech cache and streaming interact
badly** if done naively. Cache the raw synthesised PCM as today (the repo's rule
— cache raw, never processed — is right), and stream only on a cache miss.

**Verify:** trace timings before and after, P50/P95 time-to-first-audio across at
least twenty real questions per route; a null-DSP A/B confirming the streamed
radio effect is indistinguishable from the buffered one.

---

### F4 — A press during speech is dropped silently; there is no way to interrupt

**Severity: medium-high.**

```python
if not self._busy.acquire(blocking=False):
    logger.debug("Still handling the previous question — ignoring press")
    return
```

No beep, no log the driver sees, no cancellation. If the engineer has embarked on
a 30-second fuel report (F2) and the driver wants to ask something else — or
simply wants it to stop — there is nothing they can do but wait it out.

This is the one place where mainstream voice-agent practice maps cleanly onto
AIRE, and it maps *in AIRE's favour*. The hard part of barge-in is turn
detection: deciding, from audio alone, whether the human meant to interrupt.
Push-to-talk answers that question with a physical button. There is no VAD to
tune, no false-trigger risk from engine noise, no backchannel ambiguity. The
implementation is small:

1. On button-down while `_status == "speaking"`: stop playback immediately
   (`sd.stop()`), un-duck, release, and start recording. The blip already
   signals the channel is open.
2. Mark the abandoned answer in the history and the trace as `interrupted`, with
   how far through it got. That is a genuinely useful eval signal — *the driver
   cut this one off* is the cheapest quality label available.
3. Feed that back to the model on the next turn as an operator note, not as
   driver text: *"[the driver cut off your previous answer]"*. Current practice
   is explicit about injecting an interruption marker so the model knows where it
   was cut off rather than assuming it was heard.

Two guards worth having: a short minimum (~250 ms of playback) before an
interrupt counts, so a double-tap does not cancel instantly; and **never allow an
interrupt to cancel a pending pit-command read-back's confirmation window** —
that window is safety-critical and already has careful single-use semantics in
`_handle_pit_command`.

**Verify:** a test that a button-down during `speaking` stops playback, starts
recording, and marks the previous exchange interrupted; and a test that the
pending pit command survives it.

---

### F5 — `TODO.md` item 2 (prompt caching) cannot fire on the default model

**Severity: medium — it prevents wasted work rather than fixing a defect.**

The TODO's reasoning is right: roughly 1,340 of ~2,300 prompt tokens are the
identical system prompt and tool schemas on every call, tool calling sends that
prefix twice, and OpenRouter supports Anthropic caching with explicit
`cache_control` markers at 1.25× to write and 0.1× to read.

What it misses is the **minimum cacheable prefix**, which is model-dependent and
not monotonic across generations:

| Model | Minimum cacheable prefix |
| --- | --- |
| Claude Opus 5 | 512 tokens |
| Claude Opus 4.8, Sonnet 5, Sonnet 4.6 | 1,024 tokens |
| Claude Opus 4.7 | 2,048 tokens |
| **Claude Opus 4.6, Opus 4.5, Haiku 4.5** | **4,096 tokens** |

AIRE's default is `anthropic/claude-haiku-4.5`. Its ~1,340-token static prefix is
a third of the 4,096 minimum. Markers would be accepted; nothing would cache;
`prompt_tokens_details.cached_tokens` would stay at zero; **and there would be no
error.** The work would appear complete and save nothing.

Three honest options:

- **Do nothing.** Defensible. The measured LLM share is 34% of ~$0.015, so the
  theoretical ceiling on this optimisation is a few tenths of a cent per question.
  F2 (answer length → TTS characters) is a larger, simpler saving in the same bill.
- **Change the default model.** Sonnet 5 (1,024-token minimum) or Opus 5 (512)
  would cache the existing prefix immediately, but both are materially dearer per
  token than Haiku 4.5 and the repo's floor-check has only been run against
  Haiku 4.5 and a Gemini Flash. Re-run the thirteen-assertion smoke test before
  changing anything — and treat passes the way `AGENTS.md` says to: as evidence
  that those particular wires were not tripped once.
- **Implement it behind a capability check.** Send `cache_control` only when the
  configured model's known minimum is below the measured prefix size, and assert
  on `cached_tokens > 0` in a live check. This is the version that cannot silently
  do nothing.

**Whatever is chosen, add the measurement first.** Log
`usage.prompt_tokens_details.cached_tokens` and `cache_write_tokens` from the
OpenRouter response into the diagnostic trace. That single line turns this from
an argument into an observation — and it is the same discipline `AGENTS.md`
already demands for process priority: *read the value back*.

Two secondary points if caching is implemented:

- **The prefix is less stable than it looks.** `TOOLS_PROMPT` is appended only
  when tools are enabled and `DEBRIEF_PROMPT` only when the session has finished,
  so the system block changes shape mid-session — twice. Order it so the truly
  invariant part (the editable persona + `VALIDATION_PROMPT` + `ADVICE_PROMPT`)
  comes first and the conditional blocks come after the breakpoint.
- **Tool schemas render before `system`.** They are already deterministic here
  (`_TOOLS` is a module-level list), which is right — keep it that way and never
  build the schema list from a set or a dict iteration.

---

### F6 — The deterministic router is exact-string matching, and its coverage is unmeasured

**Severity: medium.**

`_direct_telemetry_answer` matches `normalise_question(question)` against literal
sets — roughly forty phrases across position, fuel, pit window, resources and race
control, plus one substring rule for gap trend. Every near-miss falls through to
the model:

| Driver says | Routed deterministically? |
| --- | --- |
| `where am i` | yes |
| `where am i now` | **no** |
| `whats my position` | yes |
| `what position am i in` | **no** |
| `hows my fuel` | yes |
| `hows the fuel looking` | **no** |
| `when should i pit` | yes |
| `when do i need to pit` | **no** |

The fall-through is *safe* — the model has the tools and the block — but it costs
a round trip and reintroduces the variance the deterministic route exists to
remove. The failure is invisible: nothing counts how often a question that had a
deterministic answer did not get one.

Three things, in order of value:

1. **Measure it.** The diagnostic trace already records `route`. Add a summary to
   `evals.py` that reports the route distribution over a corpus, and a per-question
   flag where a model-routed question would have matched a deterministic intent
   under looser matching. You cannot tune this without the number.
2. **Loosen matching deliberately, not accidentally.** Keep exactness as the
   contract but widen the input: strip filler (`now`, `mate`, `please`, `for me`,
   `can you tell me`), expand a small alias table, and match on a normalised
   intent key. Do **not** reach for embeddings or a classifier model — that
   reintroduces a model call and non-determinism into the one path built to have
   neither.
3. **`race_control_report` has no tool schema.** It is a full calculator, reachable
   only through eight exact phrases in `_direct_telemetry_answer` and never
   callable by the model. Ask *"am I under investigation?"* or *"what's this flag
   for?"* and the calculator is unreachable; the model reads the raw `Flag:` and
   `PENALTY` lines from the block instead. Either add it to `_TOOLS` or document
   why it is deliberately deterministic-only.

**Verify:** a route-distribution report over the corpus, and a parametrised test
covering the phrasing variants above.

---

### F7 — The focus filter can remove the only line that answers the question

**Severity: medium.**

`_focus_prefixes` compacts the telemetry block for four topic groups. The
docstring is careful and the groups are conservative — but the filter is applied
**regardless of whether tool calling is available**, and `summary_lines` is only
folded in for fuel/pit/strategy wording:

```python
focus = self._focus_prefixes(question)
if focus is not None:
    block = self._focused_block(block, focus)
if not tools_enabled:
    precomputed = (race_math.summary_lines(telemetry)
                   if focus is None or any(word in question.lower()
                                           for word in ("fuel", "pit", "strategy"))
                   else [])
```

Concretely: *"Am I faster than the car ahead?"* contains `faster`, so the pace
group fires and the block keeps only `Remaining`, `Fastest lap`, `Stint`, `Pace`
and `Best lap`. **`Ahead on track` and the running order are dropped.** With
tools enabled that is fine — `driver_report` or `pace_compare` fetches it. With
`use_tools: "off"`, or on a provider that rejected tools and set
`_tools_unsupported` for the session, the model is asked a question about a rival
having just been handed a block with no rival in it. The honest outcome is a
refusal; the likely outcome, given every other line reads like a live update, is
worse.

**Fix.** Make focus filtering conditional on tool availability — when the model
cannot fetch what was filtered out, do not filter. That is a two-line change:

```python
focus = self._focus_prefixes(question) if tools_enabled else None
```

The token saving the filter buys is small; the failure it can cause is not. If
the saving matters, the alternative is to widen the pace group to carry
`Ahead`/`Behind`/`Running order` — but the conditional is simpler and cannot be
wrong.

**Verify:** a test asserting the full block is used when `tools_enabled=False`,
and that a `faster`-worded question with tools off still contains `Ahead on track`.

---

### F8 — History replays old numbers with no age attached

**Severity: medium.**

`LLMClient._history` holds `(question, answer)` pairs and `PromptBuilder`
replays them as bare `user`/`assistant` turns. The current telemetry block is
attached only to the *current* user turn. So four turns back, the assistant said
*"Rossi is 1.2 seconds ahead"* — and that sentence now sits in context, in the
model's own voice, with nothing marking it as three laps old.

The system prompt forbids reuse (*"never reuse one from an earlier answer"*),
which is exactly the shape of rule `AGENTS.md` says to distrust: *when changing
prompts, assume a weaker model will ignore them; prefer a structural fix.* The
structural fix is cheap — stamp the history:

```python
self._history.append((question, text, {"lap": track.current_lap,
                                       "session_time": track.time_remaining}))
```

and render replayed turns with the delta: `[3 laps ago] Rossi is 1.2 seconds
ahead.` The model then has a reason not to reuse it rather than only a rule.
An even cheaper variant, if that feels heavy: keep `history_turns` at 4 for
*questions* but strip numerals from assistant turns older than one lap — the
conversational thread survives, the stale figures do not.

Note this interacts with F5: history sits after the cache breakpoint anyway, so
stamping it costs nothing in cache terms.

**Verify:** a prompt test asserting replayed turns carry an age marker, and a
trace-graded check that no answer restates a figure that appears only in history.

---

### F9 — The evals are good, and nothing runs them

**Severity: medium.**

`evals.py` is genuinely well-built: `grade_record` encodes real regressions
(the invented contact rate, the qualifying clock-vs-allowance failure, the
marginal-fuel-to-pit-now failure, the 45-word budget) as deterministic checks
that need no model. `test_evals.py` proves it round-trips a snapshot.

What is missing is the corpus. There is no committed set of graded records, and
CI runs `pytest tests/ -q` only. So the grader has never actually graded anything
in an automated run. F2's 76-word answer would trip the word-budget check today,
and nothing has noticed.

**Do this, in order:**

1. **Commit a synthetic corpus.** Not captured driver traces — those carry real
   names and spoken radio, and `telemetry.diagnostic_trace` is off by default for
   exactly that reason. Build records from constructed `TelemetrySnapshot`s
   covering the scenarios in `AGENTS.md`'s failure list: unclassified position,
   marginal fuel, qualifying allowance vs clock, stale tyre data, fixed setup,
   meatball vs cosmetic damage, finished session, AI field, multiclass traffic.
2. **Run `grade_record` over it in CI.** It costs nothing and needs no key.
3. **Add the metrics the current graders do not cover** — this is where 2026
   agent-eval practice is explicit that final-answer quality is not enough for an
   agent with autonomy:
   - **route correctness** — did a question with a deterministic answer take the
     deterministic route (F6)?
   - **tool-selection accuracy** — for a scripted question, was the right
     calculator called, and was it called *at all* rather than refused (the
     `TOOLS_PROMPT` failure mode)?
   - **refusal correctness** — split into *refused something it had* (the
     expensive failure) and *answered something it did not have* (the dangerous one).
   - **word budget and estimated TTS characters** per answer.
   - **latency budget**, once F3 lands.
4. **Keep `--replay-model` opt-in and out of CI.** It costs real money and the
   repo is right to gate it. But add a `make eval-live` target with the model and
   the cost printed up front, so the floor check is a command rather than a memory.

The rule of thumb worth adopting from current practice: deterministic metrics for
exact checks (tool correctness, route, word count), LLM-as-judge only for the
things that genuinely need judgement (is this in character, is this a
recommendation rather than an order). Do not reach for a judge where an assertion
works — this codebase's whole thesis is that the deterministic version is better.

**Verify:** CI green with the corpus, and a deliberate regression (put F2 back)
failing it.

---

### F10 — The engineer never initiates

**Severity: medium. This is a design choice, not a defect — but it is the largest
functional gap against the category.**

`_speak` is reachable only from `ask()` and `_handle_recording()`. Nothing is
ever said unprompted. The README states this plainly (*"It is not a spotter"*)
and that framing is right: proximity calls need sub-second latency and this
architecture cannot provide them.

But *spotter* and *engineer who initiates* are different things. A real race
engineer says four kinds of thing without being asked, none of which is
time-critical to half a second:

| Class | Example | Latency tolerance |
| --- | --- | --- |
| Race control | *"Black flag, you need to come in."* | ~2 s — still far faster than the driver noticing |
| Strategy window | *"Pit window's open now, closes lap 22."* | ~5 s |
| Consumable | *"Fuel won't reach the flag at this pace."* | ~5 s |
| Session state | *"Green flag."* · *"Last lap."* | ~2 s |

The category has moved here: CrewChief's proximity, blue-flag and fuel calls are
its defining feature, and DRE ships ~50 unique alerts against ~400 voice commands.
AIRE is strictly better than both at *answering*, and absent at *telling*.

**A scoped proposal that does not compromise the architecture:**

- **Deterministic only.** Every announcement is a Python-templated string from
  `race_math`. No LLM in the path — the trigger is a telemetry state change, not
  a judgement.
- **Off by default, per-class toggles.** Same posture as pit commands. A driver
  who wants silence keeps silence.
- **Edge-triggered with hysteresis and dedup.** Fire on transition, never on
  level; hold a per-event cooldown; never repeat the same event in a session
  segment. `_adopt_session` already gives the segment boundary.
- **A budget, not a stream.** Cap at N announcements per stint, and drop any
  announcement whose event is older than a few seconds by the time the channel is
  free. Late information is worse than none.
- **Never speak over a question.** Reuse the `_busy` lock; an announcement that
  cannot acquire it is dropped, not queued.
- **Race control is the only class on by default, if any is.** `ADVICE_PROMPT`
  already establishes that a black flag or a meatball is not the engineer's
  opinion — it is the one category where speaking unbidden is unambiguously right.

There is a pleasant side effect. The README records a **2.7% speech-cache hit
rate** in a live session because nearly every answer is unique. Announcements are
templated and repeat across sessions — *"Green flag."*, *"Last lap."*, *"Pit
window's open."* — so they cache almost perfectly. Proactive calls would be close
to free after their first use, which is the opposite of the cost profile of
everything else in the app.

**Verify:** an event-generator unit test over a scripted telemetry sequence
asserting each announcement fires exactly once, at the transition, within the
budget, and never while `_busy` is held.

---

### F11 — Every deterministic answer is hardcoded English

**Severity: low, but it is a structural ceiling.**

`config.language` is threaded into STT (`language_code`) and TTS
(`language_code`), and the system prompt would follow the driver's language.
But routes 1, 2, 4 and every terminal tool return English strings written into
`llm.py`, `orchestrator.py` and `race_math.py`:

- `COMMS_FAILURE`, `NO_ANSWER`
- `"No pit command is waiting for confirmation."`, `"Pit stop set."`, and every
  other pit-route string
- every `_no(...)` reason and every `_yes(...)` `spoken` in `race_math.py`

So a non-English driver gets an engineer that answers in their language *except*
for the safety-critical calculations and every refusal — which is arguably the
worst possible split. The setting is also not exposed in the UI or documented in
the README, so today this is latent rather than live.

**No action needed now.** But two cheap things keep the option open: put every
user-facing string behind a single lookup (even if there is only one language in
it), and note in the README that `language` currently affects recognition and
voice only. If localisation is ever wanted, F2's clause refactor is the natural
place to do it — clauses are short, finite and translatable in a way a
sentence-assembling `f`-string is not.

---

### F12 — Tool surface: fifteen schemas on every call, and no capability manifest

**Severity: low-medium. The second half aligns with `TODO.md` item 3.**

The tool design is good. Descriptions carry behavioural guidance
(*"This is the authority on whether to pit now"*, *"do not substitute best laps"*),
which is the right place for it — a tool description is read at the moment of
choosing, where a system-prompt rule is read once. `dispatch` never raises, filters
arguments against the schema and refuses cleanly on a missing required field.
`_MAX_TOOL_ROUNDS = 2` is a sound stall guard.

Two observations:

- **All fifteen schemas are sent on every request**, whatever the question. That
  is a fixed cost per call and part of the ~1,340-token prefix in F5. Do **not**
  reach for dynamic tool selection to fix it: swapping the tool set mid-session
  is one of the classic silent cache invalidators, and the token saving is small
  against the risk of the model not having the calculator it needed. If it ever
  becomes worth addressing, the correct mechanism is tool search, which *appends*
  schemas rather than swapping them and so preserves the cached prefix.
- **No capability manifest on the adapter.** `TODO.md` item 3 identifies this
  precisely for Le Mans Ultimate, and the analysis there is right: several of the
  firmest rules in `prompt_builder.py` are iRacing facts stated as universal ones
  ("there is no brake temperature channel", "no forecast exists"). Two additions
  to that plan:
  - **The calculators need the manifest too, not just the prompt.** `race_math`
    hardcodes iRacing assumptions — `fuel_plan` assumes one consumable, and the
    TODO already notes LMU's Virtual Energy makes that two. A manifest that only
    the prompt reads would leave the arithmetic wrong while the refusals were right.
  - **The eval corpus needs a capability profile per record**, or the grader will
    mark a correct LMU answer as a hallucination for reporting a brake temperature.

---

## 3. What is working, and should be protected

Listed because a review that only finds problems misrepresents the codebase, and
because several of these are load-bearing in ways that are easy to break later.

| Area | Why it is right |
| --- | --- |
| **Arithmetic in Python** | The single decision the app's credibility rests on. `_spoken_delta` handing over *words* rather than a decimal is the sharpest version of it. |
| **`session_kind()` as the only classifier** | One place to add a session type. The Test Drive bug is exactly what duplicated classification produces. |
| **`_no(...)` as a complete answer** | An honest refusal shaped like every other result means the caller cannot forget to handle it. |
| **Units converted once, at the reader** | The snapshot is metric throughout, imperial applied only where numbers become words. No calculator can disagree with another. |
| **"Zero is not missing"** | `_positive` vs `_non_negative` decided per channel, with the leader-gap bug documented. |
| **Pit-command write path** | Fail-closed grammar, model can only propose, `pit_amounts_match` catches invented and converted amounts, read-back plus separate confirmation, 30 s window, single use — with the repeat-request carve-out reasoned about explicitly. This is the best-designed part of the app. |
| **`keyterms.py`** | Session-derived recognition bias with the cost threshold designed in (80 terms, under the 100-term surcharge), ordered by who the driver is most likely to ask about. Correctly identified in `TODO.md` as something an audio-native model would delete. |
| **Speech cache keys on everything that changes the audio** | Text, voice, model *and* every voice setting. Caching raw PCM and re-applying DSP locally is the right split. |
| **Telemetry polled, not fetched per question** | A question reads memory instead of stalling on the SDK, and each snapshot comes from one frozen buffer. |
| **`AGENTS.md`** | Every rule traces to a failure that reached a real session. Keep writing it this way. |

---

## 4. To-do list

Ordered by value per unit of risk. **Effort** is rough developer-days for someone
who knows this codebase. **Risk** is the chance of breaking something that works.

### Now — correctness and cost, small and contained

| # | Item | Finding | Effort | Risk | Done when |
| --- | --- | --- | --- | --- | --- |
| 1 | Check `pit_commands.enabled` before calling `interpret_pit_command`; only the canonical `pit stop ...` form answers when disabled | F1 | 0.5 d | low | Test asserts the classifier is never called with the feature off; `"give me an update on fuel"` reaches the ordinary path; bug put back, test fails |
| 2 | Word-budget assertion over every calculator × a snapshot matrix, while `finished` is false | F2 | 0.5 d | low | Test exists and currently **fails** on `fuel_plan` — that failure is the finding |
| 3 | Split calculator output into `spoken` headline + `clauses`; terminal short-circuit returns the headline only | F2 | 2 d | med | Item 2's test passes; every clause still reachable; no calculator string rewritten by the model |
| 4 | Apply `_focus_prefixes` only when `tools_enabled` | F7 | 0.25 d | low | Test: `faster`-worded question with tools off keeps `Ahead on track` |
| 5 | Stage timings into the diagnostic trace and `status()` | F3 | 1 d | low | Trace carries `timings_ms`; P50/P95 per route reported over ≥20 questions |
| 6 | Log `prompt_tokens_details.cached_tokens` / `cache_write_tokens` from OpenRouter into the trace | F5 | 0.25 d | low | Field present; observed zero on Haiku 4.5, confirming F5 before any caching work |

### Next — the voice experience

| # | Item | Finding | Effort | Risk | Done when |
| --- | --- | --- | --- | --- | --- |
| 7 | Interrupt on push-to-talk during playback: stop audio, un-duck, record; mark the exchange `interrupted`; never cancel a pending pit read-back | F4 | 1.5 d | med | Tests for both behaviours; live check that DSP tail and ducking restore cleanly |
| 8 | Stream TTS (WebSocket, persistent connection) with chunked playback; stream only on cache miss | F3 | 3 d | **high** | Time-to-first-audio P50 down measurably against item 5's baseline; cache still stores raw PCM |
| 9 | Carry filter state across streamed chunks; A/B the radio effect against the buffered version | F3 | 1.5 d | high | Null-difference listen test; no artefacts at chunk boundaries |
| 10 | Feed `[the driver cut off your previous answer]` to the model as an operator note on the next turn | F4 | 0.5 d | low | Prompt test; the note never appears as driver text |
| 11 | Deadline budget on the model call: if no answer within N seconds, speak the deterministic result instead | F3 | 1 d | med | Test with a stalled client; driver always hears something |

### Then — evaluation, which unblocks everything after it

| # | Item | Finding | Effort | Risk | Done when |
| --- | --- | --- | --- | --- | --- |
| 12 | Commit a synthetic graded corpus covering `AGENTS.md`'s documented failures | F9 | 2 d | low | Corpus in `tests/`; no real driver names or captured radio |
| 13 | Run `grade_record` over the corpus in CI | F9 | 0.5 d | low | CI green; item 2's regression fails it |
| 14 | Add route correctness, tool-selection accuracy, refusal correctness (both directions), word budget and TTS-character estimate to the grader | F9, F6 | 2 d | low | Route distribution reported; a deliberately loosened router shows up as a change |
| 15 | Add latency-budget grading once item 5 lands | F9, F3 | 0.5 d | low | P95 regression fails CI |
| 16 | `make eval-live` — opt-in `--replay-model` with model and estimated cost printed first | F9 | 0.5 d | low | One command; never runs in CI |

### Then — routing and memory

| # | Item | Finding | Effort | Risk | Done when |
| --- | --- | --- | --- | --- | --- |
| 17 | Report deterministic-route coverage from the corpus | F6 | 0.5 d | low | Number exists; near-miss phrasings listed |
| 18 | Filler-stripping and an alias table in front of the exact-match router (no model, no embeddings) | F6 | 1 d | med | Parametrised test over the phrasing variants; coverage number improves |
| 19 | Decide `race_control_report`: add a tool schema, or document why it is deterministic-only | F6 | 0.25 d | low | Either the schema exists or `AGENTS.md` says why not |
| 20 | Age-stamp replayed history turns (`[3 laps ago] ...`) | F8 | 1 d | low | Prompt test; graded check that no answer restates a history-only figure |

### Then — proactive calls (the largest functional addition)

| # | Item | Finding | Effort | Risk | Done when |
| --- | --- | --- | --- | --- | --- |
| 21 | Event detector over the telemetry poll: edge-triggered, hysteresis, per-event dedup, session-segment scoped | F10 | 2 d | med | Scripted-sequence test: each event fires exactly once, at the transition |
| 22 | Deterministic announcement templates in `race_math`; no LLM in the path | F10 | 1 d | low | Every announcement is a Python string; word budget applies |
| 23 | Announcement scheduler: respects `_busy`, drops stale events, per-stint budget, per-class toggles, all off by default except race control | F10 | 1.5 d | med | Never speaks over a question; a dropped announcement is traced, not queued |
| 24 | Panel UI for the toggles, matching the pit-commands posture | F10 | 1 d | low | Off by default; the risk is stated in the panel, not only in the README |

### Later — strategic

| # | Item | Finding | Effort | Risk | Done when |
| --- | --- | --- | --- | --- | --- |
| 25 | Adapter capability manifest; `prompt_builder` composes refusals from it **and** calculators gate on it | F12, TODO 3 | 3 d | high | iRacing behaviour byte-identical; a synthetic adapter declaring brake temps changes the refusal *and* the calculator |
| 26 | Capability profile per eval record | F12, F9 | 0.5 d | low | An LMU-profile record reporting brake temps is not graded as a hallucination |
| 27 | Decide the prompt-caching question: measure (item 6) → either drop it, change the default model behind a re-run floor check, or gate markers on a per-model minimum | F5 | 1 d | med | `cached_tokens > 0` observed live, or the item is closed in `TODO.md` with the 4,096-token reason recorded |
| 28 | Route every user-facing string through one lookup; note in the README that `language` affects recognition and voice only | F11 | 1 d | low | No user-facing string literal outside the table |
| 29 | Le Mans Ultimate reader, on top of item 25 | TODO 3 | 10 d+ | high | As `TODO.md` specifies — and get a live session early |

---

## 5. What not to do

Three things current voice-agent practice would push you toward that would make
**this** app worse. Recording the reasoning so it does not have to be re-derived.

**Do not add VAD-based barge-in.** The 2026 literature is loud about turn
detection because most voice agents run open-mic, where deciding whether the human
meant to interrupt is genuinely hard — too sensitive and the agent stops
constantly, too conservative and it talks over people. Push-to-talk answers that
question with a physical button, on a rig with engine noise, wheel rumble and a
sim's own audio. Adding VAD would import the hardest problem in the field to
solve one AIRE does not have. Implement interruption on the *button* (F4) and
stop there.

**Do not move to a speech-to-speech model.** `TODO.md` already dropped
audio-to-multimodal on cost, and the reasoning holds, but the stronger argument is
architectural: a speech-to-speech model puts the model back in the audio path
end-to-end, which means it is also back in the *number* path. The deterministic
calculators, the terminal-tool short-circuit, `keyterms.py` and the verbatim
refusals are all things you would be giving up. This app's differentiator is that
it does not guess. Do not trade that for latency that streaming TTS (item 8) can
recover more cheaply.

**Do not queue questions.** The `_busy` lock declining a second question is
right and the README explains why: the driver has moved on by the time a queued
answer arrives. Interruption (F4) is the correct response to a press during
playback — *replace* the answer, never stack one behind it. The same rule applies
to proactive announcements (item 23): drop stale ones, never queue them.

**One more, specific to this repo:** do not fix any of the above by adding a rule
to the system prompt. `AGENTS.md` states the principle and it has held every time:
*prefer a structural fix over a stronger instruction.* F2, F7 and F8 all have
prompt-shaped fixes available and code-shaped fixes recommended. Take the
code-shaped one.

---

## Sources

Agent and API practice:

- [Context Engineering: A Practical Guide for AI Agents (2026) — Sourcegraph](https://sourcegraph.com/blog/context-engineering)
- [Context Engineering AI: How To Build Smarter LLM Agents In 2026 — mem0](https://mem0.ai/blog/context-engineering-ai-agents-guide)
- [LLM Agent Evaluation Metrics in 2026: Tool Calling, Task Completion, Reasoning, and Trace-Based Evals — Confident AI](https://www.confident-ai.com/blog/llm-agent-evaluation-complete-guide)
- [AI Agent Best Practices: Production-Ready Harness Engineering (2026)](https://medium.com/@tort_mario/ai-agent-best-practices-production-ready-harness-engineering-2026-guide-c1236d713fac)
- [Less Context, Better Agents: Efficient Context Engineering for Long-Horizon Tool-Using LLM Agents (arXiv)](https://arxiv.org/pdf/2606.10209)
- [OpenRouter — Prompt Caching](https://openrouter.ai/docs/features/prompt-caching)

Voice-agent practice:

- [Voice AI Barge-In and Turn-Taking: A 2026 Implementation Guide — Future AGI](https://futureagi.com/blog/voice-ai-barge-in-turn-taking-2026/)
- [Voice Agent Interruption Handling: Barge-In, Backchannels, and Turn Detection — Hamming AI](https://hamming.ai/resources/voice-agent-interruption-handling-runbook)
- [Real-Time vs Turn-Based Voice Agents 2026 — Softcery](https://softcery.com/lab/ai-voice-agents-real-time-vs-turn-based-tts-stt-architecture)
- [Voice AI Engineering 2026: Low-Latency Agent Design — QubitTool](https://qubittool.com/blog/voice-conversation-ai-agent-latency-architecture)
- [ElevenLabs — Latency optimization](https://elevenlabs.io/docs/eleven-api/guides/how-to/best-practices/latency-optimization)
- [ElevenLabs — Generate audio in real-time (WebSockets)](https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/realtime-tts)
- [Best Low-Latency TTS APIs in 2026: TTFA, P99 and Pipeline Impact — Gradium](https://gradium.ai/content/best-low-latency-tts-apis-2026)

Category comparison:

- [The Best Sim Racing Apps You Need in 2026 — Coach Dave Academy](https://coachdaveacademy.com/tutorials/the-best-sim-racing-apps/)
- [Crew Chief vs DRE in 2026 — Digital Race Engineer](https://www.thedigitalraceengineer.com/dre-vs-crew-chief/)
- [Apex Boss — AI iRacing Crew Chief](https://www.apexboss.net/crew-chief-iracing/)
