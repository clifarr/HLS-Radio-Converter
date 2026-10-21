"""Tests for the pure-python MPEG-TS demuxer - the FFmpeg-free path."""

from __future__ import annotations

import pytest

from radioconv import tsdemux as T

PAYLOAD = bytes(range(1, 200)) * 5  # 995 bytes: spans six TS packets


def test_detectors():
    ts = T.build_test_ts(PAYLOAD)
    assert T.looks_like_ts(ts)
    assert T.classify_segment(ts) == "ts"
    # 0x47 is ASCII 'G': a file full of G's is not a transport stream.
    assert not T.looks_like_ts(b"G" * 1000)
    assert not T.looks_like_ts(b"")
    assert T.looks_like_mp4(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20)
    assert T.classify_segment(b"nonsense") is None


# ------------------------------------------------------- ID3-tagged raw AAC
#: A valid ADTS header: sync 0xFFF, MPEG-4, AAC-LC, 48 kHz, stereo.
ADTS_HEADER = bytes([0xFF, 0xF1, 0x4C, 0x80, 0x46, 0x9F, 0xFC])


def id3_tag(payload: bytes) -> bytes:
    """Build an ID3v2.4 tag with the syncsafe length encoding real tags use."""
    size = len(payload)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x04\x00\x00" + syncsafe + payload


def test_raw_adts_is_recognised():
    """A bare ADTS stream is a container, and used to be `None`.

    HLS ``.aac`` segments are exactly this - no transport stream anywhere - and
    ``INPUT_FORMATS`` had no entry for it, so it reached ffmpeg with no usable
    ``-f`` and produced silence.
    """
    assert T.looks_like_adts(ADTS_HEADER + b"\x00" * 32)
    assert T.classify_segment(ADTS_HEADER + b"\x00" * 32) == "adts"


def test_id3_tagged_adts_is_recognised():
    """The real-world case, and the one that was shipping broken.

    Every segment from Microsoft's ``msvdn.net`` CDN starts with an ID3v2.4 tag,
    so the very first byte is ``'I'`` from ``ID3`` rather than ``0xFF``. Checking
    for the ADTS sync word without skipping the tag cannot see the audio.
    """
    segment = id3_tag(b"PRIV" + b"\x00" * 40) + ADTS_HEADER + b"\x00" * 64

    assert not T.looks_like_adts(segment), "the tag really does hide the sync word"
    assert T.classify_segment(segment) == "adts"


def test_chained_id3_tags_are_all_skipped():
    """Real segments carry two tags in a row, not one.

    Observed on a live stream: a ``PRIV`` tag holding the Apple
    ``transportStreamTimestamp``, then a ``TEXT`` tag holding song metadata -
    655 bytes - before the first ADTS frame.
    """
    segment = (
        id3_tag(b"PRIV" + b"\x00" * 53)
        + id3_tag(b"TEXT" + b"\x00" * 500)
        + ADTS_HEADER
        + b"\x00" * 64
    )

    assert T.skip_id3(segment) == 10 + 57 + 10 + 504
    assert T.classify_segment(segment) == "adts"


def test_skip_id3_offsets():
    assert T.skip_id3(ADTS_HEADER) == 0, "no tag, no offset"
    assert T.skip_id3(b"") == 0
    assert T.skip_id3(b"ID3") == 0, "truncated header is not a tag"
    tag = id3_tag(b"X" * 100)
    assert T.skip_id3(tag + b"audio") == len(tag)


def id3_frame(fid: bytes, payload: bytes, *, v24: bool = True) -> bytes:
    """One ID3 frame: id, size (syncsafe for v2.4, plain for v2.3), flags."""
    if v24:
        size = bytes(
            [
                (len(payload) >> 21) & 0x7F,
                (len(payload) >> 14) & 0x7F,
                (len(payload) >> 7) & 0x7F,
                len(payload) & 0x7F,
            ]
        )
    else:
        size = len(payload).to_bytes(4, "big")
    return fid + size + b"\x00\x00" + payload


SONG_JSON = (
    '{"songInfo":{"present":{"mus_art_name":"Sombr","mus_sng_title":"My Body Isn\'t Ready"}}}'
)


def test_song_title_prefers_the_json_text_frame():
    """TIT1 comes first on the wire; the JSON's *present* song still wins."""
    tag = id3_tag(
        id3_frame(b"TIT1", b"\x03SOMBR - MY BODY ISN'T READY\x00")
        + id3_frame(b"TEXT", b"\x03" + SONG_JSON.encode())
    )
    assert T.id3_song_title(tag + ADTS_HEADER) == "Sombr - My Body Isn't Ready"


def test_song_title_falls_back_to_tit1():
    tag = id3_tag(id3_frame(b"TIT1", b"\x03SOMBR - MY BODY ISN'T READY\x00"))
    assert T.id3_song_title(tag) == "SOMBR - MY BODY ISN'T READY"


def test_song_title_is_none_between_songs_and_without_tags():
    # `present` absent (only `future` remains) and a blank TIT1: nothing to
    # show - the caller keeps the previous title rather than blanking it.
    between = id3_tag(
        id3_frame(b"TEXT", b'\x03{"songInfo":{"future":{"mus_sng_title":"X"}}}')
        + id3_frame(b"TIT1", b"\x03   \x00")
    )
    assert T.id3_song_title(between) is None
    assert T.id3_song_title(ADTS_HEADER + b"\x00" * 32) is None
    assert T.id3_song_title(b"") is None
    # The timing-only PRIV tag every msvdn segment carries is not a title.
    priv = id3_tag(
        id3_frame(b"PRIV", b"com.apple.streaming.transportStreamTimestamp\x00\x00\x00\x00\x01")
    )
    assert T.id3_song_title(priv) is None


def test_song_title_reads_id3v2_3_frame_sizes_too():
    """v2.3 encodes frame sizes plainly (v2.4 made them syncsafe)."""
    frame = id3_frame(b"TEXT", b"\x03" + SONG_JSON.encode(), v24=False)
    header = b"ID3\x03\x00\x00" + bytes(
        [
            (len(frame) >> 21) & 0x7F,
            (len(frame) >> 14) & 0x7F,
            (len(frame) >> 7) & 0x7F,
            len(frame) & 0x7F,
        ]
    )
    assert T.id3_song_title(header + frame) == "Sombr - My Body Isn't Ready"


def test_an_absurd_id3_length_cannot_run_away():
    """A corrupt length must not walk off the end of the buffer."""
    # Length field claims ~268 MB, far more than the data we hold.
    bogus = b"ID3\x04\x00\x00\x7f\x7f\x7f\x7f" + b"\x00" * 100
    offset = T.skip_id3(bogus)
    assert offset >= len(bogus), "the offset is allowed to exceed the data"
    assert T.classify_segment(bogus) is None


def test_id3_does_not_mask_a_transport_stream():
    """Skipping the tag must not upset the containers already handled."""
    tagged_ts = id3_tag(b"TIT2" + b"\x00" * 20) + T.build_test_ts(PAYLOAD)
    assert T.classify_segment(tagged_ts) == "ts"

    tagged_mp4 = id3_tag(b"X" * 8) + b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20
    assert T.classify_segment(tagged_mp4) == "mp4"


def test_raw_mp3_is_recognised():
    """A bare MPEG audio frame is the last of the four real containers."""
    # 0xFF 0xFB: MPEG-1 Layer III, no CRC, 128 kbit/s @ 44.1 kHz.
    frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 200
    assert T.classify_segment(frame) == "mp3"


def test_short_inputs_do_not_crash_the_detectors():
    """Every detector is fed the first bytes of whatever came off the network."""
    for size in range(0, 12):
        chunk = (ADTS_HEADER + b"\x00" * 32)[:size]
        T.classify_segment(chunk)
        T.looks_like_mp3(chunk)
        T.looks_like_adts(chunk)
        T.looks_like_mp4(chunk)
        T.skip_id3(chunk)
    # `looks_like_mp3` used to index data[0] on an empty buffer.
    assert not T.looks_like_mp3(b"")
    assert not T.looks_like_mp3(b"\xff")


def _section_of(packet: bytes) -> bytes:
    """Pull the PSI section out of a TS packet, wherever it sits."""
    payload = T._packet_payload(packet)
    assert payload is not None
    pointer = payload[0]
    return payload[1 + pointer :]


def _corrupt_section(packet: bytes, index: int = 5) -> bytes:
    """Flip a byte *inside the section*, not in the adaptation stuffing."""
    payload = T._packet_payload(packet)
    assert payload is not None
    start = len(packet) - len(payload)
    buf = bytearray(packet)
    buf[start + 1 + index] ^= 0xFF
    return bytes(buf)


# ------------------------------------------------------- the PSI assembler
def _pat_section() -> bytes:
    """A minimal, well-formed PAT section."""
    body = bytes([0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x00, 0x00]) + bytes([0x00, 0x01, 0xE1, 0x00])
    crc = T.mpeg_crc32(body)
    return body + crc.to_bytes(4, "big")


def test_the_psi_assembler_discards_stuffing_instead_of_keeping_it():
    """0xFF stuffing must not be glued onto the next section.

    A PAT packet is padded with 0xFF to the end of the packet. Keeping
    those bytes in the buffer means the next packet's continuation arrives
    glued to a run of 0xFF, the section never completes, and the buffer
    grows by a whole packet on every packet.
    """
    section = _pat_section()
    assembler = T._SectionAssembler()
    # One packet: pointer field, then the section, then stuffing.
    payload = b"\x00" + section + b"\xff" * (184 - 1 - len(section))
    sections = assembler.feed(payload, payload_unit_start=True)
    assert len(sections) == 1
    assert sections[0] == section
    # Nothing is left over: the stuffing is gone, not buffered.
    assert len(assembler._buf) == 0


def test_the_psi_assembler_buffer_does_not_grow_without_bound():
    """The measured defect: +188 bytes per packet, forever.

    Every packet was a pointer field, a section that had already been
    seen, and a run of stuffing. The old assembler kept the stuffing, so
    the buffer grew by one packet's worth on every packet and the section
    was never found again.
    """
    section = _pat_section()
    assembler = T._SectionAssembler()
    payload = b"\x00" + section + b"\xff" * (184 - 1 - len(section))
    sizes = []
    for _ in range(20):
        assembler.feed(payload, payload_unit_start=True)
        sizes.append(len(assembler._buf))
    assert max(sizes) == 0, f"the buffer grew: {sizes}"
    assert max(sizes) <= T.MAX_PENDING_BYTES


def test_a_psi_section_split_across_packets_is_reassembled():
    """A section that spans two packets must still be parsed.

    The continuation packet has **no** pointer field: its first byte is
    the continuation of the section, not a skip count. Reading it as a
    pointer is what made a split section impossible to assemble.
    """
    section = _pat_section()
    split = len(section) // 2
    assembler = T._SectionAssembler()

    first = b"\x00" + section[:split]
    assert assembler.feed(first, payload_unit_start=True) == []
    assert len(assembler._buf) == split

    second = section[split:]
    sections = assembler.feed(second, payload_unit_start=False)
    assert sections == [section]
    assert T.parse_pat(sections[0]) == {1: 0x0100}


def test_a_psi_continuation_with_nothing_to_continue_is_dropped():
    """A continuation packet with no buffer behind it is a lost section."""
    assembler = T._SectionAssembler()
    sections = assembler.feed(b"\x00\x01\x02", payload_unit_start=False)
    assert sections == []
    assert len(assembler._buf) == 0


def test_the_psi_assembler_survives_a_stream_of_junk():
    """Random bytes must not make the assembler grow or hang."""
    assembler = T._SectionAssembler()
    for offset in range(0, 2000, 188):
        payload = bytes((offset + i) & 0xFF for i in range(188))
        assembler.feed(payload, payload_unit_start=(offset % 376 == 0))
        assert len(assembler._buf) <= T.MAX_PENDING_BYTES


def test_the_discontinuity_indicator_is_read_from_the_adaptation_field():
    """The flag is in the adaptation field, not in the packet header.

    The old code read ``(packet[3] >> 4) & 0x02`` - the
    ``adaptation_field_control``, which says *whether* an adaptation field
    is present, never whether there is a discontinuity.
    """
    # A packet with an adaptation field whose flags byte has bit 7 set.
    discontinuity = bytearray(T.TS_PACKET_SIZE)
    discontinuity[0] = 0x47
    discontinuity[1] = 0x01
    discontinuity[3] = 0x30  # adaptation_field_control = 0b11 (payload + adaptation)
    discontinuity[4] = 1  # adaptation_field_length = 1 (the flags byte)
    discontinuity[5] = 0x80  # discontinuity_indicator
    assert T.packet_discontinuity(bytes(discontinuity)) is True

    # The same packet without the flag.
    no_flag = bytearray(discontinuity)
    no_flag[5] = 0x00
    assert T.packet_discontinuity(bytes(no_flag)) is False

    # A packet with no adaptation field at all: no flag, so no discontinuity.
    plain = bytearray(T.TS_PACKET_SIZE)
    plain[0] = 0x47
    plain[1] = 0x01
    plain[3] = 0x10  # adaptation_field_control = 0b01 (payload only)
    assert T.packet_discontinuity(bytes(plain)) is False

    # A short packet must not raise.
    assert T.packet_discontinuity(b"") is False
    assert T.packet_discontinuity(b"\x47\x01") is False


def test_a_declared_discontinuity_is_a_reset_not_damage():
    """A discontinuity indicator means the encoder started a new sequence.

    The counter may jump on purpose. The demuxer must start fresh - and
    it must still *notice*, because the PES packet before the
    discontinuity was abandoned deliberately.
    """
    demux = T.TSDemuxer(audio_pid=0x0101)
    # Two packets with a jump in the counter, the second flagged.
    first = bytearray(T.TS_PACKET_SIZE)
    first[0] = 0x47
    first[1] = 0x01 | 0x40  # PUSI
    first[3] = 0x10  # adaptation_field_control = 0b01, counter 0
    second = bytearray(T.TS_PACKET_SIZE)
    second[0] = 0x47
    second[1] = 0x01
    second[3] = 0x30  # adaptation_field_control = 0b11 (payload + adaptation)
    second[4] = 1  # adaptation_field_length = 1 (the flags byte)
    second[5] = 0x80  # discontinuity_indicator
    second[6] = 0x05  # counter 5: a jump on purpose
    demux._check_continuity(bytes(first), 0x0101)
    assert demux.discontinuities == 0
    demux._check_continuity(bytes(second), 0x0101)
    assert demux.discontinuities == 1


def test_a_duplicate_packet_is_not_a_discontinuity():
    """A retransmitted packet has the same counter, and no data is lost."""
    demux = T.TSDemuxer(audio_pid=0x0101)
    packet = bytearray(T.TS_PACKET_SIZE)
    packet[0] = 0x47
    packet[1] = 0x01
    packet[3] = 0x10  # adaptation_field_control = 0b01, counter 0
    demux._check_continuity(bytes(packet), 0x0101)
    assert demux.discontinuities == 0
    # The same packet again: a duplicate, not a gap.
    demux._check_continuity(bytes(packet), 0x0101)
    assert demux.discontinuities == 0


def test_a_real_gap_is_still_a_discontinuity():
    """A counter that jumps without a declared discontinuity is damage."""
    demux = T.TSDemuxer(audio_pid=0x0101)
    first = bytearray(T.TS_PACKET_SIZE)
    first[0] = 0x47
    first[1] = 0x01
    first[3] = 0x10  # adaptation_field_control = 0b01, counter 0
    third = bytearray(T.TS_PACKET_SIZE)
    third[0] = 0x47
    third[1] = 0x01
    third[3] = 0x12  # adaptation_field_control = 0b01, counter 2: one lost
    demux._check_continuity(bytes(first), 0x0101)
    demux._check_continuity(bytes(third), 0x0101)
    assert demux.discontinuities == 1


def test_crc_and_psi_parsing():
    ts = T.build_test_ts(PAYLOAD, stream_type=T.STREAM_TYPE_AAC_ADTS)
    tables = T.scan_tables(ts)
    assert tables["programs"] == {1: 0x0100}
    assert tables["streams"] == [(T.STREAM_TYPE_AAC_ADTS, 0x0101)]
    assert tables["audio"] == T.STREAM_TYPE_AAC_ADTS

    section = _section_of(ts[: T.TS_PACKET_SIZE])
    assert T.section_crc_ok(section)
    assert T.parse_pat(section) == {1: 0x0100}

    # A section whose CRC no longer matches must be rejected, not believed -
    # this is what stops random audio bytes being mistaken for a table.
    bad_version = bytearray(section)
    bad_version[5] ^= 0xFF  # version/current_next: inside the CRC's coverage
    assert not T.section_crc_ok(bytes(bad_version))
    assert T.parse_pat(bytes(bad_version)) == {}

    bad_crc = bytearray(section)
    bad_crc[-1] ^= 0xFF
    assert not T.section_crc_ok(bytes(bad_crc))
    assert T.parse_pat(bytes(bad_crc)) == {}

    # build_test_ts repeats the tables at the end, so both copies must be
    # broken for the scan to come up empty - the realistic scenario for a
    # stream whose tables cannot be read.
    packets = [ts[i : i + T.TS_PACKET_SIZE] for i in range(0, len(ts), T.TS_PACKET_SIZE)]
    packets[0] = _corrupt_section(packets[0])
    packets[-2] = _corrupt_section(packets[-2])
    broken = b"".join(packets)
    assert T.scan_tables(broken)["programs"] == {}
    assert T.scan_tables(broken)["audio"] is None


@pytest.mark.parametrize(
    "stream_type,kind",
    [
        (T.STREAM_TYPE_MP3, "mp3"),
        (T.STREAM_TYPE_MP3_MPEG2, "mp3"),
        (T.STREAM_TYPE_AAC_ADTS, "adts"),
    ],
)
def test_round_trip_is_exact(stream_type, kind):
    ts = T.build_test_ts(PAYLOAD, stream_type=stream_type)
    demux = T.TSDemuxer()
    out = demux.feed(ts)
    assert demux.ready
    assert demux.audio_pid == 0x0101
    assert demux.output_kind == kind
    assert out == PAYLOAD
    assert demux.discontinuities == 0


@pytest.mark.parametrize("stream_type", [0x11, 0x1C, 0x81, 0x87])
def test_unplayable_codecs_are_not_passed_through(stream_type):
    """LATM AAC / AC-3 must go to ffmpeg, never be forwarded raw."""
    ts = T.build_test_ts(PAYLOAD, stream_type=stream_type)
    demux = T.TSDemuxer()
    out = demux.feed(ts)
    assert demux.output_kind is None
    assert out == b""


def test_chunked_feeding_is_byte_identical():
    ts = T.build_test_ts(PAYLOAD)
    demux = T.TSDemuxer()
    out = b"".join(demux.feed(ts[offset : offset + 97]) for offset in range(0, len(ts), 97))
    assert out == PAYLOAD


def test_audio_arriving_before_the_tables_is_replayed():
    """Segments that start mid-GOP must not lose their first packets."""
    ts = T.build_test_ts(PAYLOAD)
    body = ts[188 * 2 : -188 * 2]
    tables = ts[-188 * 2 :]
    demux = T.TSDemuxer()
    out = demux.feed(body) + demux.feed(tables)
    assert out == PAYLOAD
    assert demux.replayed_packets > 0


def test_tableless_fragment_yields_nothing_and_stays_unready():
    ts = T.build_test_ts(PAYLOAD)
    demux = T.TSDemuxer()
    assert demux.feed(ts[188 * 2 : -188 * 2]) == b""
    assert not demux.ready


def test_explicit_pid_skips_table_discovery():
    ts = T.build_test_ts(PAYLOAD)
    demux = T.TSDemuxer(audio_pid=0x0101)
    assert demux.feed(ts) == PAYLOAD


def test_continuity_break_drops_the_damaged_pes():
    ts = T.build_test_ts(PAYLOAD)
    packets = [ts[i : i + 188] for i in range(0, len(ts), 188)]
    demux = T.TSDemuxer()
    demux.feed(b"".join(packets[:2]))  # PAT + PMT only
    # First PES packet, then a packet with a broken continuity counter.
    out = demux.feed(packets[2])
    damaged = bytearray(packets[3])
    damaged[3] = (damaged[3] & 0xF0) | ((damaged[3] + 3) & 0x0F)
    demux.feed(bytes(damaged))
    assert demux.discontinuities >= 1
    # Whatever came out must be a prefix of the real payload, not garbage.
    assert PAYLOAD.startswith(out[: len(out)])


def test_resynchronises_after_junk():
    ts = T.build_test_ts(PAYLOAD, stream_type=T.STREAM_TYPE_MP3)
    demux = T.TSDemuxer()
    junk = b"\x00" * 300
    assert demux.feed(junk + ts) == PAYLOAD


def test_adts_and_mp3_sniffers():
    assert T.looks_like_adts(b"\xff\xf1\x50\x80\x00\x1f\xfc" + b"\x00" * 10)
    assert not T.looks_like_adts(b"\x00" * 10)
    assert T.looks_like_mp3(b"\xff\xfb\x90\x00" + b"\x00" * 20)
    assert not T.looks_like_mp3(b"\x00" * 20)


# ------------------------------------------------- MPEG audio frame lengths
def _mpeg_header(version: int, layer: int, bitrate_kbps: int, rate_hz: int) -> bytes:
    """Build a 4-byte MPEG audio frame header.

    *version* is 3 for MPEG-1, 2 for MPEG-2, 0 for MPEG-2.5; *layer* is
    1 for Layer III, 2 for Layer II, 3 for Layer I.
    """
    bitrate_index = T._MP3_BITRATES[(version, layer)].index(bitrate_kbps)
    rate_index = T._MP3_RATES[version].index(rate_hz)
    byte1 = 0xE0 | (version << 3) | (layer << 1) | 1
    byte2 = (bitrate_index << 4) | (rate_index << 2)
    return bytes([0xFF, byte1, byte2, 0x00])


@pytest.mark.parametrize(
    "version,layer,bitrate_kbps,rate_hz,expected",
    [
        # Layer III: 144 samples in MPEG-1, 72 in MPEG-2/2.5.
        (3, 1, 192, 48000, 576),
        (3, 1, 128, 44100, 417),
        (2, 1, 64, 24000, 192),
        (0, 1, 32, 12000, 192),
        # Layer II: 144 samples in EVERY version. This is the case that
        # used to be wrong - the 72-sample rule belongs to Layer III only.
        (3, 2, 192, 48000, 576),
        (3, 2, 64, 48000, 192),
        (2, 2, 64, 24000, 384),
        (2, 2, 64, 22050, 417),
        (0, 2, 48, 12000, 576),
        # Layer I: 12 samples of 4 bytes each.
        (3, 3, 32, 48000, 32),
    ],
)
def test_mpeg_frame_size_depends_on_the_layer_not_only_the_version(
    version, layer, bitrate_kbps, rate_hz, expected
):
    """The frame length must follow the layer as well as the version.

    MPEG-2 Layer II at 64 kbit/s and 24 kHz is ``144 * 64000 / 24000`` =
    384 bytes. Reading it as 72 samples gives 192, which is half the frame -
    and a frame grid built on half-size frames puts every boundary in the
    middle of a real frame.
    """
    header = _mpeg_header(version, layer, bitrate_kbps, rate_hz)
    assert T.mp3_frame_size(header) == expected


def test_mpeg_frame_size_uses_the_same_number_in_every_version_for_layer_two():
    """The explicit statement of the rule, as a property.

    Layer II is 144 samples per frame whatever the version, so the frame
    length is proportional to ``bitrate / rate`` with a factor of 144 in
    every case. Layer III is the one that halves in MPEG-2/2.5.
    """
    for version in (3, 2, 0):
        rate_hz = T._MP3_RATES[version][0]
        header = _mpeg_header(version, 2, 64, rate_hz)
        expected = 144 * 64000 // rate_hz
        assert T.mp3_frame_size(header) == expected, f"MPEG version {version}"


def test_mpeg_1_layer_two_is_not_halved_the_way_layer_three_is():
    """The two rules side by side, at the same rate.

    MPEG-2 at 24 kHz: Layer III is 72 samples (192 bytes at 64 kbit/s) and
    Layer II is 144 samples (384 bytes at 64 kbit/s). Reading Layer II with
    the Layer III factor halves every frame, and a grid built on half-size
    frames is wrong at every boundary.
    """
    layer_three = T.mp3_frame_size(_mpeg_header(2, 1, 64, 24000))
    layer_two = T.mp3_frame_size(_mpeg_header(2, 2, 64, 24000))
    assert layer_three == 192
    assert layer_two == 384
    assert layer_two == layer_three * 2
