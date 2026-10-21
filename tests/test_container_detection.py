"""Streams that are not MPEG-TS.

Every test here is a regression test for one bug, and the bug was the same each
time: the container was *guessed from the URL* (``mp4`` if the playlist had an
init segment, **``ts`` otherwise**) and that guess was handed to ffmpeg as
``-f``. Anything that was neither MP4 nor TS therefore reached ffmpeg with a
demuxer that could not read it, ffmpeg emitted nothing, and the caller had
already been told ``200 audio/mpeg``. A radio connects successfully and hears
silence - the worst possible failure, because nothing in the logs says "failed".

The reported case was a Microsoft ``msvdn.net`` stream whose ``.aac`` segments
are raw ADTS AAC behind two ID3v2 tags. ``radioconv probe`` described it as
``container : ts``, which was itself the wrong answer.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

import httpx

from radioconv import ffmpeg as ffmpeg_mod
from radioconv import hls as hls_mod
from radioconv import tsdemux
from radioconv.config import Settings
from radioconv.hls import HLSClient, PlaylistInfo, PlaylistTracker
from radioconv.pipeline import StreamPipeline

#: Long enough for a session to reach ffmpeg and produce real output, short
#: enough that a regression fails the suite rather than hanging it.
COLLECT_SECONDS = 90


# ------------------------------------------------------------------ the probe
def test_probe_describes_an_id3_adts_stream_correctly(live_server, id3_adts_server):
    """`probe` must not call a raw ADTS stream a transport stream."""
    body = httpx.get(live_server + "/probe", params={"url": id3_adts_server}, timeout=60).json()

    assert body["ok"], body.get("error")
    assert body["segment_container"] == "adts"
    assert body["container"] == "adts", "the measured container must win over the URL guess"
    assert body["audio"]["codec"] == "aac", "ffmpeg could not read the segment"
    assert body["audio"]["sample_rate"] == 48000
    # Pass-through is a TS table lookup, so it cannot apply - but the stream is
    # still decodable, which is the thing that matters.
    assert body["passthrough_possible"] is False


async def test_probe_reports_the_codec_for_an_id3_adts_segment(id3_adts_server):
    """The probe used to try exactly two input formats: mpegts, then mp4.

    Everything else came back ``codec: None``, so ``radioconv probe`` printed
    ``audio codec : None  ? Hz`` for a stream it had just downloaded in full.

    ``async def`` rather than a sync test wrapping ``asyncio.run``: on Python 3.9
    ``asyncio.run`` *closes and clears* the main thread's event loop, and any
    later test that constructs an ``asyncio.Lock`` (``SessionManager.__init__``
    does) then dies with "There is no current event loop in thread 'MainThread'".
    Running it as a coroutine means pytest-asyncio owns the loop, as it does for
    every other async test here.
    """
    info = await hls_mod.inspect(id3_adts_server, Settings())

    assert info["ok"], info.get("error")
    assert info["audio"]["codec"] == "aac"
    assert info["audio"]["channels"] == 2


# ----------------------------------------------------------- the actual stream
def test_an_id3_adts_stream_produces_audio(live_server, id3_adts_server):
    """The whole bug, end to end: this used to deliver zero bytes.

    Measured before the fix: ``HTTP 200 | audio/mpeg | 0 bytes`` for a full 25
    second read. The HTTP layer was entirely happy throughout.
    """
    received = 0
    with httpx.stream(
        "GET", live_server + "/stream", params={"url": id3_adts_server}, timeout=60
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/mpeg")
        for chunk in response.iter_bytes():
            received += len(chunk)
            if received >= 100_000:
                break

    assert received >= 100_000, f"only {received} bytes: the stream is silent"


def _describe(audio: bytes) -> str:
    """Write *audio* to a temp file and return ffmpeg's report on it.

    Blocking, and called through ``asyncio.to_thread`` - which is why it is a
    module-level function rather than inline: an async test doing its own file
    I/O and subprocess work trips ruff's ASYNC221/ASYNC240, and they are right.
    """
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
        handle.write(audio)
        path = Path(handle.name)
    try:
        return subprocess.run(
            [
                ffmpeg_mod.locate_ffmpeg(Settings().ffmpeg_path or None),
                "-hide_banner",
                "-i",
                str(path),
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stderr
    finally:
        path.unlink(missing_ok=True)


async def test_the_audio_from_an_id3_adts_stream_is_valid_mp3(id3_adts_server):
    """Bytes are not enough; they have to be decodable MP3.

    Guards against "it produced output" being mistaken for "it produced audio" -
    feeding ffmpeg the wrong ``-f`` can also produce a short burst of garbage
    before it gives up.

    Two details here are deliberate, and both were learned from this bug:

    * **The read is bounded.** With the defect present the stream yields nothing
      at all, so an unbounded ``async for`` turns a regression into a *hung test
      suite* rather than a failing test - measured: 900 seconds of hanging before
      a timeout killed it. ``wait_for`` makes the bug fail fast.
    * **The generator is closed explicitly.** Breaking out of ``async for``
      leaves ``StreamPipeline``'s feeder and stdout-reader tasks running, and
      their eventual failure surfaces as an unrelated-looking "I/O operation on
      closed file" during teardown. ``contextlib.aclosing`` would say this more
      neatly, but it is Python 3.10+ and the floor here is 3.9.
    """
    settings = Settings(bitrate=192_000)
    pipeline = StreamPipeline(settings, [id3_adts_server], label="fixture")
    gen = pipeline.stream()
    chunks: list[bytes] = []
    received = 0

    async def drain() -> None:
        # `nonlocal`: augmenting a counter inside a nested coroutine binds it
        # locally otherwise, which is a plain UnboundLocalError (ruff's F823).
        nonlocal received
        async for chunk in gen:
            chunks.append(chunk)
            received += len(chunk)
            if received >= 200_000:
                break

    try:
        await asyncio.wait_for(drain(), timeout=COLLECT_SECONDS)
    finally:
        await gen.aclose()
        await pipeline.aclose()

    audio = b"".join(chunks)
    assert len(audio) >= 200_000, (
        f"only {len(audio)} bytes in {COLLECT_SECONDS}s - the stream is silent, "
        "which is what a wrong ffmpeg input format produces"
    )

    report = await asyncio.wait_for(asyncio.to_thread(_describe, audio), timeout=COLLECT_SECONDS)
    assert "Audio: mp3" in report, report[-400:]
    assert "192 kb/s" in report, report[-400:]


def test_the_pipeline_reports_the_detected_container(live_server, id3_adts_server):
    """`/health` and the dashboard show what was measured, not what was guessed."""
    with httpx.stream(
        "GET", live_server + "/stream", params={"url": id3_adts_server}, timeout=60
    ) as response:
        for chunk in response.iter_bytes():
            if chunk:
                break
        health = httpx.get(live_server + "/health", timeout=20).json()

    modes = [detail["pipeline"]["mode"] for detail in health["streams"]["details"]]
    assert modes, "no session was created"
    assert "transcode" in modes, f"expected ffmpeg for raw ADTS, got {modes}"


# ------------------------------------------------------- the input-format table
def test_input_formats_map_to_ffmpeg_demuxers():
    """Our container names are not ffmpeg's, and the mapping is load-bearing.

    ``adts`` is the honest name for the *segment content*, but ffmpeg's demuxer
    for it is ``aac``. ``-f adts`` exists and rejects this data outright
    ("Invalid argument"), so an identity mapping would fail.
    """
    assert ffmpeg_mod.INPUT_FORMATS["ts"] == "mpegts"
    assert ffmpeg_mod.INPUT_FORMATS["mp4"] == "mp4"
    assert ffmpeg_mod.INPUT_FORMATS["adts"] == "aac"
    assert ffmpeg_mod.INPUT_FORMATS["mp3"] == "mp3"


def test_build_command_uses_the_mapped_demuxer():
    command = ffmpeg_mod.build_command(Settings(), "adts")
    assert command[command.index("-f") + 1] == "aac"
    assert command[command.index("-i") + 1] == "pipe:0"


def test_build_command_omits_the_demuxer_for_an_unknown_container():
    """Better no ``-f`` at all than a wrong one.

    ffmpeg probing the bytes finds the right demuxer; a wrong ``-f`` makes it read
    nothing and the failure is silent. Verified against a real segment: probing
    and ``-f aac`` produce byte-identical output.

    Only the *input* half of the command is examined - there is always a ``-f``
    at the end, which is the output container, and that one must stay.
    """
    for unknown in (ffmpeg_mod.UNKNOWN_INPUT, "nonsense", ""):
        command = ffmpeg_mod.build_command(Settings(), unknown)
        input_side = command[: command.index("-i")]
        assert "-f" not in input_side, f"{unknown!r} was passed to -f on the input"
        assert command[command.index("-i") + 1] == "pipe:0"
        # The output format is unaffected either way.
        assert command[-3:] == ["-f", "mp3", "-"]


def test_build_command_never_asks_ffmpeg_to_discard_startup_audio():
    """``+nobuffer`` throws away real audio, at tuning-in time of all times.

    Measured with ffmpeg 7.1 on a 60 s AAC/MPEG-TS source piped through stdin
    exactly as the pipeline pipes it: ``+genpts`` alone decoded 60.02 s, adding
    ``+nobuffer`` decoded 54.80 s - 5.2 s of the stream silently gone, heard as
    the jump forward when a listener tunes in.
    """
    command = " ".join(ffmpeg_mod.build_command(Settings(), "ts"))
    assert "+nobuffer" not in command, (
        "+nobuffer makes ffmpeg discard the audio it used while probing the input"
    )
    assert "+genpts" in command, "genpts is still needed for missing timestamps"


def test_the_mp3_output_carries_no_id3_tag():
    """No ID3v2 tag in the MP3 we serve.

    ffmpeg's MP3 muxer copies the *input's* ID3v2 metadata into the output.
    Our output is a pipe, so the tag cannot be written once at the start and
    fixed up afterwards - it lands where the muxer decides to write it. That
    tag is a 1.6 KB blob of **non-audio** inside an MP3 stream, and a radio's
    decoder cannot skip a tag in the middle of one.

    It is not hypothetical. When the pipeline restarts its encoder (an upstream
    fetch failed, so a new ffmpeg is started), the second encode's tag lands
    in the MIDDLE of the listener's stream. Measured on a real station: the
    spliced stream decoded with "Header missing" and "invalid new backstep -1",
    while the same splice with the tag suppressed decoded with zero errors.

    The song title is delivered to the radio as ICY metadata, which is the
    standard way; the tag would only be a second, non-standard copy of it.
    """
    command = ffmpeg_mod.build_command(Settings(), "adts")
    assert "-id3v2_version" in command, (
        "ffmpeg would write an ID3 tag into the MP3, and it lands in the middle "
        "of the listener's stream whenever the pipeline restarts"
    )
    assert command[command.index("-id3v2_version") + 1] == "0", (
        "the tag must be suppressed, not merely downgraded"
    )


def test_the_adts_output_is_not_affected_by_the_id3_suppression():
    """The suppression is for MP3 only; ADTS has no ID3 tag to begin with.

    The container follows the *output codec*, which is the user-visible
    contract (and what the ``Content-Type`` says). ``copy_audio`` is a
    separate question - it means "the source is already the codec being
    asked for" - and it must not be able to change the container. The old
    code let it: ``copy_audio=True`` with an MP3 output produced
    ``-acodec copy -f adts``, which is the bug this project just fixed.
    """
    command = ffmpeg_mod.build_command(Settings(output_codec="adts"), "adts", copy_audio=True)
    assert "-id3v2_version" not in command
    assert command[-3:] == ["-f", "adts", "-"]


def test_the_container_follows_the_output_codec_not_the_copy_flag():
    """The container is the output codec, whatever the copy flag says."""
    # An MP3 output with a copy flag is still an MP3 stream.
    command = ffmpeg_mod.build_command(Settings(output_codec="mp3"), "mpegts", copy_audio=True)
    assert command[-3:] == ["-f", "mp3", "-"]
    # An ADTS output is still an ADTS stream.
    command = ffmpeg_mod.build_command(Settings(output_codec="adts"), "mpegts", copy_audio=True)
    assert command[-3:] == ["-f", "adts", "-"]


def test_the_default_container_is_honest_when_the_playlist_does_not_say():
    """The false dichotomy that caused all of this.

    ``container`` used to be ``"mp4" if <evidence of mp4> else "ts"``. "Not MP4"
    does not imply "TS", and treating it as though it did is why a raw AAC stream
    was announced as a transport stream.
    """
    info = PlaylistInfo(requested_url="u", media_url="u", is_master=False)
    assert info.container == "unknown"


async def test_priming_a_non_mp4_playlist_does_not_claim_ts(id3_adts_server):
    """The *call site*, not just the field default.

    Asserting on ``PlaylistInfo``'s default alone is not enough: the bug lived in
    the argument ``prime()`` passes, and a dataclass default can be correct while
    the caller still overrides it with a wrong guess. This drives a real playlist
    through ``PlaylistTracker.prime()`` so the value that actually reaches the
    pipeline is the one under test.
    """
    settings = Settings()
    client = HLSClient(settings)
    try:
        info = await PlaylistTracker(client, id3_adts_server, settings).prime()
    finally:
        await client.aclose()

    assert info.is_master is False
    assert info.container == "unknown", (
        "a playlist with no EXT-X-MAP and no .m4s URLs gives no evidence of MP4, "
        "and no evidence of TS either - so it must not claim one"
    )


# ------------------------------------------------------------------- detection
#: Two stacked tags, then audio. The structure a real CDN sends.
_REAL_LAYOUT = (
    b"ID3\x04\x00\x00\x00\x00\x00\x29PRIV\x00\x00\x005\x00\x00com.apple.streaming"
    b"\x00\x00\x00\x01\xb1"
    b'ID3\x04\x00\x00\x00\x00\x02\x2cTEXT\x00\x00\x04%\x00\x00\x03{"songInfo"}'
    b"\x00" * 500
)


def test_a_tag_with_no_audio_after_it_is_not_guessed_at():
    """A lone ID3 tag is metadata, not a container.

    Returning a container for this would be worse than returning ``None``: the
    pipeline would start ffmpeg with a confident ``-f`` for data that has no
    audio in it at all.
    """
    assert tsdemux.classify_segment(_REAL_LAYOUT) is None
    assert tsdemux.classify_segment(b"") is None
