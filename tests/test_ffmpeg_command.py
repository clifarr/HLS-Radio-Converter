"""The ffmpeg command line: what gets copied, what gets encoded.

The output codec and the *copy* decision are two different questions, and
answering them with one flag was a real bug:

    ``output_codec="adts"`` used to force ``-acodec copy`` for **every**
    source. So an MP3 source was copied straight into an ADTS container.
    ffmpeg accepts that command, starts happily, and emits a stream no
    radio can decode - the ADTS muxer expects AAC frames and gets MP3 ones.

The radio is then sent silence, and the only symptom is "the station works
on one format and not the other". These tests pin the rule:

* ``-acodec copy`` is used only when the source really is the codec being
  asked for.
* An MP3 source going to an ADTS output is **encoded** to AAC, because
  that is the only way to produce a valid ADTS stream from MP3.
"""

from __future__ import annotations

from radioconv import ffmpeg as ff
from radioconv.config import OutputCodec, Settings


def _command(output_codec: OutputCodec, *, copy_audio: bool = False) -> list:
    settings = Settings(output_codec=output_codec, bitrate=192_000, channels=2)
    return ff.build_command(settings, "mpegts", copy_audio=copy_audio)


def _value_after(command: list, flag: str) -> str:
    return command[command.index(flag) + 1]


def test_an_mp3_source_for_an_adts_output_is_encoded_not_copied():
    """The regression: ``-acodec copy -f adts`` on an MP3 source.

    Copying MP3 into an ADTS container produces a stream that no radio can
    decode. The source has to be re-encoded to AAC.
    """
    command = _command("adts", copy_audio=False)
    assert "-acodec" in command
    assert _value_after(command, "-acodec") != "copy"
    assert _value_after(command, "-acodec") == "aac"
    assert _value_after(command, "-f") == "adts"


def test_an_aac_source_for_an_adts_output_is_copied():
    """The good case: the source already is the output codec."""
    command = _command("adts", copy_audio=True)
    assert _value_after(command, "-acodec") == "copy"
    assert _value_after(command, "-f") == "adts"


def test_an_mp3_source_for_an_mp3_output_is_encoded():
    """The default path is unchanged: MP3 in, MP3 out, re-encoded."""
    command = _command("mp3", copy_audio=False)
    assert _value_after(command, "-acodec") in ("libmp3lame", "mp3")
    assert _value_after(command, "-f") == "mp3"


def test_the_output_codec_alone_never_forces_a_copy():
    """``output_codec="adts"`` must not imply ``-acodec copy``.

    This is the whole bug in one assertion: the *setting* said ADTS, and
    the code turned that into "copy the input", regardless of what the
    input was.
    """
    for codec in ("mp3", "adts", "auto"):
        command = _command(codec, copy_audio=False)
        assert _value_after(command, "-acodec") != "copy", (
            f"output_codec={codec!r} forced -acodec copy"
        )


def test_the_adts_encoder_carries_a_bitrate():
    """An encoded ADTS stream needs a bitrate, or ffmpeg picks its own."""
    command = _command("adts", copy_audio=False)
    assert "-b:a" in command
    assert _value_after(command, "-b:a") == "192000"


def test_the_mp3_encoder_is_unchanged():
    """The MP3 path keeps its bitrate, channel count and sample rate."""
    settings = Settings(output_codec="mp3", bitrate=192_000, channels=2, sample_rate=48000)
    command = ff.build_command(settings, "mpegts")
    assert _value_after(command, "-b:a") == "192000"
    assert _value_after(command, "-ac") == "2"
    assert _value_after(command, "-ar") == "48000"
    assert _value_after(command, "-f") == "mp3"
