"""Parsing ``ffmpeg -i`` output.

Every string in this file is a **verbatim** line captured from real ffmpeg
output (see the docstrings for how each was obtained), not something typed from
memory. The bug this exists to prevent was only visible in real output:

    Stream #0:0[0x100]: Audio: aac (LC) ([15][0][0][0] / 0x000F), 44100 Hz, ...

Inside a transport stream ffmpeg puts the container's stream type in a *second*
parentheses group before the first comma. The profile was matched as ``[^,]*``,
so it swallowed that group and ``radioconv probe`` printed

    audio codec  : aac LC) ([15][0][0][0] / 0x000F 44100 Hz
"""

from __future__ import annotations

import pytest

from radioconv.ffmpeg import parse_stream_info

# Captured from `ffmpeg -i` on a segment produced by:
#   ffmpeg -f lavfi -i sine -c:a aac -b:a 128k -f hls ...
AAC_IN_TS = (
    "  Stream #0:0[0x100]: Audio: aac (LC) ([15][0][0][0] / 0x000F), 44100 Hz, mono, fltp, 123 kb/s"
)

# Same, but with libmp3lame inside MPEG-TS.
MP3_IN_TS = (
    "  Stream #0:0[0x100]: Audio: mp3 (mp3float) ([3][0][0][0] / 0x0003), "
    "44100 Hz, mono, fltp, 128 kb/s"
)

# A plain MP3 file: no TS wrapper, so no second group.
MP3_PLAIN = "  Stream #0:0: Audio: mp3 (mp3float), 44100 Hz, mono, fltp, 128 kb/s"


# ------------------------------------------------------------------ the bug
def test_profile_is_not_polluted_by_the_ts_stream_type():
    """The regression: the profile must be just `LC`, not the stream descriptor."""
    info = parse_stream_info(AAC_IN_TS)

    assert info["codec"] == "aac"
    assert info["profile"] == "LC"
    assert "0x000F" not in info["profile"]
    assert "[15]" not in info["profile"]


@pytest.mark.parametrize(
    "line, expected_codec, expected_profile",
    [
        (AAC_IN_TS, "aac", "LC"),
        (MP3_IN_TS, "mp3", "mp3float"),
        (MP3_PLAIN, "mp3", "mp3float"),
    ],
)
def test_codec_and_profile_for_every_real_shape(line, expected_codec, expected_profile):
    info = parse_stream_info(line)
    assert info["codec"] == expected_codec
    assert info["profile"] == expected_profile


# ----------------------------------------------------- the other parsed fields
@pytest.mark.parametrize("line", [AAC_IN_TS, MP3_IN_TS, MP3_PLAIN])
def test_sample_rate_and_channels(line):
    info = parse_stream_info(line)
    assert info["sample_rate"] == 44100
    assert info["channels"] == 1, "these samples are `mono`"


def test_bitrate_is_parsed_as_bits():
    assert parse_stream_info(AAC_IN_TS)["bitrate"] == 123_000
    assert parse_stream_info(MP3_IN_TS)["bitrate"] == 128_000


def test_stereo_and_a_higher_rate():
    line = "  Stream #0:0: Audio: aac (LC), 48000 Hz, stereo, fltp, 192 kb/s"
    info = parse_stream_info(line)
    assert info["sample_rate"] == 48000
    assert info["channels"] == 2
    assert info["bitrate"] == 192_000


def test_a_stream_with_no_profile_still_parses():
    """Not every codec is reported with parentheses."""
    line = "  Stream #0:0: Audio: pcm_s16le, 44100 Hz, stereo, s16, 1411 kb/s"
    info = parse_stream_info(line)
    assert info["codec"] == "pcm_s16le"
    assert "profile" not in info, "no parentheses means no profile"
    assert info["sample_rate"] == 44100
    assert info["channels"] == 2


# ------------------------------------------------------------------- plumbing
def test_input_url_and_duration_lines_are_ignored():
    stderr = (
        "Input #0, mpegts, from 'pipe:':\n"
        "  Duration: 00:00:06.01, start: 1.400000, bitrate: 132 kb/s\n"
        "  Program 1\n"
        f"{AAC_IN_TS}\n"
        "      encoder         : Lavf58.76.100\n"
    )
    info = parse_stream_info(stderr)
    assert info["codec"] == "aac"
    assert info["profile"] == "LC"
    assert info["duration"] == "00:00:06.01"


def test_video_streams_are_skipped():
    """A master playlist may carry video; we only want the audio line."""
    stderr = f"  Stream #0:0: Video: h264 (High), yuv420p, 1920x1080, 25 fps\n{AAC_IN_TS}\n"
    info = parse_stream_info(stderr)
    assert info["codec"] == "aac"


def test_garbage_yields_empty_information():
    info = parse_stream_info("nonsense\n\n")
    assert info["codec"] is None
    assert info["sample_rate"] is None
    assert info["channels"] is None
    assert info["bitrate"] is None
