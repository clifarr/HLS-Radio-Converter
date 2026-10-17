# Contributing

Everything here is for people **working on** the code. If you just want to run
it, read the [README](README.md) instead - it covers installation, pointing a
radio at it, and troubleshooting.

---

## Environment

Development is [uv](https://docs.astral.sh/uv/)-native. Everything runs through
`uv run`, which uses the project virtualenv and keeps it in sync with
`uv.lock`, so there is no virtualenv to activate and no task-runner layer:

```bash
git clone https://github.com/clifarr/HLS-Radio-Converter
cd HLS-Radio-Converter
uv sync                    # creates .venv, installs runtime + dev tools
uv run pytest -q           # runs inside that environment
```

Two details if you are used to other setups:

- **There is no Makefile.** `uv sync`, `uv run` and `uv build` already cover
  install/run/build, so a task runner would only be another thing to keep in
  step. The commands below are the whole workflow.
- **Dev tooling is a [PEP 735](https://peps.python.org/pep-0735/) dependency
  group**, not an optional-dependencies extra, which is why `uv sync` needs no
  `--extra dev`. If you would rather use pip (25.1+ understands groups):
  `pip install -e . --group dev`.

The *package* itself is an ordinary `pyproject.toml` with the Hatchling backend
and no uv-specific metadata, so it installs from a checkout with `pip install .`
just as cleanly as with `uv sync` - which is what the Raspberry Pi script in
`packaging/` relies on. It is not published to a package index and there are no
plans to: this is an application you install on the machine that runs it, not a
library other code imports.

## Everyday commands

```bash
uv run pytest -q                              # tests (live HTTP streaming, both engines)
uv run ruff format . && uv run ruff check .   # format and lint
uv run ruff check --fix .                     # apply safe lint fixes
uv run ty check                               # type check
uv run radioconv check --end-to-end           # end-to-end self-test on real HLS
./scripts/smoke.sh                            # start the server, stream, decode the MP3
uv build                                      # a wheel, to confirm it packages cleanly
```

Four scripts deliberately mutate source to prove the checks still bite; run
them before trusting a green build:

```bash
uv run python scripts/verify-linters-work.py       # ruff and ty actually catch defects
uv run python scripts/verify-web-guards.py         # the UI/metrics drift guards still fail
uv run python scripts/verify-container-detection.py  # the stream-format tests still bite
uv run python scripts/verify-playback-guards.py    # the continuity/metrics tests still bite
```

Each one reintroduces a real historical bug and asserts that a specific test
goes red; a test that passes with the bug back in place is not evidence, so
these scripts are the check on the checks. The verdict comes from pytest's
**exit code**, never from scraping its output.

Run those one at a time. Each rewrites a source file in place and restores it, so
two concurrent runs will corrupt the tree - which is exactly what happened once.

To run just the fast tests, skip the names that stream:
`uv run pytest -q -k "not stream"`.

## Linting and type checking

Two tools, both from [Astral](https://astral.sh/), both configured in
`pyproject.toml`:

- **`ruff`** - linting and formatting. The rule set is deliberately broad
  (`E`, `W`, `F`, `I`, `UP`, `B`, `C4`, `SIM`, `RUF`, `S`, `PL`, `TRY`, `BLE`,
  `ASYNC`, `RET`, `LOG`, `G`) and this codebase passes it with **zero findings**.
  Every excluded rule is listed in `pyproject.toml` with its reason directly
  above or after it, and every `noqa` says why inline. The one *directory-wide*
  exemption is `tests/**`, where security rules (`S`), magic numbers
  (`PLR2004`) and unused fixture arguments (`ARG001`) are not meaningful.
- **`ty`** - type checking, run against the same 3.9 environment.

```bash
uv run ruff format --check .   # formatting
uv run ruff check .            # lint
uv run ty check                # types
uv run python scripts/verify-linters-work.py   # prove the two above still bite
```

That last command is worth explaining: a linter reporting nothing is only
evidence of a clean codebase if it *would* have reported something. It injects
seven deliberate defects (unused import, bare `except`, undefined name,
`== None`, wrong assigned type, unknown attribute, wrong return type) into a real
source file, asserts each tool complains, then restores the file and verifies the
restore byte-for-byte. It is a script rather than a test because it temporarily
mutates source on disk.

`scripts/verify-web-guards.py` applies the same idea to the UI guards. It breaks
eight things the tests are supposed to catch - markup in a Python module, a
`data-metric` naming a metric that does not exist, the Prometheus renderer
dropping a family, an unreferenced asset, a page polling the wrong endpoint, an
unescaped form value - and asserts the matching test goes red. Both scripts
treat "the test still passed" as a failure, because a guard that cannot fail is
not a guard. They are run by hand rather than by pytest, since they edit files.

Three deliberate decisions worth knowing about:

1. **`target-version = "py39"`.** Ruff would happily rewrite every
   `Optional[X]` to `X | None` (it sees `from __future__ import annotations` and
   assumes the annotation is never evaluated). It *is* evaluated: pydantic and
   FastAPI resolve annotations at runtime, and on Python 3.9 that rewrite fails
   with `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`.
   We keep the 3.9 floor because Raspberry Pi OS Bullseye ships it, so
   `UP006`/`UP035`/`UP045` are disabled on purpose and documented in
   `pyproject.toml`. If you ever raise the floor to 3.10+, delete those three
   lines and run `ruff check --fix --unsafe-fixes` to modernise everything.
2. **A handful of `noqa: BLE001`** remain where catching `Exception` is the
   actual requirement - a stream pipeline that must survive a bad upstream, a
   health endpoint that must always answer, a diagnostic tool that must report
   rather than raise. Each one says why.
3. **Several `ty: ignore[too-many-positional-arguments]`** on `pytest.skip(...)`
   calls. This is a genuine bug in ty 0.0.88: it resolves `pytest.skip` as
   taking no positional arguments, although the real signature is
   `skip(reason: str = "", *, allow_module_level: bool = False)`. Verify with
   `inspect.signature(pytest.skip)`.

## Layout

Architecture, in the order data flows:

| Module | Responsibility |
|---|---|
| `config.py` | Settings: defaults < config file < `RADIOCONV_*` env < CLI flags. |
| `hls.py` | Fetching, master/variant selection, AES-128, byte ranges, live-edge tracking. |
| `tsdemux.py` | Dependency-free MPEG-TS/PSI demuxer - the FFmpeg-free path. |
| `pipeline.py` | One station: pass-through or FFmpeg, bounded queues, failover, restart. |
| `session.py` | Fan-out to many radios, capacity limits, linger, stall watchdog. |
| `throughput.py` | Cumulative byte counters, and the rolling last-hour view of them. |
| `metrics.py` | The one snapshot every numbers endpoint is rendered from. |
| `web.py` | Locating `templates/` and `static/`, and building each page's context. |
| `api.py` | FastAPI app: routes only. v1-compatible paths, `/health`, `/probe`, pages. |
| `monitor.py` | Process watchdog: memory and event-loop-lag based clean restart. |
| `check.py` | `radioconv check`, including the generated end-to-end stream test. |
| `templates/` | The two HTML pages (`base.html`, `index.html`, `dashboard.html`). |
| `static/` | `dashboard.css` and `dashboard.js`: not rendered, not templated. |
| `packaging/` | systemd unit, installer script, PyInstaller spec, example configs. |

## The three views of the same numbers

`/health`, `/metrics.json` and `/metrics` all report the same figures. They used
to be maintained separately - `/health` built a dict while `/metrics` printed
Prometheus lines by hand - which meant a new metric could appear in one and be
silently missing from the other. Now `metrics.snapshot()` builds one dict and
`metrics.to_prometheus()` renders it, so they cannot drift:

| Endpoint | Reader | Shape |
|---|---|---|
| `/health` | A supervisor or a person | The snapshot plus the FFmpeg report. |
| `/metrics.json` | A script, and the dashboard | The snapshot alone. |
| `/metrics` | Prometheus | The same snapshot as text exposition. |

The metric names are shared: `radioconv_sessions` in Prometheus is
`gauges.sessions` in JSON. `tests/test_web_ui.py` asserts that every published
sample equals its JSON counterpart, and that each name has a `HELP` and a `TYPE`
line - Prometheus rejects an exposition that omits them.

### Counters, gauges, and the rolling hour

Which family a number belongs to is not cosmetic. A counter only ever increases;
a gauge goes up and down. `throughput.py` keeps cumulative byte totals (counters,
so `rate()` works and they never dip) and derives a rolling one-hour view from
them, which is a **gauge** precisely because it falls again.

The window is served by a once-a-second sampler task, not by recording a point
when somebody reads the metrics. Lazy sampling looks free and is wrong: with no
point older than the window there is nothing to subtract from, the window falls
back to the whole cumulative total, and a service up for three days reports three
days of traffic as "the last hour". `tests/test_throughput.py` covers that case
explicitly, and pins `throughput.WINDOW_SECONDS` to the `*_last_hour` metric
names so the two cannot drift apart.

`bytes_out` means **bytes handed to a radio's socket**, not bytes the pipeline
produced. A station is kept warm after its last listener leaves and keeps
downloading and transcoding throughout; counting that as "audio sent" would show
traffic on a service delivering nothing.

The FFmpeg report is deliberately computed **once per process** and cached. It
runs the binary twice (for the version and the encoder list), and `/health` is
meant to be polled every few seconds. The answer cannot change while the
process is running, so a monitor asking "is this install healthy?" gets a real
answer instead of forking FFmpeg on every poll.

## The browser pages

Markup lives in `src/radioconv/templates/` and is rendered by Jinja2 (through
`web.py`); CSS and JavaScript live in `src/radioconv/static/` and are served by
`StaticFiles`. Neither imports FFmpeg, and Jinja2 itself is imported lazily
inside `web.environment()`: a Pi that only ever streams audio never pays for
any of it.

Two rules keep the pages honest, and both are tested:

* **No HTML in Python.** `test_no_html_lives_in_a_python_module` fails if a
  module contains markup. The landing page used to be an f-string here, with
  its CSS inline and its URLs escaped by hand in three places.
* **The JavaScript is static.** `dashboard.js` is not templated at all - it
  reads its endpoint and its interval from `data-metrics-url` and
  `data-refresh-ms` on `<body>` - so it stays cacheable and needs no escaping
  rules of its own. It writes every value with `textContent`, because a station
  label is a URL a user typed.

Adding a metric to a page means adding a `data-metric="gauges.sessions"`-style
attribute; `test_the_dashboard_only_asks_for_metrics_that_exist` resolves every
one of them against a live snapshot, so a typo fails the build rather than
rendering a tile that reads "–" forever.

`packaging/radioconv.spec` must list both directories in `datas`; a frozen
build reads them from `sys._MEIPASS`. `test_the_packaged_assets_are_declared_for_pyinstaller`
checks that, because getting it wrong produces a build that starts fine and then
returns a 500 to the first person who opens the page.

## How a segment's container is decided

This is worth understanding, because getting it wrong fails **silently**: the
HTTP response is already ``200 audio/mpeg`` by the time ffmpeg fails, so a radio
connects successfully and hears nothing, and nothing in the log says "failed".

There are four containers in real HLS, not two:

| Our name | ffmpeg's ``-f`` | Where it comes from |
|---|---|---|
| `ts` | `mpegts` | The classic HLS segment. |
| `mp4` | `mp4` | fMP4 / CMAF (``.m4s``), usually with an `EXT-X-MAP` init segment. |
| `adts` | **`aac`** | A bare ADTS AAC stream - what an `.aac` segment is. |
| `mp3` | `mp3` | A bare MPEG audio stream. |

The names are deliberately not identities: our `adts` is the honest description
of the *content*, while ffmpeg's demuxer for it is called `aac`. `-f adts` exists
and rejects this data with "Invalid argument".

Two rules keep this correct:

1. **The bytes decide, not the URL.** `PlaylistInfo.container` is only a hint
   (real evidence for MP4 is an `EXT-X-MAP` init segment or a `.m4s` URL;
   everything else is `"unknown"`). `tsdemux.classify_segment` reads the actual
   segment and its answer wins. The bug this replaced was
   `"mp4" if <mp4 evidence> else "ts"` - and "not MP4" does not imply "TS".
2. **If nothing is recognised, pass no `-f` at all.** ffmpeg probing the bytes
   finds the right demuxer; a wrong `-f` reads nothing and says so only on
   stderr. Verified byte-identical output between probing and `-f aac`.

`classify_segment` also skips leading ID3v2 tags, chained, before looking at the
audio. Many packagers put one at the start of every segment - Microsoft's
`msvdn.net` CDN puts **two** (`PRIV` with the Apple
`transportStreamTimestamp`, then `TEXT` with song metadata, 655 bytes) - and
without skipping them the first byte is `'I'` from `ID3` rather than the `0xFF`
sync word, so the audio is invisible to a naive check.

The ADTS check requires the layer field to be zero, which is what separates it
from an MPEG audio frame: `FF FB` (MPEG-1 Layer III) otherwise satisfies every
field a naive ADTS check looks at.

## Releasing

There is no package index involved. A release is a commit, a tag, and a note in
the GitHub release describing what changed:

```bash
# 1. bump `version` in pyproject.toml and src/radioconv/__init__.py, then commit
git tag v2.0.0
git push && git push --tags
```

Users get the new version by re-running the clone-and-install in the README, or
by pulling and re-running `packaging/install.sh` on the Pi - which reinstalls
into the existing venv and restarts the service in place. Worth saying in the
release notes whenever it applies: whether a config setting was renamed.

One thing to keep in step, guarded by `tests/test_metadata.py`: the repository
URL appears in `pyproject.toml`, the README, `packaging/install.sh` and the
systemd unit. Those disagreed once (`cliff` vs `clifarr`), and a wrong clone URL
in the README is invisible until a user tries to follow it.

---

## Why version 1 wedged after a few days

Kept here rather than in the README because it is history, not an instruction.
Version 1 was one file, three threads and a lot of shared mutable
state. Every one of its failure modes is now addressed structurally rather than
by tuning:

| Old behaviour | What happens now |
|---|---|
| `urllib.request.urlopen(...).read()` with no timeout. One unresponsive CDN connection hung the stream forever. | Every request goes through `httpx` with explicit connect/read timeouts, a total playlist budget and bounded retries. |
| A `ReadingToken` class attribute mutated from two threads with no locking, plus 1-second poll loops - a busy-spin that pegs one core on a Pi. | No threads in the hot path at all: asyncio tasks, no shared mutable flags, no polling loops. |
| Unbounded buffering between the reader thread, FFmpeg's pipe and the socket. Memory grew until the OOM killer arrived. | Every queue is bounded, so back-pressure propagates from a slow radio all the way back to the origin socket. |
| One FFmpeg process **per HTTP request**, leaked on any error path, killed only by a 15-second heuristic. | One pipeline per station, shared by all listeners, in its own process group, always reaped. |
| A 3-thread CherryPy pool: two stuck streams and the server stopped accepting connections. | Async I/O, capacity limits (`max_sessions`) and an explicit `503` + `Retry-After` instead of a silent hang. |
| `cherrypy.response.status = 500` set *after* a generator had finished. | Proper ASGI streaming responses; a listener that starves for too long is closed so the radio reconnects. |
| Nothing detected a wedged stream; it stayed wedged until reboot. | Per-operation timeouts, a stall watchdog that restarts the pipeline, and a supervisor that restarts with backoff. |
| No visibility at all. | `/health`, `/metrics.json`, `/metrics`, a live `/dashboard`, structured logs, and a resource monitor that exits *cleanly* for systemd to restart if memory or the event loop go bad. |

The systemd unit adds the OS-level half: `MemoryHigh`/`MemoryMax` so the kernel
reclaims this service rather than the whole machine, `KillMode=mixed` so FFmpeg
children cannot outlive the service, a sane `Nice`/`CPUWeight` so SSH stays
responsive, and a bounded journal so logs cannot fill an SD card.

## Where FFmpeg comes from, per platform

| Platform | Source of FFmpeg |
|---|---|
| macOS (Intel/Apple Silicon), Windows, Linux x86_64/arm64 | the [`imageio-ffmpeg`](https://pypi.org/project/imageio-ffmpeg/) wheel - installed by `pip`, no package manager |
| **32-bit ARM (`armv6l`/`armv7l`)** - older Pis, `armhf` Raspberry Pi OS | **no wheel exists**, so FFmpeg must come from the distro: `apt install ffmpeg` |

There is no `linux-armv7` build in `imageio-ffmpeg` at all, so on those Pis the
wheel installs and then quietly provides nothing. `packaging/install.sh` handles
this by asking `radioconv` where its FFmpeg is and installing the system package
when the answer is "nowhere". Resolution order at runtime: `--ffmpeg`,
`IMAGEIO_FFMPEG_EXE`, a bundled/sidecar binary, the wheel, then `$PATH`.
`radioconv check` reports exactly which one is in use - useful, because on a Pi
the answer should be `/usr/bin/ffmpeg`.

## Why there is no Docker image

Docker was considered and deliberately left out. The target hardware is
Raspberry Pi Zero/3/4/5 class, where the container layer costs tens of
megabytes of RAM, an extra image to keep current, and - most importantly - a
second copy of the FFmpeg binary on disk. On armv7 hosts the mainstream Python
images are unavailable or slow. The virtualenv the installer builds already
gives the isolation Docker would: no system Python packages are touched.
FFmpeg is the one thing that may still be needed from the system, since the
wheel has no 32-bit ARM build - but on a Pi it is usually already there for Kodi.

If you do want a container, the `Dockerfile` below is enough
(`python:3.12-slim-bookworm` is multi-arch):

```dockerfile
FROM python:3.12-slim-bookworm
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
EXPOSE 8123
ENTRYPOINT ["radioconv", "serve"]
```

Do note that the bundled FFmpeg wheel is x86_64/aarch64 only, so a 32-bit ARM
container has no FFmpeg and needs `apt install ffmpeg` in the image. The
systemd hardening - where a lot of the stability work lives - would also then be
yours to reimplement.

## How the performance figures were measured

The README quotes a Pi 3 at roughly 165% of one core per transcoded station.
Those numbers were taken by walking the process tree with `psutil` from a
separate script, deliberately sampling faster than the service's own
`memory_check_interval` (30s), which is too coarse for a short measurement
window. `rss` and `cpu` cover the server *and* its ffmpeg children.

If you re-measure, note that SD-card speed, thermal throttling and Wi-Fi all
move the result, so treat any single run as indicative.

## Why there is no station list

v2 briefly grew a `stations.toml` file holding named presets with failover
mirrors. It was **removed** deliberately, because it fought the point of the
project:

* Version 1's design was config-free: a radio holds one encoded URL and the
  server needs to know nothing. A station file means a second place to edit
  whenever a stream moves, and a Pi that has to be logged into to change a
  radio's channel.
* Preset URLs only exist to be shorter to type. A radio's URL field is filled in
  once, at setup, by pasting `radioconv url …` output, so brevity buys nothing.
* Every preset-based URL shape has to keep working forever. Supporting them
  added a small station-list JSON API, a preset streaming route, a `radioconv
  stations` subcommand, an installer branch and roughly 20 tests - all to
  duplicate information the radio already carries.

`tests/test_metadata.py`, `tests/test_api_contract.py`,
`tests/test_config.py` and `scripts/test-installer-helpers.sh` all contain guards
that fail if any of it comes back.
