"""checklists.py — Checklist + ChecklistItem entities for the Scheduling Engine.

Per canonical-data-model.md § entities 6 & 7. The Checklist is a gate:
a Phase cannot transition to `complete` until its Checklist closes,
which means the next Phase cannot start either.

V0 (this commit) ships in-memory only — templates load from JSON files
in `checklist_templates/`, instances live next to a project's Phases.
Persistence to `home_builder.checklist` + `home_builder.checklist_item`
is migration 005 (queued; see docs/specs/desktop-renderer.md § Phase 2).

The 24 phase checklists per scheduling-engine.md § Checklist Library
are listed in `phases.CHECKLIST_PHASE_NAMES`. As of 2026-05-09:
- **All 24 phases have substantive templates** — 923 items total, code-
  tied (IRC R502.1, ACI 301, Baldwin County wind zone references, Alabama
  Code citations where relevant). Items use the
  `{label, photo_required}` shape; the loader also accepts plain-string
  items for backwards compatibility. 735 of 923 items are flagged
  photo_required (~80%) — Chad's flow of approval expects photo
  evidence on physical-site-work items.
- **Precon** is the gold standard, Chad-redlined.
- **The other 23 are queued for Chad's redline** — packet at
  docs/checklists/chad-redline-packet-2026-05-09.md.

The empty-list-closes semantic is preserved as a safety net: a phase
without a template (e.g., a custom one Chad adds at runtime) still
auto-closes its gate.
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
    """An individual check. Mirrors canonical-data-model.md § entity 7.

    `photo_required` flags items where Chad's flow of approval expects
    photo evidence (e.g., "vapor barrier installed and lapped") vs.
    purely administrative items (e.g., "permit posted on-site"). The
    renderer surfaces a photo-upload affordance on flagged items.

    `photos` carries Drive references for any photos already uploaded
    against this item — list of {drive_file_id, drive_url, uploaded_at,
    uploaded_by} dicts. The Drive-side artifact lives at
    `Site Logs/<Project>/Checklist Photos/<phase>/<item-slug>/`.
    Schema column for persistence lands in migration 009 (queued).
    """

    id: str
    category: str
    label: str
    is_complete: bool = False
    completed_by: str | None = None
    completed_at: date | None = None
    notes: str | None = None
    photo_required: bool = False
    photos: list[dict] = field(default_factory=list)
    # Audit pointer back to the canonical checklist_template_item row
    # this instance was hydrated from. Set when DB-first hydration runs;
    # None for legacy rows + JSON-only in-memory instantiations.
    # See migration 010 + spec § Engine-side adapter changes.
    template_item_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "label": self.label,
            "is_complete": self.is_complete,
            "completed_by": self.completed_by,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "notes": self.notes,
            "photo_required": self.photo_required,
            "photos": list(self.photos),
            "template_item_id": self.template_item_id,
        }


@dataclass
class Checklist:
    """1:1 with a Phase. The gate per canonical-data-model.md § entity 6."""

    id: str
    phase_id: str
    template_version: str
    items: list[ChecklistItem] = field(default_factory=list)
    # Pointer back to the canonical checklist_template row this checklist
    # was hydrated from. Renderer needs this to construct the PATCH /
    # POST URLs against the template (per spec § REST routes). None for
    # legacy rows + JSON-only in-memory instantiations.
    template_id: str | None = None

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

def slugify(text: str) -> str:
    """Lowercase, dash-separated, alphanumeric-only — for stable file/id segments.

    Public so callers (the template-generation script in scripts/) can reuse
    the same canonical form when naming output files.
    """
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


# Backwards-compat alias — internal callers used `_slugify`.
_slugify = slugify


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
    conn=None,
    tenant_id: str | None = None,
) -> Checklist:
    """Build a fresh Checklist for a Phase from its template (if any).

    `id_prefix` scopes item IDs to a parent context (typically
    "{project_id}:{phase_id}") so checklist item IDs are globally unique
    when persisted. Defaults to `phase_id` for in-memory use.

    Hydration source order (per ADR 2026-05-09 D1 — DB is canonical):
      1. If `conn` is provided AND a checklist_template row exists for
         (slugify(phase_name), tenant_id), hydrate from
         checklist_template_item rows. Wording reflects Chad's in-app
         edits.
      2. JSON fallback at scheduling/checklist_templates/<slug>.json —
         used by tests and any caller that doesn't pass a connection.
      3. Stub (no items) when neither path produces a template.

    Callers that already own a Postgres connection (e.g. the engine's
    schedule composer when running against the live store) should pass
    it. In-memory use cases (tests, the round-trip CLI) get the JSON
    path automatically by leaving conn=None.
    """
    prefix = id_prefix or phase_id

    # Source 1: DB-backed template (preferred per D1)
    if conn is not None:
        # Local import to avoid circular import (store_postgres imports
        # from this module too).
        from home_builder_agent.scheduling.store_postgres import (
            list_checklist_template_items,
            load_checklist_template,
        )

        db_template = load_checklist_template(
            slugify(phase_name), tenant_id=tenant_id, conn=conn
        )
        if db_template is not None:
            db_items = list_checklist_template_items(db_template["id"], conn=conn)
            if db_items:
                items: list[ChecklistItem] = []
                # Group within category so id suffix matches sequence.
                seq_per_cat: dict[str, int] = {}
                for it in db_items:
                    cat = it["category"]
                    cat_slug = _slugify(cat)
                    seq = seq_per_cat.setdefault(cat, 0)
                    items.append(
                        ChecklistItem(
                            id=f"{prefix}:{cat_slug}:{seq:02d}",
                            category=cat,
                            label=it["label"],
                            photo_required=bool(it.get("photo_required") or False),
                            template_item_id=it["id"],
                        )
                    )
                    seq_per_cat[cat] = seq + 1
                return Checklist(
                    id=f"{prefix}:checklist",
                    phase_id=phase_id,
                    template_version=db_template["template_version"],
                    items=items,
                )

    # Source 2: JSON fallback
    template = load_template(phase_name)

    if template is None:
        # Stub — empty, auto-closes.
        return Checklist(
            id=f"{prefix}:checklist",
            phase_id=phase_id,
            template_version=STUB_TEMPLATE_VERSION,
            items=[],
        )

    items = []
    for cat in template.get("categories", []):
        cat_name = cat.get("name", "Uncategorized")
        cat_slug = _slugify(cat_name)
        for idx, item_spec in enumerate(cat.get("items", [])):
            # Items may be either a plain string (legacy V1 format) or
            # an object with {label, photo_required} (current format,
            # per Chad's flow-of-approval upgrade). Both supported.
            if isinstance(item_spec, str):
                label = item_spec
                photo_required = False
            elif isinstance(item_spec, dict):
                label = item_spec.get("label", "")
                photo_required = bool(item_spec.get("photo_required", False))
            else:
                continue
            items.append(
                ChecklistItem(
                    id=f"{prefix}:{cat_slug}:{idx:02d}",
                    category=cat_name,
                    label=label,
                    photo_required=photo_required,
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
