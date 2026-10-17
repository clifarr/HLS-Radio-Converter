"""Resource accounting: the numbers ``/health`` reports must mean something.

Transcoding happens in *child* ffmpeg processes, so measuring only the Python
process reported ~0.5% CPU on a Raspberry Pi 3 while a stream was actively being
transcoded. That figure is what the README tells operators to size a machine
with, so under-reporting it by two orders of magnitude is a real bug.

The subtle part is psutil's ``cpu_percent`` baseline: it lives on the Process
*object*, so a fresh object per sample reports 0.0 forever. The children are
therefore cached by pid, and that behaviour is pinned below.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any, List

import pytest

from radioconv.config import Settings
from radioconv.monitor import ResourceMonitor


class _FakeProc:
    """Duck-typed stand-in for ``psutil.Process``."""

    def __init__(self, pid: int, rss: int, cpu: float, kids: List[Any] | None = None):
        self.pid = pid
        self._rss = rss
        self._cpu = cpu
        self._kids = kids or []
        self.cpu_calls = 0

    def memory_info(self):
        return SimpleNamespace(rss=self._rss)

    def cpu_percent(self, interval=None):
        self.cpu_calls += 1
        return self._cpu

    def children(self, recursive=False):
        return list(self._kids)

    def oneshot(self):
        return contextlib.nullcontext()


def _monitor() -> ResourceMonitor:
    return ResourceMonitor(Settings())


# ------------------------------------------------------------------ summation
def test_tree_usage_sums_parent_and_children():
    child = _FakeProc(pid=2, rss=100, cpu=30.0)
    parent = _FakeProc(pid=1, rss=50, cpu=1.0, kids=[child])

    rss, cpu = _monitor()._tree_usage(parent)

    assert rss == 150, "ffmpeg's memory is part of the service footprint"
    assert cpu == 31.0, "ffmpeg does the transcoding, so its CPU must count"


def test_tree_usage_with_no_children_is_just_the_parent():
    parent = _FakeProc(pid=1, rss=50, cpu=1.0)
    assert _monitor()._tree_usage(parent) == (50, 1.0)


def test_nested_children_are_included(monkeypatch):
    """``children(recursive=True)`` flattens the tree; trust but verify."""
    grandchild = _FakeProc(pid=3, rss=20, cpu=5.0)
    child = _FakeProc(pid=2, rss=100, cpu=30.0, kids=[grandchild])
    parent = _FakeProc(pid=1, rss=50, cpu=1.0, kids=[child, grandchild])

    rss, cpu = _monitor()._tree_usage(parent)

    assert rss == 170
    assert cpu == 36.0


# -------------------------------------------------------------------- caching
def test_child_objects_are_reused_so_the_cpu_baseline_survives():
    """psutil keeps the cpu_percent baseline on the object, not the pid."""
    child = _FakeProc(pid=2, rss=100, cpu=30.0)
    parent = _FakeProc(pid=1, rss=50, cpu=1.0, kids=[child])
    monitor = _monitor()

    monitor._tree_usage(parent)

    # psutil hands back brand-new Process objects on every children() call.
    replacement = _FakeProc(pid=2, rss=100, cpu=30.0)
    parent._kids = [replacement]
    monitor._tree_usage(parent)

    assert monitor._child_processes[2] is child, "the cached object must be reused"
    assert replacement.cpu_calls == 0, "a fresh object has no baseline and reports 0.0"
    assert child.cpu_calls == 2, "the cached object accumulates a real average"


def test_exited_children_are_forgotten():
    parent = _FakeProc(pid=1, rss=50, cpu=1.0, kids=[_FakeProc(pid=2, rss=10, cpu=5.0)])
    monitor = _monitor()

    monitor._tree_usage(parent)
    assert set(monitor._child_processes) == {2}

    parent._kids = []
    monitor._tree_usage(parent)
    assert monitor._child_processes == {}, "a dead child must not be remembered"


def test_a_child_that_vanishes_mid_read_is_skipped():
    class _Gone(_FakeProc):
        def memory_info(self):
            raise OSError("No such process")

    parent = _FakeProc(pid=1, rss=50, cpu=1.0, kids=[_Gone(pid=2, rss=0, cpu=0.0)])
    monitor = _monitor()

    rss, cpu = monitor._tree_usage(parent)  # must not raise

    assert (rss, cpu) == (50, 1.0)
    assert monitor._child_processes == {}


# ----------------------------------------------------------------- sampling
def test_sample_records_the_tree_totals():
    child = _FakeProc(pid=2, rss=1_048_576, cpu=42.0)
    parent = _FakeProc(pid=1, rss=2_097_152, cpu=1.0, kids=[child])
    monitor = _monitor()
    monitor._process = parent  # ty: ignore[invalid-assignment]

    monitor._sample()  # the real entry point, not the helper

    assert monitor.health.checks == 1
    assert monitor.health.rss_bytes == 3_145_728
    assert monitor.health.cpu_percent == 43.0


def test_sampling_without_psutil_is_a_noop():
    """psutil is optional in effect: its absence must not stop the service."""
    monitor = _monitor()
    monitor._process = None

    monitor._sample()

    assert monitor.health.checks == 1
    assert monitor.health.rss_bytes == 0


async def test_first_sample_happens_at_startup():
    """``/health`` must not report zeros for the first sampling interval.

    The scheduled interval defaults to 30s, so without priming, ``rss_mb`` reads
    0.0 for the first half-minute - which looks like a broken service, and is
    exactly what made a benchmark here record 0.0 MB while a stream was live.
    """
    monitor = _monitor()
    monitor._process = _FakeProc(pid=1, rss=5_000_000, cpu=0.0)  # ty: ignore[invalid-assignment]
    assert monitor.health.checks == 0

    await monitor.start()
    try:
        assert monitor.health.checks == 1, "a sample should be taken immediately"
        assert monitor.health.rss_bytes == 5_000_000
    finally:
        await monitor.stop()


# -------------------------------------------------------------------- cadence
async def test_resources_are_sampled_faster_than_the_watchdog_acts():
    """The dashboard's CPU figure must move between polls.

    Sampling used to run on ``memory_check_interval``, which is 30s by default -
    an interval that exists to make *restart* decisions slow, so that one bad
    reading never kills a working service. Using it as the sampling rate too
    meant a page refreshing once a second redrew the same number thirty times,
    which reads as a dead gauge rather than a slow one.

    The two cadences are separate now, and this is the test that says so: with the
    watchdog at its 5s floor and sampling at 0.2s, well under a second of running
    must produce several samples and no restart decision at all.
    """
    import asyncio

    monitor = ResourceMonitor(Settings(resource_sample_interval=0.2, memory_check_interval=5.0))
    monitor._process = _FakeProc(pid=1, rss=1_000_000, cpu=1.0)  # ty: ignore[invalid-assignment]

    await monitor.start()
    try:
        await asyncio.sleep(0.7)
    finally:
        await monitor.stop()

    assert monitor.health.checks >= 2, (
        f"only {monitor.health.checks} samples in 0.7s at a 0.2s interval - "
        "the sampling is still tied to the watchdog clock"
    )
    assert monitor.health.unhealthy == 0, "the watchdog must not have judged anything yet"


def test_a_stall_is_judged_on_the_whole_watchdog_period(monkeypatch):
    """A 5-second freeze must not slip between two slow ticks.

    The tick used to judge the single ``loop_lag`` reading taken next to it, so a
    stall that began and ended between two ticks was invisible. The worst lag seen
    across the period is what the tick is given now.
    """
    monitor = _monitor()
    restarts = []
    monkeypatch.setattr(monitor, "_restart_process", restarts.append)

    # One tick with a bad period, one with a good one: not yet unhealthy.
    monitor._watchdog_tick(9.0)
    assert monitor._consecutive_unhealthy == 1
    monitor._watchdog_tick(0.0)
    assert monitor._consecutive_unhealthy == 0, "a good period must clear the count"

    # Three bad periods in a row: now it is sustained, and it restarts.
    for _ in range(3):
        monitor._watchdog_tick(9.0)
    assert len(restarts) == 1, "a sustained stall must restart the process"


def test_the_sample_interval_has_a_floor():
    """It is a wake-up rate on a Pi, not a styling knob."""
    with pytest.raises(ValueError):
        Settings(resource_sample_interval=0.01)
    with pytest.raises(ValueError):
        Settings(resource_sample_interval=120.0)
    assert Settings(resource_sample_interval=1.0).resource_sample_interval == 1.0
