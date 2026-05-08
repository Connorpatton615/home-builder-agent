"""triggers_agent.py — hb-triggers CLI for the engine's automatic Event emitters.

Runs the engine's notification triggers (per scheduling-engine.md
§ Notification Triggers). V1 = selection-deadline only; weather-delay
+ schedule-slip land in subsequent commits as those subsystems wire up.

Idempotent: re-running won't double-emit. The engine dedupes by
(project, category) over a 30-day lookback for selection-deadline.

CLI:
  hb-triggers                            Fire across all active projects
  hb-triggers --project "Whitfield"      One project (substring match)
  hb-triggers --project-id <uuid>        Exact match
  hb-triggers --json                     Structured output for log piping
  hb-triggers --force                    Skip dedupe (testing only)
  hb-triggers --today YYYY-MM-DD         Override "today" (testing/backfill)

Cost: $0/run. Pure Python + Postgres reads/writes. No Claude calls.

launchd cadence: typically daily at 7:05 AM (right after the morning
brief at 6 AM, so the brief sees freshly-fired Events). Plist:
com.chadhomes.notification-triggers.plist (lands when this CLI is in
production).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import date, datetime, timezone

from home_builder_agent.core.heartbeat import beat_on_success
from home_builder_agent.observability.json_log import configure_json_logging
from home_builder_agent.scheduling.notification_triggers import (
    fire_selection_deadlines_for_all_projects,
    fire_selection_deadlines_for_project,
)

logger = logging.getLogger(__name__)


def _resolve_project(name_or_id: str | None):
    """Return (project_id, project_name) for a name-substring or UUID arg."""
    if not name_or_id:
        return None, None

    from home_builder_agent.scheduling.store_postgres import (
        load_project_by_name,
        load_project_by_id,
    )

    # Try UUID-shaped first
    try:
        # Quick UUID validation
        uuid.UUID(name_or_id)
        row = load_project_by_id(name_or_id)
        if row:
            return str(row["id"]), row.get("name")
    except (ValueError, KeyError):
        pass

    row = load_project_by_name(name_or_id)
    if row:
        return str(row["id"]), row.get("name")
    return None, None


def _print_pretty(results: list) -> None:
    print()
    print(f"{'='*64}")
    print(f"NOTIFICATION TRIGGERS — selection-deadline pass")
    print(f"{'='*64}")
    if not results:
        print("(no projects to scan)")
        return

    total_fired = 0
    total_skipped = 0
    for r in results:
        proj = r.project_name or r.project_id
        print(f"\n  {proj}")
        if r.error:
            print(f"    ⚠️  {r.error}")
            continue
        print(f"    fired:            {r.fired}")
        print(f"    skipped_existing: {r.skipped_existing}")
        print(f"    skipped_no_band:  {r.skipped_no_band}")
        print(f"    alerts_total:     {r.alerts_total}")
        if r.fired_categories:
            print(f"    fired:            {', '.join(r.fired_categories)}")
        total_fired += r.fired
        total_skipped += r.skipped_existing

    print(f"\n  TOTAL: fired={total_fired} skipped_existing={total_skipped}")
    print(f"{'='*64}\n")


@beat_on_success("notification-triggers", stale_after_seconds=90000)
def main() -> None:
    configure_json_logging("hb-triggers")
    correlation_id = uuid.uuid4().hex
    logger.info(
        "pass_starting",
        extra={"event": "pass_starting", "correlation_id": correlation_id},
    )

    parser = argparse.ArgumentParser(
        description="Fire engine-side automatic Event triggers (selection-deadline).",
    )
    parser.add_argument(
        "--project", default=None,
        help="Project name substring or UUID (default: scan all active projects).",
    )
    parser.add_argument(
        "--today", default=None,
        help="Override 'today' as YYYY-MM-DD (testing/backfill).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Skip the dedupe lookup — emit Events even if open ones exist for the same category.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a JSON object instead of pretty terminal output.",
    )
    args = parser.parse_args()

    today_override: date | None = None
    if args.today:
        try:
            today_override = date.fromisoformat(args.today)
        except ValueError as e:
            print(f"❌ Invalid --today value: {e}")
            sys.exit(1)

    if args.project:
        project_id, project_name = _resolve_project(args.project)
        if not project_id:
            print(f"❌ No project matched: {args.project!r}")
            sys.exit(1)
        results = [
            fire_selection_deadlines_for_project(
                project_id=project_id,
                today=today_override,
                skip_existing=not args.force,
            )
        ]
    else:
        results = fire_selection_deadlines_for_all_projects(
            today=today_override,
            skip_existing=not args.force,
        )

    total_fired = sum(r.fired for r in results)
    total_skipped = sum(r.skipped_existing for r in results)
    error_count = sum(1 for r in results if r.error)

    logger.info(
        "pass_complete",
        extra={
            "event": "pass_complete",
            "correlation_id": correlation_id,
            "projects_scanned": len(results),
            "events_fired": total_fired,
            "skipped_existing": total_skipped,
            "errors": error_count,
            "force": bool(args.force),
        },
    )

    if args.json:
        print(json.dumps({
            "fired_at": datetime.now(timezone.utc).isoformat(),
            "force": bool(args.force),
            "projects": [r.to_dict() for r in results],
        }, indent=2))
    else:
        _print_pretty(results)

    sys.exit(2 if error_count else 0)


if __name__ == "__main__":
    main()
