"""engine.py — backwards-scheduling engine.

Pure Python — no I/O, no API calls, no Tracker reads. Takes a Project anchor
(target completion date OR target framing-start date) plus optional per-phase
duration overrides and produces the canonical schedule: Phases with planned
start/end dates, Milestones at phase boundaries, and an estimated completion
date that propagates if any input shifts.

This is the core of canonical-data-model.md § entity 14 ScheduleView's
`master` view-type. Other views (daily/weekly/monthly) are filters/projections
over the same Phase data — see view_models.py.

V1 = strict linear sequencing (Phase N+1 starts the day after Phase N ends).
V2+ adds overlap modeling via Dependency entities with non-finish-to-start
dependency_kind (see canonical-data-model.md § entity 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from home_builder_agent.scheduling.phases import (
    PHASE_TEMPLATES,
    PhaseTemplate,
    total_duration_days,
)


# ---------------------------------------------------------------------------
# Phase + Schedule models
# ---------------------------------------------------------------------------

PhaseStatus = str  # "not-started" | "in-progress" | "blocked-on-checklist" | "complete"


@dataclass
class Phase:
    """An instantiated Phase for a specific Project. Mirrors entity 2 in
    canonical-data-model.md — engine-owned, surfaces read-only."""

    sequence_index: int
    name: str
    duration_days: int                # Effective duration after overrides
    planned_start_date: date
    planned_end_date: date
    template: PhaseTemplate
    status: PhaseStatus = "not-started"
    actual_start_date: date | None = None
    actual_end_date: date | None = None

    @property
    def id(self) -> str:
        """Stable phase id within a project — index-based for V1 single-tenant."""
        return f"phase-{self.sequence_index:02d}"

    def to_dict(self) -> dict:
        """Serialize to view-model-compatible dict (snake_case, ISO dates)."""
        return {
            "id": self.id,
            "phase_template_id": self.template.sequence_index,
            "name": self.name,
            "sequence_index": self.sequence_index,
            "status": self.status,
            "planned_start_date": self.planned_start_date.isoformat(),
            "planned_end_date": self.planned_end_date.isoformat(),
            "actual_start_date": self.actual_start_date.isoformat() if self.actual_start_date else None,
            "actual_end_date": self.actual_end_date.isoformat() if self.actual_end_date else None,
            "default_duration_days": self.template.default_duration_days,
            "duration_days": self.duration_days,
        }


@dataclass
class Milestone:
    """Date-anchored event with no duration. Phase boundaries are auto-milestones."""

    name: str
    planned_date: date
    phase_id: str | None = None
    status: str = "pending"  # pending | hit | missed | rescheduled

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "planned_date": self.planned_date.isoformat(),
            "phase_id": self.phase_id,
            "status": self.status,
        }


@dataclass
class Schedule:
    """A computed schedule for a single Project.

    Engine-owned. Surfaces (desktop, mobile, Tracker bridge) consume this via
    the view_models projections — they never mutate Schedule fields directly.
    """

    project_id: str
    project_name: str
    phases: list[Phase]
    milestones: list[Milestone]
    estimated_completion_date: date
    target_completion_date: date | None = None
    target_framing_start_date: date | None = None
    overrides_applied: dict[int, int] = field(default_factory=dict)
    # ^ {sequence_index: override_duration_days}

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "estimated_completion_date": self.estimated_completion_date.isoformat(),
            "target_completion_date": self.target_completion_date.isoformat() if self.target_completion_date else None,
            "target_framing_start_date": self.target_framing_start_date.isoformat() if self.target_framing_start_date else None,
            "phases": [p.to_dict() for p in self.phases],
            "milestones": [m.to_dict() for m in self.milestones],
            "overrides_applied": dict(self.overrides_applied),
        }

    def find_phase_by_name(self, name: str) -> Phase | None:
        n = name.lower().strip()
        for p in self.phases:
            if p.name.lower() == n:
                return p
        for p in self.phases:
            if n in p.name.lower():
                return p
        return None


# ---------------------------------------------------------------------------
# Backwards scheduler
# ---------------------------------------------------------------------------

def schedule_from_target_completion(
    project_id: str,
    project_name: str,
    target_completion_date: date,
    duration_overrides: dict[int, int] | None = None,
) -> Schedule:
    """Backwards-schedule from a target completion date.

    Walks PHASE_TEMPLATES in reverse, assigning end_date = next_start_date - 1
    and start_date = end_date - (duration - 1).

    The target_completion_date IS the end of the final phase (phase 13,
    Landscaping & Irrigation). Estimated completion equals target.
    """
    overrides = duration_overrides or {}
    phases: list[Phase] = []
    cursor_end = target_completion_date

    for template in reversed(PHASE_TEMPLATES):
        duration = overrides.get(template.sequence_index, template.default_duration_days)
        end = cursor_end
        start = end - timedelta(days=duration - 1)
        phases.append(
            Phase(
                sequence_index=template.sequence_index,
                name=template.name,
                duration_days=duration,
                planned_start_date=start,
                planned_end_date=end,
                template=template,
            )
        )
        cursor_end = start - timedelta(days=1)

    phases.reverse()  # back to forward order
    milestones = _milestones_from_phases(phases)

    return Schedule(
        project_id=project_id,
        project_name=project_name,
        phases=phases,
        milestones=milestones,
        estimated_completion_date=target_completion_date,
        target_completion_date=target_completion_date,
        overrides_applied=overrides,
    )


def schedule_from_target_framing_start(
    project_id: str,
    project_name: str,
    target_framing_start_date: date,
    duration_overrides: dict[int, int] | None = None,
) -> Schedule:
    """Forward-schedule from a target framing-start date.

    Useful when Chad anchors on framing rather than completion — e.g., when
    the framing crew has a known availability window.
    """
    overrides = duration_overrides or {}
    framing = _find_template_by_name("Framing")
    if framing is None:
        raise ValueError("Framing phase template not found — phase library is broken")

    phases: list[Phase] = []

    # Forward pass for phases at or after framing
    cursor_start = target_framing_start_date
    for template in PHASE_TEMPLATES:
        if template.sequence_index < framing.sequence_index:
            continue
        duration = overrides.get(template.sequence_index, template.default_duration_days)
        start = cursor_start
        end = start + timedelta(days=duration - 1)
        phases.append(
            Phase(
                sequence_index=template.sequence_index,
                name=template.name,
                duration_days=duration,
                planned_start_date=start,
                planned_end_date=end,
                template=template,
            )
        )
        cursor_start = end + timedelta(days=1)

    # Backward pass for phases before framing
    cursor_end = target_framing_start_date - timedelta(days=1)
    pre_phases: list[Phase] = []
    for template in reversed(PHASE_TEMPLATES):
        if template.sequence_index >= framing.sequence_index:
            continue
        duration = overrides.get(template.sequence_index, template.default_duration_days)
        end = cursor_end
        start = end - timedelta(days=duration - 1)
        pre_phases.append(
            Phase(
                sequence_index=template.sequence_index,
                name=template.name,
                duration_days=duration,
                planned_start_date=start,
                planned_end_date=end,
                template=template,
            )
        )
        cursor_end = start - timedelta(days=1)

    pre_phases.reverse()
    all_phases = pre_phases + phases
    milestones = _milestones_from_phases(all_phases)

    return Schedule(
        project_id=project_id,
        project_name=project_name,
        phases=all_phases,
        milestones=milestones,
        estimated_completion_date=all_phases[-1].planned_end_date,
        target_framing_start_date=target_framing_start_date,
        overrides_applied=overrides,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _milestones_from_phases(phases: list[Phase]) -> list[Milestone]:
    """Standard milestones: phase-boundary events Chad/clients ask about."""
    name_to_milestone = {
        "Foundation": "Foundation pour complete",
        "Framing": "Framing complete (cabinets ordered)",
        "Roofing": "Dry-in complete",
        "Drywall": "Drywall complete",
        "Painting": "Paint complete",
    }
    milestones: list[Milestone] = []
    for p in phases:
        nice = name_to_milestone.get(p.name)
        if nice:
            milestones.append(
                Milestone(name=nice, planned_date=p.planned_end_date, phase_id=p.id)
            )
    if phases:
        milestones.append(
            Milestone(
                name="Certificate of Occupancy",
                planned_date=phases[-1].planned_end_date,
                phase_id=phases[-1].id,
            )
        )
    return milestones


def _find_template_by_name(name: str) -> PhaseTemplate | None:
    n = name.lower().strip()
    for t in PHASE_TEMPLATES:
        if t.name.lower() == n:
            return t
    return None
