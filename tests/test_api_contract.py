"""The HTTP surface as *documented*: what each endpoint does, and `/docs` quality.

Two things are tested here that were previously wrong or missing:

1. **Descriptions.** Every endpoint a user can call must carry a summary and a
   description in the OpenAPI schema, because `/docs` is the reference people
   actually read. A route with no description is a route nobody understands -
   the original complaint about this API was that ``/probe``, ``/stream`` and
   the ``POST /`` were all unexplained.
2. **The ``name`` control parameter.** It was listed in ``CONTROL_PARAMS`` (so it
   was silently stripped from the upstream URL) but nothing ever read it, so
   setting it did nothing - and ``icy-name``, which is what a radio *displays*,
   was always the raw source URL.
"""

from __future__ import annotations

import json

import httpx

from radioconv.api import MAX_NAME_LENGTH, create_app, parse_label


# --------------------------------------------------------------- parse_label
def test_name_is_used_when_given():
    assert parse_label({"name": "BBC Radio 6"}, "fallback") == "BBC Radio 6"


def test_fallback_used_when_name_absent_or_blank():
    assert parse_label({}, "http://example.com/x.m3u8") == "http://example.com/x.m3u8"
    assert parse_label({"name": ""}, "fallback") == "fallback"
    assert parse_label({"name": "   "}, "fallback") == "fallback"


def test_name_whitespace_is_collapsed():
    """It becomes an HTTP header value, so it must be a single tidy line."""
    assert parse_label({"name": "  BBC    Radio\n6  "}, "fb") == "BBC Radio 6"


def test_name_cannot_inject_a_header():
    """CR/LF would let a caller forge additional response headers."""
    result = parse_label({"name": "Station\r\nX-Injected: yes"}, "fb")
    assert "\r" not in result
    assert "\n" not in result
    assert result == "Station X-Injected: yes"


def test_name_is_length_bounded():
    assert len(parse_label({"name": "x" * 5000}, "fb")) == MAX_NAME_LENGTH


def test_name_of_only_control_characters_falls_back():
    assert parse_label({"name": "\r\n\r\n"}, "fallback") == "fallback"


# ------------------------------------------------- name reaches the radio
def test_name_parameter_sets_the_icy_header(live_server, hls_server):
    """The whole point of `name`: a radio shows it instead of a URL."""
    aac_url, _ = hls_server
    with httpx.stream(
        "GET",
        live_server + "/stream",
        params={"url": aac_url, "name": "BBC Radio 6 Music"},
        timeout=30,
    ) as response:
        assert response.status_code == 200
        assert response.headers["icy-name"] == "BBC Radio 6 Music"
        for _ in response.iter_bytes():
            break


def test_without_name_the_icy_header_is_the_source_url(live_server, hls_server):
    """Documented fallback behaviour, so the display is never blank."""
    aac_url, _ = hls_server
    with httpx.stream(
        "GET", live_server + "/stream", params={"url": aac_url}, timeout=30
    ) as response:
        assert response.status_code == 200
        assert aac_url in response.headers["icy-name"]
        for _ in response.iter_bytes():
            break


def test_name_also_works_on_the_legacy_encoded_route(live_server, hls_server):
    """Radios on the v1 URL shape must be able to name a station too."""
    from urllib.parse import quote

    aac_url, _ = hls_server
    with httpx.stream(
        "GET",
        f"{live_server}/{quote(aac_url, safe='')}",
        params={"name": "Legacy Station"},
        timeout=30,
    ) as response:
        assert response.status_code == 200
        assert response.headers["icy-name"] == "Legacy Station"
        for _ in response.iter_bytes():
            break


def test_name_is_not_forwarded_to_the_origin(live_server, hls_server):
    """`name` belongs to us; leaking it upstream could break a signed URL."""
    from radioconv.api import decode_target

    assert decode_target("https://example.com/a.m3u8", "name=Radio+6") == (
        "https://example.com/a.m3u8"
    )
    # A non-control parameter still IS forwarded - radios send auth tokens that way.
    assert decode_target("https://example.com/a.m3u8", "token=abc") == (
        "https://example.com/a.m3u8?token=abc"
    )


# ------------------------------------------------------------------ browser page
def test_index_page_shows_the_radio_url(live_server):
    """The page's whole job: hand you a URL to type into a radio.

    This used to be a ``POST`` handler that (a) returned 500 because
    ``python-multipart`` is not a dependency and (b) *streamed* audio, despite
    the button saying "Get radio URL". A plain GET form does what it says.
    """
    source = "https://example.com/live/index.m3u8"
    response = httpx.get(live_server + "/", params={"url": source}, timeout=10)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "https%3A%2F%2Fexample.com%2Flive%2Findex.m3u8" in response.text
    # And it must NOT have started streaming.
    assert not response.headers.get("icy-br")


def test_index_page_offers_a_link_to_listen(live_server):
    source = "https://example.com/live/index.m3u8"
    body = httpx.get(live_server + "/", params={"url": source}, timeout=10).text
    assert f"{live_server}/stream?url=" in body


def test_index_page_rejects_a_non_url(live_server):
    body = httpx.get(live_server + "/", params={"url": "not-a-url"}, timeout=10).text
    assert "does not look like a stream URL" in body


def test_index_page_without_a_url_is_just_the_form(live_server):
    response = httpx.get(live_server + "/", timeout=10)
    assert response.status_code == 200
    assert "<form" in response.text
    assert "Radio URL" not in response.text, "nothing to convert, so no result block"


def test_index_form_uses_a_get_so_no_body_is_parsed(live_server):
    """A POST form would need python-multipart; a GET form needs nothing."""
    body = httpx.get(live_server + "/", timeout=10).text
    assert 'method="get"' in body
    assert 'method="post"' not in body


def test_there_is_no_post_root_endpoint(live_server):
    """The broken form handler is gone, not merely fixed."""
    response = httpx.post(live_server + "/", data={"url": "x"}, timeout=10)
    assert response.status_code == 405, "POST / should no longer be routed"


# ----------------------------------------------- the page's own stream check
def test_the_page_shows_the_probe_diagnostics_when_asked(live_server, hls_server):
    """The feature: the same facts as `/probe`, where the URL is entered.

    Reported as "we have this nice /probe endpoint, but it loses its charm since
    we don't have a UI for it". The page now runs the identical call, so the two
    can never disagree - which is why this compares against a real `/probe`
    response rather than against hard-coded strings that would pin the fixture's
    sample rate.
    """
    aac_url, _ = hls_server
    body = httpx.get(live_server + "/", params={"url": aac_url, "probe": "1"}, timeout=60).text
    probe = httpx.get(live_server + "/probe", params={"url": aac_url}, timeout=60).json()

    assert "Stream check" in body
    assert "Engine" in body
    assert f">{probe['container']}<" in body, "the measured container is missing"
    assert probe["audio"]["codec"].upper() in body, "the source audio was not described"
    assert f"{probe['audio']['sample_rate'] / 1000:g} kHz" in body
    # The engine verdict is the question the page exists to answer, and this
    # stream needs ffmpeg (AAC in TS, output mp3 by default).
    assert "transcode" in body
    assert "ffmpeg" in body.lower()


def test_the_page_offers_the_url_that_needs_no_ffmpeg(live_server, hls_server):
    """A station that *could* be forwarded is the one case worth acting on.

    The advice has to be a URL, not a sentence: "set output_codec = adts" is an
    instruction for somebody with shell access, while the page is used by
    somebody holding a radio - and ``?codec=`` is the per-request form of the
    same choice, which changes nothing for other listeners.
    """
    aac_url, _ = hls_server
    body = httpx.get(live_server + "/", params={"url": aac_url, "probe": "1"}, timeout=60).text

    assert "avoids FFmpeg" in body
    assert "codec=adts" in body.replace("&amp;", "&"), "the pass-through URL is missing"


def test_a_failing_check_is_reported_on_the_page(live_server):
    """An unreachable station is a sentence, not a 500 or a spinner.

    The form is the first thing anybody tries with a URL they were given, and
    "it just hung" is the least useful possible answer.
    """
    body = httpx.get(
        live_server + "/",
        params={"url": "http://127.0.0.1:1/dead.m3u8", "probe": "1"},
        timeout=30,
    ).text
    assert "Stream check" in body
    assert "Could not check this stream" in body


def test_the_check_can_be_opted_out_even_with_probe_in_the_url(live_server, hls_server):
    """Unticking the box must remove the parameter, and the page must agree.

    The checkbox reflects what the user chose: after a check it stays ticked, and
    after an unticked submit it must not silently turn itself back on.
    """
    aac_url, _ = hls_server
    ticked = httpx.get(live_server + "/", params={"url": aac_url, "probe": "1"}, timeout=60).text
    unticked = httpx.get(live_server + "/", params={"url": aac_url}, timeout=60).text

    assert 'name="probe"' in ticked and "checked" in _probe_input(ticked)
    assert "checked" not in _probe_input(unticked), "the box re-ticks itself"
    # And a first visit defaults to on, so the diagnostics are discoverable.
    first_visit = httpx.get(live_server + "/", timeout=10).text
    assert "checked" in _probe_input(first_visit)


def _probe_input(body: str) -> str:
    """The ``<input>`` element for the probe checkbox, as it was rendered."""
    import re

    match = re.search(r'<input[^>]*name="probe"[^>]*>', body)
    assert match, "the converter page has no probe checkbox"
    return match.group(0)


# --------------------------------------------------------------------- /probe
def test_probe_returns_json_and_does_not_stream(live_server, hls_server):
    """`/probe` is a dry run: it answers and closes, it does not attach."""
    aac_url, _ = hls_server
    response = httpx.get(live_server + "/probe", params={"url": aac_url}, timeout=30)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    # A stream would be chunked with no length; a probe has a complete body.
    assert "content-length" in response.headers
    body = response.json()
    # The real key names - not the CLI's human-facing labels.
    assert {"ok", "container", "audio", "passthrough_possible"} <= set(body), sorted(body)
    assert {"codec", "sample_rate", "channels"} <= set(body["audio"])


def test_probe_rejects_non_http_urls(live_server):
    response = httpx.get(live_server + "/probe", params={"url": "file:///etc/passwd"}, timeout=10)
    assert response.status_code == 400


# ----------------------------------------------------------------- /docs quality
def test_every_public_route_is_described(live_server):
    """`/docs` is the reference people read, so it must not be a list of stubs."""
    schema = httpx.get(live_server + "/openapi.json", timeout=10).json()

    undocumented = []
    for path, methods in schema["paths"].items():
        for method, spec in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            missing = [k for k in ("summary", "description") if not spec.get(k)]
            if missing:
                undocumented.append(f"{method.upper()} {path} missing {missing}")

    assert not undocumented, "endpoints with no explanation:\n  " + "\n  ".join(undocumented)


def test_openapi_documents_the_controls_and_the_streaming_caveat(live_server):
    """The things a caller cannot guess from the signature must be written down."""
    text = json.dumps(httpx.get(live_server + "/openapi.json", timeout=10).json())

    # Every control parameter is named in the /stream description.
    for control in ("br", "codec", "name"):
        assert control in text, f"{control!r} is undocumented"

    # The surprising behaviours are called out explicitly.
    assert "does not stream" in text or "does not start streaming" in text, (
        "/ must say it does not play audio"
    )
    assert "No stream is started" in text, "/probe must say it does not play anything"
    assert "never" in text.lower(), "streaming responses should be described as endless"


def test_openapi_tags_group_the_endpoints(live_server):
    schema = httpx.get(live_server + "/openapi.json", timeout=10).json()
    names = {tag["name"] for tag in schema.get("tags", [])}
    assert {"playback", "diagnostics", "browser"} <= names
    for spec in schema["paths"].values():
        for method in spec.values():
            assert method.get("tags"), "every endpoint should be grouped"


def test_legacy_wildcard_route_is_hidden_from_the_schema(live_server):
    """A `/{path}` catch-all would swamp /docs and mislead readers."""
    schema = httpx.get(live_server + "/openapi.json", timeout=10).json()
    assert not [p for p in schema["paths"] if "encoded_url" in p]


# ------------------------------------------------------------------ sanity
def test_app_builds_and_serves_docs():
    app = create_app()
    assert app.title == "HLS Radio Converter"
    assert app.description and "probe" in app.description
