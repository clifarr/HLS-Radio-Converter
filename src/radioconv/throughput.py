"""Byte counters, and how much moved in the last hour.

The dashboard has to answer two questions that a running total cannot:

* how much audio has actually reached the radios *recently*?
* is anything being received from the station right now?

A cumulative counter answers neither. ``bytes_out_total`` grows all day long and
starts again from zero on every restart, so "audio sent" on a page is either a
number that never moves or one that drops to zero - and neither says whether the
last hour was quiet or busy.

So the totals stay cumulative - that is what ``rate()`` in Prometheus wants, and
a counter must never go down - and a rolling one-hour view is *derived* from them
by remembering where the totals stood an hour ago.

Why the samples are taken on a timer
------------------------------------
The obvious cheap implementation is to record a point whenever somebody reads the
numbers. That is wrong in a way that only shows up on a long-running service: if
nobody looks for an hour there is nothing an hour old to subtract, so the window
falls back to the whole cumulative total and a machine that has been up for three
days reports three days of traffic as "the last hour". Wrong numbers that look
plausible are the failure mode this project keeps having to fix, so the sampler
is an explicit once-a-second task instead (see ``SessionManager``).

What is counted
---------------
``in`` is everything downloaded from the station: HLS playlists and media
segments, encrypted or not. ``out`` is audio handed to a radio's socket - not
audio *produced*. The difference matters: a pipeline stays warm for
``linger_seconds`` after the last radio leaves and keeps downloading and
transcoding while nobody is listening, and those bytes reach no radio at all.
Counting them as "sent" would show traffic on a service that is delivering
nothing.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

#: The rolling window. Kept a constant rather than a setting because the metric
#: names say ``last_hour``: making the window configurable would let the name
#: start lying, and a test pins that this is 3600.
WINDOW_SECONDS = 3600.0

#: How often a point is recorded. One second matches the dashboard's own refresh
#: rate, and it bounds the window's error to a single second of traffic (about
#: 24 KB at 192 kbit/s - invisible next to an hour of it).
SAMPLE_SECONDS = 1.0

#: One recorded point: when, and where the two totals stood then. Taken from
#: ``time.monotonic()`` because this measures a *duration*, and a wall clock that
#: steps (NTP on a Pi with no RTC, which is every Pi without a network at boot)
#: would corrupt the window.
Point = Tuple[float, int, int]


class Throughput:
    """Cumulative byte totals plus a rolling view of them.

    Not thread-safe and not meant to be: everything here runs on the event loop,
    where ``+=`` between awaits cannot be interrupted.
    """

    def __init__(self, window: float = WINDOW_SECONDS) -> None:
        self.in_total = 0
        self.out_total = 0
        self._window = window
        #: Points inside the window, oldest first.
        self._points: Deque[Point] = deque()
        #: The newest point *before* the window - the baseline the window is
        #: measured from. Held separately rather than left as the deque's oldest
        #: entry, so the invariant is stated instead of emerging: the deque can
        #: be pruned to nothing while the anchor stays, which is the whole reason
        #: a long-running service does not report its lifetime as the last hour.
        self._anchor: Optional[Point] = None

    # ------------------------------------------------------------- accounting
    def add_in(self, count: int) -> None:
        """Record *count* bytes downloaded from upstream."""
        self.in_total += count

    def add_out(self, count: int) -> None:
        """Record *count* bytes handed to a radio."""
        self.out_total += count

    # ---------------------------------------------------------------- window
    def sample(self, now: Optional[float] = None) -> None:
        """Record where the totals are, at most once per :data:`SAMPLE_SECONDS`.

        Bounded in frequency so that calling it from a fast loop (or from several
        readers) cannot flood the deque, and pruned so it cannot grow without
        limit: a point older than the window is dropped, except the single one
        needed to subtract from.
        """
        now = time.monotonic() if now is None else now
        if self._points and now - self._points[-1][0] < SAMPLE_SECONDS:
            return

        self._points.append((now, self.in_total, self.out_total))
        self._prune(now)

    def _prune(self, now: float) -> None:
        """Move everything older than the window into the anchor.

        Each point below the cutoff is discarded *after* being remembered as
        :attr:`_anchor`, so the newest one before the window always survives -
        as a single pair of totals rather than as a stretch of history. Without
        it there is nothing to subtract from and the window silently falls back
        to the whole cumulative total, so on a long-running service
        ``bytes_out_last_hour`` would climb forever.
        """
        cutoff = now - self._window
        while self._points and self._points[0][0] < cutoff:
            self._anchor = self._points.popleft()

    def _baseline(self, cutoff: float) -> Point:
        """The totals as they stood at *cutoff*, or zeros if we are younger.

        Both sources are consulted: the anchor, and any point still in the deque
        that the cutoff has since overtaken (possible when the window is read
        without a sample in between).
        """
        base: Point = (cutoff, 0, 0)
        if self._anchor is not None and self._anchor[0] <= cutoff:
            base = self._anchor
        for point in self._points:
            if point[0] > cutoff:
                break
            base = point
        return base

    def as_dict(self, now: Optional[float] = None) -> Dict[str, float]:
        """The window, plus the raw totals it is derived from.

        Keys match the metric names in :mod:`radioconv.metrics` exactly, so the
        mapping there is a lookup rather than a translation - a rename on one
        side cannot quietly leave the other publishing a stale name.
        """
        now = time.monotonic() if now is None else now
        _, base_in, base_out = self._baseline(now - self._window)
        return {
            "bytes_in_total": self.in_total,
            "bytes_out_total": self.out_total,
            "bytes_in_last_hour": max(0, self.in_total - base_in),
            "bytes_out_last_hour": max(0, self.out_total - base_out),
            "window_seconds": self._window,
            "samples": len(self._points),
        }
