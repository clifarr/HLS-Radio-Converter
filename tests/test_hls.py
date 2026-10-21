"""Tests for the HLS layer: variant choice, keys, byte ranges, live edge."""

from __future__ import annotations

import httpx
import m3u8
import pytest

from radioconv import hls
from radioconv.config import Settings

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=512000,CODECS="avc1.4d401f,mp4a.40.2",RESOLUTION=1920x1080
video/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=128000,CODECS="mp4a.40.2"
high/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=32000,CODECS="mp4a.40.2"
tiny/index.m3u8
"""


def media_playlist(sequence: int = 0, count: int = 4, endlist: bool = False) -> str:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:6",
        f"#EXT-X-MEDIA-SEQUENCE:{sequence}",
    ]
    for index in range(count):
        lines += ["#EXTINF:6.0,", f"seg{sequence + index}.ts"]
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def parse(text: str) -> m3u8.M3U8:
    return m3u8.loads(text)


# ------------------------------------------------------------------- variants
def test_highest_prefers_audio_over_a_higher_bandwidth_video_rendition():
    """'Highest' must mean the best *audio*, not a 1080p video rendition."""
    playlist = parse(MASTER)
    chosen = hls.select_variant(playlist.playlists, "highest")
    assert chosen is not None
    assert chosen.uri == "high/index.m3u8"
    assert hls._variant_bandwidth(chosen) == 128_000


def test_lowest_and_first():
    playlist = parse(MASTER)
    lowest = hls.select_variant(playlist.playlists, "lowest")
    first = hls.select_variant(playlist.playlists, "first")
    assert lowest is not None and lowest.uri == "tiny/index.m3u8"
    assert first is not None and first.uri == "low/index.m3u8"


def test_variants_fall_back_to_video_when_there_is_nothing_else():
    text = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=64000,RESOLUTION=640x360
a/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=1280x720
b/index.m3u8
"""
    playlist = parse(text)
    chosen = hls.select_variant(playlist.playlists, "highest")
    assert chosen is not None and chosen.uri == "b/index.m3u8"


def test_no_variants():
    assert hls.select_variant([], "highest") is None


# ----------------------------------------------------------------- byte range
@pytest.mark.parametrize(
    "raw,previous,expected",
    [
        (None, None, None),
        ("1000@500", None, (500, 1000)),  # (offset, length)
        ((1000, 500), None, (500, 1000)),  # m3u8 sometimes hands us (length, offset)
        ("1000", (500, 1000), (1500, 1000)),  # implicit offset continues the previous range
        ("1000", None, (0, 1000)),
        ("bad", None, None),
        ("-5@0", None, None),
    ],
)
def test_byte_range_parsing(raw, previous, expected):
    assert hls._parse_byte_range(raw, previous) == expected


# ----------------------------------------------------------------------- keys
def test_key_rotation_including_explicit_none():
    text = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-KEY:METHOD=AES-128,URI="https://keys.example.com/k1",IV=0x00000000000000000000000000000064
#EXTINF:6.0,
a.ts
#EXT-X-KEY:METHOD=NONE
#EXTINF:6.0,
b.ts
"""
    segments = hls.extract_segments(parse(text), "https://cdn/index.m3u8")
    assert segments[0].key is not None and segments[0].key.encrypted
    assert segments[0].key.uri == "https://keys.example.com/k1"
    assert segments[0].key.iv == bytes.fromhex("00" * 15 + "64")
    # An explicit NONE must NOT inherit the earlier key.
    assert segments[1].key is not None
    assert not segments[1].key.encrypted


def test_plain_playlist_has_no_keys():
    segments = hls.extract_segments(parse(media_playlist()), "https://cdn/index.m3u8")
    assert all(segment.key is None for segment in segments)


async def test_unrecognised_encryption_is_refused():
    client = hls.HLSClient(Settings())
    try:
        with pytest.raises(hls.HLSFatal):
            await client._decrypt(b"x" * 32, hls.KeySpec(method="SAMPLE-AES", uri="https://x/k"), 0)
    finally:
        await client.aclose()


def test_pkcs7_stripping():
    assert hls._strip_pkcs7(bytes([1, 2, 3]) + bytes([13]) * 13) == bytes([1, 2, 3])
    assert hls._strip_pkcs7(bytes([9]) * 16) == bytes([9]) * 7
    # pad byte 0 is not valid PKCS#7, and "short" is not block aligned: leave alone
    assert hls._strip_pkcs7(bytes(16)) == bytes(16)
    assert hls._strip_pkcs7(b"short") == b"short"
    unpadded = bytes(range(16, 32))
    assert hls._strip_pkcs7(unpadded) == unpadded


# --------------------------------------------------------- byte-range requests
def _range_response(status: int, body: bytes, content_range: str = "") -> httpx.Response:
    headers = {}
    if content_range:
        headers["Content-Range"] = content_range
    return httpx.Response(status, content=body, headers=headers)


def test_a_byte_range_request_must_be_answered_with_206():
    """A 200 for a Range request is the whole file, not the range.

    Accepting it would splice the *beginning* of the file into the middle of
    the stream: the segment would start with a PES header that belongs
    somewhere else, and the demuxer would either resynchronise after a
    garbage frame or decode a spliced stream.
    """
    url = "https://cdn/seg.ts"
    with pytest.raises(hls.HLSError, match="expected 206"):
        hls.HLSClient._check_range_response(_range_response(200, b"x" * 10), url)
    # 416 means the segment changed under us; retrying the same range fails
    # the same way, so it is a hard error too.
    with pytest.raises(hls.HLSError, match="416"):
        hls.HLSClient._check_range_response(_range_response(416, b""), url)
    # A 206 with no Content-Range is not usable either.
    with pytest.raises(hls.HLSError, match="Content-Range"):
        hls.HLSClient._check_range_response(_range_response(206, b"x" * 10), url)
    # The good case.
    hls.HLSClient._check_range_response(_range_response(206, b"x" * 10, "bytes 0-9/100"), url)


def test_a_truncated_segment_is_refused():
    """A short body is a corrupt segment, not a segment.

    Without this check the demuxer would be handed a partial TS packet and
    would resynchronise on the next one, losing the audio in between.
    """
    url = "https://cdn/seg.ts"
    response = _range_response(206, b"x" * 5, "bytes 0-9/100")
    with pytest.raises(hls.HLSError, match="short segment"):
        hls.HLSClient._check_range_length(response, b"x" * 5, url)
    # The good case.
    hls.HLSClient._check_range_length(response, b"x" * 10, url)
    # An unparseable Content-Range is an error, not a silent pass.
    with pytest.raises(hls.HLSError, match="Content-Range"):
        hls.HLSClient._check_range_length(
            _range_response(206, b"x" * 10, "bytes nonsense/100"), b"x" * 10, url
        )


async def test_a_server_that_ignores_the_range_is_refused():
    """End to end through the client: a 200 answer to a Range request fails.

    This is the shape a CDN takes when it does not support byte ranges at
    all, and it is exactly the case that used to be accepted silently.
    """
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"the whole file"))
    client = hls.HLSClient(Settings(segment_retries=1))
    client._client = httpx.AsyncClient(transport=transport)
    try:
        with pytest.raises(hls.HLSError, match="expected 206"):
            await client._get("https://cdn/seg.ts", byte_range=(100, 50))
    finally:
        await client.aclose()


async def test_a_correct_range_response_is_accepted():
    """The good path still works: 206, a Content-Range, and the exact bytes."""
    body = b"0123456789"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Range"] == "bytes=100-109"
        return httpx.Response(206, content=body, headers={"Content-Range": "bytes 100-109/1000"})

    client = hls.HLSClient(Settings(segment_retries=1))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        got = await client._get("https://cdn/seg.ts", byte_range=(100, 10))
        assert got == body
    finally:
        await client.aclose()


def test_aes_128_decrypt_round_trip():
    key = bytes(range(16))
    iv = bytes(range(16, 32))
    plaintext = b"0123456789abcdef" * 4
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    # Use the production lookup, so this test also covers the module probing
    # that keeps working across cryptography's v43 relocation of algorithms.AES.
    AES = hls._aes_algorithm()

    encryptor = Cipher(AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    assert hls._aes_cbc_decrypt(key, iv, ciphertext) == plaintext


# ------------------------------------------------------------------- segments
def test_fmp4_init_segment_and_byte_ranges():
    text = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:4
#EXT-X-MAP:URI="init.mp4",BYTERANGE="800@0"
#EXTINF:4.0,
seg1.m4s
#EXTINF:4.0,
seg2.m4s
"""
    segments = hls.extract_segments(parse(text), "https://cdn/path/index.m3u8")
    assert segments[0].is_init and segments[0].byte_range == (0, 800)
    assert segments[0].url == "https://cdn/path/init.mp4"
    assert [s.sequence for s in segments[1:]] == [0, 1]
    assert segments[1].url == "https://cdn/path/seg1.m4s"


def test_media_sequence_numbers_are_resolved():
    segments = hls.extract_segments(
        parse(media_playlist(sequence=500, count=3)), "https://cdn/i.m3u8"
    )
    assert [s.sequence for s in segments] == [500, 501, 502]


# ---------------------------------------------------------------- live track
class StubClient:
    """A stand-in for :class:`HLSClient` that serves canned playlists."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def fetch_playlist(self, url):
        self.calls.append(url)
        if isinstance(self.pages, dict):
            return parse(self.pages[url])
        return parse(self.pages[0])

    async def fetch_segment(self, segment):
        return b"data" * 4

    async def aclose(self):
        return None


async def test_tracker_resolves_master_and_starts_at_the_live_edge():
    client = StubClient(
        {
            "https://cdn/master.m3u8": MASTER,
            "https://cdn/high/index.m3u8": media_playlist(count=4),
        }
    )
    tracker = hls.PlaylistTracker(client, "https://cdn/master.m3u8", Settings())
    info = await tracker.prime()
    assert info.media_url == "https://cdn/high/index.m3u8"
    assert info.is_master and info.variant_bandwidth == 128_000
    # Live playlist: join with 3 segments and never start on the newest one.
    assert [s.sequence for s in tracker.take_buffered()] == [1, 2, 3]
    assert tracker.take_buffered() == []


async def test_tracker_only_returns_new_segments():
    client = StubClient([media_playlist(sequence=0, count=4)])
    tracker = hls.PlaylistTracker(client, "https://cdn/i.m3u8", Settings())
    await tracker.prime()
    assert [s.sequence for s in tracker.take_buffered()] == [1, 2, 3]

    client.pages = [media_playlist(sequence=1, count=4)]
    # poll() *is* the delivery mechanism: it returns what became available.
    assert [s.sequence for s in await tracker.poll(force=True)] == [4]
    assert tracker.take_buffered() == []


async def test_tracker_skips_ahead_when_it_falls_far_behind():
    client = StubClient([media_playlist(sequence=0, count=4)])
    tracker = hls.PlaylistTracker(client, "https://cdn/i.m3u8", Settings())
    await tracker.prime()
    tracker.take_buffered()

    client.pages = [media_playlist(sequence=50, count=10)]
    fresh = await tracker.poll(force=True)
    # Jump to the live edge instead of replaying 57 segments of stale audio.
    assert [s.sequence for s in fresh] == [57, 58, 59]


async def test_vod_playlist_plays_from_the_start_and_then_ends():
    client = StubClient([media_playlist(sequence=0, count=3, endlist=True)])
    tracker = hls.PlaylistTracker(client, "https://cdn/i.m3u8", Settings())
    await tracker.prime()
    assert [s.sequence for s in tracker.take_buffered()] == [0, 1, 2]
    assert tracker.take_buffered() == []
    tracker.restart_vod()
    assert [s.sequence for s in tracker.take_buffered()] == [0, 1, 2]
