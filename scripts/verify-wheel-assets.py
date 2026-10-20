"""Prove the built wheel actually contains the templates and the static assets.

``templates/`` and ``static/`` live *inside* the package directory, so
Hatchling includes them without being told to - but "without being told to"
is not a guarantee, and the failure is silent until the day it matters. A
wheel missing ``templates/`` installs cleanly, the service starts, it streams
audio perfectly, and then it serves a 500 on its own web pages. On a
Raspberry Pi that is a field failure, hours after the deploy that caused it.

So this is checked in two places, and they must agree:

* the ``package`` job in ``.github/workflows/ci.yml``, which builds the wheel
  and looks inside it;
* the ``wheel-contains-assets`` pre-commit hook, which runs this script.

The check itself lives here rather than being written twice, so the two
cannot drift: CI calls this script too.

    uv run python scripts/verify-wheel-assets.py
"""

from __future__ import annotations

import glob
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent

#: Every file the wheel must carry. These are read at runtime by
#: ``radioconv.web``: the templates are rendered, and the CSS and JS are
#: served to the browser. A wheel without one of them is a service that
#: cannot render its own pages.
REQUIRED: List[str] = [
    "templates/base.html",
    "templates/dashboard.html",
    "templates/index.html",
    "static/dashboard.css",
    "static/dashboard.js",
]


def build() -> Path:
    """Build the wheel and return its path.

    ``uv build`` is used rather than ``python -m build`` so the same tool
    that CI uses builds it here, and so the build runs in the project's own
    environment. The sdist is built alongside it, which is what uv does by
    default.
    """
    # Resolved rather than bare: invoking a bare "uv" is an S607 lint error.
    # The argv is built entirely from this module's own constants - no shell,
    # no external input - which is the same justification as
    # verify-linters-work.py.
    uv = shutil.which("uv") or "uv"
    result = subprocess.run(  # noqa: S603
        [uv, "build"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit("the wheel did not build")

    wheels = sorted(glob.glob(str(ROOT / "dist" / "*.whl")))
    if not wheels:
        raise SystemExit("the build produced no wheel")
    return Path(wheels[-1])


def main() -> int:
    wheel = build()
    names = zipfile.ZipFile(wheel).namelist()
    missing = [needed for needed in REQUIRED if not any(name.endswith(needed) for name in names)]
    if missing:
        print(f"{wheel.name} is missing:", file=sys.stderr)
        for needed in missing:
            print(f"  - {needed}", file=sys.stderr)
        print(
            "The service would start, stream audio, and then serve a 500 on its own pages.",
            file=sys.stderr,
        )
        return 1
    print(f"{wheel.name}: all {len(REQUIRED)} assets present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
