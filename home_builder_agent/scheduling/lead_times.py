"""lead_times.py — drop-dead order date computation.

Per Chad's brief and the spec stack:
  drop_dead = install_date − lead_time − safety_buffer

Where:
  - install_date = the Phase.planned_start_date for the phase that consumes
    the material (e.g., windows install during Phase 5 Rough-In MEP, but
    we model windows as needed by Phase 4 Roofing dry-in for envelope).
  - lead_time = vendor SKU/category lead time (days).
  - safety_buffer = default 5 business days (≈ 7 calendar days).

Resolution priority for lead time (per canonical-data-model.md § entity 10):
  SKU-published → vendor-default → category-default → manual-override

V1 fallback: category-default only (Vendor Intelligence not yet live).
The category table reuses PROCUREMENT_LEAD_TIMES from config.py — same
constants, single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from home_builder_agent.config import PROCUREMENT_LEAD_TIMES
from home_builder_agent.scheduling.engine import Phase, Schedule


# ---------------------------------------------------------------------------
# Material → Phase mapping
# ---------------------------------------------------------------------------

# Maps material category keyword (matches PROCUREMENT_LEAD_TIMES keys)
# to the phase that needs the material on-site for install.
# These align with Chad's note in the AI Help List: cabinets ordered when
# framing complete, plumbing fixtures before framing starts, etc.
MATERIAL_TO_INSTALL_PHASE: dict[str, str] = {
    "window":      "Roofing",          # Need windows for envelope dry-in
    "door":        "Roofing",          # Exterior doors install with envelope
    "truss":       "Framing",          # Trusses go up during framing
    "lumber":      "Framing",          # Framing package
    "cabinet":     "Drywall",          # Install after drywall + paint primer
    "appliance":   "Trim",             # Install during trim/finish
    "hvac":        "Rough-In MEP",     # Rough-in
    "elevator":    "Framing",          # Install during framing for shaft
    "generator":   "Rough-In MEP",     # Pad + electrical rough
    "tile":        "Flooring",         # Tile is part of flooring phase
    "flooring":    "Flooring",         # Hardwood / LVP
    "roofing":     "Roofing",
    "plumbing":    "Rough-In MEP",     # Rough-in fixtures (in-wall valves)
    "electrical":  "Rough-In MEP",     # Rough-in panels + boxes
    "insulation":  "Insulation",
    "drywall":     "Drywall",
    "concrete":    "Foundation",
    "steel":       "Foundation",       # Structural steel during foundation/framing
    "pool":        "Foundation",       # Pool shell early; equipment later
    "brick":       "Siding",           # Exterior veneer
    "stucco":      "Siding",
}


DEFAULT_SAFETY_BUFFER_CALENDAR_DAYS = 7  # ~5 business days


@dataclass
class DropDeadDate:
    """A computed drop-dead order date for a material category on a project."""

    material_category: str         # e.g. "window"
    lead_time_days: int            # weeks * 7
    install_phase_name: str        # e.g. "Roofing"
    install_date: date             # phase.planned_start_date
    safety_buffer_days: int
    drop_dead_date: date           # install - lead_time - buffer
    lead_time_source: str = "category-default"  # SKU / vendor / category / manual

    def to_dict(self) -> dict:
        return {
            "material_category": self.material_category,
            "lead_time_days": self.lead_time_days,
            "lead_time_source": self.lead_time_source,
            "install_phase_name": self.install_phase_name,
            "install_date": self.install_date.isoformat(),
            "safety_buffer_days": self.safety_buffer_days,
            "drop_dead_date": self.drop_dead_date.isoformat(),
        }


def compute_drop_dead_dates(
    schedule: Schedule,
    safety_buffer_calendar_days: int = DEFAULT_SAFETY_BUFFER_CALENDAR_DAYS,
) -> list[DropDeadDate]:
    """For each material category in MATERIAL_TO_INSTALL_PHASE, compute the
    drop-dead order date based on the schedule's phase dates and the category
    lead-time defaults.

    Returns one DropDeadDate per material category whose install phase is
    present in the schedule. Sorted by drop_dead_date ascending (earliest first).
    """
    results: list[DropDeadDate] = []

    # Convert weeks → days for lead time
    lead_time_days_by_category = {
        cat: weeks * 7 for cat, weeks in PROCUREMENT_LEAD_TIMES.items()
    }

    for material, install_phase_name in MATERIAL_TO_INSTALL_PHASE.items():
        lead_days = lead_time_days_by_category.get(material)
        if lead_days is None:
            continue
        phase = schedule.find_phase_by_name(install_phase_name)
        if phase is None:
            continue
        install_date = phase.planned_start_date
        drop_dead = install_date - timedelta(days=lead_days + safety_buffer_calendar_days)
        results.append(
            DropDeadDate(
                material_category=material,
                lead_time_days=lead_days,
                install_phase_name=install_phase_name,
                install_date=install_date,
                safety_buffer_days=safety_buffer_calendar_days,
                drop_dead_date=drop_dead,
                lead_time_source="category-default",
            )
        )

    results.sort(key=lambda r: r.drop_dead_date)
    return results


# ---------------------------------------------------------------------------
# Live procurement alerts (today-relative classification)
# ---------------------------------------------------------------------------

# Today-relative urgency band for a drop-dead date. Mirrors the bands used
# by agents/procurement_alerts.check_procurement_alerts so behavior stays
# consistent between the live (Postgres-backed) and legacy (Sheet-backed)
# surfaces.
ALERT_BAND_OVERDUE   = "OVERDUE"      # drop_dead < today
ALERT_BAND_TODAY     = "ORDER NOW"    # drop_dead == today
ALERT_BAND_THIS_WEEK = "THIS WEEK"    # drop_dead within 7 days
ALERT_BAND_UPCOMING  = "UPCOMING"     # drop_dead within UPCOMING window
ALERT_BAND_LATER     = "LATER"        # outside the upcoming window (filtered out)

_ALERT_PRIORITY = {
    ALERT_BAND_OVERDUE: 0,
    ALERT_BAND_TODAY: 1,
    ALERT_BAND_THIS_WEEK: 2,
    ALERT_BAND_UPCOMING: 3,
    ALERT_BAND_LATER: 9,
}


def classify_alert_band(
    drop_dead: date,
    today: date,
    upcoming_window_days: int,
) -> str:
    """Bucket a drop-dead date into an urgency band relative to today."""
    delta = (drop_dead - today).days
    if delta < 0:
        return ALERT_BAND_OVERDUE
    if delta == 0:
        return ALERT_BAND_TODAY
    if delta <= 7:
        return ALERT_BAND_THIS_WEEK
    if delta <= upcoming_window_days:
        return ALERT_BAND_UPCOMING
    return ALERT_BAND_LATER


def compute_live_procurement_alerts(
    project_id: str,
    today: date | None = None,
    upcoming_window_days: int | None = None,
    safety_buffer_calendar_days: int = DEFAULT_SAFETY_BUFFER_CALENDAR_DAYS,
) -> dict | None:
    """Compute today-relative procurement alerts for a project.

    Reads the Postgres-backed Schedule, computes drop-dead dates, then
    classifies each by urgency band relative to today. Only alerts within
    the upcoming window (default PROCUREMENT_UPCOMING_DAYS) are returned —
    materials whose drop-dead is months away aren't actionable yet.

    Returns a dict shaped for hb-ask:
        {
            "project_id": "...",
            "project_name": "...",
            "today": "2026-05-06",
            "upcoming_window_days": 14,
            "totals": {"OVERDUE": 0, "ORDER NOW": 0, "THIS WEEK": 1, "UPCOMING": 2},
            "alerts": [
                {
                    "band": "ORDER NOW",
                    "material_category": "window",
                    "lead_time_days": 56,
                    "install_phase_name": "Roofing",
                    "install_date": "2026-07-01",
                    "drop_dead_date": "2026-05-06",
                    "days_until_drop_dead": 0,
                },
                ...
            ],
        }

    Returns None if the project has no schedule in Postgres.
    """
    from home_builder_agent.config import PROCUREMENT_UPCOMING_DAYS
    from home_builder_agent.scheduling.store_postgres import compose_schedule_from_db

    if today is None:
        today = date.today()
    if upcoming_window_days is None:
        upcoming_window_days = PROCUREMENT_UPCOMING_DAYS

    schedule = compose_schedule_from_db(project_id)
    if schedule is None:
        return None

    drop_deads = compute_drop_dead_dates(
        schedule, safety_buffer_calendar_days=safety_buffer_calendar_days,
    )

    alerts: list[dict] = []
    totals = {
        ALERT_BAND_OVERDUE: 0,
        ALERT_BAND_TODAY: 0,
        ALERT_BAND_THIS_WEEK: 0,
        ALERT_BAND_UPCOMING: 0,
    }

    for dd in drop_deads:
        band = classify_alert_band(dd.drop_dead_date, today, upcoming_window_days)
        if band == ALERT_BAND_LATER:
            continue  # outside actionable window — skip
        totals[band] += 1
        alerts.append({
            "band": band,
            "material_category": dd.material_category,
            "lead_time_days": dd.lead_time_days,
            "install_phase_name": dd.install_phase_name,
            "install_date": dd.install_date.isoformat(),
            "drop_dead_date": dd.drop_dead_date.isoformat(),
            "days_until_drop_dead": (dd.drop_dead_date - today).days,
        })

    # Sort: most urgent first, then by drop-dead date ascending within band.
    alerts.sort(key=lambda a: (_ALERT_PRIORITY[a["band"]], a["drop_dead_date"]))

    return {
        "project_id": project_id,
        "project_name": schedule.project_name,
        "today": today.isoformat(),
        "upcoming_window_days": upcoming_window_days,
        "totals": totals,
        "alerts": alerts,
    }
