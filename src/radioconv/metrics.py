"""Every number this service reports, defined once.

Three endpoints expose the same data, for three different readers:

==================  ============================================================
``/metrics.json``   The machine-readable snapshot. The dashboard polls it, and it
                    is the honest answer to "what is going on?" for a script.
``/metrics``        Prometheus text exposition, **generated from** that snapshot.
``/health``         The snapshot plus the FFmpeg report: liveness for a
                    supervisor and the full picture for an operator in one call.
==================  ============================================================

The Prometheus text used to be written out by hand, which meant a new metric
could be added to one view and silently forgotten in the other. It is now
rendered from the same :func:`snapshot` the JSON uses, so the two cannot drift;
``tests/test_web_ui.py`` compares them value by value.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Dict, List

from . import __version__
from .monitor import ResourceMonitor
from .session import SessionManager

#: Prometheus prefix. Metric names below are written without it.
PREFIX = "radioconv_"

#: Gauge name -> HELP text. The order here is the order the exposition is
#: written in, so the output is stable and diffable.
#:
#: A gauge is a value that goes up and down; these are all read straight out of
#: :func:`snapshot`, so adding a line here is all that is needed to publish it.
GAUGES: Dict[str, str] = {
    "up": "1 while the service is answering requests.",
    "uptime_seconds": "Seconds since the service started.",
    "sessions": "Active pipelines, one per distinct station URL.",
    "max_sessions": "Configured limit on concurrent pipelines.",
    "listeners": "Radios currently attached to a pipeline.",
    "silent_sessions": (
        "Sessions whose listeners have had nothing buffered for longer than "
        "stall_timeout - measured from the listener's side, so it means silence a "
        "radio is actually experiencing. These are the failures a radio hears as "
        "silence."
    ),
    "cpu_percent": (
        "CPU used by the service and its FFmpeg children, as a percentage of one core."
    ),
    "rss_bytes": "Resident memory of the service and its FFmpeg children.",
    "loop_lag_seconds": "How late the event loop last ran.",
    # Rolling-window views, so a *gauge* rather than a counter: these go down.
    # The names say `last_hour` and `throughput.WINDOW_SECONDS` is an hour - a
    # test pins the two together, because a window setting that disagreed with
    # the name would be a metric that lies.
    "bytes_in_last_hour": "Bytes downloaded from stations in the last hour.",
    "bytes_out_last_hour": "Audio bytes handed to radios in the last hour.",
}

#: Counter name -> HELP text. Counters only ever increase (until restart).
COUNTERS: Dict[str, str] = {
    "bytes_in_total": "Bytes downloaded from stations since the service started.",
    "bytes_out_total": "Audio bytes handed to radios since the service started.",
    "pipeline_failures_total": "Upstream fetch failures.",
    "pipeline_restarts_total": "Pipeline restarts, whether by failure or by watchdog.",
    "dropped_chunks_total": "Chunks discarded for a listener that fell behind.",
    "capacity_rejections_total": "Requests refused with 503 because the service was full.",
}


def snapshot(manager: SessionManager, monitor: ResourceMonitor) -> Dict[str, Any]:
    """Everything we know about the running service, as one plain dict.

    Deliberately built from :meth:`SessionManager.as_dict` rather than from the
    session objects: the aggregates below and the per-session detail the
    dashboard draws are then guaranteed to come from the same reading, so the
    totals match the rows underneath them.

    ``status`` is ``degraded`` - not ``ok`` - when anything is wrong that a
    liveness probe alone would miss: when every session has died, **or** when any
    session with a listener is producing no audio.

    The second case is the important one. A stream whose segments FFmpeg could
    not read returned ``200 audio/mpeg``, held a session open and fetched its
    segments successfully, while never emitting one byte of audio. Every figure
    here looked healthy - sessions counted, no failures, no restarts - and a
    radio heard nothing. Reporting ``silent_sessions`` and calling that
    ``degraded`` is what makes the difference between a dashboard that is
    accurate and one that is merely green.
    """
    streams = manager.as_dict()
    details = streams["details"]
    throughput = manager.throughput.as_dict()
    uptime = round(time.time() - monitor.health.started_at, 1)

    all_dead = bool(details) and all(d["fatal_error"] for d in details)
    any_silent = any(d["stalled"] for d in details)

    return {
        "status": "degraded" if (all_dead or any_silent) else "ok",
        "version": __version__,
        "uptime": uptime,
        "gauges": {
            "up": 1,
            "uptime_seconds": uptime,
            "sessions": streams["sessions"],
            "max_sessions": streams["max_sessions"],
            "listeners": streams["listeners"],
            "silent_sessions": streams["silent"],
            # Already rounded by the monitor's own presentation, but rounding
            # again here keeps the JSON and the Prometheus text identical.
            "cpu_percent": round(monitor.health.cpu_percent, 1),
            "rss_bytes": monitor.health.rss_bytes,
            "loop_lag_seconds": round(monitor.health.loop_lag, 3),
            "bytes_in_last_hour": throughput["bytes_in_last_hour"],
            "bytes_out_last_hour": throughput["bytes_out_last_hour"],
        },
        "counters": {
            # From the service-wide counters, not from a sum over the live
            # sessions. A session is reaped once its radios have been gone for
            # `linger_seconds`, and summing what is currently in the registry
            # therefore made the reported totals *fall* whenever a station went
            # quiet - a counter that goes down breaks `rate()` and, worse, is
            # simply untrue.
            "bytes_in_total": throughput["bytes_in_total"],
            "bytes_out_total": throughput["bytes_out_total"],
            "pipeline_failures_total": sum(d["pipeline"]["failures"] for d in details),
            "pipeline_restarts_total": sum(d["session"]["restarts"] for d in details),
            "dropped_chunks_total": sum(
                sub["dropped"] for d in details for sub in d["subscribers"]
            ),
            "capacity_rejections_total": streams["capacity_rejections"],
        },
        # The rolling window in full (totals, window length, how many samples it
        # is holding), so a script reading /metrics.json can see what the derived
        # figures were derived *from* rather than trusting them.
        "throughput": throughput,
        # The resource snapshot verbatim: `/health` has always reported it and
        # it carries detail (peak lag, sample count, MB alias) that does not
        # belong in a Prometheus metric name.
        "resources": monitor.health.as_dict(),
        "streams": streams,
    }


def to_prometheus(snap: Mapping[str, Any]) -> str:
    """Render a :func:`snapshot` as Prometheus text exposition (0.0.4).

    Every metric in :data:`GAUGES` / :data:`COUNTERS` gets its ``HELP`` and
    ``TYPE`` line; a value missing from the snapshot is published as ``0``
    rather than skipped, because a series that disappears is worse than a zero
    (``rate()`` and dashboards both handle zero, and neither handles a gap).
    """
    lines: List[str] = []
    for family, kind in ((GAUGES, "gauge"), (COUNTERS, "counter")):
        values = snap["gauges" if kind == "gauge" else "counters"]
        for name, help_text in family.items():
            lines.append(f"# HELP {PREFIX}{name} {help_text}")
            lines.append(f"# TYPE {PREFIX}{name} {kind}")
            lines.append(f"{PREFIX}{name} {_number(values.get(name, 0))}")
    return "\n".join(lines) + "\n"


def _number(value: Any) -> str:
    """Format a value the way the exposition format wants it.

    Everything must be a bare number: ``True`` is not a valid sample, and
    ``0.30000000000000004`` in a metric is embarrassing rather than precise.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)
