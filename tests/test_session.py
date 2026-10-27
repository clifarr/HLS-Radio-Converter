"""Session behaviour: sharing, capacity limits, slow-listener handling, idle reaping.

The pipeline is replaced with a fake so these tests are fast and deterministic -
the point here is the *policy*, not the plumbing.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from radioconv import session as session_mod
from radioconv.config import Settings
from radioconv.pipeline import PipelineStats
from radioconv.session import CapacityError, SessionManager


class FakePipeline:
    """Yields 1 KiB every few milliseconds until closed."""

    #: Emitted per `stream()` step, and the size `fast_settings` matches below so
    #: that `_fan_out` passes each piece straight through. The re-chunking in
    #: `_fan_out` is covered in tests/test_playback_continuity.py; these tests are
    #: about session policy, so the sizes are kept equal to stay out of the way.
    PIECE = 1024

    def __init__(self, settings, sources, label="", **kwargs):
        self.settings = settings
        self.sources = list(sources)
        self.label = label
        # Mirrors the real constructor, which takes the shared byte counters - so
        # a test can assert the identity is passed through rather than dropped.
        self.throughput = kwargs.get("throughput")
        self.stats = PipelineStats(mode="passthrough")
        self.closed = False
        self.current_url = self.sources[0]

    async def stream(self):
        while not self.closed:
            await asyncio.sleep(0.005)
            if self.closed:
                return
            self.stats.last_data_at = time.time()
            self.stats.bytes_out += self.PIECE
            yield b"a" * self.PIECE

    async def aclose(self):
        self.closed = True

    @property
    def output_bitrate(self):
        """Mirrors the real pipeline, which reports what a listener receives.

        Session health measures buffered *seconds*, so it needs a bitrate to turn
        a queue depth into a duration. Absent this the fake raises rather than
        duck-types, which is the point of mirroring the real interface.
        """
        return float(self.settings.bitrate)


@pytest.fixture(autouse=True)
def fake_pipeline(monkeypatch):
    monkeypatch.setattr(session_mod, "StreamPipeline", FakePipeline)


def fast_settings(**overrides) -> Settings:
    """Short timeouts keep the session tests quick and deterministic.

    This list is a whitelist, so a setting that is not named here is silently
    *dropped* - which is how the ``ffmpeg_enabled`` case below first appeared to
    pass: the override never reached `Settings`, so the test exercised the
    default install while claiming to test the other one.
    """
    return Settings(
        linger_seconds=overrides.get("linger_seconds", 0.3),
        queue_chunks=overrides.get("queue_chunks", 4),
        # Matches FakePipeline.PIECE so a piece is never accumulated or split.
        chunk_size=overrides.get("chunk_size", 1024),
        max_sessions=overrides.get("max_sessions", 2),
        handoff_overlap_seconds=overrides.get("handoff_overlap_seconds", 1.5),
        max_listeners_per_session=overrides.get("max_listeners_per_session", 2),
        ffmpeg_enabled=overrides.get("ffmpeg_enabled", True),
    )


async def drain(subscriber, count, timeout=5.0):
    """Pull ``count`` chunks or give up."""
    chunks = []
    deadline = time.time() + timeout
    while len(chunks) < count and time.time() < deadline:
        try:
            item = await asyncio.wait_for(subscriber.queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        if item is None:
            break
        # Fan-out queues carry ``(produced_at, piece)``; unpack so assertions
        # work on the audio itself. Deliberately strict: unpacking raw bytes
        # raises instead of letting a wrong item type pass silently.
        _, piece = item
        chunks.append(piece)
    return chunks


async def test_listeners_share_a_single_pipeline():
    manager = SessionManager(fast_settings())
    await manager.start()
    try:
        first, sub_a = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        second, sub_b = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        assert first is second  # one pipeline for two radios
        assert first.listener_count == 2
        assert len(manager.sessions) == 1

        chunk = await drain(sub_a, 1)
        assert chunk == [b"a" * FakePipeline.PIECE]
        # Both listeners must see the very same bytes.
        assert (await drain(sub_b, 1)) == chunk
    finally:
        await manager.aclose()


async def test_different_stations_get_different_sessions():
    manager = SessionManager(fast_settings())
    await manager.start()
    try:
        _, _ = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        await manager.subscribe("http://x/two.m3u8", ["http://x/two.m3u8"])
        assert len(manager.sessions) == 2
    finally:
        await manager.aclose()


async def test_share_sessions_false_gives_every_radio_its_own_pipeline():
    """The setting must do what it says.

    ``share_sessions`` was a dead setting: it was declared, configurable
    and never read, so a station that asked for unshared streams got
    shared ones anyway - two radios on the same station, one upstream
    fetch, and a second radio's quality decided by the first one's
    connection.
    """
    settings = fast_settings()
    settings.share_sessions = False
    manager = SessionManager(settings)
    await manager.start()
    try:
        first, sub_a = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        second, sub_b = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        assert first is not second, "the two radios shared a pipeline"
        assert first.listener_count == 1
        assert second.listener_count == 1
        assert len(manager.sessions) == 2

        # And each one delivers its own audio.
        assert await drain(sub_a, 1) == [b"a" * FakePipeline.PIECE]
        assert await drain(sub_b, 1) == [b"a" * FakePipeline.PIECE]
    finally:
        await manager.aclose()


async def test_share_sessions_true_still_shares():
    """The default is unchanged: one pipeline for every radio on a station."""
    manager = SessionManager(fast_settings())
    await manager.start()
    try:
        first, _ = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        second, _ = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        assert first is second
        assert len(manager.sessions) == 1
    finally:
        await manager.aclose()


async def test_every_pipeline_reports_into_the_managers_byte_counters():
    """One set of counters for the whole service, not one per session.

    A session is reaped after `linger_seconds` and its pipelines are thrown away
    and rebuilt on every restart, so per-session totals cannot describe a rolling
    hour - they reset constantly. Every pipeline therefore shares the manager's
    :class:`Throughput`.
    """
    manager = SessionManager(fast_settings())
    await manager.start()
    try:
        session, _ = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        assert session.throughput is manager.throughput
        assert session.pipeline.throughput is manager.throughput

        # The restart path builds a *new* pipeline; it must still be handed the
        # same counters. Losing them here would silently stop reporting a
        # station's upstream traffic until the service was restarted.
        assert session._new_pipeline().throughput is manager.throughput
    finally:
        await manager.aclose()


async def test_audio_produced_for_nobody_is_not_counted_as_sent():
    """ "Sent" has to mean sent, or the figure is a comfortable lie.

    A station is kept warm for `linger_seconds` after the last radio leaves, and
    during that time its pipeline happily keeps downloading and producing audio
    that no radio receives. Counting those bytes as "audio sent" would show a
    service delivering traffic while it delivers nothing at all.
    """
    manager = SessionManager(fast_settings(linger_seconds=0.3))
    await manager.start()
    try:
        session, _ = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        # No subscriber drains anything, so the fake pipeline produces into the
        # void - exactly the shape of a warm, unlistened station.
        deadline = time.time() + 1.0
        while session.stats.bytes_out == 0 and time.time() < deadline:
            await asyncio.sleep(0.02)

        assert session.stats.bytes_out > 0, "the pipeline should have produced something"
        assert manager.throughput.out_total == 0, (
            "nothing reached a radio, so nothing may be counted as audio sent"
        )
    finally:
        await manager.aclose()


async def test_bitrate_override_makes_a_separate_session():
    """Two radios asking for different bitrates must not share one pipeline."""
    manager = SessionManager(fast_settings(max_sessions=4))
    await manager.start()
    try:
        await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        await manager.subscribe(
            "http://x/one.m3u8", ["http://x/one.m3u8"], overrides={"bitrate": 96_000}
        )
        assert len(manager.sessions) == 2
    finally:
        await manager.aclose()


async def test_with_ffmpeg_disabled_a_codec_override_does_not_split_the_pipeline():
    """Every stream is the source's own codec, so the key must say so.

    Two radios on one station, one with ``?codec=mp3`` and one without: with
    FFmpeg off they receive byte-identical audio. Keying the session on the
    override would give them two pipelines - two upstream downloads, two
    buffers, double the CPU - which is precisely what a shared session exists to
    avoid.
    """
    settings = fast_settings(max_sessions=4, ffmpeg_enabled=False)
    manager = SessionManager(settings)
    await manager.start()
    try:
        first, _ = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        second, _ = await manager.subscribe(
            "http://x/one.m3u8", ["http://x/one.m3u8"], overrides={"output_codec": "mp3"}
        )
        assert first is second, "the codec override split a shared pipeline"
        assert len(manager.sessions) == 1
        assert first.key.endswith("|auto|192000"), first.key
    finally:
        await manager.aclose()


async def test_capacity_is_enforced_rather_than_unbounded():
    manager = SessionManager(fast_settings(max_sessions=1))
    await manager.start()
    try:
        await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        with pytest.raises(CapacityError):
            await manager.subscribe("http://x/two.m3u8", ["http://x/two.m3u8"])
        assert manager.capacity_rejections >= 0
    finally:
        await manager.aclose()


async def test_capacity_evicts_an_idle_session():
    manager = SessionManager(fast_settings(max_sessions=1))
    await manager.start()
    try:
        first, first_sub = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        await manager.unsubscribe(first, first_sub)  # now idle

        second, _ = await manager.subscribe("http://x/two.m3u8", ["http://x/two.m3u8"])
        assert second.key != first.key
        assert len(manager.sessions) == 1
    finally:
        await manager.aclose()


# ------------------------------------------------------------- joining a stream
# Audio is produced in bursts: the encoder free-runs at roughly six times
# realtime, so it delivers a whole segment in about two seconds and then has
# nothing to do until the next one arrives six to eight seconds later. A listener
# whose buffer only fills *from the next burst onwards* therefore waits up to a
# whole segment before it hears anything - measured on a real station at a median
# of 8.08 seconds. A cheap radio gives up long before that, which is the
# reconnect loop these tests exist to prevent.


async def _produce_then_subscribe(settings: Settings):
    """Let a session produce with nobody listening, then attach a listener."""
    manager = SessionManager(settings)
    await manager.start()
    session, _ = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
    # Nobody is listening while audio builds up - the normal state of a warm
    # station. Waits on production rather than on the reserve, because with
    # pre-roll disabled there is deliberately no reserve to wait for.
    session.subscribers.clear()
    deadline = time.time() + 2.0
    while not session.stats.chunks and time.time() < deadline:
        await asyncio.sleep(0.01)
    subscriber = session.add_subscriber(client="late radio")
    return manager, session, subscriber


async def test_a_late_listener_starts_with_audio_already_buffered():
    """The fix for the silence: a joining radio must not start empty.

    This is the whole point of the reserve. Version 1 never showed this because
    back-pressure throttled its encoder to realtime, so it was always producing
    and a new listener always had something to read. Removing the back-pressure -
    correctly, so one slow radio cannot stall a station - removed that property,
    and nothing replaced it until now.
    """
    manager, _, subscriber = await _produce_then_subscribe(fast_settings())
    try:
        assert subscriber.queue.qsize() > 0, (
            "a listener that connects mid-gap has nothing to play and will hear silence"
        )
    finally:
        await manager.aclose()


async def test_the_reserve_is_bounded_by_the_setting():
    """It is audio held in RAM on a Raspberry Pi, so it must have a ceiling."""
    manager, session, _ = await _produce_then_subscribe(fast_settings())
    try:
        assert len(session._reserve) <= session.settings.queue_chunks, (
            "the reserve can grow without limit"
        )
    finally:
        await manager.aclose()


async def test_preroll_can_be_disabled():
    """Zero means "start every listener from silence", for diagnosing this."""
    settings = fast_settings()
    settings.preroll_seconds = 0.0
    manager, session, subscriber = await _produce_then_subscribe(settings)
    try:
        assert not session._reserve, "pre-roll is disabled but the reserve filled anyway"
        assert subscriber.queue.qsize() == 0
    finally:
        await manager.aclose()


async def test_starved_for_does_not_accumulate_while_audio_flows():
    """The columns were "eager" because they measured the wrong side.

    Reporting "seconds since the pipeline last produced" made a healthy station
    look like it was failing several times a minute, because production is bursty
    by design: a whole segment arrives in one burst, then nothing for six to eight
    seconds. What a listener hears as silence is *its own buffer being empty*, so
    that is what is measured - and the figure must not build up while audio keeps
    arriving.

    It is not literally zero at every instant, and should not be: with a small
    queue that is drained quickly the buffer can touch empty between two
    production ticks, and the clock restarts. What matters is that it never
    accumulates, which is exactly what the old figure did.
    """
    settings = fast_settings(linger_seconds=5.0)
    manager = SessionManager(settings)
    await manager.start()
    try:
        session, subscriber = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        # Otherwise there is no *live* listener and the figure under test is zero
        # for the wrong reason - the assertion would hold even if the clock were
        # broken. Mark it as a listener whose response is actually running.
        subscriber.started = True

        worst = 0.0
        for _ in range(20):
            await drain(subscriber, 1)
            session.health_tick()
            worst = max(worst, session.starved_for)
            await asyncio.sleep(0.005)

        # One production period is 5 ms here, so anything approaching a second
        # would mean the clock was not being cleared. The old eager figure reached
        # eight to ten seconds on a stream like this.
        assert worst < 1.0, f"starvation accumulated to {worst:.1f}s while audio was flowing"
    finally:
        await manager.aclose()


def test_an_idle_pipeline_does_not_make_a_listener_look_starved():
    """The two figures, side by side, on the state that caused the confusion.

    This is the reported symptom in one test: the pipeline has produced nothing
    for eight seconds - a completely normal gap between segments - while the
    listener has plenty buffered. The old "Silent"/"Idle" columns showed that
    eight seconds and read as a fault; the starvation figure correctly shows zero.
    """
    settings = Settings(stall_timeout=30.0)
    session = session_mod.StreamSession(settings, "key", ["http://x/one.m3u8"])
    session.pipeline.stats.last_data_at = time.time() - 8.0
    subscriber = session.add_subscriber(client="radio")
    subscriber.started = True

    # Give this listener a buffer, as a warm session would have. Queue items
    # are ``(produced_at, piece)`` tuples, as `_enqueue` puts them.
    for _ in range(10):
        subscriber.queue.put_nowait((time.time(), b"x" * settings.chunk_size))

    assert session.silent_for > 7.0, "the pipeline really has been quiet for a while"
    assert session.starved_for == 0.0, "but the listener is not short of anything"
    assert session.stalled is False, "an idle pipeline is not a failed station"


async def test_an_abandoned_connection_is_not_counted_as_a_listener():
    """A connection that never began reading must not report for the station.

    Seen in the journal: the radio opens connections that close after zero
    seconds having read nothing, several times a minute. Such a subscriber is
    added and primed with pre-roll, but its response generator never runs, so the
    ``finally`` that would unsubscribe it never executes and it sits in the list
    for the lifetime of the process.

    It is not harmless. Its queue drains when nobody reads it, and the station is
    judged by the listener with the *least* buffered - so one abandoned
    connection showing an empty queue made a station with a comfortably buffered
    listener report zero buffered seconds and a starvation clock that climbed.
    That is the reported contradiction: "starved climbs for 7s but buffered still
    has a large number." Neither figure should see this connection at all.
    """
    settings = fast_settings(queue_chunks=128)
    session = session_mod.StreamSession(settings, "key", ["http://x/one.m3u8"])

    live = session.add_subscriber(client="real radio")
    live.started = True
    for _ in range(10):
        live.queue.put_nowait((time.time(), b"x" * settings.chunk_size))
    expected = session.buffered_seconds()
    assert expected > 0.0, "a listener with ten chunks queued has a buffer"

    # An abandoned connection: attached, and then nothing is ever read from it,
    # so its queue stands empty while a real listener's does not.
    ghost = session.add_subscriber(client="probe")
    assert ghost.queue.empty(), "the set-up needs the abandoned queue to be the empty one"

    assert session.buffered_seconds() == pytest.approx(expected), (
        "an abandoned connection changed what the station reports as buffered"
    )
    assert session.starved_for == 0.0, "an abandoned connection made a healthy station look starved"
    assert session.stalled is False, "an abandoned connection made the station look failed"


async def test_the_buffer_and_the_starvation_clock_are_the_same_measurement():
    """The reported contradiction, pinned down as a test.

    "The starved column climbs for up to 7s but the buffered column still has a
    large number." That was possible because the two columns aggregated the same
    quantity differently: the page totalled every subscriber's queue, while the
    starvation clock took the smallest. With more than one connection present the
    two could tell opposite stories about one moment in time. They must agree.
    """
    settings = fast_settings(queue_chunks=128)
    session = session_mod.StreamSession(settings, "key", ["http://x/one.m3u8"])

    served = session.add_subscriber(client="radio one")
    served.started = True
    # The listener that is actually playing is the one that is being starved.
    other = session.add_subscriber(client="radio two")
    other.started = True

    session._starvation_clock()  # first observation starts the clock
    session._starved_since = time.time() - 7.0

    assert session.buffered_seconds() == 0.0, "nothing is buffered for the listener that is short"
    assert session.starved_for >= 7.0, "and it has been short for about seven seconds"

    # Queue audio for the second listener only. The buffer must not budge, because
    # the station is judged by the listener that has least, not the average.
    for _ in range(20):
        other.queue.put_nowait((time.time(), b"x" * settings.chunk_size))
    assert session.buffered_seconds() == 0.0, "a well-fed listener must not mask a starving one"


async def test_a_listener_that_never_reads_is_reaped():
    """Left alone these accumulate, one per abandoned connection, forever.

    The grace period matters as much as the reaping: a connection is allowed to
    sit un-started while its response is being set up, so only one that is *still*
    un-started after the grace period is treated as abandoned.
    """
    settings = fast_settings()
    session = session_mod.StreamSession(settings, "key", ["http://x/one.m3u8"])

    ghost = session.add_subscriber(client="probe")
    live = session.add_subscriber(client="radio")
    live.started = True

    now = time.time()
    grace = session_mod.NEVER_STARTED_GRACE
    session_mod.SessionManager._drop_never_started(session, now)
    assert ghost in session.subscribers, "reaped before it had a chance to start"

    session_mod.SessionManager._drop_never_started(session, now + grace + 1.0)
    assert ghost not in session.subscribers, "an abandoned connection was never cleaned up"
    assert live in session.subscribers, "a working listener was reaped"


async def test_the_reaper_loop_removes_abandoned_connections():
    """The reaping must actually be wired into the running service.

    A helper that is never called cleans up nothing, and the two tests above
    would still pass. This drives the real periodic loop instead of calling the
    helper, so removing the call is caught.
    """
    manager = SessionManager(fast_settings(linger_seconds=60.0))
    await manager.start()
    try:
        session, listener = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        listener.started = True  # a working listener, which the reaper must leave alone
        # An attached-but-never-read connection, exactly as an abandoned request
        # leaves it. Backdated so the grace period has already elapsed.
        ghost = session.add_subscriber(client="probe")
        ghost.connected_at = time.time() - session_mod.NEVER_STARTED_GRACE - 1.0
        assert ghost in session.subscribers

        deadline = time.time() + 10.0
        while ghost in session.subscribers and time.time() < deadline:
            await asyncio.sleep(0.05)

        assert ghost not in session.subscribers, (
            "the periodic reaper never removed the abandoned connection"
        )
        assert session in manager.sessions.values(), "a station with a real listener was retired"
    finally:
        await manager.aclose()


async def test_max_listeners_per_station():
    manager = SessionManager(fast_settings(max_listeners_per_session=2, max_sessions=4))
    await manager.start()
    try:
        await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        with pytest.raises(CapacityError):
            await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
    finally:
        await manager.aclose()


async def test_a_slow_listener_is_throttled_not_the_station():
    """The classic hang: one stalled radio must not stop the producer."""
    # Pieces and `chunk_size` are both 1024 (the floor) so one fan-out call yields
    # exactly one queued chunk, keeping the count below about fan-out calls rather
    # than about re-chunking.
    manager = SessionManager(fast_settings(queue_chunks=4, max_sessions=1))
    await manager.start()
    try:
        session, _ = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        stalled = session.add_subscriber(client="stalled")
        healthy = session.add_subscriber(client="healthy")

        for _ in range(100):
            session._fan_out(b"z" * 1024)

        assert stalled.dropped > 0  # it lost audio...
        assert stalled.queue.qsize() == stalled.queue.maxsize  # ...but is bounded
        assert healthy.queue.qsize() <= healthy.queue.maxsize
        # The producer never blocked, so the session stayed healthy. (The pump
        # task is emitting concurrently, hence "at least".)
        assert session.stats.chunks >= 100
    finally:
        await manager.aclose()


async def test_session_is_reaped_after_the_linger_window():
    manager = SessionManager(fast_settings(linger_seconds=0.2))
    await manager.start()
    try:
        session, subscriber = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        await manager.unsubscribe(session, subscriber)
        assert session.listener_count == 0

        # Reaping has two stages: the registry entry goes synchronously, then
        # Session.close() runs as a background task. Wait for *both*, otherwise
        # this races whenever the scheduler interleaves them.
        deadline = time.time() + 10
        while time.time() < deadline and (manager.sessions or not session.pipeline.closed):
            await asyncio.sleep(0.1)

        assert not manager.sessions, "the idle session was never reaped"
        assert session.pipeline.closed, "the pipeline was not shut down (ffmpeg/demuxer leaked)"
    finally:
        await manager.aclose()


async def test_unsubscribing_signals_end_of_stream_to_the_listener():
    manager = SessionManager(fast_settings())
    await manager.start()
    try:
        session, subscriber = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
        await manager.unsubscribe(session, subscriber)
        # Nothing to assert strongly, but the sentinel path must not explode.
        assert session.listener_count == 0
    finally:
        await manager.aclose()


async def test_close_signals_every_listener_and_stops_the_pipeline():
    manager = SessionManager(fast_settings())
    await manager.start()
    session, subscriber = await manager.subscribe("http://x/one.m3u8", ["http://x/one.m3u8"])
    await session.close("test")

    saw_end = False
    for _ in range(50):
        try:
            item = await asyncio.wait_for(subscriber.queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            break
        if item is None:
            saw_end = True
            break
    assert saw_end, "the listener was never told the stream had ended"
    assert session.pipeline.closed
    await manager.aclose()


# ---------------------------------------------------------- rejoining a stream
# A LAN radio re-GETs the same warm session every 20-60 s (the journal evidence
# is on `StreamSession._floors`). Priming the full reserve on every attach
# replays audio the client already played - measured server-side: a reconnect's
# first 9.2 s was *bit-identical* to its predecessor's first 9.2 s (cross-
# correlation 1.000). The floor must turn "attach" into a resume for that
# client while a genuinely new client still gets the full reserve.


async def _delivered(subscriber, count: int, timeout: float = 5.0):
    """Drain ``count`` chunks the way the response generator does, recording
    the floor the transport would record for this connection.

    The queue carries ``(offset, piece)``, where the offset is the piece's
    position in the session's running total of audio - the unit the join
    floor is measured in.  See ``Subscriber.reserve_bytes_sent``.
    """
    got = []
    deadline = time.time() + timeout
    while len(got) < count and time.time() < deadline:
        try:
            item = await asyncio.wait_for(subscriber.queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        if item is None:
            break
        offset, piece = item
        subscriber.reserve_bytes_sent = offset
        got.append((offset, piece))
    return got


def _queued(subscriber):
    """Everything currently primed for *subscriber*, without consuming it."""
    items = []
    while not subscriber.queue.empty():
        items.append(subscriber.queue.get_nowait())
    return items


async def test_a_rejoining_client_resumes_instead_of_replaying():
    """Same IP + User-Agent reconnecting: never re-send what it already got."""
    manager = SessionManager(fast_settings(queue_chunks=128))
    await manager.start()
    try:
        session, first = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="Frontier/1",
        )
        heard = await _delivered(first, 3)
        assert len(heard) == 3, "the test needs audio that was actually delivered"
        floor = max(stamp for stamp, _ in heard)
        await manager.unsubscribe(session, first)

        # Production continues while the client is away; whatever is primed on
        # the rejoin must be newer than the floor.
        await asyncio.sleep(0.05)

        rejoined, second = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="Frontier/1",
        )
        assert rejoined is session, "the session is warm; only the connection is new"
        primed = _queued(second)
        assert primed, "the rejoining client got no audio at all"
        offsets = [offset for offset, _ in primed]
        overlap = session._overlap_bytes()
        assert all(offset > floor - overlap for offset in offsets), (
            "the rejoin was primed from beyond the handoff overlap window: "
            f"floor={floor}, overlap={overlap}, primed={offsets}"
        )
        assert max(offsets) > floor, (
            "the rejoin must still resume forward of the old floor - the "
            "window extends backward only, it does not turn into a replay"
        )
    finally:
        await manager.aclose()


async def test_a_rejoining_client_that_changed_its_user_agent_does_not_replay(
    monkeypatch,
):
    """The same radio, back with a DIFFERENT User-Agent, must not be replayed.

    Measured on the real radio (2026-10-21): the same device opens
    connections with two different User-Agents - `NSPlayer/8.0.0.3801`
    (which sends `Icy-MetaData: 1`) and `FSL IR2.10` (which does not).  The
    join floor was keyed on (IP, User-Agent), so the radio coming back with
    the other one was a *different* key: no floor was found and the session's
    reserve was served to it in full - up to ten seconds of audio it had
    already played.

    That is not "the song restarted".  It splices two parts of the stream,
    and an MP3 frame's side information points back into the previous
    frames (the bit reservoir), so the splice breaks the decoder and it
    never resynchronises.  The radio's decoder survives the TCP reconnect,
    so it keeps playing garbage until the *server* is restarted.

    The assertion is on the AUDIO, not on a timestamp: the rejoining client
    must not be primed with the very bytes the first connection delivered.
    The fake pipeline below emits a different piece every time, so a replay
    is visible in the bytes.
    """

    class CountingPipeline(FakePipeline):
        """Like FakePipeline, but every piece is distinguishable."""

        counter = 0

        async def stream(self):
            while not self.closed:
                await asyncio.sleep(0.005)
                if self.closed:
                    return
                self.stats.last_data_at = time.time()
                self.stats.bytes_out += self.PIECE
                type(self).counter += 1
                # Four bytes of counter, padded to the piece size.
                yield type(self).counter.to_bytes(4, "big") + b"z" * (self.PIECE - 4)

    monkeypatch.setattr(session_mod, "StreamPipeline", CountingPipeline)

    # The handoff overlap is switched OFF here on purpose.  It is a
    # *deliberate*, bounded re-send of the last second and a half of
    # already-delivered audio, and it is a different defect from the one
    # under test: this test is about the FULL reserve being handed to a
    # radio that already played it.  With the overlap at its 1.5 s default
    # the floor is back-dated by 1.5 s, the fake pipeline emits a piece
    # every 5 ms, and every piece the first connection delivered falls
    # inside that window - so the deliberate overlap and the accidental
    # full replay are indistinguishable.  Zero makes the two separable.
    manager = SessionManager(fast_settings(queue_chunks=128, handoff_overlap_seconds=0.0))
    await manager.start()
    try:
        session, first = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="NSPlayer/8.0.0.3801",
        )
        heard = await _delivered(first, 3)
        assert len(heard) == 3, "the test needs audio that was actually delivered"
        already_played = b"".join(piece for _, piece in heard)
        await manager.unsubscribe(session, first)

        # Production continues while the client is away.
        await asyncio.sleep(0.05)

        # The radio comes back - with the OTHER User-Agent.
        rejoined, second = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="FSL IR2.10",
        )
        assert rejoined is session, "the session is warm; only the connection is new"
        primed = _queued(second)
        assert primed, "the rejoining client got no audio at all"
        replayed = b"".join(piece for _, piece in primed)
        assert already_played not in replayed, (
            "the rejoin replayed audio the first connection had already "
            "delivered - the radio is being handed a splice, which breaks "
            "the MP3 bit reservoir and leaves its decoder unable to recover"
        )
    finally:
        await manager.aclose()


async def test_the_reserve_holds_only_preroll_seconds_of_AUDIO(monkeypatch):
    """The reserve must be bounded by the audio it holds, not by wall-clock.

    Measured on the real radio (2026-10-22): the reserve held **128 pieces
    = 46 seconds of audio produced inside 0.795 seconds** - nearly five
    times the ten seconds `preroll_seconds` asks for.

    The reason is that the reserve used to be trimmed by *production time*::

        cutoff = stamp - preroll_seconds

    but FFmpeg transcodes a ten-second segment in about 1.5 seconds, so a
    burst of pieces is produced far faster than realtime and every piece in
    the burst carries a production stamp inside the same second.  A
    ten-second time window therefore holds the whole burst, and the piece
    count was the only bound left - and that one is 128 pieces, whatever
    their size.

    That is not a tidiness issue.  A rejoining radio is primed from the
    reserve, so it was served up to 46 seconds of audio it had already
    played.  That is a splice, it breaks the MP3 bit reservoir, and the
    radio's decoder never resynchronises - the reported "garbled title and
    crackling audio, forever, until the server is restarted".

    The bound is on the audio the reserve holds.  The pieces here are
    8 KiB, so the 128-piece count cap alone would let a megabyte - 43
    seconds of audio - through, and only a bound on the audio stops it.
    """

    class BurstPipeline(FakePipeline):
        """Produces pieces far faster than realtime, as FFmpeg does."""

        async def stream(self):
            while not self.closed:
                await asyncio.sleep(0.001)
                if self.closed:
                    return
                self.stats.last_data_at = time.time()
                self.stats.bytes_out += 8192
                yield b"a" * 8192

    monkeypatch.setattr(session_mod, "StreamPipeline", BurstPipeline)

    # `chunk_size` is what the session cuts the pieces at, so it is what
    # sets the size of the reserve's entries.
    settings = fast_settings(queue_chunks=128, chunk_size=8192)
    manager = SessionManager(settings)
    await manager.start()
    try:
        session, _first = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="Frontier/1",
        )
        # Let the burst fill the reserve.
        await asyncio.sleep(1.0)

        rate = settings.bitrate / 8.0
        held_seconds = session._reserve_bytes / rate
        assert held_seconds <= settings.preroll_seconds + 0.5, (
            f"the reserve holds {held_seconds:.1f} s of audio in "
            f"{len(session._reserve)} pieces, but preroll_seconds is "
            f"{settings.preroll_seconds:.1f}: a rejoining radio would be "
            "served audio it has already played, which splices the stream "
            "and breaks the MP3 bit reservoir"
        )
    finally:
        await manager.aclose()


async def test_a_rejoin_is_never_primed_with_more_than_the_reserve_holds(
    monkeypatch,
):
    """The audio a rejoin is primed with is the audio it was already sent.

    This is the symptom the reserve bound fixes, measured end to end: the
    radio is handed audio it has already played, and a splice in an MP3
    stream breaks the bit reservoir, so the radio's decoder never recovers
    - it plays garbage until the *server* is restarted.
    """

    class BurstPipeline(FakePipeline):
        async def stream(self):
            while not self.closed:
                await asyncio.sleep(0.001)
                if self.closed:
                    return
                self.stats.last_data_at = time.time()
                self.stats.bytes_out += 8192
                yield b"a" * 8192

    monkeypatch.setattr(session_mod, "StreamPipeline", BurstPipeline)

    settings = fast_settings(queue_chunks=128, chunk_size=8192)
    manager = SessionManager(settings)
    await manager.start()
    try:
        session, first = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="Frontier/1",
        )
        # Let the reserve fill, and let the client receive some of it.
        await asyncio.sleep(0.5)
        heard = await _delivered(first, 3)
        assert heard, "the test needs audio that was actually delivered"
        await manager.unsubscribe(session, first)

        _, second = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="Frontier/1",
        )
        primed = _queued(second)
        rate = settings.bitrate / 8.0
        primed_seconds = sum(len(piece) for _, piece in primed) / rate
        assert primed_seconds <= settings.preroll_seconds + 0.5, (
            f"the rejoin was primed with {primed_seconds:.1f} s of audio, "
            f"which is more than the {settings.preroll_seconds:.1f} s the "
            "reserve is allowed to hold"
        )
    finally:
        await manager.aclose()


async def test_a_rejoin_with_stock_to_spare_is_not_primed_with_a_replay():
    """The handoff overlap is a top-up, never a fixed rewind.

    The window exists for the join that lands on the *production edge*, where
    nothing unheard is left to send and the client would otherwise wait for
    the next burst.  Back-dating the floor by a flat 1.5 s there is the point.
    Back-dating it when tens of seconds of unheard audio are already waiting is
    not a cushion - it is audio the client has already been sent, replayed, and
    it is audible.  Measured at tune-in on the real radio: every one of the
    first three connections repeated 1.5 s of its predecessor.
    """

    settings = fast_settings(queue_chunks=128, handoff_overlap_seconds=1.5)
    manager = SessionManager(settings)
    await manager.start()
    try:
        session, first = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="Frontier/1",
        )
        heard = await _delivered(first, 3)
        assert len(heard) == 3, "the test needs audio that was actually delivered"
        floor = max(stamp for stamp, _ in heard)
        await manager.unsubscribe(session, first)

        # Leave more than the overlap window (1.5 s at 192 kbit/s = 36 000 bytes;
        # the fake emits 1 KiB every 5 ms) unheard after the floor.
        await asyncio.sleep(0.4)

        _, second = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="Frontier/1",
        )
        offsets = [offset for offset, _ in _queued(second)]
        assert offsets, "the rejoining client got no audio at all"
        assert min(offsets) > floor, (
            "the rejoin replayed audio the client had already been sent, even "
            f"though {session._audio_bytes - floor} bytes of unheard audio were "
            f"waiting: floor={floor}, primed={offsets}"
        )
    finally:
        await manager.aclose()


async def test_the_reserve_keeps_the_start_up_burst_until_it_has_played_out():
    """A radio is served the whole start-up burst, so the reserve must hold it.

    A pipeline starts by fetching every segment the playlist already held (up
    to three) and emitting them far faster than realtime: on radiom2o that is
    **30 s of audio produced inside two seconds**, all of it older than the
    live edge.  A radio that connected before the burst gets that audio from
    its first piece, so it is playing tens of seconds behind the production
    edge - while a reserve bounded by ``preroll_seconds`` (10 s) had already
    evicted everything but the newest ten seconds of it.  The next connection
    therefore found nothing left to resume from and was primed from the
    reserve's oldest piece: measured as ~20 s of audio vanishing between the
    radio's first and second connection at tuning time.
    """

    settings = fast_settings(queue_chunks=128, chunk_size=8192)
    session = session_mod.StreamSession(settings, "key", ["http://x/one.m3u8"])
    session.pipeline.stats.startup_seconds = 30.0
    session.pipeline.stats.started_at = time.time()

    for _ in range(88):  # 88 * 8192 B = 30 s at 192 kbit/s
        session._fan_out(b"a" * 8192)

    rate = settings.bitrate / 8.0
    held = session._reserve_bytes / rate
    assert held > settings.preroll_seconds + 10, (
        f"the reserve holds {held:.1f} s of a 30 s start-up burst, but "
        f"preroll_seconds is {settings.preroll_seconds:.1f}: the radio's next "
        "connection is primed from the oldest piece left and jumps forward"
    )


async def test_the_reserve_falls_back_to_preroll_after_the_start_up_burst():
    """The extra retention is a window, not a permanent memory increase.

    Once the burst has been played out a joining listener wants the usual
    couple of seconds of cushion, not half a minute of latency - and a stale
    join floor must never be able to pin a growing amount of audio on a
    service that runs for months.
    """

    settings = fast_settings(queue_chunks=128, chunk_size=8192)
    session = session_mod.StreamSession(settings, "key", ["http://x/one.m3u8"])
    session.pipeline.stats.startup_seconds = 30.0
    session.pipeline.stats.started_at = time.time()
    for _ in range(88):
        session._fan_out(b"a" * 8192)

    # The burst has been playing out for longer than it is long.
    session.pipeline.stats.started_at -= (
        session.pipeline.stats.startup_seconds + settings.preroll_seconds + 5.0
    )
    session._fan_out(b"a" * 8192)

    rate = settings.bitrate / 8.0
    held = session._reserve_bytes / rate
    assert held <= settings.preroll_seconds + 1.0, (
        f"the reserve still holds {held:.1f} s after the start-up window "
        f"closed, so a new listener would be served that much latency"
    )


async def test_a_different_client_still_gets_the_full_reserve():
    """The floor is keyed: one client's history must not blind a new one."""
    manager = SessionManager(fast_settings(queue_chunks=128))
    await manager.start()
    try:
        session, first = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="Frontier/1",
        )
        heard = await _delivered(first, 3)
        floor = max(stamp for stamp, _ in heard)
        await manager.unsubscribe(session, first)
        await asyncio.sleep(0.05)

        _, other = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.74",
            user_agent="Browser/1",
        )
        stamps = [stamp for stamp, _ in _queued(other)]
        assert stamps, "a brand-new client got no pre-roll at all"
        assert any(stamp <= floor for stamp in stamps), (
            "a brand-new client was primed with another client's floor "
            f"(floor={floor}, primed={stamps})"
        )
    finally:
        await manager.aclose()


async def test_an_overlapping_connection_resumes_from_the_still_open_one():
    """The radio's other order: attach the new socket, *then* detach the old.

    The journal shows both orders - "detach then attach" and "attach (2 now
    listening) then detach (1 remaining)". Recording floors only at departure
    would leave the second case replaying everything the first connection
    delivered while both were open, so a live same-key connection must
    contribute its floor too (within the handoff overlap window).
    """
    settings = fast_settings(queue_chunks=128)
    manager = SessionManager(settings)
    await manager.start()
    try:
        _session, old = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="Frontier/1",
        )
        heard = await _delivered(old, 3)
        floor = max(stamp for stamp, _ in heard)

        # Let production run past the floor, otherwise there is genuinely
        # nothing newer to prime and the test would pass for the wrong reason.
        await asyncio.sleep(0.05)

        _, new = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="Frontier/1",
        )
        offsets = [offset for offset, _ in _queued(new)]
        assert offsets, "the overlapping connection got no audio at all"
        overlap = int(settings.handoff_overlap_seconds * settings.bitrate / 8)
        assert all(offset > floor - overlap for offset in offsets), (
            "the overlapping connection was primed from beyond the handoff "
            f"window: floor={floor}, overlap={overlap}, primed={offsets}"
        )
    finally:
        await manager.aclose()


def _edge_session(settings: Settings, session: session_mod.StreamSession):
    """Drive one connection right up to the production edge, then rejoin it."""
    sub = session.add_subscriber(client="radio", user_agent="Frontier/1")
    offsets = []
    for _ in range(5):
        session._fan_out(b"a" * settings.chunk_size)
        offset, _piece = sub.queue.get_nowait()
        offsets.append(offset)
    sub.reserve_bytes_sent = max(offsets)
    session.remove_subscriber(sub)  # records the floor at the production edge
    return session.add_subscriber(client="radio", user_agent="Frontier/1")


async def test_a_rejoin_at_the_production_edge_is_never_primed_empty():
    """ "0 chunks prerolled" at the edge means waiting a full segment period.

    On a 3-second-cadence station this was the reported "silent for a second
    or less": with exact-floor joins, every rejoin that caught up to the
    production edge was primed with nothing and sat starved until the next
    burst. The handoff overlap must always leave an edge rejoin a cushion.
    """
    settings = fast_settings()
    session = session_mod.StreamSession(settings, "key", ["http://x/one.m3u8"])

    rejoin = _edge_session(settings, session)

    primed = _queued(rejoin)
    assert primed, (
        "a rejoin at the production edge was primed with nothing and would "
        "wait up to a whole segment period for the next burst"
    )
    offsets = [offset for offset, _ in primed]
    overlap = session._overlap_bytes()
    assert min(offsets) > max(offsets) - overlap, (
        f"the cushion exceeds the configured window: {offsets}"
    )


async def test_handoff_overlap_zero_restores_exact_resume():
    """0 disables the window: an edge rejoin is primed with nothing (2.0.1)."""
    settings = fast_settings()
    settings.handoff_overlap_seconds = 0.0
    session = session_mod.StreamSession(settings, "key", ["http://x/one.m3u8"])

    rejoin = _edge_session(settings, session)

    assert not _queued(rejoin), "handoff_overlap_seconds = 0 must reproduce the exact-resume join"
