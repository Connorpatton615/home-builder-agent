"""json_log.py — stdlib-only JSON logging for launchd-spawned jobs.

Sprint-1 item #6 of "Chad iOS Completion Push" (patton-os proj_002).
Pairs with the heartbeat watchdog that shipped earlier — that proves a
job is alive; this proves *what* it did.

Schema (one JSON object per line, stable contract):

    ts             ISO-8601 with timezone (UTC)
    level          INFO | WARNING | ERROR | DEBUG
    service        short slug — "hb-reconcile", "hb-bridge", etc.
    correlation_id optional — pass-through scope (run id, request id)
    event          short verb — "phase_status_flipped", "supabase_connect_failed"
    message        the formatted log message
    payload        dict of extras passed via `extra={}`
    traceback      present only when an exception is logged

Why stdlib not structlog? Same constraint as "no third-party iOS SDKs":
every dep on a launchd-spawned job is one more thing that can break a
3am pager. `logging` + a 30-line formatter is enough.

USAGE — at the top of a launchd entry-point's main():

    import logging
    from home_builder_agent.observability.json_log import configure_json_logging

    log = logging.getLogger(__name__)

    def main():
        configure_json_logging("hb-reconcile")
        log.info("pass_starting", extra={"event": "pass_starting"})
        ...
        log.info("pass_complete", extra={
            "event": "pass_complete",
            "applied": 3, "skipped": 0, "errors": 0,
        })

INTERACTIVE-vs-LAUNCHD GATE:
configure_json_logging() detects whether stderr is a TTY. When it is
(human runs `hb-reconcile --dry-run` from Terminal), it skips the JSON
formatter and leaves default text logging intact — so interactive
output stays human-readable. When stderr is a pipe (launchd captures
to StandardErrorPath), it installs the JSON formatter. Override with
`force=True` for testing or explicit machine consumption.

DOES NOT TOUCH:
- print() statements anywhere — those keep going to stdout, captured by
  launchd's StandardOutPath, still readable.
- Existing custom log() helpers in watchers (dashboard.py, inbox.py)
  that write to `watcher.log` / `inbox_watcher.log`. Those continue to
  work as legacy text logs alongside the new JSON stream.
- Library-level logging anywhere else. Only entry-point main()s call
  configure_json_logging() — it never auto-installs.

Migration of existing print()/log() sites to structured logger calls
is a follow-up sweep; the formatter is the contract, the migration is
opportunistic.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback as tb_mod
from datetime import datetime, timezone
from typing import Any

# Fields stamped onto every LogRecord by Python's logging machinery. We
# want to keep these out of the `payload` dict — they're either rendered
# into top-level schema fields or aren't user-supplied at all.
_RECORD_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
        "taskName",
        # Our own pulled-out fields:
        "service", "event", "correlation_id",
    }
)


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single line of JSON matching the schema above.

    Per-record extras passed via `logger.info(..., extra={"event": "...",
    "correlation_id": "...", "any_other": ...})` are routed:
      - `event` and `correlation_id` are pulled to top-level fields.
      - anything else lands inside `payload`.

    Exceptions logged via `logger.exception(...)` get a `traceback`
    field with the formatted stack.
    """

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )
        level = record.levelname
        if level == "WARN":
            level = "WARNING"

        out: dict[str, Any] = {
            "ts": ts,
            "level": level,
            "service": getattr(record, "service", self.service),
            "event": getattr(record, "event", None),
            "correlation_id": getattr(record, "correlation_id", None),
            "message": record.getMessage(),
        }

        # Pull any other extras into payload.
        payload: dict[str, Any] = {}
        for k, v in record.__dict__.items():
            if k in _RECORD_RESERVED:
                continue
            payload[k] = _safe(v)
        if payload:
            out["payload"] = payload

        # Exception traceback if present.
        if record.exc_info:
            out["traceback"] = "".join(
                tb_mod.format_exception(*record.exc_info)
            ).rstrip()
        elif record.exc_text:
            out["traceback"] = record.exc_text

        # Drop None-valued top-level keys to keep the schema lean. Required
        # fields (ts, level, service, message) are always non-None.
        out = {k: v for k, v in out.items() if v is not None}

        try:
            return json.dumps(out, ensure_ascii=False, default=_safe)
        except Exception:
            # Last-ditch fallback — never let a logging failure bring down
            # the service. Return a minimal text record that's still
            # consumable by tools expecting JSON-per-line.
            return json.dumps(
                {
                    "ts": ts,
                    "level": "ERROR",
                    "service": self.service,
                    "message": "json_log_format_failed",
                    "raw_message": record.getMessage(),
                }
            )


def _safe(value: Any) -> Any:
    """Return a JSON-serializable representation of `value`. Falls back to
    repr() for anything exotic. Never raises."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    try:
        return repr(value)
    except Exception:
        return "<unrepr>"


def configure_json_logging(
    service: str,
    *,
    level: int = logging.INFO,
    force: bool = False,
    stream=None,
) -> logging.Logger:
    """Install JsonFormatter on the root logger when stderr is non-interactive.

    Idempotent: clears existing root handlers before installing. Safe to
    call once at the top of main(); calling twice is harmless.

    Args:
        service:  short slug for this job (stamped into every record).
        level:    root logger level (default INFO).
        force:    install JSON formatter even when stderr is a TTY.
                  Useful for tests / explicit machine consumption.
        stream:   write target for the handler. Default sys.stderr —
                  launchd captures to StandardErrorPath. Pass any writable
                  stream for tests.

    Returns the root logger so callers can use it directly if they want.
    """
    target = stream if stream is not None else sys.stderr

    # TTY gate: if a human's running this in a terminal, leave default
    # text logging alone so interactive output stays readable.
    if not force and hasattr(target, "isatty") and target.isatty():
        return logging.getLogger()

    handler = logging.StreamHandler(target)
    handler.setFormatter(JsonFormatter(service))

    root = logging.getLogger()
    # Remove any prior handlers so we don't double-emit.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)
    return root
