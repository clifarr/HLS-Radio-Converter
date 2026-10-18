"""
HLS Radio Converter
===================

Converts HLS (``.m3u8``) internet-radio streams into plain, continuous HTTP
audio streams (MP3 by default) so that Internet Radios based on Frontier
Silicon and similar chipsets - which understand ``audio/mpeg`` but not HLS -
can play them.

Design goals
------------
* **Cheap**: a pure-Python MPEG-TS demuxer takes a completely FFmpeg-free path
  whenever the source is already in a format the radio can play. FFmpeg is only
  spawned when a real codec conversion is unavoidable.
* **Boringly reliable**: every socket has a timeout, every queue is bounded,
  every subprocess runs in its own process group and is reaped, and a watchdog
  restarts stalled pipelines. No unbounded blocking anywhere.
* **Easy to install**: pure-python dependencies plus a pip-shipped FFmpeg
  binary, so ``pip install hls-radio-converter`` is the whole story - on x86,
  arm64 or 32-bit Raspberry Pi.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "2.0.1"
