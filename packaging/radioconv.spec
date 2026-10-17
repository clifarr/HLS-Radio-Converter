# PyInstaller spec: build a single-file `radioconv` with FFmpeg inside.
#
#   pip install pyinstaller
#   pyinstaller packaging/radioconv.spec
#
# The FFmpeg binary is copied in as a sibling of the executable and located at
# runtime by radioconv.ffmpeg._sidecar_ffmpeg(), so the result needs no Python,
# no pip and no system FFmpeg.
#
# ONEFILE trade-off, which matters a lot on a Raspberry Pi:
#   * ONEFILE = True  - one tidy file, but every start unpacks ~50 MB of FFmpeg
#                       into a temp dir (slow on an SD card, and it doubles
#                       peak disk use).
#   * ONEFILE = False - a folder of files; starts instantly and is what you want
#                       for a long-running service. RECOMMENDED for a Pi.
ONEFILE = False

import sys
import os
from pathlib import Path

project_root = Path(SPECPATH).parent
sys.path.insert(0, str(project_root / "src"))

# --- locate the ffmpeg binary shipped by the imageio-ffmpeg wheel -------------
ffmpeg_binary = None
try:
    import imageio_ffmpeg

    ffmpeg_binary = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as exc:  # pragma: no cover
    print(f"warning: could not locate a bundled ffmpeg ({exc}); the build will "
          "rely on a system ffmpeg at runtime", file=sys.stderr)

datas = []
if ffmpeg_binary and os.path.isfile(ffmpeg_binary):
    datas.append((ffmpeg_binary, "."))

# The browser pages and their assets, listed literally. radioconv/web.py looks
# for them under `radioconv/` when frozen (sys._MEIPASS), so a missing entry
# here is a build that starts fine and answers its first `/` with a 500.
# Written out rather than looped over so that the paths are greppable -
# tests/test_web_ui.py asserts on these exact strings.
_assets = {
    "radioconv/templates": project_root / "src" / "radioconv" / "templates",
    "radioconv/static": project_root / "src" / "radioconv" / "static",
}
for _target, _source in _assets.items():
    if _source.is_dir():
        datas.append((str(_source), _target))
    else:
        print(f"warning: {_source} is missing; the browser pages will not work",
              file=sys.stderr)

hiddenimports = [
    # uvicorn pulls its loop/protocol implementations in dynamically.
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "pydantic",
    "pydantic_settings",
    "anyio",
    "m3u8",
    "httpx",
    "httptools",
    "h11",
    "cryptography",
    "psutil",
    # Imported inside radioconv.web.environment(), so the freezer cannot see it.
    "jinja2",
    "markupsafe",
]

a = Analysis(
    [str(project_root / "packaging" / "_entry.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="radioconv",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,  # UPX can corrupt the FFmpeg binary
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="radioconv",
        debug=False,
        strip=False,
        upx=False,
        console=True,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="radioconv",
    )
