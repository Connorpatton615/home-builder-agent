"""heartbeat.py — fire-and-exit jobs publish liveness signals here.

Each launchd-driven job calls `beat()` at the end of every successful run
with a stale-after threshold (typically 5x the cadence + grace). The
watchdog (home_builder_agent.watchers.watchdog) reads these files on a
10-minute interval and alerts if any job's heartbeat is older than its
threshold.

Layout:
    ~/Projects/home-builder-agent/.heartbeats/
      dashboard-watcher.json
      inbox-watcher.json
      reconcile.json
      morning-brief.json
      client-update.json

beat() is best-effort — it never raises. If it can't write the file the
watcher still completes its real work; the watchdog will alert on the
resulting staleness, which surfaces the underlying disk/permissions issue.
"""

from __future__ import annotations

import functools
import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_DIR = Path(__file__).resolve().parents[2] / ".heartbeats"


def beat(job: str, stale_after_seconds: int, status: str = "ok", note: str = "") -> None:
    """Publish a heartbeat for `job`. Call at the end of every successful run.

    `stale_after_seconds` is the per-job alert threshold — typically 5x the
    job's cadence with grace for slow runs (e.g., 60s loop → 300s threshold,
    daily cron → 25h threshold).
    """
    try:
        HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "job": job,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ts_unix": int(time.time()),
            "stale_after_seconds": stale_after_seconds,
            "status": status,
            "note": note,
            "host": socket.gethostname(),
        }
        path = HEARTBEAT_DIR / f"{job}.json"
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(record, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def read_all() -> list[dict]:
    """Read all heartbeat files. Malformed files are silently skipped."""
    if not HEARTBEAT_DIR.exists():
        return []
    out: list[dict] = []
    for f in sorted(HEARTBEAT_DIR.glob("*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            pass
    return out


def is_stale(record: dict, now: float | None = None) -> bool:
    """True if the record's timestamp is older than its stale_after_seconds."""
    ts = record.get("ts_unix") or 0
    threshold = record.get("stale_after_seconds") or 0
    if not ts or not threshold:
        return False
    return ((now or time.time()) - ts) > threshold


def beat_on_success(
    job: str,
    stale_after_seconds: int,
    success_codes: tuple[int, ...] = (0,),
    status: str = "ok",
):
    """Decorator: beats when the wrapped function returns OR exits with a
    success code. Failure exits and exceptions skip the beat — the watchdog
    will then alert on staleness.

    Usage:
        @beat_on_success("dashboard-watcher", 300)
        def main():
            ...

    `success_codes` widens what counts as alive — e.g. reconcile uses (0, 3)
    because exit 3 means the pass completed with handler errors that will be
    retried, which still proves the process is healthy.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                result = fn(*args, **kwargs)
            except SystemExit as e:
                code = e.code if e.code is not None else 0
                if code in success_codes:
                    beat(job, stale_after_seconds, status=status)
                raise
            beat(job, stale_after_seconds, status=status)
            return result
        return wrapper
    return decorator
