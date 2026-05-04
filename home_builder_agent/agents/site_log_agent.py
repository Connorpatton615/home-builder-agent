"""site_log_agent.py — daily site log entries for legal/insurance documentation.

CLI:
  hb-log "<entry text>"     Append a timestamped entry to the project's site log
  hb-log --view             Open the site log in your browser
  hb-log --tail [N]         Print last N entries to stdout (default 5)

Examples:
  hb-log "framing crew 8 hrs, 2nd floor south wing done, weather clear"
  hb-log "rain delay, no work today"
  hb-log "concrete pour completed - foundation east wing, inspector approved"
  hb-log "subcontractor ABC Plumbing on site 7-3, started rough-in"

Why this exists:
  Construction disputes (delay claims, change-order arguments, insurance
  claims, lien actions) almost always come down to "what happened on what
  day". A timestamped, append-only site log is THE primary record. Chad
  writes one line at the end of each day; if a dispute arises 14 months
  later, the log is the contemporaneous record courts and adjusters trust.

  This agent does NOT rewrite or rephrase Chad's text — that would damage
  the legal value. It just timestamps it and appends to a Drive doc.

Storage:
  One Google Doc per project, in <DRIVE>/Site Logs/<Project Name> - Site Log.
  Append-only — never edits previous entries (idempotency + record integrity).

Cost: $0/run — no Claude calls.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime
from zoneinfo import ZoneInfo

from home_builder_agent.config import (
    DRIVE_FOLDER_PATH,
    SITE_LOGS_DIR,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.integrations import drive, docs as docs_int


# ---------------------------------------------------------------------------
# Site Log doc lookup / creation
# ---------------------------------------------------------------------------

def _site_logs_folder_path() -> list[str]:
    """Site Logs folder lives next to GENERATED TIMELINES."""
    # DRIVE_FOLDER_PATH = [..., "GENERATED TIMELINES"]
    # Strip the last segment, append "Site Logs" instead
    return DRIVE_FOLDER_PATH[:-1] + [SITE_LOGS_DIR]


def _ensure_site_logs_folder(drive_svc) -> str:
    """Find or create the Site Logs folder. Returns its folder ID."""
    path = _site_logs_folder_path()
    parent_path = path[:-1]
    folder_name = path[-1]

    parent_id = drive.find_folder_by_path(drive_svc, parent_path)

    # Look for the Site Logs subfolder
    folders = drive.find_files_by_name_pattern(
        drive_svc, folder_name, parent_id,
        mime_type="application/vnd.google-apps.folder",
    )
    for f in folders:
        if f["name"] == folder_name:
            return f["id"]

    # Create it
    meta = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = drive_svc.files().create(body=meta, fields="id,name").execute()
    print(f"  Created Drive folder: {folder_name}")
    return folder["id"]


def _find_or_create_site_log_doc(drive_svc, docs_svc, folder_id: str, project_name: str) -> dict:
    """Find or create the project's Site Log Google Doc.

    Returns: {"id": ..., "name": ..., "webViewLink": ...}
    """
    doc_name = f"{project_name} — Site Log"

    # Look for an existing doc by name
    candidates = drive.find_files_by_name_pattern(
        drive_svc, doc_name, folder_id,
        mime_type="application/vnd.google-apps.document",
    )
    for f in candidates:
        if f["name"] == doc_name:
            # Hydrate webViewLink
            full = drive_svc.files().get(
                fileId=f["id"], fields="id,name,webViewLink"
            ).execute()
            return full

    # Create the doc with a header
    print(f"  Creating new site log: {doc_name}")
    create_meta = {
        "name": doc_name,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [folder_id],
    }
    new_doc = drive_svc.files().create(
        body=create_meta, fields="id,name,webViewLink"
    ).execute()

    # Seed it with a project header
    header = (
        f"{doc_name}\n\n"
        f"Daily site log for {project_name}.\n"
        f"Append-only. Each entry is timestamped at the time of logging.\n"
        f"This document serves as a contemporaneous record for disputes, "
        f"delay claims, and insurance.\n\n"
        f"{'─' * 60}\n\n"
    )
    docs_int.append_text_to_doc(docs_svc, new_doc["id"], header)
    return new_doc


# ---------------------------------------------------------------------------
# Entry formatting
# ---------------------------------------------------------------------------

def _format_entry(text: str, timestamp: datetime) -> str:
    """Format a log entry. Timestamped, separator-bracketed, Chad's text untouched."""
    day_name = timestamp.strftime("%A")
    date_str = timestamp.strftime("%B %-d, %Y")
    time_str = timestamp.strftime("%-I:%M %p %Z")

    return (
        f"{date_str} ({day_name}) — {time_str}\n"
        f"{text.strip()}\n\n"
        f"{'─' * 60}\n\n"
    )


# ---------------------------------------------------------------------------
# Tail (read recent entries)
# ---------------------------------------------------------------------------

def _read_doc_text(docs_svc, doc_id: str) -> str:
    """Extract plain text from a Google Doc."""
    doc = docs_svc.documents().get(documentId=doc_id).execute()
    out = []
    for elem in doc.get("body", {}).get("content", []):
        para = elem.get("paragraph")
        if not para:
            continue
        for run in para.get("elements", []):
            text_run = run.get("textRun")
            if text_run:
                out.append(text_run.get("content", ""))
    return "".join(out)


def _print_tail(text: str, n: int) -> None:
    """Print last N log entries (entries are separated by long ─ runs)."""
    sep = "─" * 60
    parts = [p.strip() for p in text.split(sep) if p.strip()]

    # First part is always the header preamble (no timestamp). Skip any
    # part that doesn't start with a recognizable date-line.
    import re as _re
    date_pat = _re.compile(
        r"^\s*(January|February|March|April|May|June|July|"
        r"August|September|October|November|December)\s+\d+,\s+\d{4}",
        _re.MULTILINE,
    )
    entries = [p for p in parts if date_pat.search(p)]

    if not entries:
        print("(no entries logged yet)")
        return

    last_n = entries[-n:]
    for entry in last_n:
        print(entry.strip())
        print(sep)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Append a timestamped entry to the project's site log."
    )
    parser.add_argument(
        "text", nargs="*",
        help='Log entry text (e.g. "framing crew 8 hrs, weather clear")'
    )
    parser.add_argument(
        "--view", action="store_true",
        help="Open the site log in your browser instead of logging"
    )
    parser.add_argument(
        "--tail", type=int, nargs="?", const=5, default=None, metavar="N",
        help="Print last N entries (default 5) instead of logging"
    )
    args = parser.parse_args()

    log_text = " ".join(args.text).strip() if args.text else ""

    if not args.view and args.tail is None and not log_text:
        parser.print_help()
        print("\nExamples:")
        print('  hb-log "framing crew 8 hrs, 2nd floor south wing done"')
        print('  hb-log "rain delay, no work today"')
        print("  hb-log --view")
        print("  hb-log --tail 10")
        sys.exit(1)

    print("Authenticating...")
    creds = get_credentials()
    drive_svc = drive.drive_service(creds)
    docs_svc = docs_int.docs_service(creds)

    print("Finding latest Tracker (for project name)...")
    tracker = drive.find_latest_tracker(drive_svc, DRIVE_FOLDER_PATH)
    project_name = drive.extract_project_name(tracker["name"])
    print(f"  Project: {project_name}")

    print("Locating Site Logs folder...")
    folder_id = _ensure_site_logs_folder(drive_svc)

    site_log = _find_or_create_site_log_doc(drive_svc, docs_svc, folder_id, project_name)
    print(f"  Doc: {site_log['name']}")

    # ── --view ────────────────────────────────────────────────────────────
    if args.view:
        url = site_log.get("webViewLink", "")
        if url:
            print(f"\nOpening: {url}")
            try:
                webbrowser.open(url)
            except Exception:
                print(f"(open failed; copy this URL manually: {url})")
        return

    # ── --tail ────────────────────────────────────────────────────────────
    if args.tail is not None:
        n = args.tail
        print(f"\nFetching last {n} entries...")
        text = _read_doc_text(docs_svc, site_log["id"])
        print(f"\n{'='*60}")
        print(f"LAST {n} ENTRIES — {project_name}")
        print(f"{'='*60}\n")
        _print_tail(text, n)
        return

    # ── Default: append a new entry ───────────────────────────────────────
    # Use US Central time (Baldwin County) — consistent with where the work happens
    try:
        tz = ZoneInfo("America/Chicago")
    except Exception:
        tz = None
    now = datetime.now(tz) if tz else datetime.now()

    entry = _format_entry(log_text, now)

    print(f"\nAppending entry...")
    docs_int.append_text_to_doc(docs_svc, site_log["id"], entry)

    print(f"\n{'='*60}")
    print(f"SITE LOG ENTRY APPENDED — {project_name}")
    print(f"{'='*60}")
    print(f"  Timestamp: {now.strftime('%A, %B %-d, %Y at %-I:%M %p %Z')}")
    print(f"  Entry:     {log_text}")
    print(f"  Doc:       {site_log.get('webViewLink', '(no link)')}")
    print()


if __name__ == "__main__":
    main()
