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
import time

from radioconv.api import _Pacer
from radioconv.config import Settings
from radioconv.pipeline import PipelineStats, StreamPipeline
from radioconv.session import SessionManager

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
