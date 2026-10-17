"""The browser pages, and the three views of the numbers behind them.

This module guards a specific refactor: the landing page used to be a ~60-line
f-string inside ``api.py`` with its CSS inline and its URLs HTML-escaped by
hand, and the Prometheus exposition used to be written out line by line next to
the JSON *that reported the same numbers*. Both have moved:

* markup and assets to ``templates/`` and ``static/``, rendered by
  :mod:`radioconv.web`;
* the numbers to :mod:`radioconv.metrics`, which builds one snapshot and
  renders it twice - as JSON and as Prometheus text.

So the tests come in three groups: no markup in Python, every asset and every
``data-metric`` name actually resolves, and the JSON and the Prometheus text
agree value for value. All three are drift guards rather than behaviour tests:
each one fails the moment the two halves of a pair stop matching.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

import httpx
import pytest

from radioconv import metrics, web
from radioconv import session as session_mod
from radioconv.config import Settings
from radioconv.monitor import ResourceMonitor
from radioconv.session import SessionManager

#: The package directory, and the repo root two levels above it. Taken from
#: ``web`` rather than from ``__file__`` so this module and the code under test
#: can never disagree about where the package is.
PACKAGE = web.PACKAGE_DIR
PROJECT_ROOT = PACKAGE.parent.parent


# --------------------------------------------------------------------- source
#: Markup that must never appear in a Python module. Not a general "<" check -
#: docstrings legitimately contain things like ``/<url-encoded-url>``.
MARKUP_TOKENS = ("<!doctype", "<html", "<div", "<span", "<script", "<style", "<form", "</p>")


def test_no_html_lives_in_a_python_module():
    """The whole point of the refactor: markup belongs in a markup file.

    A page assembled from Python strings cannot be syntax-highlighted, cannot be
    validated, and - the reason this test exists - gets its escaping wrong the
    moment somebody adds a second attribute. ``templates/`` is the answer, and
    this keeps it the answer.
    """
    modules = sorted(PACKAGE.glob("*.py"))
    # Without this the test passes by finding nothing, which is how a guard
    # quietly stops guarding. (`web.py` itself is the floor.)
    assert len(modules) >= 10, f"expected the package modules, found {len(modules)}"

    offenders = []
    for path in modules:
        text = path.read_text(encoding="utf-8").lower()
        for token in MARKUP_TOKENS:
            if token in text:
                offenders.append(f"{path.name}: contains {token!r}")
    assert not offenders, "markup found outside templates/:\n  " + "\n  ".join(offenders)


def test_the_pages_are_real_template_files():
    for name in ("base.html", "index.html", "dashboard.html"):
        path = web.resource_dir(web.TEMPLATES) / name
        assert path.is_file(), f"{name} is missing from the package"


def test_every_asset_a_template_references_exists():
    """A ``{{ static_url }}/x`` that does not exist is a 404 in the browser.

    The same class of bug as a broken markdown anchor (see
    ``test_metadata.test_docs_have_no_broken_internal_links``), and just as
    invisible until someone opens the page.
    """
    static_dir = web.resource_dir(web.STATIC)
    referenced = set()
    for template in web.resource_dir(web.TEMPLATES).glob("*.html"):
        text = template.read_text(encoding="utf-8")
        for name in re.findall(r"\{\{\s*static_url\s*\}\}/([A-Za-z0-9._-]+)", text):
            referenced.add(name)
            assert (static_dir / name).is_file(), f"{template.name} links to missing asset {name}"

    # And the reverse: every file we ship is one somebody asked for. This is a
    # Raspberry Pi install, so an unreferenced asset is just weight.
    shipped = {p.name for p in static_dir.iterdir() if p.is_file()}
    assert shipped == referenced, f"unreferenced assets: {sorted(shipped - referenced)}"


def test_the_javascript_is_a_static_asset_not_a_template():
    """The script must not be rendered by Jinja.

    If it were, it would need escaping rules of its own and would stop being
    cacheable. The endpoint and the interval arrive as ``data-`` attributes on
    ``<body>`` instead, so this file is a constant.
    """
    script = (web.resource_dir(web.STATIC) / "dashboard.js").read_text(encoding="utf-8")
    assert "{{" not in script and "{%" not in script, "dashboard.js is being templated"
    assert "dataset.metricsUrl" in script and "dataset.refreshMs" in script, (
        "the script must take its endpoint and interval from <body>"
    )


def test_the_javascript_parses():
    """Syntax-check the shipped script, when a JS engine is available.

    Nothing else in this project can catch a typo in it: ruff does not read
    JavaScript, and the browser failure mode is a silently dead dashboard.
    """
    node = shutil.which("node")
    if node is None:
        # ty mis-resolves pytest.skip (see tests/conftest.py).
        pytest.skip("node is not installed")  # ty: ignore[too-many-positional-arguments]
    result = subprocess.run(
        [node, "--check", str(web.resource_dir(web.STATIC) / "dashboard.js")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def code_only(text: str, *, language: str) -> str:
    """Strip comments, so a guard checks the code and not the prose.

    Every guard below is a text search, and a text search cannot tell code from a
    comment. That mattered the moment comments started *explaining why* something
    is forbidden: the note "never innerHTML" and the note "`cursor: help` draws a
    question mark" both tripped the guards that forbid those very things, and the
    honest fix is to make the guards read code, not to make the comments vaguer.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    if language == "javascript":
        text = re.sub(r"//[^\n]*", "", text)
    return text


def test_the_javascript_writes_no_html():
    """Station labels are URLs users typed, so they must never become markup."""
    raw = (web.resource_dir(web.STATIC) / "dashboard.js").read_text(encoding="utf-8")
    script = code_only(raw, language="javascript")
    for forbidden in ("innerHTML", "insertAdjacentHTML", "document.write", "outerHTML"):
        assert forbidden not in script, f"dashboard.js uses {forbidden}"


def test_the_page_does_not_recompute_what_the_server_already_measured():
    """Buffered and Starved must be two readings of one number, not two sums.

    Reported: "the starved column climbs for up to 7s but the buffered column
    still has a large number." Both were derived in the browser, and derived
    differently - the buffer from the *total* chunks across every connection,
    starvation from the *smallest* - so with more than one connection present they
    described different moments and could disagree outright.

    The server now publishes one figure, `buffered_seconds`, computed the same way
    the starvation clock is. The page must read it, and must not reach for the
    summed `queued_chunks`, which remains in the snapshot only as a diagnostic.
    """
    raw = (web.resource_dir(web.STATIC) / "dashboard.js").read_text(encoding="utf-8")
    script = code_only(raw, language="javascript")
    assert "buffered_seconds" in script, (
        "the dashboard no longer reads the server's buffered-seconds figure"
    )
    assert "queued_chunks" not in script, (
        "the dashboard is recomputing the buffer from a chunk count again, which is "
        "the aggregation mismatch that made the two columns contradict each other"
    )


def test_the_packaged_assets_are_declared_for_pyinstaller():
    """``packaging/radioconv.spec`` must ship the directories it now needs.

    A frozen build reads them from ``sys._MEIPASS`` (see ``web.resource_dir``),
    and PyInstaller only puts files there when the spec says so - so a missing
    entry here is a working install that greets its first user with a 500.
    """
    spec = (PROJECT_ROOT / "packaging" / "radioconv.spec").read_text(encoding="utf-8")
    for name in (web.TEMPLATES, web.STATIC):
        assert f"radioconv/{name}" in spec, f"{name}/ is not in the PyInstaller datas"
    assert "jinja2" in spec, "jinja2 must be a hidden import of the frozen build"


def test_resource_dir_finds_both_directories():
    for name in (web.TEMPLATES, web.STATIC):
        assert web.resource_dir(name).is_dir(), f"{name}/ was not found next to web.py"


def test_the_assets_live_inside_the_packaged_directory():
    """They must sit under the path ``pyproject.toml`` declares as the package.

    Hatchling needs no extra configuration to include non-Python files that are
    *inside* the package directory - verified by building: the wheel contains
    ``radioconv/templates/`` and ``radioconv/static/``. Moving either one above
    ``src/radioconv`` would silently stop shipping them, so that is what this
    pins.
    """
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["src/radioconv"]' in pyproject, "the wheel package path changed"

    for name in (web.TEMPLATES, web.STATIC):
        directory = web.resource_dir(name)
        assert directory.parent == PACKAGE, (
            f"{name}/ is at {directory}, outside the packaged directory {PACKAGE}"
        )


# ---------------------------------------------------------------------- pages
def test_dashboard_page_renders_and_does_not_stream(live_server):
    response = httpx.get(live_server + web.DASHBOARD_URL, timeout=10)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "content-length" in response.headers, "a page has a length; a stream does not"
    assert not response.headers.get("icy-br")
    # The hooks the script needs, and a link back to the converter.
    assert 'id="streams"' in response.text
    assert 'id="live-state"' in response.text
    assert 'href="/"' in response.text


def test_the_pages_do_not_advertise_the_json_endpoint(live_server):
    """The pages are for people; the JSON is for scripts and lives in /docs.

    The header's "JSON" link and the "Handy endpoints" list on the landing page
    are both noise for somebody who just wants to know whether their radio is
    working. The endpoint is still *polled* - it has to be - but it must not be
    presented as a destination.
    """
    for path in ("/", web.DASHBOARD_URL):
        body = httpx.get(live_server + path, timeout=10).text
        assert f'href="{web.METRICS_JSON}"' not in body, f"{path} still links to the JSON"
        assert "Handy endpoints" not in body, f"{path} still lists handy endpoints"
        assert "<code>/metrics</code>" not in body, f"{path} still advertises Prometheus"


def test_every_metric_shown_has_a_plain_language_description(live_server):
    """A figure with no explanation is the bug this guards.

    "CPU of one core", "restarts" and "memory" are all ambiguous: a percentage of
    *what*, counted *when*, and *whose* memory. Every one of them therefore
    carries a description. The tile is what carries it, because that is the
    element a pointer is over.
    """
    for path in ("/", web.DASHBOARD_URL):
        body = httpx.get(live_server + path, timeout=10).text
        # `<div class="tile" ... data-tooltip="...">` optionally spanning lines.
        tiles = re.findall(r'<div class="tile"[^>]*?data-tooltip="([^"]*)"[^>]*>', body, re.S)
        opened = re.findall(r'<div class="tile"', body)
        metrics = re.findall(r'data-metric="([A-Za-z0-9_.]+)"', body)
        assert metrics, f"{path} shows no metrics at all"
        assert len(tiles) == len(opened), f"{path} has a tile without a description"
        for description in tiles:
            # Long enough to be an explanation, not a restatement of the label.
            assert len(description) > 40, f"{path} has a stub tooltip: {description!r}"

        # And the table headings on the dashboard carry one too.
        if path == web.DASHBOARD_URL:
            headings = re.findall(r"<th[^>]*data-tooltip=\"([^\"]*)\"", body)
            assert len(headings) >= 10, f"only {len(headings)} column headings have a tooltip"


def test_descriptions_are_not_native_title_tooltips(live_server):
    """A native `title` is not a usable tooltip, so it must not come back.

    It fails twice over: the browser renders its own tooltip after about a
    second's delay, and the `cursor: help` that conventionally signals one is
    available draws a literal question mark - so the page appears to offer a "?"
    and nothing else. `data-tooltip` plus a real element is what replaced it.
    """
    for path in ("/", web.DASHBOARD_URL):
        body = httpx.get(live_server + path, timeout=10).text
        assert 'title="' not in body.replace("data-tooltip=", ""), (
            f"{path} still uses a native title attribute; the tooltip will be "
            "delayed and drawn by the browser"
        )

    css = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.css").read_text(encoding="utf-8"),
        language="css",
    )
    assert "cursor: help" not in css, "cursor: help renders as a question mark cursor"
    assert "#tooltip" in css, "the tooltip element has no styling"

    script = (web.resource_dir(web.STATIC) / "dashboard.js").read_text(encoding="utf-8")
    # The id the stylesheet keys off, and the handlers that make it appear.
    assert 'node.id = "tooltip"' in script, "the tooltip element is not the one the CSS styles"
    for needed in ("data-tooltip", "showTip", "hideTip", "pointerover", "focusin"):
        assert needed in script, f"dashboard.js does not implement {needed}"


def test_every_description_is_reachable_without_a_mouse(live_server):
    """A phone has no hover, and a keyboard has no pointer.

    The tooltip is shown on focus as well as on hover, so every element that
    carries a description must be focusable - otherwise the text exists and can
    never be read on a touch screen, which is how "is my radio working?" is asked
    more often than not.
    """
    for path in ("/", web.DASHBOARD_URL):
        body = httpx.get(live_server + path, timeout=10).text
        for tag in re.findall(r"<(?:div|th)\b[^>]*data-tooltip=[^>]*>", body, re.S):
            assert "tabindex=" in tag, f"{path}: a description is not focusable:\n  {tag[:160]}"


def test_dashboard_is_not_the_legacy_wildcard_route(live_server):
    """``/{encoded_url:path}`` would otherwise claim it and answer 400."""
    response = httpx.get(live_server + web.DASHBOARD_URL, timeout=10)
    assert "expected /<percent-encoded m3u8 URL>" not in response.text


def test_index_page_links_to_the_dashboard(live_server):
    body = httpx.get(live_server + "/", timeout=10).text
    assert f'href="{web.DASHBOARD_URL}"' in body


def test_both_pages_ask_the_script_for_the_same_endpoint(live_server):
    """One endpoint, two views of it - not two endpoints that can disagree."""
    for path in ("/", web.DASHBOARD_URL):
        body = httpx.get(live_server + path, timeout=10).text
        assert f'data-metrics-url="{web.METRICS_JSON}"' in body, f"{path} polls the wrong URL"
        assert re.search(rf'data-refresh-ms="{web.POLL_MS}"', body), f"{path} has no refresh rate"


def test_the_pages_escape_what_the_user_typed(live_server):
    """The form echoes its input back, so it must go out escaped.

    ``?url=`` is attacker-controlled in the sense that it is whatever a browser
    or a bookmark happens to send, and it lands in both an attribute and a path.
    """
    nasty = 'https://example.com/"><script>alert(1)</script>'
    body = httpx.get(live_server + "/", params={"url": nasty}, timeout=10).text
    assert "<script>alert(1)</script>" not in body, "the echoed URL was not escaped"
    assert "&lt;script&gt;" in body, "expected the escaped form of the input"


# -------------------------------------------------------------------- metrics
def test_metrics_json_is_the_snapshot(live_server):
    response = httpx.get(live_server + web.METRICS_JSON, timeout=10)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store", "a live snapshot must not be cached"

    body = response.json()
    assert {"status", "version", "uptime", "gauges", "counters", "resources", "streams"} <= set(
        body
    )
    assert body["status"] in ("ok", "degraded")
    assert set(body["gauges"]) == set(metrics.GAUGES)
    assert set(body["counters"]) == set(metrics.COUNTERS)


def test_health_is_the_snapshot_plus_ffmpeg(live_server):
    """``/health`` gained the flat gauges/counters; it must not have lost or
    restructured anything, because that is what a supervisor already parses."""
    body = httpx.get(live_server + "/health", timeout=10).json()
    assert {"status", "version", "uptime", "resources", "streams", "ffmpeg"} <= set(body)
    assert "details" in body["streams"], "the per-session detail is documented API"
    assert {"sessions", "max_sessions", "listeners"} <= set(body["streams"])


def _published(text: str) -> dict:
    """Parse ``name value`` sample lines out of Prometheus text exposition."""
    samples = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name, _, value = line.partition(" ")
        samples[name] = float(value)
    return samples


def test_prometheus_is_rendered_from_the_snapshot():
    """The exposition must be derived from the snapshot, not maintained beside it.

    This used to be hand-written output living next to ``/health``, so a metric
    could be reported by one and silently missing from the other. Rendering the
    *same* snapshot twice and comparing is what proves the two are one source -
    and it is done as a pure function so no clock can move between the readings
    (comparing two live HTTP responses would flake on ``uptime_seconds``).
    """
    settings = Settings()
    snap = metrics.snapshot(SessionManager(settings), ResourceMonitor(settings))
    samples = _published(metrics.to_prometheus(snap))

    for family, names in (("gauges", metrics.GAUGES), ("counters", metrics.COUNTERS)):
        assert set(names), f"the {family} table is empty"
        for name in names:
            full = metrics.PREFIX + name
            assert full in samples, f"{full} is not published"
            assert samples[full] == float(snap[family][name]), (
                f"{full} does not match the snapshot it came from"
            )


def test_streaming_moves_both_byte_counters(live_server, hls_server):
    """Received and sent must both be real, and the window must agree with them.

    The two figures the landing page leads with. Both are fed from the shared
    :class:`radioconv.throughput.Throughput`, which is the only place that
    survives a session being reaped or a station restarting - so this is also the
    test that the wiring reaches all the way out to a live HTTP stream.
    """
    _, mp3_url = hls_server
    read = 0
    with httpx.stream(
        "GET", live_server + "/stream", params={"url": mp3_url}, timeout=60
    ) as response:
        assert response.status_code == 200
        for chunk in response.iter_bytes():
            read += len(chunk)
            if read >= 32_768:
                break
    assert read >= 32_768, "the stream produced less audio than expected"

    snapshot = httpx.get(live_server + web.METRICS_JSON, timeout=10).json()
    assert snapshot["counters"]["bytes_in_total"] > 0, "upstream traffic was not counted"
    assert snapshot["counters"]["bytes_out_total"] >= read, (
        "audio the radio actually read was not counted as sent"
    )
    # The service has been up for minutes, so every byte ever moved is inside the
    # window and the two must agree exactly.
    assert snapshot["gauges"]["bytes_in_last_hour"] == snapshot["counters"]["bytes_in_total"]
    assert snapshot["gauges"]["bytes_out_last_hour"] == snapshot["counters"]["bytes_out_total"]
    assert snapshot["throughput"]["samples"] >= 0, "the window is published in full"


def test_both_endpoints_publish_the_same_metric_names(live_server):
    """Live check that the two endpoints are wired to the same snapshot.

    Values are deliberately not compared here: they legitimately move between
    two requests. The *names* must match exactly, in both directions, which is
    what catches a metric added to one shape and forgotten in the other.
    """
    snapshot = httpx.get(live_server + web.METRICS_JSON, timeout=10).json()
    samples = _published(httpx.get(live_server + "/metrics", timeout=10).text)

    expected = {metrics.PREFIX + n for n in list(metrics.GAUGES) + list(metrics.COUNTERS)}
    assert set(samples) == expected, "the two endpoints publish different metrics"

    # And the JSON really does carry every one of them, under the same name.
    for name in list(metrics.GAUGES) + list(metrics.COUNTERS):
        family = "gauges" if name in metrics.GAUGES else "counters"
        assert name in snapshot[family], f"{name} is missing from /metrics.json"


def test_prometheus_exposition_is_well_formed(live_server):
    text = httpx.get(live_server + "/metrics", timeout=10).text
    assert (
        httpx.get(live_server + "/metrics", timeout=10)
        .headers["content-type"]
        .startswith("text/plain")
    )
    for name in list(metrics.GAUGES) + list(metrics.COUNTERS):
        assert f"# HELP {metrics.PREFIX}{name} " in text, f"{name} has no HELP line"
        assert re.search(rf"^# TYPE {metrics.PREFIX}{name} (gauge|counter)$", text, re.M), (
            f"{name} has no TYPE line"
        )
    # Counters are counters: Prometheus rejects a counter name without the suffix.
    for name in metrics.COUNTERS:
        assert name.endswith("_total"), f"{name} is a counter but has no _total suffix"


def test_the_dashboard_only_asks_for_metrics_that_exist(live_server):
    """Every ``data-metric`` in every template must resolve in the snapshot.

    A typo here is a tile that reads "–" forever, which looks like a quiet
    service rather than a bug - exactly the kind of thing nobody reports.
    """
    snapshot = httpx.get(live_server + web.METRICS_JSON, timeout=10).json()
    paths = set()
    for template in web.resource_dir(web.TEMPLATES).glob("*.html"):
        paths.update(re.findall(r'data-metric="([A-Za-z0-9_.]+)"', template.read_text("utf-8")))
    assert paths, "no data-metric attributes found; did the templates change?"

    missing = []
    for path in sorted(paths):
        node: object = snapshot
        for key in path.split("."):
            node = node.get(key) if isinstance(node, dict) else None  # type: ignore[union-attr]
        if node is None:
            missing.append(path)
    assert not missing, f"templates ask for metrics that do not exist: {missing}"


def test_configured_limits_are_visible_on_the_dashboard(live_server):
    """ "Why did it say 503?" is answered by the page, not by the config file."""
    body = httpx.get(live_server + web.DASHBOARD_URL, timeout=10).text
    assert "Max stations" in body
    assert "kbit/s" in body


def test_the_ffmpeg_report_is_computed_once_per_process(monkeypatch):
    """``/health`` is meant to be polled, and the FFmpeg report forks FFmpeg.

    ``ffmpeg_version()`` and ``encoders()`` each *run the binary*, and a monitor
    asking "is this install healthy?" every few seconds must not fork FFmpeg
    twice per poll on a Raspberry Pi. The answer cannot change while we are
    running, so it is computed on first use and cached. Measured on the dev
    machine: 275 ms for the first ``/health``, 0.8 ms after that.
    """
    from fastapi.testclient import TestClient

    from radioconv import api

    calls = []

    def counting(settings):
        calls.append(settings)
        return {"bundled": False, "path": "stub", "version": None}

    monkeypatch.setattr(api, "_probe_ffmpeg", counting)
    # TestClient talks ASGI in-process, so `port` never binds - it only has to
    # pass validation (the settings validator rejects 0).
    app = api.create_app(Settings(host="127.0.0.1", log_level="WARNING"))

    with TestClient(app) as client:
        for _ in range(3):
            body = client.get("/health").json()
            assert body["ffmpeg"]["path"] == "stub"

    assert len(calls) == 1, f"the FFmpeg report was computed {len(calls)} times, not once"


def test_the_snapshot_is_json_serialisable():
    """Shape guard: no dataclasses, sets or tuples leak into the payload.

    Built from the **real** manager and monitor rather than stubs, so it also
    exercises the aggregation over their actual data structures - and keeps the
    type checker honest, which duck-typed fakes would not.
    """
    settings = Settings()
    snap = metrics.snapshot(SessionManager(settings), ResourceMonitor(settings))

    assert json.loads(json.dumps(snap))["gauges"]["max_sessions"] == settings.max_sessions
    assert snap["status"] == "ok", "no sessions is idle, not degraded"
    assert snap["counters"]["bytes_out_total"] == 0
    assert set(snap["counters"]) == set(metrics.COUNTERS), "a counter must not be omitted"


# ------------------------------------------------------- a silent stream is visible
def test_a_mute_session_makes_the_service_degraded():
    """A station that is connected but silent must not look healthy.

    This is the case the metrics used to miss entirely. The stream returned
    ``200 audio/mpeg``, created a session, fetched its segments successfully and
    then emitted **nothing at all**. Every figure stayed green: the session was
    counted, ``failures`` was 0, there were no restarts. The only clue was an
    ``idle`` number quietly climbing on a dashboard nobody was watching - so the
    page reported two healthy stations while a radio heard silence.

    A session that is connected and mute is a failure, and must be reported as
    one. It is *not* reported as a `pipeline_failure`, because nothing failed to
    fetch - it is reported as silence, which is what a listener actually hears.
    """
    settings = Settings(stall_timeout=30.0)
    manager = SessionManager(settings)
    monitor = ResourceMonitor(settings)

    # A session with a listener that has never produced a byte. How a test says
    # "these listeners have had nothing to play for two minutes" without waiting
    # two minutes: the clock is normally started by the first observation of an
    # empty buffer (see `StreamSession.starved_for`).
    session = session_mod.StreamSession(settings, "key", ["http://x/one.m3u8"], label="Mute FM")
    subscriber = session.add_subscriber(client="192.168.1.65")
    # A listener only counts as one once its response is actually running - the
    # flag the API's body generator sets. Without it the session is treated as
    # having no listeners at all, which is what stops an abandoned connection
    # from being reported as a starved audience.
    subscriber.started = True
    session._starved_since = time.time() - 120.0
    manager.sessions["key"] = session

    snap = metrics.snapshot(manager, monitor)

    assert snap["status"] == "degraded", "a connected, silent stream is not 'ok'"
    assert snap["gauges"]["silent_sessions"] == 1
    assert snap["streams"]["silent"] == 1
    assert snap["streams"]["details"][0]["stalled"] is True
    assert snap["streams"]["details"][0]["producing"] is False
    assert snap["streams"]["details"][0]["starved_for"] == 120.0
    # And the counters are untouched, which is the point: nothing "failed".
    assert snap["counters"]["pipeline_failures_total"] == 0
    assert snap["counters"]["pipeline_restarts_total"] == 0


def test_a_session_that_is_merely_starting_is_not_accused(monkeypatch):
    """A session must not be flagged before it has had a chance to produce.

    Otherwise every radio connection would briefly report the service as degraded,
    which is worse than the problem being fixed.
    """
    settings = Settings(stall_timeout=60.0)
    manager = SessionManager(settings)
    monitor = ResourceMonitor(settings)

    session = session_mod.StreamSession(settings, "key", ["http://x/one.m3u8"])
    subscriber = session.add_subscriber(client="192.168.1.65")
    # A listener that has just begun reading and has nothing buffered yet. This
    # must be marked, or the session has no listeners at all and the test holds
    # for a reason it is not about - it would pass even if a fresh connection
    # were reported as a stall.
    subscriber.started = True
    manager.sessions["key"] = session

    snap = metrics.snapshot(manager, monitor)

    assert snap["status"] == "ok", "a just-connected session is starting, not stalled"
    assert snap["gauges"]["silent_sessions"] == 0
    assert snap["streams"]["details"][0]["producing"] is False
    assert snap["streams"]["details"][0]["stalled"] is False


def test_a_session_without_listeners_is_never_stalled():
    """A warm, idle session between songs is not a fault."""
    settings = Settings(stall_timeout=30.0)
    session = session_mod.StreamSession(settings, "key", ["http://x/one.m3u8"])
    session.pipeline.stats.last_data_at = time.time() - 120.0
    assert session.silent_for > 100
    assert session.stalled is False, "no listeners, so nobody is being failed"


def test_a_dead_session_is_reported_as_dead_not_merely_silent():
    settings = Settings(stall_timeout=30.0)
    session = session_mod.StreamSession(settings, "key", ["http://x/one.m3u8"])
    session.add_subscriber(client="r").started = True
    session.pipeline.stats.last_data_at = time.time() - 120.0
    session.fatal_error = "all sources failed"

    assert session.dead is True
    assert session.stalled is False, "dead is its own state; do not double-count it"
    assert session.as_dict()["fatal_error"] == "all sources failed"


def test_silent_sessions_is_published_to_prometheus(live_server):
    """The new gauge must appear in the exposition and the JSON, like every other."""
    text = httpx.get(live_server + "/metrics", timeout=10).text
    assert f"# TYPE {metrics.PREFIX}silent_sessions gauge" in text
    assert f"{metrics.PREFIX}silent_sessions " in text

    snapshot = httpx.get(live_server + web.METRICS_JSON, timeout=10).json()
    assert "silent_sessions" in snapshot["gauges"]
    assert snapshot["gauges"]["silent_sessions"] == 0, "nothing is streaming in tests"
