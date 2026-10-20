"""Logging setup.

Plain-but-pleasant console logs by default, or one-JSON-object-per-line when
``--log-json`` is given (handy when running under journald or Loki).

Logs are cheap and journald rotates them; the *absence* of a bound on disk
usage is one of the classic ways a hobby Pi service fills its SD card and dies,
so ``systemd`` ``SystemMaxUse`` is set by the provided unit file.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    value = repr(value)
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)-18s %(message)s", datefmt="%H:%M:%S"
        )

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        session = getattr(record, "session", None)
        if session and "session=" not in text:
            text = f"{text}  [session={session}]"
        return text


class _StderrHandler(logging.StreamHandler):
    """A handler that always writes to the *current* ``sys.stderr``.

    ``logging.StreamHandler(sys.stderr)`` binds the stream object once, at
    construction. If something replaces ``sys.stderr`` afterwards - pytest's
    capture does exactly that - the handler keeps writing to the old object,
    and if that object is later closed the write raises
    ``ValueError: I/O operation on closed file`` and Python prints it as
    "--- Logging error ---" instead of the log line.

    Looking the stream up on every emit costs an attribute read, and it means
    the log follows ``sys.stderr`` wherever it goes, which is what a caller
    expects.
    """

    def __init__(self) -> None:
        super().__init__(stream=sys.stderr)

    @property
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        # ``StreamHandler.__init__`` assigns to ``self.stream``; the property
        # ignores the value on purpose, because the stream is looked up
        # dynamically. The setter exists only so that assignment works.
        pass


def setup_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Configure the root logger once, idempotently."""
    root = logging.getLogger()
    handler = _StderrHandler()
    handler.setFormatter(JsonFormatter() if json_output else _ConsoleFormatter())

    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # These are chatty and never tell us anything useful.
    for noisy in ("httpx", "httpcore", "uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
