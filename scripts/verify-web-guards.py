"""Prove each UI guard actually fails when the thing it guards is broken.

A green test proves nothing unless it can go red. Every guard added alongside
the templates/static split and the shared metrics snapshot is deliberately
broken here, and the specific test is then asserted to fail. Same reasoning as
``scripts/verify-linters-work.py``: a suite that cannot fail is not evidence.

Each case restores the file it touched in a ``finally``, so the tree is left
exactly as it was found.

    uv run python scripts/verify-web-guards.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "radioconv"


class Case(NamedTuple):
    """One deliberate breakage, and the test that must notice it."""

    label: str
    target: Path
    #: Text to find. ``None`` means "create this file", for a case where the bug
    #: is an extra file rather than an edited one.
    find: Optional[str]
    replace: str
    test: str


CASES: List[Case] = [
    Case(
        "HTML in a Python module",
        PKG / "api.py",
        "log = logging.getLogger(__name__)",
        'log = logging.getLogger(__name__)\nPANEL = "<div>hi</div>"',
        "test_no_html_lives_in_a_python_module",
    ),
    Case(
        "template asks for a metric that does not exist",
        PKG / "templates" / "dashboard.html",
        'data-metric="gauges.sessions"',
        'data-metric="gauges.sesions"',
        "test_the_dashboard_only_asks_for_metrics_that_exist",
    ),
    Case(
        # The real risk is someone hand-editing the exposition beside the JSON,
        # so drop the counters family: a counter then exists in one shape and
        # not the other, which is exactly the drift this pair guards against.
        # Changing a *value* inside snapshot() would NOT be caught, and rightly
        # so - both renderings would move together. This asserts they share one
        # source, not that the source is right.
        "Prometheus stops being rendered from the snapshot",
        PKG / "metrics.py",
        'for family, kind in ((GAUGES, "gauge"), (COUNTERS, "counter")):',
        'for family, kind in ((GAUGES, "gauge"),):',
        "test_prometheus_is_rendered_from_the_snapshot",
    ),
    Case(
        "Prometheus drops a metric family",
        PKG / "metrics.py",
        'for family, kind in ((GAUGES, "gauge"), (COUNTERS, "counter")):',
        'for family, kind in ((GAUGES, "gauge"),):',
        "test_both_endpoints_publish_the_same_metric_names",
    ),
    Case(
        "a shipped asset is unreferenced",
        PKG / "static" / "unused.css",
        None,
        "/* junk */",
        "test_every_asset_a_template_references_exists",
    ),
    Case(
        # Must be base.html: `data-metrics-url` is set there, and that is the
        # attribute the pages actually poll. Changing a `{{ metrics_url }}` in
        # index.html only alters a caption, which the guard rightly ignores.
        "page polls the wrong endpoint",
        PKG / "templates" / "base.html",
        'data-metrics-url="{{ metrics_url }}"',
        'data-metrics-url="/wrong-url"',
        "test_both_pages_ask_the_script_for_the_same_endpoint",
    ),
    Case(
        "the page fails to escape user input",
        PKG / "templates" / "index.html",
        'value="{{ target }}"',
        'value="{{ target | safe }}"',
        "test_the_pages_escape_what_the_user_typed",
    ),
    Case(
        "the dashboard route stops matching",
        PKG / "api.py",
        '@app.get(\n        "/dashboard",',
        '@app.get(\n        "/dashboard-x",',
        "test_dashboard_page_renders_and_does_not_stream",
    ),
    # ---- the tooltips. The first implementation used `title`, which the browser
    # delays by about a second and draws itself, next to a `cursor: help` that
    # renders as a literal question mark. All three ways back to that are blocked
    # below.
    Case(
        "a description goes back to a native title attribute",
        PKG / "templates" / "dashboard.html",
        '<div class="tile" tabindex="0"\n       data-tooltip="Radio connections',
        '<div class="tile" tabindex="0"\n       title="Radio connections',
        "test_descriptions_are_not_native_title_tooltips",
    ),
    Case(
        "the question-mark cursor comes back",
        PKG / "static" / "dashboard.css",
        "[data-tooltip] { cursor: default; }",
        "[data-tooltip] { cursor: help; }",
        "test_descriptions_are_not_native_title_tooltips",
    ),
    Case(
        "a description stops being reachable without a mouse",
        PKG / "templates" / "dashboard.html",
        '<div class="tile" tabindex="0"\n       data-tooltip="Radio connections',
        '<div class="tile"\n       data-tooltip="Radio connections',
        "test_every_description_is_reachable_without_a_mouse",
    ),
    Case(
        "a tile is left without any description",
        PKG / "templates" / "dashboard.html",
        '<div class="tile" tabindex="0"\n       data-tooltip="Radio connections',
        '<div class="tile" tabindex="0">\n    <span class="value" data-metric="gauges.listeners"',
        "test_every_metric_shown_has_a_plain_language_description",
    ),
    Case(
        # The guard reads code, not prose - so this has to be real code to prove
        # the comment-stripping did not blunt it.
        "the tooltip is built with innerHTML",
        PKG / "static" / "dashboard.js",
        '      const node = element("div");\n      node.id = "tooltip";',
        '      const node = element("div");\n      node.id = "tooltip";\n'
        '      node.innerHTML = "<b>x</b>";',
        "test_the_javascript_writes_no_html",
    ),
    Case(
        # The status pill described itself with a native `title` once - the same
        # failure the template/css cases above block, in the last place left to
        # make it. The assertion reads code (comments stripped), so the prose in
        # this file cannot blunt it either.
        "the pill's description goes back to a native title",
        PKG / "static" / "dashboard.js",
        "    pill.dataset.tooltip = TIP[shown.state] || hint;",
        "    pill.title = TIP[shown.state] || hint;",
        "test_descriptions_are_not_native_title_tooltips",
    ),
    Case(
        # The reported bug: the pill read `status === "ok"` as "live", which is
        # true of an idle service too - so it claimed "live" while nothing
        # streamed. Dropping the idle branch is exactly the old behaviour, and
        # the test must fail for it.
        "the pill calls an idle service live",
        PKG / "static" / "dashboard.js",
        '    if (sessions === 0) return { state: "idle", text: "nothing streaming" };\n',
        "",
        "test_the_status_pill_tells_idle_from_live",
    ),
]


def run(test: str) -> Tuple[str, int]:
    """Run one test; return pytest's summary line *and* its exit code.

    The exit code is the verdict; the summary is only for the log. It is
    searched for rather than taken as the last line of the combined output,
    because ``uv run`` writes "Installed 1 package in 2ms" to stderr when the
    editable install is stale - and stderr lands after stdout, so the last line
    can be uv's, not pytest's.
    """
    # Resolved rather than bare: invoking a bare "uv" is an S607 lint error.
    # The argv is built entirely from this module's own constants - no shell, no
    # external input - which is the same justification as verify-linters-work.py.
    uv = shutil.which("uv") or "uv"
    proc = subprocess.run(  # noqa: S603
        [uv, "run", "pytest", "-q", f"tests/test_web_ui.py::{test}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    summary = next(
        (ln for ln in reversed(lines) if "passed" in ln or "failed" in ln or "error" in ln),
        lines[-1] if lines else f"<no output, exit {proc.returncode}>",
    )
    return summary, proc.returncode


def apply(case: Case) -> Tuple[bool, Optional[str]]:
    """Break the thing. Returns ``(the_file_existed, original_text)``."""
    if case.find is None:
        case.target.write_text(case.replace, encoding="utf-8")
        return False, None

    original = case.target.read_text(encoding="utf-8")
    if case.find not in original:
        raise SystemExit(
            f"{case.label}: cannot find the text to break in {case.target.name}.\n"
            f"  looked for: {case.find!r}\n"
            "  The source moved, so this case no longer proves anything - fix it."
        )
    case.target.write_text(original.replace(case.find, case.replace, 1), encoding="utf-8")
    return True, original


def restore(case: Case, existed: bool, original: Optional[str]) -> None:
    if not existed:
        case.target.unlink(missing_ok=True)
    elif original is not None:
        case.target.write_text(original, encoding="utf-8")


def main() -> int:
    still_green = []
    for case in CASES:
        try:
            existed, original = apply(case)
        except SystemExit as exc:
            print(f"ERROR: {exc}")
            return 2
        try:
            result, code = run(case.test)
            went_red = code != 0
        finally:
            restore(case, existed, original)

        print(f"{'RED  ' if went_red else 'GREEN'} {case.label:52} -> {result}")
        if not went_red:
            still_green.append(case.label)

    print()
    if still_green:
        print("THESE GUARDS DO NOT GUARD - the test passed with the bug in place:")
        for label in still_green:
            print(f"  - {label}")
        return 1
    print(f"all {len(CASES)} guards went red as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
