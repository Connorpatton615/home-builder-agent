"""checklists.py — Checklist + ChecklistItem entities for the Scheduling Engine.

Per canonical-data-model.md § entities 6 & 7. The Checklist is a gate:
a Phase cannot transition to `complete` until its Checklist closes,
which means the next Phase cannot start either.

V0 (this commit) ships in-memory only — templates load from JSON files
in `checklist_templates/`, instances live next to a project's Phases.
Persistence to `home_builder.checklist` + `home_builder.checklist_item`
is migration 005 (queued; see docs/specs/desktop-renderer.md § Phase 2).

The 24 phase checklists per scheduling-engine.md § Checklist Library
are listed in `phases.CHECKLIST_PHASE_NAMES`. V0 ships:
- **Precon** — Chad's master template, 42 items / 10 categories. Authored
  to industry standards + the 10 categories Chad explicitly named. Subject
  to redline before pilot.
- **All 23 others** — stub templates (zero items). A stub Checklist
  auto-closes (`status == "closed"` when total_count is 0), which lets
  phases pass-through the gate cleanly until Chad authors content for
  each one. Authoring per phase is a follow-up sub-task — generation
  from Chad's KB + industry standards, then his redline.

The empty-list-closes semantic is intentional: it keeps the gate
mechanism live across all 24 phases from day one rather than waiting
for every checklist to be authored. Phases with stubs currently behave
identically to "no gate" — exactly the V0 fallback the canonical model
calls for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from home_builder_agent.scheduling.phases import CHECKLIST_PHASE_NAMES

TEMPLATE_DIR = Path(__file__).resolve().parent / "checklist_templates"
STUB_TEMPLATE_VERSION = "v0-stub"


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

@dataclass
class ChecklistItem:
    """An individual check. Mirrors canonical-data-model.md § entity 7."""

    id: str
    category: str
    label: str
    is_complete: bool = False
    completed_by: str | None = None
    completed_at: date | None = None
    notes: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "label": self.label,
            "is_complete": self.is_complete,
            "completed_by": self.completed_by,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "notes": self.notes,
        }


@dataclass
class Checklist:
    """1:1 with a Phase. The gate per canonical-data-model.md § entity 6."""

    id: str
    phase_id: str
    template_version: str
    items: list[ChecklistItem] = field(default_factory=list)

    # ---- Derived properties -------------------------------------------------

    @property
    def total_count(self) -> int:
        return len(self.items)

    @property
    def completed_count(self) -> int:
        return sum(1 for i in self.items if i.is_complete)

    @property
    def status(self) -> str:
        """`closed` when every item is checked OR the checklist is empty;
        `open` otherwise.

        The empty-list-closes path lets stub checklists for not-yet-authored
        phases pass-through the gate cleanly. Authoring fills in items;
        until then, the gate is a no-op for that phase.
        """
        if self.total_count == 0:
            return "closed"
        return "closed" if self.completed_count >= self.total_count else "open"

    @property
    def items_by_category(self) -> dict[str, list[ChecklistItem]]:
        """Items grouped by category — convenience for renderers (the desktop
        UI buckets the Precon 10 categories visually; the iOS app may flatten)."""
        out: dict[str, list[ChecklistItem]] = {}
        for item in self.items:
            out.setdefault(item.category, []).append(item)
        return out


# ---------------------------------------------------------------------------
# Gate semantic (canonical-data-model.md § entity 6)
# ---------------------------------------------------------------------------

def can_advance_phase(checklist: Checklist | None) -> bool:
    """A Phase can transition to `complete` only if its Checklist is closed
    (or doesn't exist). Pure function — no side effects, no I/O.

    The reconcile pass (and any direct phase-completion path) consults this
    before flipping a Phase's status. If False, the phase stays in
    `blocked-on-checklist` rather than `complete`.
    """
    return checklist is None or checklist.status == "closed"


# ---------------------------------------------------------------------------
# Template loading + instantiation
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Lowercase, dash-separated, alphanumeric-only — for stable file/id segments."""
    out = []
    prev_dash = False
    for c in text.lower():
        if c.isalnum():
            out.append(c)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


def template_path(phase_name: str) -> Path:
    """Resolve the JSON template file for a phase name. Does not check existence."""
    return TEMPLATE_DIR / f"{_slugify(phase_name)}.json"


def load_template(phase_name: str) -> dict | None:
    """Read a phase's checklist template from disk. Returns None if no
    template file exists (stub case)."""
    path = template_path(phase_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def instantiate_checklist(
    phase_id: str,
    phase_name: str,
    *,
    id_prefix: str | None = None,
) -> Checklist:
    """Build a fresh Checklist for a Phase from its template (if any).

    `id_prefix` scopes item IDs to a parent context (typically
    "{project_id}:{phase_id}") so checklist item IDs are globally unique
    when persisted. Defaults to `phase_id` for in-memory use.
    """
    prefix = id_prefix or phase_id
    template = load_template(phase_name)

    if template is None:
        # Stub — empty, auto-closes.
        return Checklist(
            id=f"{prefix}:checklist",
            phase_id=phase_id,
            template_version=STUB_TEMPLATE_VERSION,
            items=[],
        )

    items: list[ChecklistItem] = []
    for cat in template.get("categories", []):
        cat_name = cat.get("name", "Uncategorized")
        cat_slug = _slugify(cat_name)
        for idx, label in enumerate(cat.get("items", [])):
            items.append(
                ChecklistItem(
                    id=f"{prefix}:{cat_slug}:{idx:02d}",
                    category=cat_name,
                    label=label,
                )
            )

    return Checklist(
        id=f"{prefix}:checklist",
        phase_id=phase_id,
        template_version=template.get("template_version", STUB_TEMPLATE_VERSION),
        items=items,
    )


def list_template_phase_names() -> list[str]:
    """The 24 canonical phase names per scheduling-engine.md § Checklist Library."""
    return list(CHECKLIST_PHASE_NAMES)


def authored_template_phase_names() -> list[str]:
    """Phases that have a real template file on disk. The other phases get
    stub checklists. Useful for "what's still to author" reports."""
    return [
        name for name in CHECKLIST_PHASE_NAMES
        if template_path(name).exists()
    ]
