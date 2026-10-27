"""Child-process teardown, and the one place the code is not platform-neutral.

An unkilled ffmpeg is how the original version lost a core and then the whole
machine, so this path matters more than its size suggests. It is also the only
place in the application that used POSIX-only APIs:

* ``os.killpg`` and ``os.getpgid`` do not exist on Windows;
* ``signal.SIGKILL`` does not exist on Windows either (only SIGTERM/SIGABRT/…);
* ``start_new_session=True`` is accepted and then *ignored* on Windows, so there
  is no process group to signal in the first place.

Accessing any of those raises ``AttributeError``, which the old code did not
catch - so on Windows it would have thrown *before* reaching its ``proc.kill()``
fallback and leaked the child.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys

import pytest
from pydantic import ValidationError

from radioconv import pipeline
from radioconv.config import Settings
from radioconv.hls import Segment
from radioconv.pipeline import WorkItem
from radioconv.throughput import Throughput
from radioconv.tsdemux import build_test_ts


class _FakeProcess:
    """Minimal stand-in: the helper only needs ``pid`` and ``kill()``."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.killed = False

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def fake_group(monkeypatch):
    """Install a recording ``killpg`` and a working ``getpgid``."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(pipeline, "_CAN_KILL_PROCESS_GROUP", True)
    monkeypatch.setattr(pipeline.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    monkeypatch.setattr(pipeline.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(pipeline.signal, "SIGKILL", 9)
    return calls


# ------------------------------------------------------------------ POSIX path
def test_kills_the_whole_process_group(fake_group):
    proc = _FakeProcess()
    pipeline.kill_child_process(proc)  # ty: ignore[invalid-argument-type]

    assert fake_group == [(4242, 9)], "should signal the group, not just the child"
    assert not proc.killed, "kill() is the fallback; the group kill should suffice"


# ------------------------------------------------------------------ Windows
def test_falls_back_to_kill_without_process_groups(monkeypatch):
    """Exactly what happens on Windows: no killpg, no SIGKILL, no group."""
    monkeypatch.setattr(pipeline, "_CAN_KILL_PROCESS_GROUP", False)
    proc = _FakeProcess()

    pipeline.kill_child_process(proc)  # ty: ignore[invalid-argument-type]

    assert proc.killed, "the child must still be killed"


def test_falls_back_when_the_group_kill_fails(monkeypatch):
    """A failed killpg must not leak the child - the old code swallowed it."""
    monkeypatch.setattr(pipeline, "_CAN_KILL_PROCESS_GROUP", True)
    monkeypatch.setattr(pipeline.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(pipeline.signal, "SIGKILL", 9)

    def _boom(*_args):
        raise OSError("No such process")

    monkeypatch.setattr(pipeline.os, "killpg", _boom)
    proc = _FakeProcess()

    pipeline.kill_child_process(proc)  # ty: ignore[invalid-argument-type]

    assert proc.killed, "must not leak the child when killpg fails"


def test_kill_errors_are_swallowed(monkeypatch):
    """Racing with an already-dead process must not raise."""
    monkeypatch.setattr(pipeline, "_CAN_KILL_PROCESS_GROUP", False)

    class _Racy(_FakeProcess):
        def kill(self) -> None:
            raise OSError("No such process")

    pipeline.kill_child_process(_Racy())  # ty: ignore[invalid-argument-type]


# ------------------------------------------------------------- real behaviour
def test_the_capability_flag_is_computed_not_hardcoded():
    """The flag must reflect the running interpreter, not an assumption."""
    assert isinstance(pipeline._CAN_KILL_PROCESS_GROUP, bool)
    expected = hasattr(os, "killpg") and hasattr(os, "getpgid") and hasattr(signal, "SIGKILL")
    assert pipeline._CAN_KILL_PROCESS_GROUP is expected


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
async def test_really_kills_a_running_child():
    """End-to-end on this platform: spawn a real process and reap it."""
    proc = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
    try:
        assert proc.returncode is None
        pipeline.kill_child_process(proc)
        await asyncio.wait_for(proc.wait(), timeout=5)
        assert proc.returncode is not None, "the process should be gone"
    finally:
        if proc.returncode is None:  # pragma: no cover - defensive cleanup
            proc.kill()
            await proc.wait()


# ------------------------------------------------- pass-through startup backlog
class _RecordingClient:
    """Stands in for ``HLSClient``: canned bytes in, fetches recorded."""

    def __init__(self, data: dict) -> None:
        self.data = data
        self.fetched: list = []

    async def fetch_segment(self, segment: Segment) -> bytes:
        self.fetched.append(segment.sequence)
        return self.data[segment.sequence]

    async def aclose(self) -> None:
        return None


class _EndOfPlaylistTracker:
    """``poll()`` finds nothing new and the playlist has ended, so the feeder
    finishes instead of looping - the startup backlog is the whole test."""

    endlist = True

    async def poll(self) -> list:
        return []

    def seconds_until_reload(self) -> float:
        return 0.0


async def test_passthrough_startup_feeds_every_pending_segment():
    """Readiness on the FIRST segment must not delete the rest of the backlog.

    ``take_buffered()`` has already advanced the tracker past the startup
    batch, so the old ``pending[index + 1:] = []`` made those segments
    unrecoverable: with two missing five-second segments a listener jumped
    forward ~10 s at tuning time. Every pending segment must be fetched exactly
    once, fed in order, and reach the output - even though the demuxer reports
    ready after the first.
    """
    settings = Settings(prefer_passthrough=True, output_codec="mp3")
    payloads = [b"SEG%d." % index + bytes([0x41 + index]) * 595 for index in range(3)]
    data = {100 + index: build_test_ts(payload) for index, payload in enumerate(payloads)}

    stream = pipeline.StreamPipeline(settings, ["http://x/one.m3u8"], label="startup")
    # The real client is never used; drop its pool rather than leak it.
    await stream._client.aclose()
    client = _RecordingClient(data)
    # A duck-typed stand-in for HLSClient, as `_Racy` is for a process below.
    stream._client = client  # ty: ignore[invalid-assignment]
    segments = [
        Segment(url=f"http://x/{sequence}.ts", sequence=sequence, duration=1.0)
        for sequence in (100, 101, 102)
    ]
    # Mirrors ``_stream_once``: the first segment is prefetched for detection,
    # the rest are still ``None`` and must be fetched by the feeder.
    pending: list[WorkItem] = [(segments[0], data[100]), (segments[1], None), (segments[2], None)]

    received = bytearray()
    try:
        # The tracker is a duck-typed double: a playlist that has already ended.
        async for chunk in stream._run_passthrough(
            _EndOfPlaylistTracker(),  # ty: ignore[invalid-argument-type]
            pending,
        ):
            received += chunk
    finally:
        await stream.aclose()

    output = bytes(received)
    assert client.fetched == [101, 102], (
        f"each unfetched pending segment exactly once, got {client.fetched}"
    )
    offsets = [output.find(payload) for payload in payloads]
    assert all(offset >= 0 for offset in offsets), (
        f"startup segments lost (offsets {offsets}) - readiness on the first "
        "segment discarded the rest of the backlog"
    )
    assert offsets == sorted(offsets), f"segments emitted out of order: {offsets}"


async def test_the_pacer_is_told_the_audio_rate_not_the_download_rate(monkeypatch):
    """End to end through ``stream()``: the rate the pacer is handed.

    Reported against radiom2o (AAC in MPEG-TS): *"every few seconds, it either
    repeats the last few seconds or skips a few seconds"*. ``output_bitrate`` was
    measured on the **downloaded** bytes, and a transport stream is 20 % bigger
    than the audio inside it (188-byte packets, PAT/PMT, PES headers, stuffing),
    so the pacer released 159 kbit/s for audio that needs 128 kbit/s - 1.25 s of audio every second. The jitter buffer covered
    that for about four segments, then ran dry - and a ~2 s hole per segment makes
    a radio reconnect, which replays ``handoff_overlap_seconds`` on the way back
    in (repeat) or drops the backlog (skip).

    The payload is 40 real ADTS frames (7-byte headers included) wrapped in a
    transport stream, and the declared ``EXTINF`` is wrong on purpose, so both
    wrong answers - the download rate and the playlist's duration - are far from
    the right one and a mix-up cannot pass by luck.
    """
    frames = b"".join(_adts_frame(bytes([0x40 + index]) * 40) for index in range(40))
    segment = build_test_ts(frames)
    settings = Settings(prefer_passthrough=True, output_codec="adts")

    class _Info:
        container = "ts"

    class _Tracker:
        endlist = True
        target_duration = 2.0

        def __init__(self, *_args, **_kwargs) -> None:
            self.info = Segment(url="http://x/100.ts", sequence=100, duration=1.0)

        async def prime(self):
            return _Info()

        def take_buffered(self) -> list:
            return [self.info]

        async def poll(self) -> list:
            return []

        def seconds_until_reload(self) -> float:
            return 0.0

    monkeypatch.setattr(pipeline, "PlaylistTracker", _Tracker)

    stream = pipeline.StreamPipeline(settings, ["http://x/one.m3u8"], label="rate")
    await stream._client.aclose()
    stream._client = _RecordingClient({100: segment})  # ty: ignore[invalid-assignment]

    generator = stream.stream()
    received = 0
    try:
        async for chunk in generator:
            received += len(chunk)
            if received >= len(frames):
                break
    finally:
        await generator.aclose()
        await stream.aclose()

    assert stream.stats.mode == "passthrough"
    assert received >= len(frames), "the frames must reach the output"
    # How deep the start-up burst is, in declared audio: the session sizes the
    # reserve from this so a radio that is still working through the burst can
    # resume where it stopped instead of jumping to the reserve's oldest piece.
    assert stream.stats.startup_seconds == pytest.approx(1.0), (
        "the pipeline did not report its start-up backlog, so the session "
        "cannot keep that audio in the reserve"
    )
    measured = stream.output_bitrate
    assert measured is not None
    # 40 frames at 1024 samples / 48 kHz = 0.8533 s of audio.
    seconds = 40 * 1024 / 48000
    assert stream.stats.forwarded_seconds == pytest.approx(seconds, rel=1e-6)
    assert measured == pytest.approx(len(frames) * 8 / seconds, rel=1e-6)

    download_rate = len(segment) * 8 / seconds
    assert measured < download_rate * 0.8, (
        f"{measured:.0f} bit/s is the download rate ({download_rate:.0f}), not the "
        "audio rate - the pacer would hand the radio audio faster than it plays"
    )


# ------------------------------------------------- ffmpeg turned off entirely
class _OneSegment:
    """``PlaylistTracker`` with a single segment and nothing more to poll."""

    endlist = True
    target_duration = 2.0

    def __init__(self, container: str = "ts", *_args, **_kwargs) -> None:
        self.info = Segment(url="http://x/100.ts", sequence=100, duration=1.0)
        self._container = container

    async def prime(self):
        return type("_Info", (), {"container": self._container})()

    def take_buffered(self) -> list:
        return [self.info]

    async def poll(self) -> list:
        return []

    def seconds_until_reload(self) -> float:
        return 0.0


async def test_an_unplayable_station_is_a_fatal_error_not_a_silent_stream(monkeypatch):
    """With FFmpeg off, a stream it cannot forward must say so and stop.

    The alternative - which this project has shipped before - is a ``200
    audio/mpeg`` response followed by no audio at all: the radio reports a
    successful connection and the user hears silence with no way to tell a dead
    station from a misconfigured server. ``PipelineFatal`` is what stops the
    session retrying for ever, and the message is what tells somebody which
    setting to change.
    """
    fmp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40
    monkeypatch.setattr(pipeline, "PlaylistTracker", _OneSegment)
    # If anything tried to build a command, this would fail - which is the
    # second half of the claim: no child process is even considered.
    monkeypatch.setattr(
        pipeline.ffmpeg_mod,
        "locate_ffmpeg",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ffmpeg was consulted")),
    )

    settings = Settings(ffmpeg_enabled=False)
    stream = pipeline.StreamPipeline(settings, ["http://x/one.m3u8"], label="no-ffmpeg")
    await stream._client.aclose()
    stream._client = _RecordingClient({100: fmp4})  # ty: ignore[invalid-assignment]

    try:
        with pytest.raises(pipeline.PipelineFatal) as caught:
            async for _ in stream.stream():
                pass
    finally:
        await stream.aclose()

    message = str(caught.value)
    assert "FFmpeg is disabled" in message
    assert "fMP4" in message, "the reason must be named, not just the outcome"
    assert "ffmpeg_enabled = true" in message, "the message must say how to fix it"


async def test_with_ffmpeg_disabled_a_forwardable_station_still_plays(monkeypatch):
    """The positive case: the engine that remains must work unchanged.

    A setting that *removes* an engine is easy to get wrong in the direction of
    removing too much, so this drives the whole ``stream()`` path with FFmpeg
    made unloadable. If any part of the pipeline asks for it, the test fails
    with the marker rather than passing by accident.
    """
    frames = b"".join(_adts_frame(bytes([0x40 + index]) * 40) for index in range(20))
    segment = build_test_ts(frames)
    monkeypatch.setattr(pipeline, "PlaylistTracker", _OneSegment)
    monkeypatch.setattr(
        pipeline.ffmpeg_mod,
        "locate_ffmpeg",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ffmpeg was consulted")),
    )

    settings = Settings(ffmpeg_enabled=False, output_codec="mp3")
    stream = pipeline.StreamPipeline(settings, ["http://x/one.m3u8"], label="no-ffmpeg-ok")
    await stream._client.aclose()
    stream._client = _RecordingClient({100: segment})  # ty: ignore[invalid-assignment]

    received = 0
    generator = stream.stream()
    try:
        async for chunk in generator:
            received += len(chunk)
            if received >= len(frames):
                break
    finally:
        await generator.aclose()
        await stream.aclose()

    assert stream.stats.mode == "passthrough", (
        f"expected forwarding with ffmpeg off, got {stream.stats.mode!r}: "
        "the configured output_codec must not force a transcode that cannot happen"
    )
    assert received >= len(frames)


# --------------------------------------------- bare elementary streams (no TS)
def _adts_frame(payload: bytes) -> bytes:
    """One complete ADTS frame: a 7-byte header whose length field matches."""
    length = 7 + len(payload)
    return (
        bytes([0xFF, 0xF1, 0x4C, 0x80 | (length >> 11), (length >> 3) & 0xFF])
        + bytes([((length & 0x07) << 5) | 0x1F, 0xFC])
        + payload
    )


def _id3_tag(payload: bytes) -> bytes:
    size = len(payload)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x04\x00\x00" + syncsafe + payload


def test_a_bare_adts_stream_is_a_passthrough_source():
    """`.aac` segments are raw ADTS, not a transport stream - and need no ffmpeg.

    Pass-through used to be a PAT/PMT lookup, so a station that serves the
    elementary stream directly could never use it: it was re-encoded even when
    the output codec was the source's own. This is the whole point of the raw
    forwarder - forwarding AAC to AAC is a copy, and costs no CPU at all.
    """
    segment = _id3_tag(b"PRIV" + b"\x00" * 40) + b"".join(
        _adts_frame(b"\x40" * 60) for _ in range(3)
    )

    assert pipeline.decide_mode(segment, Settings(output_codec="adts")) == (
        "passthrough",
        "adts",
    )
    assert pipeline.decide_mode(segment, Settings(output_codec="auto")) == ("passthrough", "adts")
    # Asking for MP3 cannot forward AAC: the audio has to be re-encoded. The
    # kind is still reported, because it tells the transcoder what it is given.
    assert pipeline.decide_mode(segment, Settings(output_codec="mp3")) == ("transcode", "adts")


def test_a_bare_mp3_stream_is_a_passthrough_source():
    """The other forwardable shape, and the one old radios actually want."""
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    segment = _id3_tag(b"TIT1" + b"\x00" * 20) + frame * 3

    assert pipeline.decide_mode(segment, Settings(output_codec="mp3")) == ("passthrough", "mp3")
    assert pipeline.decide_mode(segment, Settings(output_codec="adts")) == ("transcode", "mp3")


def test_passthrough_can_still_be_switched_off():
    """`--no-passthrough` must mean exactly that, for raw streams too."""
    segment = _id3_tag(b"PRIV" + b"\x00" * 40) + _adts_frame(b"\x40" * 60) * 3
    settings = Settings(output_codec="adts", prefer_passthrough=False)
    assert pipeline.decide_mode(segment, settings) == ("transcode", None)


def test_the_two_engine_settings_answer_different_questions():
    """`ffmpeg_enabled` is a capability; `prefer_passthrough` is a priority.

    On a settings page they sit next to each other and read like one switch and
    its negation, so this is the table that says otherwise - the same source
    bytes, three configurations, three different answers:

    * with a second engine available, the *preference* decides: the same ADTS
      station is a copy when forwarding is preferred and a re-encode when it is
      not, and nothing about the station changed.
    * with no second engine, the preference has nothing to prefer forwarding
      *over*, so the station is forwarded whichever way `output_codec` is set -
      asking for MP3 cannot conjure a re-encode that is unavailable.
    * "both off" is therefore not a third behaviour but a contradiction: it says
      "never forward" *and* "there is nothing else". The validator refuses it
      rather than leaving a service that answers every request with nothing.
    """
    segment = _id3_tag(b"PRIV" + b"\x00" * 40) + _adts_frame(b"\x40" * 60) * 3

    # A second engine exists, so the preference is the thing that decides. Off,
    # the source is not even inspected - there is nothing left to decide.
    assert pipeline.decide_mode(segment, Settings(output_codec="adts")) == ("passthrough", "adts")
    assert pipeline.decide_mode(
        segment, Settings(output_codec="adts", prefer_passthrough=False)
    ) == (
        "transcode",
        None,
    )

    # No second engine: `output_codec` is translated to "auto" and the source wins.
    only_forward = Settings(ffmpeg_enabled=False, output_codec="mp3")
    assert only_forward.effective_output_codec == "auto"
    assert pipeline.decide_mode(segment, only_forward) == ("passthrough", "adts")

    with pytest.raises(ValidationError):
        Settings(ffmpeg_enabled=False, prefer_passthrough=False)


def test_a_container_we_cannot_forward_still_goes_to_ffmpeg():
    """fMP4 needs a real demuxer; the forwarder only knows bare audio."""
    fmp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40
    assert pipeline.decide_mode(fmp4, Settings(output_codec="adts")) == ("transcode", None)


def test_with_ffmpeg_disabled_the_source_codec_wins_over_the_setting():
    """`--no-ffmpeg` makes every stream behave as `output_codec = "auto"`.

    This is the case the feature exists for: a radio that decodes AAC should not
    cost a re-encode at all. With FFmpeg gone there is nothing to encode *with*,
    so the configured "mp3" must not be allowed to force a transcode that cannot
    happen - the decision has to follow the stream.
    """
    off = Settings(ffmpeg_enabled=False, output_codec="mp3")

    adts = _id3_tag(b"PRIV" + b"\x00" * 40) + _adts_frame(b"\x40" * 60) * 3
    assert pipeline.decide_mode(adts, off) == ("passthrough", "adts"), (
        "an AAC station was sent to the engine that does not exist"
    )

    mp3 = _id3_tag(b"TIT1" + b"\x00" * 20) + (b"\xff\xfb\x90\x64" + b"\x00" * 413) * 3
    assert pipeline.decide_mode(mp3, off) == ("passthrough", "mp3")

    # And what it cannot forward is still reported as needing the engine, which
    # the pipeline turns into a fatal error rather than a silent stream.
    fmp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40
    assert pipeline.decide_mode(fmp4, off) == ("transcode", None)


def test_a_contradictory_configuration_still_forwards_rather_than_going_silent():
    """The decision must not depend on validation having been consulted.

    ``Settings`` refuses ``ffmpeg_enabled = false`` together with
    ``prefer_passthrough = false`` (see ``_check_engine_combination``), but a
    ``model_copy(update=...)`` - which is how per-request controls are applied -
    bypasses validators entirely. A decision that trusted the flag alone would
    then answer TRANSCODE for a station it could have played, and with no engine
    available that is silence behind a 200 for every listener.
    """
    contradictory = Settings(ffmpeg_enabled=False).model_copy(update={"prefer_passthrough": False})

    adts = _id3_tag(b"PRIV" + b"\x00" * 40) + _adts_frame(b"\x40" * 60) * 3
    assert pipeline.decide_mode(adts, contradictory) == ("passthrough", "adts")


def test_a_stream_that_needs_ffmpeg_is_refused_when_it_is_disabled():
    """The failure must be a diagnosis, not silence behind a 200.

    Reported as a whole class of bug in this project's history: a stream that
    could not be produced still answered `200 audio/mpeg` and then emitted
    nothing, so a radio connected successfully and stayed mute. Here the reason
    is known before a single byte is produced, so it is said out loud.
    """
    assert "fMP4" in pipeline._why_it_needs_ffmpeg("mp4", None)
    assert "no MP3 or ADTS AAC" in pipeline._why_it_needs_ffmpeg("ts", None)
    assert "not recognised" in pipeline._why_it_needs_ffmpeg(None, None)
    # Never empty: an unexplained refusal is what sends somebody to the forums.
    for detected, kind in ((None, None), ("ts", None), ("mp4", None), ("ts", "adts")):
        assert pipeline._why_it_needs_ffmpeg(detected, kind).strip()


async def test_a_raw_adts_stream_is_forwarded_without_ffmpeg(monkeypatch):
    """The engine proof, not just the decision: no child process is needed.

    ``locate_ffmpeg`` raising means that if anything in this path tried to
    build a command, the test fails loudly - which is the claim being made:
    this kind of station runs with no FFmpeg installed at all.
    """

    def _boom(*_args, **_kwargs):
        raise AssertionError("a raw ADTS stream must not need ffmpeg")

    monkeypatch.setattr(pipeline.ffmpeg_mod, "locate_ffmpeg", _boom)

    frames = b"".join(_adts_frame(bytes([0x40 + index]) * 60) for index in range(3))
    data = {
        100: _id3_tag(b"PRIV" + b"\x00" * 40) + _id3_tag(b"TEXT" + b"\x00" * 30) + frames,
        101: _id3_tag(b"PRIV" + b"\x00" * 40) + frames,
    }
    settings = Settings(output_codec="adts")
    stream = pipeline.StreamPipeline(settings, ["http://x/one.m3u8"], label="raw")
    await stream._client.aclose()
    stream._client = _RecordingClient(data)  # ty: ignore[invalid-assignment]
    segments = [
        Segment(url=f"http://x/{sequence}.aac", sequence=sequence, duration=3.0)
        for sequence in (100, 101)
    ]
    pending: list[WorkItem] = [(segments[0], data[100]), (segments[1], None)]
    # Mirrors ``_stream_once``: the first segment is fetched *before* the engine
    # is chosen, so it is accounted for there rather than in the feeder.
    stream._note_input(segments[0], data[100])

    received = bytearray()
    try:
        async for chunk in stream._run_passthrough(
            _EndOfPlaylistTracker(),  # ty: ignore[invalid-argument-type]
            pending,
            container="adts",
        ):
            received += chunk
    finally:
        await stream.aclose()

    output = bytes(received)
    assert output == frames + frames, "the source's own frames, and nothing else"
    assert stream.stats.detected_codec == "adts", "the frame grid is chosen from this"
    assert stream.stats.segments == 2, "every segment still goes through _note_input"
    assert stream._proc is None, "pass-through must never leave a child behind"


def _id3_song_segment(json_text: str) -> bytes:
    """A leading ID3v2.4 tag with one TEXT frame, as msvdn segments start."""
    payload = b"\x03" + json_text.encode()

    def syncsafe(n: int) -> bytes:
        return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])

    frame = b"TEXT" + syncsafe(len(payload)) + b"\x00\x00" + payload
    return b"ID3\x04\x00\x00" + syncsafe(len(frame)) + frame + b"\xff\xf1\x50\x80" + b"\x00" * 32


def test_note_input_reads_the_now_playing_title():
    """`_note_input` is where every fetched segment passes through exactly once,
    which makes it the hook for the now-playing title (ICY StreamTitle)."""
    pipe = pipeline.StreamPipeline.__new__(pipeline.StreamPipeline)
    pipe.stats = pipeline.PipelineStats()
    pipe.throughput = Throughput()

    pipe._note_input(
        Segment(url="http://x/1.aac", sequence=1, duration=3.0),
        _id3_song_segment(
            '{"songInfo":{"present":{"mus_art_name":"Sombr",'
            '"mus_sng_title":"My Body Isn\'t Ready"}}}'
        ),
    )
    assert pipe.stats.now_playing == "Sombr - My Body Isn't Ready"
    assert pipe.stats.as_dict()["now_playing"] == "Sombr - My Body Isn't Ready"

    # A segment with no title must not clear the last known one.
    pipe._note_input(
        Segment(url="http://x/2.aac", sequence=2, duration=3.0),
        b"\xff\xf1" + b"\x00" * 64,
    )
    assert pipe.stats.now_playing == "Sombr - My Body Isn't Ready"


# ------------------------------------------------------- the teardown noise
async def test_a_broken_stdin_pipe_leaves_no_unretrieved_exception():
    """A dead ffmpeg must not leave an exception nobody retrieves.

    When the pipe to ffmpeg breaks, asyncio puts the ``BrokenPipeError`` on
    the ``_stdin_closed`` future that the subprocess protocol holds. The only
    thing that ever retrieves it is ``StreamWriter.close()`` - so if the
    pipeline does not close the writer, the exception sits on that future
    until it is garbage collected, and asyncio then prints:

        Future exception was never retrieved
        future: <Future finished exception=BrokenPipeError(32, 'Broken pipe')>

    That is not a crash, but it is noise that looks like one, and it appears
    at the end of every test run that kills an encoder.

    The test drives the real thing: a child that closes its stdin, so the
    pipe really does break, and then asserts that nothing is left behind.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdin.close()",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    writer = proc.stdin
    assert writer is not None
    # Write more than the pipe buffer holds, so the write is still pending
    # when the reader is gone.
    writer.write(b"x" * (1024 * 1024))
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.drain(), timeout=0.5)
    await proc.wait()

    # The fix: close the writer and collect what it says.
    await pipeline.StreamPipeline._close_stdin(
        pipeline.StreamPipeline.__new__(pipeline.StreamPipeline), proc
    )

    # The future must be resolved AND its exception retrieved. asyncio's
    # ``Future.__del__`` prints "Future exception was never retrieved" when a
    # future is collected while ``_log_traceback`` is still set - that flag is
    # cleared by ``Future.exception()``, which is what awaiting the close
    # waiter does.
    #
    # The flag is checked *without* calling ``.exception()`` here, because
    # calling it would retrieve the exception and hide the very bug this
    # test is about.
    #
    # ``_protocol`` is asyncio's own private attribute; there is no public
    # way to reach the close waiter, and the whole point is to inspect it.
    protocol = proc._protocol  # ty: ignore[unresolved-attribute]
    waiter = protocol._stdin_closed
    assert waiter.done(), "the close waiter was never resolved"
    assert not waiter._log_traceback, (
        "the BrokenPipeError is still unretrieved; asyncio will print "
        "'Future exception was never retrieved' when this future is collected"
    )


async def test_the_close_stdin_helper_is_safe_to_call_twice():
    """`close()` runs on the normal path and in the `finally`, so it runs twice.

    The second call must not raise: the transport is already closed and the
    close waiter is already resolved.

    ``async def`` rather than a sync test wrapping ``asyncio.run``: on
    Python 3.9 ``asyncio.run`` closes and clears the main thread's event
    loop, and every later test that builds an ``asyncio.Lock``
    (``SessionManager.__init__`` does) then dies with "There is no current
    event loop in thread 'MainThread'". Running it as a coroutine means
    pytest-asyncio owns the loop.
    """
    pipe = pipeline.StreamPipeline.__new__(pipeline.StreamPipeline)

    class _Writer:
        def __init__(self) -> None:
            self.closed = 0
            self.waited = 0

        def close(self) -> None:
            self.closed += 1

        async def wait_closed(self) -> None:
            self.waited += 1

    class _Proc:
        def __init__(self) -> None:
            self.stdin = _Writer()

    proc = _Proc()
    # The helper only touches ``proc.stdin``; the stand-in is not a real
    # ``Process``, and it does not need to be.
    await pipe._close_stdin(proc)  # ty: ignore[invalid-argument-type]
    await pipe._close_stdin(proc)  # ty: ignore[invalid-argument-type]

    assert proc.stdin.closed == 2
    assert proc.stdin.waited == 2
