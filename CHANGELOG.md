# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2.0.2] - 2026-10-18

Status-pill fix: the dashboard header claimed "live" even when nothing was
streaming, and "nothing streaming" could never appear at all.

### Fixed

- **The status pill now tells idle from live.** It read the snapshot's
  `status === "ok"` as "live", but `ok` deliberately covers an idle service
  too (idle is healthy, not degraded), so with no radio attached the pill
  said "live" while nothing streamed - and "nothing streaming" was
  unreachable, living only on the `degraded` branch, which needs a session to
  be wrong. The pill now reads the session count: no sessions shows "nothing
  streaming" in neutral grey, a healthy snapshot with sessions shows "live",
  a `degraded` snapshot shows "nothing streaming" in amber, and failed polls
  still show "no contact" in red. "No contact" is reachable even though the
  page is served by the same process: it appears the moment the service stops
  or dies while the page is already open. Verified in a real browser against
  a live server: idle, a real station streaming, and the server killed
  underneath the open page.
- **The pill's description is a real tooltip again.** It used a native `title`
  attribute - delayed about a second, drawn by the browser, and the
  `cursor: help` convention this page rejects renders as a literal question
  mark - so it was effectively unreadable. It now uses the same
  `data-tooltip` bubble as every other description, is focusable with the
  keyboard, and says why the pill reads what it reads.
- **`/health` documents `status` as the field behaves**: `ok` also covers an
  idle service, and `degraded` also covers station listeners that have
  nothing to play - not only "every session has died".

### Testing

- The pill's decision is now one pure function, `statusFor`, and a new test
  runs that exact source under Node through all six states it can report
  (connecting, no contact, idle, live, silent listeners, every session dead).
- Two new cases in `scripts/verify-web-guards.py` prove the new guards bite:
  the idle branch deleted (the reported bug) and the pill describing itself
  with a native `title` again. All 15 web guards and the other three guard
  scripts (7 + 20 + 7 mutations) go red as expected - 312 tests in total.

## [2.0.1] - 2026-10-18

Playback-continuity release: fixes the two audible defects reported on
transcoded streams - a jump forward at tune-in and intermittent ~2 second
repetitions - plus the pass-through equivalent of the startup jump and a pacer
edge case.

### Fixed

- **Startup audio is no longer discarded on transcoded streams.** FFmpeg was
  invoked with `-fflags +nobuffer`, which throws away the audio used while it
  probes the input. Measured with FFmpeg 7.1 on a 60 s AAC/MPEG-TS source fed
  through stdin exactly as the pipeline feeds it: `+genpts` alone decodes
  60.02 s, adding `+nobuffer` decodes 54.80 s - over five seconds silently
  gone, heard as a jump forward when a listener tunes in. `+genpts` is kept.
- **Reconnecting listeners no longer replay audio they already heard.** A radio
  that re-GETs a warm session was primed with the full preroll reserve -
  measured as a bit-identical 9.2 s replay on every reconnect, which is the
  reported "~2 s repeated every now and then". Queue items now carry their
  production timestamp and each `(client, User-Agent)` resumes from a
  join-floor of the last audio actually delivered instead of rewinding.
  Verified on the Pi with a three-listener reference capture: 0.000 s gap and
  no overlap across a reconnect. First-time listeners still get the full
  preroll.
- **Pass-through startup no longer drops segments.** Once the demuxer reported
  ready - typically after the first segment - the rest of the startup backlog
  was deleted from the work list. The tracker had already advanced past those
  segments, so they were never fetched again: up to a ~10 s jump at tune-in.
  All pending segments are now fetched and fed in order.
- **`_Pacer.wait` honours its deadline when one delay exceeds the 5 s sleep
  cap.** It used to sleep once and send early; it now sleeps in capped slices
  until the deadline.

### Changed

- Listener attach behaviour is unchanged for new clients; only a
  `(client, User-Agent)` pair with a previous connection resumes from its
  floor. Subscriber queue items are `(produced_at, piece)` tuples (the
  end-of-stream sentinel remains `None`).

### Testing

- New regression tests for each fix: the FFmpeg command flags, the
  three-segment pass-through backlog, join-floor behaviour (rejoining client,
  brand-new client, overlapping double-connect) and the pacer sleep cap - 311
  tests in total, plus the four guard scripts.
- The playback guard's "pacer stops pacing" mutation was updated to match the
  new sleep loop so it keeps proving the test bites.

## [2.0.0] - 2026-10-17

First v2: the single-file, three-thread v1 rewritten as a single-process
asyncio service with bounded queues everywhere, a pure-Python MPEG-TS demuxer
for the FFmpeg-free pass-through path, per-listener pacing with drop
isolation, a dashboard with health metrics, and a systemd installer for the
Raspberry Pi.

[2.0.2]: https://github.com/clifarr/HLS-Radio-Converter/compare/v2.0.1...v2.0.2
[2.0.1]: https://github.com/clifarr/HLS-Radio-Converter/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.0.0
