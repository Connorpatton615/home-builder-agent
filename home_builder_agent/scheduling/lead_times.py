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
