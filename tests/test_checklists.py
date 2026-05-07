"""Tests for home_builder_agent.scheduling.checklists + checklist_gates_view."""

from __future__ import annotations

from datetime import date

import pytest

from home_builder_agent.scheduling.checklists import (
    CHECKLIST_PHASE_NAMES,
    Checklist,
    ChecklistItem,
    authored_template_phase_names,
    can_advance_phase,
    instantiate_checklist,
    list_template_phase_names,
    load_template,
    template_path,
)
from home_builder_agent.scheduling.engine import (
    Schedule,
    schedule_from_target_completion,
)
from home_builder_agent.scheduling.view_models import checklist_gates_view


# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------

def test_precon_template_exists_and_loads():
    """Precon is the master template — must load and have substantive content."""
    tpl = load_template("Precon")
    assert tpl is not None
    assert tpl.get("phase_name") == "Precon"
    cats = tpl.get("categories") or []
    assert len(cats) == 10, f"Precon should have 10 categories, got {len(cats)}"
    total = sum(len(c.get("items", [])) for c in cats)
    assert total >= 40, f"Precon should have ~44 items, got {total}"


def test_all_24_phases_have_authored_templates():
    """Post template-generation sweep: every phase has a template on disk.
    The "stub auto-closes" path in checklists.py is no longer the active
    path for any phase — every Phase carries a real gate from day one."""
    for name in CHECKLIST_PHASE_NAMES:
        tpl = load_template(name)
        assert tpl is not None, f"Missing template for phase: {name}"
        cats = tpl.get("categories") or []
        assert len(cats) >= 3, f"{name} should have ≥3 categories, got {len(cats)}"
        items = sum(len(c.get("items", [])) for c in cats)
        assert items >= 20, f"{name} should have ≥20 items, got {items}"


def test_unknown_phase_name_produces_stub():
    """A phase name that isn't in the canonical 24 produces a stub."""
    cl = instantiate_checklist(phase_id="phase-x", phase_name="Not A Real Phase")
    assert cl.total_count == 0
    assert cl.template_version == "v0-stub"


def test_template_phase_name_list_matches_canonical():
    """The 24 names per scheduling-engine.md § Checklist Library are exposed."""
    names = list_template_phase_names()
    assert len(names) == 24
    assert "Precon" in names
    assert "Final Punch Out" in names


def test_authored_phases_so_far():
    """Track which templates have been authored. V0 = Precon only."""
    authored = authored_template_phase_names()
    assert "Precon" in authored


# ---------------------------------------------------------------------------
# Checklist instantiation
# ---------------------------------------------------------------------------

def test_instantiate_precon_produces_real_items():
    """Precon checklist instantiation produces categorized items."""
    cl = instantiate_checklist(phase_id="phase-01", phase_name="Precon")
    assert cl.phase_id == "phase-01"
    assert cl.total_count >= 40
    assert cl.completed_count == 0
    cats = cl.items_by_category
    assert "Client & Contract" in cats
    assert "Selections" in cats
    # First category has multiple items
    assert len(cats["Client & Contract"]) >= 4


def test_instantiate_authored_phase_produces_real_items():
    """Drywall Rough now has a Sonnet-authored template (post-generation sweep).
    Used to be a stub; now ships ≥20 items."""
    cl = instantiate_checklist(phase_id="phase-08", phase_name="Drywall Rough")
    assert cl.total_count >= 20
    assert cl.template_version != "v0-stub"


def test_id_prefix_scopes_item_ids():
    """When a project context is supplied, item IDs are scoped to it."""
    cl = instantiate_checklist(
        phase_id="phase-01",
        phase_name="Precon",
        id_prefix="proj-whitfield:phase-01",
    )
    assert cl.id.startswith("proj-whitfield:phase-01")
    for item in cl.items:
        assert item.id.startswith("proj-whitfield:phase-01:"), \
            f"Expected proj-prefixed item id, got: {item.id}"


# ---------------------------------------------------------------------------
# Status + gate semantic
# ---------------------------------------------------------------------------

def test_empty_checklist_is_closed():
    """Stub checklists with zero items auto-close. Phase pass-through."""
    cl = Checklist(id="x", phase_id="p", template_version="v0-stub", items=[])
    assert cl.status == "closed"
    assert cl.completed_count == 0
    assert cl.total_count == 0


def test_partial_checklist_is_open():
    cl = Checklist(
        id="x", phase_id="p", template_version="v1.0",
        items=[
            ChecklistItem(id="x:1", category="A", label="One", is_complete=True),
            ChecklistItem(id="x:2", category="A", label="Two", is_complete=False),
        ],
    )
    assert cl.status == "open"
    assert cl.completed_count == 1
    assert cl.total_count == 2


def test_full_checklist_is_closed():
    cl = Checklist(
        id="x", phase_id="p", template_version="v1.0",
        items=[
            ChecklistItem(id="x:1", category="A", label="One", is_complete=True),
            ChecklistItem(id="x:2", category="A", label="Two", is_complete=True),
        ],
    )
    assert cl.status == "closed"


def test_can_advance_phase_gate_semantic():
    """Phase advancement gates on Checklist status. Per canonical-data-model § 6."""
    # No checklist → can advance (degraded-mode V0 fallback)
    assert can_advance_phase(None) is True

    # Empty (stub) checklist → can advance
    stub = Checklist(id="x", phase_id="p", template_version="v0-stub", items=[])
    assert can_advance_phase(stub) is True

    # Open checklist → cannot advance
    open_cl = Checklist(
        id="x", phase_id="p", template_version="v1.0",
        items=[ChecklistItem(id="x:1", category="A", label="One", is_complete=False)],
    )
    assert can_advance_phase(open_cl) is False

    # Closed checklist → can advance
    closed_cl = Checklist(
        id="x", phase_id="p", template_version="v1.0",
        items=[ChecklistItem(id="x:1", category="A", label="One", is_complete=True)],
    )
    assert can_advance_phase(closed_cl) is True


# ---------------------------------------------------------------------------
# items_by_category grouping
# ---------------------------------------------------------------------------

def test_items_by_category_groups_in_order():
    cl = Checklist(
        id="x", phase_id="p", template_version="v1.0",
        items=[
            ChecklistItem(id="x:1", category="A", label="One"),
            ChecklistItem(id="x:2", category="B", label="Two"),
            ChecklistItem(id="x:3", category="A", label="Three"),
        ],
    )
    grouped = cl.items_by_category
    assert list(grouped.keys()) == ["A", "B"]  # insertion order preserved
    assert len(grouped["A"]) == 2
    assert len(grouped["B"]) == 1


# ---------------------------------------------------------------------------
# View-model projection
# ---------------------------------------------------------------------------

def test_checklist_gates_view_empty_when_no_data():
    schedules = [
        schedule_from_target_completion(
            project_id="proj-x", project_name="Test Project",
            target_completion_date=date(2027, 1, 30),
        )
    ]
    payload = checklist_gates_view(schedules=schedules)
    assert payload.projects == []


def test_checklist_gates_view_projects_real_checklists():
    """End-to-end: schedule + a Precon checklist → wire payload."""
    schedules = [
        schedule_from_target_completion(
            project_id="proj-w", project_name="Whitfield Residence",
            target_completion_date=date(2027, 1, 30),
        )
    ]
    cl = instantiate_checklist(
        phase_id="phase-01", phase_name="Precon",
        id_prefix="proj-w:phase-01",
    )
    payload = checklist_gates_view(
        schedules=schedules,
        checklists_by_project={"proj-w": [cl]},
    )

    assert len(payload.projects) == 1
    p = payload.projects[0]
    assert p.project_id == "proj-w"
    assert p.project_name == "Whitfield Residence"
    assert len(p.checklists) == 1
    cl_payload = p.checklists[0]
    assert cl_payload.status == "open"
    assert cl_payload.total_count >= 40
    assert "Client & Contract" in cl_payload.items_by_category


def test_checklist_gates_view_status_propagates():
    """If every item is complete, payload's status is 'closed'."""
    schedules = [
        schedule_from_target_completion(
            project_id="p", project_name="P",
            target_completion_date=date(2027, 1, 30),
        )
    ]
    cl = instantiate_checklist(phase_id="phase-01", phase_name="Precon")
    for item in cl.items:
        item.is_complete = True

    payload = checklist_gates_view(
        schedules=schedules, checklists_by_project={"p": [cl]},
    )
    assert payload.projects[0].checklists[0].status == "closed"
    assert payload.projects[0].checklists[0].completed_count == cl.total_count


# ---------------------------------------------------------------------------
# Reconcile dispatcher wiring (structural — no DB roundtrip)
# ---------------------------------------------------------------------------

def test_checklist_tick_dispatcher_registered():
    """The checklist-tick action_type maps to _dispatch_checklist_tick,
    not the unknown stub. Migration 005 + reconcile wiring should be live."""
    from home_builder_agent.scheduling.reconcile import (
        DISPATCHERS,
        _dispatch_checklist_tick,
    )
    assert "checklist-tick" in DISPATCHERS
    assert DISPATCHERS["checklist-tick"] is _dispatch_checklist_tick


def test_checklist_tick_rejects_wrong_target_type():
    """Dispatcher rejects target_entity_type != 'checklist-item' without
    hitting the DB."""
    from home_builder_agent.scheduling.reconcile import (
        DispatchOutcome,
        _dispatch_checklist_tick,
    )

    action = {
        "id": "act-1",
        "target_entity_type": "phase",            # WRONG for checklist-tick
        "target_entity_id": "phase-1",
        "actor_user_id": None,
        "synced_at": None,
        "payload": {"is_complete": True},
    }
    result = _dispatch_checklist_tick(action, conn=None)  # conn unused on this path
    assert result.outcome == DispatchOutcome.SKIPPED
    assert "checklist-item" in (result.notes or "")


def test_checklist_tick_rejects_empty_payload():
    """Dispatcher rejects payloads with neither is_complete nor notes."""
    from home_builder_agent.scheduling.reconcile import (
        DispatchOutcome,
        _dispatch_checklist_tick,
    )

    action = {
        "id": "act-2",
        "target_entity_type": "checklist-item",
        "target_entity_id": "item-1",
        "actor_user_id": None,
        "synced_at": None,
        "payload": {},
    }
    result = _dispatch_checklist_tick(action, conn=None)
    assert result.outcome == DispatchOutcome.SKIPPED
    assert "actionable" in (result.notes or "")
