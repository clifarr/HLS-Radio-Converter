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
from html.parser import HTMLParser

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


def test_the_engine_column_reports_only_the_engine(tmp_path):
    """Reported: "the stream that is currently playing says transcode - mp3."

    The Engine column appended a codec, built as ``detected_kind ||
    detected_codec`` - the *source* kind first, the *output* codec second. One
    slot therefore meant two different things: a station with ADTS AAC in a
    transport stream read ``transcode · adts``, while a station serving bare
    ``.aac`` segments read ``transcode · mp3`` (nothing was detected, so the
    fallback named the output). Neither told a listener what to do, and the
    first one looked like it contradicted the second.

    What a listener needs is whether FFmpeg is running, so the column is the
    mode alone. This runs the shipped ``engineLabel`` under Node: a text search
    would pass for a comment, and rendering the page would need a browser.
    """
    node = shutil.which("node")
    if node is None:
        # ty mis-resolves pytest.skip (see tests/conftest.py).
        pytest.skip("node is not installed")  # ty: ignore[too-many-positional-arguments]
    script = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.js").read_text(encoding="utf-8"),
        language="javascript",
    )
    match = re.search(r"function engineLabel\(pipeline\) \{.*?\n  \}", script, re.S)
    assert match, (
        "engineLabel has moved or changed shape - the column's text must stay "
        "a pure function this test can drive"
    )
    # And the codec fields must not come back through another door.
    for field in ("detected_kind", "detected_codec"):
        assert field not in script, (
            f"dashboard.js reads {field} again; the Engine column is the engine"
        )

    # Pipelines as the snapshot sends them, including the codec fields this
    # column must ignore, and the text it must produce.
    expectations = [
        (
            "forwarded without ffmpeg",
            "{ mode: 'passthrough', detected_kind: 'mp3', detected_codec: 'mp3' }",
            "passthrough",
        ),
        (
            "ADTS source, re-encoded to mp3",
            "{ mode: 'transcode', detected_kind: 'adts', detected_codec: 'mp3' }",
            "transcode",
        ),
        (
            "raw .aac source, nothing detected",
            "{ mode: 'transcode', detected_codec: 'mp3' }",
            "transcode",
        ),
        ("first segment not arrived", "{ mode: 'unknown' }", "starting\u2026"),
        ("no reading yet", "{}", "starting"),
    ]
    checks = "\n".join(
        f"check({label!r}, engineLabel({snap}), {want!r});" for label, snap, want in expectations
    )
    harness = (
        match.group(0)
        + "\n\nfunction check(label, actual, want) {\n"
        + "  if (actual !== want) {\n"
        + '    console.error(label + ": got " + JSON.stringify(actual) + ", want "'
        + " + JSON.stringify(want));\n"
        + "    process.exit(1);\n"
        + "  }\n"
        + "}\n"
        + checks
        + '\nconsole.log("engineLabel: the column is the engine and nothing else");\n'
    )
    script_file = tmp_path / "engine_label.js"
    script_file.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [node, str(script_file)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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


def test_the_status_pill_tells_idle_from_live(tmp_path):
    """Reported: the pill says "live" even when nothing is streaming.

    ``status`` is ``ok`` for an idle service *on purpose* - idle is healthy,
    not degraded (see ``test_the_snapshot_is_json_serialisable``) - so reading
    ``status === "ok"`` as "live" claimed a live stream on any install with
    no radio attached, and "nothing streaming" was unreachable: it lived only
    on the ``degraded`` branch, which needs a session to be wrong.

    The decision is one pure function in the script, so this runs that exact
    source under Node through every state the pill can show. Asserting on the
    source text instead would pass for a comment; rendering the page would
    need a browser the suite does not have.
    """
    node = shutil.which("node")
    if node is None:
        # ty mis-resolves pytest.skip (see tests/conftest.py).
        pytest.skip("node is not installed")  # ty: ignore[too-many-positional-arguments]
    script = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.js").read_text(encoding="utf-8"),
        language="javascript",
    )
    match = re.search(r"function statusFor\(snap, failures\) \{.*?\n  \}", script, re.S)
    assert match, (
        "statusFor has moved or changed shape - the pill's decision must stay "
        "a pure function this test can drive"
    )
    # And the pill must actually be driven by it, not merely ship it unused.
    assert "statusFor(snapshot, failures)" in script, "paintStatus ignores statusFor"

    # What the pill shows, the snapshot it was shown, the poll failures counted,
    # and the state/text that must come back - the spec, in one table.
    expectations = [
        ("no reading yet", "null", 0, "unknown", "connecting\u2026"),
        (
            "polls failing",
            "{ status: 'ok', gauges: { sessions: 2 } }",
            3,
            "down",
            "no contact",
        ),
        (
            "idle service, no radio attached",
            "{ status: 'ok', gauges: { sessions: 0 } }",
            0,
            "idle",
            "nothing streaming",
        ),
        (
            "a station streaming",
            "{ status: 'ok', gauges: { sessions: 1 } }",
            0,
            "ok",
            "live",
        ),
        (
            "station silent to its listeners",
            "{ status: 'degraded', gauges: { sessions: 1 } }",
            0,
            "degraded",
            "nothing streaming",
        ),
        (
            "every session dead",
            "{ status: 'degraded', gauges: { sessions: 2 } }",
            0,
            "degraded",
            "nothing streaming",
        ),
    ]
    checks = "\n".join(
        f"check({label!r}, statusFor({snap}, {failures}), {state!r}, {text!r});"
        for label, snap, failures, state, text in expectations
    )
    harness = (
        match.group(0)
        + "\n\nfunction check(label, actual, state, text) {\n"
        + "  if (!actual || actual.state !== state || actual.text !== text) {\n"
        + '    console.error(label + ": got " + JSON.stringify(actual) + ", want "'
        + ' + state + "/" + JSON.stringify(text));\n'
        + "    process.exit(1);\n"
        + "  }\n"
        + "}\n"
        + checks
        + '\nconsole.log("statusFor: every state is what the pill should say");\n'
    )
    script_file = tmp_path / "status_for.js"
    script_file.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [node, str(script_file)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_assets_must_be_revalidated_before_reuse(live_server):
    """Reported: "the engine still says transcode - mp3" after a deploy that fixed it.

    The server was serving the new script the whole time; the browser was never
    asking for it. ``StaticFiles`` sends ``last-modified`` and ``etag`` but no
    ``cache-control``, and a response with no explicit policy is cached by
    *heuristic freshness* - roughly 10% of the file's age - so a script fetched
    while the file was a day old could be reused for hours without a request.
    The user sees the old UI, and re-running the deploy changes nothing.

    ``no-cache`` means "revalidate before reusing", not "do not cache", and the
    conditional request it triggers is still answered 304 with no body.
    """
    asset = httpx.get(live_server + "/static/dashboard.js", timeout=10)
    assert asset.status_code == 200
    assert "no-cache" in asset.headers.get("cache-control", ""), (
        "static assets are cacheable by heuristic freshness again - a browser "
        "can keep running an old dashboard.js after an upgrade"
    )
    etag = asset.headers.get("etag")
    assert etag, "without an etag the revalidation this header forces costs a full body"

    conditional = httpx.get(
        live_server + "/static/dashboard.js",
        headers={"if-none-match": etag},
        timeout=10,
    )
    assert conditional.status_code == 304, (
        "revalidation must be cheap; a full 200 on every page load is a cost the Pi should not pay"
    )
    assert "no-cache" in conditional.headers.get("cache-control", ""), (
        "the 304 must restate the policy, or the next fetch goes back to guessing"
    )


def test_asset_urls_change_with_the_version(live_server):
    """A release must be a *different* URL, not just a different file.

    ``no-cache`` covers a browser that honours it, but the query string covers
    the ones that do not - a phone's webview, a proxy, an offline cache. Both
    assets are versioned, because the CSS is as capable of going stale as the
    script.
    """
    for path in ("/", web.DASHBOARD_URL):
        body = httpx.get(live_server + path, timeout=10).text
        for name in ("dashboard.css", "dashboard.js"):
            expected = f"/static/{name}?v={web.__version__}"
            assert expected in body, f"{path} does not request {expected}"
        assert "no-cache" in httpx.get(live_server + path, timeout=10).headers.get(
            "cache-control", ""
        ), (
            f"{path} can be cached heuristically, so a browser may never see the "
            "new asset URLs at all - which is the same bug one level up"
        )


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


#: The two figures a person reads against each other. Both used to be separate
#: tiles, which the auto-fit grid was free to put in different rows.
THROUGHPUT = ("gauges.bytes_in_last_hour", "gauges.bytes_out_last_hour")


class _NearestTile(HTMLParser):
    """Map each ``data-metric`` to the ``.tile`` element that contains it.

    Identity is an integer stamped on every element that carries the ``tile``
    class, so two figures can be compared by which tile owns them. A text search
    for the class name cannot tell "one tile holding both" from "two tiles in a
    row that happens to be adjacent in the source" - which is the bug this
    guards against.
    """

    def __init__(self, wanted):
        super().__init__()
        self.wanted = set(wanted)
        self.open: list = []  # (tag, tile id or None) for each open element
        self.owners: dict = {}
        self.classes: dict = {}
        self._next_id = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        owner = None
        if "tile" in classes:
            self._next_id += 1
            owner = self._next_id
            self.classes[owner] = classes
        self.open.append((tag, owner))
        metric = attributes.get("data-metric")
        if metric in self.wanted:
            self.owners[metric] = next(
                (tile for _tag, tile in reversed(self.open) if tile is not None), None
            )

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        for index in range(len(self.open) - 1, -1, -1):
            if self.open[index][0] == tag:
                del self.open[index:]
                return


def test_received_and_sent_are_one_tile(live_server):
    """Reported: "the received and sent metrics always appear in different rows."

    They were two tiles in an ``auto-fit`` grid, so whether they landed side by
    side depended on the column count - at eight columns, an ordinary desktop
    width, "received" ended a row and "audio sent" started the next. Two figures
    a reader compares are exactly the ones the grid must not separate, so they
    share one tile, which no column count can split.
    """
    body = httpx.get(live_server + web.DASHBOARD_URL, timeout=10).text
    finder = _NearestTile(THROUGHPUT)
    finder.feed(body)

    assert set(finder.owners) == set(THROUGHPUT), (
        "the throughput figures are missing from the dashboard"
    )
    incoming, outgoing = (finder.owners[metric] for metric in THROUGHPUT)
    assert incoming is not None, "received is not inside a tile at all"
    assert incoming == outgoing, (
        "received and sent are separate tiles again; the grid can put them in "
        "different rows, which is the layout this guards against"
    )
    assert "pair" in finder.classes[incoming], (
        "the throughput tile is not marked as a pair, so it will not span the "
        "two cells that keep both halves readable"
    )

    css = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.css").read_text(encoding="utf-8"),
        language="css",
    )
    assert ".tile.pair" in css and "grid-column: span 2" in css, (
        "the pair tile no longer spans two cells; each half shrinks to half a "
        "tile and the byte figures wrap"
    )


def test_the_header_marks_which_page_you_are_on(live_server):
    """The links are chips, and the current one stays marked after a click.

    They used to inherit the body size and read as an unstyled list next to the
    title. The styling alone cannot say which page is open, so each page marks
    its own link, and that mark is what ``aria-current`` is for - a reader with
    a screen reader gets it too, not just the one seeing the highlight.
    """
    pages = {
        "/": "Converter",
        web.DASHBOARD_URL: "Dashboard",
    }
    for path, label in pages.items():
        body = httpx.get(live_server + path, timeout=10).text
        marked = re.findall(r'<a href="[^"]*" aria-current="page">([^<]+)</a>', body)
        assert marked == [label], f"{path} marks {marked}, expected only {label!r}"

    css = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.css").read_text(encoding="utf-8"),
        language="css",
    )
    assert '.head nav a[aria-current="page"]' in css, (
        "the current page is marked in the markup but not styled"
    )


def test_the_station_table_becomes_cards_on_a_phone(live_server):
    """Requested: make the dashboard fit a phone, and station rows become cards.

    An eleven-column table cannot be squeezed into 390px: the alternative to a
    card layout is either sideways scrolling (on the one page whose whole job is
    to be glanced at) or type too small to read. The same markup serves both
    layouts - the header row is hidden on a narrow screen and every cell prints
    its own name from `data-label`.

    Names are read from the headings at runtime instead of being written into
    the script, so a renamed column cannot end up with a header saying one thing
    and a card label saying another. This test pins that: the script must derive
    the label, and must not hard-code the words.
    """
    body = httpx.get(live_server + web.DASHBOARD_URL, timeout=10).text
    assert '<table class="stations" role="table">' in body, (
        "the station table is not marked for the card layout"
    )
    # Station + ten figures. The script indexes into these, so the count matters.
    stations = body.split('<table class="stations"', 1)[1].split("</table>", 1)[0]
    assert len(re.findall(r"<th\b", stations)) == 11, "the stations table changed shape"

    script = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.js").read_text(encoding="utf-8"),
        language="javascript",
    )
    assert 'querySelectorAll(".stations thead th")' in script, (
        "dashboard.js no longer reads the column names from the headings"
    )
    assert "data-label" in script or "dataset.label" in script, (
        "cells do not name themselves, so a card shows bare values"
    )
    # Every column after the station name is addressed by index. A hard-coded
    # label here would be a second copy of the heading that nothing updates.
    for index in range(1, 11):
        assert f"cell({index}," in script, f"no cell is built for column {index}"
    for hardcoded in ('cell("Radios"', 'cell("Engine"', 'cell("Starved"'):
        assert hardcoded not in script, (
            f"{hardcoded} hard-codes a column name that the heading already carries"
        )
    # `display: block` drops the table out of the accessibility tree unless the
    # roles are explicit - in the template and on every cell the script makes.
    assert 'setAttribute("role", "cell")' in script, "cells drop their row/table semantics"

    css = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.css").read_text(encoding="utf-8"),
        language="css",
    )
    mobile = re.search(r"@media \(max-width: 64rem\) \{(.*)\n\}", css, re.S)
    assert mobile, "the narrow-screen layout is gone; the table is back to scrolling"
    block = mobile.group(1)
    for needed in ("table.stations thead { display: none; }", "attr(data-label)"):
        assert needed in block, f"the card layout no longer does {needed!r}"
    # The settings list gets its own compaction rather than being squeezed.
    assert "table.kv" in block, "the configuration table is not adjusted for a phone"


def test_the_cards_read_as_cards_not_as_a_stack_of_rows():
    """Reported: the first card layout was "rather ugly and unreadable".

    It stacked one `label ......... value` row per cell: eleven lines on a
    phone, mostly `0` and `-`, in the table's monospace, with a dotted rule
    under every label. That is a spreadsheet turned sideways.

    A card is a small grid of figures instead - label above value, two figures
    to a row - in the page's own font, with the figures paired by what a person
    compares rather than by the desktop's column order. Those are the things
    that made it readable, so they are what this pins.
    """
    css = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.css").read_text(encoding="utf-8"),
        language="css",
    )
    mobile = re.search(r"@media \(max-width: 64rem\) \{(.*)\n\}", css, re.S)
    assert mobile, "the narrow-screen layout is gone"
    block = mobile.group(1)

    # Two figures per row, laid out as a grid rather than a list of rows.
    assert "grid-template-columns: 1fr 1fr" in block, (
        "the card is not a two-column grid any more; it is back to one line per "
        "figure, which is eleven lines of mostly zeroes"
    )
    # ...with the name above its value, not across a row from it.
    assert "flex-direction: column" in block, (
        "the label sits beside its value again, which is the spreadsheet look"
    )
    # The table's monospace is for aligned columns of digits; a block of labels
    # set in it is what made the card look like a terminal dump.
    assert "font-family: var(--sans)" in block, (
        "the card did not opt out of the table's monospace font"
    )
    assert "--sans:" in css, "the body font stack is not shared with the card"

    # The reading order is chosen, not inherited: the first pair a person sees
    # is radios and audio held, not radios and the `-` in Dropped.
    for label in ("Radios", "Buffered", "Sent", "Engine", "Starved"):
        assert f'td[data-label="{label}"] {{ order:' in block, (
            f"{label} has no place in the card's reading order"
        )
    assert "order: 1" in block and "order: 2" in block, "the card's order rules are gone"

    # Every label is reachable for its description, and the description is on
    # the cell now that the headings are hidden.
    script = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.js").read_text(encoding="utf-8"),
        language="javascript",
    )
    assert "node.dataset.tooltip = column.tip" in script, (
        "cards have no tooltips: the headings that carry them are hidden on a phone"
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
    for needed in ("data-tooltip", "renderTip", "tipAction", "pointerover", "focusin"):
        assert needed in script, f"dashboard.js does not implement {needed}"
    # And the script itself must not describe anything with a native `title`
    # either: the status pill used one, where it is delayed about a second,
    # drawn by the browser, and paired with the cursor this page rejects.
    assert ".title =" not in code_only(script, language="javascript"), (
        "dashboard.js went back to a native title tooltip; use data-tooltip, "
        "like every other description on the page"
    )


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


#: The rules the tooltip follows, as they are meant to behave. Each row is
#: (label, event, expected action). The event is the dictionary ``tipAction``
#: takes, and the action is what it must answer: "show" opens the description,
#: "hide" closes the source the event came from, "hide-all" closes both, and
#: "none" leaves things exactly as they are.
#:
#: This is the bug, written down. It was reported as "the tooltips tend to
#: disappear" on a phone: a description that changed every second vanished
#: almost at once, and on the stations list it vanished *always* - the only
#: way to read it was to hold a finger down.
#:
#: Then the follow-up: putting a finger down on a figure to *scroll* past it
#: opened the description, and it stayed. The tap and the scroll are the same
#: gesture until the finger moves - so the rules below are about the one
#: event that tells them apart.
TIP_RULES = [
    # A tap is what a finger does, and it is the only way to reach a
    # description on a touch screen at all. It must show - but on the
    # *click*, which the browser only sends when the finger was not dragged.
    (
        "tap a described figure",
        {"type": "click", "pointerType": "touch", "hasTip": True},
        "show",
    ),
    # A tap on something with no description means the reader asked for
    # something else, so the description goes - including the one the tap left
    # the focus sitting on.
    (
        "tap something with no description",
        {"type": "click", "pointerType": "touch", "hasTip": False},
        "hide-all",
    ),
    # The finger moving turns the gesture into a scroll, and the browser
    # cancels the tap. This is that cancellation, and it is the only thing
    # that distinguishes a scroll from a tap - without it, a finger put down
    # to scroll past a figure opens its description.
    (
        "the gesture becomes a scroll",
        {"type": "pointercancel", "pointerType": "touch", "hasTip": True},
        "hide",
    ),
    # The finger lifting. The browser reports this as the pointer leaving,
    # with no relatedTarget, because the pointer dies with the touch. Read
    # as a departure it closed the description the instant the tap ended -
    # which is the whole of the reported bug.
    (
        "the finger lifts",
        {"type": "pointerout", "pointerType": "touch", "hasTip": True, "leaves": True},
        "none",
    ),
    (
        "a touch pointer hovers",
        {"type": "pointerover", "pointerType": "touch", "hasTip": True},
        "none",
    ),
    # A touch does not open a description by giving the element the focus.
    # A tap focuses the element as a side effect, and the click has already
    # decided the matter - so a focus straight after a tap must not open one
    # for the plain text around the figures.
    (
        "the focus a tap left behind",
        {"type": "focusin", "hasTip": True, "touchFocus": True},
        "none",
    ),
    # A mouse still works the way it always did.
    ("mouse over", {"type": "pointerover", "pointerType": "mouse", "hasTip": True}, "show"),
    (
        "mouse over nothing",
        {"type": "pointerover", "pointerType": "mouse", "hasTip": False},
        "hide",
    ),
    (
        "mouse leaves",
        {"type": "pointerout", "pointerType": "mouse", "hasTip": True, "leaves": True},
        "hide",
    ),
    (
        "mouse moves to a child of the same figure",
        {"type": "pointerout", "pointerType": "mouse", "hasTip": True, "leaves": False},
        "none",
    ),
    # The keyboard.
    ("focus lands on a described figure", {"type": "focusin", "hasTip": True}, "show"),
    (
        "focus lands on nothing described",
        {"type": "focusin", "hasTip": False},
        "none",
    ),
    ("focus leaves", {"type": "focusout"}, "hide"),
    # Focus moving to another described figure: `focusin` is about to show
    # it, so closing here would make it flicker.
    ("focus moves to another described figure", {"type": "focusout", "nextHasTip": True}, "none"),
    # The stations table is rebuilt every second, which replaces the element
    # the focus was in and makes the browser report a `focusout`. The
    # description is put straight back, so closing it would flicker.
    (
        "the stations table is rebuilt",
        {"type": "focusout", "rebuilding": True},
        "none",
    ),
]


def test_a_tapped_description_stays_until_it_is_dismissed(tmp_path):
    """Reported: "the tooltips tend to disappear" - on a phone, on their own.

    A touch pointer is "over" an element only while a finger is on the screen,
    and the browser reports "out" the instant that finger lifts - with no
    relatedTarget, because the pointer is destroyed along with the touch. The
    old code read that as "the pointer left", so it closed the description the
    moment the tap ended. On a tile whose figure changes every second it looked
    intermittent, because the repaint happened to land near the tap; on the
    stations list it was always, because the table is rebuilt every second and
    the repaint killed the description too.

    The rules are the bug, so they are written down as rules and driven
    through the real function - a text search would pass for a comment, and
    rendering the page would need a browser the suite does not have.
    """
    node = shutil.which("node")
    if node is None:
        # ty mis-resolves pytest.skip (see tests/conftest.py).
        pytest.skip("node is not installed")  # ty: ignore[too-many-positional-arguments]
    script = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.js").read_text(encoding="utf-8"),
        language="javascript",
    )
    match = re.search(r"function tipAction\(\{.*?^[ ]{2}\}\) \{.*?^[ ]{2}\}", script, re.S | re.M)
    assert match, (
        "tipAction has moved or changed shape - the rules that decide when a "
        "description shows must stay a pure function this test can drive"
    )

    # The rules are written in Python, so the booleans have to be translated:
    # Python's `True` is not JavaScript's `true`, and Node says so.
    def js(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, dict):
            return "{" + ", ".join(f"{k}: {js(v)}" for k, v in value.items()) + "}"
        return repr(value)

    checks = "\n".join(
        f"check({label!r}, tipAction({js(event)}), {want!r});" for label, event, want in TIP_RULES
    )
    harness = (
        match.group(0)
        + "\n\nfunction check(label, actual, want) {\n"
        + "  if (actual !== want) {\n"
        + '    console.error(label + ": got " + actual + ", want " + want);\n'
        + "    process.exit(1);\n"
        + "  }\n"
        + "}\n"
        + checks
        + '\nconsole.log("tipAction: every rule behaves as the page promises");\n'
    )
    script_file = tmp_path / "tip_action.js"
    script_file.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [node, str(script_file)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_finger_held_down_to_scroll_does_not_open_a_description(tmp_path):
    """Reported: holding a finger on a figure to scroll past it opens the tooltip.

    A tap and a scroll are the same gesture until the finger moves, so the
    page cannot tell them apart on the way *down* - only on the way up. The
    browser draws that line for us:

    * a **tap** ends with a ``click`` (and the ``focusin`` that follows it);
    * a **scroll** ends with ``pointercancel`` and **no click at all**.

    So the description is opened by the click, not by the touch. Opening it on
    the touch is what made a figure the reader was scrolling past light up
    and stay lit.

    This is also the answer to "how do other sites do it": they all open on
    the click, for the same reason.
    """
    node = shutil.which("node")
    if node is None:
        # ty mis-resolves pytest.skip (see tests/conftest.py).
        pytest.skip("node is not installed")  # ty: ignore[too-many-positional-arguments]
    script = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.js").read_text(encoding="utf-8"),
        language="javascript",
    )
    match = re.search(r"function tipAction\(\{.*?^[ ]{2}\}\) \{.*?^[ ]{2}\}", script, re.S | re.M)
    assert match, (
        "tipAction has moved or changed shape - the rules that decide when a "
        "description shows must stay a pure function this test can drive"
    )

    # The whole gesture, as the browser reports it. A scroll is a touch that
    # moves, and the browser cancels the tap: no click, and a `pointercancel`
    # instead. A tap gets a click and never a cancellation.
    def js(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, dict):
            return "{" + ", ".join(f"{k}: {js(v)}" for k, v in value.items()) + "}"
        return repr(value)

    tap = [
        {"type": "pointerdown", "pointerType": "touch", "hasTip": True},
        {"type": "pointerup", "pointerType": "touch", "hasTip": True},
        {"type": "click", "pointerType": "touch", "hasTip": True},
    ]
    scroll = [
        {"type": "pointerdown", "pointerType": "touch", "hasTip": True},
        {"type": "pointermove", "pointerType": "touch", "hasTip": True},
        {"type": "pointercancel", "pointerType": "touch", "hasTip": True},
    ]

    checks = []
    # A tap: nothing opens it until the click, and the click opens it.
    for event in tap[:-1]:
        checks.append(f"checkNotShow('a tap', tipAction({js(event)}));")
    checks.append(f"check('a tap', tipAction({js(tap[-1])}), 'show');")
    # A scroll: nothing opens it, and the cancellation does not either.
    for event in scroll:
        checks.append(f"checkNotShow('a scroll', tipAction({js(event)}));")

    harness = (
        match.group(0)
        + "\n\nfunction check(label, actual, want) {\n"
        + "  if (actual !== want) {\n"
        + '    console.error(label + ": got " + actual + ", want " + want);\n'
        + "    process.exit(1);\n"
        + "  }\n"
        + "}\n"
        + "function checkNotShow(label, actual) {\n"
        + '  if (actual === "show") {\n'
        + '    console.error(label + ": opened the description");\n'
        + "    process.exit(1);\n"
        + "  }\n"
        + "}\n"
        + "\n".join(checks)
        + '\nconsole.log("tipAction: a scroll never opens a description");\n'
    )
    script_file = tmp_path / "tip_scroll.js"
    script_file.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [node, str(script_file)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # And the focus a tap leaves behind must not open one either: a tap on the
    # plain text around a figure would open the description of whatever
    # happened to be next to it.
    focus_harness = (
        match.group(0)
        + "\n\n"
        + "const actual = tipAction({ type: 'focusin', hasTip: true, touchFocus: true });\n"
        + "if (actual === 'show') {\n"
        + "  console.error('the focus a tap left behind opened a description');\n"
        + "  process.exit(1);\n"
        + "}\n"
        + "console.log('the focus a tap left behind opens nothing');\n"
    )
    focus_file = tmp_path / "tip_touch_focus.js"
    focus_file.write_text(focus_harness, encoding="utf-8")
    focus_result = subprocess.run(
        [node, str(focus_file)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert focus_result.returncode == 0, focus_result.stdout + focus_result.stderr


def test_the_description_survives_the_table_being_rebuilt():
    """The stations table is rebuilt every second, and the description must not blink.

    Two things go wrong when it is, and both were reported as "the tooltip
    disappears":

    * The cell the reader had tapped is replaced, so the browser reports a
      `focusout` - which closed the description. It is put back afterwards, so
      the close has to be suppressed while the rebuild runs.
    * The description has to find its trigger again in the new markup, which
      means it needs a *key* - a station and a column - rather than a
      reference to a DOM node that is about to be thrown away.
    """
    script = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.js").read_text(encoding="utf-8"),
        language="javascript",
    )
    # The key, and the look-up that uses it.
    assert "data-tip-key" in script or "dataset.tipKey" in script, (
        "the descriptions are still keyed to a DOM node, so they cannot survive "
        "the table being rebuilt"
    )
    assert "restoreTip" in script, "the description is not put back after a rebuild"
    # The rebuild has to be *announced* while it runs - that is what suppresses
    # the `focusout` the removal of the old cell causes. A bare mention of the
    # flag is not enough: it has to be set before the table is emptied and
    # cleared after, and it has to be what the `focusout` handler reads.
    assert "repainting = true" in script, (
        "the rebuild does not mark itself, so the focusout it causes closes the "
        "description it is about to restore"
    )
    assert "repainting = false" in script, (
        "the rebuild never clears its mark, so the description can never be "
        "closed by a focus change again"
    )
    assert re.search(r"rebuilding:\s*repainting", script), (
        "the focusout handler does not read the rebuild flag, so the focusout "
        "caused by a rebuild still closes the description"
    )
    # The focus has to be put back too, or a keyboard reader is thrown to the
    # top of the page every second.
    assert "restoreFocus" in script, "the focus is lost every time the table is rebuilt"

    # Every cell must carry the key, or the description cannot be found again.
    for index in range(1, 11):
        assert f'"cell:" + key + ":{index}"' in script, (
            f"column {index} does not carry a key, so its description cannot "
            "survive the table being rebuilt"
        )


def test_the_description_follows_its_trigger_when_the_page_scrolls():
    """Scrolling is not a reason to throw the description away.

    It used to be: any scroll closed it, even though the element it described
    was still on screen. The stations table scrolls sideways, and the page
    scrolls, so on a phone the description was closed by the very gesture that
    showed it.
    """
    script = code_only(
        (web.resource_dir(web.STATIC) / "dashboard.js").read_text(encoding="utf-8"),
        language="javascript",
    )
    assert "refreshTip" in script, "the description is not moved when the page scrolls"
    # The scroll listener must *move* it, not close it.
    scroll = re.search(r'addEventListener\("scroll",\s*(\w+)', script)
    assert scroll, "the page does not listen for scrolling at all"
    assert scroll.group(1) == "refreshTip", (
        f"scrolling calls {scroll.group(1)!r}, which closes the description; it "
        "should move it to where its trigger now is"
    )
    # And it has to catch the table's own sideways scroller, which does not
    # bubble to the window.
    assert "capture: true" in script, (
        "the scroll listener does not capture, so the stations table's own "
        "sideways scroll is not seen"
    )


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
