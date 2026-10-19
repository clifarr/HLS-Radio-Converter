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
import os
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Tuple

from pydantic import field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from . import _toml

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

    # ------------------------------------------------------------ validation
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
    def mime_type(self) -> str:
        return "audio/aac" if self.output_codec == "adts" else "audio/mpeg"

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
        """Build settings from file + environment + explicit overrides.

        Precedence: defaults < config file < environment < ``overrides``.
        ``overrides`` values that are ``None`` are ignored, so argparse
        defaults can never clobber file or environment configuration.
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
        with _file_values(file_values):
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
