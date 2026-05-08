"""notification_triggers.py — engine-side automatic Event emitters.

The scheduling engine fires Events at canonical thresholds without user
input. This module wires the triggers per
scheduling-engine.md § Notification Triggers.

Six triggers in spec; V1 ships the engine-only one:

  selection-deadline    drop-dead order date approaching     ← THIS MODULE
  weather-delay         forecast threshold breached            (weather subsystem)
  sub-no-show           no check-in by 9am                     (UserAction-driven)
  material-no-show      delivery past scheduled date           (UserAction-driven)
  inspection-failure    inspection result Fail/Reinspect       (UserAction-driven)
  schedule-slip         estimated completion moved             (engine-emitted, V2)

Run on a schedule (typically daily at 7 AM via launchd, after the
morning brief) — fires Events idempotently so re-running doesn't create
duplicates: if an open or acknowledged Event for the same
(project_id, type, category) already exists in the last 30 days, skip.

Severity bands per drop-dead urgency:

  OVERDUE        → critical    (already past — order now or accept slip)
  ORDER NOW      → critical    (drop-dead is today)
  THIS WEEK      → warning     (drop-dead within 7 days)
  UPCOMING       → info        (drop-dead within procurement window)
  LATER          → no Event    (filtered out — too far away to be actionable)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date

from home_builder_agent.scheduling.events import (
    EventSeverity,
    EventType,
    make_event,
)
from home_builder_agent.scheduling.lead_times import (
    ALERT_BAND_OVERDUE,
    ALERT_BAND_THIS_WEEK,
    ALERT_BAND_TODAY,
    ALERT_BAND_UPCOMING,
    compute_live_procurement_alerts,
)


_BAND_TO_SEVERITY: dict[str, EventSeverity] = {
    ALERT_BAND_OVERDUE:   EventSeverity.CRITICAL,
    ALERT_BAND_TODAY:     EventSeverity.CRITICAL,
    ALERT_BAND_THIS_WEEK: EventSeverity.WARNING,
    ALERT_BAND_UPCOMING:  EventSeverity.INFO,
}


@dataclass
class FireResult:
    """Per-project summary of one selection-deadline trigger pass."""

    project_id: str
    project_name: str | None = None
    fired: int = 0
    skipped_existing: int = 0
    skipped_no_band: int = 0
    alerts_total: int = 0
    error: str | None = None
    fired_categories: list[str] = field(default_factory=list)
    skipped_categories: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "fired": self.fired,
            "skipped_existing": self.skipped_existing,
            "skipped_no_band": self.skipped_no_band,
            "alerts_total": self.alerts_total,
            "error": self.error,
            "fired_categories": self.fired_categories,
            "skipped_categories": self.skipped_categories,
        }


def _existing_open_categories(
    project_id: str,
    *,
    lookback_hours: int,
    conn,
) -> set[str]:
    """Set of (category) strings that already have an open|acknowledged
    selection-deadline Event in the lookback window."""
    from home_builder_agent.scheduling.store_postgres import (
        load_recent_events_for_project,
    )

    recent = load_recent_events_for_project(
        project_id,
        since_hours=lookback_hours,
        status_in=(EventType.SELECTION_DEADLINE.value,)  # filtered below
        if False else ("open", "acknowledged"),
        limit=200,
        conn=conn,
    )
    out: set[str] = set()
    for e in recent:
        if e.type != EventType.SELECTION_DEADLINE.value:
            continue
        cat = (e.payload or {}).get("category")
        if cat:
            out.add(cat)
    return out


def fire_selection_deadlines_for_project(
    project_id: str,
    today: _date | None = None,
    *,
    skip_existing: bool = True,
    dedupe_lookback_hours: int = 30 * 24,
    conn=None,
) -> FireResult:
    """Compute drop-dead dates for a project and emit selection-deadline
    Events for any in OVERDUE / ORDER NOW / THIS WEEK / UPCOMING bands.

    Idempotent when `skip_existing=True` (default): an open or
    acknowledged Event for the same (project, category) within the
    lookback window suppresses re-fire. Set to False for a force-fire
    (e.g., backfill or testing).

    Returns a FireResult summary.
    """
    from home_builder_agent.scheduling.store_postgres import insert_event

    result = FireResult(project_id=project_id)

    try:
        alerts_data = compute_live_procurement_alerts(project_id, today=today)
    except Exception as e:
        result.error = f"compute_live_procurement_alerts failed: {type(e).__name__}: {e}"
        return result

    if not alerts_data:
        result.error = "no schedule in Postgres for this project"
        return result

    result.project_name = alerts_data.get("project_name")
    result.alerts_total = len(alerts_data.get("alerts", []))

    existing_categories: set[str] = set()
    if skip_existing:
        try:
            existing_categories = _existing_open_categories(
                project_id, lookback_hours=dedupe_lookback_hours, conn=conn,
            )
        except Exception as e:
            result.error = f"dedupe lookup failed: {type(e).__name__}: {e}"
            return result

    for alert in alerts_data.get("alerts", []):
        band = alert.get("band")
        category = alert.get("material_category")
        sev_enum = _BAND_TO_SEVERITY.get(band)

        if sev_enum is None:
            result.skipped_no_band += 1
            continue

        if skip_existing and category in existing_categories:
            result.skipped_existing += 1
            result.skipped_categories.append(category)
            continue

        event = make_event(
            type=EventType.SELECTION_DEADLINE,
            severity=sev_enum,
            project_id=project_id,
            payload={
                "category":              category,
                "drop_dead_date":        alert.get("drop_dead_date"),
                "lead_time_days":        alert.get("lead_time_days"),
                "lead_time_source":      "category-default",
                "install_phase_name":    alert.get("install_phase_name"),
                "install_date":          alert.get("install_date"),
                "warning_window_days":   alerts_data.get("upcoming_window_days"),
                "band":                  band,
                "days_until_drop_dead":  alert.get("days_until_drop_dead"),
            },
            source="scheduling-engine",
        )
        try:
            insert_event(event, conn=conn)
            result.fired += 1
            result.fired_categories.append(category)
        except Exception as e:
            # Don't crash the loop on a single-row insert failure;
            # surface per-project via result.error if it happens repeatedly.
            if not result.error:
                result.error = f"insert_event partial failure: {type(e).__name__}: {e}"

    return result


def fire_selection_deadlines_for_all_projects(
    today: _date | None = None,
    *,
    skip_existing: bool = True,
    dedupe_lookback_hours: int = 30 * 24,
) -> list[FireResult]:
    """Fire across every active project (status != 'archived')."""
    from home_builder_agent.scheduling.store_postgres import load_active_projects

    out: list[FireResult] = []
    try:
        projects = load_active_projects()
    except Exception as e:
        out.append(FireResult(project_id="", error=f"load_active_projects failed: {e}"))
        return out

    for p in projects:
        pid = str(p.get("id"))
        try:
            r = fire_selection_deadlines_for_project(
                project_id=pid,
                today=today,
                skip_existing=skip_existing,
                dedupe_lookback_hours=dedupe_lookback_hours,
            )
        except Exception as e:
            r = FireResult(project_id=pid, error=f"unhandled: {type(e).__name__}: {e}")
        out.append(r)
    return out
