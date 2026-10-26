"""The browser pages: where the markup lives and what each page needs.

The two pages a human can open - the converter at ``/`` and the live dashboard
at ``/dashboard`` - are HTML files in ``templates/`` with their CSS and
JavaScript in ``static/``. This module is only the glue: locating those
directories, deciding the URLs, and building each page's context.

Why not keep the HTML in :mod:`radioconv.api`? Because it was there, and it grew
to a 60-line f-string with CSS inside it, HTML-escaped by hand in three places.
Markup belongs in a markup file that an editor can highlight, and a template
engine escapes correctly rather than hopefully.

Neither page contains audio logic: the pages display, the API streams, and the
one JavaScript file polls ``/metrics.json`` for its numbers.
"""

from __future__ import annotations

import functools
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse

from . import __version__
from .config import Settings, ui_settings_path

if TYPE_CHECKING:  # pragma: no cover - import time only
    # Annotated, never imported at runtime: see environment() below.
    from jinja2 import Environment

#: Directory names inside the package (and inside a PyInstaller bundle).
TEMPLATES = "templates"
STATIC = "static"

#: The package itself: where ``templates/`` and ``static/`` sit for an ordinary
#: install, and where this module lives whether frozen or not.
PACKAGE_DIR = Path(__file__).resolve().parent

#: Where ``StaticFiles`` is mounted in :func:`radioconv.api.create_app`, and the
#: prefix the templates therefore use for their assets.
STATIC_URL = "/static"

#: The JSON snapshot the dashboard polls, and the JSON endpoint a script wants.
METRICS_JSON = "/metrics.json"

#: The live dashboard.
DASHBOARD_URL = "/dashboard"

#: The settings page, which is a form rather than a view.
SETTINGS_URL = "/settings"

#: How often the dashboard refreshes, in milliseconds. This is a *series* of
#: small JSON documents rather than a stream, so it is a cost on the Pi; once a
#: second is what a person watching wants, and the pages stop polling entirely
#: while the browser tab is hidden.
POLL_MS = 1000

#: Used as the form placeholder, and in the worked example on the page.
EXAMPLE_URL = "https://example.com/live/index.m3u8"


def resource_dir(name: str) -> Path:
    """Locate a packaged data directory, installed or frozen.

    ``packaging/radioconv.spec`` copies both directories to ``radioconv/<name>``
    in the bundle, which is what the frozen branch expects; everything else is
    an ordinary install (or a source checkout), where they sit next to this
    file. PyInstaller sets ``_MEIPASS``, and when it is set ``__file__`` points
    inside the archive rather than at real files, so the two cases genuinely
    need different answers.
    """
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "radioconv" / name
    return PACKAGE_DIR / name


@functools.lru_cache(maxsize=1)
def environment() -> Environment:
    """The Jinja environment, created on first use.

    Deferred on purpose: jinja2 is needed only when a person opens a page, and
    keeping imports off the module-load path is how this project keeps a cold
    start on a Raspberry Pi quick (see CONTRIBUTING.md). The service streams
    audio perfectly well without ever importing it.

    Autoescaping is on unconditionally - every template here is HTML - and
    ``StrictUndefined`` turns a typo in a template into a visible 500 instead of
    a blank space, because a blank space is how a broken dashboard ships.
    """
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    return Environment(
        loader=FileSystemLoader(str(resource_dir(TEMPLATES))),
        autoescape=True,
        undefined=StrictUndefined,
        # Both make the templates readable: a control tag on its own line does
        # not leave a blank line behind, so the generated HTML is as tidy as the
        # source.
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render(name: str, **context: Any) -> str:
    """Render a template, or raise. There is no silent-empty path."""
    return environment().get_template(name).render(**context)


def base_url(settings: Settings, request: Request) -> str:
    """The address a radio should use to reach us.

    ``public_host`` exists because the address we bind to (``0.0.0.0``) is not
    an address anyone can type, and behind NAT the ``Host`` header may be wrong
    too.
    """
    host = settings.public_host or request.headers.get("host") or f"{settings.host}:{settings.port}"
    return f"http://{host}"


def _page(name: str, **context: Any) -> HTMLResponse:
    """Render *name* with the context every page shares."""
    return HTMLResponse(
        render(
            name,
            version=__version__,
            static_url=STATIC_URL,
            metrics_url=METRICS_JSON,
            dashboard_url=DASHBOARD_URL,
            settings_url=SETTINGS_URL,
            poll_seconds=f"{POLL_MS / 1000:g}",
            refresh_ms=POLL_MS,
            **context,
        ),
        # The markup carries the version that the asset URLs are keyed on, so a
        # stale copy is as bad as a stale script: the browser would keep asking
        # for the previous release's assets. It costs a conditional request and
        # the page is a few kilobytes, rendered from a template on every load
        # anyway - there is nothing cheap to preserve except the bytes.
        headers={"cache-control": "no-cache"},
    )


def index_page(
    settings: Settings,
    request: Request,
    target: str = "",
    probe: Optional[Dict[str, Any]] = None,
    probe_on: bool = False,
) -> HTMLResponse:
    """``GET /`` - paste a stream URL, get back the URL to type into a radio.

    ``target`` is whatever the user typed, echoed back into the form. It is
    never trusted: the template escapes it, and it is only turned into links
    when it actually looks like an ``http(s)`` URL.

    ``probe`` is the already-fetched ``hls.inspect()`` result, or ``None`` when
    the user did not ask for a check. The route does the fetching (it is the
    only place allowed to do I/O) and this module turns the answer into rows -
    so the page cannot start a download of its own, and the diagnostics cannot
    disagree with ``/probe``, because they are literally the same call.
    """
    target = (target or "").strip()
    usable = target.startswith(("http://", "https://"))
    base = base_url(settings, request)
    # Percent-encoded for the path and for the query string, then escaped by the
    # template for the attribute it lands in: three different contexts, three
    # different encodings, each one the template's job except this one.
    encoded = quote(target, safe="") if usable else ""
    engine = _engine(probe, settings) if probe is not None else None

    return _page(
        "index.html",
        current="converter",
        base=base,
        target=target,
        usable=usable,
        example=EXAMPLE_URL,
        example_radio_url=f"{base}/{quote(EXAMPLE_URL, safe='')}",
        radio_url=f"{base}/{encoded}" if usable else "",
        listen_url=f"{base}/stream?url={encoded}" if usable else "",
        # The checkbox reflects the choice the user just made, and defaults to
        # on for somebody arriving at the form for the first time. The
        # diagnostics themselves are *not* re-run unless asked: see the route.
        probe_checked=probe_on or not target,
        probe=probe,
        engine=engine,
        probe_rows=_probe_rows(probe, engine) if probe is not None else [],
        # Only ever set when forwarding is possible but not happening, because
        # that is the one case where a different URL produces a different
        # engine *and* the reader can act on it.
        passthrough_url=_passthrough_url(engine, base, encoded),
        probe_json_url=f"{base}/probe?url={encoded}" if usable else "",
        # Shown whether or not the stream was checked: it changes what the page
        # promises, so it belongs above the form rather than inside a result.
        ffmpeg_enabled=settings.ffmpeg_enabled,
    )


def _passthrough_url(engine: Optional[Dict[str, Any]], base: str, encoded: str) -> str:
    """The same stream, asked for in the codec that needs no FFmpeg.

    Only the ``codec`` override: the bitrate is irrelevant when nothing is being
    encoded, and every extra parameter is one more thing a person has to copy
    correctly into a radio.

    Never offered on an installation with FFmpeg disabled, and not by a check
    here: ``_engine`` only ever reports a ``kind`` when a *transcode* could be
    turned into a forward, so that case cannot arise. A ``ffmpeg_enabled`` test
    in this function would be unreachable - and a guard case proved it was.
    """
    if not engine or not engine.get("kind") or not encoded:
        return ""
    return f"{base}/stream?url={encoded}&codec={engine['kind']}"


def _engine(result: Dict[str, Any], settings: Settings) -> Dict[str, Any]:
    """What this service will do with the stream, in one place.

    ``{"value", "verdict", "state", "kind"}``: a short label for the table, the
    sentence that explains it, a state the stylesheet colours, and the codec a
    ``codec=`` override would have to ask for to avoid FFmpeg (``None`` when
    there is nothing to be gained).

    The one thing that must never happen here is claiming "no FFmpeg" when
    FFmpeg is in fact running. ``passthrough_possible`` answers a question about
    the *source*, and it is the field that reads as "no FFmpeg needed" in
    ``radioconv probe`` - but forwarding an AAC station cannot produce the MP3
    that ``output_codec = "mp3"`` asks for, so the two answers differ exactly
    when a user most needs telling. ``passthrough_engine`` is the second answer,
    and it is the one this page reports.
    """
    if not result.get("ok"):
        return {
            "value": "not checked",
            "verdict": f"Could not check this stream: {result.get('error') or 'unknown error'}.",
            "state": "bad",
            "kind": None,
        }

    kind = result.get("passthrough_kind")
    output = settings.effective_output_codec
    if result.get("passthrough_engine") and kind:
        return {
            "value": f"pass-through ({kind})",
            "verdict": (
                "FFmpeg is not needed: the station's own audio is forwarded "
                f"untouched as {str(kind).upper()}. That is cheaper and better "
                "quality than re-encoding it."
            ),
            "state": "good",
            "kind": None,
        }
    if not settings.ffmpeg_enabled:
        # No second engine exists here, so an unfavourable verdict is the final
        # answer rather than a suggestion. Saying "FFmpeg re-encodes it" would be
        # actively wrong, and suggesting a `?codec=` URL would be a dead end -
        # that URL is what every request already is.
        return {
            "value": "cannot be played (ffmpeg disabled)",
            "verdict": (
                "This stream needs re-encoding and FFmpeg is disabled on this "
                f"installation: {_why_not_forwarded(result)}. Enable FFmpeg "
                "(remove --no-ffmpeg, or set ffmpeg_enabled = true) to play it."
            ),
            "state": "bad",
            "kind": None,
        }
    if kind:
        return {
            "value": f"transcode (ffmpeg, {kind} to {output})",
            "verdict": (
                f"The station's audio could be forwarded as {str(kind).upper()}, but "
                f"this service is set to output {output}, so FFmpeg re-encodes it. "
                "The URL below asks for the source codec instead, which removes "
                "FFmpeg from the picture entirely."
            ),
            "state": "warn",
            "kind": kind,
        }
    return {
        "value": f"transcode (ffmpeg to {output})",
        "verdict": (
            f"FFmpeg has to be used for this stream ({_why_not_forwarded(result)}), "
            f"so the audio is converted to {output}."
        ),
        "state": "info",
        "kind": None,
    }


def _why_not_forwarded(result: Dict[str, Any]) -> str:
    """One clause explaining why the FFmpeg-free path cannot take this stream."""
    container = result.get("container")
    if container == "mp4":
        return "the audio is packaged in fMP4, which has to be demuxed"
    if container == "ts":
        return "its transport stream carries no MP3 or ADTS AAC audio"
    return "the container was not recognised"


def _probe_rows(result: Dict[str, Any], engine: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """The facts from ``hls.inspect()`` as label/value rows for the page.

    Rows are omitted rather than shown empty: a value we do not have is not
    evidence of anything, and a table of "unknown" reads as a broken page. The
    engine verdict is a row of its own because it is the question the page
    exists to answer, even though it is also spelled out in a sentence above.
    """
    rows: List[Dict[str, str]] = []
    if not result.get("ok"):
        return rows

    def add(label: str, value: str, tip: str) -> None:
        if value:
            rows.append({"label": label, "value": value, "tip": tip})

    if engine:
        add(
            "Engine",
            engine["value"],
            "How the audio will be produced: 'pass-through' forwards the station's "
            "own audio without decoding it, 'transcode' means FFmpeg decodes and "
            "re-encodes it.",
        )
    add(
        "Container",
        _container(result),
        "What the segment bytes actually are, measured from the first segment "
        "rather than guessed from the URL: 'ts' is MPEG transport stream, 'adts' is "
        "raw AAC frames (what an .aac segment is), 'mp4' is fMP4.",
    )
    add(
        "Playlist",
        _playlist(result),
        "How the playlist resolved. A master playlist lists several renditions and "
        "this service picks one; a media playlist is already the audio itself.",
    )
    add(
        "Segments",
        _segments(result),
        "How many segments are in the playlist right now and how long each one is. "
        "A live station keeps a rolling window, so this is a snapshot.",
    )
    add(
        "Encrypted",
        "yes (AES-128)" if result.get("encrypted") else "no",
        "Whether the segments are encrypted. When they are, this service decrypts "
        "them before anything else sees the audio.",
    )
    if result.get("has_init_segment"):
        add(
            "Init segment",
            "yes (fMP4)",
            "The stream carries an EXT-X-MAP initialisation segment, which is what "
            "fMP4 rather than MPEG-TS looks like.",
        )
    add(
        "Source audio",
        _audio(result.get("audio") or {}),
        "The real codec inside the segments, read by FFmpeg from the downloaded "
        "audio - not inferred from the file extension.",
    )
    add(
        "TS streams",
        _ts_streams(result.get("ts_streams")),
        "The elementary streams found inside the transport stream, with their "
        "packet identifiers, and what each one could be played as.",
    )
    return rows


def _container(result: Dict[str, Any]) -> str:
    container = str(result.get("container") or "")
    segment = str(result.get("segment_container") or "")
    if container and segment and container != segment:
        # Both are reported when they disagree, because that disagreement is
        # itself the diagnosis - it is what the "container guessed from the URL"
        # bug looked like from the outside.
        return f"{container} (segment: {segment})"
    return container or segment


def _playlist(result: Dict[str, Any]) -> str:
    if not result.get("is_master"):
        return "media playlist"
    variants = int(result.get("variants_considered") or 0)
    parts = [f"master, {variants} variant{'' if variants == 1 else 's'}"]
    chosen = _kbits(result.get("variant_bandwidth"))
    if chosen:
        parts.append(f"chose {chosen}")
    return ", ".join(parts)


def _segments(result: Dict[str, Any]) -> str:
    count = result.get("segment_count")
    if not count:
        return ""
    target = result.get("target_duration")
    if isinstance(target, (int, float)) and target:
        return f"{count} x ~{target:g}s"
    return str(count)


def _audio(audio: Dict[str, Any]) -> str:
    """``AAC LC, 48 kHz, 2 ch, 192 kbit/s`` - or why it could not be read."""
    codec = audio.get("codec")
    if not codec:
        # `probe_bytes` reports an error when no demuxer recognised the data.
        # Saying so is more useful than an empty cell: it is the answer to
        # "why does this station not play?".
        return "not recognised"
    # Codec and profile are one name ("AAC LC"), not two facts - a comma between
    # them reads as two codecs in a list. Upper-case because the codec is a name
    # rather than a value: ffmpeg says "aac", and a table of lower-case
    # abbreviations beside "Engine" and "Container" reads like log output.
    codec_name = str(codec).upper()
    name = " ".join(str(part).strip() for part in (codec_name, audio.get("profile") or "") if part)
    bits = [_khz(audio.get("sample_rate"))]
    channels = audio.get("channels")
    if channels:
        bits.append(f"{channels} ch")
    bits.append(_kbits(audio.get("bitrate")))
    return ", ".join([name] + [bit for bit in bits if bit])


def _ts_streams(streams: Any) -> str:
    if not streams:
        return ""
    described = []
    for stream in streams:
        text = f"0x{int(stream['type']):02x} pid 0x{int(stream['pid']):x}"
        playable = stream.get("playable_as")
        described.append(f"{text} -> {playable}" if playable else f"{text} (needs ffmpeg)")
    return ", ".join(described)


def _kbits(value: Any) -> str:
    """A bit rate as a person reads it, or ``""`` when there is not one."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return f"{number // 1000} kbit/s" if number >= 1000 else f"{number} bit/s"


def _khz(value: Any) -> str:
    """A sample rate in kHz, or ``""``."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return f"{number / 1000:g} kHz"


def dashboard_page(settings: Settings, request: Request) -> HTMLResponse:
    """``GET /dashboard`` - a live view of the numbers in ``/metrics.json``.

    The figures themselves are fetched by the browser; what is rendered here is
    the shell and the configuration, which cannot change while we are running.
    """
    return _page(
        "dashboard.html",
        current="dashboard",
        base=base_url(settings, request),
        config=_config(settings),
    )


def _config(settings: Settings) -> Dict[str, str]:
    """The few settings worth reading *beside* live numbers.

    Not the whole configuration any more: that table could only be read, and a
    setting you cannot change is not much use while you are watching a graph. It
    lives on the settings page, where it can be edited. What stays here is what a
    live figure needs in order to mean anything - how the audio is being produced,
    and the limits that make a "refused as full" count or a silent-station row
    explicable.

    Built from the **effective** values, because on an installation with FFmpeg
    disabled the configured ones describe a conversion that never happens. A
    dashboard saying "Output codec: mp3" while every byte it handles is the
    station's own AAC is worse than one that says nothing: it is the figure
    somebody would quote when the audio sounds wrong.
    """
    if not settings.ffmpeg_enabled:
        # Nothing is encoded, so `bitrate` is only what a radio is *told*.
        output = (
            f"the station's own (FFmpeg is off; radios are told {settings.bitrate // 1000} kbit/s)"
        )
    elif settings.output_codec == "auto":
        output = f"the source codec, else {settings.bitrate // 1000} kbit/s"
    else:
        output = f"{settings.output_codec} @ {settings.bitrate // 1000} kbit/s"
    return {
        "FFmpeg": "enabled" if settings.ffmpeg_enabled else "disabled (audio is forwarded)",
        "Output": output,
        "Stations allowed": str(settings.max_sessions),
        "Radios per station": str(settings.max_listeners_per_session),
        "Restart a silent station after": f"{settings.stall_timeout:g}s",
        "Drop a dead radio after": (
            f"{settings.listener_write_timeout:g}s"
            if settings.listener_write_timeout
            else "never (left to the kernel)"
        ),
    }


# ------------------------------------------------------------- settings page
#: One editable field on the settings page.
#:
#: The list below is the *whole* definition of the page: the template renders
#: from it, the POST handler parses from it, and the tests assert that every name
#: in it is a real :class:`Settings` field. Three copies of that list would drift
#: the first time a setting was renamed, and the failure would be silent - a form
#: input that is rendered, submitted, and ignored.
#:
#: What is *not* here is as deliberate as what is: ``host``, ``port``,
#: ``log_level`` and the CORS origins are fixed when the process starts (uvicorn
#: is already listening, and the middleware is already built), so showing them as
#: editable would be a promise the service cannot keep. They are listed as
#: read-only instead, with the reason.
@dataclass(frozen=True)
class Field:
    name: str
    label: str
    group: str
    kind: str  # "int" | "float" | "bool" | "choice" | "text"
    help: str
    choices: Tuple[str, ...] = ()
    unit: str = ""
    optional: bool = False
    #: True for a value that only reaches *new* pipelines - the page offers to
    #: restart the running stations so a change is not left half-applied.
    new_streams_only: bool = False


#: What a browser actually submits for a ticked checkbox. Every radio button,
#: tick box and single-character `<select>` has its own idea of "yes"; these are
#: the strings that arrive in practice, and the parser and the renderer must
#: agree on them or a box re-renders as unticked after a successful save.
_TRUE = ("1", "on", "true", "yes")

SETTINGS_FIELDS: Tuple[Field, ...] = (
    Field(
        "ffmpeg_enabled",
        "FFmpeg transcode",
        "Encoding",
        "bool",
        "Leave on to allow re-encoding, which is what lets any station play. "
        "Turning it off makes the station's own audio the only possible output: "
        "no child process, almost no CPU, and a station that needs converting "
        "can no longer be played at all.",
        new_streams_only=True,
    ),
    Field(
        "bitrate",
        "Bitrate",
        "Encoding",
        "int",
        "Target bitrate for re-encoded audio, in bit/s (8000-320000). Ignored "
        "when a stream is forwarded, and when FFmpeg is off.",
        unit="bit/s",
        new_streams_only=True,
    ),
    Field(
        "output_codec",
        "Output codec",
        "Encoding",
        "choice",
        "mp3 plays on every radio. adts forwards the station's AAC untouched - "
        "no re-encode, best quality - if your radio decodes AAC. auto forwards "
        "whatever the station already sends.",
        choices=("mp3", "adts", "auto"),
        new_streams_only=True,
    ),
    Field(
        "prefer_passthrough",
        "Forward when possible",
        "Encoding",
        "bool",
        "Forward the source untouched for any stream that is already playable, "
        "instead of decoding and re-encoding it. This is what makes modern HLS "
        "radio cheap on a Pi; off means FFmpeg always runs.",
        new_streams_only=True,
    ),
    Field(
        "channels",
        "Channels",
        "Encoding",
        "int",
        "Output channel count for re-encoded audio. 2 keeps stereo.",
        new_streams_only=True,
    ),
    Field(
        "sample_rate",
        "Sample rate",
        "Encoding",
        "choice-int",
        "Resample re-encoded audio. Leave empty to keep the source rate, which "
        "avoids a resampling step.",
        choices=(
            "",
            "8000",
            "11025",
            "12000",
            "16000",
            "22050",
            "24000",
            "32000",
            "44100",
            "48000",
        ),
        unit="Hz",
        optional=True,
        new_streams_only=True,
    ),
    Field(
        "mime_override",
        "Content-Type override",
        "Encoding",
        "text",
        "Force the Content-Type sent to radios, for firmware that insists on a "
        "particular spelling (e.g. audio/mpeg3 or audio/aacp). Empty = derived "
        "from the output codec.",
        optional=True,
        new_streams_only=True,
    ),
    Field(
        "max_sessions",
        "Stations at once",
        "Limits",
        "int",
        "How many different stations can be converted simultaneously. Each one "
        "is a pipeline: cheap for a forwarded stream, roughly a core for a "
        "re-encode.",
    ),
    Field(
        "max_listeners_per_session",
        "Radios per station",
        "Limits",
        "int",
        "Listeners allowed to share one station's pipeline.",
    ),
    Field(
        "share_sessions",
        "Share one pipeline per station",
        "Limits",
        "bool",
        "Radios on the same URL share a single decode. Off gives every listener "
        "its own pipeline, which multiplies the cost.",
    ),
    Field(
        "chunk_size",
        "Audio chunk size",
        "Streaming",
        "int",
        "Bytes per queue item - the unit the per-listener jitter buffer is counted in.",
        unit="bytes",
        new_streams_only=True,
    ),
    Field(
        "queue_chunks",
        "Jitter buffer",
        "Streaming",
        "int",
        "Chunks held per listener. 128 x 8 KiB is about 43 seconds of 192 "
        "kbit/s audio, which has to cover the gap between one HLS segment and "
        "the next. Too small is heard as stutter.",
        unit="chunks",
        new_streams_only=True,
    ),
    Field(
        "preroll_seconds",
        "Audio ready on connect",
        "Streaming",
        "float",
        "Recent audio handed to a radio the moment it connects, in seconds. 0 "
        "makes every listener start in silence.",
        unit="s",
        new_streams_only=True,
    ),
    Field(
        "handoff_overlap_seconds",
        "Handoff overlap",
        "Streaming",
        "float",
        "How much already-delivered audio a rejoining radio may be sent again, "
        "in seconds. Bridges the gap when a radio reconnects; too much is heard "
        "as a repeat.",
        unit="s",
        new_streams_only=True,
    ),
    Field(
        "pace_output",
        "Pace the output",
        "Streaming",
        "bool",
        "Hand each radio its audio at the rate it plays, rather than as fast as "
        "it is produced. Only turn this off to diagnose the pacer itself: radio "
        "buffers underrun without it.",
        new_streams_only=True,
    ),
    Field(
        "initial_burst_seconds",
        "Initial burst",
        "Streaming",
        "float",
        "Seconds of audio released immediately at the start of a connection, so "
        "a radio can fill its own buffer.",
        unit="s",
        new_streams_only=True,
    ),
    Field(
        "icy_song_metadata",
        "Song titles",
        "Streaming",
        "bool",
        "Send now-playing titles as ICY metadata, to radios that ask for it. "
        "This is what puts the song name on the radio's display.",
        new_streams_only=True,
    ),
    Field(
        "linger_seconds",
        "Keep a station warm",
        "Streaming",
        "float",
        "Keep a pipeline running this long after the last radio leaves, so a "
        "radio that reconnects does not pay a cold start.",
        unit="s",
    ),
    Field(
        "stall_timeout",
        "Restart a silent station after",
        "Streaming",
        "float",
        "No audio for this long means the pipeline is killed and rebuilt, and "
        "the station counts as silent (the service reports degraded).",
        unit="s",
    ),
    Field(
        "listener_write_timeout",
        "Drop a dead radio after",
        "Streaming",
        "float",
        "How long a single write to a radio may block before that listener is "
        "dropped. A radio that loses power sends no goodbye: without this the "
        "kernel holds the connection for about 15 minutes. 0 disables it.",
        unit="s",
        new_streams_only=True,
    ),
    Field(
        "public_host",
        "Address shown to radios",
        "Management",
        "text",
        "host:port used when the page prints a URL for a radio. Empty = derived "
        "from the request, which is right unless the service is behind NAT.",
        optional=True,
    ),
    Field(
        "allow_ui_settings",
        "Allow changes from this page",
        "Management",
        "bool",
        "Turn off to make this page read-only. Worth doing if the service is "
        "reachable from somewhere you do not control, because it has no login.",
    ),
)

#: Settings that exist but cannot be changed while the process runs, and why.
#: Listed so the page answers the question rather than leaving a reader to
#: wonder whether the setting is missing or hidden.
READ_ONLY_SETTINGS: Tuple[Tuple[str, str], ...] = (
    ("host", "The listening address is bound at startup."),
    ("port", "The port is bound at startup."),
    ("log_level", "Set by --log-level, or in the config file."),
    ("ffmpeg_path", "Set by --ffmpeg, or in the config file."),
    ("cors_origins", "The CORS middleware is built once, at startup."),
)

#: The order the groups appear in, and the sentence above each one.
SETTINGS_GROUPS: Tuple[Tuple[str, str], ...] = (
    (
        "Encoding",
        "What the audio becomes. These reach a station when its pipeline next "
        "starts - tick 'restart the stations playing now' below to apply them "
        "immediately.",
    ),
    ("Limits", "How much the service is allowed to do at once."),
    ("Streaming", "How the output is shaped for a radio."),
    ("Management", "The service's own administrative settings."),
)


def settings_form(
    settings: Settings, submitted: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """The editable fields as groups, ready to render.

    Values are rendered as strings so the template has no formatting of its own
    to get wrong, and a boolean carries ``checked`` rather than ``"True"`` so
    the template is not parsing Python's repr of a truth value.

    *submitted*, when given, is the raw form the reader just sent. It wins over
    the settings, so a rejected submit comes back showing what was typed instead
    of silently reverting every field in the form. Retyping twenty values
    because one was wrong is how a settings page teaches people to stop using
    it; showing the offending field in red, with the rest intact, is the whole
    point of validating per field.
    """
    groups: List[Dict[str, Any]] = []
    for group_name, blurb in SETTINGS_GROUPS:
        fields: List[Dict[str, Any]] = []
        for field in SETTINGS_FIELDS:
            if field.group != group_name:
                continue
            raw = getattr(settings, field.name, None)
            if field.kind == "bool":
                value = ""
                if submitted is not None and field.name in submitted:
                    # An unchecked box is simply absent, so "was it submitted"
                    # and "was it ticked" are the same bit for a checkbox.
                    checked = str(submitted[field.name]).lower() in _TRUE
                else:
                    checked = bool(raw)
            else:
                value = "" if raw is None else str(raw)
                if submitted is not None and field.name in submitted:
                    value = str(submitted[field.name])
                checked = False
            fields.append(
                {
                    "name": field.name,
                    "label": field.label,
                    "help": field.help,
                    "kind": field.kind,
                    "choices": field.choices,
                    "unit": field.unit,
                    "optional": field.optional,
                    "new_streams_only": field.new_streams_only,
                    "value": value,
                    "checked": checked,
                }
            )
        if fields:
            groups.append({"name": group_name, "blurb": blurb, "fields": fields})
    return groups


def parse_settings_form(form: Any) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Turn a submitted form into ``(patch, errors)``.

    Empty means two different things and the difference is the setting's, not the
    browser's: for an optional field it means "unset", and for a required one it
    means "not answered", which is an error rather than a silent revert. Anything
    else would let a mangled submit quietly reset a value to its empty form.

    ``errors`` is ``{field_name: message}``, so the page can put the message next
    to the input that caused it instead of in a list at the top.
    """
    patch: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for field in SETTINGS_FIELDS:
        key = field.name
        if field.kind == "bool":
            # An unchecked checkbox is absent from a form post, which is why
            # every boolean is enumerated here rather than only the present ones.
            patch[key] = str(form.get(key, "")).lower() in _TRUE
            continue

        raw = str(form.get(key, "")).strip()
        if raw == "":
            if field.optional:
                patch[key] = None
            else:
                errors[key] = "Required."
            continue
        if field.kind in ("choice", "choice-int"):
            if field.choices and raw not in field.choices:
                errors[key] = f"Must be one of {', '.join(c for c in field.choices if c)}."
                continue
            patch[key] = int(raw) if field.kind == "choice-int" else raw
            continue
        if field.kind == "int":
            try:
                patch[key] = int(raw)
            except ValueError:
                errors[key] = "Must be a whole number."
            continue
        if field.kind == "float":
            try:
                patch[key] = float(raw)
            except ValueError:
                errors[key] = "Must be a number."
            continue
        patch[key] = raw
    return patch, errors


def settings_page(
    settings: Settings,
    request: Request,
    *,
    message: str = "",
    problems: Optional[Dict[str, str]] = None,
    warnings: Optional[List[str]] = None,
    editable: Optional[bool] = None,
    saved_to: str = "",
    restarted: int = 0,
    submitted: Optional[Dict[str, Any]] = None,
) -> HTMLResponse:
    """``GET``/``POST /settings`` - the same markup for both.

    Rendered directly after a POST rather than redirecting: the page is the
    result of the submission, and a redirect would discard the per-field error
    messages that are the only reason to render it again. The trade is that a
    browser reload offers to re-send the form, which is honest for an operation
    that sets values rather than accumulating them.
    """
    may_edit = settings.allow_ui_settings if editable is None else editable
    return _page(
        "settings.html",
        current="settings",
        base=base_url(settings, request),
        groups=settings_form(settings, submitted),
        read_only=[(name, why) for name, why in READ_ONLY_SETTINGS],
        read_only_values={
            name: _shown(getattr(settings, name, None)) for name, _ in READ_ONLY_SETTINGS
        },
        editable=may_edit,
        message=message,
        problems=problems or {},
        warnings=warnings or [],
        saved_to=saved_to,
        restarted=restarted,
        config_path=str(ui_settings_path()),
    )


def _shown(value: Any) -> str:
    """A setting as one short line, for the read-only table."""
    if value is None:
        return "(not set)"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value == "":
        return "(empty)"
    return str(value)
