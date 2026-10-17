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
import os
import signal

import pytest

from radioconv import pipeline


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
