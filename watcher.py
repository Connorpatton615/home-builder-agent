"""
watcher.py — Active dashboard refresh.

Polls the GENERATED TIMELINES Drive folder, finds tracker sheets that have
been modified since the last poll, and re-runs the dashboard + visual
formatting refresh on each.

Designed to run once per invocation and exit. macOS launchd handles the
1-minute scheduling (see com.chadhomes.dashboard-watcher.plist).

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
import os
import signal
import socket
import sys
from datetime import datetime

# Allow OAuth scope flexibility (Google may return fewer scopes than requested)
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# Per-socket timeout so any blocking Drive/Sheets call cannot hang indefinitely.
# Without this, a stalled network call holds the process forever and launchd
# won't spawn a new instance until the zombie dies. (Seen 2026-04-25.)
socket.setdefaulttimeout(45)

from googleapiclient.discovery import build

# Reuse all the heavy lifting from agent_2_5_dashboard
from agent_2_5_dashboard import (
    get_credentials,
    read_master_schedule,
    compute_dashboard_metrics,
    ensure_dashboard_tab,
    write_dashboard,
    apply_visual_formatting,
    extract_project_name,
    DRIVE_FOLDER_PATH,
)

# --- Config ----------------------------------------------------------

# Files live alongside this script (same dir as agent_2_v1.py etc.)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, ".watcher_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "watcher.log")

# How many error log lines we'll print before suppressing further duplicates
# in a single invocation. Prevents one bad sheet from filling the log.
MAX_ERRORS_PER_RUN = 5

# Hard ceiling on a single watcher invocation. launchd fires us every 60 sec,
# so 90 sec is generous — if we're not done by then, something is hung.
# Better to bail and let launchd spawn a fresh instance than to block the queue.
WATCHER_TIMEOUT_SEC = 90


# --- Logging --------------------------------------------------------

def log(msg):
    """Append a timestamped line to watcher.log AND stdout (launchd captures it)."""
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        # If the log write fails, just rely on stdout — don't crash the watcher
        pass


# --- State (modifiedTime memory) ------------------------------------

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
    """Persist the {sheet_id: modifiedTime} map atomically."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


# --- Drive scan -----------------------------------------------------

def walk_to_folder(drive_service, folder_path):
    """Walk a name-path list and return the deepest folder's ID."""
    parent_id = "root"
    walked = []
    for name in folder_path:
        walked.append(name)
        query = (
            f"name='{name}' "
            "and mimeType='application/vnd.google-apps.folder' "
            f"and '{parent_id}' in parents "
            "and trashed=false"
        )
        results = drive_service.files().list(
            q=query, fields="files(id,name)"
        ).execute()
        folders = results.get("files", [])
        if not folders:
            raise FileNotFoundError(
                f"Folder not found in Drive: {' / '.join(walked)}"
            )
        parent_id = folders[0]["id"]
    return parent_id


def find_all_trackers(drive_service, folder_path):
    """Return ALL Tracker spreadsheets in the GENERATED TIMELINES folder.

    Returns list of {id, name, modifiedTime, webViewLink}, most recent first.
    """
    parent_id = walk_to_folder(drive_service, folder_path)
    query = (
        "name contains 'Tracker' "
        "and mimeType='application/vnd.google-apps.spreadsheet' "
        f"and '{parent_id}' in parents "
        "and trashed=false"
    )
    results = drive_service.files().list(
        q=query,
        orderBy="modifiedTime desc",
        fields="files(id,name,modifiedTime,webViewLink)",
        pageSize=100,
    ).execute()
    return results.get("files", [])


# --- Per-sheet refresh ---------------------------------------------

def refresh_one(drive_service, sheets_service, tracker):
    """Refresh dashboard tab + visual formatting on one tracker sheet.

    Returns the post-refresh modifiedTime (which we save as the new baseline,
    so we don't trigger ourselves on the next poll).
    """
    project_name = extract_project_name(tracker["name"])
    phases = read_master_schedule(sheets_service, tracker["id"])
    metrics = compute_dashboard_metrics(phases)
    dashboard_sheet_id = ensure_dashboard_tab(sheets_service, tracker["id"])
    write_dashboard(sheets_service, tracker["id"], dashboard_sheet_id,
                    metrics, project_name)
    apply_visual_formatting(sheets_service, tracker["id"])

    # Re-fetch modifiedTime to capture the timestamp produced by our own writes
    fresh = drive_service.files().get(
        fileId=tracker["id"], fields="modifiedTime"
    ).execute()
    return fresh["modifiedTime"]


# --- Main loop body (single invocation) ----------------------------

def _timeout_handler(signum, frame):
    log(f"ERROR: watcher exceeded {WATCHER_TIMEOUT_SEC} sec — exiting so launchd can respawn.")
    sys.exit(1)


def main():
    # Hard kill if we're not done in 90 sec. Belt-and-suspenders alongside the
    # 45-sec socket timeout — covers anything that's stuck outside a socket.
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(WATCHER_TIMEOUT_SEC)

    try:
        creds = get_credentials()
    except Exception as e:
        log(f"ERROR: Google auth failed: {e}")
        sys.exit(1)

    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)

    try:
        trackers = find_all_trackers(drive_service, DRIVE_FOLDER_PATH)
    except Exception as e:
        log(f"ERROR: could not list trackers: {e}")
        sys.exit(1)

    state = load_state()
    state_changed = False
    refreshed = 0
    errors = 0

    # Track which sheets have disappeared (deleted or renamed out of pattern)
    seen_ids = set()

    for tracker in trackers:
        seen_ids.add(tracker["id"])
        last_seen = state.get(tracker["id"], "")
        current = tracker["modifiedTime"]

        if last_seen == current:
            continue  # No change since we last looked

        # Either brand new or modified — refresh it
        try:
            new_modtime = refresh_one(drive_service, sheets_service, tracker)
            state[tracker["id"]] = new_modtime
            state_changed = True
            refreshed += 1
            project = extract_project_name(tracker["name"])
            log(f"Refreshed: {project} (sheet {tracker['id'][:10]}...)")
        except Exception as e:
            errors += 1
            if errors <= MAX_ERRORS_PER_RUN:
                log(f"ERROR refreshing {tracker['name']}: {e}")
            elif errors == MAX_ERRORS_PER_RUN + 1:
                log(f"...suppressing further error logs this run.")

    # Garbage-collect state for sheets that no longer exist
    stale_ids = set(state.keys()) - seen_ids
    for sid in stale_ids:
        del state[sid]
        state_changed = True
        log(f"Forgot stale sheet: {sid[:10]}...")

    if state_changed:
        save_state(state)

    # Only log activity summary if something actually happened — keeps the
    # log readable when running once per minute for hours
    if refreshed or errors:
        log(f"Pass complete: refreshed={refreshed} errors={errors} "
            f"trackers_seen={len(seen_ids)}")


if __name__ == "__main__":
    main()
