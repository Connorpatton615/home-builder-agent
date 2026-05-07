"""Tests for home_builder_agent.observability.json_log."""

from __future__ import annotations

import io
import json
import logging
import re

import pytest

from home_builder_agent.observability.json_log import (
    JsonFormatter,
    configure_json_logging,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def captured():
    """Configure a fresh root logger writing JSON into a StringIO buffer."""
    buf = io.StringIO()
    configure_json_logging("hb-test", force=True, stream=buf)
    yield buf
    # Tests share root logger state — reset after each.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def _last_line(buf: io.StringIO) -> dict:
    lines = buf.getvalue().strip().splitlines()
    assert lines, "no log lines emitted"
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# Per-spec tests (per the prompt § Add unit tests covering)
# ---------------------------------------------------------------------------

def test_output_is_valid_json(captured):
    """(a) JSON output is valid JSON — every line parseable independently."""
    log = logging.getLogger("any.module")
    log.info("hello")
    log.warning("watch out")
    log.error("kaboom")

    for line in captured.getvalue().strip().splitlines():
        # No raises = parseable
        json.loads(line)


def test_iso_timestamp_present(captured):
    """(b) ISO timestamp present and tz-aware."""
    logging.getLogger().info("anything")
    rec = _last_line(captured)
    assert "ts" in rec
    # ISO 8601 with timezone offset (Z or +HH:MM)
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+(Z|[+-]\d{2}:\d{2})$",
        rec["ts"],
    ), f"Expected ISO-with-tz timestamp, got: {rec['ts']!r}"


def test_extras_roll_through_to_payload(captured):
    """(c) extras roll through to payload — and `event` / `correlation_id`
    are pulled to top-level fields per schema."""
    log = logging.getLogger("any.module")
    log.info(
        "phase advanced",
        extra={
            "event": "phase_status_flipped",
            "correlation_id": "run-abc-123",
            "phase_name": "Framing",
            "from_status": "in-progress",
            "to_status": "complete",
        },
    )
    rec = _last_line(captured)

    # Top-level promoted fields
    assert rec["event"] == "phase_status_flipped"
    assert rec["correlation_id"] == "run-abc-123"

    # Other extras under payload
    assert rec["payload"]["phase_name"] == "Framing"
    assert rec["payload"]["from_status"] == "in-progress"
    assert rec["payload"]["to_status"] == "complete"

    # Reserved Python LogRecord fields stay out of payload
    assert "args" not in rec["payload"]
    assert "msecs" not in rec["payload"]


def test_exception_records_traceback(captured):
    """(d) Exceptions get a traceback field."""
    try:
        raise ValueError("test crash")
    except ValueError:
        logging.getLogger().exception("something blew up")
    rec = _last_line(captured)
    assert rec["level"] == "ERROR"
    assert "traceback" in rec
    assert "ValueError: test crash" in rec["traceback"]


# ---------------------------------------------------------------------------
# Schema completeness
# ---------------------------------------------------------------------------

def test_required_fields_always_present(captured):
    """Required schema fields ts, level, service, message are always present."""
    logging.getLogger().info("ping")
    rec = _last_line(captured)
    for field in ("ts", "level", "service", "message"):
        assert field in rec, f"missing required field {field!r}"
    assert rec["service"] == "hb-test"
    assert rec["level"] == "INFO"
    assert rec["message"] == "ping"


def test_levels_normalize(captured):
    """WARN is normalized to WARNING per schema."""
    logging.getLogger().warning("careful")
    rec = _last_line(captured)
    assert rec["level"] == "WARNING"


def test_omits_empty_optional_fields(captured):
    """`event` and `correlation_id` are omitted when not set — keep the
    schema lean."""
    logging.getLogger().info("plain message")
    rec = _last_line(captured)
    assert "event" not in rec
    assert "correlation_id" not in rec


def test_one_line_per_record(captured):
    """Each record is exactly one line — newline-delimited JSON friendly."""
    log = logging.getLogger()
    log.info("first")
    log.info("second")
    log.info("third")
    lines = captured.getvalue().strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        # No embedded newlines inside a record
        assert "\n" not in line


# ---------------------------------------------------------------------------
# TTY gate
# ---------------------------------------------------------------------------

def test_tty_gate_skips_json_install():
    """When stderr is a TTY, configure_json_logging() leaves logging alone."""
    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    fake = FakeTTY()
    # Save current root handlers
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure_json_logging("hb-test", stream=fake)
        # Nothing about the root handlers should have changed
        assert list(root.handlers) == before
        # And nothing got written to our fake stream
        assert fake.getvalue() == ""
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)


def test_force_bypasses_tty_gate():
    """force=True installs JSON even on a TTY."""
    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    fake = FakeTTY()
    configure_json_logging("hb-test", force=True, stream=fake)
    try:
        logging.getLogger().info("forced through")
        assert fake.getvalue().strip(), "expected JSON output even on TTY"
        rec = json.loads(fake.getvalue().strip().splitlines()[-1])
        assert rec["service"] == "hb-test"
    finally:
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_unserializable_extras_fall_back_to_repr(captured):
    """A non-JSON-serializable extra is rendered via repr() rather than crashing."""
    class Weird:
        def __repr__(self):
            return "<Weird>"

    logging.getLogger().info("weird payload", extra={"thing": Weird()})
    rec = _last_line(captured)
    assert rec["payload"]["thing"] == "<Weird>"


def test_idempotent_configure():
    """Calling configure_json_logging twice produces a single handler — no
    double-emit."""
    buf1 = io.StringIO()
    configure_json_logging("hb-test", force=True, stream=buf1)
    buf2 = io.StringIO()
    configure_json_logging("hb-test", force=True, stream=buf2)
    try:
        logging.getLogger().info("once")
        assert buf1.getvalue() == ""  # First handler removed
        assert len(buf2.getvalue().strip().splitlines()) == 1
    finally:
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
