"""A small, dependency-free MPEG-TS demuxer.

Why this exists
---------------
FFmpeg was previously needed for *everything*, even when the HLS segments
already contained a stream the radio could play verbatim. The typical HLS radio
segment is an MPEG-TS carrying a single audio elementary stream, and pulling
that elementary stream out is just packet parsing - no decoder, no encoder, no
CPU. Doing it in Python means:

* **no FFmpeg process** for pass-through streams: a Raspberry Pi streams a
  station at well under 1% of one core, and there is no extra process to leak,
  wedge or OOM;
* if the radio understands the source codec (AAC in modern firmware), an
  AAC/ADTS source needs *no* re-encode at all - only the ADTS framing is
  produced, which is what ``audio/aac`` HTTP streams are.

Everything here is pure, synchronous and side-effect free so it is trivially
unit-testable: build a TS with :func:`build_test_ts` and assert on the output.

Only these elementary stream types are treated as directly playable:

===== ==========================================
0x03  MPEG-1 audio (MP3 in a TS, what old radios love)
0x04  MPEG-2 audio (MP3 in a TS)
0x0F  AAC in ADTS framing
===== ==========================================

Everything else (HE-AAC in LATM ``0x11``, AC-3, Opus, ...) falls back to
FFmpeg, which is the correct answer for those.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47
NULL_PID = 0x1FFF

STREAM_TYPE_MP3 = 0x03
STREAM_TYPE_MP3_MPEG2 = 0x04
STREAM_TYPE_AAC_ADTS = 0x0F

#: stream_type -> the container ffmpeg/the client expects us to emit.
PASSTHROUGH_TYPES: Dict[int, str] = {
    STREAM_TYPE_MP3: "mp3",
    STREAM_TYPE_MP3_MPEG2: "mp3",
    STREAM_TYPE_AAC_ADTS: "adts",
}

#: Never treat these as a plain single audio stream: they are surround/object
#: codecs (or LATM-wrapped AAC) that a Frontier Silicon radio cannot play and
#: that must be decoded properly by FFmpeg.
EXCLUDED_TYPES = {0x11, 0x1C, 0x81, 0x87}  # LATM AAC, MPEG-4 audio, AC-3, E-AC-3

#: An ID3v2 tag: "ID3", a version, flags, then a syncsafe 4-byte length.
ID3_MAGIC = b"ID3"
ID3_HEADER_SIZE = 10

#: How many ID3v2 tags we will skip at the start of one segment. Real segments
#: carry one or two (`PRIV` with the Apple `transportStreamTimestamp`, then
#: `TEXT` with song metadata) and the bound only exists so a corrupt length can
#: never walk us off the end of the buffer.
MAX_ID3_TAGS = 8


# --------------------------------------------------------------------- detect
def looks_like_ts(data: bytes) -> bool:
    """Cheap check for a 188-byte MPEG-TS.

    Sync bytes alone are not enough: ``0x47`` is the ASCII letter ``'G'``, so a
    text file full of G's lines up perfectly at 188-byte intervals. We also
    require the transport header fields to be *plausible* (a non-reserved
    adaptation_field_control, no transport error flag), which real streams
    always satisfy and text never does.
    """
    if len(data) < TS_PACKET_SIZE:
        return False
    for index in range(0, min(len(data) - TS_PACKET_SIZE + 1, 5 * TS_PACKET_SIZE), TS_PACKET_SIZE):
        packet = data[index : index + TS_PACKET_SIZE]
        if packet[0] != TS_SYNC_BYTE:
            return False
        if packet[1] & 0x80:  # transport_error_indicator set
            return False
        if ((packet[3] >> 4) & 0x03) == 0:  # adaptation_field_control 0 is reserved
            return False
    return True


def looks_like_mp4(data: bytes) -> bool:
    """Cheap check for a fragmented-MP4 (``.m4s``) HLS segment."""
    if len(data) < 12:
        return False
    box = data[4:8]
    return box in (b"ftyp", b"styp", b"moof", b"moov", b"sidx")


def classify_segment(data: bytes) -> Optional[str]:
    """Identify a segment's container from its first bytes.

    Returns ``"ts"``, ``"mp4"``, ``"adts"`` (raw ADTS AAC), ``"mp3"`` (raw MPEG
    audio), or ``None`` when the bytes are not recognised.

    All four occur in real HLS, and only the first two used to be recognised -
    which is why an ``.aac`` segment was not merely mis-identified but actively
    mis-handled. It is a bare ADTS stream, not a transport stream, and it
    typically carries an ID3v2 tag as well, so the tag is skipped before the
    audio is examined: without that, even a working ADTS check fails, because the
    first byte is ``'I'`` from ``ID3`` rather than the ``0xFF`` sync word.
    """
    offset = skip_id3(data)
    if offset:
        # A leading ID3v2 tag is metadata and the container is whatever follows
        # it. The audio is classified *first* because a byte inside the tag's
        # payload can look like a sync word, and the raw bytes are only tried
        # afterwards so that a tag over a transport stream still reports "ts".
        found = _classify(data[offset:])
        if found is not None:
            return found
    return _classify(data)


def _classify(data: bytes) -> Optional[str]:
    """Container check on bytes whose framing is not in doubt."""
    if looks_like_ts(data):
        return "ts"
    if looks_like_mp4(data):
        return "mp4"
    if looks_like_adts(data):
        return "adts"
    if looks_like_mp3(data):
        return "mp3"
    return None


def id3_tag_size(data: bytes) -> int:
    """Total size of the ID3v2 tag at the start of *data*, or 0 if there is none.

    The payload length is stored "syncsafe": seven bits per byte with the top bit
    always clear, so a tag can never contain a byte pair that looks like an MPEG
    sync word.
    """
    if not data.startswith(ID3_MAGIC) or len(data) < ID3_HEADER_SIZE:
        return 0
    if data[3] == 0xFF:  # version 2.5: not a version that exists
        return 0
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    total = ID3_HEADER_SIZE + size
    if data[5] & 0x10:  # an ID3v2.4 footer follows the tag
        total += ID3_HEADER_SIZE
    return total


def skip_id3(data: bytes) -> int:
    """Offset of the audio in *data*, past any run of leading ID3v2 tags.

    Chained rather than single, because that is what the wild does: a segment on
    Microsoft's ``msvdn.net`` CDN carries ``PRIV`` (the Apple
    ``transportStreamTimestamp``) followed by ``TEXT`` (song metadata) - 655
    bytes of tags - before the first ADTS frame.
    """
    offset = 0
    for _ in range(MAX_ID3_TAGS):
        size = id3_tag_size(data[offset:])
        if not size:
            break
        offset += size
    return offset


def _decode_id3_text(payload: bytes) -> str:
    """Decode an ID3 text-frame payload (encoding byte + text)."""
    if not payload:
        return ""
    encoding, text = payload[0], payload[1:]
    if encoding == 3:
        return text.decode("utf-8", "replace")
    if encoding in (1, 2):
        return text.decode("utf-16" if encoding == 1 else "utf-16-be", "replace")
    return text.decode("latin-1", "replace")


def _song_from_text_frame(payload: bytes) -> Optional[str]:
    """``songInfo.present`` as "Artist - Title" from the CDN's JSON TEXT frame.

    msvdn.net segments carry a ``TEXT`` frame whose payload is JSON:
    ``{"songInfo":{"present":{"mus_art_name":..., "mus_sng_title":...}}}``.
    Between songs ``present`` is absent (only ``future`` remains) - returning
    ``None`` there lets the caller keep showing the last real title.
    """
    text = _decode_id3_text(payload).strip("\x00")
    if not text.startswith("{"):
        return None
    try:
        present = json.loads(text).get("songInfo", {}).get("present")
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(present, dict):
        return None
    title = present.get("mus_sng_title")
    if not isinstance(title, str) or not title.strip():
        return None
    artist = present.get("mus_art_name")
    if isinstance(artist, str) and artist.strip():
        return f"{artist.strip()} - {title.strip()}"
    return title.strip()


def id3_song_title(data: bytes) -> Optional[str]:
    """Now-playing "Artist - Title" from the leading ID3 tags of *data*.

    Two sources, in priority order: the JSON ``TEXT`` frame (proper casing and
    the authoritative *present* song), then the plain ``TIT1`` frame
    ("SOMBR - MY BODY ISN'T READY" on the observed CDN). ``None`` when the
    segment carries no usable title - which is the normal answer for stations
    without in-band metadata, so every caller can treat None as "keep the
    previous title" without special-casing the station.
    """
    offset = 0
    titl: Optional[str] = None
    for _ in range(MAX_ID3_TAGS):
        total = id3_tag_size(data[offset:])
        if not total:
            break
        tag = data[offset : offset + total]
        offset += total
        if len(tag) < ID3_HEADER_SIZE or tag[3] not in (3, 4):
            continue  # truncated, or ID3v2.2 whose frame layout differs
        footer = ID3_HEADER_SIZE if tag[5] & 0x10 else 0
        body = tag[ID3_HEADER_SIZE : len(tag) - footer]
        frame = 0
        while frame + 10 <= len(body):
            fid = body[frame : frame + 4]
            if fid == b"\x00\x00\x00\x00":
                break  # padding
            if tag[3] == 4:
                size = 0
                for byte in body[frame + 4 : frame + 8]:
                    size = (size << 7) | (byte & 0x7F)
            else:
                size = int.from_bytes(body[frame + 4 : frame + 8], "big")
            payload = body[frame + 10 : frame + 10 + size]
            frame += 10 + size
            if fid == b"TEXT":
                title = _song_from_text_frame(payload)
                if title:
                    return title
            elif fid == b"TIT1" and titl is None:
                text = _decode_id3_text(payload).strip("\x00").strip()
                if text:
                    titl = text
    return titl


def looks_like_mp3(data: bytes) -> bool:
    """True if ``data`` starts (near enough) with a valid MPEG audio frame."""
    if len(data) < 5:
        return False
    limit = min(len(data) - 4, 64)
    for offset in range(max(0, limit) + 1):
        if data[offset] == 0xFF and (data[offset + 1] & 0xE0) == 0xE0:
            version = (data[offset + 1] >> 3) & 0x03
            layer = (data[offset + 1] >> 1) & 0x03
            bitrate = (data[offset + 2] >> 4) & 0x0F
            rate = (data[offset + 2] >> 2) & 0x03
            if version != 0x01 and layer != 0x00 and bitrate not in (0x00, 0x0F) and rate != 0x03:
                return True
    return False


def looks_like_adts(data: bytes) -> bool:
    """True if ``data`` starts with an ADTS frame header (sync 0xFFF).

    The layer field must be zero, which is what separates ADTS from an MPEG audio
    frame. Without that check an MPEG-1 Layer III frame (``FF FB``) is
    indistinguishable from ADTS here - the sync, the bitrate and the sample-rate
    fields all line up - and a raw MP3 segment would be mis-reported as AAC.
    """
    if len(data) < 7:
        return False
    if data[0] != 0xFF or (data[1] & 0xF0) != 0xF0:
        return False
    if data[1] & 0x06:  # layer (bits 2-1) must be 00 in ADTS
        return False
    return ((data[2] >> 2) & 0x0F) != 0x0F and ((data[3] >> 6) & 0x03) != 0x03


#: MPEG audio bitrates, in kbit/s, indexed by ``[version][layer][index]``.
#: ``version`` 3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5; ``layer`` 1 = Layer III,
#: 2 = Layer II, 3 = Layer I. A zero entry means "not allowed".
_MP3_BITRATES: Dict[Tuple[int, int], Tuple[int, ...]] = {
    (3, 1): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
    (2, 1): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    (0, 1): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    (3, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0),
    (2, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    (0, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    (3, 3): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0),
    (2, 3): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0),
    (0, 3): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0),
}

#: MPEG audio sample rates, indexed by ``[version][index]``.
_MP3_RATES: Dict[int, Tuple[int, int, int, int]] = {
    3: (44100, 48000, 32000, 0),
    2: (22050, 24000, 16000, 0),
    0: (11025, 12000, 8000, 0),
}


def mp3_frame_size(data: bytes, offset: int = 0) -> Optional[int]:
    """Length in bytes of the MPEG audio frame at *offset*, or ``None``.

    Knows the size of a frame from its header, which is what makes it possible
    to tell where the *next* frame begins without decoding anything.

    This is what a radio's own decoder does, and it is why the start of a
    stream matters so much: a radio that begins mid-frame has to throw the
    partial frame away and resynchronise, and a radio whose *metadata parser*
    is a byte offset from its *audio decoder* (the Shoutcast/ICY grid) loses
    both at once when the two disagree.

    The frame length is ``144 * bitrate / sample_rate`` for Layer III and
    Layer II (with a padding byte for MPEG-1 at 32/48 kHz), and
    ``12 * bitrate / sample_rate`` samples of 4 bytes for Layer I.
    """
    if offset + 4 > len(data):
        return None
    if data[offset] != 0xFF or (data[offset + 1] & 0xE0) != 0xE0:
        return None
    version = (data[offset + 1] >> 3) & 0x03
    layer = (data[offset + 1] >> 1) & 0x03
    if version == 1 or layer == 0:
        return None
    table = _MP3_BITRATES.get((version, layer))
    if table is None:
        return None
    bitrate_index = (data[offset + 2] >> 4) & 0x0F
    rate_index = (data[offset + 2] >> 2) & 0x03
    if bitrate_index in (0, 15) or rate_index == 3:
        return None
    bitrate = table[bitrate_index] * 1000
    rate = _MP3_RATES[version][rate_index]
    if bitrate == 0 or rate == 0:
        return None
    padding = (data[offset + 2] >> 1) & 0x01
    if layer == 3:  # Layer I: 12 samples per frame, 4 bytes per sample
        return (12 * bitrate // rate + padding) * 4
    # Layer II and III: one frame is 144 (MPEG-1) or 72 (MPEG-2/2.5)
    # samples of the bitstream.
    samples = 144 if version == 3 else 72
    return samples * bitrate // rate + padding


def mp3_frame_aligned(data: bytes, offset: int = 0) -> bool:
    """True if *offset* is the start of an MPEG audio frame.

    Checks the header at *offset* **and** that the header at the next
    expected frame position is a header too - a single ``0xFF 0xFx`` is not
    enough, because those byte pairs occur by chance inside the payload of
    every other frame.
    """
    size = mp3_frame_size(data, offset)
    if size is None:
        return False
    return mp3_frame_size(data, offset + size) is not None


def adts_frame_size(data: bytes, offset: int = 0) -> Optional[int]:
    """Length in bytes of the ADTS (AAC) frame at *offset*, or ``None``.

    ADTS frames have a **variable** length - the header carries the frame
    length - so the size of a frame is only known by reading the header.

    The sync word is 12 bits (``0xFFF``), and the layer bits must be 00, which
    is what separates an ADTS frame from an MPEG audio frame: ``FF FB`` (an
    MP3 frame) has layer 11, and ``FF F1`` (ADTS) has layer 00. Getting this
    wrong makes every MP3 frame look like an ADTS frame - the ambiguity
    ``looks_like_adts`` already has to avoid.

    A frame is only a frame if the *next* one is a frame too (see
    ``mp3_frame_aligned``): a bare ``0xFFF`` occurs by chance inside the
    payload of other frames.
    """
    if offset + 7 > len(data):
        return None
    if data[offset] != 0xFF or (data[offset + 1] & 0xF6) != 0xF0:
        return None
    length = (
        ((data[offset + 3] & 0x03) << 11)
        | (data[offset + 4] << 3)
        | ((data[offset + 5] >> 5) & 0x07)
    )
    if length < 7:
        return None
    return length


def adts_frame_aligned(data: bytes, offset: int = 0) -> bool:
    """True if *offset* is the start of an ADTS frame.

    Checks the header at *offset* **and** that the header at the next frame
    position is a header too, for the same reason as
    :func:`mp3_frame_aligned`.
    """
    size = adts_frame_size(data, offset)
    if size is None:
        return False
    return adts_frame_size(data, offset + size) is not None


def adts_align_offset(data: bytes, window: int = 8192) -> Optional[int]:
    """Offset of the first ADTS frame in *data*, or ``None``.

    Scans the first *window* bytes for a position that starts a frame whose
    successor is also a frame header.
    """
    limit = min(len(data) - 7, window)
    for offset in range(max(0, limit) + 1):
        if adts_frame_aligned(data, offset):
            return offset
    return None


def adts_has_sync(data: bytes, window: int = 4096) -> bool:
    """True if *data* contains anything that looks like an ADTS frame sync.

    The cheap "is there any ADTS in here at all" test - the counterpart of
    :func:`mp3_has_sync`.
    """
    limit = min(len(data) - 1, window)
    return any(data[i] == 0xFF and (data[i + 1] & 0xF6) == 0xF0 for i in range(max(0, limit)))


def mp3_has_sync(data: bytes, window: int = 4096) -> bool:
    """True if *data* contains anything that looks like an MPEG frame sync.

    A cheap, decisive "is there any MP3 in here at all" test.

    ``mp3_align_offset`` has to find a frame *pair* to be certain, because a
    lone ``0xFF 0xFx`` occurs by chance inside every other frame's payload.
    That is the right rule for finding the grid, but the wrong rule for
    deciding whether a stream is MP3 at all: a buffer with no sync byte in it
    anywhere is definitely not MP3, and that can be answered immediately
    instead of after buffering more data.

    Only the first *window* bytes are examined, so this stays cheap on a
    large buffer.
    """
    limit = min(len(data) - 1, window)
    return any(data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0 for i in range(max(0, limit)))


def mp3_align_offset(data: bytes, window: int = 8192) -> Optional[int]:
    """Offset of the first MPEG audio frame in *data*, or ``None``.

    Scans the first *window* bytes for a position that starts a frame whose
    successor is also a frame header. Returns the offset, which is what a
    radio would have to discard before it can start playing cleanly.
    """
    limit = min(len(data) - 4, window)
    for offset in range(max(0, limit) + 1):
        if mp3_frame_aligned(data, offset):
            return offset
    return None


# ----------------------------------------------------------------------- CRC
def mpeg_crc32(data: bytes) -> int:
    """CRC-32/MPEG-2 as used by PSI sections (polynomial 0x04C11DB7)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return crc


def section_crc_ok(section: bytes) -> bool:
    """A well-formed PSI section's own CRC makes the running CRC zero."""
    if len(section) < 4:
        return False
    return mpeg_crc32(section) == 0


# -------------------------------------------------------------------- packets
def _find_sync(buf: bytes) -> int:
    """Index of the first plausible packet start in ``buf``, or -1."""
    if len(buf) < TS_PACKET_SIZE:
        return 0 if (buf and buf[0] == TS_SYNC_BYTE) else -1
    if buf[0] == TS_SYNC_BYTE and (
        len(buf) < 2 * TS_PACKET_SIZE or buf[TS_PACKET_SIZE] == TS_SYNC_BYTE
    ):
        return 0
    limit = min(len(buf) - TS_PACKET_SIZE, TS_PACKET_SIZE * 4)
    for offset in range(1, max(1, limit)):
        if buf[offset] != TS_SYNC_BYTE:
            continue
        if len(buf) >= offset + 2 * TS_PACKET_SIZE and buf[offset + TS_PACKET_SIZE] != TS_SYNC_BYTE:
            continue
        return offset
    return -1


def _packet_payload(packet: bytes) -> Optional[bytes]:
    """Extract the payload of one TS packet, or ``None`` if it has none."""
    if packet[1] & 0x80:  # transport_error_indicator
        return None
    if packet[3] & 0xC0:  # transport_scrambling_control
        return None
    adaptation_control = (packet[3] >> 4) & 0x03
    offset = 4
    if adaptation_control & 0x02:
        offset = 5 + packet[4]
        if offset > TS_PACKET_SIZE:
            return None
    if not adaptation_control & 0x01:
        return None
    payload = packet[offset:]
    return payload or None


def packet_pid(packet: bytes) -> int:
    return ((packet[1] & 0x1F) << 8) | packet[2]


def packet_has_payload_start(packet: bytes) -> bool:
    return bool(packet[1] & 0x40)


# ------------------------------------------------------------------ PSI tables
class _SectionAssembler:
    """Reassembles PSI sections which may split across TS packets."""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, payload: bytes) -> List[bytes]:
        sections: List[bytes] = []
        if self._buf:
            buf = self._buf + payload
        else:
            if not payload:
                return sections
            pointer = payload[0]
            start = 1 + pointer
            if start >= len(payload):
                # The section did not actually begin here; nothing to keep.
                return sections
            buf = bytearray(payload[start:])

        position = 0
        while len(buf) - position >= 3:
            needed = 3 + (((buf[position + 1] & 0x0F) << 8) | buf[position + 2])
            if needed < 3 or needed > 4096 or len(buf) - position < needed:
                break
            sections.append(bytes(buf[position : position + needed]))
            position += needed

        self._buf = bytearray(buf[position:])
        return sections


def parse_pat(section: bytes) -> Dict[int, int]:
    """``{program_number: pmt_pid}``."""
    if len(section) < 12 or section[0] != 0x00:
        return {}
    if section[1] & 0x80 and not section_crc_ok(section):
        return {}
    programs: Dict[int, int] = {}
    end = len(section) - 4
    offset = 8
    while offset + 4 <= end:
        program = (section[offset] << 8) | section[offset + 1]
        pid = ((section[offset + 2] & 0x1F) << 8) | section[offset + 3]
        if program != 0:
            programs[program] = pid
        offset += 4
    return programs


def parse_pmt(section: bytes) -> List[Tuple[int, int]]:
    """``[(stream_type, elementary_pid), ...]``."""
    if len(section) < 16 or section[0] != 0x02:
        return []
    if section[1] & 0x80 and not section_crc_ok(section):
        return []
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    offset = 12 + program_info_length
    end = len(section) - 4
    streams: List[Tuple[int, int]] = []
    while offset + 5 <= end:
        stream_type = section[offset]
        pid = ((section[offset + 1] & 0x1F) << 8) | section[offset + 2]
        es_info_length = ((section[offset + 3] & 0x0F) << 8) | section[offset + 4]
        streams.append((stream_type, pid))
        offset += 5 + es_info_length
    return streams


def scan_tables(data: bytes, max_packets: int = 4000) -> Dict[str, Any]:
    """Scan a buffer of TS data for PAT/PMT information.

    Returns ``{"programs": {num: pmt_pid}, "streams": [(type, pid)], "audio": type}``
    with ``audio`` set to the first *pass-through capable* stream type found.
    """
    pat = _SectionAssembler()
    pmts: Dict[int, _SectionAssembler] = {}
    pmt_pids: Dict[int, int] = {}
    programs: Dict[int, int] = {}
    streams: List[Tuple[int, int]] = []
    audio: Optional[int] = None

    for index in range(0, min(len(data), max_packets * TS_PACKET_SIZE), TS_PACKET_SIZE):
        packet = data[index : index + TS_PACKET_SIZE]
        if len(packet) < TS_PACKET_SIZE or packet[0] != TS_SYNC_BYTE:
            break
        pid = packet_pid(packet)
        payload = _packet_payload(packet)
        if not payload:
            continue
        if pid == 0x00:
            for section in pat.feed(payload):
                found = parse_pat(section)
                for program, pmt_pid in found.items():
                    programs[program] = pmt_pid
                    pmt_pids.setdefault(pmt_pid, pmt_pid)
                    pmts.setdefault(pmt_pid, _SectionAssembler())
        elif pid in pmt_pids:
            for section in pmts[pid].feed(payload):
                found = parse_pmt(section)
                if not found:
                    continue
                streams = found
                for stream_type, _es_pid in found:
                    if stream_type in PASSTHROUGH_TYPES and stream_type not in EXCLUDED_TYPES:
                        audio = stream_type
                        break
        if audio is not None and programs:
            break

    return {"programs": programs, "streams": streams, "audio": audio}


# -------------------------------------------------------------------- demuxer
class TSDemuxer:
    """Extract one audio elementary stream from a continuous MPEG-TS.

    ``feed()`` may be called with arbitrary-sized chunks (segment boundaries,
    partial segments, several segments at once); it yields the elementary
    stream bytes that become available.

    Discovery of the audio PID happens automatically from the PAT/PMT. If the
    service has multiple programs or several audio streams, the first stream
    whose type is directly playable wins; otherwise the first audio stream of
    the first program is used and the caller is expected to hand the data to
    ffmpeg instead.
    """

    def __init__(self, audio_pid: Optional[int] = None, prefer_passthrough: bool = True) -> None:
        self._pat = _SectionAssembler()
        self._pmt_sections: Dict[int, _SectionAssembler] = {}
        self._pmt_pids: Dict[int, None] = {}
        self._programs: Dict[int, int] = {}
        self._audio_pid = audio_pid
        self._audio_type: Optional[int] = None
        self._prefer_passthrough = prefer_passthrough

        self._pes = bytearray()
        self._header_done = False
        self._skipping = False
        self._partial = b""
        self._synced = False
        self._last_cc: Dict[int, int] = {}

        # Audio that arrives before the PMT tells us which PID it is would be
        # lost. Real-world segments sometimes start mid-GOP, so keep a bounded
        # rolling window of candidate packets and replay the right ones the
        # moment the tables turn up.
        self._pending: Deque[Tuple[int, bytes, bool]] = deque(maxlen=1024)
        self._replay_pending = False

        #: Diagnostics
        self.discontinuities = 0
        self.replayed_packets = 0

    # ------------------------------------------------------------------ state
    @property
    def audio_pid(self) -> Optional[int]:
        return self._audio_pid

    @property
    def output_kind(self) -> Optional[str]:
        if self._audio_type is None:
            return None
        return PASSTHROUGH_TYPES.get(self._audio_type)

    @property
    def ready(self) -> bool:
        return self._audio_pid is not None and self._audio_type is not None

    # ------------------------------------------------------------------- feed
    def feed(self, data: bytes) -> bytes:
        if not data:
            return b""
        out = bytearray()
        buf = self._partial + data if self._partial else data
        self._partial = b""
        position = 0

        if not self._synced:
            index = _find_sync(bytes(buf))
            if index < 0:
                # Keep a short tail in case a packet straddles the boundary.
                self._partial = bytes(buf[-TS_PACKET_SIZE:])
                return b""
            position = index
            self._synced = True

        end = len(buf)
        while end - position >= TS_PACKET_SIZE:
            packet = buf[position : position + TS_PACKET_SIZE]
            position += TS_PACKET_SIZE
            if packet[0] != TS_SYNC_BYTE:
                self._synced = False
                self.discontinuities += 1
                index = _find_sync(bytes(buf[position:]))
                if index < 0:
                    position = end
                    break
                position += index
                self._synced = True
                continue
            self._handle_packet(packet, out)
            if self._replay_pending:
                self._replay_pending = False
                self._replay(out)

        if position < len(buf):
            self._partial = bytes(buf[position:])
            if len(self._partial) > 4 * TS_PACKET_SIZE:  # resync failure guard
                self._partial = b""
        return bytes(out)

    def flush(self) -> bytes:
        """Emit anything still buffered (call at shutdown only)."""
        out = b""
        if self._header_done and self._pes:
            out = bytes(self._pes)
        self._pes.clear()
        return out

    # ---------------------------------------------------------------- internals
    def _replay(self, out: bytearray) -> None:
        """Emit the payload of buffered packets that belong to the audio PID."""
        pending, self._pending = self._pending, deque(maxlen=1024)
        for pid, payload, has_start in pending:
            if pid != self._audio_pid:
                continue
            self.replayed_packets += 1
            if has_start:
                if self._header_done and self._pes:
                    out += self._pes
                self._pes.clear()
                self._header_done = False
                self._skipping = False
            if not self._skipping:
                if self._header_done:
                    out += payload
                else:
                    self._handle_pes(payload, False, out)

    def _handle_packet(self, packet: bytes, out: bytearray) -> None:
        pid = packet_pid(packet)
        if pid == NULL_PID:
            return
        payload = _packet_payload(packet)
        if not payload:
            return
        has_start = packet_has_payload_start(packet)

        if pid == 0x00:
            for section in self._pat.feed(payload):
                for program, pmt_pid in parse_pat(section).items():
                    self._pmt_pids.setdefault(pmt_pid, None)
                    self._pmt_sections.setdefault(pmt_pid, _SectionAssembler())
                    self._programs[program] = pmt_pid
            return

        if pid in self._pmt_pids:
            assembler = self._pmt_sections[pid]
            for section in assembler.feed(payload):
                self._consider_streams(parse_pmt(section))
            return

        if self._audio_pid is None:
            # We do not know yet which PID carries audio: remember it.
            self._pending.append((pid, payload, has_start))
            return

        if pid == self._audio_pid:
            self._check_continuity(packet, pid)
            self._handle_pes(payload, has_start, out)

    def _check_continuity(self, packet: bytes, pid: int) -> None:
        counter = packet[3] & 0x0F
        previous = self._last_cc.get(pid)
        # ``not (x & 0x02)`` reads as "no adaptation-field discontinuity flag".
        if (
            previous is not None
            and (previous + 1) & 0x0F != counter
            and not (packet[3] >> 4) & 0x02
        ):
            self.discontinuities += 1
            # Drop the incomplete PES: keeping it would splice garbage.
            self._pes.clear()
            self._header_done = False
        self._last_cc[pid] = counter

    def _consider_streams(self, streams: List[Tuple[int, int]]) -> None:
        if not streams:
            return
        if self._audio_pid is not None:
            return  # already latched

        playable: Optional[Tuple[int, int]] = None
        fallback: Optional[Tuple[int, int]] = None
        for stream_type, pid in streams:
            if stream_type in EXCLUDED_TYPES:
                continue
            if stream_type in PASSTHROUGH_TYPES:
                playable = (stream_type, pid)
                break
            if fallback is None and 0x01 <= stream_type <= 0x24:
                fallback = (stream_type, pid)

        chosen = playable if (playable and self._prefer_passthrough) else (fallback or playable)
        if chosen is None:
            return
        self._audio_type, self._audio_pid = chosen
        self._replay_pending = bool(self._pending)

    def _handle_pes(self, payload: bytes, has_start: bool, out: bytearray) -> None:
        if has_start:
            if self._header_done and self._pes:
                out += self._pes
            self._pes.clear()
            self._header_done = False
            self._skipping = False

        if not payload or self._skipping:
            return

        if self._header_done:
            out += payload
            return

        self._pes += payload
        # Walk to the end of the PES header. The loop is bounded so a malformed
        # stream can never make us spin forever. Running off the end without a
        # `break` means "need more data" (or junk we gave up on).
        for _ in range(4):
            if len(self._pes) < 9:
                return
            if self._pes[0:3] != b"\x00\x00\x01":
                del self._pes[0]
                continue
            stream_id = self._pes[3]
            if stream_id < 0xC0:
                # Padding stream (0xBE), private stream 2 (0xBF) and other
                # non-audio PES: skip the whole payload unit.
                self._pes.clear()
                self._skipping = True
                return
            total = 9 + self._pes[8]
            if len(self._pes) < total:
                return
            del self._pes[:total]
            self._header_done = True
            break
        else:
            return

        if self._header_done and self._pes:
            out += self._pes
            self._pes.clear()


# ------------------------------------------------------------- test utilities
def _ts_packet(pid: int, payload: bytes, *, start: bool = False, cc: int = 0) -> bytes:
    """Build a single TS packet (used by the test-suite and ``build_test_ts``).

    Short payloads are padded the way real muxers do it - with an adaptation
    field full of stuffing bytes - so that demuxing the result yields *exactly*
    the payload and nothing else.
    """
    body = bytes(payload)[:184]
    first = (0x40 if start else 0x00) | ((pid >> 8) & 0x1F)
    if len(body) == 184:
        return bytes([TS_SYNC_BYTE, first, pid & 0xFF, 0x10 | (cc & 0x0F)]) + body

    adaptation_length = 184 - len(body) - 1
    header = bytes([TS_SYNC_BYTE, first, pid & 0xFF, 0x30 | (cc & 0x0F)])
    if adaptation_length == 0:
        adaptation = b"\x00"
    else:
        adaptation = bytes([adaptation_length, 0x00]) + b"\xff" * (adaptation_length - 1)
    return header + adaptation + body


def _psi_packet(pid: int, section: bytes, *, cc: int = 0) -> bytes:
    """A TS packet carrying a single PSI section (CRC appended by the caller)."""
    payload = b"\x00" + section  # pointer_field = 0
    return _ts_packet(pid, payload, start=True, cc=cc)


def build_test_ts(
    payload: bytes,
    *,
    stream_type: int = STREAM_TYPE_AAC_ADTS,
    elementary_pid: int = 0x0101,
    pmt_pid: int = 0x0100,
    program: int = 1,
    with_pts: bool = True,
) -> bytes:
    """Build a minimal but spec-valid single-program TS.

    Only used for tests and for the ``radioconv check`` self-test, so that we
    can exercise the demuxer without invoking ffmpeg at all.
    """
    # --- PAT
    pat_body = bytearray()
    pat_body += program.to_bytes(2, "big")
    pat_body += (0xE0 | (pmt_pid >> 8)).to_bytes(1, "big") + bytes([pmt_pid & 0xFF])
    pat = bytearray([0x00, 0xB0 | ((len(pat_body) + 5 + 4) >> 8), (len(pat_body) + 5 + 4) & 0xFF])
    pat += b"\x00\x01"  # transport_stream_id
    pat += b"\xc1"  # version 0, current
    pat += b"\x00\x00"  # section 0 of 0
    pat += pat_body
    pat += mpeg_crc32(bytes(pat)).to_bytes(4, "big")

    # --- PMT
    es_body = bytearray()
    es_body += bytes([stream_type])
    es_body += (0xE0 | (elementary_pid >> 8)).to_bytes(1, "big") + bytes([elementary_pid & 0xFF])
    es_body += b"\xf0\x00"  # es_info_length = 0
    pmt_len = 9 + len(es_body) + 4
    pmt = bytearray([0x02, 0xB0 | (pmt_len >> 8), pmt_len & 0xFF])
    pmt += program.to_bytes(2, "big")
    pmt += b"\xc1"
    pmt += b"\x00\x00"
    pmt += (0xE0 | (elementary_pid >> 8)).to_bytes(1, "big") + bytes([elementary_pid & 0xFF])  # PCR
    pmt += b"\xf0\x00"  # program_info_length = 0
    pmt += es_body
    pmt += mpeg_crc32(bytes(pmt)).to_bytes(4, "big")

    # --- PES packets carrying `payload`
    packets = [_psi_packet(0x00, bytes(pat))]
    packets.append(_psi_packet(pmt_pid, bytes(pmt)))

    optional = b"\x21\x00\x01\x00\x01" if with_pts else b""
    flags = 0x80 if with_pts else 0x00
    pes_header = (
        b"\x00\x00\x01\xc0"
        + (0).to_bytes(2, "big")
        + b"\x80"
        + bytes([flags])
        + bytes([len(optional)])
        + optional
    )
    stream = pes_header + payload

    cc = 0
    first = True
    while stream:
        body, stream = stream[:184], stream[184:]
        packets.append(_ts_packet(elementary_pid, body, start=first, cc=cc))
        cc = (cc + 1) & 0x0F
        first = False
    # A second copy of the tables afterwards, so the file also exercises
    # mid-stream table repeats.
    packets.append(_psi_packet(0x00, bytes(pat), cc=1))
    packets.append(_psi_packet(pmt_pid, bytes(pmt), cc=1))
    return b"".join(packets)
