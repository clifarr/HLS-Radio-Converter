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
from .config import Settings

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

    Keys are the settings page's own labels (see :func:`_field_label`), because
    the two pages quote the same figures: a row called "Stations allowed" here
    and "Stations at once" there is one setting wearing two names, and the reader
    who goes looking for the second on the first page finds neither.

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
        _field_label("ffmpeg_enabled"): (
            "enabled" if settings.ffmpeg_enabled else "disabled (audio is forwarded)"
        ),
        "Output": output,
        _field_label("max_sessions"): str(settings.max_sessions),
        _field_label("max_listeners_per_session"): str(settings.max_listeners_per_session),
        _field_label("stall_timeout"): f"{settings.stall_timeout:g}s",
        _field_label("listener_write_timeout"): (
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
    """One editable setting, and the *only* place its wording lives.

    ``label`` is the short name the page shows; ``help`` is one sentence, because
    a settings form is read while looking for one thing, and a paragraph per
    field turns a page of twenty into something nobody scans. What needs more
    than a sentence belongs in the group blurb (when a value applies) or in
    :func:`engine_summary` (what a combination of settings adds up to), both of
    which are stated once for a group rather than repeated per field.
    """

    name: str
    label: str
    group: str
    kind: str  # "int" | "float" | "bool" | "choice" | "text"
    help: str
    choices: Tuple[str, ...] = ()
    unit: str = ""
    optional: bool = False


#: What a browser actually submits for a ticked checkbox. Every radio button,
#: tick box and single-character `<select>` has its own idea of "yes"; these are
#: the strings that arrive in practice, and the parser and the renderer must
#: agree on them or a box re-renders as unticked after a successful save.
_TRUE = ("1", "on", "true", "yes")

SETTINGS_FIELDS: Tuple[Field, ...] = (
    Field(
        "ffmpeg_enabled",
        "FFmpeg",
        "Encoding",
        "bool",
        "On: converting is possible. Off: only stations a radio can already play.",
    ),
    Field(
        "bitrate",
        "Bitrate",
        "Encoding",
        "int",
        "Converted stations only. A forwarded station keeps its own rate.",
        unit="bit/s",
    ),
    Field(
        "output_codec",
        "Output codec",
        "Encoding",
        "choice",
        "What a converted station is encoded to. mp3 plays anywhere; adts keeps AAC "
        "as AAC; auto keeps the station's own codec when it can.",
        choices=("mp3", "adts", "auto"),
    ),
    Field(
        "prefer_passthrough",
        "Prefer forwarding",
        "Encoding",
        "bool",
        "Forward audio that already plays instead of converting it. No effect when FFmpeg is off.",
    ),
    Field(
        "channels",
        "Channels",
        "Encoding",
        "int",
        "Converted stations only. 2 is stereo, 1 is mono.",
    ),
    Field(
        "sample_rate",
        "Sample rate",
        "Encoding",
        "choice-int",
        "Converted stations only. Empty keeps the source rate.",
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
    ),
    Field(
        "mime_override",
        "Content-Type",
        "Encoding",
        "text",
        "Header sent to radios, e.g. audio/aacp. Empty follows the output codec.",
        optional=True,
    ),
    Field(
        "max_sessions",
        "Stations at once",
        "Limits",
        "int",
        "Different stations at the same time. Forwarding one is nearly free.",
    ),
    Field(
        "max_listeners_per_session",
        "Radios per station",
        "Limits",
        "int",
        "Listeners that may share one station's pipeline.",
    ),
    Field(
        "share_sessions",
        "Share one pipeline",
        "Limits",
        "bool",
        "Radios on one URL share a decode. Off multiplies the cost.",
    ),
    Field(
        "chunk_size",
        "Chunk size",
        "Streaming",
        "int",
        "Bytes per queue item, the unit the jitter buffer counts in.",
        unit="bytes",
    ),
    Field(
        "queue_chunks",
        "Jitter buffer",
        "Streaming",
        "int",
        "Chunks buffered per radio. Too small is heard as stutter.",
        unit="chunks",
    ),
    Field(
        "preroll_seconds",
        "Preroll",
        "Streaming",
        "float",
        "Seconds of recent audio a radio gets as it connects; 0 starts silent.",
        unit="s",
    ),
    Field(
        "handoff_overlap_seconds",
        "Rejoin overlap",
        "Streaming",
        "float",
        "Audio re-sent to a rejoining radio. Too much is heard as a repeat.",
        unit="s",
    ),
    Field(
        "pace_output",
        "Pace output",
        "Streaming",
        "bool",
        "Send at playing speed. Off is only for diagnosing the pacer.",
    ),
    Field(
        "initial_burst_seconds",
        "Initial burst",
        "Streaming",
        "float",
        "Released at once on connect, so a radio can fill its own buffer.",
        unit="s",
    ),
    Field(
        "icy_song_metadata",
        "Song titles",
        "Streaming",
        "bool",
        "Send titles as ICY metadata - this is what shows on the display.",
    ),
    Field(
        "linger_seconds",
        "Keep warm",
        "Streaming",
        "float",
        "Keep a station running this long after its last radio leaves.",
        unit="s",
    ),
    Field(
        "stall_timeout",
        "Silence timeout",
        "Streaming",
        "float",
        "Rebuild a station that has produced no audio for this long.",
        unit="s",
    ),
    Field(
        "listener_write_timeout",
        "Dead radio timeout",
        "Streaming",
        "float",
        "Drop a radio whose socket blocks this long. 0 never drops it.",
        unit="s",
    ),
    Field(
        "public_host",
        "Public address",
        "Management",
        "text",
        "host:port used in radio URLs. Empty uses the request's own address.",
        optional=True,
    ),
    Field(
        "allow_ui_settings",
        "Allow changes here",
        "Management",
        "bool",
        "Off makes this page read-only. There is no login.",
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


def _field_label(name: str) -> str:
    """The settings page's label for a setting, so the two pages agree.

    The dashboard quotes a few settings beside live figures; the settings page is
    where they are changed. One setting under two names ("Stations allowed" on
    one page, "Stations at once" on the other) is a reader's problem, and it
    happened - the dashboard kept the pre-2.4.1 wording after the page was
    shortened. Looking the name up rather than restating it makes the drift
    impossible; a name that is not on the settings page raises here rather than
    rendering a table with a row nobody can act on.
    """
    for field in SETTINGS_FIELDS:
        if field.name == name:
            return field.label
    raise KeyError(f"{name} is not a settings-page field")


#: The order the groups appear in, and the sentence above each one.
#:
#: "When does this take effect?" is answered once per *group*, because it is a
#: property of the group: a session copies its settings when it is built, so
#: anything a session owns waits for a restart, while the limits are re-read for
#: every radio that connects. The old wording said the Streaming timeouts applied
#: immediately, which two of the three do not - they are captured per connection,
#: like the rest of a session's behaviour.
SETTINGS_GROUPS: Tuple[Tuple[str, str], ...] = (
    (
        "Encoding",
        "Two engines. Forwarding plays a station exactly as it arrives - its own "
        "codec and bitrate, no FFmpeg. Converting re-encodes it at the bitrate, "
        "channels and sample rate here. Applies when a station restarts; "
        "?codec=adts and ?br=128k override it for one radio.",
    ),
    (
        "Limits",
        "How much the service may do at once. A change applies to the next radio that connects.",
    ),
    (
        "Streaming",
        "How the output is shaped for a radio. Applies when a station restarts, or "
        "to a radio that connects afterwards.",
    ),
    ("Management", "The service's own administration. Applies immediately."),
)


def engine_summary(settings: Settings) -> str:
    """One sentence for what the engine settings *add up to*.

    "May this installation run FFmpeg?" and "which engine wins a tie?" are two
    different questions - one is a property of the machine, the other a
    priority - but a form shows settings as a flat list, and read that way the
    pair looks like one control and its negation, stated twice. The honest fix
    is not a longer label: it is to print the result, so the reader can see the
    combination they have chosen instead of assembling it in their head.

    The result is stated per *engine*, not per codec, and each sentence names
    where the bitrate applies, because that is the question the fields raise
    and cannot answer themselves: a forwarded station is played at its own rate,
    so `bitrate` does nothing to it. Naming "192 kbit/s" only for the converted
    path - and never on the forwarding-only installation, where no conversion
    can happen - is what makes the sentence trustworthy rather than decorative.

    Every branch is reachable, and refuses to describe an engine that cannot
    run: on an installation with FFmpeg off there is nothing to prefer, and the
    sentence says so rather than repeating the checkbox above it.
    """
    rate = f"{settings.bitrate // 1000} kbit/s"
    if not settings.ffmpeg_enabled:
        return (
            "In short: forwarding only. Every station plays exactly as it arrives, at "
            "its own bitrate; one that would have to be converted cannot be played, "
            "and says so instead of going silent."
        )
    if not settings.prefer_passthrough:
        # "Every station is converted", with no exception, and that is exact: the
        # source is only classified when forwarding is enabled, so with this
        # switch off every stream is decoded and encoded - AAC to AAC included.
        target = "AAC" if settings.output_codec == "adts" else "MP3"
        return f"In short: no forwarding. Every station is converted to {target} at {rate}."
    if settings.output_codec == "mp3":
        return (
            "In short: MP3 stations are forwarded at their own bitrate; AAC stations "
            f"are converted to MP3 at {rate}."
        )
    if settings.output_codec == "adts":
        return (
            "In short: AAC stations are forwarded at their own bitrate; MP3 stations "
            f"are converted to AAC at {rate}."
        )
    return (
        "In short: whatever already plays is forwarded at the station's own bitrate; "
        f"anything else is converted to MP3 at {rate}."
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
                if submitted is not None:
                    # A checkbox only exists in a form post when it is ticked, so
                    # "absent" *is* "unticked" - and that is only true when this
                    # render follows a post at all. Testing for the key instead
                    # was the bug: on a rejected submit a box the reader had just
                    # unticked came back ticked, because a missing key fell
                    # through to the stored value.
                    checked = str(submitted.get(field.name, "")).lower() in _TRUE
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
                    "value": value,
                    "checked": checked,
                }
            )
        if fields:
            groups.append(
                {
                    "name": group_name,
                    "blurb": blurb,
                    # Only the encoding group derives a result from more than one
                    # field. Rendered from the settings *in force*, not from the
                    # submitted form: after a rejected submit the useful thing to
                    # state is what the service is actually doing right now.
                    "note": engine_summary(settings) if group_name == "Encoding" else "",
                    "fields": fields,
                }
            )
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
        read_only=READ_ONLY_SETTINGS,
        read_only_values={
            name: _shown(getattr(settings, name, None)) for name, _ in READ_ONLY_SETTINGS
        },
        editable=may_edit,
        message=message,
        problems=problems or {},
        warnings=warnings or [],
        saved_to=saved_to,
        restarted=restarted,
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
