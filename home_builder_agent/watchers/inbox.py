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
import logging
import os
import signal
import socket
import subprocess
import sys
import uuid
from datetime import datetime

from home_builder_agent.classifiers.email import classify_thread
from home_builder_agent.classifiers.invoice import is_invoice_email, extract_invoice_data
from home_builder_agent.classifiers.supplier_email import (
    extract_supplier_data,
    is_supplier_email,
    supplier_payload,
)
from home_builder_agent.config import (
    INBOX_WATCHER_NOTIFY_HIGH,
    INBOX_WATCHER_TIMEOUT_SEC,
    INVOICE_NOTIFY_THRESHOLD,
    FINANCE_FOLDER_PATH,
    FINANCE_PROJECT_NAME,
    WATCHER_MAX_ERRORS_PER_RUN,
    WATCHER_SOCKET_TIMEOUT,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import make_client
from home_builder_agent.core.heartbeat import beat_on_success
from home_builder_agent.observability.json_log import configure_json_logging

logger = logging.getLogger(__name__)
from home_builder_agent.integrations import gmail as gmail_int
from home_builder_agent.integrations import drive as drive_int
from home_builder_agent.integrations.finance import add_invoice_row

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
# Invoice detection + logging (best-effort, never crashes the watcher)
# ---------------------------------------------------------------------

def _find_active_cost_tracker(drive_svc, sheets_svc):
    """Return the sheet_id of the active project's Cost Tracker, or None."""
    try:
        folder_id = drive_int.find_folder_by_path(drive_svc, FINANCE_FOLDER_PATH)
        files = drive_int.find_files_by_name_pattern(
            drive_svc, "Cost Tracker", folder_id,
            mime_type="application/vnd.google-apps.spreadsheet",
        )
        if files:
            return files[0]["id"]
    except Exception:
        pass
    return None


def _handle_possible_invoice(gmail_svc, client, drive_svc, sheets_svc,
                              summary, thread_id):
    """If the email looks like an invoice, extract data and log to Cost Tracker.

    Returns True if an invoice was detected and logged, False otherwise.
    All exceptions are swallowed — invoice detection must never crash the watcher.
    """
    try:
        # Quick rule-based check — no API call
        if not is_invoice_email(summary["subject"], summary["snippet"]):
            return False

        # Fetch the full message body (uses newest message in thread)
        msg_ids = gmail_int.get_thread_message_ids(gmail_svc, thread_id)
        if not msg_ids:
            return False
        body = gmail_int.get_message_body(gmail_svc, msg_ids[-1])

        # Extract structured invoice data with Sonnet
        invoice_data, _usage = extract_invoice_data(
            client,
            from_name=summary["from_name"],
            from_email=summary["from_email"],
            subject=summary["subject"],
            body_text=body,
        )

        # If no amount could be determined, skip — too noisy to log
        if not invoice_data.get("amount"):
            return False

        # Resolve the active Cost Tracker sheet
        sheet_id = _find_active_cost_tracker(drive_svc, sheets_svc)
        if not sheet_id:
            log(f"INVOICE | {summary['from_name']} | "
                f"${invoice_data['amount']:.0f} | no Cost Tracker found — not logged")
            return True

        # Write to Invoices tab
        add_invoice_row(sheets_svc, sheet_id, {
            "invoice_number": invoice_data.get("invoice_number", ""),
            "vendor":         invoice_data.get("vendor", summary["from_name"]),
            "description":    invoice_data.get("description", summary["subject"][:120]),
            "amount":         invoice_data.get("amount", 0),
            "invoice_date":   invoice_data.get("invoice_date", ""),
            "due_date":       invoice_data.get("due_date", ""),
            "status":         "Received",
            "job":            invoice_data.get("job_hint", FINANCE_PROJECT_NAME),
            "source":         "Email",
            "notes":          f"Thread: {thread_id}",
        })

        amount = invoice_data["amount"]
        log(f"INVOICE | {invoice_data.get('vendor', summary['from_name'])} | "
            f"${amount:,.0f} | logged to Cost Tracker Invoices tab")

        # Fire macOS notification for large invoices
        if amount >= INVOICE_NOTIFY_THRESHOLD and INBOX_WATCHER_NOTIFY_HIGH:
            notify_macos(
                f"Invoice: ${amount:,.0f}",
                f"{invoice_data.get('vendor', summary['from_name'])}: "
                f"{invoice_data.get('description', summary['subject'])[:80]}",
            )

        return True

    except Exception as e:
        log(f"WARNING: invoice detection failed for {summary.get('subject','')[:60]}: {e}")
        return False


def _handle_possible_supplier_email(client, summary: dict) -> bool:
    """Phase 2 #11 — V1 feeder for Vendor Intelligence.

    The heuristic gate (is_supplier_email) already passed; here we
    extract structured data via Haiku and emit an Event into
    home_builder.event so the iOS notification feed surfaces it.

    Returns True if an Event was emitted, False otherwise.
    """
    extracted = extract_supplier_data(client, summary)
    if not extracted:
        return False  # heuristic false-positive or parse failure

    # Local imports to keep cold-import-time of the watcher unchanged
    # (these only matter on the supplier branch).
    from home_builder_agent.scheduling.events import make_event
    from home_builder_agent.scheduling.store_postgres import (
        insert_event, upsert_vendor,
    )

    payload = supplier_payload(extracted)
    payload["from_email"] = summary.get("from_email", "")
    payload["thread_subject"] = summary.get("subject", "")[:200]

    # Upsert the vendor row by name and link the Event back to it. The
    # supplier-email watcher is the V1 ingestion path into Vendor
    # Intelligence's Vendor entity; this attaches identity + last-seen.
    vendor_id: str | None = None
    try:
        vendor_id = upsert_vendor(
            name=extracted.get("vendor_name", "") or "",
            vendor_type=extracted.get("vendor_category") or None,
            seen_via_email=True,
        )
    except Exception as e:
        log(f"WARNING: vendor upsert failed for {extracted.get('vendor_name','?')}: {e}")

    event = make_event(
        type=extracted["event_type"],
        severity=extracted["event_severity"],
        vendor_id=vendor_id,
        payload=payload,
        source="supplier-email-watcher",
    )

    try:
        event_id = insert_event(event)
    except Exception as e:
        log(f"WARNING: supplier event insert failed for {summary.get('subject','')[:60]}: {e}")
        return False

    log(f"SUPPLIER | {extracted['event_severity']:>8} | {extracted.get('vendor_name','?')} | "
        f"{extracted.get('summary','')[:80]} | event {event_id[:8]} "
        f"vendor {(vendor_id or 'none')[:8]}")

    if extracted["event_severity"] in ("warning", "critical"):
        notify_macos(
            f"Supplier: {extracted.get('vendor_name','update')}",
            extracted.get("summary", "")[:160],
        )

    return True


# ---------------------------------------------------------------------
# Main loop body (single invocation)
# ---------------------------------------------------------------------

def _timeout_handler(signum, frame):
    log(f"ERROR: inbox watcher exceeded {INBOX_WATCHER_TIMEOUT_SEC} sec — "
        f"exiting so launchd can respawn.")
    sys.exit(1)


@beat_on_success("inbox-watcher", stale_after_seconds=1500)
def main():
    configure_json_logging("hb-inbox-watcher")
    correlation_id = uuid.uuid4().hex
    logger.info("pass_starting", extra={"event": "pass_starting", "correlation_id": correlation_id})

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
        creds = get_credentials()
        from googleapiclient.discovery import build as _goog_build
        drive_svc     = drive_int.drive_service(creds)
        sheets_svc_obj = _goog_build("sheets", "v4", credentials=creds)
    except Exception:
        drive_svc = None
        sheets_svc_obj = None

    try:
        my_email = gmail_int.get_my_email(gmail_svc)
    except Exception as e:
        log(f"ERROR: could not fetch user email: {e}")
        sys.exit(1)

    classified = 0
    high = 0
    invoices_logged = 0
    supplier_events = 0
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

        # Invoice detection runs alongside (not instead of) urgency classification
        if drive_svc and sheets_svc_obj:
            if _handle_possible_invoice(gmail_svc, client, drive_svc,
                                        sheets_svc_obj, summary, tid):
                invoices_logged += 1

        # Supplier email detection — Phase 2 #11 V1 feeder for Vendor Intelligence.
        # Heuristic gate cheap; LLM extraction only runs on positive matches.
        if is_supplier_email(summary):
            try:
                if _handle_possible_supplier_email(client, summary):
                    supplier_events += 1
            except Exception as e:
                errors += 1
                if errors <= WATCHER_MAX_ERRORS_PER_RUN:
                    log(f"ERROR supplier extract {summary['subject'][:50]}: {e}")

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

    if classified or errors or invoices_logged or supplier_events:
        log(f"Pass complete: classified={classified} high={high} "
            f"invoices={invoices_logged} supplier={supplier_events} "
            f"errors={errors} new_threads={len(thread_ids)}")

    logger.info(
        "pass_complete",
        extra={
            "event": "pass_complete",
            "correlation_id": correlation_id,
            "classified": classified,
            "high_urgency": high,
            "invoices_logged": invoices_logged,
            "supplier_events": supplier_events,
            "errors": errors,
            "new_threads": len(thread_ids),
        },
    )


if __name__ == "__main__":
    main()
