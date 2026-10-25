# Changelog

All notable changes to AIRE are recorded here. Newest release first.

Entries are grouped under **Added**, **Changed**, **Fixed**, or **Removed**.

## Unreleased

### Fixed

- Asking for "status" got a recital of the whole session. The question names
  no topic, so the model was handed everything and read most of it out —
  measured at 41 to 71 words on a cheap model, with a closing trend it had
  never been given stated as fact. "Status", "update", "sitrep" and "how are
  we looking" now get the place and the gaps in a dozen words, plus one short
  line only when something needs acting on: a flag, a penalty, or fuel that
  will not reach the end. After the flag they are left to the debrief.
- "What's the gap ahead?" could be answered "I can't find ahead out there."
  Two calculators did not understand "ahead" or "behind" — the most natural
  thing a model passes them — and the two that did kept separate lists that
  had drifted apart. All four now share one.

### Changed

- Moved to ElevenLabs' current speech models. Both the ones AIRE shipped with
  have been retired: `scribe_v1` is replaced by `scribe_v2`, and
  `eleven_turbo_v2_5` by `eleven_flash_v2_5`. Neither costs more — Flash and
  Turbo are billed in the same tier, and Scribe v2 is the same price per hour
  as v1 — and both replacements are faster or more accurate. A retired model
  answers until it does not, and a model that stops answering mid-race is a
  silent failure, so a saved config naming one is moved across on load rather
  than left on it.
- The model dropdowns now list only models that still exist, and say which
  cost twice as much per character. Eleven v3 is selectable for anyone who
  wants it; it is not the default because it is dearer and, on the single-shot
  path AIRE uses, slower than Flash.

### Fixed

- Asking about fuel or tyres could be answered with a pit-command refusal,
  on the shipped default where pit commands are switched off. The classifier
  that reads natural pit instructions ran before the setting was checked, so
  ordinary radio like "give me an update on fuel" bought a model round trip
  it could never use, and where the classifier misread the question the
  driver was told "I couldn't safely interpret that pit request" about a
  feature they had never enabled. An explicit "pit stop ..." still says the
  setting is off; everything else is now answered as the question it was.
- The spoken fuel plan ran to 76 words on a timed race that was short on
  fuel — around half a minute of radio, spoken across several corners, most
  of it answering something that had not been asked. Nothing was holding it
  to a length: the 25-word rule lives in the system prompt, and this string
  reaches the driver without a model in the path. It is now a headline plus
  only the clauses that fit the radio, worst case 28 words. The full text is
  unchanged for the model, which still receives every clause.
- A question about the car ahead could arrive with no car ahead in it. The
  telemetry block is compacted for single-topic questions on the assumption
  that a calculator can fetch back whatever was cut, which is not true with
  tool calling switched off or after a provider has rejected it. The block is
  no longer compacted on that path.

### Added

- Stage timings for each exchange — speech recognition, the model, synthesis,
  the radio effect, ducking and playback, plus time to first audio — in the
  diagnostic trace and on the status panel. The pipeline previously had no
  timing in it at all.
- Token counters per exchange in the diagnostic trace, including prompt-cache
  reads and writes, so the cost of a question is a reading rather than an
  estimate.

## 0.2.0

### Fixed

- A position iRacing had not classified was reported as a place. The sim
  publishes `0` for a car it has not placed — not only before the start —
  and the telemetry block wrote that out as "P0" beside a running order
  that listed the driver second, so asked where they were the engineer
  answered pole. An unclassified car now has no position at all, and the
  block says so rather than leaving a number-shaped blank. Where the live
  channel drops out, the session's own published results table is used
  instead, which is the order CrewChief reads the two sources in.
- Qualifying ending announced itself as "RACE OVER — finished P2", with the
  race still to come, and compared the driver's lap against "the fastest lap
  of the race".
- Every public link pointed at a private repository, so the download link in
  the README, the clone command, the in-app links and the OpenRouter referer
  header were all dead for anybody who is not the author.

### Added

- The engineer now knows what a qualifying session actually gives the
  driver. It read the session clock as their budget, so it offered eight
  more minutes of running to a driver who had two laps left. iRacing
  publishes the format per session — Lone against Open Qualify, the lap
  allowance, and whether the result is a best lap or an average — and all of
  it is now read rather than assumed.
- `session_status`, a calculation for "how long have I got" and "how many
  laps left", which decides in Python whether the laps or the clock run out
  first instead of leaving the model to pick between two lines of telemetry.

### Changed

- A qualifying lap allowance is no longer rendered as a race distance;
  `SessionLaps: 2` in qualifying used to print as "Lap 3 of 2".

## 0.1.0

First release. Everything is new, so there is no per-change list here —
subsequent releases will record their changes above.
