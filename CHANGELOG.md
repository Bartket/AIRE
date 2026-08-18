# Changelog

All notable changes to AIRE are recorded here. Newest release first.

Entries are grouped under **Added**, **Changed**, **Fixed**, or **Removed**.

## Unreleased

_Nothing yet._

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
