"""Configuration loading, precedence and validation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from radioconv.config import Settings

#: The bitrate version 1 shipped with, kept as the v2 default. It is a deliberate
#: quality bar (a radio sounds noticeably worse below it), so it must not drift
#: down without a conscious decision.
V1_DEFAULT_BITRATE = 192_000

#: The project root, for asserting that shipped examples agree with the code.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_defaults_are_sane_and_conservative():
    settings = Settings()
    assert settings.port == 8123
    assert settings.bitrate == 192_000
    assert settings.output_codec == "mp3"
    assert settings.prefer_passthrough is True
    assert settings.icy_song_metadata is True  # song titles on the radio's display
    assert settings.variant_selection == "highest"
    assert settings.mime_type == "audio/mpeg"
    # Bounded by default: no unbounded queue anywhere.
    assert settings.queue_chunks > 0
    assert settings.stall_timeout > 0
    assert settings.max_sessions >= 1


def test_the_default_buffer_covers_the_gap_between_segments():
    """The buffer must outlast the silence between two HLS segments.

    A station publishing a 10-second segment every 10 seconds leaves a gap of
    about 10 seconds; measured on a real one, up to 16. The buffer has to cover
    that, and it did not: chunks are normalised to `chunk_size` now, but when
    ffmpeg's 578-byte pipe reads were counted directly the same setting was a
    three-second buffer and every listener stuttered.

    Asserted in *seconds*, because that is the unit the buffer actually has to be
    sized in - and because the previous comment claimed seconds while the code
    counted items.
    """
    settings = Settings()
    buffered_seconds = settings.queue_chunks * settings.chunk_size * 8 / settings.bitrate
    assert buffered_seconds > 20, (
        f"the default buffer holds {buffered_seconds:.0f}s, less than the gap "
        "between two segments on a normal station"
    )
    # And it must not be a memory problem on a 921 MB Pi.
    per_listener_mib = settings.queue_chunks * settings.chunk_size / (1024 * 1024)
    worst_case_mib = per_listener_mib * settings.max_listeners_per_session
    assert worst_case_mib < 16, f"{worst_case_mib:.0f} MiB for one station's radios"


def test_pacing_is_on_by_default():
    """Off is a diagnostic escape hatch, not a sensible default: without pacing a
    listener receives each segment as a burst and underruns between them."""
    assert Settings().pace_output is True
    assert Settings().initial_burst_seconds > 0


#: Settings that legitimately appear in the example file but are not simple
#: scalars, or are documented only for context.
def test_the_shipped_example_config_mentions_only_real_settings():
    """The example config must not name settings that do not exist.

    `Settings` ignores unknown keys (`extra = "ignore"`), so an example file that
    has drifted - it still said `queue_chunks = 64` after the default became 128 -
    fails silently: a user copies it, changes nothing, and gets defaults they did
    not read. Comparing the file against the real field list is the only way that
    stays true.

    Only *active* assignments are checked. Commented-out ones are prose as often
    as they are settings - the file explains the `variant_selection` values with
    `# highest = best audio quality..., # lowest = least bandwidth...`, which are
    not keys at all - and a commented line has no effect when the file is loaded.
    """
    example = (PROJECT_ROOT / "packaging" / "config.toml.example").read_text(encoding="utf-8")
    known = set(Settings.model_fields)

    active = {
        match.group(1)
        for match in re.finditer(r"^([a-z][a-z0-9_]*)\s*=", example, flags=re.MULTILINE)
    }
    unknown = sorted(active - known)
    assert not unknown, (
        f"packaging/config.toml.example sets settings that do not exist: {unknown}. "
        "They are silently ignored when the file is loaded."
    )
    assert active, "no settings found in the example; did the format change?"

    # And the values shown for the streaming ones must match the code, or a user
    # copying the file gets different behaviour from the documented defaults.
    for field in ("queue_chunks", "chunk_size", "pace_output", "initial_burst_seconds"):
        match = re.search(rf"^{field} = (.+?)\s*(?:#.*)?$", example, flags=re.MULTILINE)
        assert match, f"{field} is missing from the example config"
        written_value = match.group(1).strip()
        actual = getattr(Settings(), field)
        if isinstance(actual, bool):
            assert written_value.lower() == str(actual).lower(), f"{field}: {written_value}"
        else:
            assert written_value == str(actual), f"{field} shows {written_value}, code has {actual}"


def test_adts_output_reports_the_right_content_type():
    assert Settings(output_codec="adts").mime_type == "audio/aac"


def test_environment_variables_are_honoured(monkeypatch):
    monkeypatch.setenv("RADIOCONV_PORT", "9000")
    monkeypatch.setenv("RADIOCONV_BITRATE", "128000")
    monkeypatch.setenv("RADIOCONV_OUTPUT_CODEC", "adts")
    settings = Settings()
    assert (settings.port, settings.bitrate, settings.output_codec) == (9000, 128_000, "adts")


def test_toml_file_and_precedence(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        "[radioconv]\nport = 7000\nbitrate = 96000\nmax_sessions = 2\nzzz_unknown = 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RADIOCONV_PORT", "7100")

    # file < environment < explicit override
    settings = Settings.load(str(config))
    assert settings.port == 7100  # env wins over the file
    assert settings.bitrate == 96_000  # file wins over the default
    assert settings.max_sessions == 2

    settings = Settings.load(str(config), port=7200)
    assert settings.port == 7200  # explicit wins over everything


def test_flat_toml_without_a_section(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"port": 7300, "channels": 1}', encoding="utf-8")
    settings = Settings.load(str(config))
    assert settings.port == 7300 and settings.channels == 1


def test_none_overrides_are_ignored_so_argparse_defaults_cannot_clobber(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("port = 7400\n", encoding="utf-8")
    settings = Settings.load(str(config), port=None, bitrate=None)
    assert settings.port == 7400


def test_missing_config_file_is_an_error():
    with pytest.raises(FileNotFoundError):
        Settings.load("/definitely/not/here.toml")


@pytest.mark.parametrize(
    "bad", [{"bitrate": 100}, {"bitrate": 10_000_000}, {"port": 0}, {"port": 99_999}]
)
def test_invalid_values_are_rejected(bad):
    with pytest.raises(ValidationError):
        Settings(**bad)


def test_sample_rate_whitelist():
    assert Settings(sample_rate=44_100).sample_rate == 44_100
    with pytest.raises(ValidationError):
        Settings(sample_rate=12_345)


def test_the_listener_write_deadline_is_bounded_both_ways():
    """The window this setting exists to close, in both directions.

    Too short and it drops radios that are only congested: the kernel absorbs a
    measured 2.8 s of Wi-Fi stall before the application even sees a blocked
    write, so the validator refuses anything under 5 s. Too long and it puts the
    fifteen-minute wait back that the deadline was introduced to remove. Zero is
    allowed on purpose - it turns the deadline off and restores the stock
    behaviour, which is what makes it useful for telling a dead peer from a busy
    one.
    """
    assert Settings().listener_write_timeout == 30.0
    assert Settings(listener_write_timeout=0.0).listener_write_timeout == 0.0
    assert Settings(listener_write_timeout=5.0).listener_write_timeout == 5.0
    assert Settings(listener_write_timeout=300.0).listener_write_timeout == 300.0

    for bad in (1.0, 4.9, 301.0, 900.0):
        with pytest.raises(ValidationError):
            Settings(listener_write_timeout=bad)


def test_upstream_headers():
    headers = Settings(referer="https://example.com").headers
    assert headers["Referer"] == "https://example.com"
    assert "User-Agent" in headers
    assert "Referer" not in Settings().headers


# ------------------------------------------------------- ffmpeg is optional
def test_ffmpeg_is_required_unless_somebody_says_otherwise():
    """The default must not change: this is an opt-out, not a new default.

    Most radios on the market are still MP3-only, and the whole promise of the
    project is that *any* station can be played. Turning FFmpeg off by default
    would trade a requirement nobody notices for a class of stations that
    silently stop working.
    """
    assert Settings().ffmpeg_enabled is True
    assert Settings().effective_output_codec == "mp3", "the shipped output is unchanged"
    assert Settings().passthrough_only is False


def test_disabling_ffmpeg_makes_every_stream_ask_for_the_source_codec():
    """`--no-ffmpeg` is equivalent to appending `?codec=auto` to every URL.

    That translation is the whole feature: with nothing to encode with, the only
    producible output is the audio the station already sends, so the "what codec
    may we emit?" question has exactly one answer regardless of configuration.
    """
    off = Settings(ffmpeg_enabled=False, output_codec="mp3")

    assert off.passthrough_only is True
    assert off.effective_output_codec == "auto"
    # The configured value is *kept*, not rewritten: the setting still says what
    # somebody chose, and turning FFmpeg back on must restore it.
    assert off.output_codec == "mp3"
    assert Settings(ffmpeg_enabled=True, output_codec="mp3").effective_output_codec == "mp3"

    # And the header follows the effective codec, not the configured one - the
    # ``auto`` decision needs the stream's kind, which is why it is not answered
    # here (see api._media_type).
    assert off.mime_type == "audio/mpeg"


def test_ffmpeg_off_with_passthrough_off_is_refused():
    """Two settings that leave no engine at all must fail at startup.

    ``prefer_passthrough = false`` means "always re-encode", and
    ``ffmpeg_enabled = false`` means "never re-encode". Together they describe a
    service that answers every request with nothing, and a configuration that
    looks deliberate should not have to be discovered as silence on a radio.
    """
    with pytest.raises(ValidationError, match="prefer_passthrough"):
        Settings(ffmpeg_enabled=False, prefer_passthrough=False)

    # The supported spelling of "forward everything" is the preference left on.
    assert Settings(ffmpeg_enabled=False, prefer_passthrough=True).passthrough_only is True


def test_stations_file_no_longer_exists():
    """Pins the removal of the station-preset file.

    A ``stations.toml`` on the Pi (short names + failover mirrors) was a v2
    addition that nobody asked for and that contradicted the project's
    config-free intent: radios are given one percent-encoded m3u8 URL and
    nothing else is configured. The setting and its resolver are gone; this
    test is what stops them creeping back in.
    """
    settings = Settings()
    assert not hasattr(settings, "stations_file")
    assert not hasattr(settings, "resolved_stations_file")
    with pytest.raises(ImportError):
        __import__("radioconv.stations", fromlist=["StationRegistry"])


# ------------------------------------------------------- docs stay honest
def test_output_quality_matches_the_original_project():
    """The default must stay at the quality the original version shipped.

    v1 used 192 kbit/s to preserve audio quality. Lowering it is a product
    decision, not an implementation detail, so pin it explicitly rather than
    letting it drift with a refactor.
    """
    assert Settings().bitrate == V1_DEFAULT_BITRATE == 192_000


def test_annotated_example_config_declares_the_real_default():
    """`packaging/config.toml.example` is what users copy, so it must agree.

    A stale example is worse than no example: it silently downgrades quality
    for anyone who copies it.
    """
    example = PROJECT_ROOT / "packaging" / "config.toml.example"
    text = example.read_text(encoding="utf-8")
    match = re.search(r"^bitrate\s*=\s*(\d+)", text, re.MULTILINE)
    assert match is not None, "the example config no longer declares a bitrate"
    assert int(match.group(1)) == Settings().bitrate


def test_installer_writes_the_real_default():
    """The Pi installer generates a config file; it must encode the default too."""
    installer = PROJECT_ROOT / "packaging" / "install.sh"
    text = installer.read_text(encoding="utf-8")
    match = re.search(r'^\s*echo "bitrate = (\d+)"', text, re.MULTILINE)
    assert match is not None, "the installer no longer writes a bitrate"
    assert int(match.group(1)) == Settings().bitrate
