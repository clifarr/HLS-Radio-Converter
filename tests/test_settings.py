"""The settings page: changing the running configuration from a browser.

The interesting part is not the form. It is that a change has to be *validated
before it is applied*, *persisted*, *reported when it did not take effect*, and
*delivered to the objects that outlive a request* - a page that renders and
saves, while the service carries on with the old values, is the classic shape of
this feature going wrong.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import pytest
from conftest import serving
from pydantic import ValidationError
from starlette.requests import Request

from radioconv import web
from radioconv.config import (
    OutputCodec,
    Settings,
    SettingsStore,
    default_config_dir,
    default_state_dir,
    legacy_ui_settings_path,
    read_ui_settings,
    ui_settings_path,
)


# ------------------------------------------------------------------ the store
def test_applying_a_patch_changes_what_is_in_force():
    store = SettingsStore(Settings(bitrate=192_000))
    updated = store.apply({"bitrate": 128_000})

    assert updated.bitrate == 128_000
    assert store.current.bitrate == 128_000, "the store kept the old value"


def test_a_rejected_value_changes_nothing_at_all():
    """Validation happens on a *complete* candidate, not on the live object.

    The alternative - mutating the current settings field by field - leaves the
    service half-configured when the fourth field out of twenty is rejected, and
    the failure is not a message but a stream at a bitrate nobody asked for.
    """
    store = SettingsStore(Settings(bitrate=192_000))
    with pytest.raises(ValidationError):
        store.apply({"bitrate": 999_999})

    assert store.current.bitrate == 192_000


def test_a_contradiction_between_two_settings_is_still_caught():
    """The model-level check has to run for a patch, not just for a fresh load."""
    store = SettingsStore(Settings(ffmpeg_enabled=True))
    with pytest.raises(ValidationError, match="prefer_passthrough"):
        store.apply({"ffmpeg_enabled": False, "prefer_passthrough": False})


def test_a_patch_starts_from_what_is_in_force_not_from_the_config_file(tmp_path, monkeypatch):
    """Saving one field must not silently revert another that was already set.

    Rebuilding from the config file would throw away whatever the environment, a
    command-line flag or an earlier save had decided - and the operator would
    watch a value they never touched revert to something they never typed.
    """
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    store = SettingsStore(Settings(bitrate=128_000, channels=1))
    store.apply({"queue_chunks": 64})

    assert store.current.bitrate == 128_000, "the bitrate was reset by an unrelated edit"
    assert store.current.channels == 1
    assert store.current.queue_chunks == 64


def test_a_bad_saved_file_does_not_stop_the_service(tmp_path):
    """A corrupted override file loses a preference, not the appliance.

    ``read_ui_settings`` returning ``{}`` is what makes that true. The
    alternative - letting JSONDecodeError escape during ``Settings.load`` - means
    a settings page that can brick the service by writing something it later
    cannot read.
    """
    path = tmp_path / "ui-settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_ui_settings(path) == {}

    path.write_text('["a list, not an object"]', encoding="utf-8")
    assert read_ui_settings(path) == {}

    path.unlink()
    assert read_ui_settings(path) == {}


def test_saving_is_atomic_and_keeps_earlier_values(tmp_path, monkeypatch):
    """Two separate saves must accumulate, and a torn write must not exist.

    Atomic because a power cut mid-write would otherwise leave truncated JSON -
    which the loader tolerates, but which would silently lose *every* saved
    value rather than one.
    """
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    store = SettingsStore(Settings())
    store.save({"bitrate": 128_000})
    store.save({"channels": 1})

    saved = read_ui_settings()
    assert saved == {"bitrate": 128_000, "channels": 1}
    assert not list(tmp_path.glob("*.tmp")), "the temporary file was left behind"


def test_saved_values_are_loaded_below_the_environment(tmp_path, monkeypatch):
    """The precedence the page promises: file < saved < environment < flags.

    Getting this backwards would let the web page quietly defeat a deployment
    that pins a value with ``RADIOCONV_BITRATE`` - and the operator would find
    out by listening to the radio rather than by reading a screen.
    """
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text("bitrate = 96000\nchannels = 1\n", encoding="utf-8")
    # The state directory is created by the installer, not by hand - so a test
    # that writes into it directly has to make it first, exactly like an operator
    # copying a file into place would.
    saved = ui_settings_path()
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps({"bitrate": 128000}), encoding="utf-8")

    # Saved beats the config file...
    loaded = Settings.load()
    assert loaded.bitrate == 128_000
    assert loaded.channels == 1, "an unrelated value from the file was lost"

    # ...and the environment beats both.
    monkeypatch.setenv("RADIOCONV_BITRATE", "64000")
    assert Settings.load().bitrate == 64_000


# ------------------------------------------------- where the file is written
def test_saved_settings_are_not_written_into_the_config_directory(tmp_path, monkeypatch):
    """The regression that made the settings page useless on the systemd install.

    The shipped unit runs with ``ProtectSystem=strict`` and lists only the state
    directory in ``ReadWritePaths``, while ``config.toml`` lives in
    ``/etc/radioconv``. Keeping ``ui-settings.json`` "beside the config file"
    meant every save on the Pi was refused with ``EROFS`` - the page showed the
    green "saved and applied" notice with an amber warning underneath it, and
    the value was gone at the next restart.
    """
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path / "etc"))
    monkeypatch.setenv("RADIOCONV_STATE_DIR", str(tmp_path / "var"))

    assert ui_settings_path() == tmp_path / "var" / "ui-settings.json"

    SettingsStore(Settings()).save({"bitrate": 128_000})

    assert (tmp_path / "var" / "ui-settings.json").is_file(), (
        "nothing was written where it is writable"
    )
    assert not (tmp_path / "etc").exists(), "the read-only config directory was written to"


def test_the_default_state_directory_is_separate_from_the_config_directory(tmp_path, monkeypatch):
    """Two directories, two lifetimes: the file is rewritten, the TOML is not.

    With no ``RADIOCONV_STATE_DIR`` the state directory follows the XDG *state*
    half of the spec, which is deliberately not ``~/.config``: configuration is
    what a person writes, state is what the service rewrites.
    """
    monkeypatch.delenv("RADIOCONV_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path / "cfg"))

    assert default_state_dir("linux") == Path.home() / ".local" / "state" / "radioconv"
    assert ui_settings_path().parent != default_config_dir()

    # ...and XDG_STATE_HOME is honoured when it is set.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert ui_settings_path() == tmp_path / "xdg-state" / "radioconv" / "ui-settings.json"


def test_the_state_directory_follows_the_platform_convention(tmp_path, monkeypatch):
    """Windows has no XDG, and ``~/.local/state`` there is a POSIX artefact.

    ``%LOCALAPPDATA%`` is where Windows puts per-user, per-machine application
    state - *Local* rather than *Roaming*, because this file must not follow a
    roaming profile onto another PC. macOS keeps the XDG state location on
    purpose: ``config.toml`` already lives in ``~/.config/radioconv`` there, so
    splitting the two across ``~/.config`` and ``~/Library/Application Support``
    would be worse than being consistently XDG on both unices.

    All three answers are asserted from one host by passing *platform*, which is
    how ``retrans.py`` tests its Windows path too - otherwise the Windows branch
    is only ever exercised on Windows, i.e. never.
    """
    monkeypatch.delenv("RADIOCONV_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    local = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    assert default_state_dir("win32") == local / "radioconv"

    # A Windows environment that has lost the variable (a service account, a
    # stripped CI runner) must still resolve somewhere usable rather than crash.
    monkeypatch.delenv("LOCALAPPDATA")
    assert default_state_dir("win32") == Path.home() / "AppData" / "Local" / "radioconv"

    for name in ("linux", "linux2", "darwin"):
        assert default_state_dir(name) == Path.home() / ".local" / "state" / "radioconv"

    # An explicit XDG location is a real choice and still wins on Windows.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert default_state_dir("win32") == tmp_path / "xdg" / "radioconv"


def test_the_saved_file_is_replaced_from_a_sibling_not_another_volume(tmp_path, monkeypatch):
    """``os.replace`` is only atomic within one filesystem, which Windows enforces.

    On Linux a rename across mounts fails with ``EXDEV``; on Windows
    ``MoveFileEx`` fails outright. So the temporary file has to be written *next
    to* the target, never in ``tempfile.gettempdir()`` - and the same-directory
    sibling is also what makes a half-written file impossible to observe.
    """
    monkeypatch.setenv("RADIOCONV_STATE_DIR", str(tmp_path / "state"))

    moves: list[tuple[Path, Path]] = []
    original = Path.replace

    def spy(self: Path, target: Any) -> Path:
        moves.append((self, Path(target)))
        return original(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    SettingsStore(Settings()).save({"bitrate": 128_000})

    assert len(moves) == 1, "the save did not go through an atomic replace"
    source, target = moves[0]
    assert source.parent == target.parent, "the temporary file is on another filesystem"
    assert source.name == "ui-settings.json.tmp"
    assert target.is_file()


def test_a_locked_settings_directory_is_reported_not_raised(tmp_path, monkeypatch):
    """Windows refuses the replace while another process holds the file open.

    That is a ``PermissionError``, which is an ``OSError`` - the same thing the
    route already catches and reports as "in effect but could not be saved". The
    test pins the contract that makes that message reachable instead of a 500:
    every failure here has to be an ``OSError`` and must leave the old file
    intact.
    """
    monkeypatch.setenv("RADIOCONV_STATE_DIR", str(tmp_path / "state"))
    store = SettingsStore(Settings())
    store.save({"bitrate": 128_000})
    before = ui_settings_path().read_text(encoding="utf-8")

    def refuse(self: Path, target: Any) -> Path:
        raise PermissionError(13, "The process cannot access the file", str(target))

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(OSError):
        store.save({"bitrate": 96_000})

    assert ui_settings_path().read_text(encoding="utf-8") == before, "the saved file was damaged"


def test_settings_saved_before_the_move_are_still_read(tmp_path, monkeypatch):
    """An upgrade must not silently reset somebody's saved settings."""
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path / "etc"))
    monkeypatch.setenv("RADIOCONV_STATE_DIR", str(tmp_path / "var"))

    legacy = legacy_ui_settings_path()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"bitrate": 128000, "channels": 1}), encoding="utf-8")

    assert Settings.load().bitrate == 128_000, "the old file was ignored"

    # A later save goes to the new location, carrying the old values with it -
    # and it wins over anything left behind in the old file.
    SettingsStore(Settings.load()).save({"bitrate": 96_000})

    written = json.loads(ui_settings_path().read_text(encoding="utf-8"))
    assert written["bitrate"] == 96_000
    assert written["channels"] == 1, "the legacy values were dropped instead of migrated"
    assert Settings.load().bitrate == 96_000
    assert Settings.load().channels == 1


def test_the_service_unit_gives_the_page_a_directory_it_may_write():
    """The unit's sandbox and the app's state directory have to be the same place.

    ``ReadWritePaths`` is the only path the service can write to, so the
    environment variable that tells the app where to save has to name exactly
    that - and the config directory must not be in the writable set, or the next
    person will reasonably put the settings file back beside the TOML.
    """
    unit = (Path(__file__).resolve().parents[1] / "packaging" / "radioconv.service").read_text(
        encoding="utf-8"
    )

    state = re.search(r"^Environment=RADIOCONV_STATE_DIR=(.+)$", unit, re.MULTILINE)
    writable = re.search(r"^ReadWritePaths=(.+)$", unit, re.MULTILINE)

    assert state is not None, "the unit never tells the app where it may write"
    assert writable is not None
    assert state.group(1).strip() == writable.group(1).strip(), (
        "the settings directory is not one of the unit's writable paths"
    )


# ----------------------------------------------------------------- the field list
def test_every_editable_field_is_a_real_setting():
    """A typo in the field list would render an input that is submitted and lost."""
    for field in web.SETTINGS_FIELDS:
        assert field.name in Settings.model_fields, f"{field.name} is not a setting"


def test_every_read_only_setting_exists_too():
    for name, _ in web.READ_ONLY_SETTINGS:
        assert name in Settings.model_fields, name


def test_no_setting_is_both_editable_and_read_only():
    editable = {f.name for f in web.SETTINGS_FIELDS}
    read_only = {name for name, _ in web.READ_ONLY_SETTINGS}
    assert not (editable & read_only), editable & read_only


def test_every_field_has_a_help_sentence():
    """The page exists to explain settings, so a bare label is not enough."""
    for field in web.SETTINGS_FIELDS:
        assert len(field.help) > 40, f"{field.name} has no real explanation"
        assert field.help.endswith((".", "!")), f"{field.name} is not a sentence"


def _complete(**overrides: str) -> dict:
    """A whole form, as a browser submits it, with *overrides* applied.

    Every input is present because the page renders every input. A *partial*
    post is a broken client, and the parser answers it with "Required." for each
    missing value rather than silently leaving it unchanged - silence there would
    let a mangled submit reset a setting the sender never mentioned.
    """
    return {**(_baseline(Settings())), **overrides}


def test_parsing_records_an_error_against_the_field_that_caused_it():
    patch, problems = web.parse_settings_form(_complete(bitrate="not a number"))

    assert problems == {"bitrate": "Must be a whole number."}
    assert patch["channels"] == 2, "a valid field must survive a sibling's error"


def test_a_missing_value_is_an_error_rather_than_a_reset():
    """Absence is only meaningful for a checkbox, which the browser omits itself."""
    _, problems = web.parse_settings_form({"ffmpeg_enabled": "1"})

    assert problems.get("bitrate") == "Required."
    assert problems.get("queue_chunks") == "Required."
    assert "ffmpeg_enabled" not in problems


def test_an_unchecked_box_is_false_rather_than_missing():
    """A form post omits unchecked boxes, so absence has to mean the value."""
    form = _complete()
    # Exactly what a browser sends for the three boxes it was showing as off.
    for name in ("ffmpeg_enabled", "prefer_passthrough", "pace_output"):
        form.pop(name)
    patch, problems = web.parse_settings_form(form)

    assert problems == {}
    assert patch["ffmpeg_enabled"] is False
    assert patch["prefer_passthrough"] is False
    assert patch["pace_output"] is False
    # ...and a box that *was* ticked arrives as "1" and stays on.
    assert patch["icy_song_metadata"] is True


def test_a_cleared_optional_field_means_unset_and_a_cleared_required_one_is_an_error():
    patch, problems = web.parse_settings_form(_complete(mime_override="", public_host=""))

    assert problems == {}
    assert patch["mime_override"] is None, "an optional field cannot be cleared"
    assert patch["public_host"] is None

    _, problems = web.parse_settings_form(_complete(bitrate=""))
    assert problems == {"bitrate": "Required."}


# ---------------------------------------------------------------------- routes
def _post(app: str, values: dict) -> httpx.Response:
    """Submit the form exactly as a browser does: urlencoded, no parser needed."""
    return httpx.post(app + "/settings", data=values, timeout=20)


class _Controls(HTMLParser):
    """The page's form controls as *parsed* attributes, not as text.

    Searching the markup with a regex is how a broken attribute goes unnoticed:
    ``checkedaria-invalid="true"`` satisfies a search for ``checked`` and for
    ``aria-invalid`` both, while a browser sees one unknown attribute - an
    unticked box that is also not marked invalid. That happened here, so the
    assertions below read the attributes the way a browser does.
    """

    def __init__(self) -> None:
        super().__init__()
        self.found: dict[str, dict[str, Optional[str]]] = {}

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag not in ("input", "select"):
            return
        parsed = dict(attrs)
        name = parsed.get("name")
        if name:
            self.found[name] = parsed


def _controls(body: str) -> dict:
    parser = _Controls()
    parser.feed(body)
    return parser.found


def _render(**kwargs: Any) -> str:
    """Render the settings page with exactly the state we want to check.

    The template's job is to show what it is *given*. Some combinations are
    awkward to reach through the route - a ticked box that is also marked invalid
    needs a rule that both fires and leaves the box on, which the one cross-field
    rule here cannot do - so the contract is tested directly instead of being
    left to whatever a browser form happens to produce.
    """
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/settings",
            "query_string": b"",
            "headers": [(b"host", b"radio.local:8123")],
        }
    )
    return bytes(web.settings_page(Settings(), request, **kwargs).body).decode()


def _baseline(settings: Settings) -> dict:
    """Every field, so a post is a complete form rather than a partial patch.

    A browser submits every input in the form, which is why the page can be
    written to treat an empty required value as "not answered" without breaking
    ordinary use.
    """
    values = {}
    for field in web.SETTINGS_FIELDS:
        raw = getattr(settings, field.name)
        if field.kind == "bool":
            if raw:
                values[field.name] = "1"
            continue
        values[field.name] = "" if raw is None else str(raw)
    return values


def test_the_page_lists_the_editable_settings_and_their_values():
    with serving(Settings(bitrate=128_000)) as app:
        page = httpx.get(app + "/settings", timeout=20)

    assert page.status_code == 200
    assert 'name="bitrate"' in page.text
    assert 'value="128000"' in page.text
    assert "Settings" in page.text
    # The read-only group explains itself rather than being absent.
    assert "Not changeable here" in page.text


def test_submitting_the_form_changes_the_configuration(tmp_path, monkeypatch):
    """The whole point, end to end: a browser edit reaches the running service."""
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    base = Settings(bitrate=192_000, max_sessions=4)

    with serving(base) as app:
        values = _baseline(base)
        values["bitrate"] = "128000"
        values["max_sessions"] = "2"
        response = _post(app, values)

        assert response.status_code == 200
        assert "Settings saved" in response.text
        # The new value is what the form now shows...
        assert 'value="128000"' in response.text
        # ...and what the service actually believes: the gauge is served from
        # the live settings, not from a copy taken at startup.
        health = httpx.get(app + "/health", timeout=20).json()
        assert health["streams"]["max_sessions"] == 2


def test_a_rejected_value_changes_nothing_and_says_which_field(tmp_path, monkeypatch):
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    base = Settings(bitrate=192_000)

    with serving(base) as app:
        values = _baseline(base)
        values["bitrate"] = "999999"  # above the 320000 ceiling
        response = _post(app, values)

        assert response.status_code == 200
        assert "Nothing was changed" in response.text
        assert "bitrate" in response.text
        health = httpx.get(app + "/health", timeout=20).json()
        assert health["streams"]["max_sessions"] == base.max_sessions

        # And nothing was written: a rejected value must not become the value
        # the service reads after its next restart.
        assert not ui_settings_path().exists()


def test_a_rejected_submit_keeps_what_was_typed(tmp_path, monkeypatch):
    """The whole form comes back as submitted, not as stored.

    One bad field must not cost the reader the other nineteen. A form that
    reverts everything on every rejection teaches people to change one setting
    per visit - or to stop using the page - and it also makes the red message
    look like it applies to values that were never the problem.
    """
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    base = Settings(bitrate=192_000, max_sessions=4)

    with serving(base) as app:
        values = _baseline(base)
        values["bitrate"] = "999999"  # rejected
        values["max_sessions"] = "2"  # perfectly good, and must not be lost
        values["linger_seconds"] = ""  # an optional field, cleared on purpose
        values["prefer_passthrough"] = "1"
        response = _post(app, values)

    body = response.text
    assert 'value="999999"' in body, "the rejected value was replaced by the stored one"
    assert 'value="2"' in body, "a valid edit was thrown away with the invalid one"
    assert 'value="192000"' not in body
    # An optional field the reader cleared must come back cleared, not repopulated.
    assert re.search(r'name="linger_seconds"\s+value=""', body), "a cleared field came back"
    # A ticked box stays ticked while the reason for the rejection is fixed.
    assert 'name="prefer_passthrough"' in body
    assert re.search(r'name="prefer_passthrough"[^>]*checked', body) is not None, (
        "a checkbox reverted on a rejected submit"
    )


def test_both_engine_switches_off_is_refused_and_the_page_says_why(tmp_path, monkeypatch):
    """The one cross-field rule here, so the one that must be explained.

    "FFmpeg off" and "never prefer forwarding" are individually sensible and
    jointly impossible: the first removes the second engine, the second says
    never to use the first. Refusing it is right - the alternative is a service
    that answers every request with nothing. Refusing it *without a reason* was
    the defect: pydantic files a model-level error under no field, the template
    rendered only per-field ones, so the page said "a value was rejected" and
    marked nothing, leaving the reader to hunt through twenty inputs for an
    error that lived in the combination between two of them.
    """
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    base = Settings()

    with serving(base) as app:
        values = _baseline(base)
        values.pop("ffmpeg_enabled", None)
        values.pop("prefer_passthrough", None)
        response = _post(app, values)

        assert response.status_code == 200
        body = response.text
        assert "Nothing was changed" in body
        assert "only engine" in body, "the reason for the refusal was not shown"
        # It must not wear the success colour: this message is the explanation of
        # a failure, and green next to red is how a page contradicts itself.
        assert 'data-state="good">Nothing was changed' not in body

        # Both fields the rule is about are marked, so the sentence has somewhere
        # to point and the reader is not left to find it among twenty inputs -
        # and they come back *unticked*, as submitted, rather than reverting to
        # the stored value.
        controls = _controls(body)
        for name in ("ffmpeg_enabled", "prefer_passthrough"):
            assert controls[name].get("aria-invalid") == "true", f"{name} is not marked"
            assert controls[name].get("aria-describedby") == f"p-{name}"
            assert "checked" not in controls[name], f"{name} came back ticked"

        # Refused, not half-applied: FFmpeg is still on and nothing was saved.
        health = httpx.get(app + "/health", timeout=20).json()
        assert health["ffmpeg"]["enabled"] is True
        assert not ui_settings_path().exists()


def test_a_marked_field_keeps_its_other_attributes_apart():
    """`checkedaria-invalid="true"` is one unknown attribute, not two.

    `trim_blocks` deletes the newline after a block tag, so two conditional
    attributes separated only by a newline are emitted glued together: the
    control ends up neither ticked nor marked invalid, and the browser silently
    ignores both. This is a template contract rather than a route behaviour,
    which is why it is rendered directly with a ticked box that is also invalid.
    """
    control = _controls(_render(problems={"prefer_passthrough": "no"}))["prefer_passthrough"]

    assert control.get("aria-invalid") == "true", "the invalid marker was swallowed"
    assert control.get("aria-describedby") == "p-prefer_passthrough"
    assert "checked" in control, "the tick was swallowed by the attribute before it"
    assert control.get("value") == "1"


def test_a_message_about_no_particular_field_is_still_shown():
    """The cross-field error has no input to sit next to, and must still appear.

    Pydantic files a rule about a combination under an empty location. Rendering
    only per-field problems meant the page said "a value was rejected", marked
    nothing, and named nothing - a dead end in a form with twenty inputs.
    """
    assert "those two rules contradict" in _render(
        problems={"__all__": "those two rules contradict"}
    )


def test_the_page_says_what_the_engine_switches_add_up_to(tmp_path, monkeypatch):
    """Two switches, one result - so the page prints the result.

    `ffmpeg_enabled` says whether a second engine exists at all;
    `prefer_passthrough` decides which wins when both could work. A flat list of
    inputs makes that pair read like one setting and its negation, which is how
    "are these the same setting in reverse?" becomes a reasonable question. Each
    combination therefore gets its own sentence, and none of them describes an
    engine that is not available.
    """
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))

    cases: dict[tuple[bool, bool, OutputCodec], str] = {
        # (ffmpeg_enabled, prefer_passthrough, output_codec): a phrase only that
        # combination can produce.
        (True, True, "mp3"): "MP3 stations are forwarded at their own bitrate",
        (True, True, "adts"): "AAC stations are forwarded at their own bitrate",
        (True, True, "auto"): "anything else is converted to MP3",
        (True, False, "adts"): "converted to AAC at",
        (False, True, "mp3"): "forwarding only",
    }
    for (ffmpeg, prefer, codec), phrase in cases.items():
        base = Settings(ffmpeg_enabled=ffmpeg, prefer_passthrough=prefer, output_codec=codec)
        with serving(base) as app:
            body = httpx.get(app + "/settings", timeout=20).text

        assert phrase in body, f"ffmpeg={ffmpeg} prefer={prefer} codec={codec}: no outcome"
        if not ffmpeg:
            # The honest failure mode of a forwarding-only install is that some
            # stations cannot be played, and the summary has to say that rather
            # than implying a re-encode is still on the cards.
            assert "no forwarding." not in body
            assert "cannot be played" in body


def test_the_outcome_sentence_tracks_the_choice_not_the_default(tmp_path, monkeypatch):
    """One sentence per combination, not one sentence with a different number."""
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    seen = {}
    for codec in ("mp3", "adts", "auto"):
        with serving(Settings(output_codec=codec)) as app:
            body = httpx.get(app + "/settings", timeout=20).text
        start = body.find('class="outcome"')
        assert start != -1, f"no outcome sentence for {codec}"
        seen[codec] = body[start : body.find("</p>", start)]

    assert len(set(seen.values())) == 3, f"the three codecs share an outcome: {seen}"


def test_the_page_says_the_settings_are_defaults_a_url_can_override(tmp_path, monkeypatch):
    """The page changes the service, not every radio.

    `?codec=` and `?br=` on a station's URL win over the configured values for
    that one radio, which is the reason a per-request override exists at all -
    and it is the fact a reader is most likely to get wrong in the other
    direction ("I set mp3 here, so why is that radio getting AAC?"). Two lines:
    one saying these are defaults, one naming the parameters.
    """
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))

    with serving(Settings()) as app:
        body = httpx.get(app + "/settings", timeout=20).text

    assert "defaults for every radio" in body, "the page never says what these are"
    for param in ("?codec=", "?br="):
        assert param in body, f"the page does not mention the {param} override"


def test_the_page_says_which_fields_only_affect_converted_stations(tmp_path, monkeypatch):
    """The question the form cannot answer by itself: is this value in force?

    "If I choose AAC, does the bitrate do anything?" has no single answer - it
    depends on the *station*, not the codec: a forwarded station is played at
    its own rate and `bitrate` never reaches it, while a converted one is
    encoded at the configured rate. A reader looking at three number boxes and a
    codec dropdown has no way to work that out, so the three boxes say it
    themselves and the group says what the two engines are.

    This is a prefix rather than a per-field badge on purpose: the "new streams"
    pills were fifteen badges saying the same thing, and the reader stopped
    seeing them. Three rows beginning with the same four words are read once and
    then scan as a block.
    """
    helps = {field.name: field.help for field in web.SETTINGS_FIELDS}

    for name in ("bitrate", "channels", "sample_rate"):
        assert helps[name].startswith("Converted stations only."), (
            f"{name} does not say that a forwarded station ignores it: {helps[name]!r}"
        )
    # The engine fields are not converted-only, and saying so would be wrong.
    for name in ("ffmpeg_enabled", "prefer_passthrough", "output_codec"):
        assert "Converted stations only" not in helps[name], (
            f"{name} is about choosing the engine, not about converted output"
        )

    blurb = dict(web.SETTINGS_GROUPS)["Encoding"]
    for word in ("Forwarding", "Converting", "bitrate, channels and sample rate"):
        assert word in blurb, f"the Encoding blurb does not define both engines: {word!r}"


def test_the_outcome_sentence_says_where_the_bitrate_applies():
    """A rate is only mentioned for a path that uses one.

    The inverse mistake is the expensive one: a forwarding-only installation has
    no encoder, so naming a target bitrate there promises a conversion that
    cannot happen. And when both engines exist, the sentence has to name *both*
    outcomes - the forwarded one at "its own bitrate", the converted one at the
    configured rate - or a reader is left to assume one applies to both.
    """
    valid: list[tuple[bool, bool, OutputCodec]] = [
        (True, prefer, codec) for prefer in (True, False) for codec in ("mp3", "adts", "auto")
    ] + [(False, True, codec) for codec in ("mp3", "adts", "auto")]

    for ffmpeg, prefer, codec in valid:
        sentence = web.engine_summary(
            Settings(ffmpeg_enabled=ffmpeg, prefer_passthrough=prefer, output_codec=codec)
        )
        case = f"ffmpeg={ffmpeg} prefer={prefer} codec={codec}"

        if not ffmpeg:
            assert "kbit/s" not in sentence, f"{case}: promises a rate it cannot produce"
            assert "own bitrate" in sentence, f"{case}: omits the rate a radio really gets"
        elif prefer:
            assert "own bitrate" in sentence, f"{case}: says forwarding, not what it plays at"
            assert "kbit/s" in sentence, f"{case}: never says what a converted station becomes"
        else:
            assert "own bitrate" not in sentence, f"{case}: claims a forwarded path it disabled"
            assert "kbit/s" in sentence, f"{case}: never says what stations become"


def test_the_dashboard_names_settings_the_way_the_settings_page_does():
    """Two pages, one name per setting - the drift that already happened once.

    The dashboard quotes a handful of settings beside live figures, and the
    settings page is where they are changed. When the page's labels were
    shortened, the dashboard kept the old ones ("Stations allowed", "Restart a
    silent station after"), so a reader told to raise "Stations at once" had a
    table in front of them that said nothing of the kind.

    `_config` now looks each name up in `SETTINGS_FIELDS`, so the only way to
    break this is to stop doing that - which is what the assertion catches.
    """
    config = web._config(Settings())

    labels = {field.label for field in web.SETTINGS_FIELDS}
    # "Output" is the one derived row: it is a codec *and* a rate, which is not
    # what the `output_codec` field means on its own.
    unknown = set(config) - labels - {"Output"}
    assert not unknown, f"the dashboard names settings the settings page does not: {unknown}"
    for name in ("max_sessions", "stall_timeout", "listener_write_timeout"):
        label = next(field.label for field in web.SETTINGS_FIELDS if field.name == name)
        assert label in config, f"{name} is not quoted on the dashboard at all"


def test_a_station_is_not_called_converted_when_it_is_forwarded():
    """ "Stations being converted right now" was the count of *sessions*.

    A forwarded station is not converted, and since 2.4.0 most of them can be -
    so the tooltip described the opposite of what the tile counts, on the two
    pages where a person first meets the vocabulary. Both templates say "being
    served" now, which is true of either engine.
    """
    for template in ("index.html", "dashboard.html"):
        body = (web.resource_dir(web.TEMPLATES) / template).read_text(encoding="utf-8")
        assert "being served right now" in body, f"{template} lost the corrected wording"
        assert "being converted right now" not in body, (
            f"{template} calls a forwarded station 'converted'"
        )


def test_the_restart_is_reported_once_in_the_success_colour(tmp_path, monkeypatch):
    """One event, one sentence: the amber duplicate is gone.

    A restart used to be announced twice - the template's green "N stations
    restarted" and a warning reading "N station(s) were ended so they rebuild",
    which renders amber. Two colours for one event, one of which means "check
    this".
    """
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    base = Settings(bitrate=192_000)

    with serving(base) as app:
        values = _baseline(base)
        values["bitrate"] = "128000"
        response = _post(app, values)

    assert 'data-state="warn"' not in response.text
    assert "Settings saved and applied." in response.text


def test_the_change_survives_a_restart(tmp_path, monkeypatch):
    """Saved means saved: the next process must start with the new value."""
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    base = Settings(bitrate=192_000)
    with serving(base) as app:
        values = _baseline(base)
        values["bitrate"] = "96000"
        _post(app, values)

    reloaded = Settings.load()
    assert reloaded.bitrate == 96_000


def test_a_value_pinned_by_the_environment_is_reported_rather_than_claimed(tmp_path, monkeypatch):
    """Saving something that cannot take effect must not look like success.
    This is the honest-cost case of the precedence order: the page accepts the
    value, writes it, and then has to admit that a ``RADIOCONV_*`` variable is
    deciding instead. Silence here is how somebody spends an afternoon wondering
    why the bitrate will not change.
    """
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADIOCONV_BITRATE", "64000")
    base = Settings.load()

    with serving(base) as app:
        values = _baseline(base)
        values["bitrate"] = "128000"
        response = _post(app, values)

        assert "Saved, but not in effect" in response.text
        assert "bitrate" in response.text
        assert "RADIOCONV_" in response.text, "the message must name the reason"
        # It was still saved, so removing the variable makes it take effect.
        assert read_ui_settings()["bitrate"] == 128_000


def test_the_write_is_refused_when_the_page_is_locked():
    """403, not a silent no-op: a form that appears to work is worse than one
    that refuses, because the reader concludes the *setting* is broken."""
    with serving(Settings(allow_ui_settings=False)) as app:
        page = httpx.get(app + "/settings", timeout=20)
        assert page.status_code == 200
        assert "read-only view" in page.text
        assert "disabled" in page.text, "the inputs are still editable in the markup"

        refused = _post(app, {"bitrate": "128000"})
        assert refused.status_code == 403


def test_the_form_accepts_nothing_but_a_urlencoded_body():
    """A multipart body read as urlencoded would produce nonsense field names.

    Refusing it with a status the sender understands is the difference between a
    bug and a mystery, and it keeps the promise that this page needs no
    multipart parser.
    """
    with serving(Settings()) as app:
        response = httpx.post(
            app + "/settings",
            content='--x\r\nContent-Disposition: form-data; name="bitrate"\r\n\r\n128000\r\n--x--\r\n',
            headers={"content-type": "multipart/form-data; boundary=x"},
            timeout=20,
        )
    assert response.status_code == 415
    assert "x-www-form-urlencoded" in response.text


def test_an_oversized_body_is_refused_rather_than_read():
    """`request.body()` reads whatever it is given, and nothing here is authenticated."""
    with serving(Settings()) as app:
        response = httpx.post(
            app + "/settings",
            content=b"bitrate=128000&" + b"x=1&" * 20_000,
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
    assert response.status_code == 413


def test_applying_with_restart_reports_how_many_stations_it_ended(tmp_path, monkeypatch):
    """A change reaches a pipeline only by replacing it, so the page can ask for that.

    A session copies its settings when it is built, deliberately (its buffer and
    frame grid describe audio that is already playing). That leaves "apply this
    now" with exactly one honest implementation: end the stations and let the
    radios reconnect, which they do by themselves.

    With nothing playing, the page must say nothing rather than claim a restart
    it did not perform - the count comes from the manager, not from the form.
    """
    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    base = Settings(bitrate=192_000)

    with serving(base) as app:
        values = _baseline(base)
        values["bitrate"] = "128000"
        values["restart"] = "1"
        response = _post(app, values)

    assert response.status_code == 200
    assert "Settings saved" in response.text
    assert "restarted with the new settings" not in response.text, (
        "no station was playing, so there was nothing to restart"
    )
    assert "station(s)" not in response.text, (
        "the page claimed to end 0 stations - a false report of work done"
    )


def test_restart_ends_the_stations_that_are_playing(tmp_path, monkeypatch):
    """The tick has to reach the manager, not just the form parser.

    In-process (``TestClient``) rather than over a socket, because the assertion
    is about the *manager* the route shares: a real session is registered, the
    form is submitted, and the session must come back retired. Going through
    uvicorn would prove the reply only, which is exactly the part that can lie.
    """
    from fastapi.testclient import TestClient

    from radioconv import api

    monkeypatch.setenv("RADIOCONV_CONFIG_DIR", str(tmp_path))
    base = Settings(host="127.0.0.1", log_level="WARNING", bitrate=192_000)
    app = api.create_app(base)

    class _Station:
        """Enough of a session for the registry and the teardown to agree."""

        key = "station"
        id = "station-0"
        listener_count = 1
        dead = False

        def __init__(self) -> None:
            self.closed_with: Optional[str] = None

        def close(self, reason: str = "") -> Any:
            self.closed_with = reason

            async def _finished() -> None:
                return None

            return _finished()

    station = _Station()
    app.state.manager.sessions[station.key] = station  # type: ignore[attr-defined]

    with TestClient(app) as client:
        values = _baseline(base)
        values["bitrate"] = "128000"
        values["restart"] = "1"
        response = client.post(
            "/settings",
            content=urlencode(values),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 200
    assert station.closed_with is not None, "the station was left on the old settings"
    assert station.key not in app.state.manager.sessions
    # The notice is prose in the template, so it wraps; compare it as words.
    compact = " ".join(response.text.split())
    assert "1 station restarted with the new settings." in compact
    assert "were ended so they rebuild" not in compact, (
        "the restart is reported twice - once as a warning, which is the colour "
        "that means something went wrong"
    )


def test_a_retired_station_is_reported_by_count():
    """`retire_all` returns how many stations it ended, so the page can say so."""
    from radioconv.session import SessionManager

    manager = SessionManager(Settings())
    assert manager.retire_all("test") == 0, "an empty manager retired something"
