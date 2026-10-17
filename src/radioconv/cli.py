"""Command line interface.

::

    radioconv serve               # run the service
    radioconv check --end-to-end  # prove the install works end to end
    radioconv probe <m3u8>        # what is in this stream, and will it need ffmpeg?
    radioconv url <m3u8>          # print the URL to type into your radio
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .config import Settings, default_config_dir
from .log import setup_logging


def _add_global_args(parser: argparse.ArgumentParser, *, suppressed: bool = False) -> None:
    """Add the flags that may appear before *or* after the subcommand.

    ``argparse`` normally wants globals first; duplicating them on every
    subparser (with ``SUPPRESS`` defaults so they never clobber the top-level
    value) lets ``radioconv check --log-level DEBUG`` work as well as
    ``radioconv --log-level DEBUG check``.
    """
    default = argparse.SUPPRESS if suppressed else None
    parser.add_argument("--config", default=default, help="path to config.toml / config.json")
    parser.add_argument("--log-level", default=default, help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument(
        "--log-json",
        action="store_true",
        default=argparse.SUPPRESS if suppressed else False,
        help="one JSON object per log line",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radioconv",
        description="Convert HLS (m3u8) radio streams into plain HTTP audio streams.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"radioconv {__version__}")
    _add_global_args(parser)

    sub = parser.add_subparsers(dest="command")

    def add(name: str, help: str, **kwargs) -> argparse.ArgumentParser:
        created = sub.add_parser(
            name,
            help=help,
            parents=[],
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            **kwargs,
        )
        _add_global_args(created, suppressed=True)
        return created

    serve = add("serve", "run the streaming server")
    serve.add_argument("--host", help="interface to bind")
    serve.add_argument("--port", type=int, help="port to listen on")
    serve.add_argument("--bitrate", type=int, help="target bitrate in bit/s for MP3 output")
    serve.add_argument("--codec", choices=["mp3", "adts", "auto"], help="output codec")
    serve.add_argument("--channels", type=int, help="output channel count")
    serve.add_argument("--sample-rate", type=int, help="resample output (default: keep source)")
    serve.add_argument(
        "--variant",
        choices=["highest", "lowest", "first"],
        help="which master-playlist rendition to choose",
    )
    serve.add_argument("--max-sessions", type=int, help="concurrent pipelines")
    serve.add_argument("--max-listeners", type=int, help="radios per station")
    serve.add_argument("--ffmpeg", help="explicit ffmpeg binary")
    serve.add_argument("--user-agent", help="HTTP User-Agent sent upstream")
    serve.add_argument("--referer", help="HTTP Referer sent upstream")
    serve.add_argument("--linger", type=float, help="seconds to keep an idle stream warm")
    serve.add_argument(
        "--stall-timeout", type=float, help="seconds without audio before restarting"
    )
    serve.add_argument("--memory-limit", type=int, help="restart above this RSS in MB (0 = off)")
    serve.add_argument("--mime", help="force the Content-Type sent to radios")
    serve.add_argument(
        "--insecure", action="store_true", default=None, help="skip TLS verification upstream"
    )
    serve.add_argument(
        "--no-passthrough",
        action="store_false",
        dest="prefer_passthrough",
        default=None,
        help="always use ffmpeg, even when no conversion is needed",
    )
    serve.add_argument(
        "--access-log", action="store_true", default=None, help="log every HTTP request"
    )
    serve.add_argument("--cors", help="comma separated allowed CORS origins")
    serve.add_argument("--public-host", help="host:port shown in the index page")

    check = add("check", "check that this machine can run radioconv")
    check.add_argument(
        "--end-to-end",
        action="store_true",
        help="also generate and stream real HLS content (needs ffmpeg, takes ~25s)",
    )
    check.add_argument("--json", action="store_true", help="machine readable output")
    check.add_argument("--skip-port", action="store_true", help="do not test binding the port")

    probe = add("probe", "inspect an HLS stream without playing it")
    probe.add_argument("url", help="the .m3u8 URL")
    probe.add_argument("--json", action="store_true", help="machine readable output")

    url_cmd = add("url", "print the URL to enter into a radio")
    url_cmd.add_argument("url", help="the .m3u8 URL")
    url_cmd.add_argument("--host", help="host or IP the radio should use")
    url_cmd.add_argument("--port", type=int, help="port to use")

    return parser


# --------------------------------------------------------------------- serve
def _serve_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    """Map ``serve`` flags onto :class:`Settings` field names.

    Every attribute read here must exist on the parsed namespace; the lookup is
    explicit (rather than ``**vars(args)``) so a renamed flag fails loudly
    instead of silently keeping a default. ``tests/test_cli.py`` asserts the
    whole table against a fully specified command line.
    """
    mapping = {
        "host": "host",
        "port": "port",
        "bitrate": "bitrate",
        "codec": "output_codec",
        "channels": "channels",
        "sample_rate": "sample_rate",
        "variant": "variant_selection",
        "max_sessions": "max_sessions",
        "max_listeners": "max_listeners_per_session",
        "ffmpeg": "ffmpeg_path",
        "user_agent": "user_agent",
        "referer": "referer",
        "linger": "linger_seconds",
        "stall_timeout": "stall_timeout",
        "memory_limit": "memory_limit_mb",
        "mime": "mime_override",
        "cors": "cors_origins",
        "public_host": "public_host",
        "access_log": "access_log",
        "prefer_passthrough": "prefer_passthrough",
    }
    missing = sorted(name for name in mapping if not hasattr(args, name))
    if missing:  # pragma: no cover - a programming error, caught by the tests
        raise RuntimeError(f"serve flags out of sync with the overrides table: {missing}")

    overrides: Dict[str, Any] = {field: getattr(args, flag) for flag, field in mapping.items()}
    if getattr(args, "insecure", False):
        overrides["insecure"] = True
    return overrides


def cmd_serve(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config, **_serve_overrides(args))
    setup_logging(settings.log_level, settings.log_json)

    import logging

    log = logging.getLogger("radioconv")

    # Import late: keep startup import cost (and thus cold-start latency) low.
    import uvicorn

    from .api import create_app

    app = create_app(settings)

    log.info("radioconv %s", __version__)
    log.info("listening on http://%s:%d", settings.host, settings.port)
    log.info(
        "output=%s @ %d kbit/s, passthrough=%s, max %d stream(s)",
        settings.output_codec,
        settings.bitrate // 1000,
        "on" if settings.prefer_passthrough else "off",
        settings.max_sessions,
    )
    log.info("config dir: %s", default_config_dir())

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,  # keep our own logging setup
        log_level="warning",
        access_log=settings.access_log,
        # Deliberately one process: sessions, capacity limits and the watchdog
        # are per-process state. Concurrency comes from asyncio, not from
        # forking workers - which also keeps a Pi's memory use predictable.
        workers=1,
        # uvloop is nice on x86 but adds no value here and is one more wheel
        # that can fail to build on a small board.
        loop="asyncio",
        timeout_graceful_shutdown=10,
        server_header=False,
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover
        log.info("interrupted")
    return 0


# --------------------------------------------------------------------- probe
def cmd_probe(args: argparse.Namespace, settings: Settings) -> int:
    from . import hls as hls_mod

    result = asyncio.run(hls_mod.inspect(args.url, settings))
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    print(f"URL          : {result.get('url')}")
    if not result.get("ok"):
        print(f"ERROR        : {result.get('error_type')}: {result.get('error')}")
        return 1
    print(f"media        : {result.get('media_url')}")
    print(
        f"master       : {result.get('is_master')} ({result.get('variants_considered')} variants)"
    )
    print(
        f"chosen       : {result.get('variant_bandwidth')} bit/s {result.get('variant_label') or ''}".rstrip()
    )
    print(f"segments     : {result.get('segment_count')} x ~{result.get('target_duration')}s")
    print(f"container    : {result.get('container')} (segment: {result.get('segment_container')})")
    print(f"encrypted    : {result.get('encrypted')}")
    audio = result.get("audio") or {}
    print(
        f"audio codec  : {audio.get('codec')} {audio.get('profile') or ''} {audio.get('sample_rate') or '?'} Hz"
    )
    print(f"passthrough  : {result.get('passthrough_possible')} ({result.get('passthrough_kind')})")
    if result.get("ts_streams"):
        print(
            "TS streams   : "
            + ", ".join(
                f"0x{t['type']:02x} pid 0x{t['pid']:x} -> {t['playable_as'] or 'needs ffmpeg'}"
                for t in result["ts_streams"]
            )
        )
    return 0


# ----------------------------------------------------------------------- url
def cmd_url(args: argparse.Namespace, settings: Settings) -> int:
    from urllib.parse import quote

    host = args.host or settings.public_host or "localhost"
    port = args.port or settings.port
    print(f"http://{host}:{port}/{quote(args.url, safe='')}")
    print(f"http://{host}:{port}/stream?url={quote(args.url, safe='')}")
    return 0


SUBCOMMANDS = {"serve", "probe", "url", "check"}


# ---------------------------------------------------------------------- main
def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    # ``radioconv`` on its own (or with only global flags) means ``serve``.
    if not SUBCOMMANDS.intersection(argv):
        argv.append("serve")
    args = parser.parse_args(argv)

    settings = Settings.load(getattr(args, "config", None))

    if not getattr(args, "log_level", None):
        setup_logging(settings.log_level, bool(getattr(args, "log_json", False)))

    command = args.command
    if command == "serve":
        return cmd_serve(args)
    if command == "probe":
        return cmd_probe(args, settings)
    if command == "url":
        return cmd_url(args, settings)
    if command == "check":
        if getattr(args, "json", False):
            return _check_json(settings, end_to_end=args.end_to_end, port_check=not args.skip_port)
        from .check import run_check

        return run_check(settings, end_to_end=args.end_to_end, port_check=not args.skip_port)

    parser.print_help()
    return 2


def _check_json(settings: Settings, *, end_to_end: bool, port_check: bool) -> int:
    """Machine-readable ``radioconv check``, for scripts and CI."""
    from .check import _run_async, check_port

    checks = asyncio.run(_run_async(settings, end_to_end))
    if port_check:
        checks.append(check_port(settings.host, settings.port))
    print(
        json.dumps(
            {
                "version": __version__,
                "checks": [
                    {"name": c.name, "ok": c.ok, "skipped": c.skipped, "detail": c.detail}
                    for c in checks
                ],
                "ok": all(c.ok for c in checks if not c.skipped),
            },
            indent=2,
        )
    )
    return 0 if all(c.ok for c in checks if not c.skipped) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
