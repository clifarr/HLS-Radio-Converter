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
from .retrans import RetransmitCounter, tcp_retransmit_counter
from .throughput import SAMPLE_SECONDS, Throughput
from .tsdemux import (
    adts_align_offset,
    adts_frame_size,
    adts_has_sync,
    mp3_align_offset,
    mp3_frame_size,
    mp3_has_sync,
)

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

#: The smallest MPEG audio frame we will cut a reserve piece at. 576 bytes is
#: the frame of a 192 kbit/s 48 kHz stream - the size this project produces -
#: and is used only as a *floor* while searching: the real size always comes
#: from the frame header itself (see ``mp3_frame_size``).
FRAME_HINT = 576

#: The largest MPEG audio frame that can exist, in bytes: MPEG-2/2.5 Layer II
#: at 160 kbit/s and 8 kHz is ``144 * 160000 / 8000`` = 2880. Layer III is
#: smaller (1440) and Layer I smaller still.
MAX_MP3_FRAME_BYTES = 2880

#: The largest ADTS frame, from the 13-bit length field in the header.
MAX_ADTS_FRAME_BYTES = 8191

#: How much audio the frame-grid search may hold back while it is still
#: looking for a frame *pair*.
#:
#: **Two** maximum-size frames, because a frame is only *certain* when its
#: successor is a frame too: a lone sync word occurs by chance inside every
#: other frame's payload, so the search needs a frame *pair*. With one frame
#: of headroom the search would give up before it could possibly succeed.
#:
#: The old budget was ``4 * FRAME_HINT`` = 2304 bytes - a number taken from
#: the *MP3* frame size and applied to every format. That is smaller than a
#: single maximum-size ADTS frame, so a high-bitrate AAC stream could never
#: be aligned at all: the search was always given up on before a frame pair
#: could fit.
ALIGN_SEARCH_BUDGET = {
    "mp3": 2 * MAX_MP3_FRAME_BYTES,
    "adts": 2 * MAX_ADTS_FRAME_BYTES,
}

#: How much audio the search examines before it is willing to say "this is
#: not this format at all".
#:
#: A stream with no sync word anywhere in it is not MP3 (or not ADTS) - but
#: that verdict is only worth anything if enough bytes have been looked at.
#: The old code decided on the *first* chunk, which was as little as 578
#: bytes, and a stream that opens with a short preamble - a few hundred
#: bytes of something else before the first frame - was then declared "not
#: MP3" for ever, and was never aligned at all.
#:
#: A kilobyte is enough to be conclusive (a real frame starts within the
#: first frame's worth of bytes) and small enough that the delay is
#: invisible: 1024 bytes is 43 ms of a 192 kbit/s stream.
NO_SYNC_VERDICT_BYTES = 1024


class CapacityError(RuntimeError):
    """Too many concurrent pipelines; the caller should retry later."""


@dataclass
class Subscriber:
    """One attached radio."""

    client: str = ""
    user_agent: str = ""
    #: Whether this client asked for in-band metadata (``Icy-MetaData: 1``).
    #: Only such a client is sent the blocks; see ``_wants_icy_metadata``.
    metadata: bool = False
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    connected_at: float = field(default_factory=time.time)
    chunks: int = 0
    bytes_sent: int = 0
    dropped: int = 0
    #: How many bytes of the session's audio this connection has been given.
    #:
    #: This is the number the join floor needs, and it is measured in the
    #: session's running total of audio produced rather than in wall-clock
    #: time.  The difference is not cosmetic.  The reserve is filled in
    #: bursts, so a client that is served the reserve and then plays it is
    #: ``preroll_seconds`` behind the production edge *in audio* while being
    #: only a fraction of a second behind *in wall-clock* - the pieces of a
    #: burst are produced in a second but hold ten seconds of audio.  A floor
    #: measured in wall-clock is therefore always behind the oldest piece of
    #: the reserve, and it can never cut anything: the rejoin is served the
    #: whole reserve, which is the audio the radio had already played.
    #:
    #: Counting bytes removes the clock entirely.  The floor becomes "the
    #: client has had N bytes of the session's audio", and the rejoin is
    #: primed with everything after them.
    #:
    #: ``None`` means nothing has reached the transport yet - an abandoned or
    #: probe connection - so a later attach must not be floored by it.
    reserve_bytes_sent: Optional[int] = None
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
            # The User-Agent matters: the same radio opens connections with
            # more than one ("NSPlayer/8.0.0.3801" and "FSL IR2.10"), and
            # they are handled differently - only the one that asks for
            # `Icy-MetaData` is sent the metadata blocks.  Without this in
            # the metrics there is no way to tell which one is listening.
            "user_agent": self.user_agent,
            "metadata": self.metadata,
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
        #: Audio held back while the session finds the first MP3 frame boundary
        #: and then cuts every piece on one. See `_frame_aligned`.
        self._align_pending = bytearray()
        #: Whether the MP3 frame grid has been located yet. ``None`` means "not
        #: looked yet"; ``False`` means "this is not MP3, pass it through".
        self._frame_grid: Optional[bool] = None
        #: The format the frame grid was found for: ``"mp3"`` or ``"adts"``.
        #: A pipeline restart can change the codec under the session - the
        #: station failed over to a different source - and then the grid, the
        #: frame size and the held-back bytes all belong to the *old* format.
        #: See `_frame_aligned`.
        self._grid_format: Optional[str] = None
        #: Size in bytes of the MP3 frame this stream uses (576 for the
        #: 192 kbit/s 48 kHz output this project produces), once it is known.
        #: ``None`` until the first frame is found.
        self._frame_size: Optional[int] = None
        #: Recent audio, ``(offset, piece)``, oldest first. Handed to a
        #: listener that connects later so it does not start in silence.
        self._reserve: Deque[Tuple[int, bytes]] = deque()
        #: How many bytes of audio the reserve holds.  The reserve is bounded
        #: by the *audio* it holds, not by the wall-clock it was produced
        #: over, and the two are very different - see `_reserve_piece`.
        self._reserve_bytes = 0
        #: How many bytes of audio this session has produced since it
        #: started.  The join floor is measured against this, not against
        #: the clock - see `Subscriber.reserve_bytes_sent`.
        self._audio_bytes = 0
        #: When this station's listeners were last re-anchored by the *timer*.
        #: ``None`` means never, which is always allowed - see
        #: `SessionManager.reanchor_overdue_sessions`.
        self.last_reanchor_at: Optional[float] = None
        #: How many times the timer re-anchored this station.
        self.scheduled_reconnects = 0
        #: When the least-buffered listener last had something to play. ``None``
        #: means nothing is starved. See `starved_for`.
        self._starved_since: Optional[float] = None
        #: ``(client, user_agent) -> bytes of the session's audio`` that key
        #: was actually *sent*. A LAN radio re-GETs this warm session every
        #: 20-60 s (journal evidence: Kitchen-Roberts-Radio reattaching with
        #: 15-72 chunks prerolled on every attach), and priming the full
        #: reserve each time replayed up to 10 s of already-played audio - the
        #: reported "~2 s repeated every now and then". See `_join_floor`.
        #:
        #: The unit is bytes of audio, not a timestamp, and that is not
        #: cosmetic: the reserve is filled in bursts, so a wall-clock floor is
        #: always behind the oldest piece of the reserve and can never cut
        #: anything.  See `Subscriber.reserve_bytes_sent`.
        self._floors: Dict[Tuple[str, str], int] = {}

        #: When this station last had a listener re-anchored (``time.monotonic``),
        #: and how many times that has happened.  ``None`` - not ``0.0`` - means
        #: "never", and the difference is a bug that was found by a test: with
        #: ``0.0`` the first re-anchor is suppressed until the machine's
        #: monotonic clock has passed the cooldown, because "0.0" reads as an
        #: event at time zero.  On the Pi, up for days, that never showed; on a
        #: freshly-booted host it means the watchdog silently does nothing.
        self.last_stall_reconnect: Optional[float] = None
        self.stall_reconnects: int = 0

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

    def add_subscriber(
        self, client: str = "", user_agent: str = "", metadata: bool = False
    ) -> Subscriber:
        floor = self._join_floor(client, user_agent)
        subscriber = Subscriber(
            client=client,
            user_agent=user_agent,
            metadata=metadata,
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
        # The floor is logged because it is the difference between a rejoin that
        # *resumes* and one that rewinds: `none` means the whole reserve was
        # handed over, i.e. up to `preroll_seconds` of audio this radio has
        # already played. A rewind is a splice in the stream, which is the one
        # class of defect an MP3 decoder cannot recover from - so an attach log
        # reading `floor=none, 27 chunks` on a *reconnect* is a defect, while
        # `27 chunks` on a genuinely new listener is correct.
        log.info(
            "listener attached (%d now listening, %d chunks prerolled, floor=%s)",
            len(self.subscribers),
            subscriber.queue.qsize(),
            "none" if floor is None else floor,
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
    def reanchor_listeners(self) -> int:
        """End every listener's response so its radio opens a fresh connection.

        The session itself stays up: its pipeline, its reserve and its frame
        grid all keep running, so the radio that reconnects is served from a
        warm session and resynchronises its decoder immediately. Only the
        *connections* are ended.

        This is the one remedy for a wedged radio. Its decoder cannot recover
        from damage it has already taken, and it will not ask for a new
        connection while bytes are still arriving - so the connection has to be
        closed for it. Confirmed on the real hardware: after the server closed
        the socket, the radio reconnected on its own and the audio was clean.

        Returns how many listeners were ended.
        """
        listeners = list(self.subscribers)
        for subscriber in listeners:
            self._signal_end(subscriber)
        return len(listeners)

    def _signal_end(self, subscriber: Subscriber) -> None:
        """Ask a subscriber's response generator to stop, *now*.

        The sentinel used to be appended to the queue, and that made the
        re-anchor take as long as the backlog it had to get through. The
        response generator reads the queue in order and the pacer hands those
        chunks out at the stream's realtime rate, so a listener holding the
        27-chunk (9.2 s) reserve did not see the sentinel for 9.2 s - measured
        on the live server as a 7-11 s delay between "closing 1 listener
        connection" and the connection actually closing.

        That delay defeated the whole point. The radio's decoder is already
        broken at that moment; the re-anchor exists to cut it loose *so it can
        resynchronise*, and every extra second of backlog is more damaged
        audio handed to a decoder that cannot recover from it. It also
        swallowed the cooldown: the watchdog fired again while the first
        repair was still in flight, so the radio was hit twice and the repair
        arrived later than the "within 10 s" promise.

        So the queue is emptied and the sentinel goes in *first*: the radio is
        disconnected immediately, and the audio still queued for it is audio
        that was going to be discarded by the disconnect anyway.

        The backlog is discarded rather than played out. It is not counted as
        ``dropped``: that counter means "this listener could not keep up and
        the station threw audio away", and a deliberate disconnect is the
        opposite of a capacity problem. The audio never reached the socket, so
        ``bytes_sent`` never counted it - nothing is misreported.
        """
        while True:
            try:
                subscriber.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            subscriber.queue.put_nowait(None)
        except asyncio.QueueFull:
            # Make room for the sentinel: it is more important than audio.
            with contextlib.suppress(asyncio.QueueEmpty, asyncio.QueueFull):
                subscriber.queue.get_nowait()
                subscriber.queue.put_nowait(None)

    @property
    def frame_size(self) -> Optional[int]:
        """The size in bytes of one audio frame, once the grid has been found.

        This is the number the ICY grid has to be a multiple of, so that a
        metadata block never lands in the middle of a frame.  ``None`` until
        the session has identified the stream - the API then falls back to
        the configured grid.

        It is the size of the frame **this session is actually sending**,
        read from the frame header, so it is right for a re-encoded 576-byte
        MP3 frame and for a forwarded 417-byte frame alike.
        """
        return self._frame_size

    def _output_format(self) -> Optional[str]:
        """The format the listener actually receives: ``"mp3"``, ``"adts"`` or ``None``.

        This cannot be read off `settings.output_codec`, because that is a
        *setting* and the pipeline may not honour it:

        * ``output_codec = "mp3"`` with an AAC source -> FFmpeg transcodes to
          MP3, so the listener gets MP3.
        * ``output_codec = "adts"`` with an MP3 source -> FFmpeg transcodes to
          AAC, so the listener gets ADTS.
        * ``output_codec = "auto"`` -> the source is forwarded untouched, so
          the listener gets whatever the source carries - MP3 or ADTS
          depending on the station.

        The pipeline knows: `pipeline.stats.detected_codec` is what we emit -
        ``"mp3"``, ``"aac"``, or the source's own kind for a passthrough
        stream. Before the first segment arrives it is not known, and the
        answer is deferred until it is - which is fine, because the alignment
        is decided on the *first audio*, not on the configuration.

        The distinction matters: the frame grid a radio resynchronises on is
        the one of the format it is *actually* being sent. Aligning an MP3
        stream as if it were ADTS (or skipping an MP3 stream because the
        setting says "auto") is exactly the bug this fixes.
        """
        codec = self.pipeline.stats.detected_codec
        if codec == "mp3":
            return "mp3"
        if codec == "aac":
            return "adts"
        # The passthrough path records the source's own kind, which is either
        # "mp3" or "adts" - the same two names.
        if codec == "adts":
            return "adts"
        # Not known yet: fall back to the setting, which is right for the
        # transcode paths (where we always know what we asked for).
        if self.settings.output_codec == "mp3":
            return "mp3"
        if self.settings.output_codec == "adts":
            return "adts"
        return None

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

        For MP3 the pieces are additionally cut on **frame boundaries** (see
        :meth:`_frame_aligned`), which is not a tidiness detail but the
        difference between a radio showing the title and a radio showing
        binary noise.
        """
        chunk = self._frame_aligned(chunk)
        if not chunk:
            return
        self.stats.bytes_out += len(chunk)
        self._pending += chunk

        while len(self._pending) >= self._piece_size():
            piece = self._cut_piece()
            if piece is None:
                break
            self._enqueue(piece)

    def _cut_piece(self) -> Optional[bytes]:
        """Cut the next piece off the pending buffer, on a frame boundary.

        The piece is at least `chunk_size` bytes and ends on a frame boundary,
        so it is a whole number of frames - 15 frames = 8640 bytes for the
        192 kbit/s stream this project produces, because 8192 is not a
        multiple of 576.

        Cutting on a frame boundary matters because a piece is the unit a
        *joining* listener starts from (see `_join_floor`): a piece that
        began mid-frame forces the radio to resynchronise its decoder, and a
        radio that resynchronises re-anchors its metadata grid - which is
        what makes the title garble.

        The pending buffer holds whole frames (see `_frame_aligned`), so the
        cut is a walk over the frame sizes rather than a guess.
        """
        target = self._piece_size()
        if len(self._pending) < target:
            return None
        fmt = self._output_format()
        if fmt is None or not self._frame_grid:
            # No frame grid: cut on the size, as before.
            piece = bytes(self._pending[:target])
            del self._pending[:target]
            return piece
        # Cut at the first frame boundary at or after the target.
        cut = 0
        while True:
            size = self._frame_size_at(bytes(self._pending), cut, fmt)
            if size is None or cut + size > len(self._pending):
                break
            cut += size
            if cut >= target:
                break
        if cut == 0:
            # No complete frame yet: wait for more.
            return None
        piece = bytes(self._pending[:cut])
        del self._pending[:cut]
        return piece

    def _piece_size(self) -> int:
        """Bytes per piece: `chunk_size`.

        The queue bound is counted in pieces, so the piece size has to be a
        known number of bytes. The piece itself is a whole number of frames
        (see `_cut_piece`), which is very slightly larger than this for MP3:
        15 frames = 8640 bytes rather than 8192.
        """
        return max(1024, self.settings.chunk_size)

    def _frame_aligned(self, chunk: bytes) -> bytes:
        """Return audio that starts on an audio frame boundary.

        Which frame boundary depends on the format the listener is actually
        being sent (see `_output_format`): an MP3 frame for MP3, an ADTS
        frame for AAC.

        Why a radio needs this
        ----------------------
        A radio is two state machines over one byte stream: an MP3 decoder that
        looks for frame syncs, and an ICY metadata parser that counts
        ``metaint`` bytes and then reads a block. They have to agree about
        where the stream starts. If the audio begins in the middle of a frame,
        the radio discards the partial frame to resynchronise - and the two
        state machines then disagree about where they are, so the radio reads
        audio bytes as metadata (a garbled title) and metadata bytes as audio
        (glitching). Both symptoms at once, and neither explained by the
        station.

        The offset is a *constant*, so it does not heal: the radio's counter
        stays wrong for the life of the connection, and the display stays
        garbled until the radio reconnects and re-anchors. That is exactly
        what was reported.

        Why the pieces were not aligned
        -------------------------------
        The audio was cut into fixed-size pieces, and those sizes are not a
        multiple of a frame: the produced stream is 192 kbit/s at 48 kHz, so a
        frame is 576 bytes, while the pieces are 8192 (8192 % 576 = 128).
        Every listener therefore started at a random point *inside* a frame.
        Measured on a real radio's own TCP traffic, the first frame of each
        connection began at offsets 320, 384, 262, 390, 517 and 454 - never
        reliably 0.

        How it is done
        --------------
        The frame grid is found **once**, on the first audio that arrives, and
        then maintained: the session keeps the leftover of a partial frame and
        prepends it to the next chunk, so every piece starts exactly on a frame
        boundary and **no audio is lost** after the very first frame.

        Which grid
        ----------
        The grid is the one of the format the listener is *actually* being
        sent - see `_output_format`. That is MP3 for a transcoded stream and
        for an MP3 source forwarded as-is, and ADTS for an AAC one. Both have
        their own frame structure, and a radio resynchronises on whichever it
        is decoding.

        A stream whose format is not known yet is passed through untouched
        until it is: aligning to a *guessed* grid would throw away audio for
        nothing.
        """
        fmt = self._output_format()
        if fmt is None:
            # We do not yet know what we are sending, so we cannot align it.
            # The first bytes go out as they are.
            return chunk
        if self._grid_format is not None and self._grid_format != fmt:
            # The codec changed under us: a pipeline restart, and the station
            # failed over to a source with a different codec.
            #
            # Everything we know about the frame grid belongs to the *old*
            # format: the frame size, the grid itself, and - worst of all -
            # the bytes held back for a frame that is never going to
            # complete. Keeping them means the new format is cut on the old
            # format's boundaries, and a piece that starts mid-frame is
            # exactly what makes a radio lose sync.
            #
            # Measured: with a stale MP3 grid and an ADTS stream arriving,
            # the session delivered **zero** bytes - the radio went silent.
            log.info(
                "the output format changed from %s to %s; the frame grid is reset",
                self._grid_format,
                fmt,
                extra={"session": self.id},
            )
            self._reset_frame_grid()
        if self._frame_grid is None:
            # The first audio we see: find where a frame starts. Only the
            # leading partial frame is dropped - the radio would have discarded
            # it anyway, and it is the only thing that is ever lost.
            #
            # The search runs over everything held so far, not over this
            # chunk alone: FFmpeg's pipe hands back 578 bytes at a time, and
            # 578 bytes cannot contain a frame *pair* (576 + 576 for MP3), so
            # looking only at the first read would never find the grid. The
            # bytes are kept until the search succeeds, and only then is the
            # partial frame at the front dropped.
            self._align_pending += chunk
            pending = bytes(self._align_pending)
            offset = self._align_offset(pending, fmt)
            if offset is None:
                # Still no frame. Either the stream is not in the format we
                # expect, or a frame is still incomplete.
                #
                # "Not this format" can be answered at once: a buffer with no
                # sync byte anywhere in it is not a stream of this format.
                # This matters - buffering blindly would hold the audio back
                # for no reason, and a stream that is *not* in this format
                # must be passed through untouched anyway (aligning it to a
                # guessed grid would throw audio away for nothing).
                #
                # The "anywhere" has to mean *anywhere in the buffer we have
                # held*, not just in the first 4096 bytes: a stream that
                # starts with a few hundred bytes of something else - a
                # station's own preamble, a synchronisation marker - would
                # otherwise be declared "not MP3" forever, and the alignment
                # would never happen even though the stream is MP3.
                #
                # But the verdict needs *evidence*, so it is only given once
                # enough bytes have been examined. Deciding on the first
                # chunk - as little as 578 bytes - was how a stream with a
                # short preamble was thrown away.
                if not self._has_sync(pending, fmt):
                    if len(self._align_pending) < NO_SYNC_VERDICT_BYTES:
                        # Not enough bytes to be sure yet. Keep them and look
                        # again once there are more. The bound is small
                        # (43 ms of a 192 kbit/s stream) so the audio is not
                        # held back for long.
                        return b""
                    # Enough bytes, and not one sync word in any of them:
                    # this really is not this format.
                    self._frame_grid = False
                    out = bytes(self._align_pending)
                    self._align_pending.clear()
                    return out
                # There IS a sync byte, so it probably is this format but the
                # frame pair is not complete yet. Keep the bytes and try again
                # with the next chunk.
                budget = ALIGN_SEARCH_BUDGET.get(fmt, 4 * FRAME_HINT)
                if len(self._align_pending) > budget:
                    # A lot of bytes and still no frame: give up on the grid
                    # rather than stall the audio indefinitely.
                    self._frame_grid = False
                    out = bytes(self._align_pending)
                    self._align_pending.clear()
                    return out
                return b""
            self._frame_grid = True
            self._grid_format = fmt
            self._frame_size = self._frame_size_at(pending, offset, fmt)
            # Drop everything before the boundary, and keep the rest.
            chunk = bytes(self._align_pending[offset:])
            self._align_pending.clear()
        if not self._frame_grid:
            return chunk

        # Cut the audio at frame boundaries: keep the whole frames, and hold
        # the tail of a partial frame for the next chunk.
        self._align_pending += chunk
        data = bytes(self._align_pending)
        cut = 0
        while True:
            size = self._frame_size_at(data, cut, fmt)
            if size is None or cut + size > len(data):
                break
            cut += size
        if cut == 0:
            return b""
        out = data[:cut]
        self._align_pending = bytearray(data[cut:])
        return out

    def _reset_frame_grid(self) -> None:
        """Forget everything known about the frame grid.

        Used when the output format changes under the session. Three things
        belong to the *old* format and must not be carried over:

        * the frame size and the grid itself;
        * the bytes held back for a frame that is never going to complete -
          they are the tail of a frame in the old format, and a radio
          decoding the new format would find them to be garbage in front of
          its first frame;
        * the bytes waiting in the re-chunking buffer, which have already
          been through the old format's frame alignment.

        The last two are dropped rather than handed over. The radio
        resynchronises on the new format's first frame, which is what it
        does anyway when a stream changes codec - and the alternative, a
        piece that begins with the old format's bytes, is a piece that
        begins mid-frame in the new one.
        """
        self._align_pending.clear()
        self._pending.clear()
        self._frame_grid = None
        self._grid_format = None
        self._frame_size = None

    @staticmethod
    def _align_offset(data: bytes, fmt: str) -> Optional[int]:
        """Offset of the first frame of *fmt* in *data*, or ``None``.

        The whole buffer is searched, not just its first 8192 bytes. The
        buffer is bounded by the search budget, and a stream whose first
        frame is further in than 8 KB - a station that opens with a long
        preamble, a synchronisation marker - would otherwise never be
        aligned at all.
        """
        if fmt == "mp3":
            return mp3_align_offset(data, window=len(data))
        return adts_align_offset(data, window=len(data))

    @staticmethod
    def _has_sync(data: bytes, fmt: str) -> bool:
        """True if *data* contains anything that looks like a frame of *fmt*.

        The whole buffer is examined, not just its first few kilobytes. The
        buffer is bounded anyway - it is the alignment search's holding
        area, and it never grows past the search budget - so scanning all
        of it is cheap, and it is what makes the "this is not MP3" decision
        safe: a stream that begins with a few hundred bytes of something
        else is still recognised as MP3 once its first frame arrives.
        """
        if fmt == "mp3":
            return mp3_has_sync(data, window=len(data))
        return adts_has_sync(data, window=len(data))

    @staticmethod
    def _frame_size_at(data: bytes, offset: int, fmt: str) -> Optional[int]:
        """Length of the frame of *fmt* at *offset*, or ``None``."""
        if fmt == "mp3":
            return mp3_frame_size(data, offset)
        return adts_frame_size(data, offset)

    def _enqueue(self, piece: bytes) -> None:
        self.stats.chunks += 1
        # One offset per piece, shared by the reserve *and* every queue, so a
        # delivered piece can be compared against reserve entries later - that
        # comparison is the join-floor.  The offset is the piece's position
        # in the session's running total of audio produced, and NOT a
        # timestamp: see `Subscriber.reserve_bytes_sent` for why the clock
        # cannot be used here.  Queue items are ``(offset, piece)`` tuples;
        # the end-of-stream sentinel stays ``None``.
        self._audio_bytes += len(piece)
        offset = self._audio_bytes
        self._reserve_piece(piece, offset)
        for subscriber in self.subscribers:
            try:
                subscriber.queue.put_nowait((offset, piece))
            except asyncio.QueueFull:
                # This listener is behind. Drop the *oldest* audio for it and
                # carry on: one slow radio must never stall the station.
                subscriber.dropped += 1
                with contextlib.suppress(asyncio.QueueEmpty):
                    subscriber.queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    subscriber.queue.put_nowait((offset, piece))

    def _reserve_piece(self, piece: bytes, offset: int) -> None:
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
        self._reserve.append((offset, piece))
        # Bounded by the AUDIO it holds, not by the wall-clock it was produced
        # over.  Those are very different numbers, and the difference is the
        # whole bug this fixes.
        #
        # FFmpeg transcodes a ten-second segment in about 1.5 seconds, so a
        # burst of pieces is produced far faster than realtime and every piece
        # of a burst lands inside a time window only a second wide.  Bounding
        # the reserve by ``produced_at - preroll_seconds`` therefore held far
        # more audio than it says: measured on the live service,
        # ``preroll_seconds = 10.0`` produced a reserve of **57 pieces,
        # 20.5 seconds of audio** - twice the configured maximum.
        #
        # A rejoining radio is primed with that reserve, so it was served up
        # to twice the configured duration of audio it had already played.
        # That is a splice, it breaks the MP3 bit reservoir, and the radio's
        # decoder never resynchronises.
        #
        # So the bound is on the audio the reserve holds.  The piece count
        # follows from the bitrate, and the bitrate is not known until the
        # stream has been identified - so the reserve tracks its own byte
        # total and drops the oldest pieces until it is back within
        # ``preroll_seconds`` of audio.  Measured after: 27 pieces, 9.72 s.
        self._reserve_bytes += len(piece)
        limit = self._reserve_limit_bytes()
        while self._reserve and self._reserve_bytes > limit:
            _, dropped = self._reserve.popleft()
            self._reserve_bytes -= len(dropped)
        # A hard cap on the piece count as well, because a pathological
        # bitrate should not be able to turn this into unbounded memory on
        # a Pi.
        while len(self._reserve) > self.settings.queue_chunks:
            _, dropped = self._reserve.popleft()
            self._reserve_bytes -= len(dropped)

    def _reserve_limit_bytes(self) -> int:
        """How many bytes of audio the reserve may hold.

        ``preroll_seconds`` is a duration of *audio*, so the byte budget is
        that duration at the stream's own bitrate.  The bitrate is the
        measured one when it is known - a forwarded station runs at its own
        rate, not the configured one - and the configured one otherwise.
        """
        rate = self.pipeline.output_bitrate or self.settings.bitrate
        if rate <= 0:
            rate = self.settings.bitrate or 192000
        return max(int(self.settings.preroll_seconds * rate / 8), 1)

    def _join_floor(self, client: str, user_agent: str) -> Optional[int]:
        """How many bytes of the session's audio *client* was already sent.

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

        Keyed on the IP **alone**, not on (IP, User-Agent): the same radio
        reconnects with a *different* User-Agent, and a different User-Agent
        meant a different key, no floor, and the full reserve replayed to a
        radio that had already played it.  That replay splices two parts of
        the stream and breaks the MP3 bit reservoir, so the radio's decoder
        never resynchronises - which is the reported "garbled title and
        glitching audio, forever, until a server restart".  See
        ``_floor_key``.

        Measured in **bytes of the session's audio**, not in wall-clock
        seconds, and that is the point.  The reserve is filled in bursts, so
        a client that is served the reserve and then plays it is
        ``preroll_seconds`` behind the production edge *in audio* while
        being a fraction of a second behind *in wall-clock*: the pieces of a
        burst are produced inside a second but hold ten seconds of audio.
        A wall-clock floor was therefore always behind the oldest piece of
        the reserve, so it could never cut anything, and every rejoin was
        served the whole reserve - the audio the radio had already played.
        Counting bytes removes the clock from the comparison entirely.
        """
        key = self._floor_key(client, user_agent)
        floor = self._floors.get(key)
        for other in self.subscribers:
            if (
                self._floor_key(other.client, other.user_agent) == key
                and other.reserve_bytes_sent is not None
                and (floor is None or other.reserve_bytes_sent > floor)
            ):
                floor = other.reserve_bytes_sent
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
        overlap_bytes = self._overlap_bytes()
        return max(0, floor - overlap_bytes)

    def _overlap_bytes(self) -> int:
        """How many bytes of already-delivered audio the handoff may re-send.

        The window is a duration of *audio*, so it is a number of bytes at
        the stream's bitrate - the same conversion `_reserve_limit_bytes`
        does, and for the same reason.
        """
        rate = self.pipeline.output_bitrate or self.settings.bitrate
        if rate <= 0:
            rate = self.settings.bitrate or 192000
        return int(self.settings.handoff_overlap_seconds * rate / 8)

    @staticmethod
    def _floor_key(client: str, user_agent: str) -> Tuple[str, str]:
        """The key a join floor is stored under: the IP alone.

        Why the IP and not ``(client, user_agent)``
        --------------------------------------------
        Measured on the real radio (2026-10-21): the SAME device opens
        connections with two different User-Agents - ``NSPlayer/8.0.0.3801``
        (which sends ``Icy-MetaData: 1``) and ``FSL IR2.10`` (which does not).
        The Frontier Silicon firmware uses one as the media-player stack
        starts and the other afterwards, and which one is in use when the
        connection drops is not something this server can predict.

        Keying the floor on both meant a radio that came back with the other
        User-Agent was a *different* key: no floor was found, and the whole
        reserve was served to a radio that had already played it.

        That replay is not "the song started again".  It splices two
        different parts of the stream, and an MP3 stream's frames carry a
        **bit reservoir** - each frame's side information says how many bits
        to take from the frames before it.  A splice breaks those pointers,
        and a decoder reading the wrong bits does not merely sound wrong
        once: the reservoir is refilled from the wrong place on every frame,
        so it never resynchronises.  The radio's decoder is a continuous
        state machine that survives the TCP reconnect, which is why the
        fault outlived the interference that triggered it.

        Measured on the live server: reconnecting with a different
        User-Agent replayed from offset 0 of the previous connection's
        stream (3.34 s of already-heard audio); keyed on the IP it does not
        overlap at all.

        The cost is that two *different* devices behind one NAT address share
        a floor.  That is the mild side of the trade: a shared floor means a
        later start, not a replay.  A client never seen on that IP still gets
        the full reserve, which is what a genuinely new listener wants.
        """
        return (client, "")

    def _note_floor(self, subscriber: Subscriber) -> None:
        """Remember how far a departing connection got, for its next attach."""
        if subscriber.reserve_bytes_sent is None:
            # Nothing reached the transport (abandoned or probe connection), so
            # a later attach must not be floored by it - it heard nothing.
            return
        key = self._floor_key(subscriber.client, subscriber.user_agent)
        # Re-insert rather than assign, so eviction sees the freshest keys.
        self._floors.pop(key, None)
        self._floors[key] = subscriber.reserve_bytes_sent
        while len(self._floors) > MAX_JOIN_FLOORS:
            self._floors.pop(next(iter(self._floors)))

    def _prime(self, subscriber: Subscriber, floor: Optional[int] = None) -> None:
        """Give a newly connected listener the reserve, oldest piece first.

        Without this a joining radio waits for the next production burst, which is
        up to eight seconds of silence - measured, not estimated. Its own buffer
        also starts at zero, so the first segment that arrives has no cushion
        behind it.

        ``floor`` (from ``_join_floor``) skips the pieces this particular client
        was already sent - what turns "attach" from a replay into a resume. A
        new client has no floor and gets the full reserve, as before.

        The floor is a number of bytes of the session's audio, and each
        reserve piece carries its own offset in that total, so the
        comparison does not involve the clock at all.  See `_join_floor` for
        why a wall-clock floor could not work here.

        The reserve is already frame-aligned: it is filled from the same
        frame-aligned pieces that live listeners receive (see
        :meth:`_fan_out`), so a joining radio's decoder and its ICY metadata
        parser start in agreement.
        """
        for offset, piece in self._reserve:
            if floor is not None and offset <= floor:
                # Already sent to this client: skip it.
                continue
            try:
                subscriber.queue.put_nowait((offset, piece))
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

        The tail is sent **as it is**. It is not trimmed to a multiple of the
        frame length, because the frame length is not the same for every
        frame: MP3 frames alternate between two sizes when the sample rate
        needs a padding byte, and ADTS frames are variable-length by
        definition. Trimming by the *first* frame's length would delete a
        legitimate byte from the end of the stream - for two 192 kbit/s
        44.1 kHz frames of 626 and 627 bytes, ``1253 % 626`` is 1, so one
        valid byte would be dropped.

        Anything still in `_align_pending` is a genuinely *incomplete* frame
        - the tail of a frame whose header was seen but whose body was not
        - and is dropped, because a partial frame is not decodable.
        """
        if self._align_pending:
            # A partial frame: not decodable, and the stream is ending.
            log.debug(
                "dropping %d bytes of partial frame at end of stream",
                len(self._align_pending),
                extra={"session": self.id},
            )
            self._align_pending.clear()
        if not self._pending:
            return
        tail = bytes(self._pending)
        self._pending.clear()
        self._enqueue(tail)

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
                self._reserve_bytes = 0
                # The new pipeline may be sending a different codec - the
                # station failed over while we slept - so the frame grid,
                # the frame size and the held-back bytes all belong to the
                # old stream and must not be carried over.
                self._reset_frame_grid()
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
                # How many listeners this station's stall watchdog has closed
                # so far, so a dashboard can tell "the microwave was on" from
                # "nothing happened" without reading the journal.
                "stall_reconnects": self.stall_reconnects,
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
        #: Samples the OS retransmission counter. ``None`` when the watchdog is
        #: off by configuration or unsupported on this platform.
        self._retransmit_task: Optional[asyncio.Task] = None
        #: Reads the cumulative retransmission count; see `radioconv.retrans`.
        self.retransmit_counter: RetransmitCounter = tcp_retransmit_counter()
        #: Segments retransmitted in the most recent poll, for the dashboard, and
        #: the largest burst seen - so "was the Wi-Fi bad?" is answerable after
        #: the fact without a packet capture.
        self.retransmit_last_delta = 0
        self.retransmit_peak_delta = 0
        #: The blind periodic re-anchor: the only trigger that cannot miss.
        #: See `Settings.reanchor_interval_seconds`.
        self._safety_task: Optional[asyncio.Task] = None
        #: Service-wide count of re-anchors done by the timer rather than by
        #: evidence. Kept apart from the two watchdog counters so an operator can
        #: see which mechanism is actually repairing their radios.
        self.scheduled_reconnects = 0
        #: Segments retransmitted over the trailing window, and the largest
        #: total seen. A single poll cannot separate interference from a
        #: healthy link - the healthy link reaches 6 in one poll and
        #: interference starts at 3 - but the totals over ten seconds do.
        #: See `Settings.retransmit_window_segments`.
        self.retransmit_window_delta = 0
        self.retransmit_peak_window = 0
        self.consecutive_failures = 0
        self.capacity_rejections = 0
        #: Service-wide count of listeners closed by the stall watchdog.
        #:
        #: Here rather than summed over the sessions, for the same reason the
        #: byte counters above live here: a session is reaped once its radios
        #: have been gone for ``linger_seconds``, so summing what is currently
        #: registered makes the total *fall*. Observed on the Pi - the figure
        #: went 1, then back to 0 when the station it belonged to was reaped -
        #: and a Prometheus counter that decreases breaks ``rate()`` and is
        #: simply untrue. Each session keeps its own count as well, for the
        #: dashboard.
        self.stall_reconnects = 0
        #: Service-wide count of listeners closed because the OS reported a
        #: retransmission burst - Wi-Fi interference, in practice. Kept separate
        #: from ``stall_reconnects`` on purpose: "the upstream starved" and "the
        #: network dropped packets" are different failures with the same remedy,
        #: and merging the counters would hide which one is happening.
        self.retransmit_reconnects = 0
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
        starting_watchdog = (
            self.settings.retransmit_reconnect_segments > 0 and self.retransmit_counter.available
        )
        if starting_watchdog and self._retransmit_task is None:
            self._retransmit_task = asyncio.create_task(
                self._retransmit_loop(), name="radioconv-retransmit"
            )
        if self.settings.reanchor_interval_seconds > 0 and self._safety_task is None:
            self._safety_task = asyncio.create_task(self._safety_loop(), name="radioconv-reanchor")

    async def aclose(self) -> None:
        for task in (self._reaper, self._sampler, self._retransmit_task, self._safety_task):
            if task is not None:
                task.cancel()
        for session in list(self.sessions.values()):
            await session.close("shutting down")
        self.sessions.clear()

    # --------------------------------------------------- network watchdog
    def reanchor_stalled_sessions(self, now: Optional[float] = None) -> int:
        """End the listeners of every station allowed to act, and count them.

        Extracted from the poll loop so the decision - which stations are
        eligible, and whether the cooldown has expired - is testable without a
        socket, a clock or a broken network.

        The cooldown is per *station*, not per listener and not global. Per
        station, because one bad patch of Wi-Fi affects them all and they should
        each get their chance; not global, because a station that is working
        should not be denied a repair because another one just had one.
        """
        at = time.monotonic() if now is None else now
        cooldown = self.settings.stall_reconnect_cooldown_seconds
        touched = 0
        for session in list(self.sessions.values()):
            if not session.listener_count:
                continue
            previous = session.last_stall_reconnect
            if previous is not None and at - previous < cooldown:
                # Reconnected very recently: give the radio time to recover
                # before closing its socket again.  ``None`` means it has never
                # been re-anchored, which is always allowed.
                continue
            session.last_stall_reconnect = at
            session.stall_reconnects += 1
            touched += session.reanchor_listeners()
        if touched:
            self.retransmit_reconnects += 1
        return touched

    # ------------------------------------------------- periodic safety net
    def reanchor_overdue_sessions(self, now: Optional[float] = None) -> int:
        """End the listeners of any station that has not been re-anchored lately.

        The blind half of the repair, and the only half that cannot miss. Both
        other triggers act on *evidence* of damage, and there is a band of
        interference that wedges a radio's decoder without crossing either
        threshold - measured on the song-titles channel, where the radio went
        into the permanent garble with neither trigger firing while the stream
        being sent was verified perfect.

        Once the interference stops, a wedged radio is indistinguishable from a
        healthy one (it acknowledges every byte at full rate), so no amount of
        cleverness in the evidence-based triggers can bound how long it stays
        broken. Only a timer can - this one.

        Extracted from the loop so the decision is testable without a clock or
        a socket, exactly as ``reanchor_stalled_sessions`` is.
        """
        interval = self.settings.reanchor_interval_seconds
        if interval <= 0:
            return 0
        at = time.monotonic() if now is None else now
        touched = 0
        for session in list(self.sessions.values()):
            if not session.listener_count:
                continue
            previous = session.last_reanchor_at
            if previous is not None and at - previous < interval:
                continue
            session.last_reanchor_at = at
            session.scheduled_reconnects += 1
            touched += session.reanchor_listeners()
        if touched:
            self.scheduled_reconnects += 1
            log.warning(
                "re-anchoring %d listener connection(s) on a %.0fs timer, so a "
                "radio that has stopped decoding cannot stay that way: the "
                "evidence-based triggers key on damage, and a wedged radio "
                "produces none",
                touched,
                interval,
            )
        return touched

    async def _safety_loop(self) -> None:
        """Run the periodic safety net until cancelled."""
        interval = max(1.0, min(30.0, self.settings.reanchor_interval_seconds / 4.0))
        while True:
            await asyncio.sleep(interval)
            try:
                self.reanchor_overdue_sessions()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - a timer must never die
                log.exception("periodic re-anchor failed; it will try again")

    async def _retransmit_loop(self) -> None:
        """Re-anchor the listeners when the OS reports a retransmission burst.

        See `Settings.retransmit_reconnect_segments` for why this is the signal
        and the measured numbers behind the thresholds.

        Two triggers, because one poll is the wrong unit
        ------------------------------------------------
        A single poll cannot tell interference from a healthy link: the healthy
        link reaches 6 retransmissions in one poll and interference starts at
        3. What separates them is the total over a few seconds - healthy
        peaked at 7 over ten seconds, interference at 21 - so the loop keeps a
        trailing window of per-poll deltas and sums it.

        The per-poll threshold is kept as well, for the "one sharp burst" case
        that the window would dilute.

        Why the window is emptied when it fires
        ---------------------------------------
        Measured, before that was done: a single interference event fired
        **twice**. The per-poll trigger fired at 16:34:09 (16 segments in one
        second), and the window still held that same damage - 24, 24, 24, 23,
        23 - so the moment the cooldown expired at 16:34:14 the *window* trigger
        fired again on evidence that had already been acted upon. The radio had
        reconnected in that very second and was killed within it ("done after
        1s"), reconnected again, and stayed glitching.

        That is fatal rather than merely wasteful: a radio needs a few seconds
        of unbroken stream to rebuild its decoder state, and a second re-anchor
        taken on the same evidence guarantees it never gets them. The cooldown
        (5 s) is deliberately shorter than the window (10 s), so without
        emptying it, *any* window-triggered re-anchor can always be repeated
        once.

        Emptying the window keeps the trigger honest: damage that has been
        acted upon is not evidence for acting again. New damage still re-arms
        it, so sustained interference is caught at the same latency - it just
        has to be genuinely new.
        """
        threshold = self.settings.retransmit_reconnect_segments
        window_seconds = self.settings.retransmit_window_seconds
        window_threshold = self.settings.retransmit_window_segments
        poll_seconds = self.settings.retransmit_poll_seconds
        #: The per-poll deltas still inside the trailing window, oldest first.
        window: Deque[int] = deque()
        #: How many polls the window holds: its length in seconds.
        window_polls = max(1, round(window_seconds / max(poll_seconds, 0.01)))

        previous = await asyncio.to_thread(self.retransmit_counter.read)
        while True:
            await asyncio.sleep(poll_seconds)
            total = await asyncio.to_thread(self.retransmit_counter.read)
            if total is None or previous is None:
                # No reading this tick (a transient failure, or the very first
                # poll). Adopt whatever we have and compare from there, rather
                # than treating a failed read as a burst of zero - or worse, as
                # a huge negative one.
                previous = total
                continue
            delta = total - previous
            previous = total
            # A counter that went backwards means the OS restarted its stats (or
            # the figure wrapped); ignore it rather than reading it as damage.
            if delta < 0:
                continue
            self.retransmit_last_delta = delta
            self.retransmit_peak_delta = max(self.retransmit_peak_delta, delta)

            window.append(delta)
            while len(window) > window_polls:
                window.popleft()
            window_sum = sum(window)
            self.retransmit_window_delta = window_sum
            self.retransmit_peak_window = max(self.retransmit_peak_window, window_sum)

            burst = delta >= threshold
            sustained = window_threshold > 0 and window_sum >= window_threshold
            if not burst and not sustained:
                continue
            # Consume the evidence as soon as it is acted upon - and if there
            # is nobody to act upon, discard it anyway. A radio that attaches
            # later is a *fresh connection with a clean stream*, and must not
            # be killed for damage that arrived while it was not listening.
            # (Measured shape of that bug: the window held 29 while the station
            # was warm with no listeners, so the next radio to attach would
            # have been disconnected on its first poll.)
            window.clear()
            self.retransmit_window_delta = 0
            closed = self.reanchor_stalled_sessions()
            if not closed:
                continue
            if sustained and not burst:
                log.warning(
                    "the network retransmitted %d TCP segments over %.1fs: "
                    "closing %d listener connection(s) so the radios "
                    "re-anchor their decoders (window threshold %d)",
                    window_sum,
                    window_seconds,
                    closed,
                    window_threshold,
                )
            else:
                log.warning(
                    "the network retransmitted %d TCP segments in %.1fs: "
                    "closing %d listener connection(s) so the radios "
                    "re-anchor their decoders (threshold %d)",
                    delta,
                    poll_seconds,
                    closed,
                    threshold,
                )

    # ------------------------------------------------------------------ keys
    @staticmethod
    def make_key(primary_url: str, settings: Settings, overrides: Optional[dict] = None) -> str:
        overrides = overrides or {}
        bitrate = overrides.get("bitrate") or settings.bitrate
        codec = overrides.get("output_codec") or settings.output_codec
        return f"{primary_url}|{codec}|{bitrate}"

    def _unique_key(self, primary_url: str, settings: Settings, overrides: Optional[dict]) -> str:
        """A key that matches no other session, for an unshared stream.

        ``share_sessions = false`` means every listener gets its own
        pipeline, so two radios on the same station must not land on the
        same session. The key is still derived from the station, so the
        dashboard and the logs still show what it is - it just carries a
        unique suffix so the registry never collides.
        """
        base = self.make_key(primary_url, settings, overrides)
        return f"{base}|{uuid.uuid4().hex[:12]}"

    # ---------------------------------------------------------------- acquire
    async def subscribe(
        self,
        primary_url: str,
        sources: Sequence[str],
        *,
        settings: Optional[Settings] = None,
        client: str = "",
        user_agent: str = "",
        metadata: bool = False,
        overrides: Optional[dict] = None,
        label: str = "",
    ):
        """Attach to (or create) the session for a station.

        Returns ``(session, subscriber)``. Raises :class:`CapacityError` when
        every pipeline slot is taken and no idle session can be evicted.
        """
        settings = settings or self.settings
        if settings.share_sessions:
            key = self.make_key(primary_url, settings, overrides)
        else:
            # Every listener gets its own pipeline, so this one must not
            # find an existing session for the same station.
            key = self._unique_key(primary_url, settings, overrides)

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
            subscriber = session.add_subscriber(
                client=client, user_agent=user_agent, metadata=metadata
            )
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
            "scheduled_reconnects": self.scheduled_reconnects,
            "capacity_rejections": self.capacity_rejections,
            #: Service-wide, and monotonic - see ``SessionManager.stall_reconnects``.
            "stall_reconnects": self.stall_reconnects,
            #: Listeners closed because the OS reported a retransmission burst,
            #: plus what the counter actually said, so a dashboard can show
            #: whether the watchdog is armed and quiet or armed and working.
            "retransmit_reconnects": self.retransmit_reconnects,
            "retransmit_source": self.retransmit_counter.name,
            "retransmit_last_delta": self.retransmit_last_delta,
            "retransmit_peak_delta": self.retransmit_peak_delta,
            "retransmit_window_delta": self.retransmit_window_delta,
            "retransmit_peak_window": self.retransmit_peak_window,
            "retransmit_window_seconds": self.settings.retransmit_window_seconds,
            "retransmit_window_segments": self.settings.retransmit_window_segments,
            "details": [s.as_dict() for s in sessions],
        }

    def note_rejection(self) -> None:
        self.capacity_rejections += 1

    def note_stall_reconnect(self) -> None:
        """Record that a listener was closed by the stall watchdog."""
        self.stall_reconnects += 1
