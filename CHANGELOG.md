# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2.2.2] - 2026-10-21

Hardening release. Eleven defects found by an external review of the 2.2.x
work, each reproduced before it was fixed and each now covered by a
regression test that fails against the old code.

Two of them were in the code that 2.2.1 shipped, and one of those was
audible: a station whose title uses a typographic apostrophe had its
metadata truncated at the apostrophe.

### Fixed

- **A title with a typographic apostrophe no longer truncates the
  metadata.** `It’s a Sin` was sent as `It's a Sin` - with an **ASCII**
  quote, which is the delimiter of the `StreamTitle='...'` value. The radio
  read the value as `It` and treated the rest as junk. The sanitiser mapped
  the curly quotes to ASCII ones, undoing the very rule it exists to
  enforce. The typographic quotes are now mapped to the acute accent, and
  the ASCII-quote replacement is applied **last**, so no substitution can
  put one back.

- **The end of a stream is no longer trimmed to a frame multiple.** The
  tail was cut to a whole number of the *first* frame's length, which is
  wrong twice over: MP3 frames alternate between two sizes when the sample
  rate needs a padding byte, and ADTS frames are variable-length by
  definition. For two 44.1 kHz frames of 626 and 627 bytes, `1253 % 626`
  is 1 - so one valid byte was deleted from the end of every stream.

- **A station that changes codec no longer goes silent.** A pipeline
  restart can bring up a source with a different codec, and everything the
  session knew about the frame grid - the frame size, the grid, and the
  bytes held back for a frame that would never complete - belonged to the
  *old* format. With a stale grid the session delivered **zero** bytes: the
  radio went quiet and stayed quiet. The grid is now reset when the format
  changes, and the old format's bytes are dropped rather than handed over
  as garbage in front of the new format's first frame.

- **An MP3 source asked for as ADTS is now encoded, not copied.** The
  output codec and the *copy* decision were the same flag, so
  `output_codec = "adts"` forced `-acodec copy` for every source. An MP3
  source was copied straight into an ADTS container, which ffmpeg accepts
  and then fills with nothing a radio can decode:

  ```
  [adts @ 0x15a004480] adts muxer supports only codec aac for type audio
  [out#0/adts @ 0x15a00f900] Could not write header (incorrect codec parameters ?)
  ```

  Measured: **0 bytes** of output before, a valid 73 KB ADTS stream after.

- **The MPEG-TS demuxer no longer grows without bound.** The PSI section
  assembler kept the `0xFF` stuffing bytes that pad a PAT packet, so the
  buffer grew by a whole packet on every packet - measured at 171, 359,
  547, 735, 923 bytes over five packets - and the section was never found
  again. The assembler is now told whether the payload starts a section
  (the pointer field only exists when the PUSI bit is set) and discards
  the stuffing.

- **A declared discontinuity is now read from where it actually is.** The
  MPEG-TS `discontinuity_indicator` lives in the **adaptation field**, at
  `packet[5] & 0x80`. The code read the packet header's
  `adaptation_field_control` instead, which says *whether* an adaptation
  field is present, never whether there is a discontinuity. A stream that
  was cut and rejoined was therefore counted as a silent gap. A
  retransmitted packet - the same counter again - is now recognised as a
  duplicate rather than a gap, and does not tear down the PES packet.

- **A byte-range request is now checked.** A `Range` request answered with
  `200 OK` and the whole file is not an answer to the question: accepting
  it splices the beginning of the file into the middle of the stream. The
  response must now be a `206` with a `Content-Range`, and the body must
  be exactly as long as the range says.

- **A stream that opens with a preamble is still aligned.** The "is this
  MP3 at all?" test looked only at the first 4096 bytes, and the verdict
  was taken on the *first* chunk - as little as 578 bytes. A station that
  opens with a few hundred bytes of something else was declared "not MP3"
  for ever and was never aligned, even though it was MP3. The verdict now
  waits for 1 KB of evidence.

- **A large ADTS stream can be aligned at all.** The search budget was
  `4 * FRAME_HINT` = 2304 bytes, a number taken from the *MP3* frame size
  and applied to every format. An ADTS frame can be up to 8191 bytes, so
  two of them do not fit - and the search was given up on before a frame
  *pair* could ever be found. The budget is now per-format: two
  maximum-size frames of the format being searched for.

- **`share_sessions = false` now does what it says.** The setting was
  declared, configurable and never read: every radio on a station shared
  one upstream pipeline whatever the setting said.

- **A stream that is not the format we expected is still handed over.**
  The search gives up on a stream with no sync word in 1 KB of audio
  rather than buffering it, and hands it over untouched.

### Changed

- The 2.2.1 release note claimed that a metadata block landing mid-frame
  "necessarily desynchronises the ICY byte counter". That is too strong:
  the metaint counts *audio* bytes, and a block may legally land in the
  middle of a frame. Measured on a live capture, **36 of 40** blocks did
  split a frame, and the reassembled audio was byte-identical. The fault
  was the **anchor mismatch** - the server's grid starts at byte 0 of the
  response, the radio's at its first decoded frame - not the block
  boundary itself. The note has been corrected.

### Testing

- Twenty-one new regression tests, each verified to **fail against the
  code it guards** and pass against the fix. A test that passes both ways
  guards nothing.
- The four CI gates pass: `ruff format --check`, `ruff check`, `ty check`
  and 401 tests.
- The guard scripts were re-run: 25 playback, 20 web and 7 container
  defects, all still caught.

## [2.2.1] - 2026-10-20

Garbled-title release: the radio's display no longer turns to binary noise
because the audio it is sent starts in the middle of an MP3 frame.

### Fixed

- **The stream now starts on an MP3 frame boundary, so the radio's display
  shows the title instead of binary noise.** This is the cause of the
  "garbled text and glitching audio" that had been reported intermittently
  for months - on a station that does not even carry song names, which is
  what finally ruled out every explanation that involved the metadata.

  A radio is **two independent state machines over one byte stream**: an MP3
  decoder that looks for frame syncs, and an ICY metadata parser that counts
  `metaint` audio bytes and then reads a block. They have to agree about
  where the stream starts. They did not, because the audio was cut into
  8192-byte pieces and 8192 is not a multiple of a 576-byte MP3 frame
  (`8192 % 576 = 128`), so the phase drifted and every connection began at a
  random point *inside* a frame.

  Measured on the radio's own TCP traffic, the first frame of each connection
  began at offsets 320, 384, 262, 390, 517 and 454 - never reliably 0. The
  radio then counts bytes from its own first frame while the server counts
  them from the start of the response, so the two are out of step: the radio
  reads its "length byte" out of the middle of an audio frame and gets
  215, 200, 244, 91, 53 ... followed by binary. That is what "garbled
  characters in the title area" looks like from the inside.

  The offset is a **constant**, so it does not heal. It stays garbled for the
  life of the connection and clears only when the radio reconnects and
  re-anchors - exactly as reported.

  Note on the mechanism: the metaint counts *audio* bytes, and a metadata
  block may legally land in the middle of a frame - 36 of the 40 blocks on a
  live capture did, and the reassembled audio was byte-identical. So the
  block boundary itself is not the fault. The fault is the **anchor
  mismatch**: the server's grid starts at byte 0 of the response, and the
  radio's starts at its first decoded frame. When those two agree, a
  mid-frame block is harmless; when they differ by a constant, every block
  the radio reads lands in the wrong place.

  The audio is now aligned where it is produced: the frame grid is found once
  (by parsing the frame header, not by guessing), and every piece is cut on a
  frame boundary. Verified against the radio's real captured traffic: the
  stream now starts at offset **0**, **all 338 pieces** start on a frame
  boundary, the stream is **one unbroken chain of 5062 frames**, and at most
  one frame is lost - the partial one at the very front, which the radio
  discarded anyway.

  The grid is the one of the format the listener is **actually** being sent,
  which is not always the format the configuration names: a station with
  `output_codec = "auto"` forwards its source untouched, so it sends MP3 to
  the radio whatever the setting says. ADTS AAC has its own frame grid, and
  a radio resynchronises on that one too, so it is aligned as well - on its
  own frame boundaries, which are variable-length and therefore read from
  each frame's header.

  A stream whose format is not known yet - before the first segment arrives
  - is passed through untouched: aligning to a *guessed* grid would throw
  away audio for nothing.

  A piece is now a whole number of frames, so it is very slightly larger
  than the configured `chunk_size`: 15 frames = 8640 bytes rather than 8192,
  because 8192 is not a multiple of 576. The queue bound is counted in
  *pieces*, so a listener holds 128 * 8640 bytes instead of 128 * 8192 - 5 %
  more of an already-small buffer - and every piece is now a valid decoder
  resynchronisation point.

### Testing

- Six new regression tests cover the defect from both sides: that a new
  listener starts on a frame boundary, that *every* piece it receives does,
  that the whole stream it receives is one unbroken chain of frames (aligning
  the pieces but not joining them up would still make the radio lose sync at
  every boundary), that the alignment survives FFmpeg's 578-byte pipe reads
  (a single one of those cannot contain a frame *pair*, so an alignment
  search that looked only at the current read would never find the grid), and
  that an ADTS stream and a forwarded MP3 stream are aligned to their own
  frame grids.
- The guards were mutation-checked: 25 reintroduced defects, all caught.
  Three of the guards found real gaps while this was being written - the
  alignment was keyed off the *setting* rather than the format actually
  being sent, which left every ADTS output and every `output_codec = "auto"`
  station unaligned.

## [2.2.0] - 2026-10-20

Playback-continuity release: a radio no longer gets a corrupt MP3 stream when
the pipeline restarts, and a song title with an apostrophe in it arrives
whole. The tooltips stay put on a phone, and a commit that CI would reject
cannot be made in the first place.

### Added

- **A pre-commit hook that runs the CI checks before the commit.** Reported
  repeatedly: a commit was pushed, CI went red on something a local run
  would have caught in seconds, and the commit had to be undone and redone.
  The hook runs *the same commands CI runs* - ruff format, ruff check, ty,
  the installer tests, pytest, the end-to-end self-test and the wheel build
  - so a commit that passes it is a commit CI will pass.

  The split is deliberate. The **commit** stage runs the fast test subset
  (`pytest -q -k "not stream"`, about 21s) plus the linters, the type
  checker, the installer tests and the wheel check. The **push** stage runs
  the full test matrix, the end-to-end self-test and a clean build. Charging
  45s to every commit is how a hook gets bypassed with `--no-verify`, and a
  bypassed hook is worse than no hook.

  The hooks are `local` rather than fetched from a `repo:` URL: every tool
  here is already a pinned dev dependency, and `uv run` resolves them from
  `uv.lock` - so a hook in its own isolated environment would check the code
  with *different* versions of the tools than CI uses. That is the same class
  of bug as a stale lockfile: a green check that does not mean what it says.

  It cannot fall behind CI either. `tests/test_metadata.py` compares the two
  and fails if a check is added to one and not the other, in either
  direction, so "add a CI job" now also means "add a hook" - and the test
  says so.

### Changed

- **The wheel's asset check is now a script, shared with CI.** The `package`
  job inlined a Python heredoc that looked inside the built wheel for
  `templates/` and `static/`. That check is now
  `scripts/verify-wheel-assets.py`, which both CI and the pre-commit hook
  call - so the two cannot drift apart, and the failure message says what
  would actually happen (the service starts, streams audio, and then serves a
  500 on its own pages) instead of just naming a missing file.

### Fixed

- **A radio no longer gets a corrupt MP3 stream when the pipeline restarts.**
  This is the glitch that was reported as "garbled characters as the song
  name, and then the audio cutting and glitching and doing weird noises" after
  a couple of hours of playing.

  ffmpeg's MP3 muxer copies the *input's* ID3v2 metadata into the output. On a
  station whose segments carry per-song ID3 (RTL 102.5, a tag chain of about
  1.5 KB ahead of every segment's audio) that became a blob of non-audio
  inside an MP3 stream, which a radio cannot skip. The output is a pipe, so
  ffmpeg cannot write the tag once at the start and fix it up afterwards - and
  when the pipeline restarted after a transient upstream fetch failure, the old
  encode's tail and the new encode's head were concatenated for the listener,
  which put the second encode's tag *in the middle* of the stream.

  One misaligned frame is enough: an MP3 frame may reference the previous
  frame's bits (the bit reservoir), so the damage is not local - it runs to the
  end of the stream. Decoding the spliced stream says so plainly:
  `Header missing` and `invalid new backstep -1`.

  The MP3 output now carries no ID3 tag at all (`-id3v2_version 0`). Measured
  over 15 kill-and-restart splices of a real station's segments: **15 of 15
  corrupt with the tag, 0 of 15 without it.** The title still reaches the
  radio, through the standard ICY metadata channel, which is what a radio
  actually uses for the song name.

  Only stations carrying per-song in-band ID3 are affected, which is why this
  only ever happened on one channel. A reconnect fixes it because it gets a
  fresh, unspliced stream on a fresh grid.
- **A song title with an apostrophe in it no longer arrives truncated.**
  `Fool's Garden - Lemon Tree` was sent as
  `StreamTitle='Fool's Garden - Lemon Tree';`, and the radio showed `Fool`
  followed by `s Garden - Lemon Tree';`. A radio ends the value at the *first*
  quote, exactly as a header parser would, and the rest of the title was
  parsed as further key=value pairs and rendered as junk. The quote is now
  replaced with U+00B4, the Latin-1 ACUTE ACCENT: a look-alike, one byte,
  renders on every radio, and does not terminate the value.
- **A song title is now Latin-1, so a radio can render it.** The metadata is a
  byte string and the heritage convention is ISO-8859-1, but the titles were
  being sent as UTF-8, so `£ MINI COUNTRYMAN` arrived as
  `Â£ MINI COUNTRYMAN`. Characters that *are* in Latin-1 (`£`, `È`, `€`) are
  now kept exactly; the typographic punctuation that is not in Latin-1
  (the curly quotes, the en and em dashes, the ellipsis) is transliterated to
  an equivalent that is; anything else becomes `?`.
- **A title too long for the block is now cut on a character boundary.** The
  240-byte payload limit could previously land in the middle of a multi-byte
  character and leave the radio decoding half of one.
- **The service no longer leaves a broken pipe behind when ffmpeg dies.** At
  the end of a test run - and, in production, at the end of a station that
  drops the connection - asyncio printed:

      Future exception was never retrieved
      future: <Future finished exception=BrokenPipeError(32, 'Broken pipe')>

  It was not a crash, but it read like one, and it was in every run.

  The cause was a long way from the symptom. asyncio's subprocess stdin is a
  `StreamWriter` whose protocol holds a `_stdin_closed` future; when the pipe
  breaks, asyncio puts the `BrokenPipeError` on that future. The only thing
  that ever retrieves it is `StreamWriter.close()`, which awaits the future
  through `_get_close_waiter()`. The pipeline closed ffmpeg's input on the
  happy path, but *not* on the error path - the one where the pipe had
  actually broken - so the exception sat on that future until it was garbage
  collected, and asyncio then complained about it.

  The input pipe is now closed on every path, and the close waiter is awaited
  so the exception is retrieved rather than merely stored. The service's own
  logs are unchanged; what is gone is the noise in someone else's.
- **A log line can no longer turn into "--- Logging error ---".** The
  handler installed by `setup_logging` bound `sys.stderr` once, when it was
  created. If `sys.stderr` was replaced afterwards - which is what a test
  harness does when it captures output - the handler kept writing to the old
  object; if that object was then closed, the write raised
  `ValueError: I/O operation on closed file` inside the logging module, and
  Python printed *that* instead of the message that was supposed to be
  logged. The handler now looks the stream up on every write, so the log
  follows `sys.stderr` wherever it goes.
- **Holding a finger down on a figure to scroll past it no longer opens its
  description.** A tap and a scroll are the same gesture until the finger
  moves, so the page could not tell them apart on the way *down* - it opened
  the description as soon as the finger landed, and the reader was left with a
  tooltip stuck on screen over a figure they were scrolling past. The browser
  draws the line for us on the way up: a tap ends with a `click`, and a
  scroll ends with `pointercancel` and no click at all. The description is
  opened by the click now, and the cancellation closes anything the finger
  landed on. This is what other sites do, and for the same reason.
- **A tap on the plain text around a figure no longer opens the description
  of whatever happens to be next to it.** A tap gives the element the focus
  as a side effect, and the focus was being read as the reader asking for a
  description. The click already carries that intent; the focus is now only
  for a keyboard.
- **A tapped description no longer vanishes the moment the finger lifts.**
  Reported: "the tooltips tend to disappear" - on a figure that changes every
  second it went almost at once, and on the stations list it went *always*;
  the only way to read one was to hold a finger down. The cause was the
  browser's touch events: a finger is "over" an element only while it is on
  the screen, and the pointer reports "out" the instant it lifts, with no
  `relatedTarget` - the pointer is destroyed along with the touch. The page
  read that as "the pointer left", so it closed the description the tap had
  just opened, after a single frame. On a touch screen only `pointerdown`
  now means anything; a `pointerout` from a finger means nothing at all.
- **The stations table no longer closes a description once a second.** The
  table is rebuilt from scratch every poll, which replaces the cell the
  reader had tapped, and the browser reports that as a `focusout` - so the
  description was closed by the very repaint that was meant to update it.
  The rebuild now announces itself, and the description is put back against
  the cell it belongs to. Descriptions are keyed by station and column rather
  than by DOM node, so they survive the rebuild; the *focus* is put back too,
  which stops a keyboard reader being thrown to the top of the page every
  second.
- **Scrolling no longer closes a description.** It used to: any scroll closed
  it, even though the element it described was still on screen - and on a
  phone the sideways scroll of the stations table is the very gesture that
  shows it. A description now *follows* its trigger, and is closed only when
  the trigger has gone off the screen.

### Changed

- **A pointer and the focus are tracked separately.** A mouse leaving a figure
  does not take the focus away from the element a keyboard reader is on, and
  tabbing away does not take the mouse away from the figure it is over. One
  "who owns the tooltip" variable could not tell those apart, and guessing
  wrong was the bug.
- **The stations table's cells are focusable.** A cell that carries a
  description and cannot be reached is a description only a mouse can read.

### Testing

- **The splice that corrupted the radio's stream is now covered by a test
  that runs the real ffmpeg.** `test_a_restarted_encoder_does_not_corrupt_the
  _mp3_a_radio_receives` encodes a real AAC fixture twice, splices the two
  encodes the way a pipeline restart does, and asserts that the result decodes
  with *zero* errors and carries no ID3 tag. A single clean encode is also
  asserted, so the test says which of the two halves broke if it ever fails.
  The guards were mutation-checked: removing the `-id3v2_version 0` makes this
  test fail, so it is a real guard and not a tautology.
- **The ICY title handling is covered on the cases that were seen breaking
  in the wild**: an apostrophe (the value must not truncate, and the radio's
  view of the block must end at the value), a Latin-1 character (`£` and `È`
  survive exactly), a character that is not in Latin-1 (it degrades to `?` and
  the block still parses), a title longer than the block (it is cut, the
  payload stays within 240 bytes, and the block still ends in `';`), and a
  title that changes between blocks (every block stays well-formed and the
  audio reassembles byte-for-byte).
- **The MP3 output is asserted to carry no ID3 tag**, and the ADTS output is
  asserted *not* to be affected by the suppression, so the flag cannot creep
  onto a container that should keep its metadata.
- **The teardown is asserted to be silent.** Two guards, one for each defect
  above, and both are checked against the *real* mechanism rather than a
  stand-in:
  - the pipe guard breaks a real pipe (a child that closes its stdin) and
    asserts that asyncio's close waiter is left with a *retrieved* exception,
    which is the exact flag `Future.__del__` checks before it complains;
  - the logging guard replaces `sys.stderr` - including replacing it with a
    closed stream - and asserts that the message still arrives.
  Both were mutation-checked: reverting either fix makes them fail.
- The rules that decide when a description shows are now one pure function
  (`tipAction`), and the suite drives it through every case - a tap, a lifted
  finger, a mouse over, a mouse leaving, focus arriving, focus leaving, and
  the table being rebuilt. Three more guards in
  `scripts/verify-web-guards.py` break the page deliberately and assert the
  suite notices.

## [2.1.2] - 2026-10-19

The dashboard fits a phone.

### Changed

- **The stations table becomes a card per station below 64rem** (1024px),
  which is where its eleven columns stop fitting. An eleven-column table cannot
  be squeezed into 390px, and the alternative - sideways scrolling on the one
  page whose whole job is to be glanced at - is worse than re-laying it out.
  The card is a small two-column grid of figures with the name above each value
  (`RADIOS` / `1`), not eleven `label ..... value` lines: the first version did
  that and on a phone it was eleven rows of mostly `0` and `-`, set in the
  table's monospace, which read as a spreadsheet turned sideways. The figures
  are paired by what a person compares (radios and audio held, sent and engine,
  starved and upstream idle) rather than by the desktop's column order, use the
  page's own font with tabular digits, and carry their column's description on
  the cell because the headings that hold it are hidden on a phone. A stalled
  station colours the whole card. The station name wraps instead of being
  truncated, because a URL cut to a dozen characters identifies nothing. Column
  names are read from the headings at runtime rather than copied into the
  script, so a renamed column cannot leave a header and a card label
  disagreeing.
- **The configuration list is narrower and compacts on a phone.** Two columns of
  short strings stretched across a wide page read as a gap with an underline, so
  it is capped at 34rem; below 64rem it drops a size and gives the label column
  60% of the width, which stops values wrapping while labels take the room.
- **The desktop table is trimmed** (station column 26rem -> 20rem, tighter cell
  padding), which brings its natural width from about 1260px to 1150px - it now
  fits a 1280px window instead of needing the horizontal scroller.

### Testing

- Two guards pin the card layout. The first: the table's shape (11 headings),
  the labels being derived from the headings rather than hard-coded, every cell
  being built by index, the explicit ARIA roles that `display: block` would
  otherwise remove, and the narrow-screen CSS block. The second pins the things
  that made it readable after the first attempt was rejected as unreadable: a
  two-column grid with the label above its value, the page font rather than the
  table's monospace, a chosen reading order, and descriptions on the cells.
  329 tests in total.

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

[2.2.1]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.2.1
[2.2.0]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.2.0
[2.1.2]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.1.2
[2.1.1]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.1.1
[2.1.0]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.1.0
[2.0.3]: https://github.com/clifarr/HLS-Radio-Converter/compare/v2.0.2...v2.0.3
[2.0.2]: https://github.com/clifarr/HLS-Radio-Converter/compare/v2.0.1...v2.0.2
[2.0.1]: https://github.com/clifarr/HLS-Radio-Converter/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.0.0
