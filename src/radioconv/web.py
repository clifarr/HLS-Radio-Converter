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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict
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


def index_page(settings: Settings, request: Request, target: str = "") -> HTMLResponse:
    """``GET /`` - paste a stream URL, get back the URL to type into a radio.

    ``target`` is whatever the user typed, echoed back into the form. It is
    never trusted: the template escapes it, and it is only turned into links
    when it actually looks like an ``http(s)`` URL.
    """
    target = (target or "").strip()
    usable = target.startswith(("http://", "https://"))
    base = base_url(settings, request)
    # Percent-encoded for the path and for the query string, then escaped by the
    # template for the attribute it lands in: three different contexts, three
    # different encodings, each one the template's job except this one.
    encoded = quote(target, safe="") if usable else ""

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
    )


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
    """The settings a person watching a dashboard wants in front of them.

    All strings, already formatted: rendering "192" and "kbit/s" in two places
    is how a dashboard ends up claiming 192 *bytes* per second.
    """
    return {
        "Bitrate": f"{settings.bitrate // 1000} kbit/s",
        "Output codec": settings.output_codec,
        "Max stations": str(settings.max_sessions),
        "Max listeners each": str(settings.max_listeners_per_session),
        "Keep warm after the last radio leaves": f"{settings.linger_seconds:g}s",
        "Restart a silent station after": f"{settings.stall_timeout:g}s",
        "Sample rate": f"{settings.sample_rate} Hz" if settings.sample_rate else "source",
        "Channels": str(settings.channels),
    }
