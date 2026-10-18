"""FFmpeg discovery, command construction and stream probing.

FFmpeg is *effectively mandatory*. The pure-python demuxer can forward an
MP3-in-MPEG-TS stream untouched, but that is a narrow case: almost every real
radio stream is AAC (or HE-AAC) in MPEG-TS and has to be decoded and re-encoded
to MP3, which only FFmpeg can do. ``pipeline.py`` decides which engine a given
stream ends up using; this module only answers "where is the binary?".

Where the binary comes from depends on the platform. ``imageio-ffmpeg`` is a
normal pip wheel that bundles one for macOS, Windows and Linux x86_64/aarch64 -
but it publishes **no build for 32-bit ARM** (``armv6l``/``armv7l``, the default
Raspberry Pi OS install on older Pis). There, and anywhere else the wheel is
absent, a *system* ffmpeg is required (``apt install ffmpeg``).

We deliberately keep all knowledge of FFmpeg in this one module: the rest of the
application asks for a *pipeline* and does not care whether it is served by
FFmpeg or by the pure-python demuxer.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from typing import Any, Dict, List, Optional

from .config import Settings

log = logging.getLogger(__name__)

#: Input container (our name, as returned by ``tsdemux.classify_segment``) -> the
#: name ffmpeg's ``-f`` wants. Deliberately not an identity mapping: our name for
#: a bare AAC segment is ``adts`` because that is what the HLS segment contains,
#: while ffmpeg's demuxer for it is called ``aac``. (``-f adts`` exists but
#: rejects real data - it cannot read the `aac` demuxer's ID3 handling.)
INPUT_FORMATS = {"ts": "mpegts", "mp4": "mp4", "adts": "aac", "mp3": "mp3"}

#: A container we could not identify from the bytes. ffmpeg is then left to probe
#: the stream itself, which is strictly better than guessing: a *wrong* ``-f``
#: makes ffmpeg read nothing from a stream it could have decoded, and the failure
#: is silent - HTTP 200, the right Content-Type, and not one byte of audio.
UNKNOWN_INPUT = "unknown"


class FFmpegNotFound(RuntimeError):
    """Raised when no usable ffmpeg binary can be found."""


#: Architectures for which ``imageio-ffmpeg`` publishes a bundled binary. This
#: mirrors ``imageio_ffmpeg._definitions.FNAME_PER_PLATFORM`` (Windows x86_64 and
#: i686, macOS x86_64 and arm64, Linux x86_64 and aarch64). 32-bit ARM is
#: conspicuously absent - and that is the default install on older Pis, which
#: makes it the single most likely platform to hit a missing ffmpeg.
_FFMPEG_WHEEL_ARCHES = frozenset({"x86_64", "amd64", "aarch64", "arm64", "i686", "x86"})


def bundled_ffmpeg_supported() -> bool:
    """Whether the ``imageio-ffmpeg`` wheel ships a binary for this machine.

    Only used to make advice accurate. Telling a Raspberry Pi user to
    "reinstall imageio-ffmpeg" is actively misleading: no 32-bit ARM build has
    ever been published, so no amount of reinstalling will produce one.
    """
    return (platform.machine() or "").lower() in _FFMPEG_WHEEL_ARCHES


def install_hint() -> str:
    """Platform-appropriate advice for a missing ffmpeg binary."""
    if bundled_ffmpeg_supported():
        return (
            "install one (apt install ffmpeg), or reinstall the package so the "
            "bundled binary is present (pip install --force-reinstall imageio-ffmpeg)"
        )
    machine = platform.machine() or "this architecture"
    return (
        f"imageio-ffmpeg publishes no binary for {machine}, so ffmpeg has to come "
        "from the system: sudo apt install ffmpeg"
    )


def _sidecar_ffmpeg() -> Optional[str]:
    """Find an ffmpeg binary shipped *next to* us.

    Two cases matter:

    * ``IMAGEIO_FFMPEG_EXE`` is set (the documented escape hatch of
      ``imageio-ffmpeg``, and how the PyInstaller build points at its bundled
      copy);
    * we are running from a PyInstaller bundle, where the binary was unpacked
      into ``sys._MEIPASS`` or sits beside the executable.

    Checking this *before* importing ``imageio_ffmpeg`` keeps the frozen build
    from having to guess.
    """
    candidates: List[str] = []
    override = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if override:
        candidates.append(override)
    if getattr(sys, "frozen", False):  # pragma: no cover - only in a bundle
        for directory in {getattr(sys, "_MEIPASS", ""), os.path.dirname(sys.executable)}:
            if not directory:
                continue
            for name in ("ffmpeg", "ffmpeg.exe"):
                candidates.append(os.path.join(directory, name))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


@lru_cache(maxsize=1)
def locate_ffmpeg(configured: Optional[str] = None) -> str:
    """Return the path of a usable ffmpeg binary.

    Search order: explicit configuration, a sidecar/bundled copy, an ffmpeg
    already on ``PATH``, and only then the ``imageio-ffmpeg`` wheel.
    """
    if configured:
        path = shutil.which(configured) or (configured if _runs(configured) else None)
        if not path:
            raise FFmpegNotFound(f"configured ffmpeg is not executable: {configured}")
        return path

    sidecar = _sidecar_ffmpeg()
    if sidecar:
        return sidecar

    # A system ffmpeg, before the wheel.
    #
    # The wheel exists so that installing this package is enough on a machine with
    # no ffmpeg at all - not so that it can override one the administrator already
    # installed and the installer deliberately provides. That distinction turned
    # out to matter: imageio-ffmpeg's x86_64 release build segfaults on *any*
    # MPEG-TS input, so preferring it made every transcoded station silent on
    # exactly the machines that had a working distro ffmpeg sitting beside it -
    # and `/health` blamed the bundled path, which pointed at the wheel rather
    # than at the cause.
    system = shutil.which("ffmpeg")
    if system and _runs(system):
        return system

    try:  # pragma: no cover - depends on the installed wheel
        import imageio_ffmpeg

        candidate = imageio_ffmpeg.get_ffmpeg_exe()
        if candidate:
            # imageio-ffmpeg's last resort is the *system* binary, and it returns
            # that as the bare name "ffmpeg" (``_utils._get_ffmpeg_exe`` step 4).
            # That happens on exactly the platforms with no bundled build, i.e.
            # 32-bit ARM. Resolve it to a real path: reporting "ffmpeg" tells the
            # user nothing about where their binary came from, and a bare name
            # can be shadowed by anything else on PATH later.
            if not os.path.isabs(candidate):
                candidate = shutil.which(candidate) or candidate
            if _runs(candidate):
                return candidate
    except Exception as exc:  # noqa: BLE001 - any import/probe failure means "no bundled ffmpeg"
        log.debug("bundled ffmpeg unavailable: %s", exc)

    raise FFmpegNotFound(f"no ffmpeg binary found: {install_hint()}")


def _runs(path: str) -> bool:
    try:
        # Explicit argv list, no shell, and a hard timeout: the binary path is
        # either ours or the administrator's, never remote input.
        proc = subprocess.run(
            [path, "-hide_banner", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    else:
        return proc.returncode == 0


@lru_cache(maxsize=1)
def ffmpeg_version(path: str = "") -> str:
    try:
        binary = locate_ffmpeg(path or None)
        proc = subprocess.run(
            [binary, "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable ({exc})"
    first = (proc.stdout or proc.stderr).splitlines()[0]
    return first.strip()


@lru_cache(maxsize=1)
def encoders(path: str = "") -> set:
    """Set of encoder names compiled into this ffmpeg build."""
    try:
        binary = locate_ffmpeg(path or None)
        proc = subprocess.run(
            [binary, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    found = set()
    for line in (proc.stdout or "").splitlines():
        match = re.match(r"^\s*[A-Z.]{6}\s+(\S+)", line)
        if match:
            found.add(match.group(1))
    return found


def build_command(settings: Settings, input_format: str, *, copy_audio: bool = False) -> List[str]:
    """Build the ffmpeg command for one stream.

    ``input_format`` is one of :data:`INPUT_FORMATS`' keys, or anything else (in
    which case ffmpeg probes the input).

    ``copy_audio`` performs a *remux only* (no re-encode) which is essentially
    free - used when the radio understands the source codec.
    """
    binary = locate_ffmpeg(settings.ffmpeg_path)
    fmt = INPUT_FORMATS.get(input_format)

    cmd: List[str] = [
        binary,
        "-hide_banner",
        "-nostdin",  # never read from our tty; stdin is the audio pipe
        "-loglevel",
        "warning",
        "-nostats",
        "-threads",
        str(max(1, settings.ffmpeg_threads)),
        # ``genpts`` fills in missing timestamps. ``+nobuffer`` is deliberately
        # NOT set: it makes ffmpeg discard the audio used during input probing,
        # which silently drops seconds of real audio at startup. Measured with
        # ffmpeg 7.1 on a 60s AAC/MPEG-TS source fed through stdin, exactly as
        # the pipeline feeds it: ``+genpts`` alone decoded 60.02s, adding
        # ``+nobuffer`` decoded 54.80s - 5.2s of the stream simply gone, heard
        # as a jump forward when a listener tunes in.
        "-fflags",
        "+genpts",
    ]
    if fmt:
        cmd += ["-f", fmt]
    # else: no `-f` at all, so ffmpeg probes the bytes. Verified against a real
    # ID3-tagged ADTS segment: probing and `-f aac` produce byte-identical output.
    if settings.ffmpeg_input_args:
        cmd += settings.ffmpeg_input_args.split()
    cmd += ["-i", "pipe:0"]

    cmd += ["-vn", "-sn", "-dn", "-map", "0:a:0"]

    if copy_audio or settings.output_codec == "adts":
        cmd += ["-acodec", "copy"]
    else:
        # libmp3lame is present in the bundled wheel and in every distro build.
        encoder = "libmp3lame" if "libmp3lame" in encoders(settings.ffmpeg_path) else "mp3"
        cmd += ["-acodec", encoder, "-b:a", str(settings.bitrate), "-ac", str(settings.channels)]
        if settings.sample_rate:
            cmd += ["-ar", str(settings.sample_rate)]

    if settings.ffmpeg_output_args:
        cmd += settings.ffmpeg_output_args.split()

    container = "adts" if (copy_audio or settings.output_codec == "adts") else "mp3"
    cmd += ["-f", container, "-"]
    return cmd


# --------------------------------------------------------------------- probing
# Parses a line such as:
#
#   Stream #0:0[0x100]: Audio: aac (LC) ([15][0][0][0] / 0x000F), 44100 Hz, mono, fltp, 123 kb/s
#   Stream #0:0:        Audio: mp3 (mp3float), 44100 Hz, mono, fltp, 128 kb/s
#
# The profile is the *first* parenthesised group. Inside a transport stream
# ffmpeg adds a SECOND group - the MPEG-TS stream type, `([15][0][0][0] / 0x000F)` -
# before the first comma. Capturing the profile as `[^,]*` therefore swallowed
# that descriptor as well, and `radioconv probe` printed:
#
#   audio codec  : aac LC) ([15][0][0][0] / 0x000F 44100 Hz
#
# so the profile is matched explicitly as the contents of the first parentheses,
# and whatever follows it up to the comma is discarded.
_STREAM_RE = re.compile(
    r"Stream #(?P<index>\d+):(?P<sub>\d+)"
    r".*?: Audio: (?P<codec>[A-Za-z0-9_]+)"
    r"(?:\s*\((?P<profile>[^)]*)\))?"
    r"[^,]*"
    r"(?:,\s*(?P<rate>\d+) Hz)?"
    r"(?:,\s*(?P<layout>mono|stereo|[\d.]+(?:\([^)]*\))?))?"
    r"(?:.*?,\s*(?P<bitrate>\d+) kb/s)?",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(r"Duration: (?P<dur>[\d:.]+)")


def parse_stream_info(stderr: str) -> Dict[str, Any]:
    """Parse ``ffmpeg -i`` stderr into a small dict.

    Kept as a pure function so it can be unit-tested without ffmpeg present.
    """
    info: Dict[str, Any] = {"codec": None, "sample_rate": None, "channels": None, "bitrate": None}
    for line in stderr.splitlines():
        if "Duration:" in line and info.get("duration") is None:
            match = _DURATION_RE.search(line)
            if match:
                info["duration"] = match.group("dur")
        if "Audio:" not in line:
            continue
        match = _STREAM_RE.search(line)
        if not match:
            continue
        info["codec"] = match.group("codec").lower()
        profile = (match.group("profile") or "").strip()
        if profile:
            info["profile"] = profile
        if match.group("rate"):
            info["sample_rate"] = int(match.group("rate"))
        layout = match.group("layout")
        if layout:
            info["channels"] = {"mono": 1, "stereo": 2}.get(layout.lower())
        if match.group("bitrate"):
            info["bitrate"] = int(match.group("bitrate")) * 1000
        break
    return info


def probe_bytes(
    data: bytes,
    settings: Optional[Settings] = None,
    timeout: float = 20.0,
    input_format: Optional[str] = None,
) -> Dict[str, Any]:
    """Identify the audio stream in a chunk of HLS data.

    ``input_format`` is the container ``tsdemux.classify_segment`` reported. It is
    only a hint about *which candidate to try first*: the formats are attempted
    in turn and the first one that yields a codec wins, ending with no ``-f`` at
    all so ffmpeg probes the bytes itself. Passing it therefore saves time rather
    than changing the answer.

    The candidate list matters because this used to try exactly two: mpegts, then
    mp4. Every other container reported ``codec: None``, so ``probe`` printed
    ``audio codec : None  ? Hz`` for a stream it had just downloaded and could
    have described perfectly.

    Blocking; call from a worker thread (``asyncio.to_thread``).
    """
    settings = settings or Settings()
    binary = locate_ffmpeg(settings.ffmpeg_path)

    candidates: List[Optional[str]] = []
    for name in (input_format, "ts", "mp4", "adts", "mp3"):
        fmt = INPUT_FORMATS.get(name) if name else None
        if fmt is not None and fmt not in candidates:
            candidates.append(fmt)
    candidates.append(None)  # no -f: let ffmpeg work it out

    last_error = ""
    for fmt in candidates:
        cmd = [binary, "-hide_banner", "-nostdin"]
        if fmt:
            cmd += ["-f", fmt]
        cmd += ["-i", "pipe:0", "-f", "null", "-"]
        try:
            proc = subprocess.run(
                cmd, input=data, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            last_error = "probe timed out"
            continue
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = str(exc)
            continue

        info = parse_stream_info((proc.stderr or b"").decode("utf-8", "replace"))
        if info.get("codec") is not None:
            info.setdefault("bytes_probed", len(data))
            return info

    return {"codec": None, "error": last_error or "no container recognised the data"}
