"""The ``radioconv check`` self-test.

Everything here is designed to answer three questions on the machine that is
*actually* misbehaving, without network access:

1. Can this machine run the FFmpeg-free path at all? (pure-python demuxer
   round-trip - no ffmpeg involved)
2. Is an ffmpeg binary present, recent, and does it have ``libmp3lame``? (On
   32-bit ARM there is no bundled wheel, so this is normally the system one.)
3. Does a complete stream actually work end to end?

For (3) we build real HLS content with FFmpeg into a temporary directory, serve
it from a throwaway local HTTP server, and push it through the same code path a
radio would use - once with an MP3 source (expect: pass-through, no ffmpeg) and
once with an AAC source (expect: ffmpeg transcode). That is the strongest
signal we can produce without touching the internet.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import logging
import platform
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from . import __version__
from . import ffmpeg as ffmpeg_mod
from .config import Settings
from .pipeline import StreamPipeline
from .tsdemux import (
    STREAM_TYPE_AAC_ADTS,
    STREAM_TYPE_MP3,
    TSDemuxer,
    build_test_ts,
)

log = logging.getLogger(__name__)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False

    @property
    def symbol(self) -> str:
        return "--" if self.skipped else ("ok" if self.ok else "FAIL")


# --------------------------------------------------------------- quick checks
def check_python() -> Check:
    version = platform.python_version()
    ok = sys.version_info >= (3, 9)
    detail = f"python {version} on {platform.system()} {platform.machine()}"
    if not ok:
        detail += " - Python 3.9 or newer is required"
    return Check("python", ok, detail)


def check_demuxer() -> Check:
    """Pure-python MPEG-TS round-trip: proves the no-ffmpeg path works here."""
    payload = bytes(range(1, 120)) * 7  # 833 bytes: spans several TS packets
    failures: List[str] = []
    for stream_type, expected in ((STREAM_TYPE_MP3, "mp3"), (STREAM_TYPE_AAC_ADTS, "adts")):
        ts = build_test_ts(payload, stream_type=stream_type)
        demux = TSDemuxer()
        out = demux.feed(ts)
        if not demux.ready:
            failures.append(f"type 0x{stream_type:02x}: tables not detected")
        elif out != payload:
            failures.append(f"type 0x{stream_type:02x}: {len(out)} bytes out, {len(payload)} in")
        elif demux.output_kind != expected:
            failures.append(f"type 0x{stream_type:02x}: kind {demux.output_kind!r} != {expected!r}")

        # the awkward case: audio that arrives before the PAT/PMT
        late = TSDemuxer()
        out = late.feed(ts[188 * 2 :]) + late.feed(ts[: 188 * 2])
        if out != payload:
            failures.append(f"type 0x{stream_type:02x}: late-tables replay lost data")

    if failures:
        return Check("demuxer", False, "; ".join(failures))
    return Check("demuxer", True, "MPEG-TS demux round-trip exact (mp3 + adts)")


def check_ffmpeg(settings: Settings) -> Check:
    try:
        path = ffmpeg_mod.locate_ffmpeg(settings.ffmpeg_path)
    except Exception as exc:  # noqa: BLE001 - report any failure, never raise
        return Check("ffmpeg", False, str(exc))
    version = ffmpeg_mod.ffmpeg_version(settings.ffmpeg_path or "")
    have_lame = "libmp3lame" in ffmpeg_mod.encoders(settings.ffmpeg_path or "")
    detail = f"{path} - {version}"
    if not have_lame:
        detail += " (no libmp3lame encoder: MP3 output unavailable, use codec=adts)"
    return Check("ffmpeg", have_lame, detail)


def check_port(host: str, port: int) -> Check:
    """Can we bind, or is something already there - and is that something *us*?

    A live install obviously owns its own port, so treating "address in use" as a
    failure made ``radioconv check`` report FAIL on exactly the machine where a
    user is most likely to run it. Ask the occupier what it is before judging.
    """
    bind_host = "" if host in ("0.0.0.0", "::") else host
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((bind_host, port))
        return Check("port", True, f"{host}:{port} is free to bind")
    except OSError as exc:
        if listening_radioconv(host, port):
            return Check(
                "port", True, f"{host}:{port} is already serving radioconv (service is up)"
            )
        return Check("port", False, f"cannot bind {host}:{port}: {exc}")
    finally:
        probe.close()


def listening_radioconv(host: str, port: int, timeout: float = 2.0) -> bool:
    """Is a radioconv server serving on this port?

    Deliberately a plain socket + stdlib HTTP request: this runs as part of
    ``radioconv check``, whose whole job is to work when other things do not.
    """
    target = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        with socket.create_connection((target, port), timeout=timeout) as sock:
            sock.sendall(
                f"GET /health HTTP/1.0\r\nHost: {target}\r\nConnection: close\r\n\r\n".encode()
            )
            raw = b""
            while len(raw) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
    except OSError:
        return False

    if b"200" not in raw.split(b"\r\n", 1)[0]:
        return False
    try:
        body = json.loads(raw.split(b"\r\n\r\n", 1)[1].decode("utf-8", "replace"))
    except (IndexError, ValueError):
        return False
    # Both markers are ours: /health reports a "streams" block and this version.
    return isinstance(body, dict) and "streams" in body and "version" in body


#: Modules every stream needs. If one of these is missing the service cannot
#: work at all, so it is a hard failure.
REQUIRED_MODULES = ("fastapi", "uvicorn", "httpx", "m3u8", "cryptography", "psutil")

#: Modules only the browser pages need. These are imported lazily by
#: :mod:`radioconv.web`, which is what keeps them off the cold-start path on a
#: Pi - and also what makes a missing one invisible: the service starts, streams
#: audio perfectly, and answers its first request to ``/`` with a 500. That is
#: exactly the kind of silent breakage ``radioconv check`` exists to catch, so it
#: is checked separately and reported as non-fatal.
WEB_MODULES = ("jinja2",)


def _missing(modules: Tuple[str, ...]) -> List[str]:
    failures: List[str] = []
    for module in modules:
        try:
            __import__(module)
        except Exception as exc:  # noqa: BLE001 - an import can fail in many ways
            failures.append(f"{module} ({exc})")
    return failures


def check_imports() -> List[Check]:
    """Check both dependency sets, separately and honestly.

    Two checks rather than one because the consequences differ: without a
    streaming dependency nothing works, and without jinja2 the radios are
    entirely unaffected - only a human opening the landing page or the dashboard
    sees the problem. Reporting them together would either overstate the jinja2
    failure or hide it; this way the message says which one it is.
    """
    checks: List[Check] = []

    missing = _missing(REQUIRED_MODULES)
    checks.append(
        Check("dependencies", not missing, "missing: " + ", ".join(missing))
        if missing
        else Check("dependencies", True, ", ".join(REQUIRED_MODULES))
    )

    missing = _missing(WEB_MODULES)
    checks.append(
        Check(
            "web ui",
            not missing,
            "missing: "
            + ", ".join(missing)
            + " - / and /dashboard will return 500 (radios are unaffected)",
        )
        if missing
        else Check("web ui", True, ", ".join(WEB_MODULES) + " (the browser pages)")
    )

    return checks


# ------------------------------------------------------------ end-to-end test
class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        """Silence the test server; its access log is pure noise here."""


class _ReusableServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _serve(directory: Path) -> tuple:
    handler = lambda *a, **kw: _QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    server = _ReusableServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _generate_hls(directory: Path, codec: str, bitrate: int, seconds: int = 12) -> Path:
    """Generate a real HLS playlist with ffmpeg, returning the playlist path.

    ``bitrate`` is passed in rather than hard-coded, so the test content is
    produced at whatever the configured output bitrate is. That keeps this
    self-test honest: it exercises the pipeline at realistic settings, and the
    generated stream can never drift away from the shipped default.
    """
    binary = ffmpeg_mod.locate_ffmpeg()
    encoder = "libmp3lame" if codec == "mp3" else "aac"
    playlist = directory / "index.m3u8"
    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=44100:duration={seconds}",
        "-ac",
        "2",
        "-c:a",
        encoder,
        "-b:a",
        str(bitrate),
        "-f",
        "hls",
        "-hls_time",
        "2",
        "-hls_list_size",
        "0",
        "-hls_segment_filename",
        str(directory / "seg%d.ts"),
        str(playlist),
    ]
    subprocess.run(command, check=True, timeout=120, capture_output=True)
    if not playlist.is_file():
        raise RuntimeError("ffmpeg did not produce a playlist")
    return playlist


async def _collect(pipeline: StreamPipeline, min_bytes: int, timeout: float) -> int:
    total = 0
    deadline = time.time() + timeout
    stream = pipeline.stream()
    try:
        while total < min_bytes and time.time() < deadline:
            remaining = deadline - time.time()
            try:
                chunk = await asyncio.wait_for(
                    stream.__anext__(), timeout=max(1.0, min(5.0, remaining))
                )
            except (asyncio.TimeoutError, StopAsyncIteration):
                continue
            total += len(chunk)
    finally:
        await stream.aclose()
    return total


async def _end_to_end(settings: Settings, codec: str) -> Check:
    """Generate, serve and stream a real HLS source; verify the engine used."""
    name = f"end-to-end ({codec} source)"
    try:
        ffmpeg_mod.locate_ffmpeg(settings.ffmpeg_path)
    except Exception as exc:  # noqa: BLE001 - report any failure, never raise
        return Check(name, False, f"ffmpeg unavailable: {exc}")

    if codec == "mp3" and "libmp3lame" not in ffmpeg_mod.encoders(settings.ffmpeg_path or ""):
        return Check(name, True, "skipped: this ffmpeg build has no libmp3lame", skipped=True)

    with tempfile.TemporaryDirectory(prefix="radioconv-check-") as tmp:
        directory = Path(tmp)
        try:
            await asyncio.to_thread(_generate_hls, directory, codec, settings.bitrate)
        except Exception as exc:  # noqa: BLE001 - any generator failure is a check failure
            return Check(name, False, f"could not generate test content: {exc}")

        server, port = _serve(directory)
        url = f"http://127.0.0.1:{port}/index.m3u8"
        test_settings = settings.model_copy(
            update={"max_sessions": 1, "stall_timeout": 10.0, "segment_retries": 1}
        )
        pipeline = StreamPipeline(test_settings, [url], label=f"check-{codec}")
        try:
            collected = await _collect(pipeline, min_bytes=120_000, timeout=45.0)
            mode = pipeline.stats.mode
            stats = pipeline.stats.as_dict()
        finally:
            await pipeline.aclose()
            server.shutdown()
            server.server_close()

    if collected < 10_000:
        return Check(name, False, f"only {collected} bytes of output (expected >10 kB)")
    if codec == "mp3" and mode != "passthrough":
        return Check(name, False, f"expected the ffmpeg-free path, got {mode!r}: {stats}")
    if codec == "aac" and mode != "transcode":
        return Check(name, False, f"expected a transcode for AAC, got {mode!r}")
    return Check(
        name,
        True,
        f"{mode} engine, {collected // 1024} KiB out, kind={stats.get('detected_kind')}",
    )


# ------------------------------------------------------------------- driver
async def _run_async(settings: Settings, end_to_end: bool) -> List[Check]:
    checks = [check_python(), *check_imports(), check_demuxer(), check_ffmpeg(settings)]
    if end_to_end:
        checks.append(await _end_to_end(settings, "mp3"))
        checks.append(await _end_to_end(settings, "aac"))
    return checks


def run_check(
    settings: Optional[Settings] = None, *, end_to_end: bool = False, port_check: bool = True
) -> int:
    """Print the check table; return a process exit code (0 = everything passed)."""
    settings = settings or Settings()
    checks = asyncio.run(_run_async(settings, end_to_end))

    if port_check:
        checks.append(check_port(settings.host, settings.port))

    print(f"radioconv {__version__} - check")
    print("-" * 64)
    for check in checks:
        print(f"[{check.symbol:>4}] {check.name:<22} {check.detail}")
    print("-" * 64)

    failed = [c for c in checks if not c.ok and not c.skipped]
    if failed:
        print(f"{len(failed)} check(s) failed.")
        return 1
    print("All checks passed.")
    if not end_to_end:
        print("Tip: 'radioconv check --end-to-end' also generates and streams real HLS.")
    return 0
