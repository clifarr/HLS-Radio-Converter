# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2.4.0] - 2026-10-26

This release is mostly about **not re-encoding**. The FFmpeg-free path now covers
the two shapes of stream that exist in the wild, which matters because most
radios already decode AAC - any set that receives DAB+ has to - so for them a
re-encode was a second round of lossy encoding, 165% of a core, and a child
process, all to produce a worse copy of bytes the radio could already play. The
converter page finally uses the diagnostics the service always had, to say which
engine a given station will get and to offer the URL that avoids FFmpeg when the
service's own configuration is the only thing in the way. The configuration is
also editable from the browser now, so tuning any of it no longer means editing a
file and restarting.

### Added

- **The converter page checks the stream, not just the URL.** `/probe` was only
  reachable by a script or by hand, which made it a feature nobody used - so
  `GET /` now runs the identical inspection and prints the answer: the engine
  that will be used, the container as measured from the bytes, the playlist and
  segment shape, encryption, the real audio codec, and the elementary streams
  found inside a transport stream. An engine verdict sits above the table in one
  sentence (*"FFmpeg is not needed: the station's own audio is forwarded
  untouched as ADTS"*), coloured only when there is something to act on. When
  the stream *could* be forwarded but this service's `output_codec` prevents it,
  the page prints the `?codec=` URL that avoids FFmpeg for that station -
  the per-request form of the same choice, so it changes nothing for anybody
  else listening.

  The check is **opt-in** (`probe=1`, set by a checkbox that is ticked on a
  first visit): it downloads a segment and runs FFmpeg, so a bare `?url=` still
  renders instantly and touches no network at all. A station that cannot be
  reached comes back as a sentence on the page rather than a 500 or a hang -
  over an overall 20 s deadline, since those bounds are additive.

  When the page offers the codec override it also says what that costs, because
  the URL is only half the decision: the response stops being `audio/mpeg` and
  becomes `audio/aac`, which is a header old Frontier Silicon firmware may not
  accept (and may want spelled `audio/aacp`; `mime_override` sets it). A radio
  that decodes only MP3 should keep the plain URL, where the price of doing so
  is CPU rather than quality. Related, and now stated in one place: `codec=aac`
  and `codec=adts` are **the same request**, not two codecs, so the page shows a
  single spelling (`adts` - the container name, and the word its verdict
  sentence already uses) and the README calls `aac` an alias rather than leaving
  the two to look like a choice.

- **A station that serves bare ADTS can now run with no FFmpeg at all.**
  Pass-through was a PAT/PMT lookup, so it only ever applied to MPEG-TS
  segments; a stream whose `.aac` segments are raw ADTS behind an ID3v2 tag
  (Microsoft's `msvdn.net` CDN, among others) was re-encoded even when the
  output codec was the source's own. `tsdemux.ElementaryStreamForwarder` fills
  that gap: it drops each segment's ID3 tags, starts on a frame boundary and
  keeps frames whole across segment boundaries, forwarding the source's own
  bytes. Measured on a Raspberry Pi against a real station: `passthrough`
  instead of a 165 %-of-a-core transcode, and the delivered stream decodes with
  `ffmpeg -err_detect explode` and zero errors. Bare `.mp3` segments take the
  same path. Song titles are unchanged - they are read from the removed tag and
  delivered as ICY metadata.

- `/probe` and `radioconv probe` now report **`passthrough_engine`** alongside
  `passthrough_possible`/`passthrough_kind`. The first two describe the source;
  the new one says whether the configured `output_codec` will actually use the
  FFmpeg-free path, and the CLI prints how to switch (`output_codec = "adts"`)
  when it will not.

- **The settings are editable from the browser, on `/settings`.** The dashboard
  has always listed the configuration and always had to add that it was
  read-only - a table you can only look at, on the page where you most want to
  change something. Twenty values (bitrate, output codec, forwarding,
  pass-through, sample rate, channels, the listener limits, the pacing and
  timeout knobs, `public_host`) now have a form; `host`, `port`, `log_level`,
  `ffmpeg_path` and `cors_origins` stay in `config.toml` and are listed as such,
  because changing a bind address from a web page is how you lose the page.

  A change is **validated before it is applied**: the whole candidate
  configuration is built and checked, so a rejected edit changes nothing in
  memory, writes nothing, and comes back with the offending field marked and the
  sentence explaining which rule it broke - and with every *other* edit still in
  the boxes, since reverting twenty fields because one was wrong is how people
  stop using a form.

  It is then **persisted** to `ui-settings.json` in the config directory, merged
  into whatever was saved before (so saving one field does not erase last
  week's), written to a sibling file and renamed over the target - a power cut
  mid-write would otherwise leave truncated JSON and a service that will not
  start at all. A value that cannot take effect is *reported* rather than
  claimed: `RADIOCONV_*` variables and command-line flags still outrank the
  page, so the result says `Saved, but not in effect: bitrate.` and names the
  reason. The precedence order is now defaults < `config.toml` < saved page
  values < environment < flags.

  Reaching a *running* station needs one more step, because a session copies its
  settings at birth (its buffer, bitrate and jitter grid describe audio that is
  already playing). The form offers **restart the stations playing now**, which
  ends them so they rebuild - radios reconnect by themselves, which is what the
  whole service already depends on - and says how many it ended.

  `allow_ui_settings = false` locks the page: `GET` renders the values and
  explains that the installation is read-only, and `POST` is refused with `403`
  rather than silently doing nothing. The route parses
  `application/x-www-form-urlencoded` itself with `parse_qsl`, capped at 16 KiB,
  and refuses any other content type: `await request.form()` would reintroduce
  the `python-multipart` dependency this project removed, and a multipart body
  read as urlencoded yields boundary strings as field names.

### Fixed

- **Pass-through no longer overruns the radio** (reported as *"every few
  seconds, it either repeats the last few seconds or else skips a few seconds"*,
  on a station carrying AAC in a transport stream). Two measurements fed the
  output pacer, and both were wrong by a few per cent or more:

  1. **The rate was the download rate.** It was computed from `bytes_in`, and a
     transport stream is mostly not audio: 188-byte packets, PAT/PMT tables, PES
     headers and adaptation-field stuffing around every payload. Measured on the
     station in question: 199 092 bytes per segment against 165 148 bytes of
     elementary stream - **20 % overhead**. Together with (2) that is
     159 kbit/s of pacing for audio that needs 128 kbit/s, i.e. 1.25 s of audio
     handed over every second.
  2. **The duration was the playlist's claim.** It came from `EXTINF`, which
     said 10.0 s for a segment holding **10.325 s** of audio (HE-AAC: 1024
     samples per frame at a 24 kHz core rate). That alone is 3 % fast, which
     looks harmless in a short run and drains the jitter buffer in under three
     minutes - after which every segment is followed by a gap, just like (1).

  The jitter buffer absorbs either error for a few segments and then runs dry, so
  each segment's bytes were followed by roughly two seconds of silence. That is
  longer than the ~1.2 s a Frontier-Silicon radio tolerates, so it reconnects -
  and a rejoin replays `handoff_overlap_seconds` (heard as a **repeat**) or
  discards the queued backlog (heard as a **skip**).

  Both figures are now measured on the audio that is actually being forwarded:
  `bytes_out` for the rate, and a duration counted from the ADTS/MPEG frame
  headers (`tsdemux.audio_duration`) - 1024 samples per ADTS frame at the
  header's rate, 384/576/1152 per MPEG frame depending on layer and version. No
  decoding, one header walk per segment. Transcoded output was never affected:
  it uses the configured bitrate, which is exact.

  Verified on the station by measuring a listener connection before and after:
  pre-fix it was paced at 162 kbit/s for audio that needs 128 kbit/s and then
  showed ~2 s holes per segment; post-fix it holds 130 kbit/s (the audio rate
  plus the one-off startup burst) with no interval over 1 s across four minutes.

- **A low-bitrate station is no longer delivered in one-second lumps** (reported
  as *"constant repeating every few seconds"* on a ~64 kbit/s HE-AAC stream -
  the same symptom as above, but caused by a different mistake, and far more
  audible). The pacer was correct to the byte; what was wrong was the *shape* of
  what it handed over. The unit it paces is a queue chunk of **8192 bytes**, and
  a chunk is not a fixed amount of time: that is 0.34 s of audio at 192 kbit/s
  but **1.02 s at 64 kbit/s**. The chunk was written in one piece, so the
  listener got ~8 KiB at once and then nothing until the next chunk was due -
  a burst followed by a ~1.1 s silence, repeating every second. That sits just
  under the ~1.2 s a Frontier-Silicon radio tolerates, so it reconnected every
  one to two seconds and each rejoin replayed `handoff_overlap_seconds`, heard
  as a stutter that never stops. It went unnoticed for so long because at
  128 kbit/s and above the same chunk is short enough (0.5 s or less) to pass.

  Every chunk is now cut, at the measured output rate, into writes of at most
  `MAX_WRITE_SECONDS` (0.25 s) of audio before it is paced and yielded
  (`api.write_slices`) - the rate is measured, the write size is derived from
  it, so the shape no longer depends on the station's bitrate. Nothing else
  changes: the same bytes, the same schedule, one `bytes_sent`/throughput
  update per write instead of per chunk. Pacing disabled (`pace_output = false`)
  still writes chunks whole.

  Verified on the station that reported it: a listener connection delivered
  66 kbit/s with the longest silence **0.26 s** (was ~1.1 s) and 4.9 writes a
  second, and on the Raspberry Pi a listener now holds for minutes instead of
  seconds - `listener done after 0s` / `after 2s` reconnects are gone, the
  jitter buffer sits flat at 6.1 s, and stalls, retransmit reconnects and
  dropped chunks all stay at zero.

- **A radio that loses power is no longer counted as listening for a quarter of
  an hour** (reported as *"the server keeps thinking that the radio is
  listening ... `dropped` keeps accumulating, but it keeps the stream active for
  a very long time, like 15 minutes"*). Nothing here had a long timeout. A
  powered-off radio sends no FIN, so the connection stays `ESTABLISHED` from
  the kernel's point of view and TCP retransmits into the void until
  `tcp_retries2` gives up - fifteen attempts, **about 15 minutes**. That is a
  system-wide kernel figure, and the per-socket alternative (`TCP_USER_TIMEOUT`)
  can only be set on a socket ASGI never hands to the application.

  The reason it went unnoticed is narrower, and is the actual defect: the stall
  watchdog runs **between** writes, so it catches a listener with an empty buffer
  but can never fire while the write itself is what is stuck - which is exactly
  what happens once the socket's send buffer fills. So the response generator sat
  in one `await` until the kernel gave up, holding a listener and a capacity
  slot, while the audio produced for it piled into a bounded queue and overflowed
  it: the `dropped` counter climbing, second after second, on behalf of a radio
  that was off.

  The application can bound its own write, so it now does. `_ListenerStream`
  puts a deadline (`listener_write_timeout`, 30 s by default, 0 = off) on each
  socket write; when one expires the listener is unsubscribed immediately and the
  response ends, which makes the server close the socket. 30 s is deliberately
  far above the measured 2.8 s of stall a congested Wi-Fi link absorbs inside the
  kernel before the application sees a blocked write at all, so a slow-but-alive
  radio is unaffected - and it is thirty times shorter than waiting for the
  kernel. The one visible cost is the server's own
  `ASGI callable returned without completing response` line, which is how an
  ASGI response asks for its connection to be closed: it is preceded by this
  service's explanation, and a guard pins that the response does *not* claim to
  be complete, because that would keep a dead connection alive instead.

  Verified against a real socket - a client that requests the stream and then
  never reads, which is what a power cut looks like: the listener disappeared at
  exactly the deadline (6 s in the test configuration) instead of at the kernel's
  15 minutes.

- **FFmpeg is no longer a hard requirement** - it stays *required by default*,
  and can now be switched off with `radioconv serve --no-ffmpeg` (or
  `ffmpeg_enabled = false`). Plenty of radios decode AAC, and for those a
  re-encode is pure waste: CPU, memory and a child process per station, to
  produce a stream that is worse than the one the station already broadcasts.
  With it off, no child process is ever started and the binary is not even
  looked for, so the service also runs where FFmpeg cannot be installed at all.

  What changes is not the same service with fewer features, it is a service with
  one engine. `output_codec` and `?codec=` are ignored, because there is nothing
  to encode with: every stream is served in whatever codec the station uses,
  which is exactly the same thing as appending `?codec=auto` to every URL. That
  translation lives in one property (`Settings.effective_output_codec`) rather
  than at each decision site, because there are six of them - the engine, the
  `Content-Type`, the probe report, the two pages, the session key - and a site
  that forgot it would contradict the rest. The session key is included on
  purpose: keying a pipeline on a `?codec=` that cannot change anything would
  give two radios on one station two downloads of byte-identical audio.

  The honest cost is a smaller set of playable stations, so the failure is
  explicit rather than audible. A station that cannot be forwarded (fMP4, or a
  transport stream with no MP3/ADTS audio) is refused with a `503`, a sentence
  naming the reason, and a `Retry-After` - not the `200 audio/mpeg` followed by
  silence this project has shipped before. The refusal is possible because the
  effective codec is `auto` there, so the response already waits for the
  pipeline's verdict before its headers go out; the listener is unsubscribed
  explicitly, since a request refused before the response exists has no response
  generator to run its cleanup. The generic "all 1 source URL(s) failed" wrapper
  now carries the reason with it; it used to replace the one sentence anybody
  could act on.

  The UI reports it in the places it matters. The converter page carries a
  standing notice whenever FFmpeg is off - above the form, outside the opt-in
  diagnostics, because it changes what every URL on that page means and the
  person most likely to be confused never ticks the checkbox. A station's
  verdict says *"This stream needs re-encoding and FFmpeg is disabled"* with the
  reason, rather than claiming a transcode that cannot happen. The dashboard's
  encoder rows report the effective values, since "Output codec: mp3" over a
  stream that is all forwarded AAC is the figure somebody would quote when the
  audio sounds wrong. `/probe` gained `ffmpeg_enabled`, its `audio` row falls
  back to the container the demuxer already identified instead of running a
  binary that is off, and `radioconv check` reports FFmpeg as *disabled* rather
  than failing - it exits 1 on any failed row, and it is the command somebody
  runs precisely when something is wrong.

  A contradictory configuration (`--no-ffmpeg` with `--no-passthrough`) is
  refused at startup rather than discovered as silence on a radio, and the
  engine decision does not depend on that validator having run: it is reachable
  without validation through `model_copy`, which is how per-request overrides
  are applied.

### Repo

- `scripts/verify-playback-guards.py` had a stale mutation pattern for the
  pacer (`if delay <= 0:` gained a comment when the last-delivery stamp was
  added in 2.3.2), so the script aborted before checking anything. Fixed; it
  catches all 35 reintroduced defects again, including the two that produced the
  2.4.0 pacing fix, the one that reintroduces whole-chunk writes, the three that
  let a powered-off radio be waited on, the two that send an AAC station to an
  engine that does not exist, and the two that serve an unproducible stream as a
  silent `200` or leak its listener.
- `scripts/verify-web-guards.py` gained three cases for the converter page's
  check - believing `passthrough_possible` instead of `passthrough_engine`,
  offering a codec override that cannot remove FFmpeg, and running the
  inspection when it was not asked for - and two for the FFmpeg-disabled
  install: promising a transcode that cannot happen, and making the standing
  notice opt-in. It catches all 25 now. A third case there - refusing the
  `codec=` offer on an FFmpeg-free install - was written, went green, and was
  deleted: `_engine` only ever reports a codec to override when a *transcode*
  could be turned into a forward, so the guard was protecting a clause that
  could not be reached. The guard script is what found that, which is the
  reason it exists.
- README and CONTRIBUTING explain the two forwardable source shapes and why the
  page reports the engine rather than the source. README gained a *Running
  without FFmpeg* section with the enabled/disabled comparison, since the
  trade-off is the whole decision.
- `scripts/verify-web-guards.py` gained eight cases for the settings page -
  accepting a submit while the page is locked, accepting a body it cannot parse,
  reading an unbounded body, overwriting a pinned value, reporting a restart it
  did not perform, writing the file in place, and re-rendering a rejected submit
  from the stored settings instead of from what was typed - and now names the
  test module per case, so a settings test cannot be "proved" by a page test
  that happens to fail. Thirty-three guards, all of which go red on demand. Two
  of the first drafts guarded nothing: one mutated `_pinned` in a line that the
  next line immediately rebuilt from `_env_pins()`, and one asserted `0` only
  after still calling `retire_all`, so a reply-only test could not tell the
  difference. Both were retargeted, and the restart case gained an in-process
  test that registers a real session and checks the manager, since that is the
  half a socket-level test cannot see.
- README was rewritten around what this release is actually for: *Most radios
  need no transcoding*, with the DAB+ argument (a set that receives DAB+ already
  has an AAC decoder, so the network player usually needs no help), the measured
  cost of the two engines side by side, and the firmware caveats that are the
  real risk. It gained a *Who this is for* section, because "turn HLS into MP3"
  reads as a media server to people who want a media server, and is not one.
  The two old sections that both explained the FFmpeg-free path were merged into
  one, which is what kept it inside its 400-line budget - the length guard is a
  test, so trimming rather than raising it was the point.
- `docs/dashboard.png`, `docs/landing-page.png` and a new `docs/settings.png`
  were recaptured on the Pi against 2.4.0. The old pair dated from 2.0.0 and
  showed the read-only configuration table, the *"fixed for the life of the
  process"* note, and a transcoded station - every one of which this release
  changed.
- `tests/test_session.py`'s `fast_settings` whitelists its overrides, so a
  setting it does not name is silently dropped - which made the new
  `ffmpeg_enabled` test pass while exercising the default install. Fixed, and
  the helper now says so.

## [2.3.2] - 2026-10-22

2.3.1 fixed the self-repair, and the repair worked. It also introduced a
regression that the next hardware test caught within minutes, because one of
the three repair triggers was a blind timer and a blind timer cannot tell a
healthy stream from a broken one.

### Fixed

- **The periodic re-anchor is off by default.** `reanchor_interval_seconds` was
  `120`, so every listener of every station was disconnected every two minutes
  whether or not anything was wrong. Heard on the real radio, verbatim:

  > every 2 minutes, I'm noticing the radio either skipping a few seconds, or
  > repeating a few seconds

  Both halves are this mechanism, and neither is a bug in it:

  - the re-anchor **discards the listener's queued backlog** (up to the 9.2 s
    reserve) so the disconnect is immediate - which is correct, and was the
    whole point of 2.3.1. The radio then resumes at the live edge having never
    decoded that audio: heard as a **skip**.
  - the fresh connection is primed with `handoff_overlap_seconds` (1.5 s) of
    audio that was already sent, to bridge the join: heard as a **repeat**.

  Paying that is right when the radio is already garbled - a skip costs nothing
  on audio you cannot hear anyway. It is wrong on a healthy stream, and a timer
  has no way to know which it is looking at. The two evidence-based triggers
  are unaffected and still repair real damage; in the 2.3.1 hardware test they
  recovered three induced glitches in about a second each.

  What is given up: a decoder wedged with *no* evidence of damage stays wedged
  until the radio reconnects by itself, which it does every 20-60 s anyway
  (measured over hundreds of connections), so the exposure is bounded by the
  hardware regardless. That case was observed once, on the song-titles channel
  - permanent garble with neither trigger firing while the delivered stream was
  verified perfect. Operators who see it can set `reanchor_interval_seconds` to
  a value between 30 and 3600 and accept the periodic skip/repeat as the price.

### Repo

- The CI `Install ffmpeg` step no longer depends on a single apt mirror. The
  GitHub runner image points apt at `azure.archive.ubuntu.com`, which stalled
  mid-transfer on three consecutive runs (once sitting on one 9 MB archive for
  9.5 minutes without a byte arriving) and got two of them cancelled. Each
  attempt is now bounded with `timeout --signal=INT` and retried, rotating
  through the image's own mirror, `archive.ubuntu.com` and
  `mirrors.edge.kernel.org`.

- `CONTRIBUTING.md` now requires a **listening test on a real radio** for
  anything that can change what is heard, and says why: every audio defect this
  project has shipped had a green test suite. The bundled FFmpeg that
  segfaulted on MPEG-TS, the stream verified byte-perfect that a radio rendered
  as garble, and the 120 s timer in 2.3.1 were all only ever audible, because
  the tests measure the bytes we send while the defect lives in how the
  receiver decodes them. Guarded by
  `test_contributing_requires_a_listening_test_for_audio_changes`.

## [2.3.1] - 2026-10-22

Makes the self-repair actually work, after measuring the previous release on the
real radio rather than trusting it.

Three things were wrong, and each was found by a hardware test that failed:

1. a re-anchor took **7-11 s** to reach the radio, so the radio kept being fed
   damaged audio while it waited;
2. the retransmission trigger looked at **one second**, a unit in which healthy
   Wi-Fi and interference cannot be told apart;
3. there was a band of interference that wedged the decoder producing **no
   evidence at all**, which no threshold can ever detect.

Verified on the real hardware, three times, after the fixes: three induced
glitches, three recoveries, "almost instantly" each time.

### Fixed

- **The re-anchor no longer waits for the queued audio to drain.** The
  end-of-stream sentinel was *appended* to the listener's queue, and the
  response generator reads that queue in order while the pacer hands those
  chunks out at the stream's realtime rate. A listener holding the usual
  27-chunk (9.2 s) reserve therefore did not see the sentinel for 9.2 s.
  Measured on the live Pi: a **7-11 s** gap between "closing 1 listener
  connection" and the connection actually closing, in 26 of 33 watchdog
  fires.

  That delay defeated the purpose of the whole feature. The radio's decoder is
  already broken when the watchdog fires; the re-anchor exists to cut it loose
  so it can resynchronise, and every extra second of backlog is more damaged
  audio handed to a decoder that cannot recover from it. It also swallowed the
  cooldown - the watchdog fired again while the first repair was still in
  flight - so the radio was hit twice and the repair arrived later than the
  "within 10 s" the cooldown was supposed to bound.

  The backlog is now discarded and the sentinel is the next thing the
  generator sees. That audio was going to be thrown away by the disconnect
  anyway, and it is *not* counted as dropped chunks: that counter means "this
  listener could not keep up", and a deliberate disconnect is the opposite of a
  capacity problem.

- **The retransmission trigger now sums a trailing window instead of reading a
  single poll.** A single poll cannot separate interference from a healthy
  Wi-Fi link, and the measurement says so plainly:

  ==================  ===================  ======================
  Window              Healthy (observed)   Interference (observed)
  ==================  ===================  ======================
  one poll            6                    9
  5 s                 6                    -
  10 s                **13**               **21 - 29**
  ==================  ===================  ======================

  The healthy figures come from **1920 consecutive one-second polls** on a
  quiet Pi network (1890 at zero, the rest 1-7, with one ten-second total
  reaching 13). The interference figures come from the microwave tests. The
  per-poll distributions **overlap** - the healthy link reaches 6 and
  interference starts at 3 - so no single-poll threshold can separate them.
  The ten-second totals can.

  The damage from a microwave is spread across several polls rather than
  concentrated in one, which is exactly what a 20-second burst looks like.
  The watchdog keeps the last `retransmit_window_seconds` (10) of per-poll
  deltas and fires when the total passes `retransmit_window_segments` (14).

  The per-poll threshold (`retransmit_reconnect_segments`, 10) is kept for the
  "one enormous poll" case, which the window would dilute. It is deliberately
  high, above every healthy poll ever measured, and it is *not* the working
  trigger - the interference never produced a poll above 9, so a per-poll
  threshold of 10 would never fire alone. An earlier build set it to 3, which
  fired on healthy traffic; the test after that is what proved it.

- **A finished interference burst re-anchors once, not once per cooldown.**
  The windowed trigger kept the damage it had already acted upon, so the same
  event fired again the moment the cooldown expired. Measured on the Pi:

  ```
  16:34:09  window=24  reconnects=1  <- per-poll trigger (16 segments in 1.0s)
  16:34:14  window=23  reconnects=2  <- window trigger, SAME damage
  16:34:14  listener done after 1s     <- killed in the second it connected
  ```

  The radio had reconnected in that very second and was disconnected within
  it, reconnected again, and stayed glitching. That is fatal rather than
  wasteful: a radio needs a few seconds of unbroken stream to rebuild its
  decoder state, and a second re-anchor on the same evidence guarantees it
  never gets them. The cooldown (5 s) is deliberately shorter than the window
  (10 s), so without clearing the window *any* window-triggered re-anchor could
  always be repeated once.

  The window is now emptied when it fires. New damage still re-arms it, so
  sustained interference is caught at the same latency - it just has to be
  genuinely new.

- **Damage that arrives while no radio is listening no longer kills the next
  one.** The window accumulated while the station was warm with nobody
  attached, so the next radio to connect was disconnected on its first poll -
  for damage it never received, on a connection whose stream was clean. The
  window is now discarded whether or not there was a listener to act on.

- `SessionManager.retransmit_counter` is typed as a `RetransmitCounter`
  protocol rather than the concrete class, which is what it actually accepts -
  any object with `name`, `available` and `read()`.

### Added

- **A periodic safety net: `reanchor_interval_seconds` (default 120, `0`
  disables it).** Every listener of a station is re-anchored on a timer,
  whether or not anything looks wrong.

  It exists because the evidence-based triggers **cannot** cover every case,
  and the song-titles channel proved it. Measured there: the radio went into
  the permanent garble with **neither trigger firing** - the journal for the
  whole episode contains no `retransmitted` and no `silence` line - while the
  stream being delivered was verified perfect:

  ```
  captured     320611 bytes, 19 ICY blocks
  audio        319680 bytes = 555 frames, 0 trailing
  bad syncs    0
  titles       StreamTitle='Cesare Cremonini - Paparazzi';
  ffmpeg -err_detect explode   ->  clean decode, zero errors
  ```

  That last check is the one that catches a bit-reservoir break, which a
  frame-sync check cannot see. There was nothing wrong with the stream, and
  nothing to detect.

  This is not a tuning problem, so tuning cannot fix it. Once the interference
  stops, a wedged radio acknowledges every byte at full rate - measured, for
  776 s - which is exactly what a healthy radio does. No threshold can separate
  two identical observations, so **no** evidence-based trigger can bound how
  long a radio stays broken. Only a timer can.

  The cost is honest: one brief audio gap per interval, on every station, even
  on a perfect network. 120 s is the same order as the radio's own reconnect
  cadence (20-60 s, measured over hundreds of connections in the journal), so
  the decoder already tolerates it, and it bounds "glitches until someone
  restarts the service" - the reported failure - to two minutes. Raise it if the
  interruptions matter more to you than the worst case; set it to `0` to rely on
  the evidence-based triggers alone.

- **The three repair mechanisms are counted separately** -
  `scheduled_reconnects_total` (timer), `retransmit_reconnects_total` (network
  damage) and `stall_reconnects_total` (silence) - so an operator can see which
  one is actually repairing their radios instead of having to guess. The window
  figures are published too (`retransmit_window_delta`,
  `retransmit_peak_window`), so the threshold can be read off a running service
  rather than trusted from a changelog.

- **The attach log now reports the join floor** (`floor=none` or a byte
  offset), because that is the difference between a rejoin that *resumes* and
  one that rewinds - and a rewind is a splice, the one class of defect an MP3
  decoder cannot recover from.

### Verified on the hardware

Four microwave tests. Each of the first three found a bug, and the last one
passed.

**Test 1 - the re-anchor is immediate.** Oven on for about twenty seconds, off
as soon as the glitch started; the radio recovered by itself:

```
14:34:16 WARNING listener 8440322f got 9.57s of silence; closing the
         connection so the radio reconnects and re-anchors (threshold 1.00s)
14:34:16 INFO    listener 8440322f done after 904s (21031 KiB)
14:34:17 INFO    listener attached (1 now listening, 27 chunks prerolled)
```

Back **one second** after the watchdog fired, against 7-11 s in the previous
release. That run also showed the retransmission trigger *not* firing at all,
because its threshold was above any poll the interference produced - which is
what led to the windowed redesign.

**Test 2 - the windowed trigger works, and fires twice on one event.** The
first hardware confirmation that ten seconds is the right unit: a window total
of **29**, against a healthy maximum of 13. Also where the double fire was
caught - the second fire disconnected a radio that had reconnected in that
same second.

**Test 3 - the wedge with no evidence.** On the song-titles channel the radio
glitched permanently with neither trigger firing, while the stream was verified
perfect. That is what the periodic safety net exists for.

**Test 4 - passes.** Three induced glitches, three recoveries. All three
mechanisms fired, each one repairing the radio:

```
16:56:29  TIMER     re-anchoring 1 listener on a 120s timer
16:58:29  TIMER     (exactly 120s later)
16:59:17  WINDOW    14 TCP segments over 10.0s  (threshold 14)
16:59:42  WINDOW    14 TCP segments over 10.0s  (threshold 14)
17:00:04  STALL     listener 96eeb7b6 got 1.31s of silence (threshold 1.00s)
17:00:29  TIMER
```

Every recovery was "almost instantly", in the words of the person listening.
The radio ended the run healthy: connected, `dropped_chunks: 0`, 6.8 s
buffered, 0.0 s starved.

### Known limits

- **The window threshold has a thin margin.** Both window fires in the passing
  test landed at exactly 14, its own threshold, while the highest healthy
  ten-second total measured was 13. It has not false-fired, but the gap is one
  segment. If spurious re-anchors ever appear on a clean network, raise
  `retransmit_window_segments` to 16-18 - the interference figures measured 21-29,
  so there is room - rather than lowering it.
- **The detections are machine-wide.** The count is the whole kernel's, so the
  service's own upstream downloads contribute to it. Per-socket numbers exist
  (`/proc/net/tcp`'s `retrnsmt`, `ss -tni`) but are not portable to the same
  three platforms, so they are not used.
- **The Windows reader has never run on real Windows.** See 2.3.0.
- **A re-anchor still replays a little audio.** `handoff_overlap_seconds`
  (1.5 s) re-sends already-delivered audio to bridge the handoff. Measured on
  a reconnect: 5 chunks, 1.71 s, of which 1.37 s had been sent before. That is
  deliberate and bounded, but it is a deliberate discontinuity.

## [2.3.0] - 2026-10-22

A **minor** release rather than a patch, because the remedy is a new capability
rather than a correction of one: the server can now **deliberately end a
listener's connection** when it has evidence that the radio is decoding garbage,
so that the radio reconnects and re-anchors its decoder. Both triggers are
configurable, independently, and either can be switched off.

It fixes the radio falling into a **permanent** garbled-audio-and-garbage-title
state, and it is verified on the real hardware: with a microwave oven running at
high power the audio now glitches only *while* the interference lasts, and clears
immediately after. Before this release it stayed broken until the service was
restarted - the reported "glitched, then kept glitching".

The investigation is worth recording, because the answer was not where any of
us looked first. During the fault the server's stream was measured and found
**perfect**: no gaps in the TCP byte stream across 39 connections over 3.5
hours, an unbroken chain of 32 355 MP3 frames, the ICY grid landing on its
declared offset every time, and the radio acknowledging every byte at full
rate. The defect was never the bytes - it was that the radio's own MP3 decoder
gives up on damaged audio and never recovers on its own.

There is also a lesson in this release about **which** signal works, because
the first attempt at it did not. See "Re-anchoring the listeners" below.

### Added

- **Listeners are re-anchored when the operating system reports a TCP
  retransmission burst** (`retransmit_reconnect_segments`, default `10`, polled
  once a second; `0` disables it). This is the fix that actually works.

  Why a burst of retransmissions, and not a stall: Wi-Fi interference damages
  packets, TCP resends them, and what the radio receives is *late* rather than
  absent. That makes it invisible from inside this process. Measured during a
  microwave test: a **2.8 s** stall and **27 retransmissions (36 KB)** on the
  wire, while the application saw nothing at all - the kernel's send buffer
  (44.8 KB, about 1.9 s of audio) plus uvicorn's 64 KB transport buffer
  absorbed the whole gap, so the pacer kept draining and never reported a lag.
  A first version of this watchdog therefore watched *timing* and **never
  fired** on the channel that was failing.

  The kernel does know. The numbers are three orders of magnitude apart:
  **47 retransmissions in 776 s** on a healthy connection (0.06 per second),
  against **21 inside a single ten-second window** during interference.

  Supported on Linux (`/proc/net/snmp`, field located by name so a kernel that
  adds a counter cannot shift it), macOS (`netstat -s -p tcp`, singular and
  plural spellings) and Windows (the IP Helper API's `GetTcpStatisticsEx`,
  which returns a struct rather than text, so it is unaffected by the system
  language). On any other platform the watchdog stays off and the service
  behaves as it did before.

  **Windows is untested on real hardware.** The Linux and macOS readers were
  exercised against live systems; the Windows reader has never run on an actual
  Windows machine, because none was available. Its struct layout and decoding
  are unit-tested against a fake, and a failure there leaves the watchdog off
  rather than misreading the counter - but that is reasoning, not evidence. If
  you run this on Windows and the dashboard's `retransmit_source` reads
  `windows` with a plausible, rising `retransmit_last_delta`, the path works;
  if it reads `unavailable`, it does not, and the service behaves exactly as it
  did before this release. Nothing else depends on it.

  Closing the connection is the only remedy available: the radio's decoder
  keeps no way to resynchronise once it has decoded damaged audio, and it will
  not ask for a new connection while bytes keep arriving - measured while
  garbled, it acknowledged every byte at full rate for 776 s. Only the
  *listeners* are ended; the station's pipeline and reserve keep running, so a
  reconnecting radio is served from a warm session and resynchronises at once.

- **A stall watchdog, for the other failure: the upstream starving**
  (`stall_reconnect_seconds`, default `1.0`). A listener that has been handed
  no chunk for a second is reconnected too. This is what catches a starved
  pipeline rather than a damaged network, and it did fire in the field - on a
  **3.28 s** gap - so the two are kept side by side rather than one standing in
  for the other.

- `stall_reconnect_cooldown_seconds` (default `5`) bounds how often either
  watchdog may act on one station, and therefore bounds how long a radio can
  stay broken: cooldown plus one poll plus the radio's own reconnect. Without
  a cooldown, an outage where every listener stalls at once would close and
  reopen every connection every second - a shape of failure this project has
  produced before: 6940 connections in 89 seconds.

- `retransmit_reconnects_total` and `stall_reconnects_total` (Prometheus),
  per-session counts, and `retransmit_source` / `retransmit_last_delta` /
  `retransmit_peak_delta` in `/metrics.json`, so an operator can tell an armed
  watchdog from a silently disabled one and set the threshold from the
  machine's own numbers rather than from a guess.

### Fixed

- **The first re-anchor of a station is no longer suppressed.** The "last
  re-anchored" timestamp was initialised to `0.0`, which reads as *an event at
  time zero*, so on a freshly booted host the cooldown blocked the watchdog
  until the monotonic clock had passed it. Invisible on a Pi that had been up
  for days, permanent on one that had just started. Both watchdogs now use
  `None` for "never"; the tests caught it.

- **A reconnecting radio is no longer replayed audio it has already played.**
  The join floor - the mechanism that stops a rejoin rewinding - was keyed on
  `(IP, User-Agent)`. The same radio reconnects with **two different**
  User-Agents (`FSL IR2.10` from the firmware, `NSPlayer/8.0.0.3801` from the
  media stack), so coming back with the other one meant no floor was found
  and the session's whole reserve was handed over - which in this case held
  **20.5 s** of audio the radio had already played (see the reserve bound
  below). Measured on the live server, before and after: the new
  connection's first 4096 bytes were found at **offset 0** of the previous
  connection's stream (3.34 s replayed); now the two do not overlap at all.

- **The join floor is an audio position, not a timestamp.** The reserve is
  filled in bursts - a whole segment is transcoded far faster than realtime -
  so a wall-clock floor was always *behind* the oldest piece in the reserve
  and could never cut anything. It is now the piece's offset in the station's
  running total of audio.

- **The reserve is bounded by audio duration, not by piece count.**
  `preroll_seconds` (10 s) was enforced as `queue_chunks` (128 *pieces*), and
  because pieces arrive in bursts that held **20.5 s** of audio - measured as
  57 chunks of 8640 bytes in the live log. A rejoin could therefore be primed
  with twice the configured maximum. Now bounded by bytes: 27 chunks, 9.72 s.

- **ICY metadata blocks no longer split an MP3 frame.** `ICY_METAINT` was the
  heritage 16000, and an MP3 frame for this project's 192 kbit/s, 48 kHz
  output is 576 bytes: `16000 / 576 = 27.78`, so **89 %** of blocks landed
  inside a frame and the radio had to reassemble a frame the block had cut in
  half. The grid is now `16128 = 28 × 576`, computed per stream by the new
  `icy_metaint_for_frame()`. Measured after: **0 of 39** blocks land inside a
  frame (was 89 %).

- **ICY metadata is opt-in per request.** The metadata grid was sent to every
  client, including the radio's `FSL IR2.10` connection, which never sends
  `Icy-MetaData` and so was being handed a byte stream with 17-byte blocks
  embedded in it - a firmware path that does not handle them. The
  `icy-metaint` header and the blocks now go only to a client that asked for
  them.

- **`stall_reconnects_total` no longer decreases.** It was summed over the
  live sessions, so when the station a watchdog event belonged to was reaped
  the total fell from 1 back to 0. A Prometheus counter that goes down breaks
  `rate()`, and it was simply untrue. It is now a service-wide counter.

### Verified on the hardware

- Four independent packet captures, including one of 832 MB over 3.5 hours, on
  the real radio's own traffic.
- The radio's two connections behave as both fixes intend: `FSL IR2.10` gets
  no `icy-metaint` and no blocks, `NSPlayer` gets both on a 16 128-byte grid.
- The live stream on a channel carrying real song titles: 55 ICY blocks, **0
  malformed**, an unbroken chain of **1540 frames**, **0 of 54** blocks landing
  inside a frame, and a valid title in every block.
- **The microwave test passes on both channels.** With the oven at full power
  the audio glitches for as long as the interference lasts and is clean
  immediately after, with no manual restart. The journal from that run:

  ```
  the network retransmitted 6 TCP segments in 1.0s: closing 1 listener
  connection(s) so the radios re-anchor their decoders (threshold 3)
  ... 4, 3, 5, 3 segments ...
  listener c7f009ea got 3.28s of silence; closing the connection so the radio
  reconnects and re-anchors (threshold 1.00s)
  ```

  Both watchdogs fired where they should: five retransmission bursts and one
  genuine starvation, with `dropped_chunks_total: 0` and
  `pipeline_restarts_total: 0` throughout.
- **No false positives in steady state.** On two hours of ordinary network
  after the microwave test, both counters stayed at **0** - not one re-anchor
  was forced. The mechanism costs nothing when the network is healthy, which is
  the difference between it and a blunt "reconnect every N minutes" rule.

The channel mattered, and it is worth saying so: the timing-only watchdog did
fire on one station and appeared to work, then did **not** fire on another that
was failing the same way. That is what sent the investigation to the
retransmission count. A single passing test on a single station was not
evidence, and it was treated as if it were.

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

[2.3.2]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.3.2
[2.3.1]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.3.1
[2.3.0]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.3.0
[2.2.2]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.2.2
[2.2.1]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.2.1
[2.2.0]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.2.0
[2.1.2]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.1.2
[2.1.1]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.1.1
[2.1.0]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.1.0
[2.0.3]: https://github.com/clifarr/HLS-Radio-Converter/compare/v2.0.2...v2.0.3
[2.0.2]: https://github.com/clifarr/HLS-Radio-Converter/compare/v2.0.1...v2.0.2
[2.0.1]: https://github.com/clifarr/HLS-Radio-Converter/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/clifarr/HLS-Radio-Converter/releases/tag/v2.0.0
