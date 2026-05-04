"""view_models.py — engine projections for renderers.

Implements the view-model contract from canonical-data-model.md
§ View-model contract. The engine computes; renderers consume.

Six view types defined in the canonical model:
  - master           — full Gantt-equivalent timeline (engine.Schedule.to_dict)
  - daily            — today across all active projects
  - weekly           — next 7 days
  - monthly          — next 30 days + % completion vs plan
  - checklist-gates  — open phases + checklist state (later — needs Checklist entity)
  - notification-feed — open Events sorted by severity (later — needs Event store)

V1 ships master + daily + weekly + monthly. Checklist gates and notifications
arrive when their backing entities are wired up.

Naming convention (canonical-data-model.md § Naming conventions):
  - snake_case for field names
  - lowercase-hyphenated enums (`not-started`, `in-progress`)
  - Entity refs end with `_id`
"""

from __future__ import annotations

from datetime import date, timedelta

from home_builder_agent.scheduling.engine import Phase, Schedule
from home_builder_agent.scheduling.lead_times import DropDeadDate


# ---------------------------------------------------------------------------
# Master view
# ---------------------------------------------------------------------------

def project_master_view(
    schedule: Schedule,
    drop_dead_dates: list[DropDeadDate] | None = None,
) -> dict:
    """Master view-model: full Gantt-equivalent timeline + drop-dead overlay.

    This is the canonical 'master' projection. Aligns drop-dead dates to the
    phases they precede so renderers can show "phase X is preceded by Y
    drop-dead dates."
    """
    drop_dead_dates = drop_dead_dates or []

    # Map drop-dead dates to their install phase
    by_install_phase: dict[str, list[DropDeadDate]] = {}
    for dd in drop_dead_dates:
        by_install_phase.setdefault(dd.install_phase_name, []).append(dd)

    phases_payload = []
    for p in schedule.phases:
        d = p.to_dict()
        d["drop_dead_dates"] = [
            dd.to_dict() for dd in by_install_phase.get(p.name, [])
        ]
        phases_payload.append(d)

    return {
        "view_type": "master",
        "project_id": schedule.project_id,
        "project_name": schedule.project_name,
        "estimated_completion_date": schedule.estimated_completion_date.isoformat(),
        "target_completion_date": (
            schedule.target_completion_date.isoformat()
            if schedule.target_completion_date else None
        ),
        "target_framing_start_date": (
            schedule.target_framing_start_date.isoformat()
            if schedule.target_framing_start_date else None
        ),
        "phases": phases_payload,
        "milestones": [m.to_dict() for m in schedule.milestones],
        "drop_dead_dates": [dd.to_dict() for dd in drop_dead_dates],
    }


# ---------------------------------------------------------------------------
# Daily view
# ---------------------------------------------------------------------------

def daily_view(
    schedules: list[Schedule],
    drop_dead_by_project: dict[str, list[DropDeadDate]] | None = None,
    today: date | None = None,
) -> dict:
    """Daily view-model: what's happening TODAY across all active projects.

    Items per canonical-data-model.md § Daily view: tasks, deliveries,
    inspections, drop-dead dates hitting today, no-show flags. V1 surfaces:
      - Active phase per project (today is in [start, end])
      - Drop-dead dates hitting today
    Future V1.x adds Deliveries, Inspections, no-shows from Event store.
    """
    if today is None:
        today = date.today()
    drop_dead_by_project = drop_dead_by_project or {}

    projects_payload = []
    for sched in schedules:
        active = [
            p for p in sched.phases
            if p.planned_start_date <= today <= p.planned_end_date
        ]
        items = []
        for p in active:
            day_n = (today - p.planned_start_date).days + 1
            items.append({
                "kind": "phase-active",
                "phase_id": p.id,
                "phase_name": p.name,
                "day_n": day_n,
                "of_total": p.duration_days,
                "tap_action": f"phase:{p.id}",
            })

        # Drop-dead dates hitting today for this project
        for dd in drop_dead_by_project.get(sched.project_id, []):
            if dd.drop_dead_date == today:
                items.append({
                    "kind": "drop-dead",
                    "material_category": dd.material_category,
                    "install_phase_name": dd.install_phase_name,
                    "install_date": dd.install_date.isoformat(),
                    "lead_time_days": dd.lead_time_days,
                    "tap_action": f"drop-dead:{dd.material_category}",
                })

        if items:
            projects_payload.append({
                "project_id": sched.project_id,
                "project_name": sched.project_name,
                "items": items,
            })

    return {
        "view_type": "daily",
        "date": today.isoformat(),
        "projects": projects_payload,
    }


# ---------------------------------------------------------------------------
# Weekly view
# ---------------------------------------------------------------------------

def weekly_view(
    schedules: list[Schedule],
    drop_dead_by_project: dict[str, list[DropDeadDate]] | None = None,
    today: date | None = None,
) -> dict:
    """Weekly view-model: next 7 days across all projects.

    Items per canonical-data-model.md § Weekly view: tasks this week, drop-dead
    dates this week, milestone meetings, weather summary (when wired in).
    V1 surfaces phases active this week + drop-dead dates this week.
    """
    if today is None:
        today = date.today()
    drop_dead_by_project = drop_dead_by_project or {}

    window_end = today + timedelta(days=7)

    projects_payload = []
    for sched in schedules:
        # Phases that overlap the 7-day window
        active_or_starting = [
            p for p in sched.phases
            if not (p.planned_end_date < today or p.planned_start_date > window_end)
        ]
        items = []
        for p in active_or_starting:
            items.append({
                "kind": "phase",
                "phase_id": p.id,
                "phase_name": p.name,
                "planned_start_date": p.planned_start_date.isoformat(),
                "planned_end_date": p.planned_end_date.isoformat(),
                "tap_action": f"phase:{p.id}",
            })

        # Drop-dead dates this week
        for dd in drop_dead_by_project.get(sched.project_id, []):
            if today <= dd.drop_dead_date <= window_end:
                items.append({
                    "kind": "drop-dead",
                    "material_category": dd.material_category,
                    "drop_dead_date": dd.drop_dead_date.isoformat(),
                    "install_phase_name": dd.install_phase_name,
                    "tap_action": f"drop-dead:{dd.material_category}",
                })

        if items:
            projects_payload.append({
                "project_id": sched.project_id,
                "project_name": sched.project_name,
                "items": items,
            })

    return {
        "view_type": "weekly",
        "date_window_start": today.isoformat(),
        "date_window_end": window_end.isoformat(),
        "projects": projects_payload,
    }


# ---------------------------------------------------------------------------
# Monthly view
# ---------------------------------------------------------------------------

def monthly_view(
    schedules: list[Schedule],
    drop_dead_by_project: dict[str, list[DropDeadDate]] | None = None,
    today: date | None = None,
) -> dict:
    """Monthly view-model: next 30 days + % completion vs plan per project.

    Per canonical-data-model.md § Monthly view, includes earned-time-style
    completion percentage (phases complete weighted by duration).
    """
    if today is None:
        today = date.today()
    drop_dead_by_project = drop_dead_by_project or {}

    window_end = today + timedelta(days=30)
    projects_payload = []

    for sched in schedules:
        total_days = sum(p.duration_days for p in sched.phases) or 1
        completed_days = sum(
            p.duration_days for p in sched.phases if p.status == "complete"
        )
        in_progress_days = sum(
            p.duration_days for p in sched.phases if p.status == "in-progress"
        )
        pct_complete = round(
            (completed_days + in_progress_days * 0.5) / total_days * 100, 1
        )

        next_drop_dead = next(
            (dd for dd in drop_dead_by_project.get(sched.project_id, [])
             if dd.drop_dead_date >= today),
            None,
        )

        # Phases overlapping the 30-day window
        phases_in_window = [
            {
                "phase_id": p.id,
                "phase_name": p.name,
                "planned_start_date": p.planned_start_date.isoformat(),
                "planned_end_date": p.planned_end_date.isoformat(),
                "status": p.status,
            }
            for p in sched.phases
            if not (p.planned_end_date < today or p.planned_start_date > window_end)
        ]

        projects_payload.append({
            "project_id": sched.project_id,
            "project_name": sched.project_name,
            "pct_complete_vs_plan": pct_complete,
            "estimated_completion_date": sched.estimated_completion_date.isoformat(),
            "next_drop_dead_date": (
                next_drop_dead.drop_dead_date.isoformat() if next_drop_dead else None
            ),
            "next_drop_dead_material": (
                next_drop_dead.material_category if next_drop_dead else None
            ),
            "phases_in_window": phases_in_window,
        })

    return {
        "view_type": "monthly",
        "date_window_start": today.isoformat(),
        "date_window_end": window_end.isoformat(),
        "projects": projects_payload,
    }
