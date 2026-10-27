"""Configuration.

Everything is settable from (in increasing order of precedence):

1. the built-in defaults below,
2. a config file (TOML / JSON) passed with ``--config``,
3. environment variables prefixed ``RADIOCONV_`` (e.g. ``RADIOCONV_PORT=9000``),
4. command line flags.

This keeps ``radioconv serve`` a one-liner while still allowing a fully
declarative ``/etc/radioconv/config.toml`` on a Raspberry Pi.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import TypeAdapter, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from . import _toml

log = logging.getLogger(__name__)

OutputCodec = Literal["mp3", "adts", "auto"]
VariantSelection = Literal["highest", "lowest", "first"]

#: Values read from a config file, visible only for the duration of ``load()``.
#: A ContextVar (rather than a plain global) keeps this correct under threads
#: and async tasks and guarantees a later bare ``Settings()`` cannot inherit a
#: previous call's file values. The default is ``None`` rather than ``{}``
#: because a shared mutable default would be one ``.update()`` away from
#: leaking file values into unrelated calls.
_FILE_VALUES: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "radioconv_file_values", default=None
)


def _file_values_now() -> Dict[str, Any]:
    return _FILE_VALUES.get() or {}


#: Values saved from the settings page, for the duration of ``load()``.
#:
#: A second ContextVar rather than one merged dict, because the two have
#: different precedence (saved settings beat the config file) and because only
#: these may ever be *written* back. Keeping them apart is what stops a save from
#: rewriting somebody's annotated ``config.toml`` - which would delete every
#: comment in it.
_UI_VALUES: ContextVar[Optional[Dict[str, Any]]] = ContextVar("radioconv_ui_values", default=None)


def _ui_values_now() -> Dict[str, Any]:
    return _UI_VALUES.get() or {}


def ui_settings_path() -> Path:
    """Where the settings page saves what it is told.

    JSON rather than TOML because this file is *written* by the service as well
    as read, and hand-written TOML comments cannot survive a rewrite - so the
    file is the machine's, and the annotated example stays the human's.
    """
    return default_config_dir() / "ui-settings.json"


class _UIOverridesSource(PydanticBaseSettingsSource):
    """Settings saved from the web page, sitting *above* the config file.

    The order that matters is this one: a value somebody typed into the browser
    is a later decision than a value in ``config.toml``, so it wins. It must not
    win over ``RADIOCONV_*`` or a command-line flag, though - those are how
    systemd and the installer pin an installation, and a web page quietly
    defeating them would make the deployment's own configuration a lie. The
    consequence is surfaced rather than hidden: the page reports a field it saved
    but could not apply, naming the reason.
    """

    def __init__(self, settings_cls: type) -> None:
        super().__init__(settings_cls)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def get_field_value(self, field: FieldInfo, field_name: str) -> Tuple[Any, str, bool]:
        value = _ui_values_now().get(field_name)
        return value, field_name, False

    def __call__(self) -> Dict[str, Any]:
        return dict(_ui_values_now())


class _ConfigFileSource(PydanticBaseSettingsSource):
    """Settings source that sits *below* environment variables.

    That ordering - defaults < file < environment < explicit arguments - is
    what lets a Raspberry Pi keep a declarative ``config.toml`` while still
    being overridden by ``RADIOCONV_*`` for a one-off experiment.
    """

    def __init__(self, settings_cls: type) -> None:
        super().__init__(settings_cls)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def get_field_value(self, field: FieldInfo, field_name: str) -> Tuple[Any, str, bool]:
        value = _file_values_now().get(field_name)
        return value, field_name, False

    def __call__(self) -> Dict[str, Any]:
        return dict(_file_values_now())


@contextmanager
def _file_values(values: Dict[str, Any]) -> Generator[None, None, None]:
    token = _FILE_VALUES.set(values)
    try:
        yield
    finally:
        _FILE_VALUES.reset(token)


@contextmanager
def _ui_values(values: Dict[str, Any]) -> Generator[None, None, None]:
    token = _UI_VALUES.set(values)
    try:
        yield
    finally:
        _UI_VALUES.reset(token)


def default_config_dir() -> Path:
    """Where we look for ``config.toml``.

    There is deliberately no companion "stations" file: this project is meant to
    be config-free, and a radio is given a plain percent-encoded m3u8 URL.
    """
    env = os.environ.get("RADIOCONV_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "radioconv"


class SettingsStore:
    """The settings, in a form that can be changed while the service runs.

    Why a holder rather than passing :class:`Settings` around
    --------------------------------------------------------
    A ``Settings`` instance is immutable in practice: every closure and every
    long-lived object that captured one would keep the old values for ever, so
    "change a setting" could only mean "restart the process". This holds *which*
    settings are current and hands out the present one, so the objects that
    outlive a request - the session manager, the resource monitor, the route
    closures - follow a change instead of ignoring it.

    What a change does *not* do
    ---------------------------
    It does not rewrite a session that is already running. A ``StreamSession``
    copies the settings it was created with, deliberately: its chunk size, its
    codec and its queue depth describe a pipeline that exists, and swapping them
    underneath it would be a different stream spliced onto the current one. So a
    change reaches every *new* session, and the page offers to retire the
    running ones for anybody who wants it now.

    Why ``model_dump`` rather than the source dicts
    ----------------------------------------------
    Applying a patch starts from the values that are *in force*, not from the
    config file. Rebuilding from the file would silently discard whatever the
    environment, a command-line flag, or a previous save had decided - and the
    operator would watch a setting revert to something they never typed.
    """

    def __init__(self, settings: Settings, pinned: Optional[Dict[str, Any]] = None) -> None:
        self._settings = settings
        self._listeners: List[Callable[[Settings], None]] = []
        #: Values that must always win, because something outside this service
        #: decided them: a ``RADIOCONV_*`` variable, or a command-line flag (which
        #: the CLI passes in, since it is the only place that knows about them).
        #:
        #: Without this, a patch would override a pinned value *in memory* and the
        #: pin would return on the next restart - a change that appears to work
        #: and then silently reverts, which is worse than being refused. So a pin
        #: is re-applied over every patch, and ``apply`` reports the fields whose
        #: submitted value lost.
        self._pinned: Dict[str, Any] = dict(pinned or {})
        self._pinned.update(_env_pins())

    @property
    def current(self) -> Settings:
        return self._settings

    def pinned(self) -> Dict[str, Any]:
        """The fields whose value is decided outside this service."""
        return dict(self._pinned)

    def subscribe(self, callback: Callable[[Settings], None]) -> None:
        """Call *callback* with the new settings whenever they change."""
        self._listeners.append(callback)

    def apply(self, patch: Dict[str, Any]) -> Settings:
        """Merge *patch* into the current settings and validate the result.

        Raises ``pydantic.ValidationError`` for a value that is out of range or
        that contradicts another setting, in which case **nothing changes**: the
        replacement is built and validated in full before it is installed, so a
        rejected edit cannot leave the service half-configured.

        Pinned values are re-applied last, so what is in force after this call is
        the same thing the next process would load.
        """
        clean = {k: v for k, v in patch.items() if k in Settings.model_fields}
        if not clean:
            return self._settings
        merged = {**self._settings.model_dump(), **clean, **self._pinned}
        updated = Settings(**merged)
        self._settings = updated
        for callback in self._listeners:
            callback(updated)
        return updated

    def save(self, patch: Dict[str, Any]) -> Path:
        """Persist *patch* to :func:`ui_settings_path` so it survives a restart.

        Merged into whatever was saved before, so saving one field does not
        erase another that was set last week. Written atomically (temp file then
        rename) because a power cut mid-write would otherwise leave a truncated
        JSON file, and the service would then fail to start at all - a settings
        page that can brick the appliance is not worth having.
        """
        path = ui_settings_path()
        existing = read_ui_settings(path)
        existing.update({k: v for k, v in patch.items() if k in Settings.model_fields})
        # A key pydantic ignores, so the file explains itself to whoever finds
        # it in /etc and wonders why the value is not in config.toml.
        existing["__note__"] = (
            "Written by the radioconv settings page. These values override "
            "config.toml but are overridden by RADIOCONV_* and by command-line "
            "flags. Delete this file to fall back to config.toml."
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", "utf-8")
        temporary.replace(path)
        return path


def _env_pins() -> Dict[str, Any]:
    """Settings that a ``RADIOCONV_*`` variable is deciding, as real values.

    Coerced through the field's own annotation rather than read as a string,
    because a pin of ``"false"`` has to become ``False`` - and guessing at
    ``int()``/``bool()`` by hand is how a setting ends up pinned to the wrong
    type and then rejected by the very validators that were supposed to check it.

    A variable that does not parse is skipped rather than raised on: the service
    is already running with whatever ``Settings`` made of it, and a settings page
    that refuses to render because one environment variable is malformed would be
    a new way to take the appliance down.
    """
    pins: Dict[str, Any] = {}
    for name in Settings.model_fields:
        raw = os.environ.get(f"RADIOCONV_{name.upper()}")
        if raw is None:
            continue
        try:
            annotation = Settings.model_fields[name].annotation
            pins[name] = TypeAdapter(annotation).validate_python(raw)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            # Logged rather than swallowed, because "my bitrate will not change"
            # is exactly the symptom of a malformed variable, and a silent skip
            # would leave that unanswerable from the outside.
            log.debug("ignoring unusable RADIOCONV_%s: %s", name.upper(), exc)
            continue
    return pins


def read_ui_settings(path: Optional[Path] = None) -> Dict[str, Any]:
    """The values saved by the settings page, or ``{}``.

    Unreadable or malformed is ``{}`` rather than an exception, and deliberately:
    this file is *extra* configuration. The service must still start if somebody
    has corrupted it - losing a preference is recoverable, refusing to boot is
    not, and the alternative would be a settings page that can take the service
    down by writing something it later cannot read.
    """
    target = path or ui_settings_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("__")}


class Settings(BaseSettings):
    """Runtime settings."""

    model_config = SettingsConfigDict(
        env_prefix="RADIOCONV_",
        env_file=os.environ.get("RADIOCONV_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type,
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,  # explicit arguments win
            env_settings,  # then RADIOCONV_*
            dotenv_settings,  # then .env
            _UIOverridesSource(settings_cls),  # then what the settings page saved
            _ConfigFileSource(settings_cls),  # then config.toml / config.json
            file_secret_settings,  # then secrets files
        )

    # ---------------------------------------------------------------- network
    host: str = "0.0.0.0"
    port: int = 8123
    #: Advertised in the index page and ``icy-url`` headers.
    public_host: Optional[str] = None
    cors_origins: str = ""  # comma separated, empty = disabled

    # --------------------------------------------------------------- encoding
    #: Target bitrate in bits/s for the transcoded output.
    #:
    #: 192 kbit/s, deliberately: this is what the original single-file version
    #: shipped (`bitrate = 192000`) to preserve audio quality, and people set
    #: their radios up against it. Lowering this is a product decision with an
    #: audible cost, so it should never drift as a side effect of a refactor -
    #: `tests/test_config.py::test_output_quality_matches_the_original_project`
    #: pins it, and refuses to let the shipped example config disagree.
    bitrate: int = 192_000
    #: ``None`` keeps the source sample rate (cheaper: avoids a resample step).
    sample_rate: Optional[int] = None
    channels: int = 2
    #: ``mp3``  - always hand the radio MP3 (widest compatibility).
    #: ``adts`` - leave AAC as raw ADTS (no re-encode at all, best for Pi).
    #: ``auto`` - MP3 unless the source is already MP3.
    output_codec: OutputCodec = "mp3"
    #: When the source is already playable, forward it untouched (0% CPU).
    prefer_passthrough: bool = True

    # ---------------------------------------------------------------- ffmpeg
    #: Can this installation run FFmpeg at all?
    #:
    #: ``True`` (the default) is the historical behaviour: every stream can be
    #: re-encoded, so any station can be played whatever its source codec, and
    #: widest compatibility is what a Frontier-Silicon radio needs.
    #:
    #: ``False`` turns FFmpeg into an *optional* dependency. Many radios decode
    #: AAC, and for them a re-encode is pure waste - it costs CPU, memory and a
    #: child process per station to produce a stream that is worse than the one
    #: the station already broadcasts. With this off the service never starts a
    #: child process and only ever uses the FFmpeg-free path, which is a fraction
    #: of one core on a Pi.
    #:
    #: The consequence is not a degraded version of the same service, it is a
    #: different one: what can be played is decided entirely by the station. See
    #: :attr:`effective_output_codec` - ``output_codec`` is ignored, and every
    #: stream behaves as though its URL ended in ``?codec=auto``. A station whose
    #: audio cannot be forwarded (fMP4, or a transport stream with no MP3/ADTS
    #: audio) then cannot be played at all, and says so rather than playing
    #: silence.
    ffmpeg_enabled: bool = True
    #: Explicit ffmpeg binary; ``None`` = bundled wheel, then ``$PATH``.
    ffmpeg_path: Optional[str] = None
    #: Threads handed to ffmpeg. 1 is the right answer on a single-board
    #: computer - audio transcoding is not worth saturating the cores.
    ffmpeg_threads: int = 1
    #: Extra ``-i``-side ffmpeg args (advanced).
    ffmpeg_input_args: str = ""
    #: Extra output-side ffmpeg args (advanced), inserted before ``-f <fmt>``.
    ffmpeg_output_args: str = ""

    # ------------------------------------------------------------- HLS client
    #: Sent to every upstream CDN, so the URL must be the real repository -
    #: tests/test_metadata.py checks it against the canonical constant.
    user_agent: str = "radioconv/2.0 (+https://github.com/clifarr/HLS-Radio-Converter)"
    referer: Optional[str] = None
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    #: Total wall-clock budget for a single playlist fetch.
    playlist_timeout: float = 12.0
    segment_retries: int = 3
    #: Consecutive unrecoverable failures before failing over to the next URL.
    failover_after: int = 3
    max_playlist_bytes: int = 2 * 1024 * 1024
    #: Hard cap per segment - protects against a hostile/broken origin.
    max_segment_bytes: int = 16 * 1024 * 1024
    #: Honour ``#EXT-X-KEY:AES-128`` (most real-world streams use it).
    decrypt_segments: bool = True
    #: Skip TLS certificate verification (some radio CDNs are sloppy).
    insecure: bool = False
    #: ``highest`` quality variant wins. That is what you want on a stream that
    #: gets re-encoded to 192 kbit/s MP3 anyway.
    variant_selection: VariantSelection = "highest"

    # -------------------------------------------------------------- streaming
    chunk_size: int = 8192
    #: Bounded per-listener queue: the jitter buffer that absorbs the fact that
    #: HLS arrives in segments.
    #:
    #: 128 chunks * 8 KiB = 1 MiB, which is **~43 seconds** of 192 kbit/s audio
    #: per radio. It was 64 (512 KiB, ~21s), which was too tight to be a buffer
    #: at all: FFmpeg transcodes a whole segment faster than realtime, so the
    #: queue has to hold the burst that arrives while the listener is still
    #: playing the previous one. At 10-second segments, two segments in flight is
    #: 480 KiB - so the old value was within one segment of overflowing, and
    #: overflow means dropping audio for that listener.
    #:
    #: Cost is bounded and per-listener: 1 MiB * max_listeners_per_session.
    #: A Pi 3 with 921 MB and 8 listeners spends 8 MiB on this.
    queue_chunks: int = 128
    #: Hand a radio its audio at the stream's realtime rate instead of as fast as
    #: we can read it. See ``radioconv.api._Pacer``: without this, each segment
    #: arrives as a burst and the radio underruns in the gap between bursts,
    #: which is heard as the audio stopping for a second and resuming.
    #: Only turn this off to diagnose pacing itself.
    pace_output: bool = True
    #: Seconds of audio released without delay at the start of a connection, so a
    #: radio can fill its own buffer before the steady drip begins.
    initial_burst_seconds: float = 1.5
    #: Close a listener's connection when it has received **no audio for this
    #: long**, so the radio reconnects and re-anchors its decoder. 0 disables it.
    #:
    #: Why this exists, and why the value is 1.0
    #: -----------------------------------------
    #: The stream itself is not at fault, which is worth recording here so it is
    #: not re-investigated: during the fault the wire was measured perfect over
    #: 39 connections and 3.5 hours - no gaps in the TCP byte stream, every MP3
    #: frame intact, the ICY grid on its declared offset, and the radio
    #: acknowledging every byte. The defect is that the radio's decoder cannot
    #: recover from a gap in the audio it is fed, and only a new connection
    #: clears it.
    #:
    #: The radio does have a timeout of its own: freezing the server with
    #: ``SIGSTOP`` for 1.2 s, and separately for 2.0 s, was each followed by the
    #: radio reconnecting by itself with the audio clean. But that timeout only
    #: fires when it receives **nothing at all**. Microwave interference is not
    #: like that - TCP retransmits, so bytes keep trickling in and the radio
    #: never gives up, while its decoder starves and resynchronises into noise
    #: it cannot recover from. Measured on the wire: 21 retransmissions inside
    #: one 10-second window against a handful an hour otherwise, and an
    #: inter-segment gap of 1.492 s.
    #:
    #: So this watchdog watches something the radio does not: how long since
    #: this listener was handed a chunk, which covers the socket write as well
    #: as the queue and therefore sees the stall the moment the radio's receive
    #: window fills. The stalls it actually fired on were **1.80 s** and
    #: **2.27 s**. 1.0 s is below both and above ordinary scheduling jitter.
    stall_reconnect_seconds: float = 1.0
    #: Minimum gap between two forced reconnects of the same station.
    #:
    #: Without it a genuine upstream outage - where every listener is starved -
    #: would close and reopen every connection once a second, forever. That is
    #: the reconnect storm this project has already caused once: 6940
    #: connections in 89 seconds. The cooldown makes the watchdog act on an
    #: isolated stall while leaving a real outage to the pipeline's own recovery.
    #:
    #: It is also the ceiling on how long a radio can stay broken: the glitch
    #: clears on the next re-anchor, so this plus one poll and the radio's own
    #: reconnect is the worst case. 5 s keeps that under the 10 s that was asked
    #: for, and 12 re-anchors a minute is nowhere near a storm.
    stall_reconnect_cooldown_seconds: float = 5.0
    #: Re-anchor the listeners when the operating system reports this many
    #: retransmitted TCP segments within one poll. 0 disables it.
    #:
    #: Why the retransmission count and not a timer
    #: --------------------------------------------
    #: This is the signal that actually sees Wi-Fi interference. A radio's
    #: decoder cannot recover from damaged audio, and the damage arrives as lost
    #: packets that TCP resends - so what the radio receives is *late*, not
    #: missing. That is invisible from inside this process: measured during a
    #: microwave test, the kernel absorbed a **2.8 s** stall in its socket
    #: buffers and the application never saw a blocked write, so a server-side
    #: timing threshold never fired.
    #:
    #: The kernel counts what it had to send twice, and the numbers are far
    #: apart. Measured on one real connection: **47 retransmissions in 776 s**
    #: while healthy (0.06 per second), against **21 in a ten-second window**,
    #: and 27 (36 KB) inside a single stall, during interference.
    #:
    #: A radio also cannot be asked: measured while garbled, it acknowledged
    #: every byte at full rate for 776 s straight. It is reading the stream
    #: perfectly and decoding noise. So nothing above TCP can tell a working
    #: radio from a broken one, and the retransmission count is the only signal
    #: left.
    #:
    #: Detecting it needs no privileges, and is available on all three platforms:
    #: ``/proc/net/snmp`` on Linux, ``netstat -s`` on macOS, and the IP Helper
    #: API (``GetTcpStatisticsEx``) on Windows - a struct field there, not text,
    #: so it works whatever language the system is set to.
    #:
    #: Two thresholds, because one poll is the wrong unit
    #: ------------------------------------------------
    #: **A single poll cannot separate the two cases.** Measured on the Pi over
    #: 420 consecutive one-second polls on a quiet network: 415 polls at zero
    #: retransmissions, two at 1, and **three at 6** - so a healthy Wi-Fi link
    #: produces bursts of 6 in one poll. During interference the measured
    #: per-poll bursts were 3, 4, 5, 5, 5, 7 and 9.
    #:
    #: The two distributions **overlap** (healthy reaches 6, interference starts
    #: at 3), so no per-poll threshold separates them: a threshold of 3 fires
    #: on the healthy bursts, and a threshold of 7 or more misses the
    #: interference polls of 3, 4 and 5.
    #:
    #: What separates them is the total over a few seconds, because the damage
    #: from a microwave is spread across several polls rather than
    #: concentrated in one. Measured, healthy against interference:
    #:
    #: ==================  ==========================  =========================
    #: Window              Healthy (max observed)      Interference (observed)
    #: ==================  ==========================  =========================
    #: one poll            6                           9
    #: 5 s                 6                           -
    #: 10 s                **13**                      **21 - 29**
    #: ==================  ==========================  =========================
    #:
    #: (The healthy column is 1920 consecutive one-second polls: 1890 at zero,
    #: the rest 1-7 per poll, with a single ten-second total reaching 13. The
    #: interference column is the microwave tests, where the service's own peak
    #: window reached 29 and a packet capture showed 21.)
    #:
    #: So the watchdog sums the last ``retransmit_window_seconds`` of polls and
    #: compares the total with ``retransmit_window_segments``, which sit between
    #: the healthy maximum and the interference figure.
    #:
    #: The per-poll threshold stays high, at **10**: it is the "one enormous
    #: poll" case, which the window would dilute, and 10 is above every
    #: healthy poll ever measured. It is deliberately not the working trigger -
    #: the interference never produced a poll above 9, so a per-poll threshold
    #: of 10 would never fire at all. (It was the only trigger for one
    #: release; the short-burst microwave test is what proved that wrong.)
    retransmit_reconnect_segments: int = 10
    #: The length of the trailing window the retransmissions are summed over.
    #:
    #: Ten seconds, because that is the window the measurements were taken in
    #: and the one where the two distributions separate (healthy max 13,
    #: interference 21-29).
    retransmit_window_seconds: float = 10.0
    #: The sum of retransmissions over that window which counts as damage.
    #:
    #: **14**: just above the highest healthy ten-second total measured (13) and
    #: below every interference total (21-29). A shorter window would be
    #: noisier; a longer one would delay the repair, and the repair is what the
    #: radio is waiting for.
    #:
    #: The margin is one segment, which is thinner than it looks and has not
    #: false-fired: both window fires in the passing hardware test landed at
    #: exactly 14, on glitches the listener confirmed by ear. If spurious
    #: re-anchors ever appear on a clean network, raise this to 16-18 rather
    #: than lowering it - there is room up to 21.
    retransmit_window_segments: int = 14
    #: Re-anchor every listener of a station this often, whatever the network
    #: looks like. **Off by default**; set a number of seconds to enable it.
    #:
    #: Why it is off
    #: -------------
    #: It works, and it was shipped on by default in 2.3.1. On the real radio it
    #: is worse than the problem it solves: every fire costs audible audio, and
    #: the listener hears it every time. Verbatim, from the hardware test of
    #: 2.3.1 with `scheduled_reconnects` climbing every two minutes:
    #:
    #:     "every 2 minutes, I'm noticing the radio either skipping a few
    #:      seconds, or repeating a few seconds"
    #:
    #: Both halves are this mechanism, and neither is a bug in it. The re-anchor
    #: discards the listener's queued backlog (up to the 9.2 s reserve) so the
    #: disconnect is immediate - the radio resumes at the live edge, having never
    #: decoded that audio, which is heard as a **skip**. Then the fresh
    #: connection is primed with `handoff_overlap_seconds` (1.5 s) of audio that
    #: was already sent, to bridge the join - heard as a **repeat**.
    #:
    #: That trade is worth it when the radio is genuinely broken: it is already
    #: garbled, so a skip costs nothing. It is not worth it on a healthy stream,
    #: and a timer cannot tell the two apart.
    #:
    #: What is lost by leaving it off
    #: ------------------------------
    #: A wedged decoder that produced no evidence of damage stays wedged until
    #: the radio reconnects by itself - which it does every 20-60 s anyway
    #: (measured over hundreds of connections), so the exposure is bounded by
    #: the hardware either way. The case the timer actually covered was measured
    #: once: the radio stuck in permanent garble with **neither** trigger firing,
    #: while the delivered stream was verified perfect (555 frames, 0 bad syncs,
    #: clean `ffmpeg -err_detect explode`). Once the interference stops such a
    #: radio acknowledges every byte at full rate - identical to a healthy one -
    #: so no evidence-based threshold can catch it. Enable this if you see that
    #: failure and accept the periodic skip/repeat as the price.
    #:
    #: Valid values are 0 (off) or 30-3600 seconds.
    reanchor_interval_seconds: float = 0.0
    #: How often the retransmission count is sampled, in seconds.
    #:
    #: One second is one ``/proc/net/snmp`` read - on the order of 10 microseconds
    #: on a Pi - and it bounds detection latency. Sampling less often would let a
    #: whole stall pass between two reads.
    retransmit_poll_seconds: float = 1.0
    #: Concurrent pipelines. Each one is either an ffmpeg process or a very
    #: cheap pure-python demuxer.
    max_sessions: int = 4
    max_listeners_per_session: int = 8
    #: Share one upstream pipeline between identical station requests.
    share_sessions: bool = True
    #: Keep a pipeline warm for this long after the last radio disconnects, so
    #: a radio that reconnects (or a seek) does not pay a cold start.
    linger_seconds: float = 25.0
    #: Recent audio handed to a *newly connected* listener, in seconds.
    #:
    #: Without this a joining radio's buffer starts empty and it hears nothing
    #: until the pipeline next produces. That is a real wait, not a theoretical
    #: one: the encoder free-runs at roughly six times realtime, so it delivers a
    #: whole segment in a ~2 second burst and then blocks until the next segment
    #: arrives - a gap of six to eight seconds. A radio connecting inside that gap
    #: waited a measured **median of 8.08 seconds** before its first byte, and a
    #: cheap radio gives up long before that.
    #:
    #: Version 1 never showed this because its FFmpeg was throttled to realtime by
    #: back-pressure, so it was always producing. We removed the back-pressure
    #: deliberately (one slow radio must not stall a station) and nothing replaced
    #: the property it provided: *there is always audio available to hand over*.
    #: This is that replacement, and it costs a few hundred kilobytes per station.
    preroll_seconds: float = 10.0
    #: How far behind a reconnecting client's last-delivered position its rejoin
    #: may be primed, in seconds - the *handoff overlap*.
    #:
    #: A radio that re-GETs the stream at the exact production edge would be
    #: primed with nothing and then wait - up to a whole segment period - for
    #: the next burst, which on a 3-second-cadence station was measured as
    #: 0.5-2.1 s of starvation every few seconds (the reported "silent for a
    #: second or less"). The audio it had received but not yet played is
    #: discarded with its old socket on the handoff (the reported "skips a
    #: second or so"). Re-sending at most this much already-delivered audio
    #: bridges both: the rejoin always has an instant cushion, and the skipped
    #: tail is reclaimed. The replay is bounded to this value; 0 restores
    #: exact-resume joins, where an edge rejoin may wait for the next burst.
    handoff_overlap_seconds: float = 1.5
    #: Send now-playing song titles to listeners as ICY in-stream metadata
    #: (``icy-metaint`` blocks carrying ``StreamTitle='...'``), read from the
    #: source's in-band ID3 tags when it carries them. This is what puts the
    #: current song on an internet radio's display. Off = responses carry no
    #: metadata blocks at all.
    icy_song_metadata: bool = True
    #: No output bytes for this long => kill and restart the pipeline.
    stall_timeout: float = 30.0
    #: How long a single socket write to a radio may block before that listener
    #: is dropped as gone. 0 = no deadline.
    #:
    #: This is the only bound the application has over a radio that vanishes
    #: without saying goodbye - a power cut, a Wi-Fi dropout that never recovers,
    #: a router that reboots. Nothing is acknowledged, so the kernel retransmits
    #: until ``tcp_retries2`` gives up: fifteen attempts, **about fifteen
    #: minutes**. That figure is system-wide and ASGI exposes neither the socket
    #: nor ``TCP_USER_TIMEOUT``, so the application bounds its own write instead
    #: (``api._ListenerStream``). Until it did, a powered-off radio stayed in the
    #: listener list for a quarter of an hour while its `dropped` counter climbed
    #: at the audio rate.
    #:
    #: The default is deliberately generous. A congested Wi-Fi link absorbs
    #: measured **2.8 s** of stall inside the kernel before the application sees
    #: a blocked write at all, and a healthy radio on a bad link can pause for a
    #: few seconds more - 30 s is an order of magnitude above that, while still
    #: being thirty times faster than waiting for the kernel.
    listener_write_timeout: float = 30.0
    #: Delay before restarting a failed pipeline (exponential up to 8x).
    restart_backoff: float = 2.0

    # ---------------------------------------------------------- housekeeping
    #: How often CPU and memory are sampled for the dashboard. Deliberately much
    #: faster than ``memory_check_interval``: that interval is how long a bad
    #: reading takes to become a restart decision, but sampling at the same slow
    #: rate also meant the CPU figure was up to 30 seconds stale, so a page
    #: refreshing every second redrew the same number - which reads as a broken
    #: gauge rather than a slow one.
    resource_sample_interval: float = 1.0
    #: Watchdog: exit (so systemd restarts us cleanly) above this RSS. 0 = off.
    memory_limit_mb: int = 0
    memory_check_interval: float = 30.0
    #: Number of pipeline failures tolerated before the watchdog exits.
    max_consecutive_failures: int = 10
    log_level: str = "INFO"
    log_json: bool = False
    access_log: bool = False

    # ------------------------------------------------------------- management
    #: Whether the web page at ``/settings`` may change anything.
    #:
    #: On by default, because the page exists to save somebody an SSH session -
    #: and this service is a LAN appliance behind no authentication at all. Off
    #: makes the page read-only, which is what you want if the port is reachable
    #: from somewhere you do not control, or if the configuration is managed by
    #: something else (Ansible, a config file in a git repo) and must not be
    #: edited behind its back.
    allow_ui_settings: bool = True

    # ------------------------------------------------------------ validation
    @model_validator(mode="after")
    def _check_engine_combination(self) -> Settings:
        """Refuse a configuration that asks for two mutually exclusive engines.

        ``ffmpeg_enabled = false`` leaves forwarding as the only engine, so
        ``prefer_passthrough = false`` next to it is a contradiction: it is the
        setting that says "always re-encode". Left alone it would take a
        configuration that looks deliberate and turn it into a service that
        answers every request with nothing at all, which is a very expensive way
        to discover a typo.
        """
        if not self.ffmpeg_enabled and not self.prefer_passthrough:
            # Worded so it reads as a sentence on the settings page *and* names
            # the two keys, since the other place it can be hit is a hand-edited
            # config file, where the keys are all the reader has.
            raise ValueError(
                "with FFmpeg turned off, forwarding is the only engine there is, so "
                "'prefer forwarding' cannot be off as well - nothing would be left "
                "to produce any audio (ffmpeg_enabled = false needs "
                "prefer_passthrough = true)"
            )
        return self

    @field_validator("bitrate")
    @classmethod
    def _check_bitrate(cls, v: int) -> int:
        if not 8_000 <= v <= 320_000:
            raise ValueError("bitrate must be between 8000 and 320000 bit/s")
        return v

    @field_validator("port")
    @classmethod
    def _check_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return v

    @field_validator("chunk_size")
    @classmethod
    def _check_chunk(cls, v: int) -> int:
        return max(1024, min(v, 1 << 20))

    @field_validator("resource_sample_interval")
    @classmethod
    def _check_sample_interval(cls, v: float) -> float:
        # A floor, because this is a wake-up rate on a Raspberry Pi and somebody
        # will eventually try 0.01 to make the page "smoother"; a ceiling because
        # anything slower than the check interval would make the setting a lie.
        if not 0.2 <= v <= 60.0:
            raise ValueError("resource_sample_interval must be between 0.2 and 60 seconds")
        return v

    @field_validator("preroll_seconds")
    @classmethod
    def _check_preroll(cls, v: float) -> float:
        # Zero is allowed and means "start every listener from silence", which is
        # only useful for diagnosing this behaviour. The ceiling is because this
        # is audio held in memory per station, and a value larger than the HLS
        # playlist window could not be filled anyway.
        if not 0.0 <= v <= 60.0:
            raise ValueError("preroll_seconds must be between 0 and 60")
        return v

    @field_validator("handoff_overlap_seconds")
    @classmethod
    def _check_handoff_overlap(cls, v: float) -> float:
        # Zero is allowed and means "exact resume: never re-send a byte the
        # client was already sent", which is the diagnostic opposite of this
        # setting. The ceiling keeps a typo from turning every radio handoff
        # into a multi-second replay.
        if not 0.0 <= v <= 10.0:
            raise ValueError("handoff_overlap_seconds must be between 0 and 10")
        return v

    @field_validator("stall_reconnect_seconds")
    @classmethod
    def _check_stall_reconnect(cls, v: float) -> float:
        # Zero disables the watchdog. The upper bound is generous on purpose:
        # the radio has a timeout of its own (a frozen server for 1.2 s was
        # enough to make it reconnect), so a large value would rarely fire - but
        # forbidding a diagnostic setting is not this validator's job. Below
        # 0.25 s it would fire on ordinary scheduling jitter and turn the stream
        # into a reconnect storm.
        if v != 0.0 and not 0.25 <= v <= 10.0:
            raise ValueError("stall_reconnect_seconds must be 0 (off) or between 0.25 and 10")
        return v

    @field_validator("stall_reconnect_cooldown_seconds")
    @classmethod
    def _check_stall_cooldown(cls, v: float) -> float:
        # Zero is allowed for testing, but it is genuinely dangerous in
        # production: an upstream outage would close and reopen every connection
        # once a second. The ceiling only stops a typo from disabling the
        # watchdog in practice.
        if not 0.0 <= v <= 600.0:
            raise ValueError("stall_reconnect_cooldown_seconds must be between 0 and 600")
        return v

    @field_validator("listener_write_timeout")
    @classmethod
    def _check_listener_write_timeout(cls, v: float) -> float:
        # Zero disables the deadline, which is a legitimate diagnostic position -
        # it restores the old behaviour of leaving the decision to the kernel.
        # The floor is above measured kernel buffering (2.8 s of Wi-Fi stall is
        # absorbed before the application sees a blocked write), so anything
        # shorter would fire on ordinary congestion and drop working radios. The
        # ceiling keeps a typo from putting the fifteen-minute wait back.
        if v != 0.0 and not 5.0 <= v <= 300.0:
            raise ValueError("listener_write_timeout must be 0 (off) or between 5 and 300")
        return v

    @field_validator("retransmit_reconnect_segments")
    @classmethod
    def _check_retransmit_segments(cls, v: int) -> int:
        # Zero disables the watchdog, which is also what happens automatically on
        # a platform that cannot report the count. A negative value has no
        # meaning, and one is already the smallest count that is evidence of
        # anything - the default of 3 exists only to ride out a stray blip.
        if v < 0:
            raise ValueError("retransmit_reconnect_segments must be 0 or more")
        return v

    @field_validator("retransmit_window_seconds")
    @classmethod
    def _check_retransmit_window(cls, v: float) -> float:
        # The window has to be at least a few polls long, or it is just the
        # per-poll threshold again with extra steps. The ceiling is because a
        # longer window delays the repair, and the repair is the whole point:
        # the radio is broken and waiting.
        if not 1.0 <= v <= 60.0:
            raise ValueError("retransmit_window_seconds must be between 1 and 60")
        return v

    @field_validator("retransmit_window_segments")
    @classmethod
    def _check_retransmit_window_segments(cls, v: int) -> int:
        # Zero disables the windowed trigger, leaving the per-poll one.
        # Negative has no meaning.
        if v < 0:
            raise ValueError("retransmit_window_segments must be 0 or more")
        return v

    @field_validator("reanchor_interval_seconds")
    @classmethod
    def _check_reanchor_interval(cls, v: float) -> float:
        # Zero disables it. Below 30 s it would interrupt a working radio far
        # more often than the radio reconnects by itself, which is pure cost for
        # no benefit; the ceiling stops a typo from turning the safety net off
        # without meaning to.
        if v != 0.0 and not 30.0 <= v <= 3600.0:
            raise ValueError("reanchor_interval_seconds must be 0 (off) or between 30 and 3600")
        return v

    @field_validator("retransmit_poll_seconds")
    @classmethod
    def _check_retransmit_poll(cls, v: float) -> float:
        # Below a quarter second the poll costs more than it can tell you; above
        # ten seconds a whole interference burst can pass between two reads, and
        # the count would be attributed to the wrong moment.
        if not 0.25 <= v <= 10.0:
            raise ValueError("retransmit_poll_seconds must be between 0.25 and 10")
        return v

    @field_validator("max_sessions", "max_listeners_per_session")
    @classmethod
    def _check_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v

    @field_validator("sample_rate")
    @classmethod
    def _check_rate(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v not in (
            8_000,
            11_025,
            12_000,
            16_000,
            22_050,
            24_000,
            32_000,
            44_100,
            48_000,
        ):
            raise ValueError(f"unsupported sample_rate: {v}")
        return v

    # ---------------------------------------------------------------- helpers
    @property
    def effective_output_codec(self) -> OutputCodec:
        """The output codec this installation can actually produce.

        Without FFmpeg the only possible output is the audio the station is
        already sending, so this answers ``"auto"`` no matter what
        ``output_codec`` says - which is exactly the same thing as every URL
        ending in ``?codec=auto``. That translation lives in one property rather
        than at each decision site because there are six of them (the engine, the
        ``Content-Type``, the probe report, the two pages, the session key) and a
        site that forgot it would disagree with the rest.
        """
        return "auto" if not self.ffmpeg_enabled else self.output_codec

    @property
    def passthrough_only(self) -> bool:
        """True when the FFmpeg-free path is the only engine available.

        Distinct from ``prefer_passthrough``, which is a *preference*: that one
        means "forward when you can, re-encode when you cannot". This one means
        there is no second engine at all.
        """
        return not self.ffmpeg_enabled

    @property
    def mime_type(self) -> str:
        return "audio/aac" if self.effective_output_codec == "adts" else "audio/mpeg"

    #: Force the ``Content-Type`` sent to radios (some firmware is picky and
    #: wants e.g. ``audio/mpeg3`` or ``audio/aacp``).
    mime_override: Optional[str] = None

    @property
    def cors_list(self) -> list:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def headers(self) -> Dict[str, str]:
        h = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if self.referer:
            h["Referer"] = self.referer
        return h

    # ------------------------------------------------------------ construction
    @classmethod
    def load(cls, config_file: Optional[str] = None, **overrides: Any) -> Settings:
        """Build settings from file + saved settings + environment + overrides.

        Precedence: defaults < config file < saved-from-the-page < environment <
        ``overrides``. ``overrides`` values that are ``None`` are ignored, so
        argparse defaults can never clobber file or environment configuration.

        The saved values sit above the config file because a change made in the
        browser is a later decision than one written into a file, and below the
        environment and the command line because those are how an installation
        is *pinned* - a deployment that sets ``RADIOCONV_BITRATE`` must not have
        it silently defeated by whoever opened the settings page.
        """
        file_values: Dict[str, Any] = {}
        path: Optional[Path] = None

        if config_file:
            path = Path(config_file).expanduser()
        else:
            candidate = default_config_dir() / "config.toml"
            if candidate.is_file():
                path = candidate

        if path is not None:
            if not path.is_file():
                raise FileNotFoundError(f"config file not found: {path}")
            file_values = _read_structured_file(path)

        clean = {k: v for k, v in overrides.items() if v is not None}
        with _file_values(file_values), _ui_values(read_ui_settings()):
            return cls(**clean)


def _read_structured_file(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".toml",):
        data = _toml.loads(text)
    elif path.suffix.lower() in (".json",):
        data = json.loads(text)
    else:
        raise TypeError(f"unsupported config file type: {path.suffix}")

    if not isinstance(data, dict):
        raise TypeError(f"config file must contain a table/object: {path}")
    # Allow everything to live under a [radioconv] table as well.
    section = data.get("radioconv")
    if isinstance(section, dict):
        merged = {k: v for k, v in data.items() if k != "radioconv"}
        merged.update(section)
        return merged
    return data
