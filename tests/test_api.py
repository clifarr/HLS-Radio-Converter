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
from radioconv.config import Settings


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
        ("codec=adts", {"output_codec": "adts"}),
        ("codec=mp3", {"output_codec": "mp3"}),
        ("br=1", {}),  # out of range, ignored rather than fatal
        ("bogus=1", {}),
    ],
)
def test_parse_controls(query, expected):
    assert parse_controls(_Request(query).query_params) == expected


def test_a_url_override_beats_the_configured_default():
    """`?codec=` and `?br=` are per-radio overrides, not suggestions.

    The settings page calls its values *defaults for every radio*, and that
    sentence is only true while the override still wins - which it does by
    replacing the stored value in the settings a session is built from. This pins
    the precedence the page promises, using the real parser, so neither half can
    drift: if `parse_controls` kept the value but the route stopped applying it,
    every radio would silently get the default.
    """
    configured = Settings(output_codec="mp3", bitrate=192_000)
    overrides = parse_controls(_Request("codec=adts&br=96k").query_params)
    assert overrides == {"output_codec": "adts", "bitrate": 96_000}

    used = configured.model_copy(update=overrides)
    assert used.output_codec == "adts"
    assert used.bitrate == 96_000
    # And the configured values are untouched: one radio's choice is not a
    # change to the service, which is what makes the page's "defaults" true.
    assert configured.output_codec == "mp3"
    assert configured.bitrate == 192_000


def test_aac_and_adts_are_the_same_request():
    """Two spellings, one meaning - so neither can be "the better one".

    The page prints ``adts`` (that is ``passthrough_kind``'s value and the word
    its verdict sentence uses), while people type ``aac`` because that is what
    the radio's specification sheet says. Both set the same field, so both
    produce the same engine decision, the same bytes and the same
    ``Content-Type``.

    Pinned here so a later edit cannot quietly give them different meanings -
    for example a real LOAS/HE-AAC output, which no old radio decodes.
    """
    aac = parse_controls(_Request("codec=aac").query_params)
    adts = parse_controls(_Request("codec=adts").query_params)

    assert aac == adts == {"output_codec": "adts"}


# ------------------------------------------------ ffmpeg turned off entirely
def test_the_content_type_follows_what_is_actually_being_sent():
    """With FFmpeg off every stream is an ``auto`` stream, header included.

    ``audio/mpeg`` for a forwarded AAC stream makes a radio pick its MP3
    decoder: it plays nothing, or noise, and the station looks broken. The header
    is decided before the first byte, which is why ``_settle_kind`` waits - and
    why it has to ask the *effective* codec rather than the configured one.
    """
    from radioconv.api import _media_type

    off = Settings(ffmpeg_enabled=False)
    assert _media_type(off, "adts") == "audio/aac"
    assert _media_type(off, "mp3") == "audio/mpeg"

    # The configured codec alone would have said audio/mpeg for both.
    assert _media_type(Settings(output_codec="mp3"), None) == "audio/mpeg"
    assert _media_type(Settings(output_codec="adts"), None) == "audio/aac"

    # An explicit override still wins: it exists for picky firmware.
    assert _media_type(Settings(ffmpeg_enabled=False, mime_override="audio/aacp"), None) == (
        "audio/aacp"
    )


async def test_settling_the_codec_waits_when_ffmpeg_is_disabled():
    """``auto`` means "wait and see", and with FFmpeg off that is every stream.

    Skipping the wait would send the header before the pipeline had identified
    the source, so a forwarded ADTS station would be announced as ``audio/mpeg``
    for the life of the connection - the exact bug ``_settle_kind`` exists to
    prevent.
    """
    from types import SimpleNamespace

    from radioconv.api import _settle_kind

    session = SimpleNamespace(
        pipeline=SimpleNamespace(stats=SimpleNamespace(detected_kind="adts", mode="passthrough")),
        dead=False,
    )
    assert await _settle_kind(Settings(ffmpeg_enabled=False), session) == "adts"  # ty: ignore[invalid-argument-type]
    # A normal install with a concrete codec does not wait at all.
    assert await _settle_kind(Settings(output_codec="mp3"), session) is None  # ty: ignore[invalid-argument-type]


def test_the_ffmpeg_report_does_not_look_for_a_binary_when_it_is_disabled():
    """`--no-ffmpeg` must mean "not used", not "search for one anyway".

    ``_probe_ffmpeg`` runs the binary twice to fill in its answer, once per
    process, and ``/health`` is polled. On an installation that has deliberately
    turned FFmpeg off, reporting a path would read as a working install being
    ignored - and the whole point of the setting is that nothing is forked.
    """
    from radioconv import ffmpeg as ffmpeg_mod
    from radioconv.api import _probe_ffmpeg

    report = _probe_ffmpeg(Settings(ffmpeg_enabled=False))
    assert report["enabled"] is False
    assert report["disabled"] is True
    assert report["path"] is None
    assert report["version"] is None

    # And the enabled answer still says so explicitly, so a monitor can tell the
    # two apart without inferring it from a missing key.
    assert "enabled" in _probe_ffmpeg(Settings())
    assert ffmpeg_mod.INPUT_FORMATS["adts"] == "aac", "sanity: ffmpeg module still importable"


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
