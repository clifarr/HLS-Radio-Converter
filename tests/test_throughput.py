"""The rolling hour: how much moved lately, and why it cannot be lazy.

``bytes_out_total`` is a running total, which is what a Prometheus ``rate()``
wants but useless on a dashboard: it either never moves or it drops to zero on
restart. The dashboard instead shows the last 60 minutes, derived by remembering
where the totals stood an hour ago.

Everything here is measured with an injected ``now`` rather than a patched clock,
so the tests say what they mean and cannot flake on timing.
"""

from __future__ import annotations

import pytest

from radioconv import metrics
from radioconv import session as session_mod
from radioconv.config import Settings
from radioconv.monitor import ResourceMonitor
from radioconv.session import SessionManager
from radioconv.throughput import SAMPLE_SECONDS, WINDOW_SECONDS, Throughput


def test_a_young_service_reports_its_whole_total_as_the_last_hour():
    """Below one hour of uptime there is no history to subtract, and none needed."""
    throughput = Throughput()
    throughput.add_in(1000)
    throughput.add_out(2000)
    throughput.sample(now=10.0)

    window = throughput.as_dict(now=11.0)

    assert window["bytes_in_last_hour"] == 1000
    assert window["bytes_out_last_hour"] == 2000
    assert window["bytes_in_total"] == 1000
    assert window["bytes_out_total"] == 2000


def test_traffic_older_than_the_window_falls_out_of_it():
    throughput = Throughput(window=60.0)
    throughput.add_in(500)
    throughput.sample(now=1000.0)

    throughput.add_in(300)
    throughput.sample(now=1061.0)

    window = throughput.as_dict(now=1061.0)
    assert window["bytes_in_total"] == 800, "the counter itself never goes down"
    assert window["bytes_in_last_hour"] == 300, "only what happened inside the window"


def test_the_window_is_correct_after_an_hour_without_a_single_reader():
    """The bug that lazy sampling would have introduced.

    Recording a point only when somebody reads the metrics looks free, and is
    wrong: with nothing an hour old to subtract from, the window falls back to
    the whole cumulative total, so a service up for three days reports three days
    of traffic as the last hour. So the sampler runs on a timer - and this is the
    test that says what that buys.
    """
    window = 100.0
    throughput = Throughput(window=window)

    # Ten minutes of one byte per second, with nobody reading anything.
    for second in range(600):
        throughput.add_in(1)
        throughput.add_out(2)
        throughput.sample(now=float(second))

    latest = throughput.as_dict(now=599.0)

    assert latest["bytes_in_total"] == 600
    assert latest["bytes_in_last_hour"] == 100, "must be the last window, not the lifetime"
    assert latest["bytes_out_last_hour"] == 200
    # The deque holds only in-window points; the one before the cutoff lives in
    # the anchor, so memory is bounded by the window and not by uptime.
    assert latest["samples"] <= window / SAMPLE_SECONDS + 1, "the deque must stay bounded"


@pytest.mark.parametrize("offset", [0.0, 0.5, 0.9])
def test_the_window_is_right_at_any_offset_from_the_sample_grid(offset):
    """The sample timer and the reader's clock are unrelated - test that.

    Both run on ``time.monotonic()``, so a read almost never lands exactly on a
    recorded sample. An earlier version of the anchor rule only held when the two
    happened to line up, which meant the window was correct in a test that read
    at a whole second and could be wrong for a real request that did not.
    Everything here is measured at an offset from the sample grid for that reason.
    """
    window = 100.0
    throughput = Throughput(window=window)
    for second in range(600):
        throughput.add_in(1)
        throughput.sample(now=float(second))

    assert throughput.as_dict(now=599.0 + offset)["bytes_in_last_hour"] == 100


def test_a_read_long_after_the_last_sample_still_uses_the_anchor():
    """The sampler could have stalled; the window must not turn into a lifetime."""
    window = 100.0
    throughput = Throughput(window=window)
    for second in range(600):
        throughput.add_in(1)
        throughput.sample(now=float(second))

    # 50 seconds after the last recorded sample, so the cutoff has moved well past
    # anything the sampler wrote. The anchor is what makes this answerable: every
    # byte from the cutoff onwards is still accounted for.
    assert throughput.as_dict(now=649.0)["bytes_in_last_hour"] == 50


def test_one_point_before_the_cutoff_is_always_kept():
    """The prune rule. Without the retained anchor there is nothing to subtract."""
    throughput = Throughput(window=10.0)
    for second in range(50):
        throughput.add_in(1)
        throughput.sample(now=float(second))

    # 50 samples over a 10-second window: pruned hard, but the last point before
    # the cutoff must have been kept as the anchor for the subtraction to be
    # possible at all.
    assert throughput._anchor is not None, "the anchor was discarded with the history"
    cutoff = 49.0 - 10.0
    assert throughput._anchor[0] < cutoff < 49.0
    assert throughput.as_dict(now=49.0)["bytes_in_last_hour"] == 10


def test_samples_are_rate_limited():
    """Sampling on every read must not flood the deque."""
    throughput = Throughput()
    throughput.add_in(1)

    throughput.sample(now=100.0)
    throughput.sample(now=100.0 + SAMPLE_SECONDS / 2)
    assert throughput.as_dict(now=100.0)["samples"] == 1

    throughput.sample(now=100.0 + SAMPLE_SECONDS)
    assert throughput.as_dict(now=101.0)["samples"] == 2


def test_a_counter_that_went_backwards_cannot_produce_a_negative_window():
    """Defensive: a negative byte count is nonsense, and nonsense in a gauge is
    worse than a zero because it renders as a plausible small number."""
    throughput = Throughput(window=1.0)
    throughput.add_in(100)
    throughput.sample(now=0.0)
    # Simulate the anchor being *ahead* of the total, which cannot happen through
    # add_in but could through a future refactor.
    throughput._points[0] = (0.0, 500, 500)

    window = throughput.as_dict(now=2.0)
    assert window["bytes_in_last_hour"] == 0


def test_the_window_setting_and_the_metric_names_agree():
    """``*_last_hour`` is a promise the window has to keep.

    Now that the window is a constant rather than a setting, the only way these
    can disagree is if somebody renames one and not the other - so this pins both
    ends. A metric whose name says "hour" and whose window is anything else is
    the exact class of confidently wrong number this project keeps fixing.
    """
    assert WINDOW_SECONDS == 3600.0
    for name in ("bytes_in_last_hour", "bytes_out_last_hour"):
        assert name in metrics.GAUGES, f"{name} is missing from the gauges"
        assert "_total" not in name, "a rolling window goes down, so it is not a counter"


def test_the_service_publishes_both_the_totals_and_the_window():
    """The counter and the gauge must describe the same two quantities."""
    for name in ("bytes_in", "bytes_out"):
        assert f"{name}_total" in metrics.COUNTERS
        assert f"{name}_last_hour" in metrics.GAUGES


async def test_the_sampler_records_points_on_its_own():
    """The window depends on a task nobody calls. Prove the task runs.

    This is the guard for the design decision that matters most here: the points
    are recorded on a timer rather than when somebody reads the metrics. A
    silently dead sampler would not break anything visibly - the totals would
    still be right and the gauges would still be present - it would just make the
    window drift into "everything ever", which is the failure this whole module
    exists to prevent. Nothing else in the suite would notice.
    """
    import asyncio
    import time

    manager = SessionManager(Settings())
    await manager.start()
    try:
        deadline = time.time() + 5.0
        while manager.throughput.as_dict()["samples"] < 1 and time.time() < deadline:
            await asyncio.sleep(0.1)
        waited = manager.throughput.as_dict()
    finally:
        await manager.aclose()

    assert waited["samples"] >= 1, "no sampling task is running; the window cannot work"
    assert waited["samples"] < 5, "points are being recorded far faster than once a second"


@pytest.mark.parametrize("family", ["in", "out"])
def test_the_window_never_exceeds_the_total(family):
    throughput = Throughput(window=5.0)
    for second in range(20):
        getattr(throughput, f"add_{family}")(10)
        throughput.sample(now=float(second))

    window = throughput.as_dict(now=19.0)
    assert window[f"bytes_{family}_last_hour"] <= window[f"bytes_{family}_total"]


def test_the_totals_do_not_fall_when_a_session_is_reaped():
    """A counter that goes down is not a counter.

    The totals used to be summed over the sessions currently in the registry,
    which meant they *dropped* whenever a station went quiet: after
    ``linger_seconds`` with no listeners the session is retired and its bytes
    left the sum. That breaks ``rate()`` and, more simply, is untrue - the bytes
    were sent.

    They come from the monotonic store now, so losing a session cannot move them.
    """
    settings = Settings()
    manager = SessionManager(settings)
    monitor = ResourceMonitor(settings)
    manager.throughput.add_in(5000)
    manager.throughput.add_out(4000)

    before = metrics.snapshot(manager, monitor)["counters"]
    assert before["bytes_in_total"] == 5000

    # Everything at once: the reaper retiring the last idle session, and a
    # restart of the whole registry.
    session = session_mod.StreamSession(settings, "k", ["http://x/a.m3u8"])
    session.stats.bytes_out = 999_999
    manager.sessions["k"] = session
    manager._forget(session)

    after = metrics.snapshot(manager, monitor)["counters"]
    assert after["bytes_in_total"] == 5000, "losing a session must not lose received bytes"
    assert after["bytes_out_total"] == 4000
    assert after["bytes_out_total"] != 999_999, (
        "the counter must not be derived from what is currently in the registry"
    )
