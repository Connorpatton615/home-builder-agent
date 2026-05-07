"""watchers/dashboard.py — Active dashboard refresh.

Polls the GENERATED TIMELINES Drive folder, finds tracker sheets that have
been modified since the last poll, and re-runs the dashboard refresh on each.

Designed to run once per invocation and exit. macOS launchd handles the
1-minute scheduling (see launchd plist at the repo root).

Why fire-and-exit (vs long-running daemon):
- launchd is the source of truth for "is this thing running" — surviving
  reboots, crashes, and login cycles is its job, not ours.
- Memory leaks, stale tokens, and accidental long-running state become
  non-issues when the process exits every minute.
- Logs are easy to scan because each invocation is a discrete entry.

Self-edit avoidance:
After this script writes to a tracker (its dashboard tab + formatting),
Drive's modifiedTime updates. We capture the post-write modifiedTime and
save THAT to state, so the next poll sees no change and skips. Only when
Chad/Connor edits a cell does modifiedTime change beyond what we saved.
"""

import json
import logging
import os
import signal
import socket
import sys
import uuid
from datetime import datetime

# Per-socket timeout so any blocking Drive/Sheets call cannot hang indefinitely.
# Without this, a stalled network call holds the process forever and launchd
# won't spawn a new instance until the zombie dies.
from home_builder_agent.config import (
    DRIVE_FOLDER_PATH,
    WATCHER_MAX_ERRORS_PER_RUN,
    WATCHER_SOCKET_TIMEOUT,
    WATCHER_TIMEOUT_SEC,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.heartbeat import beat_on_success
from home_builder_agent.integrations import drive, sheets
from home_builder_agent.observability.json_log import configure_json_logging

logger = logging.getLogger(__name__)

socket.setdefaulttimeout(WATCHER_SOCKET_TIMEOUT)

# State + log files live alongside .env / credentials.json (project root).
# Resolved relative to cwd, which launchd sets via WorkingDirectory.
STATE_FILE = os.path.abspath(".watcher_state.json")
LOG_FILE = os.path.abspath("watcher.log")


# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------

def log(msg):
    """Timestamped line to watcher.log AND stdout (launchd captures stdout)."""
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass  # don't crash watcher over a log-write failure


# ---------------------------------------------------------------------
# State (modifiedTime memory)
# ---------------------------------------------------------------------

def load_state():
    """Return mapping of {sheet_id: last_seen_modifiedTime}."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception as e:
        log(f"WARNING: could not read state file ({e}); starting fresh.")
        return {}


def save_state(state):
    """Persist {sheet_id: modifiedTime} atomically (write tmp, rename)."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------
# Per-sheet refresh
# ---------------------------------------------------------------------

def refresh_one(drive_svc, sheets_svc, tracker):
    """Refresh dashboard tab + visual formatting on one tracker sheet.

    Returns the post-refresh modifiedTime, which becomes the new baseline so
    we don't trigger ourselves on the next poll.
    """
    project_name = drive.extract_project_name(tracker["name"])
    phases = sheets.read_master_schedule(sheets_svc, tracker["id"])
    orders = sheets.read_order_schedule(sheets_svc, tracker["id"])
    metrics = sheets.compute_dashboard_metrics(phases, orders=orders)
    dashboard_sheet_id = sheets.ensure_dashboard_tab(sheets_svc, tracker["id"])
    sheets.write_dashboard(
        sheets_svc, tracker["id"], dashboard_sheet_id, metrics, project_name
    )
    sheets.apply_visual_formatting(sheets_svc, tracker["id"])

    return drive.get_file_modified_time(drive_svc, tracker["id"])


# ---------------------------------------------------------------------
# Main loop body (single invocation)
# ---------------------------------------------------------------------

def _timeout_handler(signum, frame):
    log(f"ERROR: watcher exceeded {WATCHER_TIMEOUT_SEC} sec — exiting "
        f"so launchd can respawn.")
    sys.exit(1)


@beat_on_success("dashboard-watcher", stale_after_seconds=300)
def main():
    configure_json_logging("hb-dashboard-watcher")
    correlation_id = uuid.uuid4().hex
    logger.info("pass_starting", extra={"event": "pass_starting", "correlation_id": correlation_id})

    # Hard kill if not done in WATCHER_TIMEOUT_SEC — belt-and-suspenders
    # alongside the per-socket timeout. Covers stalls outside socket calls.
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(WATCHER_TIMEOUT_SEC)

    try:
        creds = get_credentials()
    except Exception as e:
        log(f"ERROR: Google auth failed: {e}")
        sys.exit(1)

    drive_svc = drive.drive_service(creds)
    sheets_svc = sheets.sheets_service(creds)

    try:
        trackers = drive.find_all_trackers(drive_svc, DRIVE_FOLDER_PATH)
    except Exception as e:
        log(f"ERROR: could not list trackers: {e}")
        sys.exit(1)

    state = load_state()
    state_changed = False
    refreshed = 0
    errors = 0
    seen_ids = set()

    for tracker in trackers:
        seen_ids.add(tracker["id"])
        last_seen = state.get(tracker["id"], "")
        current = tracker["modifiedTime"]

        if last_seen == current:
            continue  # No change since last poll

        try:
            new_modtime = refresh_one(drive_svc, sheets_svc, tracker)
            state[tracker["id"]] = new_modtime
            state_changed = True
            refreshed += 1
            project = drive.extract_project_name(tracker["name"])
            log(f"Refreshed: {project} (sheet {tracker['id'][:10]}...)")
        except Exception as e:
            errors += 1
            if errors <= WATCHER_MAX_ERRORS_PER_RUN:
                log(f"ERROR refreshing {tracker['name']}: {e}")
            elif errors == WATCHER_MAX_ERRORS_PER_RUN + 1:
                log("...suppressing further error logs this run.")

    # GC state for sheets that no longer exist
    stale_ids = set(state.keys()) - seen_ids
    for sid in stale_ids:
        del state[sid]
        state_changed = True
        log(f"Forgot stale sheet: {sid[:10]}...")

    if state_changed:
        save_state(state)

    # Only log activity summary when something actually happened
    if refreshed or errors:
        log(f"Pass complete: refreshed={refreshed} errors={errors} "
            f"trackers_seen={len(seen_ids)}")

    logger.info(
        "pass_complete",
        extra={
            "event": "pass_complete",
            "correlation_id": correlation_id,
            "trackers_seen": len(seen_ids),
            "refreshed": refreshed,
            "errors": errors,
        },
    )


if __name__ == "__main__":
    main()
