"""Session management: one pipeline per station, many radios per pipeline.

Why this matters
----------------
The old server started a *fresh* ffmpeg process, a download thread and a
thread-local "reading token" for **every HTTP request**. Ten radios listening
to four stations meant ten ffmpeg processes, ten pipelines and a thread pool of
three - the arithmetic never worked, which is why the Pi eventually fell over.

Here, every listener of the same station attaches to one :class:`StreamSession`
that owns exactly one pipeline. Subscribers are cheap: a bounded
``asyncio.Queue`` each. A radio that stops reading (or crawls) degrades only
itself - the oldest chunks are dropped for *that* subscriber and the producer
never blocks on it.

Sessions also implement:

* **capacity limits** - at most ``max_sessions`` concurrent pipelines, so a
  misconfigured radio cannot fork-bomb a Pi;
* **linger** - a session stays warm for ``linger_seconds`` after the last
  listener leaves, so a reconnect (very common on cheap radios) is instant;
* **a stall watchdog** - a generator that stops producing is cancelled and
  restarted rather than being waited on forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Coroutine, Deque, Dict, List, Optional, Set, Tuple

from .config import Settings
from .pipeline import PipelineFatal, StreamPipeline
from .throughput import SAMPLE_SECONDS, Throughput

log = logging.getLogger(__name__)

#: How long a listener may sit attached without its response having started
#: reading before it is treated as abandoned. Generous, because a working
#: response begins reading within milliseconds - see `_drop_never_started`.
NEVER_STARTED_GRACE = 5.0

#: How many (client, user_agent) join-floors a session remembers. A stale floor
#: is harmless - it only ever suppresses reserve pieces that key was already
#: sent, and the reserve has moved on - but the map must not grow without limit
#: on a service that runs for months.
MAX_JOIN_FLOORS = 64


class CapacityError(RuntimeError):
    """Too many concurrent pipelines; the caller should retry later."""


@dataclass
class Subscriber:
    """One attached radio."""

    client: str = ""
    user_agent: str = ""
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    connected_at: float = field(default_factory=time.time)
    chunks: int = 0
    bytes_sent: int = 0
    dropped: int = 0
    #: ``produced_at`` of the newest audio the transport actually accepted for
    #: this connection - the floor a rejoining client is primed from, so it is
    #: never re-sent audio it already received (see ``StreamSession._join_floor``).
    last_delivered: Optional[float] = None
    disconnected_at: Optional[float] = None
    #: Set by the response generator the moment it starts reading. A subscriber
    #: whose client vanished between the request and the body running never gets
    #: here, which means its ``finally`` never runs and it would sit in the
    #: listener list forever - holding a capacity slot and, worse, contributing a
    #: queue depth to the health figures that nobody is draining.
    started: bool = False

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "client": self.client,
            "connected": round(time.time() - self.connected_at, 1),
            "chunks": self.chunks,
            "bytes": self.bytes_sent,
            "dropped": self.dropped,
            "queued": self.queue.qsize(),
            "started": self.started,
        }


@dataclass
class SessionStats:
    restarts: int = 0
    chunks: int = 0
    #: Output bytes the *pipeline* produced. Not the same as bytes a radio
    #: received: a warm pipeline keeps producing for `linger_seconds` after the
    #: last listener leaves, and a listener that falls behind has chunks dropped.
    bytes_out: int = 0
    #: Output bytes actually handed to a radio's socket. This is the one that
    #: answers "how much audio did my radios get?", and the one the dashboard and
    #: the rolling window both use.
    bytes_sent: int = 0
    created_at: float = field(default_factory=time.time)
    last_subscriber_left: Optional[float] = field(default_factory=time.time)
    listeners_total: int = 0


class StreamSession:
    """Owns one pipeline and fans its output out to many subscribers."""

    def __init__(
        self,
        settings: Settings,
        key: str,
        sources: Sequence[str],
        label: str = "",
        throughput: Optional[Throughput] = None,
    ) -> None:
        self.settings = settings
        self.key = key
        self.sources = list(sources)
        self.label = label or self.sources[0]
        self.id = uuid.uuid4().hex[:8]
        self.stats = SessionStats()
        #: Shared with every pipeline this session builds, and with the manager,
        #: so the totals survive a restart or a session being reaped. Made here
        #: when absent so a session can still be built on its own (tests do).
        self.throughput = throughput or Throughput()

        self.subscribers: List[Subscriber] = []
        self.pipeline = self._new_pipeline()
        self.fatal_error: Optional[str] = None

        self._task: Optional[asyncio.Task] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._closing = False
        #: Partial chunk held back by `_fan_out` while it accumulates to
        #: `chunk_size`. See `_fan_out` for why the size has to be fixed.
        self._pending = bytearray()
        #: Recent audio, ``(produced_at, piece)``, oldest first. Handed to a
        #: listener that connects later so it does not start in silence.
        self._reserve: Deque[Tuple[float, bytes]] = deque()
        #: When the least-buffered listener last had something to play. ``None``
        #: means nothing is starved. See `starved_for`.
        self._starved_since: Optional[float] = None
        #: ``(client, user_agent) -> produced_at`` of the newest audio that key
        #: was actually *sent*. A LAN radio re-GETs this warm session every
        #: 20-60 s (journal evidence: Kitchen-Roberts-Radio reattaching with
        #: 15-72 chunks prerolled on every attach), and priming the full
        #: reserve each time replayed up to 10 s of already-played audio - the
        #: reported "~2 s repeated every now and then". See `_join_floor`.
        self._floors: Dict[Tuple[str, str], float] = {}

    # ------------------------------------------------------------------ public
    @property
    def listener_count(self) -> int:
        return len(self.subscribers)

    @property
    def dead(self) -> bool:
        return self.fatal_error is not None

    @property
    def silent_for(self) -> float:
        """Seconds since this session last produced a byte of audio.

        Kept for the stall watchdog and for diagnosing a pipeline that has gone
        quiet, but **not** what the dashboard shows: production is bursty by
        design, so this climbs to a whole segment on a healthy station. Use
        :attr:`starved_for` for anything a listener would notice.
        """
        return max(0.0, time.time() - self.pipeline.stats.last_data_at)

    @property
    def stalled(self) -> bool:
        """A session with listeners whose listeners have nothing to play.

        Measured from the listeners' side - an empty buffer - rather than from the
        pipeline's. A pipeline that pauses between segments is normal and audible
        to nobody; a listener whose buffer has been empty for longer than
        ``stall_timeout`` is hearing silence right now.

        This still catches the failure it was introduced for. A stream whose
        segments FFmpeg could not read returned ``200 audio/mpeg``, counted
        successful fetches and emitted nothing at all - so its subscribers' queues
        stayed empty and this fires exactly as intended.
        """
        if not self._live_subscribers() or self.dead:
            return False
        return self.starved_for > self.settings.stall_timeout

    @property
    def producing(self) -> bool:
        """Whether the pipeline has produced any audio since it last started."""
        return self.pipeline.stats.bytes_out > 0

    def add_subscriber(self, client: str = "", user_agent: str = "") -> Subscriber:
        floor = self._join_floor(client, user_agent)
        subscriber = Subscriber(
            client=client,
            user_agent=user_agent,
            queue=asyncio.Queue(maxsize=max(4, self.settings.queue_chunks)),
        )
        self.subscribers.append(subscriber)
        self.stats.listeners_total += 1
        self.stats.last_subscriber_left = None
        # Hand over the recent audio *before* the caller starts reading, so a
        # radio that tuned in mid-gap has something to play immediately - but
        # only the audio this key has *not* already been sent; see
        # `_join_floor` for why the distinction is audible.
        self._prime(subscriber, floor)
        log.info(
            "listener attached (%d now listening, %d chunks prerolled)",
            len(self.subscribers),
            subscriber.queue.qsize(),
            extra={"session": self.id},
        )
        return subscriber

    def remove_subscriber(self, subscriber: Subscriber) -> None:
        if subscriber in self.subscribers:
            self.subscribers.remove(subscriber)
            log.info(
                "listener detached (%d remaining)",
                len(self.subscribers),
                extra={"session": self.id},
            )
            self._note_floor(subscriber)
        if not self.subscribers:
            self.stats.last_subscriber_left = time.time()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"radioconv-session:{self.id}")

    async def close(self, reason: str = "closed") -> None:
        """Stop the session and tell every listener the stream has ended."""
        if self._closing:
            return
        self._closing = True
        log.info("closing session (%s)", reason, extra={"session": self.id})

        # Deliver the last partial chunk before saying goodbye, so closing a
        # session does not clip up to a third of a second of audio.
        self._flush_pending()
        for subscriber in list(self.subscribers):
            self._signal_end(subscriber)
        self.subscribers.clear()

        for task in (self._pump_task, self._task):
            if task is not None and not task.done():
                task.cancel()
        # gather(..., return_exceptions=True) never raises, and unlike
        # suppressing CancelledError it still propagates *our* cancellation.
        pending = [t for t in (self._pump_task, self._task) if t is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self.pipeline.aclose()

    # ------------------------------------------------------------------ fan-out
    def _signal_end(self, subscriber: Subscriber) -> None:
        try:
            subscriber.queue.put_nowait(None)
        except asyncio.QueueFull:
            # Make room for the sentinel: it is more important than audio.
            with contextlib.suppress(asyncio.QueueEmpty, asyncio.QueueFull):
                subscriber.queue.get_nowait()
                subscriber.queue.put_nowait(None)

    def _fan_out(self, chunk: bytes) -> None:
        """Re-chunk to `chunk_size` and queue the result for every listener.

        The re-chunking is what makes a queue bound mean anything. A queue is
        bounded by a number of *items*, and the two producers disagree wildly
        about what an item is:

        * **FFmpeg's pipe** returns whatever is available per read. Measured on a
          real 192 kbit/s stream: **578 bytes**, not the 8192 that ``chunk_size``
          asks for, because a pipe hands back what has been written rather than
          filling the buffer. A 128-item limit was therefore a 74 KB buffer -
          about **three seconds** of audio, while the comment beside it claimed
          forty-three. Three seconds cannot cover the 8-16 second gap between HLS
          segments, so a listener's buffer emptied in every gap and the audio
          stopped and restarted.
        * **The pass-through demuxer** yields a whole segment at once, which with
          the same limit could have buffered tens of megabytes for one listener.

        So both directions are needed: small reads are accumulated, large blobs
        are split. Either alone leaves one producer wrong. The side effect is that
        queue operations and paced wake-ups drop by more than an order of
        magnitude on a Pi, which matters when eight listeners each need 40 of them
        a second.
        """
        self.stats.bytes_out += len(chunk)
        size = max(1024, self.settings.chunk_size)
        self._pending += chunk

        while len(self._pending) >= size:
            piece = bytes(self._pending[:size])
            del self._pending[:size]
            self._enqueue(piece)

    def _enqueue(self, piece: bytes) -> None:
        self.stats.chunks += 1
        # One stamp per piece, shared by the reserve *and* every queue, so a
        # delivered piece can be compared against reserve entries later - that
        # comparison is the join-floor. Queue items are therefore
        # ``(produced_at, piece)`` tuples; the end-of-stream sentinel stays
        # ``None``.
        stamp = time.time()
        self._reserve_piece(piece, stamp)
        for subscriber in self.subscribers:
            try:
                subscriber.queue.put_nowait((stamp, piece))
            except asyncio.QueueFull:
                # This listener is behind. Drop the *oldest* audio for it and
                # carry on: one slow radio must never stall the station.
                subscriber.dropped += 1
                with contextlib.suppress(asyncio.QueueEmpty):
                    subscriber.queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    subscriber.queue.put_nowait((stamp, piece))

    def _reserve_piece(self, piece: bytes, stamp: float) -> None:
        """Hold this piece for a moment, so a radio that arrives late is served.

        This is the whole reason a listener can start instantly. Audio is produced
        in bursts (see ``preroll_seconds``), and the reserve is what bridges the
        gap between one burst and the next for somebody who was not here when the
        burst happened. It fills whether or not anybody is listening, so a *warm*
        station with no radios - the normal state of this service - can still
        serve the first radio that tunes in.
        """
        if self.settings.preroll_seconds <= 0:
            return
        self._reserve.append((stamp, piece))
        # Bounded by time, not by a piece count: the correct count depends on the
        # stream's bitrate, which is not known when the reserve is created (a
        # pass-through station runs at its own rate, not the configured one).
        cutoff = stamp - self.settings.preroll_seconds
        while self._reserve and self._reserve[0][0] < cutoff:
            self._reserve.popleft()
        # A hard cap as well, because a pathological bitrate should not be able to
        # turn this into unbounded memory on a Pi.
        while len(self._reserve) > self.settings.queue_chunks:
            self._reserve.popleft()

    def _join_floor(self, client: str, user_agent: str) -> Optional[float]:
        """Newest ``produced_at`` that *(client, user_agent)* was already sent.

        Why a floor exists at all: the reserve stops a *new* listener from
        starting in silence, but a client reconnecting to this warm session -
        which a Frontier-Silicon radio does every 20-60 s, measured in the
        journal - has already played the very audio the reserve holds. Priming
        it again replayed up to ten seconds on every attach: a reconnect's
        first 9.2 s was measured *bit-identical* to its predecessor's first
        9.2 s, and that is the reported "~2 s repeated every now and then".
        The floor makes a rejoin resume where the old connection stopped
        instead of rewinding.

        Two sources, because radios do both:

        * the key's last *departed* connection (it recorded how far it got);
        * a same-key connection that is *still open* - the observed overlap
          pattern is "attach the new socket, then detach the old one", so the
          predecessor's floor is the freshest truth at prime time.

        Keyed on IP *and* User-Agent: two devices share an IP only behind NAT
        and then differ by User-Agent, while one device reconnecting shares
        both - exactly the case to floor. A second tab of the same browser
        also shares the key and therefore joins nearer the live edge with less
        pre-roll: a deliberate trade, since the replay is a measured defect
        and a same-UA second listener is rare for this service.
        """
        key = (client, user_agent)
        floor = self._floors.get(key)
        for other in self.subscribers:
            if (
                (other.client, other.user_agent) == key
                and other.last_delivered is not None
                and (floor is None or other.last_delivered > floor)
            ):
                floor = other.last_delivered
        if floor is None:
            return None
        # Pull the join point back by the handoff overlap. At the exact
        # production edge the post-floor stock is zero, so an exact-floor rejoin
        # is primed with nothing and waits for the next burst - measured as
        # 0.5-2.1 s of starvation every few seconds on a 3 s-cadence station,
        # which is the reported "silent for a second or less". The audio the
        # old socket delivered but the radio never played (discarded on the
        # handoff) is the reported "skips a second or so". Re-sending at most
        # `handoff_overlap_seconds` of already-delivered audio bridges both,
        # with the replay bounded to that window (see Settings for the tradeoff).
        return floor - self.settings.handoff_overlap_seconds

    def _note_floor(self, subscriber: Subscriber) -> None:
        """Remember how far a departing connection got, for its next attach."""
        if subscriber.last_delivered is None:
            # Nothing reached the transport (abandoned or probe connection), so
            # a later attach must not be floored by it - it heard nothing.
            return
        key = (subscriber.client, subscriber.user_agent)
        # Re-insert rather than assign, so eviction sees the freshest keys.
        self._floors.pop(key, None)
        self._floors[key] = subscriber.last_delivered
        while len(self._floors) > MAX_JOIN_FLOORS:
            self._floors.pop(next(iter(self._floors)))

    def _prime(self, subscriber: Subscriber, floor: Optional[float] = None) -> None:
        """Give a newly connected listener the reserve, oldest piece first.

        Without this a joining radio waits for the next production burst, which is
        up to eight seconds of silence - measured, not estimated. Its own buffer
        also starts at zero, so the first segment that arrives has no cushion
        behind it.

        ``floor`` (from ``_join_floor``) skips the pieces this particular client
        was already sent - what turns "attach" from a replay into a resume. A
        new client has no floor and gets the full reserve, as before.
        """
        for produced_at, piece in list(self._reserve):
            if floor is not None and produced_at <= floor:
                continue
            try:
                subscriber.queue.put_nowait((produced_at, piece))
            except asyncio.QueueFull:
                break

    def buffered_seconds(self) -> float:
        """Seconds of audio held for the *least* well-buffered live listener.

        The minimum rather than the total, because a station is only as safe as
        its worst-served radio: summing would let a full buffer on one connection
        hide a listener that is about to run dry on another.

        Only listeners that are actually being drained are counted. A connection
        that was abandoned before its response began has a queue nothing reads,
        and averaging or summing it in produced a figure that contradicted the
        starvation column - one reporting plenty while the other reported silence.
        """
        live = self._live_subscribers()
        if not live:
            return 0.0
        rate = self.pipeline.output_bitrate or self.settings.bitrate
        if rate <= 0:
            return 0.0
        smallest = min(s.queue.qsize() for s in live)
        return smallest * max(1024, self.settings.chunk_size) * 8 / rate

    def _live_subscribers(self) -> List[Subscriber]:
        """Listeners whose response is actually running, so are being served."""
        return [s for s in self.subscribers if s.started]

    def health_tick(self) -> None:
        """Once a second: keep the starvation clock honest.

        The tick exists so the duration stays accurate on a quiet service, since
        a listener that is starved is not reading the metrics either. The reading
        itself is what does the work - see :meth:`_starvation_clock`.
        """
        self._starvation_clock()

    def _starvation_clock(self) -> float:
        """Seconds the least-buffered listener has had nothing to play, updated.

        One implementation shared by :meth:`health_tick` and :attr:`starved_for`,
        so the figure cannot differ depending on who asks for it.

        Self-correcting on purpose: the clock is started by the first observation
        of an empty buffer rather than only by the tick, so the signal cannot be
        missed just because nothing happened to be ticking - which is precisely
        the case for a mute stream, where no work is happening at all. Reading it
        has the side effect of starting the clock, which is the price of a figure
        that can never be wrong; the tick merely keeps it accurate unevaluated.
        """
        if not self._live_subscribers() or self.dead:
            self._starved_since = None
            return 0.0
        if self.buffered_seconds() > 0.0:
            self._starved_since = None
            return 0.0
        if self._starved_since is None:
            self._starved_since = time.time()
        return max(0.0, time.time() - self._starved_since)

    @property
    def starved_for(self) -> float:
        """Seconds the least-buffered listener has had nothing to play.

        This is the honest replacement for "seconds since the pipeline last
        produced anything", which climbed to a whole segment - about ten seconds -
        on a perfectly healthy stream, because the encoder delivers a segment in
        one burst and then has nothing to do until the next one. That figure made
        every station look like it was failing several times a minute.

        What a listener actually experiences as silence is *its buffer being
        empty*, so that is what this measures. It stays at zero while audio is
        flowing, however bursty the producing side is.
        """
        return self._starvation_clock()

    def _flush_pending(self) -> None:
        """Send the final partial chunk, if any.

        Without this the tail of a stream - up to `chunk_size` bytes, a third of a
        second - would be held back and discarded, which is audible as a clipped
        ending when a station fails over or a session is closed.
        """
        if self._pending:
            self._enqueue(bytes(self._pending))
            self._pending.clear()

    # ------------------------------------------------------------------- run
    async def _run(self) -> None:
        backoff = self.settings.restart_backoff
        while not self._closing:
            self._pump_task = asyncio.create_task(self._pump(), name=f"radioconv-pump:{self.id}")
            watchdog = asyncio.create_task(self._watchdog(), name=f"radioconv-wd:{self.id}")
            try:
                await asyncio.wait({self._pump_task, watchdog}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (self._pump_task, watchdog):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(self._pump_task, watchdog, return_exceptions=True)

            if self._closing:
                return

            error: Optional[BaseException] = None
            for task in (self._pump_task, watchdog):
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None and error is None:
                    error = exc

            self.stats.restarts += 1
            if isinstance(error, PipelineFatal):
                self.fatal_error = str(error)
                log.error(
                    "pipeline is fatal; ending the session: %s", error, extra={"session": self.id}
                )
                self._end_all_subscribers()
                return
            log.warning(
                "restarting pipeline (#%d) after: %s",
                self.stats.restarts,
                error or "watchdog timeout",
                extra={"session": self.id},
            )
            await asyncio.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2, 30.0)
            if not self._closing:
                # The reserve spans audio from before the failure, and the new
                # pipeline starts wherever the playlist is *now*. Handing it over
                # after a restart would splice two unrelated moments together with
                # a silent jump between them - worse than a listener waiting out
                # the restart, which is what it would otherwise do.
                self._reserve.clear()
                # Fresh pipeline: it may have failed over while we slept.
                await self.pipeline.aclose()
                self.pipeline = self._new_pipeline()

    async def _pump(self) -> None:
        async for chunk in self.pipeline.stream():
            if self._closing:
                return
            self._fan_out(chunk)

    def _new_pipeline(self) -> StreamPipeline:
        """Build a pipeline, handing on the shared byte counters.

        One place, so the two sites that create a pipeline (the constructor and
        the restart path) cannot disagree about the arguments - a restart that
        forgot ``throughput`` would silently stop reporting upstream traffic for
        the rest of that station's life.
        """
        return StreamPipeline(
            self.settings, self.sources, label=self.label, throughput=self.throughput
        )

    async def _watchdog(self) -> None:
        """Force a restart if the pipeline stops producing for far too long.

        ``pipeline.stream()`` already times out every individual operation, so
        this is defence in depth against a wedged await we have not thought of:
        it is much better to restart a station than to leave a radio silent
        until someone reboots the Pi.
        """
        limit = max(60.0, self.settings.stall_timeout * 3)
        while not self._closing:
            await asyncio.sleep(min(10.0, limit / 3))
            if self._closing:
                return
            idle = time.time() - self.pipeline.stats.last_data_at
            warming_up = time.time() - self.pipeline.stats.started_at < limit
            if idle > limit and not warming_up:
                log.error(
                    "pipeline produced nothing for %.0fs; forcing a restart",
                    idle,
                    extra={"session": self.id},
                )
                raise TimeoutError(f"pipeline stalled for {idle:.0f}s")

    def _end_all_subscribers(self) -> None:
        for subscriber in list(self.subscribers):
            self._signal_end(subscriber)
        self.subscribers.clear()

    # ------------------------------------------------------------------ views
    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "key": self.key,
            "label": self.label,
            "listeners": self.listener_count,
            "sources": len(self.sources),
            "current_source": self.pipeline.current_url,
            "pipeline": self.pipeline.stats.as_dict(),
            "session": {
                "restarts": self.stats.restarts,
                "chunks": self.stats.chunks,
                "bytes_out": self.stats.bytes_out,
                "bytes_sent": self.stats.bytes_sent,
                "uptime": round(time.time() - self.stats.created_at, 1),
            },
            # Health, not just counters. `producing` is false until the first
            # byte of audio exists, and `stalled` is true once a connected
            # session has been silent for longer than `stall_timeout`.
            "producing": self.producing,
            "stalled": self.stalled,
            # From the listeners' side: how long the least-buffered one has had
            # nothing to play. This is what "silent" means to a person, and it
            # stays at zero through the pipeline's normal burst-and-wait rhythm.
            "starved_for": round(self.starved_for, 1),
            # From the pipeline's side, kept for diagnosis. Climbs to a whole
            # segment on a healthy station, so it is not the silence figure.
            "silent_for": round(self.silent_for, 1),
            # The one the dashboard's Buffered column shows, computed here rather
            # than re-derived in JavaScript from a chunk count. Two places doing
            # the same arithmetic is how the page came to show a healthy buffer
            # beside a climbing starvation figure.
            "buffered_seconds": round(self.buffered_seconds(), 1),
            # Total across listeners, kept for diagnosis only: this SUMS while
            # `buffered_seconds` takes the minimum, so they are not comparable.
            "queued_chunks": sum(s.queue.qsize() for s in self.subscribers),
            "chunk_bytes": max(1024, self.settings.chunk_size),
            "output_bitrate": round(self.pipeline.output_bitrate or 0) or None,
            "subscribers": [s.as_dict() for s in self.subscribers],
            "fatal_error": self.fatal_error,
        }


class SessionManager:
    """Registry of live sessions with capacity control and idle reaping."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sessions: Dict[str, StreamSession] = {}
        self._lock = asyncio.Lock()
        self._reaper: Optional[asyncio.Task] = None
        self._sampler: Optional[asyncio.Task] = None
        self.consecutive_failures = 0
        self.capacity_rejections = 0
        self.started_at = time.time()
        #: Service-wide byte counters. They live here rather than in the sessions
        #: because a session is reaped, restarted and replaced constantly, and a
        #: total that resets cannot describe a rolling hour.
        self.throughput = Throughput()
        #: Fire-and-forget shutdown tasks. Held in a set because a task that is
        #: only referenced by the event loop can be garbage collected mid-flight,
        #: which silently cancels it half way through shutting a session down.
        self._background: Set[asyncio.Task] = set()

    def _spawn_background(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop(), name="radioconv-reaper")
        if self._sampler is None:
            self._sampler = asyncio.create_task(
                self._throughput_loop(), name="radioconv-throughput"
            )

    async def aclose(self) -> None:
        for task in (self._reaper, self._sampler):
            if task is not None:
                task.cancel()
        for session in list(self.sessions.values()):
            await session.close("shutting down")
        self.sessions.clear()

    # ------------------------------------------------------------------ keys
    @staticmethod
    def make_key(primary_url: str, settings: Settings, overrides: Optional[dict] = None) -> str:
        overrides = overrides or {}
        bitrate = overrides.get("bitrate") or settings.bitrate
        codec = overrides.get("output_codec") or settings.output_codec
        return f"{primary_url}|{codec}|{bitrate}"

    # ---------------------------------------------------------------- acquire
    async def subscribe(
        self,
        primary_url: str,
        sources: Sequence[str],
        *,
        settings: Optional[Settings] = None,
        client: str = "",
        user_agent: str = "",
        overrides: Optional[dict] = None,
        label: str = "",
    ):
        """Attach to (or create) the session for a station.

        Returns ``(session, subscriber)``. Raises :class:`CapacityError` when
        every pipeline slot is taken and no idle session can be evicted.
        """
        settings = settings or self.settings
        key = self.make_key(primary_url, settings, overrides)

        async with self._lock:
            session = self.sessions.get(key)
            if session is None or session.dead:
                if len(self.sessions) >= settings.max_sessions:
                    self._evict_idle(settings)
                if len(self.sessions) >= settings.max_sessions:
                    raise CapacityError(
                        f"{settings.max_sessions} streams are already active; try again shortly"
                    )
                # The label is the station's `name` parameter when it has one, and
                # is otherwise the source URL. Radios on the same station share a
                # session, so with two different names for one URL the first one to
                # arrive is what the dashboard shows - unlike `icy-name`, which is
                # per-listener and therefore always that listener's own name.
                session = StreamSession(
                    settings,
                    key,
                    sources,
                    label=label or primary_url[:120],
                    throughput=self.throughput,
                )
                self.sessions[key] = session
                await session.start()

            if session.listener_count >= settings.max_listeners_per_session:
                raise CapacityError(
                    f"{settings.max_listeners_per_session} listeners already attached "
                    "to this station; try again shortly"
                )
            subscriber = session.add_subscriber(client=client, user_agent=user_agent)
            return session, subscriber

    async def unsubscribe(self, session: StreamSession, subscriber: Subscriber) -> None:
        session.remove_subscriber(subscriber)
        # A session whose pipeline died while nobody was looking can go now.
        if session.dead and session.listener_count == 0:
            self._forget(session)

    def _forget(self, session: StreamSession) -> None:
        if self.sessions.get(session.key) is session:
            del self.sessions[session.key]
            log.info("session removed", extra={"session": session.id})

    def _retire(self, session: StreamSession, reason: str) -> None:
        """Unhook a session from the registry *and* shut it down.

        The two steps must travel together: ``_forget`` only removes the
        registry entry (synchronously), while the teardown of the pipeline runs
        as a background task. Pairing them here means a future caller cannot
        drop a session from the registry and silently leak its pipeline.
        """
        self._forget(session)
        self._spawn_background(session.close(reason), f"radioconv-close:{session.id}")

    def _evict_idle(self, settings: Settings) -> None:
        """Free a slot by dropping the longest-idle session with no listeners."""
        idle = [s for s in self.sessions.values() if s.listener_count == 0]
        if not idle:
            return
        victim = max(
            idle,
            key=lambda s: s.stats.last_subscriber_left or s.stats.created_at,
        )
        log.warning("evicting idle session to free a slot", extra={"session": victim.id})
        self._retire(victim, "evicted for capacity")

    # ----------------------------------------------------------------- reaper
    async def _throughput_loop(self) -> None:
        """Once a second: record the byte totals and each station's health.

        A task rather than lazy sampling at read time: if nobody looked at the
        metrics for an hour there would be no point old enough to subtract from,
        and the window would quietly report the whole cumulative total - so a
        service up for three days would claim three days of traffic happened in
        the last hour. See :mod:`radioconv.throughput`.

        The same tick drives starvation tracking, for the same reason: "how long
        has this listener had nothing" cannot be answered by measuring only at the
        moment somebody asks.
        """
        while True:
            await asyncio.sleep(SAMPLE_SECONDS)
            self.throughput.sample()
            for session in list(self.sessions.values()):
                session.health_tick()

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            now = time.time()
            for session in list(self.sessions.values()):
                self._drop_never_started(session, now)
                if session.listener_count:
                    continue
                left_at = session.stats.last_subscriber_left or now
                if session.dead or (now - left_at) >= self.settings.linger_seconds:
                    self._retire(session, "idle")

    @staticmethod
    def _drop_never_started(session: StreamSession, now: float) -> None:
        """Remove listeners whose response never began, after a grace period.

        The response generator is what unsubscribes, so a client that disconnects
        between the request and the body running is never cleaned up by it. That
        left a phantom listener holding a capacity slot, and - because its queue
        was primed but never drained - it made the summed buffer figure report
        plenty of audio while a real listener's starvation was climbing. Observed
        on a radio that opens a probe connection and abandons it immediately.

        The grace period is generous next to the milliseconds a working response
        takes to start reading, and `started` is only ever cleared by the generator
        actually running, so a live listener cannot be dropped by this.
        """
        for subscriber in list(session.subscribers):
            if not subscriber.started and now - subscriber.connected_at > NEVER_STARTED_GRACE:
                log.warning(
                    "dropping listener %s: connected but never began reading",
                    subscriber.id,
                    extra={"session": session.id},
                )
                session.remove_subscriber(subscriber)

    # ---------------------------------------------------------------- views
    def count_listeners(self) -> int:
        return sum(s.listener_count for s in self.sessions.values())

    def as_dict(self) -> dict:
        sessions = list(self.sessions.values())
        return {
            "sessions": len(sessions),
            "max_sessions": self.settings.max_sessions,
            "listeners": sum(s.listener_count for s in sessions),
            #: Sessions that are connected but mute - see ``StreamSession.stalled``.
            "silent": sum(1 for s in sessions if s.stalled),
            #: Sessions that have not yet produced a single byte.
            "starting": sum(1 for s in sessions if not s.producing),
            "capacity_rejections": self.capacity_rejections,
            "details": [s.as_dict() for s in sessions],
        }

    def note_rejection(self) -> None:
        self.capacity_rejections += 1
