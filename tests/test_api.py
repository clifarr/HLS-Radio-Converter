"""The HTTP layer: backwards-compatible URLs, diagnostics, and real streaming.

The streaming test runs a genuine uvicorn server in a background thread and
reads the response with httpx, so the whole stack - ASGI, session fan-out,
pipeline, HTTP - is exercised the way a radio would use it.
"""

from __future__ import annotations

import time
from urllib.parse import quote

import httpx
import pytest

from radioconv.api import decode_target, parse_controls


# ------------------------------------------------------- pure helper functions
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://example.com/live/index.m3u8", "https://example.com/live/index.m3u8"),
        ("http://example.com/a.m3u8", "http://example.com/a.m3u8"),
        # already percent-encoded once (the common radio case)
        ("https%3A%2F%2Fexample.com%2Flive%2Findex.m3u8", "https://example.com/live/index.m3u8"),
        # double encoded
        (
            "https%253A%252F%252Fexample.com%252Flive%252Findex.m3u8",
            "https://example.com/live/index.m3u8",
        ),
        ("example.com/a.m3u8", None),
        ("ftp://example.com/a.m3u8", None),
        ("", None),
    ],
)
def test_decode_target_accepts_every_shape_radios_use(raw, expected):
    assert decode_target(raw) == expected


def test_decode_target_rebuilds_an_unencoded_query_string():
    """Some radios send the upstream '?...' as a real query string."""
    target = decode_target("https://example.com/live/index.m3u8", "sd=10&rebase=on")
    assert target == "https://example.com/live/index.m3u8?sd=10&rebase=on"


def test_decode_target_reserves_our_own_parameters():
    target = decode_target("https://example.com/live/index.m3u8", "br=128000")
    assert target == "https://example.com/live/index.m3u8"


class _Request:
    """Kept so the parametrised test below reads like the real call site."""

    def __init__(self, query: str = ""):
        from starlette.datastructures import QueryParams

        self.query_params = QueryParams(query)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("br=96000", {"bitrate": 96000}),
        ("br=96k", {"bitrate": 96000}),
        ("bitrate=128000", {"bitrate": 128000}),
        ("codec=aac", {"output_codec": "adts"}),
        ("codec=mp3", {"output_codec": "mp3"}),
        ("br=1", {}),  # out of range, ignored rather than fatal
        ("bogus=1", {}),
    ],
)
def test_parse_controls(query, expected):
    assert parse_controls(_Request(query).query_params) == expected


# ------------------------------------------------------------- live HTTP tests
# `live_server` and `hls_server` both live in conftest.py: more than one test
# module now drives the real HTTP surface, so the fixtures are shared.


def test_index_page_and_health(live_server):
    page = httpx.get(live_server + "/", timeout=10)
    assert page.status_code == 200
    assert "HLS Radio Converter" in page.text

    health = httpx.get(live_server + "/health", timeout=10)
    assert health.status_code == 200
    body = health.json()
    assert body["status"] in ("ok", "degraded")
    assert "streams" in body and "ffmpeg" in body


def test_metrics_endpoint_is_prometheus_text(live_server):
    response = httpx.get(live_server + "/metrics", timeout=10)
    assert response.status_code == 200
    assert "radioconv_up 1" in response.text
    assert "radioconv_sessions" in response.text


def test_station_endpoints_are_gone(live_server):
    """There is deliberately no station-list API.

    Radios are given a percent-encoded stream URL; there is no repository of
    presets on the Pi to list or play. The routes were removed outright, so
    `/stations` is not merely empty - it does not exist, and OpenAPI does not
    advertise it. Requests fall through to the legacy `/<encoded-url>` route and
    are rejected as the malformed URL they are.
    """
    openapi_paths = httpx.get(live_server + "/openapi.json", timeout=10).json()["paths"]
    assert not [p for p in openapi_paths if "station" in p]

    for path in ("/stations", "/stations/anything"):
        response = httpx.get(live_server + path, timeout=10)
        # Not a station list, and specifically not a 404-style "wrong path"
        # either: it is the catch-all telling the client its URL is malformed.
        assert response.status_code == 400
        assert "percent-encoded" in response.text


def test_garbage_url_is_rejected_with_a_helpful_message(live_server):
    response = httpx.get(live_server + "/not-a-url", timeout=10)
    assert response.status_code == 400
    assert "percent-encoded" in response.json()["detail"]


def test_probe_requires_a_url(live_server):
    assert httpx.get(live_server + "/probe", timeout=10).status_code == 422


def test_query_stream_round_trip(live_server, hls_server):
    aac_url, _ = hls_server
    with httpx.stream(
        "GET", live_server + "/stream", params={"url": aac_url}, timeout=30
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/")
        assert response.headers["icy-name"]
        assert response.headers["cache-control"].startswith("no-store")
        received = 0
        for chunk in response.iter_bytes():
            received += len(chunk)
            if received >= 60_000:
                break
    assert received >= 60_000, "the radio would have got almost nothing"


def test_metadata_is_only_sent_to_clients_that_ask_for_it(live_server, hls_server):
    """A client that did not send `Icy-MetaData: 1` must not get metadata.

    Measured on the radio (2026-10-21): the same device opens two
    connections with two different User-Agents - `NSPlayer/8.0.0.3801`,
    which sends `icy-metadata: 1`, and `FSL IR2.10`, which does not.  The
    server sent the metadata grid to BOTH, so the client that never asked
    for it was handed a byte stream with 17-byte blocks embedded in it -
    a different firmware path that does not handle them.  That is the
    reported "garbled title and glitching audio, forever".

    So the metadata is opt-in per request.
    """
    aac_url, _ = hls_server

    # A client that asks for it gets the grid.
    with httpx.stream(
        "GET",
        live_server + "/stream",
        params={"url": aac_url},
        headers={"Icy-MetaData": "1"},
        timeout=30,
    ) as response:
        assert response.status_code == 200
        assert response.headers.get("icy-metaint"), (
            "a client that asked for metadata did not get an icy-metaint"
        )
        metaint = int(response.headers["icy-metaint"])
        body = b""
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > metaint * 3:
                break
        assert b"StreamTitle='" in body, "no metadata block was sent"

    # A client that does NOT ask for it gets plain audio.
    with httpx.stream(
        "GET", live_server + "/stream", params={"url": aac_url}, timeout=30
    ) as response:
        assert response.status_code == 200
        assert "icy-metaint" not in response.headers, (
            "metadata was sent to a client that never asked for it"
        )
        body = b""
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > 60_000:
                break
        assert b"StreamTitle='" not in body, (
            "a metadata block was sent to a client that never asked for one"
        )


def test_compat_encoded_url_route_still_works(live_server, hls_server):
    """The original ``/<percent-encoded-url>`` interface must keep working."""
    aac_url, _ = hls_server
    with httpx.stream("GET", f"{live_server}/{quote(aac_url, safe='')}", timeout=30) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/")
        received = 0
        for chunk in response.iter_bytes():
            received += len(chunk)
            if received >= 30_000:
                break
    assert received >= 30_000


def test_mp3_source_is_served_by_the_ffmpeg_free_path(live_server, hls_server, tmp_path):
    """An MP3-in-TS source must never need ffmpeg (requirement 5)."""
    _, mp3_url = hls_server
    if not mp3_url:
        # See tests/conftest.py: ty mis-resolves pytest.skip's signature.
        pytest.skip("this ffmpeg build has no libmp3lame")  # ty: ignore[too-many-positional-arguments]

    with httpx.stream(
        "GET", live_server + "/stream", params={"url": mp3_url}, timeout=30
    ) as response:
        assert response.status_code == 200
        received = 0
        for chunk in response.iter_bytes():
            received += len(chunk)
            if received >= 40_000:
                break
    assert received >= 40_000

    health = httpx.get(live_server + "/health", timeout=10).json()
    modes = [detail["pipeline"]["mode"] for detail in health["streams"]["details"]]
    assert "passthrough" in modes, f"expected a pass-through session, got {modes}"


def test_sessions_are_released_after_the_listener_leaves(live_server, hls_server):
    aac_url, _ = hls_server
    with httpx.stream(
        "GET", live_server + "/stream", params={"url": aac_url}, timeout=30
    ) as response:
        for chunk in response.iter_bytes():
            if len(chunk):
                break

    # linger_seconds is 0.5 and the reaper polls every 2s.
    deadline = time.time() + 15
    while time.time() < deadline:
        health = httpx.get(live_server + "/health", timeout=10).json()
        if health["streams"]["sessions"] == 0:
            break
        time.sleep(0.5)
    assert health["streams"]["sessions"] == 0
