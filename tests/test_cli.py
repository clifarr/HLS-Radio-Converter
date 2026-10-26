"""CLI wiring.

The important test here is :func:`test_every_serve_flag_is_actually_read`: the
first version of the CLI declared flags with ``dest=`` overrides that
``cmd_serve`` never read, so ``radioconv serve --ffmpeg /usr/bin/ffmpeg`` died
with an ``AttributeError`` at startup. Only a real command line catches that,
so that is what we parse.
"""

from __future__ import annotations

import pytest

from radioconv import cli
from radioconv.config import Settings

#: A command line that sets every single ``serve`` flag.
FULL_SERVE_ARGV = [
    "serve",
    "--host",
    "127.0.0.1",
    "--port",
    "9001",
    "--bitrate",
    "128000",
    "--codec",
    "adts",
    "--channels",
    "1",
    "--sample-rate",
    "44100",
    "--variant",
    "lowest",
    "--max-sessions",
    "3",
    "--max-listeners",
    "5",
    "--ffmpeg",
    "/usr/bin/ffmpeg",
    "--user-agent",
    "test-agent",
    "--referer",
    "https://example.com/",
    "--linger",
    "12.5",
    "--stall-timeout",
    "20",
    "--memory-limit",
    "200",
    "--mime",
    "audio/mpeg3",
    "--cors",
    "http://a,http://b",
    "--public-host",
    "radio.local:9001",
    "--access-log",
    "--insecure",
    "--no-passthrough",
]


def test_every_serve_flag_is_actually_read():
    args = cli.build_parser().parse_args(FULL_SERVE_ARGV)
    overrides = cli._serve_overrides(args)

    assert overrides == {
        "host": "127.0.0.1",
        "port": 9001,
        "bitrate": 128000,
        "output_codec": "adts",
        "channels": 1,
        "sample_rate": 44100,
        "variant_selection": "lowest",
        "max_sessions": 3,
        "max_listeners_per_session": 5,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        # Not given in FULL_SERVE_ARGV: `--no-ffmpeg` contradicts
        # `--no-passthrough` (see Settings._check_engine_combination), so the
        # two cannot appear on one command line. It is covered on its own below.
        "ffmpeg_enabled": None,
        "user_agent": "test-agent",
        "referer": "https://example.com/",
        "linger_seconds": 12.5,
        "stall_timeout": 20.0,
        "memory_limit_mb": 200,
        "mime_override": "audio/mpeg3",
        "cors_origins": "http://a,http://b",
        "public_host": "radio.local:9001",
        "access_log": True,
        "prefer_passthrough": False,
        "insecure": True,
    }

    # ...and every one of those names must be a real Settings field, so a typo
    # cannot hide behind pydantic's "ignore extra" behaviour.
    #
    # Through `Settings.load`, exactly as `cmd_serve` does it: the overrides
    # table legitimately carries `None` for every flag that was not given (that
    # is what lets a config file or an environment variable win), and `load`
    # is the thing that drops them. Constructing `Settings(**overrides)` here
    # would test a path the service never takes - and now fails on it, because
    # `ffmpeg_enabled` is a plain bool and cannot accept `None`.
    settings = Settings.load(None, **overrides)
    assert settings.output_codec == "adts"
    assert settings.prefer_passthrough is False
    assert settings.mime_override == "audio/mpeg3"
    assert settings.sample_rate == 44100
    assert settings.cors_list == ["http://a", "http://b"]


def test_no_ffmpeg_is_opt_in_and_absence_changes_nothing():
    """FFmpeg stays required unless somebody says otherwise, and then it is off.

    ``default=None`` rather than ``True`` is what lets a config file or
    ``RADIOCONV_FFMPEG_ENABLED`` decide: a ``store_false`` whose default was
    ``True`` would silently overrule both, and the flag would appear to work
    while quietly resetting a deliberate configuration every time the service
    started from the command line.
    """
    bare = cli._serve_overrides(cli.build_parser().parse_args(["serve"]))
    assert bare["ffmpeg_enabled"] is None, "a bare serve must not decide this"
    assert Settings().ffmpeg_enabled is True, "FFmpeg must stay required by default"

    off = cli._serve_overrides(cli.build_parser().parse_args(["serve", "--no-ffmpeg"]))
    assert off["ffmpeg_enabled"] is False

    settings = Settings.load(None, **off)
    assert settings.ffmpeg_enabled is False
    # ...and the whole point: the codec becomes "whatever the station sends".
    assert settings.effective_output_codec == "auto"
    assert settings.output_codec == "mp3", "the configured value is kept, not rewritten"


def test_defaults_do_not_override_anything():
    """A bare ``serve`` must produce all-None overrides (so file/env win)."""
    args = cli.build_parser().parse_args(["serve"])
    overrides = cli._serve_overrides(args)
    assert all(value is None for value in overrides.values()), overrides


def test_global_flags_work_on_both_sides_of_the_subcommand():
    before = cli.build_parser().parse_args(["--log-level", "DEBUG", "check"])
    after = cli.build_parser().parse_args(["check", "--log-level", "DEBUG"])
    assert before.log_level == after.log_level == "DEBUG"
    assert before.command == after.command == "check"
    assert before.log_json is False


def test_bare_invocation_defaults_to_serve(monkeypatch):
    captured = {}

    def fake_cmd_serve(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(cli, "cmd_serve", fake_cmd_serve)
    assert cli.main([]) == 0
    assert captured["args"].command == "serve"


@pytest.mark.parametrize("command", ["serve", "check", "probe", "url"])
def test_every_subcommand_parses(command):
    # `probe` and `url` need a positional URL.
    argv = [command, "https://example.com/x.m3u8"] if command in ("probe", "url") else [command]
    args = cli.build_parser().parse_args(argv)
    assert args.command == command


def test_stations_subcommand_is_gone():
    """There is no station list to configure, so there is no command for it."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["stations"])
    assert "stations" not in cli.SUBCOMMANDS


def test_doctor_subcommand_is_gone():
    """``doctor`` was jargon only mobile developers recognise; it is ``check``."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["doctor"])
    assert "doctor" not in cli.SUBCOMMANDS


def test_check_flag_says_what_the_extra_work_costs():
    """``--full`` became ``--end-to-end``: it generates and streams real HLS."""
    args = cli.build_parser().parse_args(["check", "--end-to-end", "--skip-port"])
    assert args.end_to_end is True
    assert args.skip_port is True
    assert not hasattr(args, "full"), "the old flag name must not survive as dead weight"

    defaults = cli.build_parser().parse_args(["check"])
    assert defaults.end_to_end is False
    assert defaults.skip_port is False


def test_url_command_prints_both_shapes(capsys):
    assert (
        cli.main(["url", "https://example.com/a.m3u8", "--host", "10.0.0.5", "--port", "9000"]) == 0
    )
    out = capsys.readouterr().out
    assert "http://10.0.0.5:9000/https%3A%2F%2Fexample.com%2Fa.m3u8" in out
    assert "http://10.0.0.5:9000/stream?url=https%3A%2F%2Fexample.com%2Fa.m3u8" in out
