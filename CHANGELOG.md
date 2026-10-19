# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2.1.1] - 2026-10-19

A deploy that changes the dashboard now actually reaches the browser.

### Fixed

- **Static assets and pages are revalidated instead of being cached by
  heuristic freshness.** Reported: after 2.1.0 was deployed, the Engine column
  still read `transcode · mp3`. The server was serving the new script the whole
  time; the browser was never asking for it. `StaticFiles` sends `last-modified`
  and `etag` but no `cache-control`, and a response with no explicit policy is
  cached for roughly 10% of the file's age, so a script fetched while the file
  was a day old could be reused for hours without a single request. Both assets
  now carry `cache-control: no-cache`, which means "revalidate before reusing"
  (the conditional request is answered 304, no body), and the HTML pages carry
  it too, so the page cannot keep pointing at a previous release's assets.
- **Asset URLs are versioned** (`/static/dashboard.js?v=2.1.1`). `no-cache`
  only helps browsers that honour it; a query string that changes with the
  release makes an upgrade a different URL for every cache in the path,
  including a phone's webview or an offline copy.

### Testing

- The static response must carry `cache-control: no-cache`, an `etag`, and
  answer a conditional request with 304 (a full 200 on every page load is a
  cost the Pi should not pay). Both pages must request the assets at the
  current version and must themselves be non-cacheable. 327 tests in total.

## [2.1.0] - 2026-10-19

Dashboard-clarity release: the Engine column says what it means, the two
throughput figures are read as a pair, and the header stops looking like an
unstyled list.

### Changed

- **The Engine column shows only the engine** (`passthrough`, `transcode` or
  `starting…`). It used to append a codec built as
  `detected_kind || detected_codec` - the *source* kind first, the *output*
  codec as a fallback - so one slot meant two different things: a station with
  ADTS AAC inside a transport stream read `transcode · adts`, while one serving
  bare `.aac` segments read `transcode · mp3` because nothing had been detected
  and the fallback named the output. Neither told a listener anything
  actionable, and the first looked like it contradicted the second. What the
  column now answers is the only question it was ever read for: is FFmpeg
  running? The codec fields remain in `/metrics.json` as diagnostics, and
  `detected_kind` still drives the `codec=auto` Content-Type.
- **Received and audio-sent share one tile.** They were two tiles in an
  `auto-fit` grid, so whether they landed side by side depended on the column
  count - at an ordinary desktop width the row broke exactly between them,
  separating the two figures a reader compares. They now live in one tile that
  spans two grid cells, so no column count can split them; below ~330px the
  pair falls back to a single cell with both halves still side by side.

### Added

- **The header marks the current page.** The links were plain text at body
  size, which read as an unstyled list next to the title. They are now chips in
  the same visual language as the tiles and the status pill, and the current
  page is marked with `aria-current="page"` - styled, and announced to a
  screen reader.

### Testing

- New guards: the throughput figures must resolve to the *same* `.tile`
  ancestor (a text search cannot tell one tile holding both from two adjacent
  tiles, which is the bug); `engineLabel` is driven under Node with real
  snapshot shapes and must ignore both codec fields; each page marks its own
  nav link and styles it. 324 tests in total.

## [2.0.3] - 2026-10-19

Song-titles and rejoin-continuity release: standard HLS timed metadata now
reaches the radio's display, and the recurring silence/skip loop on reconnect
is fixed.

### Added

- **ICY song metadata (on by default; `icy_song_metadata = false` turns it
  off).** Every 16 000 bytes of the outgoing stream are followed by a
  heritage-style ICY metadata frame - `StreamTitle='artist - title';` - which
  is the now-playing line Frontier-Silicon radios display (VLC, ffplay and
  mpg123 show it too). The title comes from the standard mechanism for this in
  HLS: ID3 timed-metadata tags carried inside the segments (TEXT frames
  holding the station's songInfo JSON, with the TIT1 text frame as fallback),
  extracted once per segment and kept as the last known title between songs.
  Stations that ship ID3 - Radio 105 verified live end to end, from segment to
  radio display. A station whose segments carry no ID3 at all (radiom2o:
  proven to be a bare AAC transport stream with no metadata PID and no tags)
  correctly sends empty titles, because there is nothing standard to send. The
  current title is also exposed as `now_playing` under `pipeline` in
  `/metrics.json`, and the metadata bytes are counted into the output
  counters.

### Fixed

- **The recurring "silent for a second, then skips a second" loop is gone.**
  A radio re-joining exactly at the production edge was primed with zero
  chunks and starved until the next segment deadline - up to ~3 s of silence
  on a 3 s cadence, alternating with skipping the unplayed tail it should
  have resumed from. The join floor now overlaps that discarded tail by
  `handoff_overlap_seconds` (default 1.5, configurable 0-10), so a rejoin
  always has audio to play immediately. Set it to 0 to restore exact,
  piece-exact resume.

### Testing

- 321 tests plus all four guard scripts (20 + 15 + 7 + 7 mutations). Verified
  live on the Pi: a listener re-joining at the production edge primes ~30-35
  chunks instead of 0 (60/60 samples no longer starve), and ICY frames decoded
  off the live wire carry real titles from the ID3 extraction.

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

[2.1.1]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.1.1
[2.1.0]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.1.0
[2.0.3]: https://github.com/clifarr/HLS-Radio-Converter/compare/v2.0.2...v2.0.3
[2.0.2]: https://github.com/clifarr/HLS-Radio-Converter/compare/v2.0.1...v2.0.2
[2.0.1]: https://github.com/clifarr/HLS-Radio-Converter/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.0.0
