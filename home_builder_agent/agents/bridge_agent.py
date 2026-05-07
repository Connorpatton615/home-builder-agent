"""bridge_agent.py — hb-bridge CLI: sync Drive Trackers into Postgres.

The agent that bridges Phase 1's source-of-truth (Drive Tracker sheets,
where Chad and his existing 14 agents have been writing for months) into
Phase 2's engine state store (Supabase Postgres, what the iOS app reads).

CLI:
  hb-bridge                              Sync all Trackers in DRIVE_FOLDER_PATH
  hb-bridge "Whitfield"                  Sync just the Whitfield project (substring match)
  hb-bridge --dry-run                    Read + compute, rollback. No DB writes.
  hb-bridge --json                       Emit JSON results for log piping
  hb-bridge --list                       List all Trackers in Drive without syncing

Idempotent: re-running against the same Tracker UPDATES existing rows.
Match key: home_builder.project.drive_folder_id (the stable Drive ID,
not the project name).

Cost: $0/run. Pure SQL + Drive read. No Claude calls.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timezone

from home_builder_agent.config import DRIVE_FOLDER_PATH
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.integrations import drive, sheets
from home_builder_agent.observability.json_log import configure_json_logging
from home_builder_agent.scheduling.bridge import (
    TrackerSyncResult,
    sync_all_trackers,
    sync_tracker,
)

logger = logging.getLogger(__name__)


def _print_pretty(results: list[TrackerSyncResult]) -> None:
    if not results:
        print("\n  (no Trackers matched)\n")
        return

    grand_inserted = 0
    grand_updated = 0
    grand_unchanged = 0
    grand_error = 0

    for r in results:
        print(f"\n{'='*64}")
        print(f"TRACKER — {r.tracker_name}")
        print(f"{'='*64}")
        if r.error:
            print(f"  🚨 ERROR: {r.error}\n")
            continue
        print(f"  project_name:      {r.project_name}")
        print(f"  drive_folder_id:   {r.drive_folder_id or '(none)'}")
        print(f"  project_id:        {r.project_id}")
        print(f"  project_outcome:   {r.project_outcome}")
        print(f"  phases scanned:    {r.phase_count}")

        counts = r.summary_counts()
        grand_inserted += counts.get("inserted", 0)
        grand_updated  += counts.get("updated", 0)
        grand_unchanged += counts.get("unchanged", 0)
        grand_error    += counts.get("error", 0)

        print(f"  phase outcomes:")
        print(f"    ➕ inserted:   {counts.get('inserted', 0)}")
        print(f"    ✏️  updated:    {counts.get('updated', 0)}")
        print(f"    ⏸  unchanged:  {counts.get('unchanged', 0)}")
        print(f"    🚨 errors:     {counts.get('error', 0)}")

        if counts.get("error", 0) > 0:
            print(f"\n  Per-phase detail (errors only):")
            for p in r.phase_outcomes:
                if p.outcome == "error":
                    print(f"    🚨 #{p.sequence_index} {p.name}: {p.notes}")

    print(f"\n{'='*64}")
    print(f"GRAND TOTAL")
    print(f"{'='*64}")
    print(f"  Trackers scanned:    {len(results)}")
    print(f"  Phases inserted:     {grand_inserted}")
    print(f"  Phases updated:      {grand_updated}")
    print(f"  Phases unchanged:    {grand_unchanged}")
    print(f"  Phases errored:      {grand_error}")
    print(f"{'='*64}\n")


def _list_trackers_only(drive_svc) -> None:
    trackers = drive.find_all_trackers(drive_svc, DRIVE_FOLDER_PATH)
    print(f"\n{'='*64}")
    print(f"ALL TRACKERS in {' / '.join(DRIVE_FOLDER_PATH)}")
    print(f"{'='*64}")
    if not trackers:
        print("  (none found)")
    else:
        for t in trackers:
            project = drive.extract_project_name(t["name"])
            print(f"  • {project:<40} (sheet id: {t['id']})")
    print(f"{'='*64}\n")


def main():
    configure_json_logging("hb-bridge")
    correlation_id = uuid.uuid4().hex
    logger.info("pass_starting", extra={"event": "pass_starting", "correlation_id": correlation_id})

    parser = argparse.ArgumentParser(
        description="Sync Drive Tracker sheets into Postgres engine state."
    )
    parser.add_argument(
        "name_filter", nargs="?", default=None,
        help="Optional substring to match project name (case-insensitive)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Read + compute, but rollback. No DB writes.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON results instead of pretty terminal output",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List all Trackers in Drive without syncing",
    )
    args = parser.parse_args()

    print("Authenticating...")
    creds = get_credentials()
    drive_svc = drive.drive_service(creds)
    sheets_svc = sheets.sheets_service(creds)

    if args.list:
        _list_trackers_only(drive_svc)
        return

    print(f"Scanning Trackers in {' / '.join(DRIVE_FOLDER_PATH)}...")
    if args.name_filter:
        print(f"  Filter: '{args.name_filter}' (substring, case-insensitive)")
    if args.dry_run:
        print("  Mode: DRY RUN (no DB writes)")

    results = sync_all_trackers(
        drive_svc, sheets_svc, DRIVE_FOLDER_PATH,
        dry_run=args.dry_run,
        name_filter=args.name_filter,
    )

    if args.json:
        out = []
        for r in results:
            out.append({
                "tracker_name": r.tracker_name,
                "project_name": r.project_name,
                "drive_folder_id": r.drive_folder_id,
                "project_id": r.project_id,
                "project_outcome": r.project_outcome,
                "phase_count": r.phase_count,
                "summary": r.summary_counts(),
                "phase_outcomes": [
                    {"sequence_index": p.sequence_index, "name": p.name,
                     "outcome": p.outcome, "notes": p.notes}
                    for p in r.phase_outcomes
                ],
                "error": r.error,
            })
        print(json.dumps({
            "started_at": datetime.now(timezone.utc).isoformat(),
            "tracker_count": len(results),
            "results": out,
        }, indent=2))
        return

    _print_pretty(results)

    # Exit non-zero if any tracker failed
    any_error = any(r.error or any(p.outcome == "error" for p in r.phase_outcomes)
                    for r in results)

    logger.info(
        "pass_complete",
        extra={
            "event": "pass_complete",
            "correlation_id": correlation_id,
            "tracker_count": len(results),
            "any_error": any_error,
            "dry_run": bool(args.dry_run),
            "filter": args.name_filter,
        },
    )
    sys.exit(2 if any_error else 0)


if __name__ == "__main__":
    main()
