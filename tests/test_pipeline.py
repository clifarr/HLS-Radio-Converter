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
