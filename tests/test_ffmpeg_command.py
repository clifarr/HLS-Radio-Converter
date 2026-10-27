"""The ffmpeg command line: which encoder runs, and with what settings.

The output codec and the *copy* decision were once answered by one flag, and
that was a real bug:

    ``output_codec="adts"`` used to force ``-acodec copy`` for **every**
    source. So an MP3 source was copied straight into an ADTS container.
    ffmpeg accepts that command, starts happily, and emits a stream no
    radio can decode - the ADTS muxer expects AAC frames and gets MP3 ones.

The radio was sent silence, and the only symptom was "the station works on one
format and not the other". The fix at the time was to pass ``copy_audio`` in;
the better answer, once the FFmpeg-free path could forward a matching source
without a child process at all, was that nothing needs the copy: every stream
that reaches this command is genuinely decoded and encoded. These tests pin
that, and pin what each encoder is told.
"""

from __future__ import annotations

import inspect
from typing import Optional

from radioconv import ffmpeg as ff
from radioconv.config import OutputCodec, Settings


def _command(output_codec: OutputCodec, *, sample_rate: Optional[int] = None) -> list:
    settings = Settings(
        output_codec=output_codec, bitrate=192_000, channels=2, sample_rate=sample_rate
    )
    return ff.build_command(settings, "mpegts")


def _value_after(command: list, flag: str) -> str:
    return command[command.index(flag) + 1]


def test_an_mp3_source_for_an_adts_output_is_encoded():
    """The regression: ``-acodec copy -f adts`` on an MP3 source.

    Copying MP3 into an ADTS container produces a stream that no radio can
    decode. Asking for AAC has to *encode* to AAC.
    """
    command = _command("adts")
    assert "-acodec" in command
    assert _value_after(command, "-acodec") == "aac"
    assert _value_after(command, "-f") == "adts"


def test_an_mp3_source_for_an_mp3_output_is_encoded():
    """The default path: MP3 in, MP3 out, re-encoded."""
    command = _command("mp3")
    assert _value_after(command, "-acodec") in ("libmp3lame", "mp3")
    assert _value_after(command, "-f") == "mp3"


def test_no_output_codec_ever_asks_ffmpeg_to_copy():
    """``-acodec copy`` is gone, for every codec - not merely shown not to happen.

    The command builder cannot express a copy any more, and the pipeline cannot
    ask for one: a source that already matches the output is forwarded by
    ``_run_passthrough`` with no FFmpeg at all. So the check is on the assembled
    command *and* on the signature, because a parameter is how this came back
    the first time.
    """
    assert "copy_audio" not in inspect.signature(ff.build_command).parameters, (
        "a copy flag is back on build_command - see this module's docstring"
    )
    for codec in ("mp3", "adts", "auto"):
        command = _command(codec)
        assert "copy" not in command, f"output_codec={codec!r} asked ffmpeg to copy"


def test_the_adts_encoder_carries_a_bitrate():
    """An encoded ADTS stream needs a bitrate, or ffmpeg picks its own."""
    command = _command("adts")
    assert _value_after(command, "-b:a") == "192000"


def test_the_adts_encoder_carries_the_sample_rate_too():
    """``sample_rate`` applied to the MP3 path only, which made it a trap.

    The settings page calls it "Converted stations only. Empty keeps the source
    rate." Nothing there says "MP3 only", so choosing AAC quietly turned the
    field into a no-op - and a no-op field is worse than an absent one, because
    the operator believes they changed something. Both encoders take ``-ar``.
    """
    command = _command("adts", sample_rate=44_100)

    assert _value_after(command, "-acodec") == "aac"
    assert _value_after(command, "-ar") == "44100"


def test_the_mp3_encoder_is_unchanged():
    """The MP3 path keeps its bitrate, channel count and sample rate."""
    command = _command("mp3", sample_rate=48_000)
    assert _value_after(command, "-b:a") == "192000"
    assert _value_after(command, "-ac") == "2"
    assert _value_after(command, "-ar") == "48000"
    assert _value_after(command, "-f") == "mp3"
