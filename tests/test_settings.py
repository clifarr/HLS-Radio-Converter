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
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import pytest
from conftest import serving
from pydantic import ValidationError

from radioconv import web
from radioconv.config import (
    Settings,
    SettingsStore,
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
    ui_settings_path().write_text(json.dumps({"bitrate": 128000}), encoding="utf-8")

    # Saved beats the config file...
    loaded = Settings.load()
    assert loaded.bitrate == 128_000
    assert loaded.channels == 1, "an unrelated value from the file was lost"

    # ...and the environment beats both.
    monkeypatch.setenv("RADIOCONV_BITRATE", "64000")
    assert Settings.load().bitrate == 64_000


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
    assert "station(s) were ended" not in response.text, (
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
    assert "1 station(s) were ended" in response.text


def test_a_retired_station_is_reported_by_count():
    """`retire_all` returns how many stations it ended, so the page can say so."""
    from radioconv.session import SessionManager

    manager = SessionManager(Settings())
    assert manager.retire_all("test") == 0, "an empty manager retired something"
