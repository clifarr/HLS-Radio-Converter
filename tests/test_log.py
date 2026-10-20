"""Logging setup, and the one thing a logger must not do.

The failure this file is about is not a wrong log line. It is a log line that
*cannot be written*: the handler writes to a stream that has been closed, and
Python prints the resulting ``ValueError`` as "--- Logging error ---" instead
of the message that was supposed to be logged.

The cause is that ``logging.StreamHandler(sys.stderr)`` binds the stream
object once, when the handler is created. If ``sys.stderr`` is replaced
afterwards - which is exactly what a test harness does when it captures
output - the handler keeps writing to the old object, and if that object is
later closed, the write fails.
"""

from __future__ import annotations

import io
import logging
import sys

import pytest

from radioconv.log import setup_logging


@pytest.fixture
def restore_root_logger():
    """Put the root logger back the way it was.

    ``setup_logging`` reconfigures the *root* logger, so a test that calls it
    has to undo that, or every later test in the session logs through this
    project's handler.
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(level)


def test_the_handler_follows_sys_stderr_when_it_is_replaced(restore_root_logger):
    """The handler must write to the *current* ``sys.stderr``.

    This is the whole point: a handler that captured the stream once goes on
    writing to a stream nobody reads any more - or, worse, to a stream that
    has been closed.
    """
    setup_logging("INFO")
    root = logging.getLogger()
    assert root.handlers, "setup_logging installed no handler"

    first = io.StringIO()
    second = io.StringIO()
    original = sys.stderr
    try:
        sys.stderr = first
        logging.getLogger("radioconv.test").info("to the first stream")
        sys.stderr = second
        logging.getLogger("radioconv.test").info("to the second stream")
    finally:
        sys.stderr = original

    assert "to the first stream" in first.getvalue()
    assert "to the second stream" in second.getvalue()
    assert "to the second stream" not in first.getvalue()


def test_a_closed_stream_does_not_turn_a_log_line_into_a_crash(restore_root_logger):
    """A closed stderr must not produce "--- Logging error ---".

    The old behaviour: the handler held the stream it was given at setup
    time; that stream was closed; the next log record raised
    ``ValueError: I/O operation on closed file`` inside ``emit``, and the
    logging module printed its own traceback to stderr instead of the message.

    Here the stream is closed *and* replaced, which is the case that used to
    fail. Writing to the closed one raises; writing to the replacement works.
    """
    setup_logging("INFO")

    closed = io.StringIO()
    replacement = io.StringIO()
    original = sys.stderr
    try:
        sys.stderr = closed
        closed.close()
        sys.stderr = replacement
        logging.getLogger("radioconv.test").info("still delivered")
    finally:
        sys.stderr = original

    assert "still delivered" in replacement.getvalue()


def test_setup_logging_is_idempotent(restore_root_logger):
    """Calling it twice must not leave two handlers behind.

    Two handlers means every line is written twice, and the second copy goes
    to a stream that may be closed.
    """
    setup_logging("INFO")
    setup_logging("INFO")
    root = logging.getLogger()
    assert len(root.handlers) == 1, f"expected one handler, found {root.handlers}"


def test_the_json_formatter_still_produces_json(restore_root_logger):
    """The lazy-stderr change must not break the JSON output."""
    import json

    setup_logging("INFO", json_output=True)
    buffer = io.StringIO()
    original = sys.stderr
    try:
        sys.stderr = buffer
        logging.getLogger("radioconv.test").info("a message", extra={"session": "abc"})
    finally:
        sys.stderr = original

    payload = json.loads(buffer.getvalue().strip())
    assert payload["msg"] == "a message"
    assert payload["logger"] == "radioconv.test"
    assert payload["session"] == "abc"
