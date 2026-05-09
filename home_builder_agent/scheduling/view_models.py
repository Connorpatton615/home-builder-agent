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

from datetime import datetime as _datetime, timezone as _timezone

from home_builder_agent.scheduling.checklists import Checklist, ChecklistItem
from home_builder_agent.scheduling.engine import Phase, Schedule
from home_builder_agent.scheduling.events import Event, click_action_for
from home_builder_agent.scheduling.lead_times import DropDeadDate
from home_builder_agent.scheduling.schemas import (
    ChecklistGatesProjectPayload,
    ChecklistGatesViewPayload,
    ChecklistItemPayload,
    ChecklistPayload,
    DailyItemKind,
    DailyItemPayload,
    DailyProjectPayload,
    DailyViewPayload,
    DraftActionPayload,
    DraftKind,
    DraftStatus,
    DropDeadDatePayload,
    LeadTimeSource,
    MasterPhasePayload,
    MasterViewPayload,
    MilestonePayload,
    MilestoneStatus,
    MonthlyPhaseInWindowPayload,
    MonthlyProjectPayload,
    MonthlyViewPayload,
    MorningDropDeadItemPayload,
    MorningDropDeadsPayload,
    MorningJudgmentQueuePayload,
    MorningOvernightEventsPayload,
    MorningTodayItemPayload,
    MorningTodayOnSitePayload,
    MorningViewPayload,
    NotificationFeedViewPayload,
    NotificationItemPayload,
    NotificationStatus,
    PhaseStatus,
    Severity,
    UrgencyBand,
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

def _checklist_item_to_payload(item: ChecklistItem) -> ChecklistItemPayload:
    return ChecklistItemPayload(
        id=item.id,
        category=item.category,
        label=item.label,
        is_complete=item.is_complete,
        completed_by=item.completed_by,
        completed_at=item.completed_at,
        notes=item.notes,
        photo_required=item.photo_required,
        photos=list(item.photos),
        tap_action=f"checklist-item:{item.id}",
        photo_upload_action=(
            f"checklist-item-photo-upload:{item.id}" if item.photo_required else None
        ),
        template_item_id=item.template_item_id,
    )


def _checklist_to_payload(cl: Checklist) -> ChecklistPayload:
    items_by_cat = {
        cat: [_checklist_item_to_payload(i) for i in items]
        for cat, items in cl.items_by_category.items()
    }
    return ChecklistPayload(
        id=cl.id,
        phase_id=cl.phase_id,
        template_version=cl.template_version,
        status=cl.status,
        completed_count=cl.completed_count,
        total_count=cl.total_count,
        items_by_category=items_by_cat,
        template_id=cl.template_id,
    )


def checklist_gates_view(
    schedules: list[Schedule],
    checklists_by_project: dict[str, list[Checklist]] | None = None,
) -> ChecklistGatesViewPayload:
    """Project a per-project list of Checklists into the wire payload.

    `checklists_by_project` is a dict keyed by `Schedule.project_id`. Projects
    without a corresponding entry (or with an empty list) are omitted from
    the payload — empty payloads are degraded-mode behavior, the renderer
    treats "project absent" as "no checklist data yet."
    """
    checklists_by_project = checklists_by_project or {}

    projects: list[ChecklistGatesProjectPayload] = []
    for sched in schedules:
        cl_list = checklists_by_project.get(sched.project_id, [])
        if not cl_list:
            continue
        projects.append(
            ChecklistGatesProjectPayload(
                project_id=sched.project_id,
                project_name=sched.project_name,
                checklists=[_checklist_to_payload(cl) for cl in cl_list],
            )
        )

    return ChecklistGatesViewPayload(projects=projects)


def _event_to_notification_payload(
    event: Event,
    *,
    notification_id: str | None = None,
    now: _datetime | None = None,
) -> NotificationItemPayload:
    """Project an engine Event into the wire NotificationItemPayload.

    `notification_id` is supplied by the persistence layer when the Event
    has been wrapped by an actual Notification row; in v0 (in-app feed
    only, no APNs yet) the projection synthesizes one 1:1 with the event
    so the wire shape is honored.
    """
    age = event.age_seconds(now=now)
    nid = notification_id or f"notif:{event.id}"

    return NotificationItemPayload(
        event_id=event.id,
        notification_id=nid,
        type=event.type,
        severity=Severity(event.severity),
        status=NotificationStatus(event.status),
        summary=event.summary(),
        project_id=event.project_id,
        phase_id=event.phase_id,
        age_seconds=age,
        created_at=event.created_at.date() if hasattr(event.created_at, "date") else event.created_at,
        acknowledged_at=event.acknowledged_at.date() if event.acknowledged_at else None,
        resolved_at=event.resolved_at.date() if event.resolved_at else None,
        acknowledge_action=f"event-acknowledge:{event.id}",
        resolve_action=f"event-resolve:{event.id}",
        click_action=click_action_for(event),
    )


def notification_feed_view(
    events: list[Event] | None = None,
    *,
    notification_ids_by_event: dict[str, str] | None = None,
    now: _datetime | None = None,
) -> NotificationFeedViewPayload:
    """Project a list of engine Events into the notification-feed view payload.

    Caller supplies the events (typically from store_postgres.load_recent_events
    over a project or actor scope). Items are returned in newest-first order.

    `notification_ids_by_event` is optional — when the engine has already
    persisted Notification rows (one per channel), the caller can pass the
    in-app surface's notification_id per event. Without it, this projection
    synthesizes a stable id per event for the in-app feed (notif:<event_id>).
    """
    events = events or []
    notification_ids_by_event = notification_ids_by_event or {}
    now = now or _datetime.now(_timezone.utc)

    # Newest-first
    sorted_events = sorted(
        events, key=lambda e: e.created_at, reverse=True,
    )

    items = [
        _event_to_notification_payload(
            e,
            notification_id=notification_ids_by_event.get(e.id),
            now=now,
        )
        for e in sorted_events
    ]
    return NotificationFeedViewPayload(items=items)


# ---------------------------------------------------------------------------
# Morning view — Chad's coffee-cup landing
# ---------------------------------------------------------------------------
# Per docs/specs/morning-view-model.md. This projection is the home-builder
# vertical's contribution to the "morning coffee work station" surface;
# the renderer (native Mac, mobile, future surfaces) consumes the
# MorningViewPayload it returns.
#
# Section ordering (1 → 7) is part of the contract — the renderer is
# expected to render top-to-bottom in the order the payload carries.
#
# External-call sections (weather, voice_brief, action_items) are NOT
# computed here — those involve NOAA fetches and Anthropic API calls
# that don't belong in a pure projection. The caller (route handler /
# CLI / cron) precomputes them and passes via kwargs. If omitted, the
# corresponding fields stay None / empty and the renderer handles the
# empty state per spec.


def _drop_dead_urgency_band(dd: DropDeadDate, today: date) -> UrgencyBand:
    """Map a drop-dead date to the morning surface's urgency band.

    Per spec: morning surface only carries OVERDUE / ORDER NOW band
    items (everything else belongs on daily/weekly). The urgency
    enumeration on the morning view is always URGENT for these.
    """
    days_until = (dd.drop_dead_date - today).days
    # OVERDUE or ORDER NOW threshold: <=0 days → already past or due today
    return UrgencyBand.URGENT if days_until <= 0 else UrgencyBand.WATCH


def _phase_active_urgency(p: Phase, today: date) -> tuple[UrgencyBand, str | None]:
    """Per § urgency_band semantics: phase-active urgency is drift from plan.

    V1 rule (no critical-path computation yet):
      calm   → on plan or ahead
      watch  → 1-2 days behind plan
      urgent → 3+ days behind plan

    Returns (band, urgency_reason_chip).
    """
    if today < p.planned_start_date:
        return (UrgencyBand.CALM, None)  # not started yet — calm
    days_into_plan = (today - p.planned_start_date).days + 1
    if p.duration_days <= 0:
        return (UrgencyBand.CALM, None)
    behind = days_into_plan - p.duration_days
    if behind <= 0:
        return (UrgencyBand.CALM, None)
    if behind <= 2:
        return (
            UrgencyBand.WATCH,
            f"{behind} day{'s' if behind != 1 else ''} behind plan",
        )
    return (
        UrgencyBand.URGENT,
        f"{behind} days behind plan",
    )


def _draft_row_to_payload(row: dict, *, now: _datetime | None = None) -> DraftActionPayload:
    """Project a raw home_builder.draft_action row into the wire payload."""
    now = now or _datetime.now(_timezone.utc)
    created_at = row["created_at"]
    age = max(0, int((now - created_at).total_seconds())) if hasattr(created_at, "tzinfo") else 0
    created_date = created_at.date() if hasattr(created_at, "date") else created_at
    decided_date = (
        row["decided_at"].date() if row.get("decided_at") and hasattr(row["decided_at"], "date") else None
    )
    return DraftActionPayload(
        draft_action_id=row["id"],
        project_id=row["project_id"],
        kind=DraftKind(row["kind"]),
        status=DraftStatus(row["status"]),
        originating_agent=row["originating_agent"],
        summary=row["summary"],
        subject_line=row.get("subject_line"),
        from_or_to=row.get("from_or_to"),
        external_ref=row.get("external_ref"),
        age_seconds=age,
        created_at=created_date,
        decided_at=decided_date,
        decided_by=row.get("decided_by"),
        click_action=f"draft:{row['id']}",
    )


def morning_view(
    project_id: str,
    project_name: str,
    *,
    schedule: Schedule | None = None,
    drop_dead_dates: list[DropDeadDate] | None = None,
    overnight_events: list[Event] | None = None,
    pending_drafts: list[dict] | None = None,
    weather=None,                    # MorningWeatherPayload | None — caller-precomputed
    voice_brief=None,                # MorningVoiceBriefPayload | None — caller-precomputed
    action_items: list[str] | None = None,
    today: date | None = None,
    tz: str = "America/Chicago",
    now: _datetime | None = None,
    notification_ids_by_event: dict[str, str] | None = None,
) -> MorningViewPayload:
    """Project the morning view-model payload for Chad's coffee-cup landing.

    Per docs/specs/morning-view-model.md.

    Inputs (caller-supplied; this function does no I/O):
      schedule              The active engine.Schedule for project_id.
                            Used for today_on_site (phase-active items).
                            None → today_on_site has no phase-active items.
      drop_dead_dates       List of DropDeadDate for the project.
                            Filtered to OVERDUE / ORDER NOW band only.
      overnight_events      Events fired in the last ~14h, severity ≥ warning.
                            Caller fetches via load_recent_events_for_project
                            with the right time + severity filters.
      pending_drafts        Raw rows from list_draft_actions_for_project
                            (status='pending'). Most-recent-first.
      weather               Pre-computed MorningWeatherPayload (NOAA fetch +
                            weather_risk_check upstream of this call).
      voice_brief           Pre-computed MorningVoiceBriefPayload (Sonnet
                            call via chad_voice("narrator") upstream).
      action_items          1–5 imperative items, composed in the same
                            Sonnet call as voice_brief.

    Empty-state behavior follows the spec (§ Section ordering):
      - weather omitted entirely if None (renderer skips the section)
      - voice_brief None → renderer handles fallback (synthesis didn't run)
      - judgment_queue, today_on_site, todays_drop_deads always populated
        (count=0 / items=[] is the empty state)
      - overnight_events empty → renderer omits the section
      - action_items empty → renderer handles fallback
    """
    if today is None:
        today = date.today()
    now = now or _datetime.now(_timezone.utc)
    drop_dead_dates = drop_dead_dates or []
    overnight_events = overnight_events or []
    pending_drafts = pending_drafts or []
    notification_ids_by_event = notification_ids_by_event or {}

    # ── Section 3: Judgment queue ──────────────────────────────────────────
    queue_items = [_draft_row_to_payload(r, now=now) for r in pending_drafts]
    judgment_queue = MorningJudgmentQueuePayload(
        count=len(queue_items),
        items=queue_items,
    )

    # ── Section 4: Today on site ───────────────────────────────────────────
    today_items: list[MorningTodayItemPayload] = []
    if schedule is not None:
        for p in schedule.phases:
            if p.planned_start_date <= today <= p.planned_end_date:
                day_n = (today - p.planned_start_date).days + 1
                band, reason = _phase_active_urgency(p, today)
                today_items.append(
                    MorningTodayItemPayload(
                        kind=DailyItemKind.PHASE_ACTIVE,
                        phase_id=p.id,
                        phase_name=p.name,
                        day_n=day_n,
                        of_total=p.duration_days,
                        urgency_band=band,
                        urgency_reason=reason,
                        tap_action=f"phase:{p.id}",
                    )
                )
    today_on_site = MorningTodayOnSitePayload(items=today_items)

    # ── Section 5: Today's drop-deads (OVERDUE + ORDER NOW only) ──────────
    drop_dead_items: list[MorningDropDeadItemPayload] = []
    for dd in drop_dead_dates:
        days_until = (dd.drop_dead_date - today).days
        # OVERDUE = past, ORDER NOW = within next 3 days (heuristic V1)
        if days_until > 3:
            continue
        drop_dead_items.append(
            MorningDropDeadItemPayload(
                material_category=dd.material_category,
                install_phase_name=dd.install_phase_name,
                install_date=dd.install_date,
                drop_dead_date=dd.drop_dead_date,
                lead_time_days=dd.lead_time_days,
                urgency_band=_drop_dead_urgency_band(dd, today),
                tap_action=f"drop-dead:{dd.material_category}",
            )
        )
    todays_drop_deads = MorningDropDeadsPayload(items=drop_dead_items)

    # ── Section 6: Overnight events ───────────────────────────────────────
    sorted_events = sorted(overnight_events, key=lambda e: e.created_at, reverse=True)
    overnight_items = [
        _event_to_notification_payload(
            e,
            notification_id=notification_ids_by_event.get(e.id),
            now=now,
        )
        for e in sorted_events
    ]
    overnight = MorningOvernightEventsPayload(items=overnight_items)

    return MorningViewPayload(
        project_id=project_id,
        project_name=project_name,
        generated_at=now,
        as_of_local_date=today,
        tz=tz,
        weather=weather,
        voice_brief=voice_brief,
        judgment_queue=judgment_queue,
        today_on_site=today_on_site,
        todays_drop_deads=todays_drop_deads,
        overnight_events=overnight,
        action_items=action_items or [],
    )
