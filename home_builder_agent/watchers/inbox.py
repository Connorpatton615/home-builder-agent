"""watchers/inbox.py — Active inbox triage.

Polls Gmail for new INBOX messages since the last poll, classifies each
new thread via Haiku, and surfaces high-urgency hits via macOS notification.

Designed to run once per invocation and exit. macOS launchd handles the
5-minute scheduling (see com.chadhomes.inbox-watcher.plist at the repo root).

Why 5 min vs the dashboard watcher's 60 sec:
- Email is not sheet-edit latency. A few minutes' delay is fine.
- Sonnet prompt-cache TTL is 5 min — if/when this watcher grows to call
  Sonnet for richer summarization, the cadence aligns with cache reuse.

State (.inbox_watcher_state.json): just the last historyId we processed.
We don't track per-thread classifications because Gmail's history cursor
already gives us "events since last poll" — each pass naturally classifies
exactly the new arrivals, no dedupe table needed.

First-run behavior: establish the historyId baseline and exit without
classifying. Otherwise we'd dump the entire 7-day history into the
classifier on the very first poll.
"""

import json
import os
import signal
import socket
import subprocess
import sys
from datetime import datetime

from home_builder_agent.classifiers.email import classify_thread
from home_builder_agent.config import (
    INBOX_WATCHER_NOTIFY_HIGH,
    INBOX_WATCHER_TIMEOUT_SEC,
    WATCHER_MAX_ERRORS_PER_RUN,
    WATCHER_SOCKET_TIMEOUT,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import make_client
from home_builder_agent.integrations import gmail as gmail_int

socket.setdefaulttimeout(WATCHER_SOCKET_TIMEOUT)

STATE_FILE = os.path.abspath(".inbox_watcher_state.json")
LOG_FILE = os.path.abspath("inbox_watcher.log")


# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------

def log(msg):
    """Timestamped line to inbox_watcher.log AND stdout."""
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass  # don't crash watcher over a log-write failure


# ---------------------------------------------------------------------
# State (historyId cursor)
# ---------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception as e:
        log(f"WARNING: could not read state file ({e}); starting fresh.")
        return {}


def save_state(state):
    """Persist state atomically (write tmp, rename)."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------
# Notification (macOS, best-effort)
# ---------------------------------------------------------------------

def notify_macos(title, body):
    """Display a macOS notification banner. Failures logged, never raised."""
    title_esc = title.replace("\\", "\\\\").replace('"', '\\"')
    body_esc = body.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{body_esc}" with title "{title_esc}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            timeout=5, capture_output=True, check=False,
        )
    except Exception as e:
        log(f"notification failed: {e}")


# ---------------------------------------------------------------------
# Main loop body (single invocation)
# ---------------------------------------------------------------------

def _timeout_handler(signum, frame):
    log(f"ERROR: inbox watcher exceeded {INBOX_WATCHER_TIMEOUT_SEC} sec — "
        f"exiting so launchd can respawn.")
    sys.exit(1)


def main():
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(INBOX_WATCHER_TIMEOUT_SEC)

    try:
        creds = get_credentials()
    except Exception as e:
        log(f"ERROR: Google auth failed: {e}")
        sys.exit(1)

    gmail_svc = gmail_int.gmail_service(creds)

    state = load_state()
    last_history_id = state.get("last_history_id")

    # First run: establish baseline, classify nothing.
    if not last_history_id:
        try:
            current = gmail_int.get_current_history_id(gmail_svc)
        except Exception as e:
            log(f"ERROR: could not fetch initial historyId: {e}")
            sys.exit(1)
        state["last_history_id"] = current
        save_state(state)
        log(f"Initialized history baseline at {current}; no work first run.")
        return

    # Fetch new INBOX messageAdded events since last poll.
    try:
        thread_ids, latest_history_id, baseline_expired = (
            gmail_int.list_inbox_message_added_since(gmail_svc, last_history_id)
        )
    except Exception as e:
        log(f"ERROR: history.list failed: {e}")
        sys.exit(1)

    if baseline_expired:
        # Gmail discarded the historyId we asked for (>~7 days old).
        # Re-baseline so future polls work; visibility into events in the
        # gap is lost (acceptable — Chad can run hb-inbox manually).
        try:
            current = gmail_int.get_current_history_id(gmail_svc)
        except Exception as e:
            log(f"ERROR: re-baseline failed: {e}")
            sys.exit(1)
        state["last_history_id"] = current
        save_state(state)
        log(f"WARNING: history cursor {last_history_id} was too old; "
            f"re-baselined to {current}.")
        return

    if not thread_ids:
        # Nothing new — advance cursor silently if it moved.
        if latest_history_id and latest_history_id != last_history_id:
            state["last_history_id"] = latest_history_id
            save_state(state)
        return

    # Classify each new thread.
    client = make_client()
    try:
        my_email = gmail_int.get_my_email(gmail_svc)
    except Exception as e:
        log(f"ERROR: could not fetch user email: {e}")
        sys.exit(1)

    classified = 0
    high = 0
    errors = 0

    for tid in thread_ids:
        try:
            summary = gmail_int.get_thread_summary(gmail_svc, tid, my_email)
        except Exception as e:
            errors += 1
            if errors <= WATCHER_MAX_ERRORS_PER_RUN:
                log(f"ERROR fetching thread {tid[:10]}: {e}")
            continue
        if not summary:
            continue

        try:
            classification, _usage = classify_thread(client, summary)
        except Exception as e:
            errors += 1
            if errors <= WATCHER_MAX_ERRORS_PER_RUN:
                log(f"ERROR classifying {summary['subject'][:50]}: {e}")
            continue

        classified += 1
        urgency = classification.get("urgency", "none")
        needs = classification.get("needs_followup", False)

        if urgency == "high":
            high += 1
            log(f"HIGH | {summary['from_name']} | {summary['subject'][:80]}")
            if INBOX_WATCHER_NOTIFY_HIGH:
                notify_macos(
                    "Chad Inbox: HIGH",
                    f"{summary['from_name']}: {summary['subject']}",
                )
        elif needs:
            log(f"{urgency:6} | {summary['from_name']} | "
                f"{summary['subject'][:80]}")

    # Advance cursor.
    state["last_history_id"] = latest_history_id or last_history_id
    save_state(state)

    if classified or errors:
        log(f"Pass complete: classified={classified} high={high} "
            f"errors={errors} new_threads={len(thread_ids)}")


if __name__ == "__main__":
    main()
