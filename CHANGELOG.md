# Changelog

All notable changes to AIRE are recorded here. Newest release first.

Entries are grouped under **Added**, **Changed**, **Fixed**, or **Removed**.

## Unreleased

### Changed

- The engineer talks like one on the answers it gives straight from its own
  calculations: fuel, position, gaps, pit window, status and the car's
  resources. Gaps are said to the tenth ("a second and a half", not "one
  point five one seconds"), rivals by surname ("Rossi", not "M. Rossi"), and
  a healthy tank is a verdict and one margin ("Fuel's fine to the end, a lap
  and a half spare") instead of four figures, two of them the same fact in
  different units. Ask "how much fuel" and the litres are still there. Fuel
  margins only ever round down, and a shortfall keeps its exact figures.
- The engineer no longer calls you by name in the middle of a session. It
  did in a third of answers, measured; it is kept for the debrief.
- Lap times are given to the tenth in a race and to the hundredth in
  practice and qualifying, never to the thousandth.
- Practice and qualifying no longer sound like a race. "How are we looking"
  and "where am I" give your place on lap time and how far off the best lap
  in your class you are ("You're P4 on lap time, 3 tenths of a second off the
  session best") instead of a bare place. The cars either side are traffic
  you are catching or being caught by, not rivals "pulling away", and the
  engineer is told outright that practice has no pit window, no fuel to the
  end and no battles.
- The rules that stop the engineer inventing anything are no longer part of
  the System Prompt box, where clearing or rewriting it switched them off.
  They are always added. A blank box now means the default prompt, and
  **Restore default prompt** brings back the shipped one. The default is
  half its old length, with rules the app already enforces taken out.
- The answers the model puts into its own words follow the same radio rules
  as the ones read straight from the calculators: in a race, gaps, lap times
  and differences to the tenth, and rivals by surname. "How much quicker is
  Rossi?" came back as "0.85 seconds" in 6 of 20 runs, because the figure
  handed to the model beside the sentence still had its hundredths; the
  model now gets every figure rounded the way it should be said. Practice
  and qualifying keep their hundredths. A sentence and its own figures now
  round the same way, so "9 tenths" is never stored as 0.8.
- The telemetry the model reads now gives how far your best lap is off the
  session's fastest, worked out, instead of two lap times to subtract. Asked
  how much quicker the fastest car was, the model sometimes did the
  subtraction itself and got it wrong ("point eight" for nine tenths). Gaps
  in it are rounded exactly as the engineer's calculators round them.
- Earlier questions replayed to the model now say how long ago they were
  asked ("3 laps ago"). An old answer's figures used to sit in the
  conversation looking current, and only an instruction said not to reuse
  them.
- Natural-language pit commands ask the model for a fixed reply shape on
  OpenRouter instead of relying on temperature 0. Claude Haiku 5.5 does not
  take a temperature, and OpenRouter dropped it without saying so. The reply
  is still parsed and checked exactly as before.

### Added

- Each exchange in the log shows the session type iRacing reported and what
  the engineer took it for (e.g. `Practice → practice`). An unrecognised or
  empty type is treated as a race on purpose, and until now nothing showed
  when that had happened.
- Warnings and errors are written to `aire.log` beside `config.json`.
  `AIRE.exe` has no console, so until now every one of them went nowhere,
  including the ones meant to be loud because they had already failed in
  silence once. Only warnings and errors are kept. Your questions and the
  engineer's answers are not, so the exchange log still lives in memory only.
- `python -m ai_race_engineer.evals --floor 5` — the model check as a command.
  Five questions that reach the model, each asked five times on simulated
  telemetry, each graded on every run being clean rather than the best one.
  It calls the configured model, so it costs a few cents. `--effort` sends a
  reasoning effort, to measure one before changing it.

### Fixed

- "Am I under investigation?", "did I get a penalty for that?" and every
  other race-control question outside eight exact phrasings went to the
  model with nothing to call, and it read the raw flag lines itself. Race
  control is now one of the engineer's calculators, spoken as written. With
  a clean flag it says so: "Green flag, no penalty showing." iRacing has no
  signal for an investigation, so the engineer never suggests one.
- With simulated telemetry, "how far am I behind Silva?" — any gap to a
  named car — answered "I can't place you in the order right now". The
  simulated field never said which car was yours. Live telemetry was not
  affected.
- An answer could be spoken with the model's own draft and its correction
  in it — "Wait, I need to keep that to one sentence" — read out between two
  versions of the answer. Only the final one is spoken now.
- `AIRE.exe --server` never started. It exited at once with nothing
  logged, because the build has no console and the web server asked the
  console whether it could show colours. Desktop mode had the same flaw
  and only worked because a library it loads first happened to cover it.
- A press that came back with no words was met with silence. Taps are
  already ignored, so this is a muted or wrong microphone far more often
  than not, and you could ask into it all stint without finding out. The
  engineer now says "I lost that one, say again."
- "How's my fuel?" on the Simulated source, the first test the README
  suggests, answered "I've got no fuel reading". The simulator still gave
  fuel only as a percentage, which the fuel calculators no longer read. It
  now gives litres, a tank size and a burn per lap.
- A transcription that failed was met with silence. You asked, heard the
  roger beep, and then nothing, so an ElevenLabs outage sounded exactly like
  a slow answer. The engineer now says "I have comms issues right now", as it
  already did when the model failed.
- The settings panel could freeze the engineer. Loading voices, previewing
  one, listing or testing models, and listing devices or controllers all
  waited on the provider or the device on the same loop that hands a
  push-to-talk question to the pipeline and keeps telemetry fresh. A
  one-second voice list stalled everything for that second; a slow provider
  could do so for up to thirty. They now wait off to one side.
- The first answer after starting AIRE no longer waits for the radio effect
  to load. It now loads at startup; that wait measured about 0.3 s, and
  1.3 s on a cold start.
- Choosing a voice when `config.json` could not be written looked like it
  worked, and the next launch spoke in the old voice. It now reports the
  failed write, as every other saved setting does.
- Switching the telemetry source, or quitting, while a reading was in
  progress could fail that reading with an error and traceback that looked
  like a telemetry bug. The reader now finishes the reading before it shuts
  down.
- "How's my fuel?" — and every other question asked with a contraction —
  never reached its deterministic answer when speech recognition kept the
  apostrophe. "How's" became "how s" while every phrase table spells it
  "hows", so the most-asked question on the radio went to the model instead.
- An answer could stop mid-sentence on air. A model that thinks before
  speaking can spend nearly its whole budget thinking and be cut a few words
  in; only a completely empty answer used to be retried. A sentence cut short
  by thinking is now retried too.

### Changed

- "Status" is about the car, and when nothing is wrong the answer is "Car
  looks fine." When something is wrong it names only that, most urgent
  first: a penalty, being towed, a warning light, a leak, damage, fuel that
  will not last. Normal readings are not mentioned; ask for one and you get
  it. "How are we looking", "how's it going" and "sitrep" give the place and
  the gaps, plus the same short list when something needs acting on. Flags
  you can see on your own dash are no longer read back.
- The engineer is told how to answer a broad question: pick what most needs
  your attention, or say all's well in a few words, and give detail when you
  ask for it. The 25-word limit is now a ceiling rather than a target, rivals
  are named but you are not addressed by name in every call, and the model is
  handed a one-line verdict on the car instead of a dozen readings to sift.

- "How far am I behind the leader?" could be answered "I lost that one, say
  again" — five times in five on Claude Haiku 4.5. The model gave its answer
  in the same turn as calling a calculator, then said nothing after the
  result because it already had, and AIRE only ever spoke the last turn. A
  model that falls silent after its tool now gets the calculator's own
  sentence spoken, never its own earlier text, which was written before the
  calculator ran and so carries figures nothing has checked.

- A model that thinks without showing its reasoning could answer with
  nothing. AIRE retries with more room when a model spends its whole budget
  thinking, but recognised that only from the reasoning text — and Claude
  Haiku 5.5 through OpenRouter withholds the text and reports a count. A
  final answer at the one-sentence budget came back empty and the driver
  heard "I lost that one, say again". The count now counts.

- Asked about tyres on track, the engineer read out a dozen figures that were
  frozen at the last pit stop. iRacing only refreshes tyre wear and
  temperature for outside apps in the pit box, and AIRE treated the refresh
  in the box as proof of a live feed, so the out-lap after every stop — and
  the first two and a half minutes after starting the app — presented
  last-stop figures as current. Tyre readings now count as current in the
  box, or if they are actually seen to move on track. Otherwise the engineer
  is given nothing to recite and says it can't see the tyres out there. In
  the box it gets the spread worked out — lowest tread, highest carcass —
  rather than twelve numbers.
- "Tyre age" is no longer stated. It was the stint lap count copied to all
  four corners, so after a stop that did not change tyres it was wrong.
- An unedited system prompt is no longer written into the config file. It
  froze at whatever shipped the day the file was first saved, so later fixes
  to the prompt never reached an existing install. An edited prompt is kept.

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

- The default model is now Claude Haiku 5.5. Over five runs of each probe —
  invented corner names, mental arithmetic, skipped calculators, inventing a
  trend, reading out stale tyre figures, breaking character, running past a
  sentence — it failed none of 125 checks, answered faster, and costs about a
  tenth of Haiku 4.5 per question.

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
