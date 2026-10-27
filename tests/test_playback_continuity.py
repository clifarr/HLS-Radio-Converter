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
from radioconv.api import (
    ICY_METAINT,
    MAX_WRITE_SECONDS,
    _insert_icy_metadata,
    _ListenerStream,
    _Pacer,
    icy_metadata_block,
    icy_metaint_for_frame,
    stall_watchdog_due,
    write_slices,
)
from radioconv.config import Settings
from radioconv.pipeline import PipelineStats, StreamPipeline
from radioconv.session import SessionManager, StreamSession
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


# ------------------------------------------- one write, one slice of audio
#: The rate a radio consumes audio at, per stream bitrate. The whole point of
#: these tests: the *piece* is a fixed 8192 bytes, so how long it lasts varies by
#: a factor of ten across the bitrates this project serves.
RATES = {
    "32 kbit/s": 32_000 / 8,
    "64 kbit/s (the reported station)": 64_000 / 8,
    "128 kbit/s": 128_000 / 8,
    "192 kbit/s (the shipped default)": 192_000 / 8,
    "320 kbit/s": 320_000 / 8,
}


def test_a_socket_write_never_carries_more_than_a_quarter_second_of_audio():
    """The reported defect: at 64 kbit/s one write was a *second* of audio.

    Reported as *"constant repeating every few seconds, much worse than
    radiom2o"*, on a 64 kbit/s HE-AAC station, with the user's own diagnosis
    attached: *"maybe because it's lower bitrate?"* - which was exactly right.

    A queue piece is a fixed 8192 bytes, so the silence that follows handing one
    over is ``8192 / rate``: 0.34 s at the shipped 192 kbit/s, but **1.02 s** at
    64 kbit/s. Measured on the real station: one ~8.5 KiB burst every ~1.02 s
    with 1.1 s of nothing in between - just under the ~1.2 s a Frontier radio
    tolerates, so it dropped the connection every second or two and the rejoin
    replayed ``handoff_overlap_seconds`` of audio it had already played.

    So the ceiling has to be a *duration*, and it has to hold at every bitrate.
    """
    for label, rate in RATES.items():
        chunk = bytes(8192)
        slices = write_slices(chunk, rate)
        assert b"".join(slices) == chunk, f"{label}: slicing lost or duplicated audio"

        for piece in slices:
            seconds = len(piece) / rate
            assert seconds <= MAX_WRITE_SECONDS + 1e-9, (
                f"{label}: one write carried {seconds:.2f}s of audio - a radio "
                f"gets nothing to read for that long"
            )
        assert len(slices) >= 1


def test_the_silence_between_writes_is_short_compared_with_a_radios_tolerance():
    """The number that matters is the gap, and a radio gives up at ~1.2 s.

    At least eight writes per second at *every* bitrate this project serves, so
    the stream is a trickle rather than a burst - which is what the whole pacer
    exists to produce (see the module docstring: 31 of 40 seconds carrying zero
    bytes).
    """
    for label, rate in RATES.items():
        slices = write_slices(bytes(8192), rate)
        gap = len(slices[0]) / rate
        assert gap * 4 <= 1.2, f"{label}: a radio could see {gap:.2f}s of silence"
        assert gap <= 0.25, f"{label}: {gap:.2f}s between writes"


def test_a_piece_shorter_than_one_write_goes_out_whole():
    """A small piece (a flushed remainder, or a 320 kbit/s chunk) is one write."""
    assert write_slices(b"abc", 8000.0) == [b"abc"]
    assert write_slices(b"", 8000.0) == []
    # 8192 bytes at 320 kbit/s is 0.20 s, under the ceiling.
    assert write_slices(bytes(8192), 40_000.0) == [bytes(8192)]


def test_the_slice_size_never_rounds_down_to_nothing():
    """A very low bitrate must still make progress, not spin on empty writes."""
    for rate in (1.0, 0.5, 100.0):
        slices = write_slices(bytes(2000), rate)
        assert slices and all(slices), f"rate {rate} produced an empty write"
        assert b"".join(slices) == bytes(2000)


def test_slicing_is_skipped_when_pacing_is_off():
    """`pace_output = false` means an infinite rate, not an infinite slice count.

    ``_pace_rate`` answers ``inf`` when pacing is disabled, which is the
    documented escape hatch for diagnosing pacing itself. Multiplying that out
    would raise ``OverflowError`` (an infinity is not an integer), so a listener
    would be dropped for a reason that has nothing to do with its stream - the
    kind of collision between two settings that only a test like this one
    catches.
    """
    assert write_slices(bytes(2000), float("inf")) == [bytes(2000)]
    assert write_slices(bytes(2000), float("nan")) == [bytes(2000)]


# ------------------------------- the socket write has a deadline as well
class _StalledSocket:
    """An ASGI ``send`` whose writes never complete, and what it was given.

    A radio that loses power is exactly this: a TCP connection whose peer stops
    acknowledging. The kernel buffers keep the application writing for a while
    (measured: the socket absorbed 2.8 s of stall before a write blocked at all),
    and then one ``send`` never returns.
    """

    def __init__(self, block: bool = False) -> None:
        self.messages: list = []
        self._block = block

    async def send(self, message: dict) -> None:
        if self._block and message.get("body"):
            await asyncio.Event().wait()
        self.messages.append(message)


def _audio_stream(chunks, closed: list):
    """A body generator that records whether it was closed."""

    async def _gen():
        try:
            for chunk in chunks:
                yield chunk
        finally:
            closed.append("closed")

    return _gen()


async def _not_abandoned() -> None:
    raise AssertionError("a listener that is reading must never be abandoned")


async def test_a_radio_that_stops_reading_is_dropped_instead_of_waited_for():
    """A powered-off radio never sends a FIN, so the *kernel* owns the decision.

    Reported: *"if the radio loses power suddenly, the server keeps thinking the
    radio is listening ... `dropped` keeps accumulating, but it keeps the stream
    active for a very long time, like 15 minutes."* Fifteen minutes is
    ``tcp_retries2``: the peer acknowledges nothing, TCP retransmits fifteen
    times, the send buffer fills and the write blocks. The stall watchdog cannot
    see it, because it is evaluated *between* writes and the write is the thing
    that is stuck - so the generator sits there until the kernel gives up.

    This is the bound that replaces that wait: no single write may block for
    longer than ``listener_write_timeout``, and when one does the listener is
    unsubscribed and the response ends, which makes the server close a socket
    that will never accept another byte.
    """
    socket_ = _StalledSocket(block=True)
    closed: list = []
    abandoned: list = []

    async def on_abandoned() -> None:
        abandoned.append(True)

    response = _ListenerStream(
        _audio_stream([bytes(8), bytes(8), bytes(8)], closed),
        write_timeout=0.05,
        on_abandoned=on_abandoned,
        media_type="audio/mpeg",
        status_code=200,
    )

    # The outer bound is the assertion as much as a safety net: without the
    # deadline this call never returns, and a test that hangs is worse than one
    # that fails - the guard script has to report a red test, not stall.
    await asyncio.wait_for(response.stream_response(socket_.send), timeout=5.0)

    assert abandoned == [True], "the listener was never unsubscribed, so it stays on the page"
    assert closed == ["closed"], (
        "the body generator was never closed, so its `finally` never logs "
        "`listener done` and never runs the cleanup it owns"
    )
    # The response ends *without* a terminating chunk, deliberately: that is the
    # only way to make the server close a socket whose write buffer is full. See
    # the class docstring.
    assert all(m.get("more_body") is not False for m in socket_.messages), (
        "the response claimed to be complete: the server would keep a dead "
        "connection alive for the rest of the kernel's retransmission timeout"
    )


async def test_a_working_radio_still_gets_the_whole_stream():
    """The deadline must be invisible to a radio that is reading: no clipping."""
    socket_ = _StalledSocket()

    response = _ListenerStream(
        _audio_stream([b"aaaa", b"bbbb"], []),
        write_timeout=5.0,
        on_abandoned=_not_abandoned,
        media_type="audio/mpeg",
        status_code=200,
    )
    await response.stream_response(socket_.send)

    assert [m.get("body") for m in socket_.messages[1:]] == [b"aaaa", b"bbbb", b""]
    assert socket_.messages[0]["type"] == "http.response.start"
    assert socket_.messages[-1]["more_body"] is False, "the response was not terminated"


async def test_the_write_deadline_can_be_turned_off():
    """0 means "no deadline", and nothing else may change: a slow write finishes.

    It is a diagnostic setting, for telling a genuinely dead peer from a busy
    one, so it has to restore the stock behaviour exactly rather than merely
    loosen it.
    """
    socket_ = _StalledSocket()
    seen: list = []

    async def slow_send(message: dict) -> None:
        if message.get("body"):
            seen.append(message.get("body"))
            await asyncio.sleep(0.2)  # slower than the deadline below would allow
        socket_.messages.append(message)

    response = _ListenerStream(
        _audio_stream([b"aaaa"], []),
        write_timeout=0.0,
        on_abandoned=_not_abandoned,
        media_type="audio/mpeg",
        status_code=200,
    )
    await response.stream_response(slow_send)

    assert seen == [b"aaaa"]
    assert socket_.messages[-1]["more_body"] is False


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


# --------------------------------------------------- the stall watchdog
async def test_the_pacer_reports_the_silence_it_forgave():
    """The watchdog needs the gap the pacer hides.

    ``wait`` forgives a starvation debt so it is never repaid as a burst - which
    is right - but that also erases the evidence that anything went wrong. It
    returns the lag it measured *before* forgiving it, because that number is
    the only record that the listener heard nothing for that long.

    The anchoring call must return zero. Its schedule is deliberately placed
    ``initial_burst`` behind now, so reporting a lag there would invent a stall
    of exactly the burst length and close every connection as it opened.
    """
    pacer = _Pacer(lambda: FAST_RATE, initial_burst=1.5)
    assert await pacer.wait(8192) == 0.0, (
        "the anchoring call reported a stall: the initial-burst credit is being "
        "read as a gap, which would close every connection the moment it opens"
    )
    for _ in range(5):
        assert await pacer.wait(8192) < 0.05, "a pacer that is on time reported a lag"

    await asyncio.sleep(0.4)
    lag = await pacer.wait(8192)
    assert 0.3 < lag < 2.0, (
        f"a 0.4s silence was reported as {lag:.3f}s: the watchdog would not see a real stall"
    )


def _bare_session(**overrides) -> StreamSession:
    """A session object with no pipeline, for the watchdog arithmetic only."""
    s = settings(**overrides)
    return StreamSession(s, "http://x/one.m3u8", ["http://x/one.m3u8"])


def test_the_watchdog_fires_only_past_the_threshold():
    """1.0 s is chosen to sit under the stalls actually measured.

    The two stalls this watchdog fired on in the field were **1.80 s** and
    **2.27 s** (journal, during microwave interference). A gap under a second
    has to be tolerated, or ordinary jitter would close connections; anything
    at or above a second has to fire, or the radio is left to wedge.
    """
    session = _bare_session()
    s = settings()

    assert not stall_watchdog_due(0.0, session, s, now=100.0), "fired with no gap"
    assert not stall_watchdog_due(0.99, session, s, now=100.0), (
        "fired below the threshold: ordinary jitter would close connections"
    )
    assert stall_watchdog_due(1.0, session, s, now=100.0), "did not fire at the threshold"
    assert stall_watchdog_due(1.80, session, s, now=100.0), (
        "did not fire on the smaller of the two stalls measured in the field "
        "(1.80 s), which is the whole point of the threshold"
    )
    assert stall_watchdog_due(2.27, session, s, now=100.0), (
        "did not fire on the larger measured stall (2.27 s)"
    )


def test_the_watchdog_can_be_turned_off():
    session = _bare_session()
    s = settings(stall_reconnect_seconds=0.0)
    assert not stall_watchdog_due(60.0, session, s, now=100.0), (
        "the watchdog fired with stall_reconnect_seconds = 0"
    )


def test_the_watchdog_respects_its_cooldown():
    """A station-wide outage must not become a reconnect storm.

    Without the cooldown, a starved station closes and reopens every connection
    once a second, forever. This project has produced exactly that: 6940
    connections in 89 seconds, from a different bug but the same shape.
    """
    session = _bare_session()
    s = settings()
    assert s.stall_reconnect_cooldown_seconds > 0.0, "a zero cooldown allows a storm"

    session.last_stall_reconnect = 100.0
    assert not stall_watchdog_due(5.0, session, s, now=100.5), (
        "fired again half a second after closing a connection"
    )
    assert not stall_watchdog_due(
        5.0, session, s, now=100.0 + s.stall_reconnect_cooldown_seconds - 0.1
    ), "fired inside the cooldown window"
    assert stall_watchdog_due(5.0, session, s, now=100.0 + s.stall_reconnect_cooldown_seconds), (
        "never fired again after the cooldown expired"
    )


def test_the_watchdog_is_counted_and_reported():
    """The event has to be visible, or nobody can tell it happened."""
    session = _bare_session()
    session.stall_reconnects = 3
    assert session.as_dict()["session"]["stall_reconnects"] == 3, (
        "the watchdog fired but the session does not report it"
    )


async def test_the_watchdog_total_never_falls():
    """A Prometheus counter must be monotonic - observed going 1 then 0.

    The first version summed the per-session counts over the live sessions. A
    session is reaped once its radios have been gone for ``linger_seconds``, so
    when the station the watchdog had acted on went away its contribution
    vanished and the total dropped from 1 back to 0. Seen on the Pi during the
    microwave test. A counter that decreases breaks ``rate()``, and it is also
    simply untrue: the event did happen.
    """
    from radioconv.metrics import snapshot
    from radioconv.monitor import ResourceMonitor

    manager = SessionManager(settings())
    await manager.start()
    try:
        monitor = ResourceMonitor(settings())
        session, sub = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="NSPlayer/8.0.0.3801",
        )

        manager.note_stall_reconnect()
        first = snapshot(manager, monitor)["counters"]["stall_reconnects_total"]
        assert first == 1, "the watchdog count did not reach the metrics"

        # The station goes away entirely, exactly as it does after a rejoin.
        await manager.unsubscribe(session, sub)
        manager.sessions.clear()

        after = snapshot(manager, monitor)["counters"]["stall_reconnects_total"]
        assert after >= first, (
            f"stall_reconnects_total fell from {first} to {after} after the "
            "session was reaped: it is being summed over live sessions instead "
            "of kept as a service-wide counter"
        )
    finally:
        await manager.aclose()


# ------------------------------------------------------- ICY song metadata
async def test_icy_metadata_blocks_land_on_the_audio_grid():
    """A block every `metaint` audio bytes; removing blocks restores the stream.

    The heritage Shoutcast grid - the length byte counts 16-byte payload
    blocks - pinned against a live Icecast capture.
    """
    audio = b"\xff\xf1" * (ICY_METAINT // 2)  # exactly ICY_METAINT bytes = one grid window

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


# ------------------------------------------- the grid must not split a frame
def test_icy_grid_is_a_whole_number_of_audio_frames():
    """The metadata block must never land in the middle of an audio frame.

    This is the defect that made the radio glitch forever.  A radio is two
    state machines over one byte stream: an MP3 decoder looking for frame
    syncs, and an ICY parser counting audio bytes.  The block is inserted
    into the audio, so it lands wherever the grid count falls.

    For the 192 kbit/s, 48 kHz output the frame is 576 bytes, and
    16000 / 576 = 27.78 - so with the heritage 16000-byte grid, 89 % of the
    blocks split a frame in half.  The radio then has to reassemble a frame
    the block cut in two; a radio that gets that wrong loses frame sync and
    reads audio as text, permanently, because the offset never changes.

    So the grid has to be a whole number of frames.
    """
    assert ICY_METAINT % 576 == 0, (
        f"the ICY grid ({ICY_METAINT}) is not a multiple of the 576-byte MP3 "
        "frame, so a metadata block lands inside a frame"
    )


def test_icy_grid_is_a_whole_number_of_shoutcast_blocks():
    """The length byte counts 16-byte payload blocks, so the grid stays a
    multiple of 16 - otherwise the Shoutcast convention is broken."""
    assert ICY_METAINT % 16 == 0


def test_icy_metaint_for_frame_picks_a_whole_number_of_frames():
    """`icy_metaint_for_frame` returns a grid that is a multiple of the frame."""
    for frame_size in (576, 417, 313, 1440, 2880):
        grid = icy_metaint_for_frame(frame_size)
        assert grid % frame_size == 0, (
            f"grid {grid} is not a multiple of the {frame_size}-byte frame"
        )
        assert grid % 16 == 0, f"grid {grid} is not a multiple of 16"


def test_icy_metaint_for_frame_falls_back_when_the_frame_is_unknown():
    """No frame size yet (the stream has not been identified): use the
    configured grid rather than dividing by zero."""
    assert icy_metaint_for_frame(None) == ICY_METAINT
    assert icy_metaint_for_frame(0) == ICY_METAINT
    assert icy_metaint_for_frame(-1) == ICY_METAINT


def test_icy_metaint_for_frame_stays_close_to_the_target():
    """The grid must not drift far from the heritage 16000: the wire rate
    changes with it, and a radio is told the rate in `icy-br`."""
    for frame_size in (576, 417, 313, 1440, 2880):
        grid = icy_metaint_for_frame(frame_size)
        assert abs(grid - 16000) <= 16000 // 4, (
            f"grid {grid} for a {frame_size}-byte frame is too far from 16000"
        )


async def test_the_block_lands_on_a_frame_boundary_in_a_real_stream():
    """End to end: a real 576-byte-frame stream, with the blocks inserted, must
    still be a continuous chain of frames once the blocks are removed - and
    the block must fall exactly on a frame boundary in the raw wire."""
    # MPEG-1 Layer III, 192 kbit/s, 48 kHz: a 576-byte frame.
    frame = bytes.fromhex("fffb9864") + b"\x00" * 572
    frames = frame * 64  # 64 frames = 36864 bytes
    assert len(frames) % 576 == 0
    assert mp3_frame_size(frames, 0) == 576, "the test frame is not 576 bytes"

    async def chunks():
        yield frames[:20000]
        yield frames[20000:]

    metaint = icy_metaint_for_frame(576)
    gen = _insert_icy_metadata(chunks(), metaint, lambda: "A Title")
    out = bytearray()
    async for piece in gen:
        out.extend(piece)

    # --- the block sits on a frame boundary in the AUDIO.
    #
    # The wire is: `metaint` audio bytes, then the length byte, then the
    # payload, then `metaint` more audio bytes, and so on.  The stride
    # between blocks on the wire is therefore `metaint + 1 + length*16`,
    # because the block itself occupies wire bytes.
    #
    # What matters is the offset in the AUDIO, because that is what the
    # radio's decoder sees once it has stripped the block: the audio
    # before each block must be a whole number of frames, so the radio
    # never has to reassemble a frame the block cut in half.
    block_at = []
    pos = metaint
    while pos < len(out):
        length = out[pos]
        assert 0 < length <= 15, f"bad length byte {length} at {pos}"
        block_at.append(pos)
        pos += 1 + length * 16 + metaint
    assert block_at, "no metadata block was inserted"

    audio = bytearray()
    pos = 0
    audio_offset_of = []
    for at in block_at:
        audio += out[pos:at]
        audio_offset_of.append(len(audio))
        length = out[at]
        pos = at + 1 + length * 16
    audio += out[pos:]

    for off in audio_offset_of:
        assert off % 576 == 0, (
            f"a block follows {off} audio bytes, which is {off % 576} bytes "
            "into an MP3 frame - the radio would have to reassemble it"
        )

    # --- and the audio is still a continuous frame chain
    assert len(audio) % 576 == 0
    off = 0
    count = 0
    while off < len(audio):
        size = mp3_frame_size(bytes(audio), off)
        assert size == 576, f"frame at {off} is {size} bytes"
        count += 1
        off += size
    assert count == len(audio) // 576


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


def test_icy_title_with_a_typographic_apostrophe_does_not_truncate_it():
    """A curly apostrophe must not become an ASCII one.

    The sanitiser maps typographic punctuation to ASCII equivalents, and the
    ASCII apostrophe is the delimiter of the ``StreamTitle='...'`` value. If
    U+2019 is translated to U+0027, the value ends early - exactly the bug
    the ASCII apostrophe rule exists to prevent.

    This is a real title shape: ``It's a Sin`` (Soft Cell) and
    ``Rock 'n' Roll`` (Hall & Oates) both use the curly apostrophe, and
    plenty of stations send them that way.
    """
    for title in (
        "It’s a Sin",
        "Rock ’n’ Roll",
        "‘Quoted’",
        "don’t stop",
    ):
        block = icy_metadata_block(title)
        value, leftover = _radio_reads(block)
        assert leftover == ";", f"the value ended early for {title!r}"
        assert "'" not in value, f"an ASCII quote survived in {value!r}"
        # The title is still there, just with the acute accent in place of
        # the curly apostrophe.
        assert value.count("´") == title.count("’") + title.count("‘")


def test_no_title_sanitiser_output_can_contain_the_delimiter():
    """The invariant, over a spread of awkward titles.

    Every ASCII quote and every control character must be gone. This is the
    property the radio depends on, and it is cheap to state as a property
    rather than as one example.
    """
    from radioconv.api import sanitize_icy_title

    titles = [
        "Fool's Garden",
        "It’s a Sin",
        "Rock ’n’ Roll",
        "‘Quoted’",
        "She said “hi”",
        "a – b — c",
        "wait…",
        " nbsp",
        "€100",
        "mixed ' and ’ and “",
        "",
    ]
    for title in titles:
        text = sanitize_icy_title(title)
        assert "'" not in text, f"ASCII quote survived in {text!r} (from {title!r})"
        assert "\x00" not in text
        assert all(ch >= " " or ch == "\t" for ch in text), text
        # And it must be encodable as Latin-1 without loss.
        text.encode("latin-1")


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
    audio = b"\xff\xf1" * ICY_METAINT  # two grid windows
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
        PipelineStats(mode="passthrough", forwarded_bytes=320_000, forwarded_seconds=10.0),
        Settings(bitrate=192_000),
    )
    measured = pipeline.output_bitrate
    assert measured == 256_000


def test_the_forwarded_rate_ignores_the_container_it_arrived_in():
    """The rate must be measured on the audio, not on the download.

    Reported against a real station (radiom2o, AAC in MPEG-TS): *"every few
    seconds, it either repeats the last few seconds or else skips a few
    seconds"*. The cause was here - the rate was ``bytes_in``, which for a
    transport stream is 20 % bigger than the audio it carries (188-byte packets,
    PAT/PMT, PES headers, adaptation-field stuffing). Measured on that station:
    199 KB downloaded per 10.3 s of audio, 165 KB of elementary stream.

    Pacing at the downloaded rate hands a radio 1.25 s of audio every second. The
    jitter buffer covers that for about four segments and then runs dry, so from
    then on every segment is followed by ~2 s of silence - longer than the ~1.2 s
    a Frontier-Silicon radio tolerates, so it reconnects, and the rejoin replays
    ``handoff_overlap_seconds`` (heard as a repeat) or drops the backlog (heard
    as a skip).

    The numbers below are that station's, in miniature: a TS whose audio payload
    is a fraction of what was downloaded, over the audio's real duration.
    """
    audio_bytes = 165_148
    downloaded = 199_092  # the same 10.325 s inside MPEG-TS
    pipeline = _pipeline_with(
        PipelineStats(
            mode="passthrough",
            seconds_in=10.0,  # what the playlist claims
            bytes_in=downloaded,
            bytes_out=audio_bytes,
            forwarded_bytes=audio_bytes,
            forwarded_seconds=10.325,  # what the frames say
        ),
        Settings(bitrate=192_000),
    )

    measured = pipeline.output_bitrate
    assert measured is not None
    assert measured == audio_bytes * 8 / 10.325
    assert measured < 130_000, (
        f"{measured:.0f} bit/s is the *download* rate, not the audio rate - "
        "pacing there makes the listener run dry every segment"
    )


def test_the_forwarded_rate_ignores_what_the_playlist_claims():
    """EXTINF is the packager's claim; the frames are the measurement.

    radiom2o declares ``EXTINF:10`` for segments that hold **10.325 s** of audio
    (HE-AAC: 1024 samples per frame at a 24 kHz core rate). Using the claim is a
    +3 % error, which does not sound like much and is invisible in a short test -
    but it drains a five-second jitter buffer in under three minutes, after which
    every segment is followed by a gap. Delivering a fraction of a per cent too
    *slowly* is just as bad in the other direction: the queue fills and audio is
    dropped for the listener.
    """
    audio_bytes = 165_148
    pipeline = _pipeline_with(
        PipelineStats(
            mode="passthrough",
            seconds_in=10.0,
            bytes_out=audio_bytes,
            forwarded_bytes=audio_bytes,
            forwarded_seconds=10.325,
        ),
        Settings(bitrate=192_000),
    )

    measured = pipeline.output_bitrate
    assert measured is not None
    claimed = audio_bytes * 8 / 10.0
    assert measured < claimed, "the playlist's EXTINF must not outrank the frames"
    assert abs(measured - claimed) / claimed > 0.02, "this test needs the two to differ"


def test_the_rate_falls_back_to_the_playlist_before_any_frames_are_counted():
    """With no counted frames yet, use the bytes we have over the declared time.

    This is the first moments of a forwarded stream (a whole segment has to
    arrive before its frame headers can be counted), and the last resort of
    ``output_bitrate``. It used to also be the path for a ``-c:a copy`` remux;
    that branch is gone - nothing copies any more - but the fallback still has
    to answer something rather than ``None``, because ``None`` means "no pacing".
    """
    pipeline = _pipeline_with(
        PipelineStats(mode="transcode", seconds_in=10.0, bytes_out=240_000),
        Settings(bitrate=192_000, output_codec="adts"),
    )
    assert pipeline.output_bitrate == 192_000


def test_a_transcode_reports_the_bitrate_we_asked_for():
    """A re-encode runs at the bitrate we chose, so that *is* the answer.

    ``bytes_out`` is deliberately not a round multiple of the bitrate here: it
    used to be (240 KB over 10 s is 192 kbit/s), which made this test pass
    whether the constant was used or the measurement was - and the guard script
    for "the bitrate is no longer measured" then had nothing to catch.
    """
    pipeline = _pipeline_with(
        PipelineStats(mode="transcode", seconds_in=10.0, bytes_out=250_000),
        Settings(bitrate=192_000, output_codec="mp3"),
    )
    assert pipeline.output_bitrate == 192_000, "the *measured* figure would be 200 kbit/s"


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


#: A 44.1 kHz MPEG-1 Layer III frame header at 192 kbit/s, without the
#: padding bit. The frame is 626 bytes.
MP3_44K_HEADER = bytes([0xFF, 0xFB, 0xB0, 0x00])
#: The same frame with the padding bit set: 627 bytes. 44.1 kHz does not
#: divide evenly into 1152 samples, so real encoders alternate between the
#: two sizes - which is exactly why a frame length cannot be a constant.
MP3_44K_HEADER_PADDED = bytes([0xFF, 0xFB, 0xB2, 0x00])


def _mp3_44k_frames(count: int) -> bytes:
    """*count* alternating 417/418-byte MPEG-1 Layer III frames at 44.1 kHz."""
    out = bytearray()
    for index in range(count):
        header = MP3_44K_HEADER_PADDED if index % 2 else MP3_44K_HEADER
        size = mp3_frame_size(header)
        assert size is not None, f"{header!r} is not a frame header"
        out += header
        out += b"\x00" * (size - 4)
    return bytes(out)


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


async def test_the_tail_of_the_stream_is_not_trimmed_to_a_frame_multiple():
    """Closing the stream must not delete valid audio.

    The tail used to be trimmed to a multiple of the *first* frame's length.
    That is wrong for two reasons: MP3 frames alternate between two sizes
    when the sample rate needs a padding byte, and ADTS frames are
    variable-length by definition. For two 44.1 kHz frames of 626 and 627
    bytes, ``1253 % 626`` is 1, so one perfectly valid byte was deleted from
    the end of every stream.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="mp3"))
    session, _ = await subscribe(manager)
    try:
        # Two whole frames of 626 and 627 bytes: 1253 bytes in total, which
        # is not a multiple of 626.
        payload = _mp3_44k_frames(2)
        assert len(payload) == 1253
        assert len(payload) % 626 == 1, "the tail must not be a frame multiple"

        session._fan_out(payload)
        session._flush_pending()

        pieces = await _drain(session)
        handed_over = b"".join(pieces)
        assert handed_over == payload, (
            f"expected {len(payload)} bytes, got {len(handed_over)}: "
            f"{len(payload) - len(handed_over)} bytes were lost at the end"
        )
    finally:
        await session.close("test")


async def test_a_partial_frame_at_the_end_is_dropped_but_the_whole_frames_are_not():
    """The tail of an *incomplete* frame is dropped; the frames before it are not.

    A partial frame is not decodable, so it is dropped - the radio would
    discard it anyway. But the whole frames that came before it must all
    arrive: the trim used to take them with it.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="mp3"))
    session, _ = await subscribe(manager)
    try:
        whole = _mp3_44k_frames(3)  # 626 + 627 + 626 = 1879 bytes
        partial = bytes([0xFF, 0xFB, 0xB2, 0x00]) + b"\x11" * 10
        session._fan_out(whole + partial)
        session._flush_pending()

        pieces = await _drain(session)
        handed_over = b"".join(pieces)
        assert handed_over == whole, (
            f"expected the {len(whole)} whole bytes, got {len(handed_over)}"
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


async def test_a_large_adts_stream_is_still_aligned():
    """The search budget must fit the format it is searching for.

    The old budget was ``4 * FRAME_HINT`` = 2304 bytes, a number taken from
    the *MP3* frame size and applied to every format. An ADTS frame can be
    up to 8191 bytes, so two of them do not fit in 2304 bytes - and the
    search was given up on before a frame *pair* could ever be found. A
    high-bitrate AAC stream could therefore never be aligned at all.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="adts"))
    session, _ = await subscribe(manager)
    try:
        session.pipeline.stats.detected_codec = "aac"
        # Two frames of 4000 bytes: the pair alone is 8000 bytes, which is
        # more than the old 2304-byte budget.
        audio = _adts_frames(12, length=4000)
        for offset in range(0, len(audio), MEASURED_PIPE_READ):
            session._fan_out(audio[offset : offset + MEASURED_PIPE_READ])
        session._flush_pending()

        pieces = await _drain(session)
        assert pieces, "the listener was given nothing"
        for index, piece in enumerate(pieces):
            offset = adts_align_offset(piece)
            assert offset == 0, (
                f"piece {index} starts {offset} bytes into an ADTS frame "
                f"({len(piece)} bytes) - a large ADTS stream is not aligned"
            )
    finally:
        await session.close("test")


async def test_a_stream_that_opens_with_a_preamble_is_still_aligned():
    """A stream that starts with something else is not "not MP3" for ever.

    The "is there any sync byte at all" test used to look only at the
    first 4096 bytes, and the "this is not this format" decision was made
    on the *first* chunk. A station that opens with a few hundred bytes of
    preamble would then be declared "not MP3" before its first frame
    arrived, and the alignment would never happen - even though the stream
    is MP3.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="mp3"))
    session, _ = await subscribe(manager)
    try:
        session.pipeline.stats.detected_codec = "mp3"
        # 600 bytes of preamble - enough to fill the first chunk, not enough
        # to contain a frame.
        preamble = b"\x11" * 600
        audio = preamble + _mp3_frames(60)
        for offset in range(0, len(audio), MEASURED_PIPE_READ):
            session._fan_out(audio[offset : offset + MEASURED_PIPE_READ])
        session._flush_pending()

        pieces = await _drain(session)
        assert pieces, "the listener was given nothing"
        for index, piece in enumerate(pieces):
            offset = _frame_offset(piece)
            assert offset == 0, (
                f"piece {index} starts {offset} bytes into an MP3 frame "
                f"({len(piece)} bytes) - a stream with a preamble is not aligned"
            )
    finally:
        await session.close("test")


async def test_a_stream_that_is_really_not_mp3_is_still_handed_over():
    """The other half: the search must still give up eventually.

    A stream that is not MP3 must not be buffered for ever waiting for a
    frame that will never come.
    """
    manager = SessionManager(settings(chunk_size=8192, output_codec="mp3"))
    session, _ = await subscribe(manager)
    try:
        session.pipeline.stats.detected_codec = "mp3"
        # 64 KB of noise: no sync word anywhere, and certainly no frame pair.
        audio = bytes((i * 37) & 0xFF for i in range(65536))
        for offset in range(0, len(audio), MEASURED_PIPE_READ):
            session._fan_out(audio[offset : offset + MEASURED_PIPE_READ])
        session._flush_pending()

        pieces = await _drain(session)
        handed_over = b"".join(pieces)
        assert handed_over, "the listener was given nothing"
        # The audio must not have been thrown away: the whole point of
        # giving up is to hand it over untouched.
        assert len(handed_over) >= len(audio) - 1024
    finally:
        await session.close("test")


async def test_a_codec_change_under_the_session_does_not_silence_it():
    """A pipeline restart can change the codec, and the grid must follow.

    When the station fails over, the new pipeline may be sending a different
    codec. Everything the session knew about the frame grid - the frame
    size, the grid, and the bytes held back for a frame that will never
    complete - belongs to the *old* format.

    With a stale grid the session delivered **zero** bytes: the radio went
    silent and stayed silent. The pieces were cut on the old format's
    boundaries and the alignment search was looking for MP3 frames in an
    ADTS stream.
    """
    manager = SessionManager(settings(chunk_size=1024, output_codec="auto"))
    session, _ = await subscribe(manager)
    try:
        # An MP3 stream first: the grid is found and the format recorded.
        session.pipeline.stats.detected_codec = "mp3"
        session._fan_out(_mp3_frames(3))
        assert session._grid_format == "mp3"
        # Take the MP3 pieces out of the queue: they are legitimately
        # delivered, and this test is about what comes *after* the change.
        await _drain(session)

        # The pipeline restarts and the station is now sending ADTS.
        session.pipeline.stats.detected_codec = "aac"
        session._fan_out(_adts_frames(20, length=300))
        session._flush_pending()

        pieces = await _drain(session)
        assert pieces, "the session went silent after the codec changed"
        assert session._grid_format == "adts", "the grid must follow the codec"
        for index, piece in enumerate(pieces):
            offset = adts_align_offset(piece)
            assert offset == 0, (
                f"piece {index} starts {offset} bytes into an ADTS frame "
                f"({len(piece)} bytes) after the codec changed"
            )
    finally:
        await session.close("test")


async def test_a_codec_change_drops_the_bytes_of_the_old_format():
    """The held-back bytes of the old format must not be handed over.

    They are the tail of a frame in the *old* format. A radio decoding the
    new format would find garbage in front of its first frame.
    """
    manager = SessionManager(settings(chunk_size=1024, output_codec="auto"))
    session, _ = await subscribe(manager)
    try:
        session.pipeline.stats.detected_codec = "mp3"
        # A partial MP3 frame at the end leaves bytes in the hold-back.
        session._fan_out(_mp3_frames(2) + MP3_FRAME_HEADER + b"\x11" * 10)
        assert session._align_pending, "the test needs held-back bytes"
        await _drain(session)

        session.pipeline.stats.detected_codec = "aac"
        session._fan_out(_adts_frames(20, length=300))
        session._flush_pending()

        pieces = await _drain(session)
        handed_over = b"".join(pieces)
        # The old format's bytes must not be in there.
        assert not handed_over.startswith(MP3_FRAME_HEADER), (
            "the old format's held-back bytes were handed to the listener"
        )
        for index, piece in enumerate(pieces):
            offset = adts_align_offset(piece)
            assert offset == 0, f"piece {index} starts {offset} bytes into an ADTS frame"
    finally:
        await session.close("test")


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
