"""view_models.py — engine projections for renderers.

Implements the view-model contract from canonical-data-model.md
§ View-model contract. Engine compute uses internal @dataclass types
(engine.Schedule, lead_times.DropDeadDate); this module projects them into
the Pydantic wire types defined in schemas.py at the surface boundary.

Renderers consume Pydantic models (or their `.model_dump()` JSON form).
The shell does not transform the payload.

Six view types defined in the canonical model:
  - master           — full Gantt-equivalent timeline + drop-dead overlay
  - daily            — today across all active projects
  - weekly           — next 7 days
  - monthly          — next 30 days + % completion vs plan
  - checklist-gates  — V2: needs Checklist entity wired up
  - notification-feed — V2: needs Event store wired up

V1 ships master + daily + weekly + monthly. The two V2 view-models have
schemas published (so iOS can pre-generate Codable types) but the engine
projections return empty payloads until their backing entities exist.
"""

from __future__ import annotations

from datetime import date, timedelta

from home_builder_agent.scheduling.engine import Phase, Schedule
from home_builder_agent.scheduling.lead_times import DropDeadDate
from home_builder_agent.scheduling.schemas import (
    ChecklistGatesViewPayload,
    DailyItemKind,
    DailyItemPayload,
    DailyProjectPayload,
    DailyViewPayload,
    DropDeadDatePayload,
    LeadTimeSource,
    MasterPhasePayload,
    MasterViewPayload,
    MilestonePayload,
    MilestoneStatus,
    MonthlyPhaseInWindowPayload,
    MonthlyProjectPayload,
    MonthlyViewPayload,
    NotificationFeedViewPayload,
    PhaseStatus,
    WeeklyItemKind,
    WeeklyItemPayload,
    WeeklyProjectPayload,
    WeeklyViewPayload,
)


# ---------------------------------------------------------------------------
# Internal converters: @dataclass → Pydantic
# ---------------------------------------------------------------------------

def _phase_to_payload(p: Phase) -> MasterPhasePayload:
    """Project an engine Phase into the master-view phase payload (drop-deads
    are appended later by project_master_view)."""
    return MasterPhasePayload(
        id=p.id,
        phase_template_id=p.template.sequence_index,
        name=p.name,
        sequence_index=p.sequence_index,
        status=PhaseStatus(p.status),
        planned_start_date=p.planned_start_date,
        planned_end_date=p.planned_end_date,
        actual_start_date=p.actual_start_date,
        actual_end_date=p.actual_end_date,
        default_duration_days=p.template.default_duration_days,
        duration_days=p.duration_days,
        drop_dead_dates=[],
    )


def _drop_dead_to_payload(dd: DropDeadDate) -> DropDeadDatePayload:
    return DropDeadDatePayload(
        material_category=dd.material_category,
        lead_time_days=dd.lead_time_days,
        lead_time_source=LeadTimeSource(dd.lead_time_source),
        install_phase_name=dd.install_phase_name,
        install_date=dd.install_date,
        safety_buffer_days=dd.safety_buffer_days,
        drop_dead_date=dd.drop_dead_date,
    )


# ---------------------------------------------------------------------------
# Master view
# ---------------------------------------------------------------------------

def project_master_view(
    schedule: Schedule,
    drop_dead_dates: list[DropDeadDate] | None = None,
) -> MasterViewPayload:
    """Master view-model: full Gantt-equivalent timeline + drop-dead overlay."""
    drop_dead_dates = drop_dead_dates or []

    # Map drop-dead dates to their install phase
    by_install_phase: dict[str, list[DropDeadDate]] = {}
    for dd in drop_dead_dates:
        by_install_phase.setdefault(dd.install_phase_name, []).append(dd)

    phases_payload: list[MasterPhasePayload] = []
    for p in schedule.phases:
        payload = _phase_to_payload(p)
        payload.drop_dead_dates = [
            _drop_dead_to_payload(dd) for dd in by_install_phase.get(p.name, [])
        ]
        phases_payload.append(payload)

    return MasterViewPayload(
        project_id=schedule.project_id,
        project_name=schedule.project_name,
        estimated_completion_date=schedule.estimated_completion_date,
        target_completion_date=schedule.target_completion_date,
        target_framing_start_date=schedule.target_framing_start_date,
        phases=phases_payload,
        milestones=[
            MilestonePayload(
                name=m.name,
                planned_date=m.planned_date,
                phase_id=m.phase_id,
                status=MilestoneStatus(m.status),
            )
            for m in schedule.milestones
        ],
        drop_dead_dates=[_drop_dead_to_payload(dd) for dd in drop_dead_dates],
    )


# ---------------------------------------------------------------------------
# Daily view
# ---------------------------------------------------------------------------

def daily_view(
    schedules: list[Schedule],
    drop_dead_by_project: dict[str, list[DropDeadDate]] | None = None,
    today: date | None = None,
) -> DailyViewPayload:
    """Daily view-model: what's happening TODAY across all active projects."""
    if today is None:
        today = date.today()
    drop_dead_by_project = drop_dead_by_project or {}

    projects_payload: list[DailyProjectPayload] = []

    for sched in schedules:
        active = [
            p for p in sched.phases
            if p.planned_start_date <= today <= p.planned_end_date
        ]
        items: list[DailyItemPayload] = []
        for p in active:
            day_n = (today - p.planned_start_date).days + 1
            items.append(
                DailyItemPayload(
                    kind=DailyItemKind.PHASE_ACTIVE,
                    phase_id=p.id,
                    phase_name=p.name,
                    day_n=day_n,
                    of_total=p.duration_days,
                    tap_action=f"phase:{p.id}",
                )
            )

        for dd in drop_dead_by_project.get(sched.project_id, []):
            if dd.drop_dead_date == today:
                items.append(
                    DailyItemPayload(
                        kind=DailyItemKind.DROP_DEAD,
                        material_category=dd.material_category,
                        install_phase_name=dd.install_phase_name,
                        install_date=dd.install_date,
                        lead_time_days=dd.lead_time_days,
                        tap_action=f"drop-dead:{dd.material_category}",
                    )
                )

        if items:
            projects_payload.append(
                DailyProjectPayload(
                    project_id=sched.project_id,
                    project_name=sched.project_name,
                    items=items,
                )
            )

    return DailyViewPayload(date=today, projects=projects_payload)


# ---------------------------------------------------------------------------
# Weekly view
# ---------------------------------------------------------------------------

def weekly_view(
    schedules: list[Schedule],
    drop_dead_by_project: dict[str, list[DropDeadDate]] | None = None,
    today: date | None = None,
) -> WeeklyViewPayload:
    """Weekly view-model: next 7 days across all projects."""
    if today is None:
        today = date.today()
    drop_dead_by_project = drop_dead_by_project or {}

    window_end = today + timedelta(days=7)
    projects_payload: list[WeeklyProjectPayload] = []

    for sched in schedules:
        active_or_starting = [
            p for p in sched.phases
            if not (p.planned_end_date < today or p.planned_start_date > window_end)
        ]
        items: list[WeeklyItemPayload] = []
        for p in active_or_starting:
            items.append(
                WeeklyItemPayload(
                    kind=WeeklyItemKind.PHASE,
                    phase_id=p.id,
                    phase_name=p.name,
                    planned_start_date=p.planned_start_date,
                    planned_end_date=p.planned_end_date,
                    tap_action=f"phase:{p.id}",
                )
            )

        for dd in drop_dead_by_project.get(sched.project_id, []):
            if today <= dd.drop_dead_date <= window_end:
                items.append(
                    WeeklyItemPayload(
                        kind=WeeklyItemKind.DROP_DEAD,
                        material_category=dd.material_category,
                        drop_dead_date=dd.drop_dead_date,
                        install_phase_name=dd.install_phase_name,
                        tap_action=f"drop-dead:{dd.material_category}",
                    )
                )

        # Surface milestones falling in window
        for m in sched.milestones:
            if today <= m.planned_date <= window_end:
                items.append(
                    WeeklyItemPayload(
                        kind=WeeklyItemKind.MILESTONE,
                        milestone_name=m.name,
                        milestone_date=m.planned_date,
                        tap_action=f"milestone:{m.name}",
                    )
                )

        if items:
            projects_payload.append(
                WeeklyProjectPayload(
                    project_id=sched.project_id,
                    project_name=sched.project_name,
                    items=items,
                )
            )

    return WeeklyViewPayload(
        date_window_start=today,
        date_window_end=window_end,
        projects=projects_payload,
    )


# ---------------------------------------------------------------------------
# Monthly view
# ---------------------------------------------------------------------------

def monthly_view(
    schedules: list[Schedule],
    drop_dead_by_project: dict[str, list[DropDeadDate]] | None = None,
    today: date | None = None,
) -> MonthlyViewPayload:
    """Monthly view-model: next 30 days + % completion vs plan per project."""
    if today is None:
        today = date.today()
    drop_dead_by_project = drop_dead_by_project or {}

    window_end = today + timedelta(days=30)
    projects_payload: list[MonthlyProjectPayload] = []

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

        phases_in_window = [
            MonthlyPhaseInWindowPayload(
                phase_id=p.id,
                phase_name=p.name,
                planned_start_date=p.planned_start_date,
                planned_end_date=p.planned_end_date,
                status=PhaseStatus(p.status),
            )
            for p in sched.phases
            if not (p.planned_end_date < today or p.planned_start_date > window_end)
        ]

        projects_payload.append(
            MonthlyProjectPayload(
                project_id=sched.project_id,
                project_name=sched.project_name,
                pct_complete_vs_plan=pct_complete,
                estimated_completion_date=sched.estimated_completion_date,
                next_drop_dead_date=next_drop_dead.drop_dead_date if next_drop_dead else None,
                next_drop_dead_material=next_drop_dead.material_category if next_drop_dead else None,
                phases_in_window=phases_in_window,
            )
        )

    return MonthlyViewPayload(
        date_window_start=today,
        date_window_end=window_end,
        projects=projects_payload,
    )


# ---------------------------------------------------------------------------
# V2 placeholders — schemas exist, projections return empty payloads
# ---------------------------------------------------------------------------

def checklist_gates_view(*_args, **_kwargs) -> ChecklistGatesViewPayload:
    """V2 — returns empty payload until Checklist entity is wired up."""
    return ChecklistGatesViewPayload(projects=[])


def notification_feed_view(*_args, **_kwargs) -> NotificationFeedViewPayload:
    """V2 — returns empty payload until Event store is wired up."""
    return NotificationFeedViewPayload(items=[])
