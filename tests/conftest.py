"""Shared test fixtures.

The interesting fixture is :func:`hls_server`: it generates *real* HLS content
with ffmpeg (an AAC playlist and an MP3 playlist) and serves it from a
throwaway local HTTP server, so the pipeline, the HLS client and the HTTP API
are all exercised against something genuine rather than a stub.

:func:`live_server` runs the real app under a real uvicorn on a real port. It
lives here rather than in one test module because more than one module now needs
to make HTTP requests against a running service.
"""

from __future__ import annotations

import http.server
import socket
import socketserver
import subprocess
import threading
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any, Tuple

import pytest
import uvicorn

from radioconv import ffmpeg as ffmpeg_mod
from radioconv.api import create_app
from radioconv.config import Settings

pytest_plugins = ()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        """Silence the test server; its access log is pure noise."""


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def has_ffmpeg() -> bool:
    try:
        ffmpeg_mod.locate_ffmpeg()
    except Exception:
        return False
    return True


def build_hls(directory: Path, codec: str, seconds: int = 8) -> Path:
    """Create a VOD HLS playlist with ffmpeg and return its path.

    The source uses the shipped default bitrate so the tests see the same kind
    of stream a real radio does.
    """
    directory.mkdir(parents=True, exist_ok=True)
    playlist = directory / "index.m3u8"
    subprocess.run(
        [
            ffmpeg_mod.locate_ffmpeg(),
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
            "libmp3lame" if codec == "mp3" else "aac",
            "-b:a",
            str(Settings().bitrate),
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
            str(directory / "seg%d.ts"),
            str(playlist),
        ],
        check=True,
        timeout=120,
        capture_output=True,
    )
    return playlist


@pytest.fixture(scope="session")
def hls_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Tuple[str, str]]:
    """``(aac_url, mp3_url)`` served over HTTP from a temporary directory."""
    if not has_ffmpeg():
        # ty mis-resolves pytest.skip (it sees 0 positional parameters, while the
        # real signature is `skip(reason: str = "", *, allow_module_level=False)`).
        pytest.skip("ffmpeg is not available")  # ty: ignore[too-many-positional-arguments]

    root = tmp_path_factory.mktemp("hls")
    mp3_ok = "libmp3lame" in ffmpeg_mod.encoders()
    build_hls(root / "aac", "aac")
    if mp3_ok:
        build_hls(root / "mp3", "mp3")

    handler = partial(_QuietHandler, directory=str(root))
    server = _Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    aac_url = f"http://127.0.0.1:{port}/aac/index.m3u8"
    mp3_url = f"http://127.0.0.1:{port}/mp3/index.m3u8" if mp3_ok else ""
    try:
        yield aac_url, mp3_url
    finally:
        server.shutdown()
        server.server_close()


def id3_tag(payload: bytes) -> bytes:
    """An ID3v2.4 tag, with the syncsafe length encoding real tags use."""
    size = len(payload)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x04\x00\x00" + syncsafe + payload


def build_id3_adts_hls(directory: Path, segments: int = 6, seconds: int = 2) -> Path:
    """Build HLS whose segments are raw ADTS AAC behind two ID3v2 tags.

    Reproduces the layout that shipped broken: a Microsoft ``msvdn.net`` stream
    serving ``.aac`` segments, each prefixed with a ``PRIV`` tag (the Apple
    ``transportStreamTimestamp``) and a ``TEXT`` tag (song metadata), then raw
    ADTS. Nothing here is a transport stream, and the audio begins 655 bytes in.

    A real capture is deliberately *not* committed as a fixture: it would be
    somebody else's audio, and it would not document the structure. Generating
    the exact byte layout tests more and ships less.
    """
    directory.mkdir(parents=True, exist_ok=True)

    # ADTS uses no container at all, so the segments are encoded individually and
    # the playlist is written by hand: ffmpeg's hls muxer only emits TS or fMP4.
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{seconds}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for index in range(segments):
        name = f"media_{index}.aac"
        subprocess.run(
            [
                ffmpeg_mod.locate_ffmpeg(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={440 + index * 20}:sample_rate=48000:duration={seconds}",
                "-ac",
                "2",
                "-c:a",
                "aac",
                "-b:a",
                str(Settings().bitrate),
                "-f",
                "adts",
                str(directory / name),
            ],
            check=True,
            timeout=120,
            capture_output=True,
        )

        raw = (directory / name).read_bytes()
        # Two tags, in the order the CDN sends them, and enough payload that the
        # metadata is far longer than any single ADTS frame.
        tagged = (
            id3_tag(b"PRIV\x00\x00\x005\x00\x00com.apple.streaming.transportStreamTimestamp")
            + id3_tag(
                b"TEXT\x00\x00\x10\x00\x00\x03"
                + b'{"songInfo":{"title":"Fixture"}}'.ljust(240, b" ")
            )
            + raw
        )
        (directory / name).write_bytes(tagged)

        lines.append(f"#EXTINF:{seconds}.000,")
        lines.append(name)
    lines.append("#EXT-X-ENDLIST")

    playlist = directory / "index.m3u8"
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return playlist


@pytest.fixture(scope="session")
def id3_adts_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A URL for an HLS stream of ID3-tagged raw ADTS AAC segments.

    This is the regression fixture for the bug where such a stream returned
    ``200 audio/mpeg`` and then not a single byte of audio, because the container
    was guessed from the URL as ``ts`` and ffmpeg was handed ``-f mpegts`` for
    data it could not read.
    """
    if not has_ffmpeg():
        pytest.skip("ffmpeg is not available")  # ty: ignore[too-many-positional-arguments]

    root = tmp_path_factory.mktemp("id3adts")
    build_id3_adts_hls(root / "stream")

    handler = partial(_QuietHandler, directory=str(root))
    server = _Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/stream/index.m3u8"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _free_port() -> int:
    """Ask the OS for a port nothing is using, then let it go."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@pytest.fixture(scope="module")
def live_server() -> Iterator[str]:
    """A real uvicorn server, on a real port, with a real app.

    Module-scoped so the (relatively slow) startup is paid once per module.
    Settings are deliberately small so tests are quick: ``linger_seconds=0.5``
    means a session is released almost immediately, and ``stall_timeout=15``
    keeps a stalled pipeline from holding a test open.
    """
    settings = Settings(
        host="127.0.0.1",
        port=_free_port(),
        linger_seconds=0.5,
        max_sessions=3,
        stall_timeout=15.0,
        log_level="WARNING",
    )
    app = create_app(settings)

    config = uvicorn.Config(
        app, host="127.0.0.1", port=settings.port, log_level="warning", loop="asyncio"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline and not server.started:
        time.sleep(0.1)
    if not server.started:  # pragma: no cover - only on a broken environment
        raise AssertionError("uvicorn did not start")

    try:
        yield f"http://127.0.0.1:{settings.port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
