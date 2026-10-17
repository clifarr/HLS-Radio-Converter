"""``radioconv check``: the checks must be right on a live install, not a fresh one.

The port check used to report FAIL whenever the port was taken, which is the
normal state of a machine that is already running the service - the exact place
a user runs ``radioconv check``.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import pytest

from radioconv.check import Check, check_port, listening_radioconv
from radioconv.config import Settings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Handler(BaseHTTPRequestHandler):
    payload: ClassVar[dict] = {}
    status = 200

    def do_GET(self):
        body = json.dumps(self.payload).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # keep test output clean
        return


@pytest.fixture
def server():
    """Run a throwaway HTTP server; the caller sets its payload/status."""
    started = []

    def _start(payload, status=200):
        handler = type("_H", (_Handler,), {"payload": payload, "status": status})
        httpd = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return httpd.server_address[1]

    yield _start
    for httpd in started:
        httpd.shutdown()


# ------------------------------------------------------------- the good news
def test_free_port_passes():
    result = check_port("127.0.0.1", _free_port())
    assert isinstance(result, Check)
    assert result.ok
    assert "free to bind" in result.detail


def test_port_held_by_radioconv_is_not_a_failure(server):
    """A live install owns its port; that is success, not a broken check."""
    port = server({"status": "ok", "version": "2.0.0", "streams": {"sessions": 0}})
    result = check_port("127.0.0.1", port)
    assert result.ok, "check must not report FAIL just because the service is running"
    assert "serving radioconv" in result.detail


def test_port_held_by_something_else_is_a_failure(server):
    """A stranger on the port is a genuine problem worth reporting."""
    port = server({"hello": "not radioconv"})
    result = check_port("127.0.0.1", port)
    assert not result.ok
    assert "cannot bind" in result.detail


# ---------------------------------------------------------- the raw predicate
def test_listening_radioconv_recognises_our_health(server):
    port = server({"status": "ok", "version": "2.0.0", "streams": {"sessions": 0}})
    assert listening_radioconv("127.0.0.1", port) is True


@pytest.mark.parametrize(
    "payload",
    [
        {"hello": "world"},  # someone else's JSON
        {"version": "2.0.0"},  # ours, but not the health shape
        {"streams": {"sessions": 0}},  # health shape, no version marker
    ],
)
def test_listening_radioconv_rejects_lookalikes(server, payload):
    port = server(payload)
    assert listening_radioconv("127.0.0.1", port) is False


def test_listening_radioconv_handles_non_200(server):
    port = server({"status": "ok", "version": "2.0.0", "streams": {}}, status=500)
    assert listening_radioconv("127.0.0.1", port) is False


def test_listening_radioconv_false_when_nothing_is_there():
    assert listening_radioconv("127.0.0.1", _free_port(), timeout=0.5) is False


def test_check_reports_the_configured_ffmpeg_path():
    """The default settings must find a usable ffmpeg on a dev machine."""
    from radioconv.check import check_ffmpeg

    result = check_ffmpeg(Settings())
    assert result.ok, result.detail
    assert "/" in result.detail, "the path must be absolute, not a bare command name"
