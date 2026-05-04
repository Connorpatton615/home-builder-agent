"""phases.py — phase definitions for the Scheduling Engine.

Source: Chad's brief in samples/chad-ai-help-list.xlsx (Brief tab, section 1).
Each phase has a default duration in calendar days. Projects can override
duration per-phase via Phase.project_override_duration_days (see
canonical-data-model.md § entity 2 Phase).

Sequencing is the V1 strict-linear chain. Overlap (roofing + siding,
trim + paint + flooring) is modeled via per-project Dependency overrides
in V2 (canonical-data-model.md § entity 5 Dependency).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PhaseTemplate:
    """A phase template — the constant definition. Projects instantiate Phase
    rows from these templates and may override duration per-project."""

    sequence_index: int           # 1-based position in the linear chain
    name: str                     # Display name
    default_duration_days: int    # Calendar days
    notes: str = ""               # Optional anchor note


# The 13-phase canonical sequence (Chad's brief).
# When a phase has a duration range like "2-5 days", we pick the upper bound
# as the conservative default — engine plans backwards from a target date,
# so longer-than-actual is safer than shorter.
PHASE_TEMPLATES: tuple[PhaseTemplate, ...] = (
    PhaseTemplate(1,  "Land Clearing",                 5, "2-5 days range; default to upper"),
    PhaseTemplate(2,  "Foundation",                   10, "Footing → slab pour"),
    PhaseTemplate(3,  "Framing",                      20, "Order cabinets when framing complete"),
    PhaseTemplate(4,  "Roofing",                       3, ""),
    PhaseTemplate(5,  "Rough-In MEP",                 15, "Plumbing + HVAC + Electrical"),
    PhaseTemplate(6,  "Siding",                        7, ""),
    PhaseTemplate(7,  "Insulation",                    3, ""),
    PhaseTemplate(8,  "Drywall",                       6, "Hang, sand, finish"),
    PhaseTemplate(9,  "Flooring",                     15, "Tile + Hardwood"),
    PhaseTemplate(10, "Trim",                          5, ""),
    PhaseTemplate(11, "Painting",                     15, "Interior + Exterior"),
    PhaseTemplate(12, "Final Grade",                   1, ""),
    PhaseTemplate(13, "Landscaping & Irrigation",      7, "Prep work"),
)


def get_phase_by_name(name: str) -> PhaseTemplate | None:
    """Lookup a phase template by case-insensitive substring match."""
    name_lower = name.lower().strip()
    for p in PHASE_TEMPLATES:
        if p.name.lower() == name_lower:
            return p
    # Fuzzy match — substring
    for p in PHASE_TEMPLATES:
        if name_lower in p.name.lower() or p.name.lower() in name_lower:
            return p
    return None


def get_phase_by_index(idx: int) -> PhaseTemplate | None:
    """Lookup phase template by sequence_index (1-based)."""
    for p in PHASE_TEMPLATES:
        if p.sequence_index == idx:
            return p
    return None


def total_duration_days() -> int:
    """Sum of all phase durations — minimum project length under V1 linear sequencing."""
    return sum(p.default_duration_days for p in PHASE_TEMPLATES)


# The 24-phase checklist library (per scheduling-engine.md § Checklist Library).
# Names only — checklist content comes from Chad's templates (44-item Precon
# is the model; non-precon 23 are agent-generated drafts subject to redline).
# Used by the engine to gate phase completion (Phase cannot transition to
# `complete` until its Checklist closes).
CHECKLIST_PHASE_NAMES: tuple[str, ...] = (
    "Precon",
    "Sitework",
    "Foundation",
    "Pre-framing",
    "Framing",
    "Post-framing",
    "Plumbing Rough",
    "HVAC Rough",
    "Electrical Rough",
    "Siding & Porch",
    "Insulation Rough",
    "Drywall Rough",
    "Cabinet",
    "Countertop",
    "Trim & Stairs",
    "Paint",
    "Tile",
    "Wood Flooring",
    "Plumbing Set Out",
    "Electrical Set Out",
    "HVAC Trim Out",
    "Landscape & Irrigation",
    "Final Paint",
    "Final Punch Out",
)
