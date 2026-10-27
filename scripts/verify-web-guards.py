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
    #: Which test module holds ``test``. The page tests live in one file and the
    #: settings-page tests in another; naming the wrong one would silently look
    #: like a guard that fires for the wrong reason, so it is explicit.
    test_file: str = "tests/test_web_ui.py"


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
    Case(
        # Reported on a phone: "the tooltips tend to disappear", and on the
        # stations list they always did. A touch pointer reports "out" the
        # instant the finger lifts, with no relatedTarget - and reading that as
        # a departure closed the description the moment the tap ended, so the
        # only way to read it was to hold a finger down. This is that line
        # coming back.
        #
        # The rule itself now lives in the touch branch of `tipAction`: a
        # `pointerout` from a finger must be ignored. Breaking it is what puts
        # the old behaviour back.
        "a lifted finger closes the description again",
        PKG / "static" / "dashboard.js",
        '      if (type === "click") return hasTip ? "show" : "hide-all";\n',
        '      if (type === "click") return hasTip ? "show" : "hide-all";\n'
        '      if (type === "pointerout") return leaves ? "hide" : "none";\n',
        "test_a_tapped_description_stays_until_it_is_dismissed",
    ),
    Case(
        # Reported: holding a finger on a figure to scroll past it opened the
        # description, and it stayed. A tap and a scroll are the same gesture
        # until the finger moves, so the description was opened on the way
        # down. It has to be opened by the click instead - the browser only
        # sends one when the finger was not dragged.
        "the touch opens the description again",
        PKG / "static" / "dashboard.js",
        '      if (type === "click") return hasTip ? "show" : "hide-all";\n'
        "      // The gesture turned into a scroll: the browser cancelled the tap. Any\n"
        "      // description the finger landed on was never asked for, so it closes.\n"
        '      if (type === "pointercancel") return "hide";',
        '      if (type === "pointerdown") return hasTip ? "show" : "hide-all";\n'
        '      if (type === "pointercancel") return "hide";',
        "test_a_finger_held_down_to_scroll_does_not_open_a_description",
    ),
    Case(
        # The other half of the same report: a tap gives the element the focus
        # as a side effect, and the focus was being read as the reader asking
        # for a description - so a tap on the text around a figure opened the
        # wrong one.
        "the focus a tap leaves behind opens a description again",
        PKG / "static" / "dashboard.js",
        '      if (touchFocus) return "none";',
        '      if (touchFocus) return hasTip ? "show" : "none";',
        "test_a_finger_held_down_to_scroll_does_not_open_a_description",
    ),
    Case(
        # The other half of the same report: the stations table is rebuilt
        # every second, and the rebuild made the browser report a `focusout`,
        # which closed the description. The rebuild has to say it is rebuilding,
        # and the focusout handler has to read that. Breaking the *reading* is
        # the honest breakage: the flag is still there, but nothing consults it.
        "the rebuild stops suppressing the focusout it causes",
        PKG / "static" / "dashboard.js",
        "      rebuilding: repainting,",
        "      rebuilding: false,",
        "test_the_description_survives_the_table_being_rebuilt",
    ),
    Case(
        # Scrolling used to close the description, even though the element it
        # described was still on screen - and on a phone the sideways scroll of
        # the stations table is the gesture that shows it.
        "scrolling closes the description again",
        PKG / "static" / "dashboard.js",
        'document.addEventListener("scroll", refreshTip, { passive: true, capture: true });',
        'document.addEventListener("scroll", hideAll, { passive: true, capture: true });',
        "test_the_description_follows_its_trigger_when_the_page_scrolls",
    ),
    # ---- the converter page's stream check. The whole feature is a judgement
    # call about which of two similar fields to believe, and the wrong one reads
    # perfectly plausibly - so each way of getting it wrong is reintroduced.
    Case(
        # `passthrough_possible` says "no FFmpeg needed" about the *source*, and
        # that is false whenever the output codec differs - which is exactly the
        # case where a reader needs the truth. Believing it is the confusion the
        # page exists to remove, so the page must not be able to have it.
        "the verdict believes the source instead of the engine",
        PKG / "web.py",
        'if result.get("passthrough_engine") and kind:',
        'if result.get("passthrough_possible") and kind:',
        "test_the_verdict_reports_the_engine_not_the_source",
    ),
    Case(
        # A dead end: a codec override cannot help a stream with nothing
        # forwardable in it, so offering one wastes the reader's time and makes
        # the advice untrustworthy where it does work.
        "the page suggests a codec override that cannot help",
        PKG / "web.py",
        '    if not engine or not engine.get("kind") or not encoded:',
        "    if not engine or not encoded:",
        "test_the_verdict_says_when_ffmpeg_is_simply_required",
    ),
    Case(
        # The check costs a segment download and an FFmpeg probe. Making it
        # implicit would slow down the page every radio owner bookmarks and
        # reach out to a third party's CDN for a URL that was only being
        # converted.
        "the page inspects the stream without being asked",
        PKG / "api.py",
        'if probe and target.startswith(("http://", "https://")):',
        'if target.startswith(("http://", "https://")):',
        "test_a_bare_url_never_touches_the_network",
    ),
    # ---- ffmpeg is optional. Three ways the pages can lie about it.
    Case(
        # With FFmpeg disabled there is no second engine, so "FFmpeg re-encodes
        # it" describes a process that cannot start. Guarding the disabled branch
        # itself is the point: an edit that dropped it would fall through to the
        # transcode verdict, which is the sentence that was wrong before.
        "the page promises a transcode on an installation that has no ffmpeg",
        PKG / "web.py",
        "    if not settings.ffmpeg_enabled:\n        # No second engine exists here",
        "    if False:\n        # No second engine exists here",
        "test_with_ffmpeg_disabled_an_unplayable_stream_says_exactly_that",
    ),
    Case(
        # The notice has to be outside the opt-in diagnostics block: it changes
        # what every URL on the page means, including for somebody who never
        # ticks the checkbox - the person most likely to be baffled by a station
        # that will not play.
        "the ffmpeg-disabled notice becomes opt-in with the check",
        PKG / "templates" / "index.html",
        "{% if not ffmpeg_enabled %}",
        "{% if not ffmpeg_enabled and probe %}",
        "test_the_notice_about_ffmpeg_appears_without_asking_for_a_check",
    ),
    # ---- the settings page. One case per promise it makes.
    Case(
        # The page is a write endpoint on a service that anyone on the LAN can
        # reach. `allow_ui_settings` is the only thing standing between a
        # dashboard the owner trusts and one anybody can reconfigure, so the
        # guard must bite at the route and not merely grey out the inputs: a
        # disabled input is a hint to a browser, not a rule.
        "the locked settings page still accepts a submit",
        PKG / "api.py",
        "    if not current.allow_ui_settings:",
        "    if False:",
        "test_the_write_is_refused_when_the_page_is_locked",
        "tests/test_settings.py",
    ),
    Case(
        # `request.form()` would need python-multipart, which this project
        # deliberately does not depend on. Accepting any content type is how you
        # end up reaching for it: a multipart body parsed as urlencoded yields
        # field names like '------WebKitFormBoundary...' and nothing else.
        "the settings form accepts a body it cannot parse",
        PKG / "api.py",
        '    if not content_type.startswith("application/x-www-form-urlencoded"):',
        "    if False:",
        "test_the_form_accepts_nothing_but_a_urlencoded_body",
        "tests/test_settings.py",
    ),
    Case(
        # Called out because the alternative is reading an unbounded body into
        # memory on a 512MB Pi. The declared length is a hint from the client, so
        # the byte count is checked again after reading - breaking the constant
        # removes both checks at once, which is the point.
        "an unbounded form body is read anyway",
        PKG / "api.py",
        "MAX_FORM_BYTES = 16 * 1024",
        "MAX_FORM_BYTES = 16 * 1024 * 1024",
        "test_an_oversized_body_is_refused_rather_than_read",
        "tests/test_settings.py",
    ),
    Case(
        # A value pinned by the command line or the environment wins over the
        # page. Forgetting that is not a cosmetic bug: the page would report a
        # change that the next restart silently reverts, which is the sort of
        # thing discovered weeks later as "the setting keeps going back".
        #
        # The break is in `apply`, not in the constructor: emptying `_pinned`
        # there would be undone by the `_env_pins()` line under it, which is a
        # mutation that changes nothing and would guard nothing.
        "a value pinned by the environment is quietly overwritten",
        PKG / "config.py",
        "        merged = {**self._settings.model_dump(), **clean, **self._pinned}",
        "        merged = {**self._settings.model_dump(), **clean}",
        "test_a_value_pinned_by_the_environment_is_reported_rather_than_claimed",
        "tests/test_settings.py",
    ),
    Case(
        # "Apply to the stations playing now" is the only part of a save that
        # touches a live radio, and it is the radio that suffers if it is skipped:
        # the page says the change took effect while every existing listener keeps
        # the old settings for the life of its session.
        #
        # Aimed at the *count*, not the call: `restarted = 0` still calls
        # `retire_all`, so a test that only looks at the reply could not tell the
        # difference. The page must report the manager's number, not its own.
        "applying a change leaves the running stations on the old settings",
        PKG / "api.py",
        '            restarted = manager.retire_all("settings changed from the web page")',
        "            restarted = 0",
        "test_restart_ends_the_stations_that_are_playing",
        "tests/test_settings.py",
    ),
    Case(
        # A half-written file is a service that will not start, so the write goes
        # to a sibling and is renamed over the target. The observable promise is
        # the one a power cut tests: the file on disk is either the old content or
        # the new one, never a truncated JSON document. Breaking the *rename* is
        # what makes that visible; the test reads the file back through
        # `Settings.load()`, which is what the next process does.
        "the settings file is written in place",
        PKG / "config.py",
        "        temporary.replace(path)",
        '        path.write_text(temporary.read_text(encoding="utf-8")[:5], encoding="utf-8")',
        "test_the_change_survives_a_restart",
        "tests/test_settings.py",
    ),
    Case(
        # The dashboard's old read-only table claimed settings were "fixed for the
        # life of the process". That sentence is still true of a running station
        # and false of the service, and the page must not repeat it now that the
        # settings are editable - it is the single line that made the table
        # useless.
        "the dashboard claims settings cannot be changed",
        PKG / "templates" / "dashboard.html",
        "Runtime settings can be changed on the",
        "These are fixed for the life of the process. Runtime settings can be changed on the",
        "test_the_dashboard_points_at_the_settings_page_instead_of_claiming_immutability",
    ),
    Case(
        # Re-rendering a rejected submit from the *stored* settings throws away
        # every other edit in the form, which is how one bad number costs twenty
        # good ones. It also makes the red message look like it condemns values
        # that were never the problem.
        "a rejected submit is re-rendered from the stored settings",
        PKG / "api.py",
        "                problems=problems,\n                submitted=form,",
        "                problems=problems,",
        "test_a_rejected_submit_keeps_what_was_typed",
        "tests/test_settings.py",
    ),
    Case(
        # A rule about a *combination* has no field of its own, so pydantic files
        # it under `__all__` and the template has to render that. Dropping it
        # leaves the page saying "a value was rejected" while naming nothing -
        # a dead end in a form with twenty inputs. (Found by asking what happens
        # when the two engine switches are both off.)
        #
        # Aimed at the template block, not at `_field_errors`: a cross-field error
        # is *also* attached to the fields its sentence names, so removing the
        # attachment still shows the sentence and would leave this case green.
        "a message about no particular field is dropped",
        PKG / "templates" / "settings.html",
        '{% if problems.get("__all__") %}',
        "{% if False %}",
        "test_a_message_about_no_particular_field_is_still_shown",
        "tests/test_settings.py",
    ),
    Case(
        # A checkbox is absent from a form post when it is unticked, so "absent"
        # means unticked only when the render follows a post at all. Testing for
        # the key instead let a box the reader had just unticked fall through to
        # the stored value and come back ticked - the opposite of the promise a
        # re-rendered form makes.
        "a checkbox the reader unticked comes back ticked",
        PKG / "web.py",
        '                    checked = str(submitted.get(field.name, "")).lower() in _TRUE',
        "                    checked = bool(raw)",
        "test_both_engine_switches_off_is_refused_and_the_page_says_why",
        "tests/test_settings.py",
    ),
    Case(
        # `trim_blocks` deletes the newline after a block tag, so two conditional
        # attributes separated only by a newline are emitted glued together:
        # `checkedaria-invalid="true"` is one unknown attribute, and the control
        # ends up neither ticked nor marked. Each attribute carries its own
        # leading space for that reason, and this case removes one.
        "two conditional attributes are glued together",
        PKG / "templates" / "settings.html",
        ' checked{% endif %}{% if bad %} aria-invalid="true"',
        ' checked{% endif %}{% if bad %}aria-invalid="true"',
        "test_a_marked_field_keeps_its_other_attributes_apart",
        "tests/test_settings.py",
    ),
    Case(
        # The settings rows are a two-column grid so the names line up. Left as
        # two columns on a phone, the control is squeezed into a strip; the media
        # query is what prevents that, and it is invisible to any test that does
        # not run a browser - so it is asserted from the stylesheet.
        "the settings grid stops collapsing on a phone",
        PKG / "static" / "dashboard.css",
        "  .setting {\n    grid-template-columns: minmax(0, 1fr);",
        "  .setting {\n    grid-template-columns: 13.5rem minmax(0, 1fr);",
        "test_the_settings_rows_stack_on_a_phone",
    ),
    Case(
        # The settings page changes the *service*. `?codec=` and `?br=` on a
        # station's URL still win for that one radio, and a reader who does not
        # know that reads "I set mp3 here, so why is that radio getting AAC?" as
        # a bug. Dropping the sentence is how the page stops saying it.
        "the page stops mentioning the per-URL override",
        PKG / "web.py",
        '"?codec=adts and ?br=128k override it for one radio.",',
        '"they stay until the station restarts.",',
        "test_the_page_says_the_settings_are_defaults_a_url_can_override",
        "tests/test_settings.py",
    ),
    Case(
        # "Defaults" is the word that makes the override sentence mean anything.
        # Without it the page reads as a description of what every radio gets.
        "the page stops calling the settings defaults",
        PKG / "templates" / "settings.html",
        "<strong>These are the defaults for every radio.</strong> Changes are saved and",
        "Changes are saved and",
        "test_the_page_says_the_settings_are_defaults_a_url_can_override",
        "tests/test_settings.py",
    ),
    Case(
        # The README overpromised pass-through twice, and both times it was a
        # sentence that *read* well: pass-through is the best thing this service
        # does, so describing it as what happens is the natural mistake. It is
        # not what happens - `output_codec` ships as mp3 - and a reader who
        # believes it expects 1% CPU and gets 165%.
        "the README promises pass-through by default",
        ROOT / "README.md",
        "**It does not happen by default.**",
        "It happens by default.",
        "test_readme_does_not_promise_passthrough_by_default",
        "tests/test_metadata.py",
    ),
    Case(
        # The DAB+ argument is true but says nothing about the network player's
        # HTTP support, which is separate firmware. "Most" is the word that
        # turns a fair claim into an unsupported one.
        "the README claims most radios need no transcoding",
        ROOT / "README.md",
        "## Many radios need no transcoding",
        "## Most radios need no transcoding",
        "test_readme_does_not_promise_passthrough_by_default",
        "tests/test_metadata.py",
    ),
    Case(
        # A reader copies whichever string is nearest, so a second address in
        # the prose is a wrong URL waiting to be pasted. The screenshots were
        # taken from a machine at .82 while the text said .50, which is exactly
        # how this happens: the document is edited in one place and rendered in
        # another.
        "the README uses two different example addresses",
        ROOT / "README.md",
        "http://192.168.1.50:8123/https%3A%2F%2Fexample.com%2Flive%2Findex.m3u8",
        "http://192.168.1.82:8123/https%3A%2F%2Fexample.com%2Flive%2Findex.m3u8",
        "test_readme_uses_one_explained_example_address",
        "tests/test_metadata.py",
    ),
    Case(
        # The page has to say that a forwarded station ignores the encoder
        # fields. "Re-encoded audio only" was the old wording: true, and useless
        # to a reader who does not already know which stations are re-encoded.
        "the bitrate stops saying forwarded stations ignore it",
        PKG / "web.py",
        '"Converted stations only. A forwarded station keeps its own rate.",',
        '"Output bitrate for re-encoded audio.",',
        "test_the_page_says_which_fields_only_affect_converted_stations",
        "tests/test_settings.py",
    ),
    Case(
        # The summary must not name a rate on a forwarding-only installation:
        # there is no encoder, so the number would promise a conversion that
        # cannot happen.
        "the forwarding-only summary promises a bitrate",
        PKG / "web.py",
        '"In short: forwarding only. Every station plays exactly as it arrives, at "',
        '"In short: forwarding only at 192 kbit/s. Every station plays as it arrives, at "',
        "test_the_outcome_sentence_says_where_the_bitrate_applies",
        "tests/test_settings.py",
    ),
    Case(
        # The dashboard quotes settings beside live figures, and the settings page
        # is where they are changed. Restating a name instead of looking it up is
        # exactly how "Stations allowed" outlived the page's rename.
        "the dashboard renames a setting the settings page has",
        PKG / "web.py",
        '_field_label("stall_timeout")',
        '"Restart a silent station after"',
        "test_the_dashboard_names_settings_the_way_the_settings_page_does",
        "tests/test_settings.py",
    ),
    Case(
        # One event, one sentence. The restart was announced twice: green from
        # the template's count, amber from a warning - and amber means trouble.
        "the restart is announced twice again",
        PKG / "api.py",
        '            restarted = manager.retire_all("settings changed from the web page")',
        '            warnings.append("1 station(s) were ended so they rebuild.")\n'
        '            restarted = manager.retire_all("settings changed from the web page")',
        "test_restart_ends_the_stations_that_are_playing",
        "tests/test_settings.py",
    ),
    Case(
        # A dash on this page means "no reading yet". Using it for 0 made a
        # healthy station and an unreported one look the same, in the column
        # that exists to show a bad one.
        "a zero becomes a missing reading again",
        PKG / "static" / "dashboard.js",
        'cell(2, "num", count(dropped),',
        'cell(2, "num", dropped ? count(dropped) : "\\u2013",',
        "test_a_zero_is_a_reading_not_a_missing_value",
        "tests/test_web_ui.py",
    ),
]


def run(case: Case) -> Tuple[str, int]:
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
        [uv, "run", "pytest", "-q", f"{case.test_file}::{case.test}"],
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
            result, code = run(case)
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
