"""The HTTP API.

Two groups of endpoints, and the difference matters:

**They play audio** (a radio points here, and the response *is* the stream -
it never ends on its own, so a client must be prepared to read forever):

``GET  /<percent-encoded-m3u8-url>``
    The original interface, byte-for-byte compatible: radios already configured
    as ``http://pi:8123/https%3A%2F%2F...`` keep working untouched. Hidden from
    the OpenAPI schema because a wildcard path would swamp it.
``GET  /stream?url=...``
    The same thing with a readable query string, plus the ``br`` / ``codec`` /
    ``name`` overrides.

**They answer questions** (request/response, they return and the connection
closes):

``GET  /probe?url=...``
    Dry run: variant chosen, container, real audio codec, and whether the
    FFmpeg-free pass-through path will be used. No stream is started.
``GET  /health``
    Liveness, the whole metrics snapshot, and the FFmpeg report.
``GET  /metrics.json``
    The snapshot alone, as JSON: what the dashboard polls, and what a script
    should read.
``GET  /metrics``
    The same numbers in Prometheus text exposition, generated from that same
    snapshot so the two can never disagree.

**Browser helpers** (for a human with a web browser, not for a radio):

``GET  /``
    An HTML page: paste a stream URL and it shows the percent-encoded URL to
    type into a radio. A plain ``GET`` form, so the page stays HTML and the
    result is bookmarkable.
``GET  /dashboard``
    Live figures, refreshed once a second from ``/metrics.json``.
``GET  /static/...``
    The stylesheet and the script those two pages share.
``GET  /docs``
    FastAPI's generated reference for the endpoints above (not the wildcard).

None of the HTML is written here: the pages are templates under
``radioconv/templates`` rendered by :mod:`radioconv.web`, and the metrics are
built by :mod:`radioconv.metrics`. This module is the routes.

There is no station-list endpoint and no on-disk station repository: the whole
point of this project is that a radio is given one percent-encoded stream URL
and nothing else needs configuring.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, unquote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import __version__, web
from . import hls as hls_mod
from . import metrics as metrics_mod
from .config import Settings
from .monitor import ResourceMonitor
from .session import CapacityError, SessionManager, StreamSession

log = logging.getLogger(__name__)

#: Query parameters that belong to *us* rather than to the upstream stream, and
#: are therefore stripped before the URL is handed to the origin.
#:
#: * ``br`` / ``bitrate`` - output bitrate, e.g. ``br=128k``.
#: * ``codec`` / ``output_codec`` - ``mp3`` or ``aac``/``adts``.
#: * ``name`` - the ICY station name shown on a radio's display. Without it the
#:   display shows the source URL, which is accurate but ugly.
CONTROL_PARAMS = {"br", "bitrate", "codec", "output_codec", "name"}

#: Longest ICY name we will accept. Radios have small displays and the header
#: goes out on every response, so this is bounded deliberately.
MAX_NAME_LENGTH = 120


# --------------------------------------------------------------------- helpers
def decode_target(raw: str, query: str = "") -> Optional[str]:
    """Turn a request path back into the upstream URL it encodes.

    Radios are inconsistent: some percent-encode the whole URL, some leave the
    ``://`` alone, some double-encode, and some pass the query string as a real
    query string instead of encoding its ``?``. All four shapes are accepted.
    """
    candidate = raw.strip()
    for _ in range(2):
        if candidate.startswith(("http://", "https://")):
            break
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded

    if not candidate.startswith(("http://", "https://")):
        return None

    if query and "?" not in candidate:
        extras = [
            (k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k not in CONTROL_PARAMS
        ]
        if extras:
            # A radio that sent the query string unencoded: give it back to the
            # origin, otherwise the playlist URL is missing its auth/token args.
            candidate = candidate + "?" + "&".join(f"{k}={v}" for k, v in extras)
    return candidate


def parse_controls(params: Mapping[str, str]) -> Dict[str, Any]:
    """Read our own query parameter overrides (``br``, ``codec``).

    Takes the query parameters rather than a whole ``Request``: that is all it
    ever needed, and it makes the function trivially testable.
    """
    overrides: Dict[str, Any] = {}
    raw_bitrate = params.get("br") or params.get("bitrate")
    if raw_bitrate:
        digits = "".join(ch for ch in raw_bitrate if ch.isdigit())
        if digits:
            value = int(digits)
            if "k" in raw_bitrate.lower() and value < 1000:
                value *= 1000
            if 8_000 <= value <= 320_000:
                overrides["bitrate"] = value
    codec = params.get("codec") or params.get("output_codec")
    if codec:
        codec = codec.lower()
        if codec in ("mp3", "mpeg"):
            overrides["output_codec"] = "mp3"
        elif codec in ("aac", "adts"):
            overrides["output_codec"] = "adts"
    return overrides


def parse_label(params: Mapping[str, str], fallback: str) -> str:
    """Read the optional ``name`` control parameter for the ICY station name.

    Frontier Silicon and similar radios render ``icy-name`` on their display, so
    without this the screen shows a percent-decoded source URL. ``name`` was
    already listed in :data:`CONTROL_PARAMS` - meaning it was silently stripped
    from the upstream URL - but nothing ever read it, so setting it did nothing.

    Whitespace is collapsed because the value ends up in an HTTP header, and the
    result is length-bounded because displays are small. Control characters are
    dropped rather than escaped: a header cannot carry them, and this value is
    user-supplied on a URL that may be typed into a radio by hand.
    """
    raw = (params.get("name") or "").strip()
    if not raw:
        return fallback
    cleaned = "".join(ch if ch.isprintable() and ch not in "\r\n" else " " for ch in raw)
    cleaned = " ".join(cleaned.split())
    return cleaned[:MAX_NAME_LENGTH] or fallback


# -------------------------------------------------------------------- factory
#: Shown at the top of ``/docs``. The grouping is the point: a reader needs to
#: know immediately that some endpoints stream forever and others return JSON,
#: because those two behave very differently in a client.
API_DESCRIPTION = """
Converts an **HLS** (``.m3u8``) Internet radio stream into a plain, continuous
HTTP audio stream that devices which only understand ``audio/mpeg`` can play.

### Which endpoint do I want?

| Goal | Use |
|---|---|
| **Play a station** (what a radio does) | `GET /<percent-encoded-url>`, or `GET /stream?url=…` |
| **Find out what a stream is** before playing it | `GET /probe?url=…` |
| **Check the service is healthy** | `GET /health` |
| **Read the numbers from a script** | `GET /metrics.json` |
| **Scrape numbers into Prometheus** | `GET /metrics` |
| **Watch it live in a browser** | `GET /dashboard` |
| **Try it by hand in a browser** | `GET /` |

### Things that surprise people

* **Streaming responses never end.** They are live radio; a client must read
  until it decides to stop, or the connection stays open for as long as the
  station plays. There is no `Content-Length` and no final byte.
* **`/probe` starts no stream.** It fetches the playlist, reports what it found
  and returns. Use it to answer "why is this station slow?" without attaching.
* **`GET /` does not stream either.** It is a normal HTML page that shows you a
  URL to copy; playing is a separate link.
* **`/health`, `/metrics.json` and `/metrics` report the same numbers.** They
  differ only in shape: one document for a supervisor, one for a script, one for
  Prometheus. All three are built from a single snapshot.
* **`/dashboard` is a page, not an endpoint.** It is HTML that polls
  `/metrics.json` once a second; the JSON is the interface, the page is a view
  of it.
* **Radio URLs are often percent-encoded, and radios differ.** Some encode the
  whole URL, some leave `://` alone, some double-encode. All the common shapes
  are accepted on the legacy path.
* **One pipeline per station URL, shared by every listener.** Ten radios on one
  station means one decode, not ten; what costs CPU is the number of *distinct*
  URLs.
* **`name`, `br` and `codec` are ours, not the origin's.** They are stripped
  before the URL is requested upstream; everything else in the query string is
  forwarded, because CDNs use query tokens for authorisation.
"""

#: Grouping for ``/docs``. Order here is the order shown.
API_TAGS = [
    {
        "name": "playback",
        "description": "Streams audio. The response is a live, never-ending body.",
    },
    {
        "name": "diagnostics",
        "description": "Request/response endpoints that return JSON and close.",
    },
    {
        "name": "browser",
        "description": "Convenience pages for a human at a web browser, not for a radio.",
    },
]


class _RevalidatingStaticFiles(StaticFiles):
    """Static assets a browser must check before reusing.

    ``StaticFiles`` sends ``last-modified`` and ``etag`` but no
    ``cache-control``, and a response with no explicit policy is cached by
    *heuristic freshness* - in practice about 10% of the time since the file
    was last modified. A script fetched while the file was a day old can
    therefore be reused for hours **without the browser asking**, which is how
    a shipped change to the dashboard kept showing the old column: the server
    was serving the new file the whole time, and the page in front of the user
    was running the previous one.

    ``no-cache`` does not mean "do not cache"; it means "revalidate before
    reusing", and the conditional request that follows costs a 304 with no
    body. The templates also add ``?v=<version>`` to the asset URLs, so an
    upgrade is a new URL as well - this header covers the case where a browser,
    proxy or phone ignores the query string.
    """

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers["cache-control"] = "no-cache"
        return response


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()

    manager = SessionManager(settings)
    monitor = ResourceMonitor(settings, extra=manager.as_dict)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        log.info("radioconv %s starting on %s:%d", __version__, settings.host, settings.port)
        await manager.start()
        await monitor.start()
        try:
            yield
        finally:
            log.info("shutting down")
            await monitor.stop()
            await manager.aclose()

    app = FastAPI(
        title="HLS Radio Converter",
        version=__version__,
        summary="Turn HLS (.m3u8) radio streams into plain MP3/AAC for Internet radios.",
        description=API_DESCRIPTION,
        openapi_tags=API_TAGS,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.manager = manager
    app.state.monitor = monitor

    #: The FFmpeg report, computed once per process. ``ffmpeg_version()`` and
    #: ``encoders()`` each *run the binary*, and ``/health`` exists to be polled
    #: - by a monitor every few seconds, by a person watching the dashboard. On
    #: a Pi, forking FFmpeg twice per poll to re-answer a question whose answer
    #: cannot change while we are running is unacceptable, so the answer is
    #: cached. A monitor is answering "is this install healthy?", and it is: the
    #: binary cannot appear or vanish underneath a running process.
    ffmpeg_report_cache: Dict[str, Any] = {}

    def ffmpeg_report() -> Dict[str, Any]:
        if not ffmpeg_report_cache:
            ffmpeg_report_cache.update(_probe_ffmpeg(settings))
        return ffmpeg_report_cache

    if settings.cors_list:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_list,
            allow_methods=["GET", "HEAD", "OPTIONS"],
            allow_headers=["*"],
        )

    # ------------------------------------------------------------------ assets
    # Mounted before anything else, and in particular before the legacy
    # catch-all at the bottom of this function: Starlette matches routes in
    # registration order, so a /static/... request must be claimed here or
    # `/{encoded_url:path}` would swallow it and answer "not a stream URL".
    app.mount(
        web.STATIC_URL,
        _RevalidatingStaticFiles(directory=str(web.resource_dir(web.STATIC))),
        name="static",
    )

    # ------------------------------------------------------------------ pages
    @app.get(
        "/",
        response_class=HTMLResponse,
        tags=["browser"],
        summary="Test page: turn a stream URL into a radio URL",
        description=(
            "An HTML page for a human with a web browser - not part of the machine "
            "interface, which is `/stream` and the legacy encoded route.\n\n"
            "Paste a `.m3u8` URL into the form (equivalently, pass `?url=`) and the "
            "page shows the exact percent-encoded URL to type into a radio, plus a "
            "link to play it in the browser. It also shows live figures polled from "
            "`/metrics.json`, and links to the diagnostic endpoints.\n\n"
            "The form is a plain `GET`, so **no request body is parsed** and the "
            "result is an ordinary, bookmarkable page - it does not start streaming "
            "in your browser."
        ),
        response_description="An HTML page.",
    )
    async def index(
        request: Request,
        url: str = Query(
            "",
            description="Optional .m3u8 URL to convert into a radio URL.",
            examples=["https://example.com/live/index.m3u8"],
        ),
    ) -> HTMLResponse:
        return web.index_page(settings, request, url)

    @app.get(
        "/dashboard",
        tags=["browser"],
        summary="Live dashboard: what every station is doing right now",
        description=(
            "An HTML page. **It is a view of `/metrics.json`, not an interface of "
            "its own**: the browser polls that endpoint once a second and fills in "
            "the numbers, so a script should read `/metrics.json` directly rather "
            "than parse this page.\n\n"
            "Shows totals (stations, listeners, CPU for the whole process tree, "
            "memory, event loop lag, bytes received and audio sent over the last "
            "hour, dropped chunks, failures, restarts) and a row per station with "
            "its engine, listeners, restarts and idle time. Every figure and every "
            "column heading carries a hover description. Polling stops while the "
            "browser tab is hidden."
        ),
        response_description="An HTML page.",
    )
    async def dashboard(request: Request) -> HTMLResponse:
        return web.dashboard_page(settings, request)

    @app.get(
        "/health",
        tags=["diagnostics"],
        summary="Liveness, resources and every live session",
        description=(
            "Returns immediately; it never touches the network. Wire it into "
            "whatever you already alert on.\n\n"
            "It is the `/metrics.json` snapshot plus the FFmpeg report, so one "
            "call answers both *is it up?* and *what is it doing?*\n\n"
            "`status` is `ok` when the service is healthy - which includes "
            "being idle with no sessions, because nobody listening is not a "
            "fault - and `degraded` when something is wrong that a liveness "
            "probe would miss: every session has died, or a station's "
            "listeners have nothing to play.\n\n"
            "`resources.cpu_percent` and `resources.rss_bytes` cover the **whole "
            "process tree**, including the ffmpeg children that do the "
            "transcoding, so they reflect what the machine is really spending. "
            "`cpu_percent` is a percentage of **one core**, not of the machine: "
            "100 means a single core saturated, so a 4-core Pi has 400 to give. "
            "Both are sampled every `resource_sample_interval` (1s by default), "
            "which is independent of `memory_check_interval` - that slower "
            "interval is when the watchdog may act on them, not how often they "
            "are read."
        ),
        response_description="Service health as JSON.",
    )
    async def health() -> JSONResponse:
        report = metrics_mod.snapshot(manager, monitor)
        report["ffmpeg"] = ffmpeg_report()
        return JSONResponse(report)

    @app.get(
        "/metrics.json",
        tags=["diagnostics"],
        summary="Every live number, as JSON",
        description=(
            "The machine-readable form of `/metrics`, and **the endpoint the "
            "`/dashboard` page polls** once a second - so this is what a script, "
            "a status display or a home-automation rule should read.\n\n"
            "`gauges` go up and down (`sessions`, `listeners`, `cpu_percent`, "
            "`rss_bytes`, `loop_lag_seconds`, `max_sessions`, "
            "`bytes_in_last_hour`, `bytes_out_last_hour`); `counters` only "
            "increase (`bytes_in_total`, `bytes_out_total`, "
            "`pipeline_failures_total`, `pipeline_restarts_total`, "
            "`dropped_chunks_total`, `capacity_rejections_total`). Each name is "
            "the Prometheus metric name with the `radioconv_` prefix removed, so "
            "this endpoint and `/metrics` are the same numbers in two shapes.\n\n"
            "The two `*_last_hour` figures are gauges, not counters, because a "
            "rolling window goes down. `throughput` carries the same numbers "
            "plus the cumulative totals they were derived from and the length of "
            "the window.\n\n"
            "`streams.details` holds one entry per station, with its pipeline, "
            "listeners and counters - the table the dashboard draws."
        ),
        response_description="The metrics snapshot as JSON.",
    )
    async def metrics_json() -> JSONResponse:
        return JSONResponse(
            metrics_mod.snapshot(manager, monitor),
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/metrics",
        tags=["diagnostics"],
        summary="Prometheus metrics",
        description=(
            "Prometheus text exposition format (`text/plain; version=0.0.4`), "
            "**generated from the `/metrics.json` snapshot** rather than written "
            "out separately, so a metric cannot exist in one form and not the "
            "other.\n\n"
            "Gauges: `radioconv_up`, `radioconv_uptime_seconds`, "
            "`radioconv_sessions`, `radioconv_max_sessions`, "
            "`radioconv_listeners`, `radioconv_silent_sessions`, "
            "`radioconv_cpu_percent`, `radioconv_rss_bytes`, "
            "`radioconv_loop_lag_seconds`, `radioconv_bytes_in_last_hour`, "
            "`radioconv_bytes_out_last_hour`.\n\n"
            "Counters: `radioconv_bytes_in_total`, "
            "`radioconv_bytes_out_total`, `radioconv_pipeline_failures_total`, "
            "`radioconv_pipeline_restarts_total`, "
            "`radioconv_dropped_chunks_total`, "
            "`radioconv_capacity_rejections_total`.\n\n"
            "`bytes_in` is everything downloaded from a station. `bytes_out` is "
            "audio handed to a radio's socket - audio produced while nobody was "
            "listening is deliberately excluded."
        ),
        response_description="Metrics in Prometheus text format.",
    )
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            metrics_mod.to_prometheus(metrics_mod.snapshot(manager, monitor)),
            media_type="text/plain; version=0.0.4",
        )

    @app.get(
        "/probe",
        tags=["diagnostics"],
        summary="Inspect a stream without playing it",
        description=(
            "A dry run: fetches the playlist, resolves variants, identifies the "
            "container and the real audio codec, and reports whether the "
            "FFmpeg-free pass-through path can be used.\n\n"
            "**No stream is started and no audio is downloaded** - only the "
            "playlist and enough segment data to identify the codec. Safe to "
            "call on a station a radio is already playing.\n\n"
            "Use it to answer *why is this station slow?* before attaching a "
            "radio, and to check whether a station can use "
            '`output_codec = "adts"` (which avoids re-encoding entirely).\n\n'
            "The CLI equivalent is `radioconv probe <url>`, which prints the same "
            "information as readable text."
        ),
        response_class=JSONResponse,
        response_description=(
            "Stream properties as JSON. Notable keys: `ok`; `is_master`, "
            "`variants_considered`, `variant_bandwidth`, `variant_label` "
            "(variant selection); `container`, `segment_container`, "
            "`segment_count`, `target_duration`, `encrypted`, "
            "`has_init_segment`; `passthrough_possible` and `passthrough_kind` "
            "(whether FFmpeg is needed at all); `ts_streams` (the MPEG-TS "
            "stream types found); and `audio` with `codec`, `profile`, "
            "`sample_rate`, `channels` and `bitrate`."
        ),
        responses={400: {"description": "`url` is not an http(s) URL."}},
    )
    async def probe(
        url: str = Query(
            ...,
            description="The source .m3u8 URL",
            examples=["https://example.com/live/index.m3u8"],
        ),
    ) -> JSONResponse:
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="url must be http(s)")
        result = await hls_mod.inspect(url, settings)
        return JSONResponse(result)

    @app.get(
        "/stream",
        tags=["playback"],
        summary="Stream a station (query-string form)",
        description=(
            "The readable equivalent of `/<percent-encoded-url>`. **Attaches this "
            "request to a live pipeline and streams audio until the client "
            "disconnects.**\n\n"
            "The response is chunked, has no `Content-Length`, and carries ICY "
            "headers (`icy-name`, `icy-br`) which radios display.\n\n"
            "Optional overrides:\n\n"
            "* `br` / `bitrate` - output bitrate, e.g. `br=128k` or `br=128000`. "
            "Accepted range 8000-320000.\n"
            "* `codec` / `output_codec` - `mp3` (default) or `aac`/`adts` to remux "
            "instead of re-encoding.\n"
            "* `name` - the station name shown on the radio's display. Without it "
            "the display shows the source URL.\n\n"
            "If the same `url` is already playing, this joins that pipeline "
            "rather than starting a second one: radios on one station share a "
            "single decode."
        ),
        response_class=StreamingResponse,
        response_description="A continuous `audio/mpeg` (or `audio/aac`) stream.",
        responses={
            400: {"description": "`url` is missing or is not an http(s) URL."},
            503: {
                "description": "At capacity (`max_sessions` distinct stations are already live). Retry after the `Retry-After` header.",
                "headers": {
                    "Retry-After": {
                        "description": "Seconds to wait before retrying.",
                        "schema": {"type": "integer"},
                    }
                },
            },
        },
    )
    async def stream_query(
        request: Request,
        url: str = Query(
            ...,
            description="The source .m3u8 URL",
            examples=["https://example.com/live/index.m3u8"],
        ),
    ) -> Response:
        target = decode_target(url)
        if target is None:
            raise HTTPException(status_code=400, detail="url must be an http(s) .m3u8 URL")
        overrides = parse_controls(request.query_params)
        return await _stream(
            request,
            primary_url=target,
            sources=[target],
            settings=settings,
            manager=manager,
            overrides=overrides,
            label=parse_label(request.query_params, target),
        )

    # ------------------------------------------------------------------ compat
    @app.get("/{encoded_url:path}", include_in_schema=False)
    async def compat(request: Request, encoded_url: str) -> Response:
        """The original ``/<url-encoded-url>`` interface."""
        target = decode_target(encoded_url, request.url.query)
        if target is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "expected /<percent-encoded m3u8 URL>. Use /stream?url=... or see / for help."
                ),
            )
        overrides = parse_controls(request.query_params)
        return await _stream(
            request,
            primary_url=target,
            sources=[target],
            settings=settings,
            manager=manager,
            overrides=overrides,
            label=parse_label(request.query_params, target),
        )

    return app


class _Pacer:
    """Hand a radio its audio at the stream's realtime rate.

    Why this exists
    ---------------
    HLS arrives in *segments*, and a segment is transcoded far faster than
    realtime: FFmpeg turns a 10-second segment into MP3 in about 1.5 seconds.
    Without pacing, a listener gets ten seconds of audio in one burst and then
    silence until the next segment is ready. Measured on a real station: **31 of
    40 consecutive seconds carried zero bytes**, with peaks of 1147 kbit/s into a
    radio that plays 192 kbit/s. A cheap radio has a small input buffer, so it
    underruns in every gap - the audio stops for a second and resumes, over and
    over. That is the "frequent cutouts" a listener hears.

    Version 1 did this by accident: FFmpeg wrote into a pipe that CherryPy copied
    to the socket, so a slow radio's socket back-pressure travelled all the way
    back and throttled FFmpeg to realtime. The v2 rewrite replaced that with a
    bounded queue and per-listener dropping, which removed the pacing altogether
    and introduced the cutouts.

    Why here, and not in the pipeline
    ---------------------------------
    Pacing is per-listener, in the response generator. Sleeping in the producer
    would throttle every *other* listener on the same station, and would stop us
    draining FFmpeg's stdout - which is exactly the back-pressure failure the
    bounded queue was introduced to prevent.

    Why an absolute schedule
    ------------------------
    The bucket is anchored to the connection's start time rather than sleeping
    ``len(chunk) / rate`` after each chunk. A fixed per-chunk sleep cannot catch
    up: if we ever fall behind (a slow start, a stalled upstream, a radio on a
    congested network) the shortfall accumulates forever. Anchoring to an
    absolute schedule means a backlog is delivered at full speed until we are
    back on time, and no further.
    """

    #: Longest single sleep. A cap keeps the loop responsive to cancellation and
    #: to the queue growing; the schedule is absolute, so waking early and
    #: sleeping again lands in exactly the same place.
    MAX_SLEEP = 5.0

    def __init__(self, rate: Callable[[], float], initial_burst: float = 1.5) -> None:
        self._rate = rate
        #: Seconds of audio handed over *ahead of realtime*, and also the most we
        #: will ever run ahead. Used as an initial cushion so a radio can fill
        #: its own buffer, and as the ceiling that stops a starvation gap from
        #: being repaid as a burst - see ``wait``.
        self._burst = max(0.0, initial_burst)
        self._started: Optional[float] = None
        self._emitted = 0
        #: When a chunk was last released to this listener, or ``None`` before
        #: the first. The gap between one release and the next is the only
        #: honest measure of "this listener was given nothing for N seconds":
        #: it spans the queue wait, the socket write and any freeze of the
        #: process, and it excludes the pacer's own deliberate sleep - which is
        #: pacing, not silence. See ``wait``.
        self._last_released: Optional[float] = None
        self.waited = 0

    async def wait(self, size: int) -> float:
        """Block until sending *size* more bytes keeps us on schedule.

        Returns how long this listener has gone **without a chunk**, in seconds:
        the gap since the previous call returned, which covers the queue wait,
        the socket write and any freeze of the process. That figure is what the
        stall watchdog acts on - see ``Settings.stall_reconnect_seconds``.

        Zero on the first call, and zero whenever the loop came straight back.

        Why not "how far behind schedule we are"
        ----------------------------------------
        That was the first attempt, and it is wrong: the schedule is anchored
        ``initial_burst`` *behind* now so the opening chunks can fly out and
        fill a radio's buffer, so the pacer is legitimately "1.4 s behind" for
        as long as it is spending that credit. Every connection would have been
        judged stalled the moment it opened. The test that pinned this
        (``test_the_pacer_reports_the_silence_it_forgave``) failed on the second
        chunk, which is exactly what it was written to catch.
        """
        now = time.monotonic()
        lag = 0.0 if self._last_released is None else max(0.0, now - self._last_released)

        anchoring = self._started is None
        if anchoring:
            # Anchor the schedule *behind* now by the burst, not ahead of it.
            #
            # The burst is a credit: permission to run up to this many seconds
            # ahead of realtime so a radio can fill its own buffer. Anchoring it
            # forward turned that credit into a delay - the very first chunk was
            # held back for the full burst, so a listener waited ~1.5s before
            # receiving anything at all, on top of whatever the pipeline needed to
            # produce it. Measured on a real station: 1.86s to first byte, of
            # which 1.5s was this line.
            self._started = now - self._burst
        self._emitted += size

        # How long this listener went without a chunk, measured *before* the
        # debt below is forgiven. The clamp re-anchors the schedule so a
        # shortfall is never repaid as a burst, and after that no one can tell a
        # 1.5 s silence from a well-behaved stream - so the gap has to be read
        # at the top.
        rate = max(1.0, self._rate())
        target = self._started + self._emitted / rate

        # Nothing to send means the upstream starved, and while it did, `_emitted`
        # stayed still while the clock ran on - so the schedule fell behind by the
        # whole gap. Repaying that as a burst is wrong twice over: it overruns a
        # radio's small buffer, and it *empties* ours, so the next gap is heard
        # too. Measured before this clamp existed: audio for ~4 seconds, then
        # seven seconds of nothing, repeating. So the debt is forgiven and the
        # schedule is re-anchored to now, which keeps the jitter buffer intact.
        if now - target > self._burst:
            self._started = now - self._emitted / rate
            target = now

        # Sleep until the deadline, in capped slices so cancellation and the
        # queue keep being serviced. Sleeping once with ``min(delay,
        # MAX_SLEEP)`` and returning would send early whenever a single delay
        # exceeds the cap - the docstring above promises "waking early and
        # sleeping again lands in exactly the same place", so do that.
        while True:
            delay = target - time.monotonic()
            if delay <= 0:
                # Stamped *after* the sleep, so the pacer's own pacing is never
                # counted as silence: a deliberate 0.34 s wait between chunks is
                # the mechanism working, not a stall.
                self._last_released = time.monotonic()
                return lag
            self.waited += 1
            await asyncio.sleep(min(delay, self.MAX_SLEEP))


#: Bytes of audio between ICY metadata blocks. The heritage Shoutcast grid:
#: the length byte counts *16-byte blocks* of payload - verified against a live
#: Icecast 2.4 capture (SomaFM), where direct-length decoding yields garbage,
#: so this speaks the format every radio firmware has ever seen.
#:
#: The value is a **multiple of the MP3 frame size** (see
#: `icy_metaint_for_frame`). That is not cosmetic: the block lands in the
#: middle of an audio frame otherwise, and a radio has to reassemble a frame
#: the block split in half. See `icy_metaint_for_frame` for the full
#: argument and the measurement that forced it.
ICY_METAINT = 16128


def icy_metaint_for_frame(frame_size: Optional[int], target: int = ICY_METAINT) -> int:
    """The ICY grid size to use for a stream whose frames are *frame_size* bytes.

    Why the grid has to be a multiple of the frame size
    ----------------------------------------------------
    A radio is two state machines over one byte stream: an MP3 decoder
    looking for frame syncs, and an ICY parser counting audio bytes.  The
    ICY block is inserted into the *audio*, so it lands wherever the
    16000-byte count happens to fall.

    For this project's 192 kbit/s, 48 kHz output the frame is 576 bytes:

        16000 / 576 = 27.78 frames

    so 89 % of the blocks land **in the middle of a frame**, and the wire
    reads:

        [ frame 0 .. frame 27, the first 448 bytes ]
        [ 17 bytes of metadata ]
        [ frame 27, the last 128 bytes .. frame 28 ]

    A radio has to strip the metadata and glue those two halves back
    together before its decoder can use the frame.  That is a much harder
    job than it looks: the decoder's input is a *continuous byte stream*,
    so the radio has to feed it the frame's bytes with the metadata
    removed, and the frame's side information (the bit reservoir) refers
    to bytes that were sent in a *previous* frame.  A radio that gets
    this wrong does not fail loudly - it loses frame sync and emits
    noise, and the ICY parser, reading the same stream, reads audio bytes
    as a length byte and fills the display with garbage.  The two
    symptoms the user reports together: bad audio *and* garbled text.

    Why it does not heal
    --------------------
    The offset is constant, so every later block is read at the wrong
    place too.  Only a fresh connection re-anchors the radio, which is
    why a reconnect fixes it and the glitch then never comes back on its
    own.

    The fix
    -------
    Choose a grid that is a whole number of frames, so the block always
    lands on a frame boundary and the radio never has to reassemble
    anything:

        16128 = 28 * 576

    16128 is within 0.8 % of the heritage 16000, is still a multiple of
    16 (so the Shoutcast "length in 16-byte blocks" convention holds),
    and 16000 itself is *not* a multiple of 576, which is the whole
    problem.

    A frame size that is unknown (the stream has not been identified yet)
    or that does not divide into a sensible grid falls back to *target*.
    """
    if not frame_size or frame_size <= 0:
        return target
    # The grid must be a whole number of frames AND a whole number of the
    # Shoutcast 16-byte blocks, so the step is the least common multiple of
    # the two.  For a 576-byte frame that is 576 (already a multiple of 16);
    # for a 417-byte frame it is 6672 (417 * 16), which is 32 frames.
    step = frame_size * 16 // _gcd(frame_size, 16)
    frames = max(1, round(target / step))
    grid = frames * step
    # It must not drift absurdly far from the target: the wire rate changes
    # with it, and the radio is told the rate in `icy-br`.
    if abs(grid - target) > target // 4:
        return target
    return grid


def _gcd(a: int, b: int) -> int:
    """Greatest common divisor."""
    while b:
        a, b = b, a % b
    return a


#: The most payload bytes one ICY block can carry: the length byte counts
#: 16-byte blocks and must fit in a byte, so 15 blocks = 240 bytes.
ICY_MAX_PAYLOAD = 15 * 16

#: The Shoutcast metadata value is a single-quoted string, and a radio ends it
#: at the FIRST quote - exactly as a header parser would. A title containing an
#: apostrophe therefore truncates the value and leaves the rest of the title
#: to be parsed as further key=value pairs, which the radio renders as junk.
#:
#: A real title from a real station: ``Fool's Garden - Lemon Tree`` arrived as
#: ``StreamTitle='Fool's Garden - Lemon Tree';`` and the radio showed
#: ``Fool`` followed by ``s Garden - Lemon Tree';``.
ICY_QUOTE = "'"


def sanitize_icy_title(title: str) -> str:
    """Make *title* safe to put inside a Shoutcast ``StreamTitle='...'`` value.

    Three things can go wrong, and all three were seen on a real station
    (RTL 102.5, whose segments carry per-song ID3):

    * **An apostrophe** ends the value early. It is replaced with U+00B4, the
      Latin-1 ACUTE ACCENT - a look-alike that is one byte, renders on every
      radio, and does not terminate the value. The real title
      ``Fool's Garden - Lemon Tree`` used to arrive as
      ``StreamTitle='Fool's Garden - Lemon Tree';``, and the radio showed
      ``Fool`` followed by ``s Garden - Lemon Tree';``.
    * **Characters outside Latin-1** cannot be encoded at all, because the
      metadata is a byte string and the heritage convention is ISO-8859-1.
      The common ones are transliterated; the rest become ``?``. Characters
      that *are* in Latin-1 - ``£``, ``È`` - are kept exactly, because a
      radio can render them. (The Euro sign is *not* in ISO-8859-1: it
      arrives as U+20AC and becomes ``?``, which is correct for the format.)
    * **A title longer than the block** is cut, and the cut must never land in
      the middle of a multi-byte character.

    The apostrophe rule applies to the **typographic** apostrophes too. A
    title that uses ``’`` (U+2019) is just as likely to be a possessive or a
    contraction - ``It’s a Sin``, ``Rock ’n’ Roll`` - and it must not be
    turned back into an ASCII quote, because an ASCII quote is exactly the
    delimiter this function exists to remove. The typographic quotes are
    mapped to the acute accent directly, and the ASCII-quote replacement is
    applied **after** every other substitution, so no substitution can
    reintroduce it.

    The result is Latin-1 by construction, which is what the metadata format
    is, and every radio firmware can display it.
    """
    if not title:
        return ""
    # 1. The typographic punctuation that is *not* in Latin-1, mapped to
    #    equivalents that are. The curly single quotes become the acute
    #    accent, NOT an ASCII quote: an ASCII quote terminates the value.
    #    Everything else outside Latin-1 is replaced with "?" by the encode
    #    below.
    text = (
        title.replace("‘", "´")
        .replace("’", "´")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
        .replace("…", "...")
        .replace(" ", " ")
    )
    text = text.replace(ICY_QUOTE, "´")
    text = text.encode("latin-1", "replace").decode("latin-1")
    # 3. Control characters would confuse a parser that expects a line.
    return "".join(ch for ch in text if ch >= " " or ch == "\t")


def icy_metadata_block(title: str) -> bytes:
    """One ICY metadata block: length (in 16-byte payload blocks), then payload.

    There is no separate marker byte - the length byte itself (0 = nothing this
    interval) is the whole preamble. Verified against a live Icecast capture:
    reading a marker first desynchronises the grid and every later block decodes
    as garbage.

    The title is sanitised first (see :func:`sanitize_icy_title`): an
    unescaped quote truncates the value, and a non-Latin-1 character cannot be
    rendered. The payload is also cut on a *character* boundary, because the
    240-byte limit can otherwise land in the middle of a multi-byte character
    and leave the radio decoding a broken one.
    """
    text = f"StreamTitle='{sanitize_icy_title(title)}';"
    encoded = text.encode("latin-1", "replace")
    if len(encoded) > ICY_MAX_PAYLOAD:
        # Cut the *title*, not the wrapper, so the block always ends with "';".
        # The room is what is left of the 240-byte payload once the wrapper
        # and the closing "'; " are accounted for.
        room = ICY_MAX_PAYLOAD - len("StreamTitle='';")
        # Cut on a character boundary: a multi-byte character is 2 bytes, and
        # half of one is not a character a radio can decode.
        cut = ""
        used = 0
        for ch in sanitize_icy_title(title):
            width = len(ch.encode("latin-1", "replace"))
            if used + width > room:
                break
            cut += ch
            used += width
        encoded = f"StreamTitle='{cut}';".encode("latin-1", "replace")
    payload = encoded + b"\x00" * (-len(encoded) % 16)
    return bytes([len(payload) // 16]) + payload


async def _insert_icy_metadata(
    chunks: AsyncGenerator[bytes, None],
    metaint: int,
    title: Callable[[], str],
    on_block: Optional[Callable[[int], None]] = None,
) -> AsyncGenerator[bytes, None]:
    """Interleave an ICY metadata block every *metaint* bytes of *chunks*.

    The audio stream reassembles byte-identically when the blocks are removed,
    which is the whole contract: clients count *audio* bytes up to ``metaint``,
    swallow one block, and continue - a block on a wrong offset desynchronises
    the decoder, so the split happens exactly on the grid, never mid-block.
    Blocks carry whatever ``title()`` says *now* (titles change between
    blocks, exactly like Icecast), empty ``StreamTitle=''`` until the source's
    first ID3 tag has been seen.
    """
    since = 0
    async for chunk in chunks:
        offset = 0
        while offset < len(chunk):
            take = min(metaint - since, len(chunk) - offset)
            yield chunk[offset : offset + take]
            since += take
            offset += take
            if since == metaint:
                block = icy_metadata_block(title())
                yield block
                if on_block is not None:
                    # Blocks are bytes the radio reads, so they belong in the
                    # same counters as audio - the metrics describe the socket.
                    on_block(len(block))
                since = 0


def stall_watchdog_due(lag: float, session: StreamSession, settings: Settings, now: float) -> bool:
    """Should this listener's connection be closed after *lag* seconds of silence?

    A Frontier-Silicon radio cannot recover from a gap in the audio it is fed.
    It does have a timeout of its own - freezing this process with ``SIGSTOP``
    for 1.2 s, and separately for 2.0 s, was each followed by the radio
    reconnecting by itself, and the audio came back clean - but that timeout
    only fires when it receives **nothing at all**.

    Microwave interference does not look like that.  TCP keeps retransmitting,
    so bytes keep trickling in and the radio never gives up; what it loses is
    the *rate*, and its decoder starves and resynchronises into audio it
    cannot recover from.  Measured on the wire during the fault: 21
    retransmissions inside one 10-second window against a handful an hour
    otherwise, and an inter-segment gap of 1.492 s.

    The signal this function uses is therefore not "did the radio time out" but
    "how long since this listener was given a chunk".  That covers the socket
    write as well as the queue, so it sees the stall as soon as the radio's
    receive window fills and the write blocks.  Measured stalls that this
    watchdog actually fired on: **1.80 s** and **2.27 s**.

    Closing the connection turns a permanent wedge into the reconnect that
    clears it - which is what removed the "glitched, and then kept glitching"
    behaviour this release exists to fix.

    The cooldown is what makes this safe.  A genuine upstream outage starves
    every listener at once; without it the service would close and reopen every
    connection once a second - the reconnect storm this project has caused
    before (6940 connections in 89 seconds).  The cooldown lives on the session,
    not the subscriber, because that storm is a station-wide event: two
    connections dropping together must not each justify a third.

    Pure on purpose - the caller records the decision, so the arithmetic can be
    tested without a socket, a clock, or a radio.
    """
    if settings.stall_reconnect_seconds <= 0.0:
        return False
    if lag < settings.stall_reconnect_seconds:
        return False
    previous = session.last_stall_reconnect
    if previous is None:
        # Never re-anchored before, and ``None`` rather than 0.0 is what makes
        # that true: a 0.0 sentinel reads as "an event at time zero", which
        # suppresses the first re-anchor until the monotonic clock has passed
        # the cooldown. Invisible on a host up for days, permanent on one that
        # has just booted.
        return True
    return (now - previous) >= settings.stall_reconnect_cooldown_seconds


def _pace_rate(settings: Settings, session: StreamSession) -> float:
    """Bytes per second to release to a listener of *session*.

    Prefers the pipeline's **measured** bitrate, because the correct value
    differs by engine: a re-encode such as AAC to MP3 runs at the bitrate we
    configured, while a remux or the FFmpeg-free forward runs at whatever the
    station's own bitrate is. Pacing a 256 kbit/s forwarding stream at the
    configured 192 kbit/s would make the listener fall permanently behind, which
    the queue resolves by dropping audio - the very symptom being fixed here.

    Falls back to the configured bitrate before anything has been measured. An
    imprecise rate is not harmful in itself: it only mis-times the release, and
    the queue absorbs the difference.
    """
    if not settings.pace_output:
        return float("inf")
    measured = session.pipeline.output_bitrate
    if measured and measured > 0:
        return measured / 8.0
    return max(1.0, settings.bitrate / 8.0)


def _wants_icy_metadata(request: Request) -> bool:
    """Whether *request* asked for in-band Shoutcast metadata.

    The Shoutcast convention is opt-in: the client sends ``Icy-MetaData: 1``
    and the server answers with ``icy-metaint`` and interleaved blocks.  A
    client that did not send the header expects plain audio.

    Measured on the radio (2026-10-21): the same device opens two
    connections with two different User-Agents - ``NSPlayer/8.0.0.3801``,
    which sends ``icy-metadata: 1``, and ``FSL IR2.10``, which does not.
    The server sent the metadata grid to BOTH, so the client that never
    asked for metadata was handed a byte stream with 17-byte blocks
    embedded in it.  It is a different firmware path and it does not handle
    them - which is the reported "garbled title and glitching audio,
    forever": the radio that is glitching is the one that never asked for
    the metadata in the first place.

    A missing header therefore means NO metadata.  That is the conservative
    reading of the protocol and it is what the client asked for by omission.
    """
    raw = request.headers.get("icy-metadata")
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "yes", "true", "on")


async def _stream(
    request: Request,
    *,
    primary_url: str,
    sources: List[str],
    settings: Settings,
    manager: SessionManager,
    overrides: Dict[str, Any],
    label: str = "",
) -> Response:
    """Attach a radio to the session for ``primary_url`` and stream to it."""
    if not primary_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="only http(s) sources are supported")

    session_settings = settings.model_copy(update=overrides) if overrides else settings
    client = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "")[:120]
    # Does this client want in-band metadata?
    #
    # The Shoutcast convention is that a client ASKS for it, with
    # `Icy-MetaData: 1`, and the server then declares `icy-metaint` and
    # interleaves the blocks.  A client that did not ask for it expects
    # a plain audio stream.
    #
    # Measured on the radio (2026-10-21): the same physical device opens
    # TWO connections with TWO different User-Agents - "NSPlayer/8.0.0.3801",
    # which sends `icy-metadata: 1`, and "FSL IR2.10", which does not.  The
    # server sent the metadata grid to BOTH, so the client that never asked
    # for metadata was handed a byte stream with 17-byte blocks embedded in
    # it, and it had to work out what they were.  That is a different radio
    # firmware path (it is a different User-Agent) and it does not handle
    # them.
    #
    # So the metadata is now opt-in **per request**: it goes to the clients
    # that ask for it, and to nobody else.  The setting stays the master
    # switch; this is the second switch, and both have to be on.
    wants_metadata = _wants_icy_metadata(request)

    try:
        session, subscriber = await manager.subscribe(
            primary_url,
            sources,
            settings=session_settings,
            client=client,
            user_agent=user_agent,
            metadata=wants_metadata,
            overrides=overrides,
            # So `/dashboard` names a station the way the radio's display does,
            # instead of showing a percent-decoded URL.
            label=label,
        )
    except CapacityError as exc:
        manager.note_rejection()
        log.warning("refusing %s: %s", client, exc)
        return PlainTextResponse(
            f"busy: {exc}\n",
            status_code=503,
            headers={"Retry-After": "15", "Cache-Control": "no-store"},
        )

    log.info(
        "streaming to %s (%s) via session %s",
        client,
        label or primary_url,
        session.id,
        extra={"session": session.id},
    )

    media_type = _media_type(session_settings, await _settle_kind(session_settings, session))
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "icy-name": (label or "HLS Radio")[:120],
        "icy-description": "HLS Radio Converter",
        "icy-br": str(session_settings.bitrate // 1000),
        "icy-url": "/",
        "Accept-Ranges": "none",
    }
    if session_settings.icy_song_metadata and wants_metadata:
        # The body interleaves metadata blocks on this grid (see
        # `_insert_icy_metadata`); clients only look for them if declared here,
        # and only before the first byte, so this is decided with the headers.
        #
        # The grid is a whole number of AUDIO FRAMES, so a block never lands
        # in the middle of one.  A radio would otherwise have to reassemble
        # the frame the block split, and a radio that does that badly loses
        # frame sync and reads audio as text - which is the reported
        # "garbled title and glitching audio, forever".  The header and the
        # body must agree, so both use this one value.
        metaint = icy_metaint_for_frame(session.frame_size)
        headers["icy-metaint"] = str(metaint)

    async def body() -> AsyncGenerator[bytes, None]:
        idle_limit = max(20.0, session_settings.stall_timeout * 2)
        # Records that this listener is genuinely being served. The reaper drops
        # subscribers that never reach this line, because their `finally` below
        # can never run - a client that disconnects between the request and the
        # body starting would otherwise leave a phantom listener behind forever.
        subscriber.started = True
        # Pacing is per-listener on purpose: see _Pacer. Reading from the queue
        # at full speed and handing the bytes straight to the socket is what
        # causes the cutouts, because a whole segment arrives at once and is
        # followed by silence.
        pacer = _Pacer(
            lambda: _pace_rate(session_settings, session),
            initial_burst=session_settings.initial_burst_seconds,
        )
        try:
            while True:
                try:
                    item = await asyncio.wait_for(subscriber.queue.get(), timeout=idle_limit)
                except asyncio.TimeoutError:
                    log.warning(
                        "listener %s starved for %.0fs; closing the connection",
                        subscriber.id,
                        idle_limit,
                        extra={"session": session.id},
                    )
                    return
                if item is None:
                    return
                offset, chunk = item
                lag = await pacer.wait(len(chunk))
                if stall_watchdog_due(lag, session, session_settings, time.monotonic()):
                    session.last_stall_reconnect = time.monotonic()
                    session.stall_reconnects += 1
                    # Service-wide as well as per-session: the per-session count
                    # dies with the session, and a counter that falls is a lie.
                    manager.note_stall_reconnect()
                    log.warning(
                        "listener %s got %.2fs of silence; closing the "
                        "connection so the radio reconnects and re-anchors "
                        "(threshold %.2fs)",
                        subscriber.id,
                        lag,
                        session_settings.stall_reconnect_seconds,
                        extra={"session": session.id},
                    )
                    return
                subscriber.chunks += 1
                subscriber.bytes_sent += len(chunk)
                # The one place a byte actually reaches a radio. Counted here and
                # not at the pipeline, because the pipeline also produces audio
                # for a listener that has fallen behind (dropped) and while a
                # station is merely being kept warm with nobody listening - and
                # neither of those is "audio sent".
                session.stats.bytes_sent += len(chunk)
                manager.throughput.add_out(len(chunk))
                yield chunk
                # Only what the transport accepted counts as delivered: this is
                # the floor a rejoining connection is primed from (see
                # `StreamSession._join_floor`), and claiming audio the client
                # may never have received would turn a replay into a gap.
                #
                # The floor is the piece's offset in the session's running
                # total of audio, and NOT a timestamp: the reserve is filled
                # in bursts, so a client served from the reserve is
                # `preroll_seconds` behind the production edge in audio while
                # being a fraction of a second behind in wall-clock, and a
                # timestamp could never cut the reserve.  See
                # `Subscriber.reserve_bytes_sent`.
                subscriber.reserve_bytes_sent = offset
        finally:
            elapsed = time.time() - subscriber.connected_at
            log.info(
                "listener %s done after %.0fs (%d KiB)",
                subscriber.id,
                elapsed,
                subscriber.bytes_sent // 1024,
                extra={"session": session.id},
            )
            await manager.unsubscribe(session, subscriber)

    stream: AsyncGenerator[bytes, None] = body()
    if session_settings.icy_song_metadata and wants_metadata:
        # The same grid the header declared - see the comment there.

        def _count_metadata(size: int) -> None:
            # The counters describe the socket, and these are bytes the radio
            # reads just like audio; without this the wire total outruns
            # bytes_out_total by exactly the metadata overhead.
            subscriber.bytes_sent += size
            session.stats.bytes_sent += size
            manager.throughput.add_out(size)

        stream = _insert_icy_metadata(
            stream,
            metaint,
            lambda: session.pipeline.stats.now_playing or "",
            on_block=_count_metadata,
        )
    return StreamingResponse(stream, media_type=media_type, headers=headers, status_code=200)


async def _settle_kind(
    settings: Settings, session: StreamSession, timeout: float = 8.0
) -> Optional[str]:
    """For ``codec=auto`` wait until we know what the source actually is.

    The ``Content-Type`` cannot be changed once the first byte is on the wire,
    so for the auto codec we wait (briefly) for the pipeline to tell us whether
    it is forwarding MP3 or ADTS AAC.
    """
    if settings.output_codec != "auto":
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        kind = session.pipeline.stats.detected_kind
        if kind:
            return kind
        if session.pipeline.stats.mode != "unknown":
            return session.pipeline.stats.detected_kind or "mp3"
        if session.dead:
            return None
        await asyncio.sleep(0.05)
    return None


def _media_type(settings: Settings, kind: Optional[str]) -> str:
    if settings.mime_override:
        return settings.mime_override
    if settings.output_codec == "auto":
        return "audio/aac" if kind == "adts" else "audio/mpeg"
    return settings.mime_type


def _probe_ffmpeg(settings: Settings) -> Dict[str, Any]:
    """Ask FFmpeg what it is and what it can do.

    Costly: ``ffmpeg_version()`` and ``encoders()`` each *run the binary*. See
    ``ffmpeg_report`` inside :func:`create_app`, which calls this once per
    process rather than once per request.
    """
    from . import ffmpeg as ffmpeg_mod

    report: Dict[str, Any] = {"bundled": False, "path": None, "version": None}
    try:
        path = ffmpeg_mod.locate_ffmpeg(settings.ffmpeg_path)
        report["path"] = path
        report["version"] = ffmpeg_mod.ffmpeg_version(settings.ffmpeg_path or "")
        report["bundled"] = "imageio_ffmpeg" in path or "imageio-ffmpeg" in path
        report["libmp3lame"] = "libmp3lame" in ffmpeg_mod.encoders(settings.ffmpeg_path or "")
    except Exception as exc:  # noqa: BLE001 - a health report must never raise
        report["error"] = str(exc)
    return report
