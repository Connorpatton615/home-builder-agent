"""watchdog.py — alerts on stale heartbeats from home-builder watchers.

Fire-and-exit. Runs every 10 min via launchd (com.chadhomes.watchdog).
Reads .heartbeats/*.json, checks each against its stale_after_seconds,
appends a line to .heartbeat_alerts.log per stale job, and fires a
single macOS notification summarizing all stale jobs.

Exit codes:
  0 — pass complete (alerts fired or all fresh)
  1 — couldn't read heartbeat directory at all (rare; permission/disk)

The watchdog never tries to "fix" a stale job — surfacing is the job.
Connor reads .heartbeat_alerts.log or sees the macOS banner and decides
whether to reload the launchd job, restart the Mac Mini, or dig in.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from home_builder_agent.core.heartbeat import HEARTBEAT_DIR, is_stale, read_all
from home_builder_agent.observability.telemetry import emit_event

ALERT_LOG = os.path.abspath(".heartbeat_alerts.log")


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{_ts()}] {msg}"
    print(line)
    try:
        with open(ALERT_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify_macos(title: str, body: str) -> None:
    title_esc = title.replace("\\", "\\\\").replace('"', '\\"')
    body_esc = body.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{body_esc}" with title "{title_esc}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            timeout=5, capture_output=True, check=False,
        )
    except Exception:
        pass


def main() -> None:
    if not HEARTBEAT_DIR.exists():
        print(f"[{_ts()}] WARN: no heartbeat dir yet at {HEARTBEAT_DIR}")
        sys.exit(0)

    records = read_all()
    if not records:
        print(f"[{_ts()}] WARN: heartbeat dir empty — no jobs have beat yet")
        sys.exit(0)

    now = time.time()
    stale = [r for r in records if is_stale(r, now)]

    if not stale:
        print(f"[{_ts()}] OK — all {len(records)} heartbeats fresh")
        sys.exit(0)

    log(f"STALE — {len(stale)} of {len(records)} heartbeat(s) overdue:")
    for r in stale:
        age = int(now - r.get("ts_unix", 0))
        threshold = r.get("stale_after_seconds", 0)
        last = r.get("ts", "?")
        log(f"  {r.get('job', '?')} — last beat {age}s ago "
            f"(threshold {threshold}s, last_ts {last})")

    summary = ", ".join(
        f"{r.get('job', '?')} ({int(now - r.get('ts_unix', 0))}s)" for r in stale
    )
    notify_macos(
        title=f"Patton AI: {len(stale)} stale heartbeat(s)",
        body=summary[:200],
    )

    # Telemetry — one agent.alert_paged per stale job (per ADR 2026-05-09).
    # The macOS notification consolidates them, but the canonical event
    # log gets one row per affected job so analytics can attribute.
    for r in stale:
        emit_event(
            event_type="agent.alert_paged",
            source="home-builder-agent.watchdog",
            subject_type="launchd_job",
            subject_id=r.get("job"),
            metadata={
                "channel": "macos_notification",
                "alert_kind": "stale_heartbeat",
                "age_seconds": int(now - r.get("ts_unix", 0)),
                "threshold_seconds": r.get("stale_after_seconds", 0),
                "last_ts": r.get("ts"),
            },
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
