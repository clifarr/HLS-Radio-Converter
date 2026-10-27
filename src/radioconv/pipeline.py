"""The streaming pipeline: HLS segments in, one continuous byte stream out.

Two engines are possible, chosen per stream *after looking at the actual data*:

``passthrough``
    The audio in the segments is something the radio can play as-is (MP3 or
    ADTS AAC). Two source shapes reach this engine, and both are recognised
    from the bytes:

    * an **MPEG-TS** carrying that elementary stream, which a pure-python
      demuxer (``tsdemux.TSDemuxer``) extracts;
    * a **bare elementary stream** - the ``.aac`` segments many CDNs serve as
      raw ADTS behind an ID3v2 tag - which needs no demuxing at all, only the
      per-segment metadata tags removed
      (``tsdemux.ElementaryStreamForwarder``).

    Either way: **no FFmpeg process, no decoding, no re-encoding** - on a
    Raspberry Pi this costs a fraction of one core and cannot leak a child
    process.

``transcode``
    A bundled FFmpeg process (``-c:a copy`` when only the *container* needs
    changing, ``libmp3lame`` otherwise) converts to the target format.

Everything here is built around *bounded* behaviour, because unbounded
buffering and untimed blocking are what made the original version freeze a Pi
after days of uptime:

* every queue is bounded, so back-pressure propagates from the slowest radio
  all the way back to the HTTP read on the origin;
* every awaited operation has a timeout, so one stuck socket cannot wedge a
  pipeline forever;
* every child process runs in its own session so it can be killed as a group,
  and is always reaped;
* failures are counted and either retried, failed over to the next URL, or
  surfaced so the session supervisor can restart the pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections import deque
from collections.abc import AsyncGenerator, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Deque,
    List,
    Optional,
    Tuple,
)

from . import ffmpeg as ffmpeg_mod
from .config import Settings
from .hls import HLSClient, HLSError, HLSFatal, PlaylistTracker, Segment
from .throughput import Throughput
from .tsdemux import (
    PASSTHROUGH_TYPES,
    ElementaryStreamForwarder,
    TSDemuxer,
    audio_duration,
    classify_segment,
    id3_song_title,
    scan_tables,
)

log = logging.getLogger(__name__)

MODE_PASSTHROUGH = "passthrough"
MODE_TRANSCODE = "transcode"

#: Can we kill a whole process group? That needs ``os.killpg`` *and* the signal
#: it is given, and both are POSIX-only. Windows has neither, and also silently
#: ignores ``start_new_session``, so there is no group to kill in the first
#: place - see :func:`kill_child_process`.
_CAN_KILL_PROCESS_GROUP = (
    hasattr(os, "killpg") and hasattr(os, "getpgid") and hasattr(signal, "SIGKILL")
)


def kill_child_process(proc: asyncio.subprocess.Process) -> None:
    """Stop an ffmpeg child, and on POSIX its entire process group.

    Children are spawned with ``start_new_session=True`` so that on POSIX they
    lead their own group and one ``killpg`` reaps anything they spawned. That is
    what stops an escaped ffmpeg from burning a core forever, and it is why the
    service's systemd unit can use ``KillMode=mixed``.

    Windows needs a different route: ``os.killpg`` and ``signal.SIGKILL`` do not
    exist (accessing either raises ``AttributeError``), and
    ``start_new_session`` is accepted but ignored, so there is no group. The
    correct equivalent is ``kill()`` on the child itself.
    """
    if _CAN_KILL_PROCESS_GROUP:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            # Already gone, or not a group leader after all: fall through to
            # kill() below rather than leaking the child.
            pass
        else:
            return
    with contextlib.suppress(OSError, ProcessLookupError):
        proc.kill()


#: A ``(segment, already_downloaded_bytes_or_None)`` work item.
WorkItem = Tuple[Segment, Optional[bytes]]
#: A producer task body. It must be a *coroutine* (not merely an awaitable)
#: because it is handed to ``asyncio.create_task``.
Feeder = Callable[[asyncio.Queue], Coroutine[Any, Any, None]]


class PipelineError(RuntimeError):
    """The pipeline cannot continue on the current source."""


class PipelineFatal(PipelineError):
    """Nothing can be done with *any* of the configured sources."""


@dataclass
class PipelineStats:
    mode: str = "unknown"
    source_index: int = 0
    segments: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    #: Total duration the *playlist* claims for the segments consumed
    #: (``EXTINF``). The packager's own accounting, kept for diagnosis: it is a
    #: claim, not a measurement, and live packagers get it wrong in both
    #: directions - see ``forwarded_seconds`` for the number to measure with.
    seconds_in: float = 0.0
    #: Audio **as produced** by the FFmpeg-free path, and its real duration
    #: counted from the elementary stream's own frames. Together these give the
    #: exact rate a radio consumes audio at, which is what the output pacer has
    #: to keep up with. Measured rather than derived because both alternatives
    #: are wrong in a way that is only visible after minutes of playback:
    #: ``bytes_in`` includes the container (20 % for a transport stream), and
    #: ``EXTINF`` is the packager's claim (radiom2o declares 10 s for 10.32 s).
    forwarded_bytes: int = 0
    forwarded_seconds: float = 0.0
    #: Seconds of audio in the start-up batch - every segment the playlist
    #: already held when this pipeline started, which is fed in one burst far
    #: faster than realtime. It is reported because it is *older* audio than
    #: the steady state ever produces: a radio served from it is playing tens
    #: of seconds behind the live edge, and the session has to keep that audio
    #: in its reserve or the radio's next connection cannot resume from it -
    #: see ``StreamSession._reserve_limit_bytes``.
    startup_seconds: float = 0.0
    failures: int = 0
    failovers: int = 0
    started_at: float = field(default_factory=time.time)
    last_data_at: float = field(default_factory=time.time)
    detected_codec: Optional[str] = None
    detected_kind: Optional[str] = None
    #: "Artist - Title" of the audio currently being produced, read from the
    #: source's in-band ID3 tags when it carries them (msvdn-style CDNs do).
    #: Exposed through ``as_dict`` and injected into responses as ICY
    #: ``StreamTitle`` metadata by the API layer.
    now_playing: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "source_index": self.source_index,
            "segments": self.segments,
            "seconds_in": round(self.seconds_in, 1),
            "startup_seconds": round(self.startup_seconds, 1),
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "failures": self.failures,
            "failovers": self.failovers,
            "uptime": round(time.time() - self.started_at, 1),
            "idle": round(time.time() - self.last_data_at, 1),
            "detected_codec": self.detected_codec,
            "detected_kind": self.detected_kind,
            "now_playing": self.now_playing,
        }


def _why_it_needs_ffmpeg(detected: Optional[str], kind: Optional[str]) -> str:
    """One clause naming why this stream cannot be forwarded, for the radio owner.

    The counterpart of ``web._why_not_forwarded``, and deliberately separate:
    that one is written for somebody looking at a web page who can try a
    different URL, this one for a log line and an HTTP error where the reader is
    deciding whether to turn FFmpeg back on. Both exist because "it does not
    work" is not a diagnosis.
    """
    if detected == "mp4":
        return "the audio is packaged in fMP4, which needs a demuxer"
    if detected == "ts":
        if kind is None:
            return "its transport stream carries no MP3 or ADTS AAC audio"
        return f"forwarding cannot turn {kind} into what this installation asked for"
    if detected is None:
        return "the segment container was not recognised, so it cannot be forwarded"
    return f"the source audio ({kind or detected}) cannot be forwarded as-is"


# --------------------------------------------------------------- mode picking
def decide_mode(first_segment: bytes, settings: Settings) -> Tuple[str, Optional[str]]:
    """Decide whether the FFmpeg-free path can be used.

    Returns ``(mode, kind)``; ``kind`` is ``"mp3"``/``"adts"`` when the source
    audio can be forwarded untouched.

    Two source shapes can be forwarded, and both are answered by the bytes
    rather than by the playlist:

    * a **transport stream** carrying an MP3 or ADTS AAC elementary stream,
      which the demuxer extracts by PID;
    * a **bare elementary stream** - ``.aac`` segments on Microsoft's CDN are
      raw ADTS behind an ID3v2 tag - which needs no demuxing at all, only the
      metadata tags removed.

    What the output codec allows decides whether forwarding is *used*:
    forwarding an AAC source can only ever produce AAC, so asking for MP3 means
    a real re-encode, and ``kind`` is still reported because it tells the
    transcoder which input it is being handed.

    ``effective_output_codec`` rather than ``output_codec`` is read here, and
    that is not a shortcut: on an installation with FFmpeg disabled nothing can
    be re-encoded, so the answer to "which codec may we emit?" is "the station's
    own" whatever the config file says. See :attr:`Settings.passthrough_only`.
    """
    if not (settings.prefer_passthrough or settings.passthrough_only):
        return MODE_TRANSCODE, None

    detected = classify_segment(first_segment)
    wanted = settings.effective_output_codec

    if detected in ("mp3", "adts"):
        # Already a playable elementary stream: there is no container in the way.
        if wanted in (detected, "auto"):
            return MODE_PASSTHROUGH, detected
        return MODE_TRANSCODE, detected

    if detected != "ts":
        return MODE_TRANSCODE, None

    tables = scan_tables(first_segment)
    kind = PASSTHROUGH_TYPES.get(tables["audio"]) if tables["audio"] else None
    if kind is None:
        return MODE_TRANSCODE, None

    if kind == "adts" and wanted in ("adts", "auto"):
        return MODE_PASSTHROUGH, kind
    if kind == "mp3" and wanted in ("mp3", "auto"):
        return MODE_PASSTHROUGH, kind
    return MODE_TRANSCODE, kind


class StreamPipeline:
    """One upstream station, turned into a continuous stream of output bytes."""

    def __init__(
        self,
        settings: Settings,
        sources: Sequence[str],
        *,
        preferred_source: int = 0,
        label: str = "",
        throughput: Optional[Throughput] = None,
    ) -> None:
        self.settings = settings
        #: Where bytes downloaded from upstream are reported. Per-pipeline stats
        #: die with the pipeline and reset on every restart, so a rolling window
        #: cannot be built from them; this is the service-wide, monotonic view.
        self.throughput = throughput or Throughput()
        self.sources = [s for s in sources if s]
        if not self.sources:
            raise ValueError("at least one source URL is required")
        self.label = label or self.sources[0]

        self.stats = PipelineStats(source_index=preferred_source % len(self.sources))
        self._client = HLSClient(settings)
        self._closed = False
        self._proc: Optional[asyncio.subprocess.Process] = None

    # ------------------------------------------------------------------ public
    @property
    def current_url(self) -> str:
        return self.sources[self.stats.source_index % len(self.sources)]

    @property
    def output_bitrate(self) -> Optional[float]:
        """Bits per second this pipeline is really sending a listener.

        Measured where possible, because the number matters: the output pacer
        releases bytes at this rate, and an estimate that is too low makes the
        listener fall permanently behind (so the queue fills and audio is
        dropped).

        Two cases, and only the first is a guess-free constant:

        * **re-encoded** (the default: AAC in, MP3 out at ``settings.bitrate``) -
          FFmpeg is encoding to a bitrate we chose, so that bitrate *is* the
          answer.
        * **everything else** - either FFmpeg-free forward - where the rate is
          whatever the *produced* bytes work out to over the audio they carry.

        Why it is ``bytes_out`` and not ``bytes_in``
        --------------------------------------------
        ``bytes_in`` is what was **downloaded**, and for a transport stream that
        is mostly not audio: 188-byte packets with PAT/PMT tables, PES headers
        and adaptation-field stuffing around every payload. Measured on a real
        station (radiom2o, AAC in MPEG-TS): 199 KB downloaded per 10.3 s of audio
        against 165 KB of elementary stream - **20 % overhead**. Pacing the
        output at the downloaded rate therefore hands a radio 1.25 s of audio
        every second.

        That is not a harmless mis-timing: the queue drains in about four
        segments, and from then on the listener gets a segment's bytes and then
        two seconds of silence, every segment, forever. A Frontier-Silicon radio
        gives up after ~1.2 s of silence and reconnects, so the audible result
        is the reported *"every few seconds it either repeats the last few
        seconds or skips a few seconds"* - the repeat being
        ``handoff_overlap_seconds`` replayed on the rejoin, the skip being the
        backlog discarded with the old socket.

        Each segment also carries its own ID3 tags, so bare-ADTS forwarding has
        the same defect in miniature (a few per cent). ``bytes_out`` has both
        problems by construction: it counts only the audio that is handed
        downstream.

        Why the **duration** is counted from the frames too
        ---------------------------------------------------
        Dividing by ``EXTINF`` instead leaves a smaller version of the same
        error, and one that only shows up after minutes: the pacer's rate would
        be a few per cent above or below the real one, so the jitter buffer
        creeps empty (or full) until the listener hears gaps (or dropped audio).
        radiom2o declares 10 s for 10.32 s of audio, which is +3 % - enough to
        drain a five-second buffer in under three minutes. ``forwarded_seconds``
        is counted from the ADTS/MPEG frame headers, so both sides of the ratio
        come from the audio itself and the drift is exactly zero.
        """
        if self.stats.mode != MODE_PASSTHROUGH and self.settings.effective_output_codec == "mp3":
            # Re-encoded to a bitrate we asked for.
            return float(self.settings.bitrate)
        if self.stats.forwarded_seconds > 0 and self.stats.forwarded_bytes > 0:
            # Forwarded: bytes produced over the audio duration they carry, both
            # counted from the stream itself. This is the rate the radio's
            # decoder consumes at, so it is the rate to release at.
            return self.stats.forwarded_bytes * 8.0 / self.stats.forwarded_seconds
        if self.stats.seconds_in > 0 and self.stats.bytes_out > 0:
            # The first moments of a forwarded stream, before a whole segment has
            # been measured. Downloaded bytes over declared duration is the
            # roughest of the three, which is why it is only the last resort.
            return self.stats.bytes_out * 8.0 / self.stats.seconds_in
        return None

    def _note_input(self, segment: Segment, data: bytes) -> None:
        """Account for one downloaded segment.

        All five fetch sites go through here so that ``segments``, ``bytes_in``
        and ``seconds_in`` cannot disagree with each other - they are used
        together to derive the stream's bitrate, and a site that forgot one of
        them would skew it silently.

        It is also the single place the now-playing title is read: every fetch
        site sees the whole segment exactly once, and none of them has to care
        what metadata the station happens to carry.
        """
        self.stats.segments += 1
        self.stats.bytes_in += len(data)
        self.throughput.add_in(len(data))
        self.stats.seconds_in += segment.duration or 0.0
        title = id3_song_title(data)
        if title:
            # None means "no title in this segment" (between songs, or a station
            # without metadata) - keep showing the last real one.
            self.stats.now_playing = title

    async def aclose(self) -> None:
        self._closed = True
        await self._kill_child()
        await self._client.aclose()

    async def stream(self) -> AsyncGenerator[bytes, None]:
        """Yield output bytes until cancelled or every source has failed."""
        failures = 0
        consecutive_fatal = 0
        backoff = self.settings.restart_backoff
        #: The reason the last source gave up, kept so the final error can say it.
        #: "all 1 source URL(s) failed" is true, unhelpful, and - for the failures
        #: that are decided rather than suffered, such as a stream that needs
        #: disabled FFmpeg - it throws away the only sentence anybody can act on.
        last_fatal: Optional[str] = None

        while not self._closed:
            url = self.current_url
            try:
                async for chunk in self._stream_once(url):
                    failures = 0
                    backoff = self.settings.restart_backoff
                    yield chunk
                if self._closed:
                    return
                # A finite (VOD) playlist ended: loop it, which is what a radio
                # pointed at an on-demand item expects.
                log.info("playlist exhausted; restarting the stream")
                await asyncio.sleep(0.5)
                continue
            except asyncio.CancelledError:
                raise
            except (PipelineFatal, HLSFatal) as exc:
                consecutive_fatal += 1
                last_fatal = str(exc)
                self.stats.failures += 1
                # No traceback on purpose: an upstream 404 or a non-HLS URL is
                # an expected event, and we are about to try another source.
                log.error("source is unusable: %s", exc)  # noqa: TRY400
            except (PipelineError, HLSError, OSError, asyncio.TimeoutError) as exc:
                failures += 1
                self.stats.failures += 1
                log.warning("stream problem (%d in a row): %s", failures, exc)
            except Exception:  # never let one bad stream stop the service
                failures += 1
                self.stats.failures += 1
                log.exception("unexpected pipeline error")

            if self._closed:
                return

            if consecutive_fatal >= len(self.sources):
                reason = f": {last_fatal}" if last_fatal else ""
                raise PipelineFatal(
                    f"all {len(self.sources)} source URL(s) failed; last tried {url}{reason}"
                )
            if failures >= self.settings.failover_after:
                failures = 0
                self._advance_source()
                log.warning("failing over to %s", self.current_url)

            await asyncio.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2, 30.0)

    # ---------------------------------------------------------------- internals
    def _advance_source(self) -> None:
        self.stats.source_index = (self.stats.source_index + 1) % len(self.sources)
        self.stats.failovers += 1

    async def _kill_child(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        kill_child_process(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:  # pragma: no cover - a truly stuck child
            # Deliberately no traceback: this is a self-explanatory condition.
            log.error("ffmpeg would not exit; leaving it to the OS")  # noqa: TRY400

    async def _stream_once(self, url: str) -> AsyncGenerator[bytes, None]:
        """Run one source until it fails or ends, yielding output bytes."""
        tracker = PlaylistTracker(self._client, url, self.settings)
        info = await tracker.prime()

        batch = tracker.take_buffered()
        init_segments = [s for s in batch if s.is_init]
        media = [s for s in batch if not s.is_init]
        if not media:
            raise PipelineError("playlist has no media segments")

        #: Work items; ``None`` as the payload means "still to be downloaded".
        pending: List[WorkItem] = [(segment, None) for segment in media]

        #: How much audio this start-up burst is about to hand downstream, from
        #: the playlist's own EXTINF. It is the depth of the audio every
        #: listener will be given *before* the pipeline settles onto the live
        #: edge, and the session needs it: without it the reserve is trimmed
        #: to `preroll_seconds` while the radio is still working through the
        #: burst, and its next connection is served from the reserve's oldest
        #: piece instead of from where it stopped - heard as a jump forward of
        #: however much older the burst is than the reserve.
        self.stats.startup_seconds = sum((segment.duration or 0.0) for segment in media)

        # Fetch the first segment before anything else, because its bytes are the
        # only reliable answer to "what are we reading?". They are needed either
        # way - the transcode downloads ``pending[0]`` regardless - so this moves
        # the download earlier rather than adding one.
        #
        # The playlist's own ``container`` is a guess derived from the URL, and
        # this project shipped a bug from trusting it: for an `.aac` segment
        # (raw ADTS AAC behind an ID3v2 tag) the guess said "ts", so ffmpeg was
        # started with ``-f mpegts`` on data it could not read. It then emitted
        # nothing at all, while the HTTP response had already been sent as
        # ``200 audio/mpeg`` - a radio sees a successful connection and silence.
        first_data = await self._client.fetch_segment(media[0])
        self._note_input(media[0], first_data)
        pending[0] = (media[0], first_data)

        detected = classify_segment(first_data)
        input_format = detected or info.container

        mode = MODE_TRANSCODE
        kind: Optional[str] = None
        if self.settings.prefer_passthrough or self.settings.passthrough_only:
            mode, kind = decide_mode(first_data, self.settings)

        if mode == MODE_TRANSCODE and self.settings.passthrough_only:
            # FFmpeg is disabled and this stream needs it, so there is nothing to
            # try and no point retrying: say why, in a message a radio owner can
            # act on, and end the session rather than emit silence behind a
            # `200 audio/...` (the failure mode this project has shipped before).
            raise PipelineFatal(
                "this station has to be re-encoded, and FFmpeg is disabled on this "
                f"installation ({_why_it_needs_ffmpeg(detected, kind)}). Enable it "
                "again (remove --no-ffmpeg, or set ffmpeg_enabled = true) to play "
                "this station, or use one whose audio can be forwarded as-is."
            )

        self.stats.mode = mode
        self.stats.detected_kind = kind
        if mode == MODE_PASSTHROUGH and detected in ("mp3", "adts"):
            # A forwarded elementary stream *is* the output, so the format is
            # known before a single byte is produced. That matters: the
            # listener's frame grid and the ICY grid are both derived from
            # `detected_codec`, and they have to be right from byte one.
            self.stats.detected_codec = detected
        if mode == MODE_PASSTHROUGH:
            log.info(
                "pass-through engine: %s %s forwarded without ffmpeg "
                "(no re-encode, no child process)",
                "bare" if detected in ("mp3", "adts") else "MPEG-TS",
                kind,
            )
        else:
            if self.settings.effective_output_codec == "adts":
                target = (
                    "ADTS (copy, no re-encode)" if kind == "adts" else "ADTS (re-encoded to AAC)"
                )
            else:
                target = f"MP3 {self.settings.bitrate // 1000} kbit/s"
            log.info(
                "ffmpeg engine: converting to %s (input %s, source kind=%s)",
                target,
                input_format,
                kind or "unknown",
            )

        if mode == MODE_PASSTHROUGH:
            # Pass-through is only ever chosen for a container that was
            # recognised, so this is the classifier's ``None`` living in the
            # type rather than a case that can happen.
            container = "ts"
            if detected in {"adts", "mp3"}:
                container = detected
            iterator = self._run_passthrough(tracker, pending, container=container)
        else:
            iterator = self._run_transcode(tracker, input_format, pending, init_segments)

        async for chunk in iterator:
            self.stats.bytes_out += len(chunk)
            self.stats.last_data_at = time.time()
            yield chunk

    # ------------------------------------------------------------ pump helper
    async def _pump(self, feeder: Feeder) -> AsyncGenerator[bytes, None]:
        """Run ``feeder(queue)`` concurrently and yield what it produces.

        The feeder runs as a *sibling* task, so a failure inside it surfaces
        immediately instead of waiting for the stall timeout, and a stall is
        detected as "no bytes produced for N seconds".
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=max(4, self.settings.queue_chunks))
        task = asyncio.create_task(feeder(queue), name=f"radioconv-feed:{self.label}")
        try:
            while True:
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {getter, task},
                    timeout=self.settings.stall_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    getter.cancel()
                    raise PipelineError(
                        f"no audio for {self.settings.stall_timeout}s while producing the stream"
                    )
                if task in done and getter not in done:
                    getter.cancel()
                    if task.cancelled():
                        break
                    error = task.exception()
                    if error is not None:
                        raise error
                    break
                chunk = getter.result()
                if chunk is None:
                    break
                yield chunk
        finally:
            if not task.done():
                task.cancel()
            # ``gather(..., return_exceptions=True)`` never raises, and unlike
            # suppressing CancelledError it still propagates *our* cancellation.
            await asyncio.gather(task, return_exceptions=True)
            await self._kill_child()

    # ---------------------------------------------------------- passthrough
    async def _run_passthrough(
        self, tracker: PlaylistTracker, pending: List[WorkItem], container: str = "ts"
    ) -> AsyncGenerator[bytes, None]:
        """Forward the source's audio with no FFmpeg, from either source shape.

        ``container`` is what the first segment's bytes were classified as. A
        transport stream has to be demuxed; a bare elementary stream
        (``"adts"``/``"mp3"``) only needs its ID3 tags skipped and its frames
        kept whole. Both expose the same three members - ``feed``, ``ready``
        and ``output_kind`` - so the loop below is identical for each, and
        neither can start a child process.
        """
        if container == "ts":
            demux = TSDemuxer(prefer_passthrough=True)
        else:
            demux = ElementaryStreamForwarder(container)

        async def feeder(queue: asyncio.Queue) -> None:
            async def emit(segment: Segment, data: bytes) -> None:
                plain = demux.feed(data)
                if plain:
                    # The format is recorded *before* the audio is queued, not
                    # after the backlog has been fed: the listener's first
                    # bytes need to know what they are, because the frame
                    # grid they are aligned to depends on it.
                    self.stats.detected_codec = demux.output_kind or self.stats.detected_codec
                    # Both halves of the pacing rate come from the audio that is
                    # actually being forwarded, and the duration is counted from
                    # the frame headers rather than taken from EXTINF - see
                    # `output_bitrate`. Counting is one header walk per segment
                    # (~250 frames), i.e. free.
                    self.stats.forwarded_bytes += len(plain)
                    self.stats.forwarded_seconds += audio_duration(
                        plain, self.stats.detected_codec or "adts"
                    )
                    await queue.put(plain)

            # The first couple of segments may not carry PAT/PMT, so the
            # demuxer is not "ready" until it has seen the tables. Readiness is
            # a validation condition, NOT permission to drop the rest: the
            # tracker already advanced past these segments, so deleting them
            # here (as this code used to, ``pending[index + 1:] = []``) made
            # them unrecoverable - with two missing 5s segments a listener
            # jumped forward ~10s at startup. Feed every pending segment in
            # order and only then check that a usable stream was found.
            for segment, data in pending:
                if data is None:
                    data = await self._client.fetch_segment(segment)
                    self._note_input(segment, data)
                await emit(segment, data)
            if not demux.ready:
                raise PipelineError(
                    f"no usable audio stream found in the {container.upper()} segments; "
                    "use ffmpeg instead"
                )

            while not self._closed:
                try:
                    segments = await tracker.poll()
                except Exception as exc:
                    raise PipelineError(f"playlist refresh failed: {exc}") from exc
                if not segments:
                    if tracker.endlist:
                        break
                    await asyncio.sleep(tracker.seconds_until_reload())
                    continue
                for segment in segments:
                    if self._closed:
                        break
                    data = await self._client.fetch_segment(segment)
                    self._note_input(segment, data)
                    await emit(segment, data)
                if self._closed:
                    break
            await queue.put(None)

        async for chunk in self._pump(feeder):
            yield chunk

    # ------------------------------------------------------------- transcode
    async def _run_transcode(
        self,
        tracker: PlaylistTracker,
        input_format: str,
        pending: List[WorkItem],
        init_segments: Sequence[Segment],
    ) -> AsyncGenerator[bytes, None]:
        # No ``copy_audio`` here: a stream reaches this method only when its
        # source is *not* what the output codec asks for. The case where it is -
        # AAC asked for while the station sends AAC - is forwarded by
        # ``_run_passthrough`` with no child process at all, so there is nothing
        # left for ``-acodec copy`` to do. See `ffmpeg.build_command`.
        command = ffmpeg_mod.build_command(self.settings, input_format)
        self.stats.detected_codec = (
            "aac" if self.settings.effective_output_codec == "adts" else "mp3"
        )

        async def feeder(queue: asyncio.Queue) -> None:
            proc = await self._spawn(command)
            stderr_lines: Deque[str] = deque(maxlen=25)
            reader = asyncio.create_task(self._read_stdout(proc, queue))
            errors = asyncio.create_task(self._drain_stderr(proc, stderr_lines))
            try:
                # The EXT-X-MAP init segment must reach ffmpeg first, otherwise
                # fMP4 input cannot be decoded at all.
                for segment in init_segments:
                    data = await self._client.fetch_segment(segment)
                    self._note_input(segment, data)
                    await self._write_stdin(proc, data)

                backlog = list(pending)
                while not self._closed:
                    if backlog:
                        for segment, data in backlog:
                            if data is None:
                                data = await self._client.fetch_segment(segment)
                                self._note_input(segment, data)
                            await self._write_stdin(proc, data)
                        backlog = []
                        continue
                    if proc.returncode is not None:
                        break
                    try:
                        segments = await tracker.poll()
                    except Exception as exc:
                        raise PipelineError(f"playlist refresh failed: {exc}") from exc
                    if not segments:
                        if tracker.endlist:
                            break
                        await asyncio.sleep(tracker.seconds_until_reload())
                        continue
                    backlog.extend((segment, None) for segment in segments)

                await self._close_stdin(proc)
                try:
                    code = await asyncio.wait_for(proc.wait(), timeout=20)
                except asyncio.TimeoutError:
                    code = -9
                # The reader is finished or wedged; the exit code below is the
                # authoritative signal either way.
                with contextlib.suppress(asyncio.TimeoutError, PipelineError):
                    await asyncio.wait_for(reader, timeout=10)
                if code not in (0, -15, -9) and not self._closed:
                    detail = " | ".join(list(stderr_lines)[-3:]) or "no diagnostics"
                    raise PipelineError(f"ffmpeg exited with code {code}: {detail}")
            finally:
                for job in (reader, errors):
                    if not job.done():
                        job.cancel()
                await asyncio.gather(reader, errors, return_exceptions=True)
                # The stdin pipe is closed *before* the child is killed, and in
                # every case: the exception path (ffmpeg died, the playlist
                # failed) never reaches the `_close_stdin` call above, and that
                # is exactly the case where the pipe breaks and asyncio is left
                # holding an unretrieved BrokenPipeError. See `_close_stdin`.
                await self._close_stdin(proc)
                await self._kill_child()
            await queue.put(None)

        async for chunk in self._pump(feeder):
            yield chunk

    # ------------------------------------------------------------ subprocess
    async def _spawn(self, command: Sequence[str]) -> asyncio.subprocess.Process:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Own session => the whole group can be killed, and a Ctrl-C on
                # the service cannot leak the child. POSIX only; Windows accepts
                # and ignores this, hence the fallback in kill_child_process().
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise PipelineFatal(f"ffmpeg is not available: {exc}") from exc
        self._proc = proc
        log.debug("started ffmpeg pid=%s: %s", proc.pid, " ".join(command))
        return proc

    async def _write_stdin(self, proc: asyncio.subprocess.Process, data: bytes) -> None:
        if not data:
            return
        if proc.stdin is None or proc.returncode is not None:
            raise PipelineError("ffmpeg is no longer accepting input")
        try:
            proc.stdin.write(data)
            # A bounded drain is what turns "ffmpeg stopped reading" from a
            # hung pipeline into a recoverable error.
            await asyncio.wait_for(proc.stdin.drain(), timeout=self.settings.stall_timeout)
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise PipelineError("ffmpeg closed its input") from exc
        except asyncio.TimeoutError as exc:
            raise PipelineError("ffmpeg stopped reading its input") from exc

    async def _close_stdin(self, proc: asyncio.subprocess.Process) -> None:
        """Close ffmpeg's input pipe and *collect* whatever it says.

        ``close()`` alone is not enough, and the reason is worth writing down
        because the symptom is very far from the cause.

        asyncio's subprocess stdin is a ``StreamWriter`` whose protocol holds a
        ``_stdin_closed`` future. When the pipe breaks, asyncio puts the
        ``BrokenPipeError`` on that future - and the only thing that ever
        retrieves it is ``StreamWriter.close()``, which awaits the future
        through ``_get_close_waiter()``. If the writer is never closed, the
        future keeps an exception nobody asked for, and asyncio reports it
        when the future is garbage collected:

            Future exception was never retrieved
            future: <Future finished exception=BrokenPipeError(32, 'Broken pipe')>

        That message is not about the pipe, it is about *our* future being
        dropped on the floor, and it turns a routine "ffmpeg exited" into
        something that looks like a crash in the test output. So the close
        waiter is awaited here and its exception is consumed.

        Calling this twice is harmless: the second call finds the transport
        already closed and the future already resolved.
        """
        if proc.stdin is None:
            return
        # Closing a pipe whose process has already died is expected to fail.
        with contextlib.suppress(Exception):
            proc.stdin.close()
        with contextlib.suppress(Exception):
            await proc.stdin.wait_closed()

    async def _read_stdout(self, proc: asyncio.subprocess.Process, queue: asyncio.Queue) -> None:
        assert proc.stdout is not None
        chunk_size = self.settings.chunk_size
        while True:
            try:
                data = await asyncio.wait_for(
                    proc.stdout.read(chunk_size), timeout=self.settings.stall_timeout
                )
            except asyncio.TimeoutError as exc:
                raise PipelineError(
                    f"ffmpeg produced no output for {self.settings.stall_timeout}s"
                ) from exc
            if not data:
                return
            # Bounded and awaited: this back-pressure is what stops FFmpeg from
            # buffering the whole station in RAM on a 512 MB Pi.
            await queue.put(data)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process, sink: Deque[str]) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            sink.append(text)
            lowered = text.lower()
            if "error" in lowered or "invalid" in lowered or "no such" in lowered:
                log.warning("ffmpeg: %s", text)
            else:
                log.debug("ffmpeg: %s", text)
