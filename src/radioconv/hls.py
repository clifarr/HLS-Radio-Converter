"""HLS client: fetching, variant selection, decryption and live-edge tracking.

Design notes
------------
* **We fetch, the library only parses.** ``m3u8`` has a convenient
  ``m3u8.load(url)`` that does its own blocking network I/O with no timeout -
  the single most likely cause of the old version wedging for days on end. We
  always fetch with :mod:`httpx` (strict connect/read timeouts) and hand the
  *text* to ``m3u8.loads``.
* **Byte ranges, ``EXT-X-MAP`` and ``EXT-X-KEY``** are all supported because
  real radio CDNs use them, and AES-128 is very common.
* **Live edge tracking**: a live playlist is a sliding window. We join a few
  target durations behind the edge (enough to ride out jitter, small enough to
  start fast) and never crawl backwards to catch up; if we fall too far behind
  we jump forward rather than download minutes of stale audio.
* The ``m3u8`` object model has drifted a lot between v3 and v6, so every
  access goes through a small compatibility shim.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple
from urllib.parse import urljoin, urlparse

import httpx
import m3u8

from .config import Settings

log = logging.getLogger(__name__)


class PlaylistSource(Protocol):
    """The slice of :class:`HLSClient` that :class:`PlaylistTracker` needs.

    Declaring it as a protocol keeps the tracker testable with a lightweight
    stub, and documents precisely what a replacement transport must provide.
    """

    async def fetch_playlist(self, url: str) -> m3u8.M3U8:  # pragma: no cover - interface
        ...

    async def fetch_segment(self, segment: Segment) -> bytes:  # pragma: no cover - interface
        ...


class HLSError(RuntimeError):
    """A recoverable problem with the upstream stream (retry / failover)."""


class HLSFatal(HLSError):
    """The URL is not usable at all (not a playlist, unsupported encryption)."""


# --------------------------------------------------------------------- models
@dataclass
class KeySpec:
    method: str = "NONE"
    uri: Optional[str] = None
    iv: Optional[bytes] = None
    key_format: Optional[str] = None

    @property
    def encrypted(self) -> bool:
        return bool(self.method) and self.method != "NONE"


@dataclass
class Segment:
    url: str
    sequence: int
    duration: float = 0.0
    byte_range: Optional[Tuple[int, int]] = None
    key: Optional[KeySpec] = None
    discontinuity: bool = False
    #: ``True`` for the ``EXT-X-MAP`` initialisation segment of a fMP4 stream.
    is_init: bool = False
    #: Filled in once downloaded, for logging and metrics.
    bytes_downloaded: int = field(default=0, compare=False)

    def __str__(self) -> str:  # pragma: no cover - logging only
        return f"#{self.sequence}{' init' if self.is_init else ''} {self.url}"


@dataclass
class PlaylistInfo:
    """What we learned about a stream on first contact."""

    requested_url: str
    media_url: str
    is_master: bool
    variant_bandwidth: Optional[int] = None
    variant_label: Optional[str] = None
    variants_considered: int = 0
    target_duration: float = 6.0
    segment_count: int = 0
    endlist: bool = False
    encrypted: bool = False
    has_init_segment: bool = False
    #: The container the *playlist* suggests, which is only a hint: an
    #: ``EXT-X-MAP`` init segment or a ``.m4s`` URL is real evidence for MP4, but
    #: the previous "not mp4 therefore ts" default was a false dichotomy that
    #: actively mislabelled raw AAC segments. ``"unknown"`` is the honest answer,
    #: and ``tsdemux.classify_segment`` supplies the real one from the bytes.
    container: str = "unknown"

    def as_dict(self) -> Dict[str, object]:
        return {
            "requested_url": self.requested_url,
            "media_url": self.media_url,
            "is_master": self.is_master,
            "variant_bandwidth": self.variant_bandwidth,
            "variant_label": self.variant_label,
            "variants_considered": self.variants_considered,
            "target_duration": self.target_duration,
            "segment_count": self.segment_count,
            "endlist": self.endlist,
            "encrypted": self.encrypted,
            "has_init_segment": self.has_init_segment,
            "container": self.container,
        }


# ------------------------------------------------------------ compatibility
def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _spec(key_obj: Any, base_url: str) -> Optional[KeySpec]:
    if key_obj is None:
        return None
    method = (getattr(key_obj, "method", None) or "NONE").upper()
    uri = getattr(key_obj, "uri", None)
    if uri:
        uri = urljoin(base_url, uri)
    return KeySpec(
        method=method,
        uri=uri,
        iv=_parse_iv(getattr(key_obj, "iv", None)),
        key_format=_attr(key_obj, "keyformat", "key_format"),
    )


def _key_from(obj: Any, base_url: str) -> Optional[KeySpec]:
    """Resolve the *authoritative* key for a playlist or segment.

    ``m3u8`` resolves ``EXT-X-KEY`` per segment, so ``segment.key`` is already
    the right answer - including an explicit ``METHOD=NONE``, which must **not**
    be replaced by an earlier key (that would corrupt a segment that the origin
    deliberately left in the clear, and it would also panic about a key that
    does not apply).

    ``obj.keys`` (the newer list form) is only used as a fallback, and only
    when it is unambiguous.
    """
    if obj is None:
        return None

    if hasattr(obj, "key"):
        resolved = _spec(obj.key, base_url)
        if resolved is not None:
            return resolved

    many = getattr(obj, "keys", None) or []
    candidates = [spec for spec in (_spec(k, base_url) for k in many) if spec and spec.encrypted]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _parse_iv(raw: Any) -> Optional[bytes]:
    if not raw:
        return None
    cleaned = str(raw).strip().lower().removeprefix("0x")
    try:
        return bytes.fromhex(cleaned.zfill(32))
    except ValueError:
        return None


def _parse_byte_range(raw: Any, previous: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    """Normalise the many shapes ``m3u8`` has used for BYTERANGE over time."""
    if raw in (None, ""):
        return None
    length: Any = None
    offset: Any = None
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        length, offset = raw
    elif isinstance(raw, str) and "@" in raw:
        length, offset = raw.split("@", 1)
    elif isinstance(raw, str):
        length = raw
    elif hasattr(raw, "length"):
        length = raw.length
        offset = getattr(raw, "offset", None)
    else:
        return None

    try:
        length_int = int(length)
        offset_int = (
            int(offset) if offset is not None else ((previous[0] + previous[1]) if previous else 0)
        )
    except (TypeError, ValueError):
        return None
    if length_int <= 0 or offset_int < 0:
        return None
    return (offset_int, length_int)


def _init_section(playlist: Any) -> Any:
    """Find the ``EXT-X-MAP`` init segment, wherever this m3u8 version put it."""
    direct = _attr(playlist, "media_init", "init_section", "map")
    if direct is not None and getattr(direct, "uri", None):
        return direct
    for segment in getattr(playlist, "segments", []) or []:
        found = _attr(segment, "init_section", "map")
        if found is not None and getattr(found, "uri", None):
            return found
    return None


# ------------------------------------------------------- variant selection
def _variant_bandwidth(variant: Any) -> int:
    info = getattr(variant, "stream_info", None)
    for source in (info, variant):
        if source is None:
            continue
        for attr in ("bandwidth", "average_bandwidth"):
            value = getattr(source, attr, None)
            if value:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
    return 0


def _variant_is_audio_only(variant: Any) -> bool:
    info = getattr(variant, "stream_info", None)
    resolution = getattr(info, "resolution", None) if info else None
    return not resolution


def select_variant(variants: Sequence[Any], mode: str = "highest") -> Optional[Any]:
    """Pick a variant from a master playlist.

    Video renditions are de-prioritised: for an audio-only radio there is no
    point pulling a 1080p rendition just because its ``BANDWIDTH`` is higher.
    ``highest`` therefore means "the highest bitrate *audio* rendition" - the
    best source available for a fixed-rate MP3 re-encode.
    """
    variants = list(variants)
    if not variants:
        return None
    if mode == "first":
        return variants[0]
    pool = [v for v in variants if _variant_is_audio_only(v)] or variants
    ordered = sorted(pool, key=_variant_bandwidth)
    return ordered[-1] if mode == "highest" else ordered[0]


# ----------------------------------------------------------------- segments
def extract_segments(playlist: m3u8.M3U8, base_url: str) -> List[Segment]:
    """Flatten a media playlist into our own segment list."""
    static_key = _key_from(playlist, base_url)
    segments: List[Segment] = []
    previous_range: Optional[Tuple[int, int]] = None

    try:
        base_sequence = int(_attr(playlist, "media_sequence", default=0) or 0)
    except (TypeError, ValueError):
        base_sequence = 0

    init = _init_section(playlist)
    if init is not None:
        init_uri = getattr(init, "uri", None)
        if init_uri:
            segments.append(
                Segment(
                    url=urljoin(base_url, init_uri),
                    sequence=-1,
                    byte_range=_parse_byte_range(_attr(init, "byterange", "byte_range"), None),
                    key=_key_from(init, base_url) or static_key,
                    is_init=True,
                )
            )

    for index, raw in enumerate(getattr(playlist, "segments", []) or []):
        byte_range = _parse_byte_range(_attr(raw, "byterange", "byte_range"), previous_range)
        if byte_range:
            previous_range = byte_range

        sequence = _attr(raw, "media_sequence_number")
        try:
            sequence = int(sequence) if sequence is not None else base_sequence + index
        except (TypeError, ValueError):
            sequence = base_sequence + index

        segments.append(
            Segment(
                url=urljoin(base_url, raw.uri),
                sequence=sequence,
                duration=float(_attr(raw, "duration", default=0) or 0),
                byte_range=byte_range,
                key=_key_from(raw, base_url) or static_key,
                discontinuity=bool(getattr(raw, "discontinuity", False)),
            )
        )
    return segments


# --------------------------------------------------------------------- client
class HLSClient:
    """Thin, well-behaved HTTP layer for one HLS stream."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        timeout = httpx.Timeout(
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.read_timeout,
            pool=settings.connect_timeout,
        )
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            verify=not settings.insecure,
            headers=settings.headers,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            http2=False,  # an extra failure mode with no benefit for radio
        )
        self._keys: Dict[str, Tuple[float, bytes]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(
        self,
        url: str,
        *,
        byte_range: Optional[Tuple[int, int]] = None,
        limit: int = 0,
    ) -> bytes:
        headers: Dict[str, str] = {}
        if byte_range is not None:
            start, length = byte_range
            headers["Range"] = (
                f"bytes={start}-{start + length - 1}" if length else f"bytes={start}-"
            )

        last_error: Optional[Exception] = None
        for attempt in range(1, self.settings.segment_retries + 1):
            try:
                async with self._client.stream("GET", url, headers=headers) as response:
                    if byte_range is not None:
                        self._check_range_response(response, url)
                    elif response.status_code >= 400:
                        raise HLSError(f"HTTP {response.status_code} for {url}")
                    cap = limit or self.settings.max_segment_bytes
                    chunks: List[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > cap:
                            raise HLSError(f"response exceeded {cap} bytes for {url}")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    if byte_range is not None:
                        self._check_range_length(response, body, url)
                    return body
            except (httpx.HTTPError, HLSError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < self.settings.segment_retries:
                    await asyncio.sleep(min(0.5 * attempt, 2.0))
        raise HLSError(f"failed to fetch {url}: {last_error}")

    @staticmethod
    def _check_range_response(response: httpx.Response, url: str) -> None:
        """Require a 206 with a ``Content-Range`` for a byte-range request.

        A server that answers a ``Range`` request with ``200 OK`` and the
        *whole* resource is not answering the question. Accepting that
        response would splice the beginning of the file into the middle of
        the stream - the segment would then start with a PES header that
        belongs somewhere else, and the demuxer would either resynchronise
        after a garbage frame or, worse, decode a spliced stream.

        ``416 Range Not Satisfiable`` is also a hard error: the segment
        changed under us (a live playlist that rotated while we were
        fetching), and retrying with the same range would fail the same way.
        """
        if response.status_code == 416:
            raise HLSError(f"HTTP 416 (range not satisfiable) for {url}")
        if response.status_code != 206:
            raise HLSError(f"HTTP {response.status_code} (expected 206 for a byte range) for {url}")
        if not response.headers.get("content-range"):
            raise HLSError(f"HTTP 206 without Content-Range for {url}")

    @staticmethod
    def _check_range_length(response: httpx.Response, body: bytes, url: str) -> None:
        """Check the body length against the ``Content-Range`` we asked for.

        A truncated segment is a corrupt segment. Without this check the
        demuxer would be handed a partial TS packet and would resynchronise
        on the next one, losing the audio in between.
        """
        content_range = response.headers.get("content-range", "")
        # Content-Range: bytes <start>-<end>/<total>
        spec = content_range.split(" ", 1)[-1].split("/", 1)[0].strip()
        start_text, _, end_text = spec.partition("-")
        try:
            start = int(start_text)
            end = int(end_text)
        except ValueError as exc:
            raise HLSError(f"unparseable Content-Range {content_range!r} for {url}") from exc
        expected = end - start + 1
        if expected < 0:
            raise HLSError(f"invalid Content-Range {content_range!r} for {url}")
        if len(body) != expected:
            raise HLSError(f"short segment for {url}: asked for {expected} bytes, got {len(body)}")

    async def fetch_playlist(self, url: str) -> m3u8.M3U8:
        """Fetch and parse a playlist (master or media)."""
        try:
            raw = await asyncio.wait_for(
                self._get(url, limit=self.settings.max_playlist_bytes),
                timeout=self.settings.playlist_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise HLSError(
                f"playlist fetch timed out after {self.settings.playlist_timeout}s: {url}"
            ) from exc

        text = raw.decode("utf-8", "replace").lstrip("\ufeff").strip()
        if not text.startswith("#EXTM3U"):
            raise HLSFatal(f"{url} is not an HLS playlist (no #EXTM3U header)")

        try:
            playlist = m3u8.loads(text)
        except Exception as exc:
            raise HLSError(f"could not parse playlist {url}: {exc}") from exc
        return playlist

    async def fetch_segment(self, segment: Segment) -> bytes:
        data = await self._get(segment.url, byte_range=segment.byte_range)
        segment.bytes_downloaded = len(data)
        if segment.key is not None and segment.key.encrypted:
            data = await self._decrypt(data, segment.key, segment.sequence)
        return data

    # ------------------------------------------------------------- decryption
    async def fetch_key(self, uri: str) -> bytes:
        cached = self._keys.get(uri)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        key = await self._get(uri, limit=64)
        if len(key) != 16:
            raise HLSError(f"expected a 16 byte AES-128 key, got {len(key)} from {uri}")
        self._keys[uri] = (time.time(), key)
        return key

    async def _decrypt(self, data: bytes, key_spec: KeySpec, sequence: int) -> bytes:
        if not self.settings.decrypt_segments:
            raise HLSFatal("stream is AES-128 encrypted but decryption is disabled")
        if key_spec.method != "AES-128":
            raise HLSFatal(f"unsupported HLS encryption method: {key_spec.method}")
        if not key_spec.uri:
            raise HLSFatal("encrypted segment without a key URI")

        key = await self.fetch_key(key_spec.uri)
        iv = key_spec.iv or (max(sequence, 0) & 0xFFFFFFFFFFFFFFFF).to_bytes(16, "big")
        plain = await asyncio.to_thread(_aes_cbc_decrypt, key, iv, data)
        return _strip_pkcs7(plain)


#: ``cryptography`` moved ``algorithms.AES`` under ``.decrepit`` in v43.
_AES_MODULE_PATHS = (
    "cryptography.hazmat.decrepit.ciphers.algorithms",
    "cryptography.hazmat.primitives.ciphers.algorithms",
)


def _import_or_none(module_path: str) -> Any:
    """Import a module, returning ``None`` when it does not exist."""
    import importlib

    try:
        return importlib.import_module(module_path)
    except ImportError:
        # Expected: only one of the two locations exists for a given version.
        return None


def _aes_algorithm() -> Any:
    """Return the AES algorithm class from whichever module has it."""
    for module_path in _AES_MODULE_PATHS:
        module = _import_or_none(module_path)
        if module is None:
            continue
        algorithm = getattr(module, "AES", None)
        if algorithm is not None:
            return algorithm
    raise HLSFatal("AES decryption unavailable: install the 'cryptography' package")


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC decrypt, tolerant of the ``algorithms`` module move.

    Support both module locations so any pip-installed ``cryptography`` works.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    decryptor = Cipher(_aes_algorithm()(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _strip_pkcs7(data: bytes, block: int = 16) -> bytes:
    """Remove the PKCS#7 padding HLS mandates on the final AES block."""
    if not data or len(data) % block:
        return data
    pad = data[-1]
    if 0 < pad <= block and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


# ------------------------------------------------------------- live tracking
class PlaylistTracker:
    """Keeps one live media playlist in step with the encoder.

    ``poll()`` returns the segments that became available since the previous
    call, in play order. It understands master playlists, the sliding window
    and ``EXT-X-MAP`` initialisation segments.
    """

    def __init__(self, client: PlaylistSource, url: str, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.requested_url = url
        self.media_url = url

        self.info: Optional[PlaylistInfo] = None
        self.target_duration = 6.0
        self.endlist = False
        self.live_edge_segments = 3

        self._cached: List[Segment] = []
        self._cached_at = 0.0
        self._last_sequence: Optional[int] = None
        self._started = False

    # -------------------------------------------------------------- discovery
    async def prime(self, *, live_edge_segments: int = 3) -> PlaylistInfo:
        """Resolve master -> media playlist and record what we found."""
        self.live_edge_segments = max(1, live_edge_segments)
        playlist = await self.client.fetch_playlist(self.requested_url)
        is_master = bool(getattr(playlist, "is_variant", False))
        considered = 0
        bandwidth: Optional[int] = None
        label: Optional[str] = None

        for _ in range(3):  # nested masters do exist in the wild
            if not getattr(playlist, "is_variant", False):
                break
            variants = list(getattr(playlist, "playlists", []) or [])
            if not variants:
                raise HLSFatal(f"master playlist has no variants: {self.media_url}")
            considered = max(considered, len(variants))
            chosen = select_variant(variants, self.settings.variant_selection)
            if chosen is None:
                # ("choose", not "select": the latter trips ruff's S608 SQL
                # heuristic on the f-string, which is a false positive.)
                raise HLSFatal(f"could not choose a variant from {self.media_url}")
            bandwidth = _variant_bandwidth(chosen)
            stream_info = getattr(chosen, "stream_info", None)
            label = _attr(stream_info, "name", "audio") if stream_info is not None else None
            self.media_url = urljoin(self.media_url, chosen.uri)
            playlist = await self.client.fetch_playlist(self.media_url)

        segments = extract_segments(playlist, self.media_url)
        if not segments:
            raise HLSFatal(f"playlist contains no segments: {self.media_url}")

        self.target_duration = float(_attr(playlist, "target_duration", default=6) or 6)
        self.endlist = bool(getattr(playlist, "is_endlist", False))
        self._cached = segments
        self._cached_at = time.time()

        media = [s for s in segments if not s.is_init]
        encrypted = any(bool(s.key and s.key.encrypted) for s in media)
        has_init = any(s.is_init for s in segments)

        self.info = PlaylistInfo(
            requested_url=self.requested_url,
            media_url=self.media_url,
            is_master=is_master,
            variant_bandwidth=bandwidth,
            variant_label=label,
            variants_considered=considered,
            target_duration=self.target_duration,
            segment_count=len(media),
            endlist=self.endlist,
            encrypted=encrypted,
            has_init_segment=has_init,
            container="mp4" if has_init or _url_looks_mp4(self.media_url) else "unknown",
        )
        log.info(
            "playlist ready: master=%s variants=%s bandwidth=%s segments=%s encrypted=%s target=%.1fs",
            is_master,
            considered,
            bandwidth,
            len(media),
            encrypted,
            self.target_duration,
        )
        return self.info

    # --------------------------------------------------------------- polling
    def take_buffered(self) -> List[Segment]:
        """Segments to play from the currently cached playlist."""
        if self.info is None:
            return []
        media = [s for s in self._cached if not s.is_init]
        if not media:
            return []

        out: List[Segment] = []
        if not self._started:
            if self.info.has_init_segment:
                out.extend(s for s in self._cached if s.is_init)
            if self.endlist:
                out.extend(media)
            else:
                # Never start on the very last segment: the origin may still be
                # writing it, and a little jitter buffer avoids a stutter.
                out.extend(media[max(0, len(media) - self.live_edge_segments) :])
            self._last_sequence = media[-1].sequence
            self._started = True
        else:
            new = [
                s for s in media if self._last_sequence is None or s.sequence > self._last_sequence
            ]
            if not new:
                return []
            if len(new) > 6:
                # We fell behind badly (network stall, radio asleep). Skip ahead
                # rather than replay a minute of stale audio.
                skipped = len(new) - self.live_edge_segments
                log.warning(
                    "playlist advanced by %d segments; skipping %d to reach the live edge",
                    len(new),
                    skipped,
                )
                new = new[-self.live_edge_segments :]
            out.extend(new)
            self._last_sequence = new[-1].sequence

        self._cached_at = time.time()
        return out

    def _reload_interval(self) -> float:
        """Seconds between live playlist checks.

        Must be comfortably **under half** the segment duration. The old rule was
        ``target_duration * 0.75``, which for a 10-second-segment station is 8.25s
        - and because a check that finds nothing new is simply repeated after the
        same interval, two consecutive checks could be 16.5 seconds apart against
        a 10-second segment period. Each occurrence lost a whole segment's worth
        of time, so the pipeline delivered ~0.88x realtime, with gaps of up to 16
        seconds in which a listener received *nothing at all*.

        Staying at or below half the period guarantees at least two checks per
        segment, so a segment cannot be missed however the phases line up. The 4s
        cap keeps the check rate sane on slow-segment stations (a 10s period needs
        no more than a check every 5s) and the 0.5s floor stops very short
        segments from turning into a poll flood.

        The playlist is a few hundred bytes, so this is a cheap request - and it
        is the difference between a listener having a jitter buffer and not.
        """
        return min(4.0, max(0.5, self.target_duration / 2))

    def should_reload(self, *, force: bool = False) -> bool:
        if self.endlist:
            return False
        if force or not self._started:
            return True
        return (time.time() - self._cached_at) >= self._reload_interval()

    async def poll(self, *, force: bool = False) -> List[Segment]:
        """Reload the playlist if it is due and return newly available segments."""
        if self.info is None:
            await self.prime(live_edge_segments=self.live_edge_segments)
            return self.take_buffered()

        if self.endlist:
            return self.take_buffered() if self._has_pending_vod() else []

        if not self.should_reload(force=force):
            return []

        playlist = await self.client.fetch_playlist(self.media_url)
        if getattr(playlist, "is_variant", False):
            raise HLSError("media playlist unexpectedly became a master playlist")
        segments = extract_segments(playlist, self.media_url)
        if not segments:
            raise HLSError(f"playlist became empty: {self.media_url}")
        self.target_duration = float(
            _attr(playlist, "target_duration", default=self.target_duration) or self.target_duration
        )
        self.endlist = bool(getattr(playlist, "is_endlist", False))
        self._cached = segments
        self._cached_at = time.time()
        return self.take_buffered()

    def _has_pending_vod(self) -> bool:
        media = [s for s in self._cached if not s.is_init]
        if not media:
            return False
        if self._last_sequence is None:
            return True
        return self._last_sequence < media[-1].sequence

    def restart_vod(self) -> None:
        """Rewind a finite (VOD) playlist so the stream can loop."""
        self._started = False
        self._last_sequence = None
        self._cached_at = 0.0

    def seconds_until_reload(self) -> float:
        if self.endlist:
            return 1.0
        return max(0.2, self._reload_interval() - (time.time() - self._cached_at))


def _url_looks_mp4(url: str) -> bool:
    return urlparse(url).path.lower().endswith((".mp4", ".m4a", ".m4s", ".cmfv", ".cmfa"))


# ------------------------------------------------------------------ one-shots
async def inspect(url: str, settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Everything we can learn about a stream without downloading much.

    Backs ``radioconv probe`` and the ``/probe`` endpoint: playlist shape, the
    variant we would choose, encryption, container, the real audio codec, and
    whether the FFmpeg-free pass-through path will be used.
    """
    from . import ffmpeg as ffmpeg_mod
    from .tsdemux import PASSTHROUGH_TYPES, classify_segment, scan_tables

    settings = settings or Settings()
    client = HLSClient(settings)
    result: Dict[str, Any] = {"url": url, "ok": False}
    try:
        tracker = PlaylistTracker(client, url, settings)
        info = await tracker.prime()
        result.update(info.as_dict())
        result["ok"] = True

        segment = next((s for s in tracker.take_buffered() if not s.is_init), None)
        if segment is not None:
            data = await client.fetch_segment(segment)
            result["segment_bytes"] = len(data)
            result["segment_seconds"] = segment.duration or None
            detected = classify_segment(data)
            result["segment_container"] = detected
            if detected:
                # Report what the bytes actually are. `container` above came from
                # the playlist, and for a raw AAC stream it said "ts" - which is
                # precisely the wrong answer that made this stream look like an
                # ffmpeg problem rather than a detection one.
                result["container"] = detected
            if detected == "ts":
                tables = scan_tables(data)
                result["ts_streams"] = [
                    {"type": t, "pid": p, "playable_as": PASSTHROUGH_TYPES.get(t)}
                    for t, p in tables["streams"]
                ]
                kind = PASSTHROUGH_TYPES.get(tables["audio"]) if tables["audio"] else None
                result["passthrough_possible"] = kind is not None
                result["passthrough_kind"] = kind
            else:
                # Pass-through is a TS-table lookup, so it is only available for a
                # transport stream. Raw ADTS can still avoid a re-encode, but by
                # remuxing (`output_codec = "adts"`), which is a different path.
                result["passthrough_possible"] = False
                result["passthrough_kind"] = None
            result["audio"] = await asyncio.to_thread(
                ffmpeg_mod.probe_bytes, data, settings, 20.0, detected
            )
    except Exception as exc:  # noqa: BLE001 - /probe must always answer, never raise
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__
    finally:
        await client.aclose()
    return result
