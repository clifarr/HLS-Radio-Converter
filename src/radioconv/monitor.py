"""Process-level watchdog.

The original symptom was "the Raspberry Pi freezes after a few days". Two very
different failure modes hide behind that sentence:

1. *The service wedges.* Handled inside the pipeline and session layers
   (timeouts, bounded queues, per-stream restarts) - a wedged station never
   takes the process down.
2. *The process itself degrades.* A slow memory leak, or the event loop
   blocked by something entirely outside our control (a pathological DNS
   lookup, a driver stall on a cheap SD card, thermal throttling).

This module covers the second case. It samples RSS and, more importantly, the
event loop's *lag* - the difference between when a wake-up was scheduled and
when it actually ran. If the loop is no longer keeping up, the honest thing to
do is die cleanly and let systemd start a fresh process, rather than sit there
silently serving nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class HealthSnapshot:
    started_at: float = field(default_factory=time.time)
    rss_bytes: int = 0
    cpu_percent: float = 0.0
    loop_lag: float = 0.0
    max_loop_lag: float = 0.0
    checks: int = 0
    unhealthy: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "uptime": round(time.time() - self.started_at, 1),
            "rss_bytes": self.rss_bytes,
            "rss_mb": round(self.rss_bytes / (1024 * 1024), 1),
            "cpu_percent": round(self.cpu_percent, 1),
            "loop_lag": round(self.loop_lag, 3),
            "max_loop_lag": round(self.max_loop_lag, 3),
            "checks": self.checks,
            "unhealthy": self.unhealthy,
        }


class ResourceMonitor:
    """Samples resources, logs status and restarts the process when unhealthy."""

    def __init__(self, settings: Settings, extra: Optional[Callable[[], dict]] = None) -> None:
        self.settings = settings
        self.extra = extra
        self.health = HealthSnapshot()
        self._task: Optional[asyncio.Task] = None
        self._last_log = time.time()
        self._consecutive_unhealthy = 0
        #: ffmpeg children, keyed by pid, kept between samples so that
        #: ``cpu_percent`` keeps a usable baseline (see ``_tree_usage``).
        self._child_processes: Dict[int, Any] = {}
        try:
            import psutil

            self._process = psutil.Process(os.getpid())
            self._process.cpu_percent(None)  # prime the counter
        except Exception as exc:  # noqa: BLE001 - psutil error classes vary by version
            # psutil is optional in effect: without it we simply stop reporting
            # resource usage, which must not stop the service from starting.
            log.debug("psutil unavailable for monitoring: %s", exc)
            self._process = None

    # ------------------------------------------------------------------ public
    async def start(self) -> None:
        if self._task is None:
            # Take one sample up front. The first *scheduled* sample is
            # ``resource_sample_interval`` (1s) away, and until it happens
            # ``/health`` reports zeros - which reads as a broken service to an
            # operator, and to any script sizing a machine from ``rss_mb``.
            # Memory is accurate immediately; CPU needs a second sample to have
            # an interval to average over, which is inherent to ``cpu_percent``.
            self._sample()
            self._task = asyncio.create_task(self._run(), name="radioconv-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            # gather(..., return_exceptions=True) never raises, and unlike
            # suppressing CancelledError it still propagates *our* cancellation.
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    # --------------------------------------------------------------- internals
    async def _run(self) -> None:
        """Sample resources often; decide whether to restart rarely.

        These are two different jobs on two different clocks, and conflating them
        was a visible bug. ``memory_check_interval`` answers "has this been bad
        for long enough to act on?" - deliberately slow, so one stalled moment
        never triggers a restart. But sampling at that same rate also meant the
        CPU and memory figures were up to 30 seconds old, so a dashboard
        refreshing every second painted the same number over and over. Reported
        as "CPU usage seems to only refresh once every 10 seconds or so", and
        correct: nothing was broken, the reading was simply never taken.

        Measuring a process tree is a couple of syscalls, so the sampling runs at
        ``resource_sample_interval`` (1s, matching the browser) while the watchdog
        keeps its own slower clock.
        """
        sample_interval = max(0.2, self.settings.resource_sample_interval)
        watchdog_interval = max(5.0, self.settings.memory_check_interval)
        next_watchdog = time.monotonic() + watchdog_interval
        #: Worst lag seen since the last watchdog tick. A stall is judged on the
        #: whole period rather than on whichever single sample the tick happened
        #: to land next to, so a 5-second freeze cannot slip between two ticks.
        worst_lag = 0.0

        while True:
            expected = time.monotonic() + sample_interval
            await asyncio.sleep(sample_interval)
            lag = max(0.0, time.monotonic() - expected)
            self.health.loop_lag = lag
            self.health.max_loop_lag = max(self.health.max_loop_lag, lag)
            worst_lag = max(worst_lag, lag)
            self._sample()

            if time.monotonic() >= next_watchdog:
                next_watchdog = time.monotonic() + watchdog_interval
                self._watchdog_tick(worst_lag)
                worst_lag = 0.0

    def _watchdog_tick(self, worst_lag: float) -> None:
        """The slow half of the loop: judge, and restart only if it persists."""
        if worst_lag > 5.0:
            log.warning("event loop is lagging by %.1fs", worst_lag)
            self._consecutive_unhealthy += 1
            self.health.unhealthy += 1
        else:
            self._consecutive_unhealthy = 0

        self._check_memory()

        if self._consecutive_unhealthy >= 3:
            self._restart_process(f"event loop stalled for {self._consecutive_unhealthy} checks")

        if time.time() - self._last_log >= 900:
            self._last_log = time.time()
            self._log_status()

    def _sample(self) -> None:
        self.health.checks += 1
        process = self._process
        if process is None:
            return
        self._sample_from(process)

    def _sample_from(self, process: Any) -> None:
        """Record the resource use of *process* and its children."""
        try:
            with process.oneshot():
                rss, cpu = self._tree_usage(process)
        except Exception as exc:  # noqa: BLE001 - psutil error classes vary by version
            # Reported at debug only: a single failed sample is not actionable,
            # and this method must never raise inside the monitor loop.
            log.debug("could not sample process resources: %s", exc)
            return
        self.health.rss_bytes = rss
        self.health.cpu_percent = cpu

    def _tree_usage(self, process: Any) -> tuple[int, float]:
        """RSS and CPU for the whole service, *including* its ffmpeg children.

        Measuring only the Python process is badly misleading: transcoding
        happens in child ffmpeg processes, so the parent's own CPU sits near
        zero (observed: 0.5% on a Pi 3 while a stream was actively being
        transcoded) while the cores are genuinely busy. ``/health`` is the number
        an operator sizes a machine with, so it has to cover the whole tree.

        ``cpu_percent`` keeps its baseline *on the Process object*, so the child
        objects are cached by pid: fetching fresh ones every sample would reset
        the baseline and report 0.0 forever.
        """
        rss = process.memory_info().rss
        cpu = process.cpu_percent(None)

        live: Dict[int, Any] = {}
        for child in process.children(recursive=True):
            proc = self._child_processes.get(child.pid, child)
            live[child.pid] = proc
            try:
                rss += proc.memory_info().rss
                cpu += proc.cpu_percent(None)
            except Exception:  # noqa: BLE001 - exited between listing and reading
                live.pop(child.pid, None)
        self._child_processes = live
        return rss, cpu

    def _check_memory(self) -> None:
        limit = self.settings.memory_limit_mb
        if limit <= 0:
            return
        rss_mb = self.health.rss_bytes / (1024 * 1024)
        if rss_mb > limit:
            self._restart_process(f"memory usage {rss_mb:.0f} MB exceeds the {limit} MB limit")

    def _log_status(self) -> None:
        detail = {}
        if self.extra is not None:
            try:
                detail = self.extra()
            except Exception as exc:  # noqa: BLE001 - reporting must not crash the loop
                log.debug("status callback failed: %s", exc)
                detail = {}
        log.info(
            "status: rss=%.0fMB cpu=%.0f%% lag=%.2fs sessions=%s listeners=%s",
            self.health.rss_bytes / (1024 * 1024),
            self.health.cpu_percent,
            self.health.loop_lag,
            detail.get("sessions", "?"),
            detail.get("listeners", "?"),
        )

    def _restart_process(self, reason: str) -> None:
        """Ask systemd/supervisor for a clean restart.

        SIGTERM (rather than ``os._exit``) so that uvicorn shuts down, all
        ffmpeg children are reaped and the socket is closed properly - exactly
        the cleanup a wedged process would otherwise skip.
        """
        log.error("restarting the service: %s", reason)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except (OSError, ProcessLookupError):  # pragma: no cover - signal unavailable
            # No SIGTERM possible: the only remaining option is a hard exit, and
            # a non-zero code is what makes supervisord/systemd restart us.
            raise SystemExit(1) from None
