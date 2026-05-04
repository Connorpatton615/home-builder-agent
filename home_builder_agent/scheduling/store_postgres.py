"""store_postgres.py — Postgres adapter for the Scheduling Engine.

Phase A read/write path against the schema in migration_002_home_builder_schema.sql
(home_builder.* tables on Supabase). This module is the boundary between the
engine's pure-Python data model (engine.Schedule / engine.Phase / engine.Milestone)
and the canonical Postgres store.

V0 scope (what this module ships):
  - load_project_by_id  → home_builder.project row → engine.Project-equivalent
  - load_project_by_name → for hb-schedule "Pelican Point" lookups (idx_hb_project_name)
  - load_phases_for_project → home_builder.phase rows → engine.Phase list
  - load_milestones_for_project → home_builder.milestone rows → engine.Milestone list
  - compose_schedule → builds engine.Schedule from the three above
  - save_phase_status_change → write Phase.status / actual_*_date back

Out of v0 scope (lands as needed):
  - Task / Dependency / Checklist / ChecklistItem reads/writes
  - Vendor / VendorItem / LeadTime reads (engine resolves lead times locally
    against config.PROCUREMENT_LEAD_TIMES until Vendor Intelligence is live)
  - Delivery / Inspection / WeatherImpact reads
  - Event emission (the canonical insert path; lands when Event store is wired)
  - UserAction insert (engine accepts these from iOS shell via FastAPI;
    iOS shell route handler will insert directly via service_role)

The adapter is intentionally narrow — read paths first to feed view-models,
write paths only where the engine owns state changes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import psycopg

from home_builder_agent.integrations.postgres import connection
from home_builder_agent.scheduling.engine import (
    Milestone,
    Phase,
    Schedule,
)
from home_builder_agent.scheduling.phases import (
    PHASE_TEMPLATES,
    PhaseTemplate,
    get_phase_by_index,
)


# ---------------------------------------------------------------------------
# Project read paths
# ---------------------------------------------------------------------------

def load_project_by_id(project_id: str, conn: psycopg.Connection | None = None) -> dict | None:
    """Load a project row by UUID. Returns the raw row dict or None."""
    return _query_one(
        """
        SELECT
            id::text AS id,
            name,
            customer_name,
            address,
            target_completion_date,
            target_framing_start_date,
            status,
            contract_signed_at,
            drive_folder_id,
            drive_folder_path,
            tenant_id,
            created_at,
            updated_at
        FROM home_builder.project
        WHERE id = %s::uuid
        """,
        (project_id,),
        conn=conn,
    )


def load_project_by_name(name: str, conn: psycopg.Connection | None = None) -> dict | None:
    """Load a project row by name (uses idx_hb_project_name).

    Used by hb-schedule when invoked with a project name argument before
    UUIDs are routine in V1 development.
    """
    return _query_one(
        """
        SELECT
            id::text AS id,
            name,
            customer_name,
            address,
            target_completion_date,
            target_framing_start_date,
            status,
            contract_signed_at,
            drive_folder_id,
            drive_folder_path,
            tenant_id,
            created_at,
            updated_at
        FROM home_builder.project
        WHERE name = %s AND status != 'archived'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (name,),
        conn=conn,
    )


# ---------------------------------------------------------------------------
# Phase read paths
# ---------------------------------------------------------------------------

def load_phases_for_project(
    project_id: str,
    conn: psycopg.Connection | None = None,
) -> list[Phase]:
    """Load phase rows for a project, ordered by sequence_index, projected to engine.Phase."""
    rows = _query_many(
        """
        SELECT
            id::text AS id,
            project_id::text AS project_id,
            phase_template_id,
            name,
            sequence_index,
            status,
            planned_start_date,
            planned_end_date,
            actual_start_date,
            actual_end_date,
            default_duration_days,
            project_override_duration_days
        FROM home_builder.phase
        WHERE project_id = %s::uuid
        ORDER BY sequence_index ASC
        """,
        (project_id,),
        conn=conn,
    )
    return [_row_to_phase(row) for row in rows]


def _row_to_phase(row: dict) -> Phase:
    """Convert a phase row dict into the engine's Phase dataclass.

    Resolves the PhaseTemplate by sequence_index. If the row's template is
    out of range (V2 24-phase library), falls back to a synthesized template
    using the row's own duration values.
    """
    template = get_phase_by_index(row["sequence_index"])
    if template is None:
        # Synthesize for phases not in the canonical 13-phase v0 library
        # (e.g., the full 24-phase library when 003 lands)
        template = PhaseTemplate(
            sequence_index=row["sequence_index"],
            name=row["name"],
            default_duration_days=row.get("default_duration_days") or 0,
        )

    duration = (
        row.get("project_override_duration_days")
        or row.get("default_duration_days")
        or template.default_duration_days
    )

    return Phase(
        sequence_index=row["sequence_index"],
        name=row["name"],
        duration_days=duration,
        planned_start_date=row["planned_start_date"],
        planned_end_date=row["planned_end_date"],
        template=template,
        status=row["status"],
        actual_start_date=row.get("actual_start_date"),
        actual_end_date=row.get("actual_end_date"),
    )


# ---------------------------------------------------------------------------
# Milestone read paths
# ---------------------------------------------------------------------------

def load_milestones_for_project(
    project_id: str,
    conn: psycopg.Connection | None = None,
) -> list[Milestone]:
    """Load milestones for a project, ordered by planned_date."""
    rows = _query_many(
        """
        SELECT
            id::text AS id,
            project_id::text AS project_id,
            phase_id::text AS phase_id,
            name,
            planned_date,
            actual_date,
            status
        FROM home_builder.milestone
        WHERE project_id = %s::uuid
        ORDER BY planned_date ASC NULLS LAST
        """,
        (project_id,),
        conn=conn,
    )
    return [
        Milestone(
            name=row["name"],
            planned_date=row["planned_date"],
            phase_id=row["phase_id"],
            status=row.get("status") or "pending",
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Schedule composition
# ---------------------------------------------------------------------------

def compose_schedule_from_db(
    project_id: str,
    conn: psycopg.Connection | None = None,
) -> Schedule | None:
    """Load a Project + its Phases + Milestones from Postgres and return a
    Schedule dataclass equivalent of what the in-memory scheduler produces.

    Returns None if the project doesn't exist or has no phases.
    """
    project = load_project_by_id(project_id, conn=conn)
    if not project:
        return None

    phases = load_phases_for_project(project_id, conn=conn)
    if not phases:
        return None

    milestones = load_milestones_for_project(project_id, conn=conn)

    # Estimated completion = last phase's end date (or actual if complete)
    last_phase = phases[-1]
    est_completion = last_phase.actual_end_date or last_phase.planned_end_date

    return Schedule(
        project_id=project["id"],
        project_name=project["name"],
        phases=phases,
        milestones=milestones,
        estimated_completion_date=est_completion,
        target_completion_date=project.get("target_completion_date"),
        target_framing_start_date=project.get("target_framing_start_date"),
        overrides_applied={},  # Reflected in each Phase.duration_days; not separately tracked here
    )


# ---------------------------------------------------------------------------
# Phase write paths
# ---------------------------------------------------------------------------

def save_phase_status_change(
    phase_id: str,
    status: str,
    actual_start_date: date | None = None,
    actual_end_date: date | None = None,
    conn: psycopg.Connection | None = None,
) -> bool:
    """Update a phase's status (and optionally actual dates).

    Used when hb-update / status_updater applies an NL update like
    "Foundation done" — flips the phase to 'complete' and stamps actual_end_date.

    Returns True on success, False if no row was updated.
    """
    sql = """
        UPDATE home_builder.phase
        SET
            status = %s,
            actual_start_date = COALESCE(%s, actual_start_date),
            actual_end_date   = COALESCE(%s, actual_end_date),
            updated_at = now()
        WHERE id = %s::uuid
    """
    params = (status, actual_start_date, actual_end_date, phase_id)

    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount > 0

    with connection(application_name="hb-engine-phase-write") as c:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _query_one(
    sql: str,
    params: tuple = (),
    *,
    conn: psycopg.Connection | None = None,
) -> dict | None:
    """Run a query expected to return 0 or 1 row; return the dict or None."""
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    with connection(application_name="hb-engine-read") as c:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def _query_many(
    sql: str,
    params: tuple = (),
    *,
    conn: psycopg.Connection | None = None,
) -> list[dict]:
    """Run a query expected to return 0+ rows; return the list of dicts."""
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    with connection(application_name="hb-engine-read") as c:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
