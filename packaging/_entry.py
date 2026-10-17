#!/usr/bin/env python3
"""Entry point used by the PyInstaller build."""

from __future__ import annotations

import multiprocessing
import sys

from radioconv.cli import main

if __name__ == "__main__":
    # Harmless when there is no multiprocessing; required by PyInstaller on
    # some platforms to avoid re-executing the whole frozen bootstrap.
    multiprocessing.freeze_support()
    sys.exit(main())
