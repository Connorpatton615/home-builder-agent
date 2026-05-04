"""schedule_agent.py — hb-schedule CLI for the Scheduling Engine.

CLI:
  hb-schedule "<project name>" --target-completion YYYY-MM-DD
  hb-schedule "<project name>" --target-framing-start YYYY-MM-DD
  hb-schedule "<project name>" --target-completion YYYY-MM-DD --view daily
  hb-schedule "<project name>" --target-completion YYYY-MM-DD --json

Examples:
  hb-schedule "Pelican Point" --target-completion 2026-12-15
  hb-schedule "Magnolia Bay" --target-framing-start 2026-06-01 --view monthly
  hb-schedule "Pelican Point" --target-completion 2026-12-15 --view daily --json

This is a pure-engine CLI for v0 — no Tracker reads, no Drive writes.
Takes a project name + an anchor date, returns the schedule (master view by
default, or daily/weekly/monthly with --view). Use --json for machine-
readable output suitable for piping into the iOS shell's backend later.

Cost: $0/run. No Claude calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

from home_builder_agent.scheduling.engine import (
    Schedule,
    schedule_from_target_completion,
    schedule_from_target_framing_start,
)
from home_builder_agent.scheduling.lead_times import compute_drop_dead_dates
from home_builder_agent.scheduling.phases import PHASE_TEMPLATES
from home_builder_agent.scheduling.view_models import (
    daily_view,
    monthly_view,
    project_master_view,
    weekly_view,
)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _slugify(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


# ---------------------------------------------------------------------------
# Pretty terminal output
# ---------------------------------------------------------------------------

def _print_master(schedule: Schedule, drop_deads: list) -> None:
    print(f"\n{'='*70}")
    print(f"MASTER SCHEDULE — {schedule.project_name}")
    print(f"{'='*70}")
    if schedule.target_completion_date:
        print(f"Target completion:  {schedule.target_completion_date}")
    if schedule.target_framing_start_date:
        print(f"Target framing:     {schedule.target_framing_start_date}")
    print(f"Estimated complete: {schedule.estimated_completion_date}")
    print(f"Total phases:       {len(schedule.phases)}\n")

    print(f"  {'#':>2}  {'Phase':<30} {'Start':>10}  {'End':>10}  {'Days':>4}")
    print(f"  {'-'*2}  {'-'*30} {'-'*10}  {'-'*10}  {'-'*4}")
    for p in schedule.phases:
        print(
            f"  {p.sequence_index:>2}. {p.name:<30} "
            f"{p.planned_start_date}  {p.planned_end_date}  {p.duration_days:>4}"
        )

    if schedule.milestones:
        print(f"\n  Milestones:")
        for m in schedule.milestones:
            print(f"    {m.planned_date}  {m.name}")

    if drop_deads:
        print(f"\n  Drop-dead order dates ({len(drop_deads)}):")
        for dd in drop_deads:
            print(
                f"    {dd.drop_dead_date}  order {dd.material_category:<12} "
                f"(lead {dd.lead_time_days}d, +{dd.safety_buffer_days}d buffer) "
                f"→ install starts {dd.install_date} ({dd.install_phase_name})"
            )

    print(f"\n{'='*70}\n")


def _print_view_payload(payload: dict) -> None:
    """Pretty-print a daily/weekly/monthly view-model."""
    view_type = payload.get("view_type", "?")
    print(f"\n{'='*70}")
    print(f"{view_type.upper()} VIEW")
    print(f"{'='*70}")
    if view_type == "daily":
        print(f"Date: {payload.get('date')}\n")
    else:
        print(
            f"Window: {payload.get('date_window_start')} → "
            f"{payload.get('date_window_end')}\n"
        )

    if not payload.get("projects"):
        print("  (no items in this window)")
        print(f"\n{'='*70}\n")
        return

    for project in payload["projects"]:
        print(f"  {project['project_name']}:")
        if view_type == "monthly":
            print(f"    {project['pct_complete_vs_plan']}% complete vs plan")
            print(f"    Estimated completion: {project['estimated_completion_date']}")
            if project.get("next_drop_dead_material"):
                print(
                    f"    Next drop-dead: {project['next_drop_dead_date']} "
                    f"({project['next_drop_dead_material']})"
                )
            for ph in project.get("phases_in_window", []):
                print(
                    f"      • {ph['phase_name']:<30} "
                    f"{ph['planned_start_date']} → {ph['planned_end_date']} "
                    f"[{ph['status']}]"
                )
        else:
            for item in project.get("items", []):
                kind = item.get("kind")
                if kind == "phase-active":
                    print(
                        f"    • Active: {item['phase_name']} "
                        f"(day {item['day_n']} of {item['of_total']})"
                    )
                elif kind == "phase":
                    print(
                        f"    • Phase: {item['phase_name']} "
                        f"({item['planned_start_date']} → {item['planned_end_date']})"
                    )
                elif kind == "drop-dead":
                    if "drop_dead_date" in item:
                        print(
                            f"    • Drop-dead {item['drop_dead_date']}: order "
                            f"{item['material_category']} "
                            f"({item['install_phase_name']} install)"
                        )
                    else:
                        print(
                            f"    • Drop-dead TODAY: order "
                            f"{item['material_category']} "
                            f"(install {item['install_date']})"
                        )
        print()
    print(f"{'='*70}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run the Scheduling Engine for a project."
    )
    parser.add_argument("project_name", help="Project name (e.g. 'Pelican Point')")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--target-completion", dest="target_completion",
        help="Target completion date (YYYY-MM-DD)",
    )
    grp.add_argument(
        "--target-framing-start", dest="target_framing_start",
        help="Target framing-start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--view", choices=["master", "daily", "weekly", "monthly"], default="master",
        help="Which view-model to render (default: master)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output the view-model as JSON instead of pretty terminal display",
    )
    args = parser.parse_args()

    project_id = _slugify(args.project_name)

    # Build the schedule from the target anchor
    if args.target_completion:
        schedule = schedule_from_target_completion(
            project_id=project_id,
            project_name=args.project_name,
            target_completion_date=_parse_date(args.target_completion),
        )
    else:
        schedule = schedule_from_target_framing_start(
            project_id=project_id,
            project_name=args.project_name,
            target_framing_start_date=_parse_date(args.target_framing_start),
        )

    drop_deads = compute_drop_dead_dates(schedule)

    # Render the requested view
    if args.view == "master":
        if args.json:
            print(json.dumps(project_master_view(schedule, drop_deads), indent=2))
        else:
            _print_master(schedule, drop_deads)
        return

    drop_dead_by_project = {schedule.project_id: drop_deads}
    schedules = [schedule]

    if args.view == "daily":
        payload = daily_view(schedules, drop_dead_by_project)
    elif args.view == "weekly":
        payload = weekly_view(schedules, drop_dead_by_project)
    elif args.view == "monthly":
        payload = monthly_view(schedules, drop_dead_by_project)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_view_payload(payload)


if __name__ == "__main__":
    main()
