"""Playback continuity: a listener must receive a steady stream, not bursts.

Every other test in this suite reads the socket as fast as it can, and that is
precisely the case that hides a pacing bug. So this module exists for the failure
a human hears rather than a test catches: the audio stopping for a second and
starting again.

Two separate defects produced it, and both went unnoticed for the same reason.

1. **The buffer was a seventh of the size it claimed to be.** A queue is bounded
   by a number of *items*, and FFmpeg's pipe returns what is available - measured
   at **578 bytes** per read on a real 192 kbit/s stream, not the 8192 asked for.
   So ``queue_chunks = 128`` was a 74 KB buffer, about **three seconds**, while the
   comment beside it said forty-three. Three seconds cannot cover the 8-16 second
   gap between HLS segments, so the buffer emptied in every gap.

2. **Audio was handed over as fast as it could be read.** HLS arrives in segments
   and a segment is transcoded far quicker than realtime, so a listener got ten
   seconds of audio in a burst followed by silence. Measured before the fix: 31 of
   40 consecutive seconds carried **zero** bytes.

Together those meant a radio's small input buffer underran in every gap. These
tests drive the fan-out and the pacer directly rather than through a fake
pipeline, so they measure those two mechanisms and nothing else.
"""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest
from conftest import ffmpeg_binary, has_ffmpeg

from radioconv import ffmpeg as ffmpeg_mod
from radioconv.api import ICY_METAINT, _insert_icy_metadata, _Pacer, icy_metadata_block
from radioconv.config import Settings
from radioconv.pipeline import PipelineStats, StreamPipeline
from radioconv.session import SessionManager
from radioconv.tsdemux import adts_align_offset, mp3_align_offset, mp3_frame_size

#: A deliberately high rate so the pacing arithmetic is real but the tests run in
#: seconds. Chunk time is 8192/200000 = 41ms.
FAST_RATE = 200_000.0
FAST_CHUNK_SECONDS = 8192 / FAST_RATE

#: What a read from FFmpeg's stdout actually returned when measured against a real
#: 192 kbit/s MP3 stream. Not chunk_size - a pipe hands back what is written.
MEASURED_PIPE_READ = 578


def settings(**overrides) -> Settings:
    return Settings(
        linger_seconds=0.3,
        max_sessions=2,
        max_listeners_per_session=2,
        **overrides,
    )


async def subscribe(manager: SessionManager):
    return await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])


# ------------------------------------------------------- the buffer is real
async def test_small_pipe_reads_are_accumulated_into_full_chunks():
    """`queue_chunks` must mean chunks of `chunk_size`, whatever the pipe returns.

    This is the bug in one assertion: with 578-byte items a 128-item queue is a
    74 KB buffer (three seconds), not the 1 MiB (forty-three) it advertises.
    Accumulating makes the arithmetic true for FFmpeg's dribbling pipe.
    """
    manager = SessionManager(settings(chunk_size=8192))
    session, subscriber = await subscribe(manager)
    try:
        for _ in range(40):
            session._fan_out(b"a" * MEASURED_PIPE_READ)

        _, chunk = await asyncio.wait_for(subscriber.queue.get(), timeout=5)
        assert len(chunk) == 8192, (
            f"expected an accumulated 8192-byte chunk, got {len(chunk)} - the queue "
            "bound is only meaningful if items have a known size"
        )
        # 40 * 578 = 23120 bytes: two full chunks, with 6736 held back.
        assert not subscriber.queue.empty(), "the second full chunk is missing"
        assert len(session._pending) == 40 * MEASURED_PIPE_READ - 2 * 8192
    finally:
        await session.close("test")


async def test_large_blobs_are_split_into_full_chunks():
    """And the other direction: the pass-through demuxer emits whole segments.

    Without splitting, one 240 KB item would count as one against a 128-item
    bound, so a single listener could buffer tens of megabytes.
    """
    manager = SessionManager(settings(chunk_size=8192))
    session, subscriber = await subscribe(manager)
    try:
        session._fan_out(b"\x00" * 240_000)
        sizes = []
        while not subscriber.queue.empty():
            item = subscriber.queue.get_nowait()
            if item is None:
                break
            sizes.append(len(item[1]))

        assert sizes and all(size == 8192 for size in sizes), f"chunk sizes: {set(sizes)}"

        # 240000 is not a multiple of 8192, so the remainder is deliberately held
        # back rather than sent as a runt - and must still arrive on flush.
        held = len(session._pending)
        assert sum(sizes) + held == 240_000, "re-chunking must not lose or duplicate bytes"
        session._flush_pending()
        assert held > 0 and subscriber.queue.get_nowait()[1] == b"\x00" * held
        assert session._pending == b""
    finally:
        await session.close("test")


def test_the_buffer_is_measured_in_seconds_not_items():
    """The number an operator cares about: how many seconds can we absorb?

    With 128 chunks of 8 KiB at 192 kbit/s it is ~43 seconds, which is what the
    setting says and enough to cover the gap between two segments. The measured
    pipe read of 578 bytes - the value that shipped - gives under four, which is
    not a buffer at all against an 8-16 second gap.
    """
    configured = settings(chunk_size=8192, queue_chunks=128, bitrate=192_000)
    seconds = configured.queue_chunks * configured.chunk_size * 8 / configured.bitrate
    assert seconds > 30, f"only {seconds:.0f}s of buffer"

    as_shipped = configured.queue_chunks * MEASURED_PIPE_READ * 8 / configured.bitrate
    assert as_shipped < 5, f"578-byte items would have made the buffer {as_shipped:.1f}s"
    assert seconds > as_shipped * 8


async def test_the_buffer_absorbs_a_whole_segment_without_dropping():
    """The property that stops the cutouts: a segment's worth of audio fits.

    FFmpeg transcodes a whole segment ahead of realtime and hands it over at once,
    so the buffer has to hold that burst. Sized in items of whatever the pipe
    returned, it did not: most of the segment was dropped and the listener heard a
    gap until the next one arrived.
    """
    configured = settings(chunk_size=8192, queue_chunks=16, bitrate=192_000)
    buffer_seconds = configured.queue_chunks * configured.chunk_size * 8 / configured.bitrate
    segment_seconds = 3.0
    assert buffer_seconds > segment_seconds, "the test needs a buffer that fits the burst"

    manager = SessionManager(configured)
    session, subscriber = await subscribe(manager)
    try:
        segment = b"x" * int(configured.bitrate / 8 * segment_seconds)
        session._fan_out(segment)
        session._flush_pending()

        assert subscriber.dropped == 0, (
            f"{subscriber.dropped} chunks dropped - the buffer could not hold one "
            "segment, so the listener would hear a gap"
        )
        # Assert on bytes, not a chunk count: the segment is not a whole number of
        # chunks, so the tail is held back and then flushed as a final short piece.
        queued = []
        while not subscriber.queue.empty():
            item = subscriber.queue.get_nowait()
            if item is None:
                break
            queued.append(item[1])
        assert sum(len(item) for item in queued) == len(segment), "audio was lost"
        assert len(queued) == 9, "8 full chunks plus the flushed remainder"
    finally:
        await session.close("test")


# ------------------------------------------------------------- the pacer
async def test_the_pacer_releases_at_the_stream_rate():
    pacer = _Pacer(lambda: FAST_RATE, initial_burst=0.0)
    started = time.monotonic()
    for _ in range(24):
        await pacer.wait(8192)
    elapsed = time.monotonic() - started
    expected = 24 * FAST_CHUNK_SECONDS
    assert expected * 0.6 < elapsed < expected * 2.0, (
        f"released 192 KB in {elapsed:.2f}s, expected about {expected:.2f}s"
    )


async def test_the_initial_burst_is_a_credit_and_not_a_delay():
    """The cushion must let audio out early, never hold it back.

    The burst means "you may run up to this far ahead of realtime so the radio
    can fill its own input buffer". Anchoring the schedule *forward* by it turned
    that permission into a wait: the very first chunk was withheld for the whole
    burst, so a listener heard nothing for 1.5 seconds before the stream had even
    begun. Measured end to end on a real station, that single sign accounted for
    1.5s of the 1.86s time-to-first-byte.
    """
    pacer = _Pacer(lambda: FAST_RATE, initial_burst=1.5)

    started = time.monotonic()
    await pacer.wait(8192)
    first = time.monotonic() - started
    assert first < 0.5, (
        f"the first chunk waited {first:.2f}s: the initial burst is being charged "
        "to the listener instead of credited to it"
    )

    # The credit is worth about 1.5s of audio, so the opening chunks should still
    # fly out - that is what fills a radio's buffer before the first gap.
    for _ in range(3):
        await pacer.wait(8192)
    assert time.monotonic() - started < 0.5, "the initial burst did not release audio ahead of time"


async def test_the_pacer_does_not_repay_a_starvation_gap_as_a_burst():
    """Credit must not accumulate while there is nothing to send.

    The first version anchored the schedule only to bytes emitted, so a gap in
    which the queue was empty earned the same amount of credit and was then spent
    at full speed. Measured on a real stream: audio for ~4 seconds, then seven
    seconds of nothing, repeating. Forgiving the debt keeps the buffer intact -
    and the buffer is the whole reason a listener survives the gap.
    """
    pacer = _Pacer(lambda: FAST_RATE, initial_burst=0.1)

    for _ in range(20):
        await pacer.wait(8192)
    # Nothing to hand over for a while: the upstream is between segments.
    await asyncio.sleep(2.0)

    started = time.monotonic()
    for _ in range(20):
        await pacer.wait(8192)
    elapsed = time.monotonic() - started
    expected = 20 * FAST_CHUNK_SECONDS
    assert elapsed > expected * 0.6, (
        f"20 chunks released in {elapsed:.2f}s after a 2s gap (expected ~{expected:.2f}s): "
        "the gap was repaid as a burst, which empties the buffer so the next gap is "
        "heard too"
    )


async def test_pacing_can_be_turned_off():
    """An escape hatch for diagnosing pacing itself."""
    pacer = _Pacer(lambda: float("inf"), initial_burst=0.0)
    started = time.monotonic()
    for _ in range(100):
        await pacer.wait(8192)
    assert time.monotonic() - started < 1.0


async def test_a_delay_beyond_the_sleep_cap_still_waits_for_its_deadline():
    """Waking early must sleep again - the cap is a slice, not a deadline.

    ``wait`` used to sleep once with ``min(delay, MAX_SLEEP)`` and return, so
    any single delay over the cap handed the chunk to the socket before its
    time. The comment beside it promises "waking early and sleeping again
    lands in exactly the same place"; the loop is what makes that true.
    """
    pacer = _Pacer(lambda: 8192 / 0.3, initial_burst=0.0)  # 0.3 s per chunk
    pacer.MAX_SLEEP = 0.05  # instance cap, so the test runs in 0.3 s, not 5 s

    started = time.monotonic()
    await pacer.wait(8192)
    elapsed = time.monotonic() - started
    assert elapsed >= 0.25, (
        f"wait returned after {elapsed:.3f}s of a 0.3s delay: the capped sleep "
        "was not repeated, so the chunk was sent early"
    )


# ------------------------------------------------------- ICY song metadata
async def test_icy_metadata_blocks_land_on_the_audio_grid():
    """A block every `metaint` audio bytes; removing blocks restores the stream.

    The heritage Shoutcast grid - the length byte counts 16-byte payload
    blocks - pinned against a live Icecast capture.
    """
    audio = b"\xff\xf1" * 8000  # exactly ICY_METAINT bytes = one grid window

    async def chunks():
        yield audio[:9000]
        yield audio[9000:]

    gen = _insert_icy_metadata(chunks(), ICY_METAINT, lambda: "Sombr - My Body Isn't Ready")
    out = bytearray()
    async for piece in gen:
        out.extend(piece)

    # Audio up to the first block is untouched and on the grid.
    assert bytes(out[:ICY_METAINT]) == audio[:ICY_METAINT]
    blocks = out[ICY_METAINT]
    payload = bytes(out[ICY_METAINT + 1 : ICY_METAINT + 1 + blocks * 16])
    assert blocks == len(payload) // 16 and blocks > 0
    # The title carries an apostrophe, which a radio would take as the end of
    # the value - so it is replaced with a look-alike that does not. See
    # `test_icy_title_with_an_apostrophe_does_not_truncate_the_value`.
    assert payload.split(b"\x00", 1)[0] == "StreamTitle='Sombr - My Body Isn´t Ready';".encode(
        "latin-1"
    )
    # Everything after the block is byte-identical: audio reassembles exactly.
    rest = bytes(out[ICY_METAINT + 1 + blocks * 16 :])
    assert bytes(out[:ICY_METAINT]) + rest == audio


async def test_icy_empty_title_still_keeps_the_grid():
    """Before the source's first ID3 title, blocks still arrive - just empty."""

    async def chunks():
        yield b"\xff" * ICY_METAINT

    gen = _insert_icy_metadata(chunks(), ICY_METAINT, lambda: "")
    out = bytearray()
    async for piece in gen:
        out.extend(piece)

    blocks = out[ICY_METAINT]
    payload = bytes(out[ICY_METAINT + 1 : ICY_METAINT + 1 + blocks * 16])
    assert blocks == 1
    assert payload.split(b"\x00", 1)[0] == b"StreamTitle='';"


# ------------------------------------------------- ICY titles a radio can read
def _radio_reads(block: bytes) -> tuple[str, str]:
    """Parse an ICY block the way a radio does, and return (value, leftover).

    A radio ends the quoted value at the FIRST quote - the same rule a header
    parser uses - and then tries to read the rest as further key=value pairs.
    """
    length = block[0]
    payload = block[1 : 1 + length * 16]
    text = payload.split(b"\x00", 1)[0].decode("latin-1")
    marker = "StreamTitle='"
    start = text.find(marker)
    assert start >= 0, f"no StreamTitle in {text!r}"
    start += len(marker)
    end = text.find("'", start)
    assert end >= 0, f"unterminated value in {text!r}"
    return text[start:end], text[end + 1 :]


def test_icy_title_with_an_apostrophe_does_not_truncate_the_value():
    """A quote in the title must not end the value early.

    Reported on a real station: the radio's display showed ``Fool`` and then
    the rest of the title as junk, because the value ended at the apostrophe
    in ``Fool's Garden - Lemon Tree``.
    """
    block = icy_metadata_block("Fool's Garden - Lemon Tree")
    value, leftover = _radio_reads(block)
    assert value == "Fool´s Garden - Lemon Tree"
    assert leftover == ";", "the block must end cleanly after the value"


def test_icy_title_is_latin1_so_a_radio_can_render_it():
    """The metadata is a byte string; the heritage convention is ISO-8859-1.

    A title sent as UTF-8 came out as ``Â£ MINI COUNTRYMAN`` on a radio that
    decodes Latin-1. Latin-1 characters must survive unchanged, and anything
    outside Latin-1 must be replaced rather than left as a broken byte.
    """
    # A character that IS Latin-1 is kept exactly.
    value, _ = _radio_reads(icy_metadata_block("£ MINI COUNTRYMAN"))
    assert value == "£ MINI COUNTRYMAN"
    value, _ = _radio_reads(icy_metadata_block("ADÈLA"))
    assert value == "ADÈLA"
    # A character that is NOT Latin-1 cannot be sent at all.
    value, _ = _radio_reads(icy_metadata_block(""))
    assert value == ""
    block = icy_metadata_block("歌")
    # The block is still well-formed and the value still parses.
    value, leftover = _radio_reads(block)
    assert leftover == ";"
    assert block[0] <= 15


def test_icy_title_longer_than_the_block_is_cut_safely():
    """A long title is cut, but never in the middle of a character.

    The block holds at most 240 bytes of payload. Cutting a multi-byte
    character in half would leave the radio decoding a broken one.
    """
    for title in ("x" * 500, "£" * 200, "È" * 200, "a" * 100 + "£" * 100):
        block = icy_metadata_block(title)
        payload = block[1:]
        assert block[0] <= 15, "the length byte counts 16-byte blocks"
        assert len(payload) <= 15 * 16
        value, leftover = _radio_reads(block)
        assert leftover == ";", "the closing quote and semicolon must survive"
        # The payload must be valid Latin-1 with no broken character.
        payload.split(b"\x00", 1)[0].decode("latin-1")
        assert value


async def test_icy_title_survives_a_song_change_on_the_grid():
    """The title changes between blocks; the grid does not.

    A station with per-song ID3 changes its title every few minutes. Each
    change must produce a well-formed block on the grid, and the audio must
    still reassemble exactly.
    """
    audio = b"\xff\xf1" * 16000  # two grid windows
    titles = ["Sombr - back to friends", "Fool's Garden - Lemon Tree", "£ MINI COUNTRYMAN"]

    async def chunks():
        yield audio[:9000]
        yield audio[9000:]

    state = {"index": 0}

    def next_title() -> str:
        title = titles[state["index"] % len(titles)]
        state["index"] += 1
        return title

    gen = _insert_icy_metadata(chunks(), ICY_METAINT, next_title)
    out = bytearray()
    async for piece in gen:
        out.extend(piece)

    # The audio is intact: removing the blocks restores it byte for byte.
    rebuilt = bytearray()
    pos = 0
    while pos < len(out):
        rebuilt += out[pos : pos + ICY_METAINT]
        pos += ICY_METAINT
        length = out[pos]
        value, leftover = _radio_reads(bytes(out[pos : pos + 1 + length * 16]))
        assert leftover == ";", f"block at {pos} is malformed: {leftover!r}"
        assert value
        pos += 1 + length * 16
    assert bytes(rebuilt) == audio


async def test_no_gap_appears_while_a_segment_plays_out():
    """The end-to-end property, measured the way a radio experiences it.

    A segment is queued and then consumed exactly as the response generator does.
    The listener must never wait long between chunks - that wait *is* the cutout.
    """
    configured = settings(chunk_size=8192, queue_chunks=64, bitrate=192_000)
    manager = SessionManager(configured)
    session, subscriber = await subscribe(manager)
    pacer = _Pacer(lambda: FAST_RATE, initial_burst=0.0)

    gaps = []
    received = 0
    try:
        session._fan_out(b"y" * int(FAST_RATE * 1.5))
        session._flush_pending()

        last_at = time.monotonic()
        deadline = time.monotonic() + 1.2
        target = int(FAST_RATE * 1.2)
        while time.monotonic() < deadline and received < target:
            try:
                _, chunk = await asyncio.wait_for(subscriber.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                gaps.append(time.monotonic() - last_at)
                break
            await pacer.wait(len(chunk))
            now = time.monotonic()
            gaps.append(now - last_at)
            last_at = now
            received += len(chunk)
    finally:
        await session.close("test")

    assert received > 0, "nothing was delivered at all"
    worst = max(gaps)
    assert worst < 0.3, (
        f"a {worst:.2f}s gap between chunks - the listener's buffer underruns, which "
        "is the cutout a radio hears"
    )
    assert subscriber.dropped == 0, f"{subscriber.dropped} chunks dropped"


# ------------------------------------- the restart splice, with real ffmpeg
def _id3_tags(data: bytes) -> list[tuple[int, int]]:
    """Every plausible ID3v2 tag in *data*, as (offset, size)."""
    found = []
    pos = 0
    while True:
        index = data.find(b"ID3", pos)
        if index < 0:
            break
        if index + 10 <= len(data):
            version = data[index + 3]
            size = 0
            for byte in data[index + 6 : index + 10]:
                size = (size << 7) | (byte & 0x7F)
            total = 10 + size
            if data[index + 5] & 0x10:
                total += 10
            if version in (2, 3, 4) and total <= len(data) - index and size < 100000:
                found.append((index, size))
        pos = index + 1
    return found


def test_a_restarted_encoder_does_not_corrupt_the_mp3_a_radio_receives(
    tmp_path_factory: pytest.TempPathFactory,
):
    """The splice a restart causes must not break the listener's stream.

    This is the whole defect, measured the way it was found:

    * The upstream fetch failed, so the pipeline started a **new** ffmpeg.
    * The listener was given the tail of the old encode followed by the head of
      the new one - one byte stream, two encodes.
    * ffmpeg's MP3 muxer had written an ID3v2 tag (copied from the source's
      own metadata) into the second encode, and with a pipe as the output that
      tag cannot be written once at the start and fixed up afterwards. It landed
      **in the middle** of the listener's stream.
    * A radio's MP3 decoder cannot skip a tag in the middle of a stream: it
      loses frame sync and the bit reservoir breaks. That is the reported
      "garbled song name, then the audio cutting and making weird noises".

    Measured on the real channel: a spliced stream decoded with "Header
    missing" and "invalid new backstep -1"; the same splice with the tag
    suppressed decoded with **zero** errors.
    """
    if not has_ffmpeg():
        # ty mis-resolves pytest.skip (see tests/conftest.py).
        pytest.skip("ffmpeg is not available")  # ty: ignore[too-many-positional-arguments]
    binary = ffmpeg_binary()
    # A source with ID3 metadata, like the station that showed the problem:
    # ffmpeg copies the input's tag into the output, which is the whole point.
    # The channel in question serves raw ADTS AAC behind an ID3v2 tag.
    source = tmp_path_factory.mktemp("splice")
    raw = source / "source.aac"
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=6",
            "-ac",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-f",
            "adts",
            str(raw),
        ],
        check=True,
        timeout=120,
        capture_output=True,
    )
    payload = raw.read_bytes()
    assert payload, "the fixture produced no audio"

    command = ffmpeg_mod.build_command(Settings(), "adts")

    def encode(data: bytes) -> bytes:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        out, _ = proc.communicate(data, timeout=120)
        return out

    def decode_errors(data: bytes) -> list[str]:
        proc = subprocess.run(
            [binary, "-v", "error", "-f", "mp3", "-i", "pipe:0", "-f", "null", "-"],
            input=data,
            capture_output=True,
            timeout=120,
        )
        return [line for line in proc.stderr.decode("utf-8", "replace").splitlines() if line]

    first = encode(payload)
    second = encode(payload)
    assert first, "ffmpeg produced no output at all"
    assert not decode_errors(first), "a single encode must be clean"

    # The splice: two encodes, one byte stream - what a restart hands the radio.
    spliced = first + second
    assert not decode_errors(spliced), (
        f"the spliced stream is corrupt: {decode_errors(spliced)} - a radio's "
        "decoder loses frame sync here and the audio breaks up"
    )
    # And there is no tag anywhere in it, so nothing non-audio can appear in
    # the middle of the stream.
    assert not _id3_tags(spliced), (
        f"ID3 tags at {[o for o, _ in _id3_tags(spliced)]} - a tag in the middle "
        "of an MP3 stream is what a radio cannot skip"
    )


# ---------------------------------------------- the measured output rate
def _pipeline_with(stats: PipelineStats, settings_obj: Settings) -> StreamPipeline:
    pipeline = StreamPipeline.__new__(StreamPipeline)
    pipeline.stats = stats
    pipeline.settings = settings_obj
    return pipeline


def test_a_forwarded_stream_reports_its_own_bitrate():
    """The pacer needs the real rate, and the right answer depends on the engine.

    Pacing a 256 kbit/s forwarding stream at a configured 192 kbit/s would make
    the listener fall permanently behind, which the queue resolves by dropping
    audio - the very symptom being fixed.
    """
    pipeline = _pipeline_with(
        PipelineStats(mode="passthrough", seconds_in=10.0, bytes_in=320_000),
        Settings(bitrate=192_000),
    )
    assert pipeline.output_bitrate == 256_000


def test_a_transcode_reports_the_bitrate_we_asked_for():
    pipeline = _pipeline_with(
        PipelineStats(mode="transcode", seconds_in=10.0, bytes_in=320_000),
        Settings(bitrate=192_000, output_codec="mp3"),
    )
    assert pipeline.output_bitrate == 192_000


def test_the_output_rate_is_unknown_until_measured():
    pipeline = _pipeline_with(PipelineStats(mode="passthrough"), Settings())
    assert pipeline.output_bitrate is None


# ---------------------------------------------- the reload cadence
def test_the_playlist_reload_interval_is_under_half_a_segment():
    """Half a segment guarantees two checks per period, so none can be missed.

    The old rule was `target * 0.75`, and a check that found nothing new repeated
    after the same interval, so two checks could be 16.5s apart against a
    10-second period. Each occurrence lost a segment's worth of time: the pipeline
    ran at ~0.88x realtime with gaps of up to 16 seconds in which a listener heard
    nothing at all.
    """
    from radioconv.hls import PlaylistTracker

    for target in (2.0, 4.0, 6.0, 10.0, 11.0, 30.0):
        tracker = PlaylistTracker.__new__(PlaylistTracker)
        tracker.target_duration = target
        tracker.endlist = False
        interval = tracker._reload_interval()
        assert interval <= target / 2 + 1e-9, (
            f"a {target}s segment is only checked every {interval}s, so two empty "
            "checks in a row would exceed the segment period"
        )
        assert 0.5 <= interval <= 4.0, interval


def test_the_old_reload_rule_really_did_drift():
    """Pins the arithmetic that made the defect invisible.

    With a 10-second segment the old interval was 7.5s, so an empty check repeated
    at 7.5s put consecutive checks 15s apart - longer than the segment period.
    """
    target = 10.0
    assert max(1.0, target * 0.75) * 2 > target, "two old intervals exceeded one segment"
    assert min(4.0, max(0.5, target / 2)) * 2 <= target, "two new intervals fit inside one"


# ------------------------------------------- the radio's two state machines
#: A real MPEG-1 Layer III frame header: 192 kbit/s, 48 kHz, stereo.
#: 144 * 192000 / 48000 = 576 bytes per frame.
MP3_FRAME_HEADER = bytes([0xFF, 0xFB, 0xB4, 0x64])
MP3_FRAME_SIZE = 576


def _mp3_frames(count: int) -> bytes:
    """*count* valid MPEG audio frames of 192 kbit/s / 48 kHz."""
    return (MP3_FRAME_HEADER + b"\x00" * (MP3_FRAME_SIZE - 4)) * count


def _frame_offset(data: bytes) -> int:
    """Offset of the first MPEG audio frame in *data*, or -1.

    Uses the project's own parser, so the test and the fix agree on what a
    frame is - and a frame is only a frame if the *next* one is a frame too
    (a bare ``0xFF 0xFx`` occurs by chance inside every other frame).
    """
    offset = mp3_align_offset(data)
    return -1 if offset is None else offset


async def _drain(session) -> list[bytes]:
    """Everything the session's first subscriber has been handed."""
    pieces = []
    while session.subscribers and not session.subscribers[0].queue.empty():
        item = session.subscribers[0].queue.get_nowait()
        if item is None:
            break
        pieces.append(item[1])
    return pieces


async def test_a_new_listener_starts_on_an_mp3_frame_boundary():
    """A radio's decoder and its metadata parser must start together.

    A radio is two state machines over one byte stream: an MP3 decoder looking
    for frame syncs, and an ICY metadata parser counting ``metaint`` bytes. If
    the stream begins in the middle of a frame, the radio discards the partial
    frame - and the two state machines then disagree about where they are, so
    the radio reads audio bytes as metadata (garbled title) and metadata bytes
    as audio (glitching).

    The reserve is cut into fixed-size pieces, and those sizes are not a
    multiple of a frame: the produced stream is 192 kbit/s at 48 kHz, so a
    frame is 576 bytes, while the pieces are 8192 (8192 % 576 = 128). Every
    listener therefore started at a random point *inside* a frame. Measured on
    a real radio's own TCP traffic, the first frame of each connection began
    at offsets 384, 448, 512, 0, 256, 320, 128, 64 - never reliably 0.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="mp3"))
    session, _ = await subscribe(manager)
    try:
        # A stream whose frames start 384 bytes in - the phase the real radio
        # was measured at.
        session._fan_out(b"\x00" * 384 + _mp3_frames(40))
        session._flush_pending()

        pieces = await _drain(session)
        assert pieces, "the listener was given nothing"

        handed_over = b"".join(pieces)
        offset = _frame_offset(handed_over)
        assert offset == 0, (
            f"the listener's audio starts {offset} bytes into an MP3 frame; the radio "
            "has to resynchronise, and its metadata parser desynchronises with it"
        )
    finally:
        await session.close("test")


async def test_every_piece_a_listener_receives_starts_on_a_frame_boundary():
    """Not just the first piece: every piece must be frame-aligned.

    A piece that begins mid-frame is as bad as a stream that begins mid-frame:
    the radio's decoder resynchronises at every piece boundary, and its
    metadata parser - which counts a fixed number of bytes and does not care
    about frames - drifts away from it.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="mp3"))
    session, _ = await subscribe(manager)
    try:
        session._fan_out(b"\x00" * 384 + _mp3_frames(200))
        session._flush_pending()

        pieces = await _drain(session)
        assert len(pieces) > 1, "expected several pieces"

        for index, piece in enumerate(pieces):
            offset = _frame_offset(piece)
            assert offset == 0, (
                f"piece {index} starts {offset} bytes into an MP3 frame ({len(piece)} bytes)"
            )
    finally:
        await session.close("test")


async def test_alignment_does_not_lose_the_audio():
    """Aligning must not cost more than the partial frame it discards.

    The whole point of the reserve is that a joining radio has something to
    play immediately. Throwing away real audio to achieve the alignment would
    trade a garbled display for a silent one.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="mp3"))
    session, _ = await subscribe(manager)
    try:
        prefix = 384
        frames = 200
        session._fan_out(b"\x00" * prefix + _mp3_frames(frames))
        session._flush_pending()

        pieces = await _drain(session)
        handed_over = b"".join(pieces)

        # At most one frame may be lost - the partial one at the front.
        whole_frames = len(handed_over) // MP3_FRAME_SIZE
        assert whole_frames >= frames - 1, (
            f"only {whole_frames} whole frames survived out of {frames}: the "
            "alignment is throwing away audio"
        )
    finally:
        await session.close("test")


async def test_the_alignment_survives_ffmpegs_small_pipe_reads():
    """The real producer hands back 578 bytes at a time - and that broke it.

    FFmpeg's pipe returns whatever has been written, so the session is fed
    578-byte reads. A single one of those cannot contain a frame *pair* (576 +
    576 bytes), so an alignment search that only looked at the current read
    would never find the frame grid: it concluded the stream was not MP3 and
    passed everything through unaligned.

    The search has to run over the bytes held so far, not over one read.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="mp3"))
    session, _ = await subscribe(manager)
    try:
        audio = b"\x00" * 384 + _mp3_frames(200)
        for offset in range(0, len(audio), MEASURED_PIPE_READ):
            session._fan_out(audio[offset : offset + MEASURED_PIPE_READ])
        session._flush_pending()

        pieces = await _drain(session)
        assert pieces, "the listener was given nothing"

        for index, piece in enumerate(pieces):
            offset = _frame_offset(piece)
            assert offset == 0, (
                f"piece {index} starts {offset} bytes into an MP3 frame "
                f"({len(piece)} bytes) - the alignment did not survive the "
                "small pipe reads"
            )

        # And the audio must still be there: 200 frames in, nearly 200 out.
        frames_out = len(b"".join(pieces)) // MP3_FRAME_SIZE
        assert frames_out >= 200 - 1, (
            f"only {frames_out} of 200 frames survived: the alignment is throwing away audio"
        )
    finally:
        await session.close("test")


async def test_the_whole_stream_a_listener_receives_is_one_frame_chain():
    """Frame-aligned pieces, but the stream between them must still chain.

    Aligning each piece is not enough: if the pieces do not join up, the
    radio's decoder loses sync at every boundary - which is the glitching
    half of the same defect. The whole handed-over stream has to be one
    unbroken run of frames.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="mp3"))
    session, _ = await subscribe(manager)
    try:
        audio = b"\x00" * 384 + _mp3_frames(200)
        for offset in range(0, len(audio), MEASURED_PIPE_READ):
            session._fan_out(audio[offset : offset + MEASURED_PIPE_READ])
        session._flush_pending()

        pieces = await _drain(session)
        handed_over = b"".join(pieces)

        position = 0
        frames = 0
        while position + 4 <= len(handed_over):
            size = mp3_frame_size(handed_over, position)
            assert size is not None, (
                f"the stream breaks at byte {position} of {len(handed_over)}: "
                "the radio's decoder would lose sync here"
            )
            position += size
            frames += 1
        assert frames >= 199, f"only {frames} frames in the handed-over stream"
    finally:
        await session.close("test")


async def test_a_stream_that_is_not_mp3_is_handed_over_unchanged():
    """ADTS (or a forwarded stream) has no frame grid to align to.

    Aligning to a *wrong* grid would throw away audio for nothing, so the
    alignment is skipped when the output is not MP3.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="adts"))
    session, _ = await subscribe(manager)
    try:
        payload = bytes([0xFF, 0xF1, 0x50, 0x80]) + b"\x00" * 20000
        session._fan_out(payload)
        session._flush_pending()

        pieces = await _drain(session)
        handed_over = b"".join(pieces)
        assert len(handed_over) == len(payload), (
            f"{len(payload)} bytes in, {len(handed_over)} bytes out: a non-MP3 "
            "stream must not be altered"
        )
    finally:
        await session.close("test")


# ------------------------------------------------------- the format we send
#: An ADTS frame header: sync 0xFFF, layer 00, a 13-bit frame length.
#: ADTS frames are variable-length - the header carries the length - so a
#: frame is only a frame if the next one is a frame too.
def _adts_frames(count: int, length: int = 256) -> bytes:
    """*count* ADTS frames of *length* bytes each."""
    out = bytearray()
    for _ in range(count):
        header = bytearray(7)
        header[0] = 0xFF
        header[1] = 0xF1  # sync 0xFFF, layer 00 (ADTS, not MPEG audio)
        header[2] = 0x50  # MPEG-4, AAC LC, 48 kHz
        header[3] = 0x80  # channel config 1
        header[3] |= (length >> 11) & 0x03
        header[4] = (length >> 3) & 0xFF
        header[5] = ((length & 0x07) << 5) | 0x1F
        header[6] = 0xFC
        out += bytes(header) + b"\x00" * (length - 7)
    return bytes(out)


async def test_an_adts_stream_is_also_aligned_to_its_own_frames():
    """ADTS has its own frame grid, and a radio resynchronises on it too.

    The alignment used to be keyed off the *setting* (`output_codec !=
    "mp3"`), which left every ADTS output unaligned - and ADTS is what a
    station with `output_codec = "adts"` sends to the radio. The grid has to
    follow the format actually being sent, not the setting.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="adts"))
    session, _ = await subscribe(manager)
    try:
        # The pipeline says what it is sending.
        session.pipeline.stats.detected_codec = "aac"
        audio = b"\x00" * 137 + _adts_frames(60)
        for offset in range(0, len(audio), MEASURED_PIPE_READ):
            session._fan_out(audio[offset : offset + MEASURED_PIPE_READ])
        session._flush_pending()

        pieces = await _drain(session)
        assert pieces, "the listener was given nothing"

        for index, piece in enumerate(pieces):
            offset = adts_align_offset(piece)
            assert offset == 0, (
                f"piece {index} starts {offset} bytes into an ADTS frame "
                f"({len(piece)} bytes) - the ADTS stream is not aligned"
            )
    finally:
        await session.close("test")


async def test_an_auto_codec_station_sending_mp3_is_aligned():
    """`output_codec = "auto"` forwards the source untouched - which may be MP3.

    Keying the alignment off the *setting* would skip this case entirely,
    because the setting is neither "mp3" nor "adts". The grid has to follow
    what is actually sent.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="auto"))
    session, _ = await subscribe(manager)
    try:
        # The pipeline detected an MP3 source and forwards it untouched.
        session.pipeline.stats.detected_codec = "mp3"
        audio = b"\x00" * 384 + _mp3_frames(60)
        for offset in range(0, len(audio), MEASURED_PIPE_READ):
            session._fan_out(audio[offset : offset + MEASURED_PIPE_READ])
        session._flush_pending()

        pieces = await _drain(session)
        assert pieces, "the listener was given nothing"

        for index, piece in enumerate(pieces):
            offset = _frame_offset(piece)
            assert offset == 0, (
                f"piece {index} starts {offset} bytes into an MP3 frame "
                f"({len(piece)} bytes) - a forwarded MP3 stream is not aligned"
            )
    finally:
        await session.close("test")


async def test_a_stream_whose_format_is_not_known_yet_is_handed_over_unchanged():
    """Before the first segment arrives, the format is unknown.

    Aligning to a guessed grid would throw away audio for nothing, so the
    audio goes out untouched until the pipeline says what it is sending.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="auto"))
    session, _ = await subscribe(manager)
    try:
        payload = bytes([0xFF, 0xF1, 0x50, 0x80]) + b"\x00" * 20000
        session._fan_out(payload)
        session._flush_pending()

        pieces = await _drain(session)
        handed_over = b"".join(pieces)
        assert len(handed_over) == len(payload), (
            f"{len(payload)} bytes in, {len(handed_over)} bytes out: a stream "
            "of unknown format must not be altered"
        )
    finally:
        await session.close("test")
