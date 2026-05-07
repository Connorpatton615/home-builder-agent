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
from home_builder_agent.scheduling.checklists import (
    Checklist,
    ChecklistItem,
    STUB_TEMPLATE_VERSION,
    load_template,
)
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


def load_active_projects(
    *,
    tenant_id: str | None = None,
    conn: psycopg.Connection | None = None,
) -> list[dict]:
    """List all non-archived projects, newest first.

    Used by the Chad Agent context loader (core.chad_context.get_chad_context)
    and any future "show me everything live" surface. Compact projection —
    skips heavy fields (drive_folder_path, contract_signed_at, timestamps)
    that the master agent doesn't need in its system prompt.
    """
    where: list[str] = ["status != 'archived'"]
    params: list = []
    if tenant_id is not None:
        where.append("tenant_id = %s::uuid")
        params.append(tenant_id)
    where_sql = "WHERE " + " AND ".join(where)

    sql = f"""
        SELECT
            id::text                  AS id,
            name,
            customer_name,
            status,
            target_completion_date,
            target_framing_start_date,
            tenant_id::text           AS tenant_id
        FROM home_builder.project
        {where_sql}
        ORDER BY created_at DESC
    """
    return _query_many(sql, tuple(params), conn=conn)


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
        db_id=row["id"],  # Canonical UUID from home_builder.phase.id — drives iOS UserAction.target_entity_id
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

# ---------------------------------------------------------------------------
# Schedule seed (insert) — used by hb-schedule --seed-postgres
# ---------------------------------------------------------------------------

def seed_schedule_to_db(
    schedule: Schedule,
    customer_name: str = "TBD",
    address: str | None = None,
    drive_folder_id: str | None = None,
    drive_folder_path: str | None = None,
    conn: psycopg.Connection | None = None,
) -> str:
    """Insert a Project + its Phases + Milestones from a computed Schedule
    into Postgres. Returns the new project_id (UUID).

    Used to bootstrap staging data and for the round-trip test:
      compute → seed → load back → verify equivalence.

    Each call creates a NEW project row (UUIDs are random). If you want
    idempotent re-seeds for the same name, delete the existing rows first
    (CASCADE on project_id will clean up phases + milestones).
    """
    own_conn = conn is None
    if own_conn:
        from home_builder_agent.integrations.postgres import connection
        conn_ctx = connection(application_name="hb-schedule-seed")
        conn = conn_ctx.__enter__()

    try:
        with conn.cursor() as cur:
            # 1. Insert Project
            cur.execute(
                """
                INSERT INTO home_builder.project (
                    name, customer_name, address,
                    target_completion_date, target_framing_start_date,
                    drive_folder_id, drive_folder_path,
                    status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')
                RETURNING id::text AS id
                """,
                (
                    schedule.project_name,
                    customer_name,
                    address,
                    schedule.target_completion_date,
                    schedule.target_framing_start_date,
                    drive_folder_id,
                    drive_folder_path,
                ),
            )
            row = cur.fetchone()
            project_uuid = row["id"]

            # 2. Insert Phases
            phase_id_by_seq: dict[int, str] = {}
            for p in schedule.phases:
                cur.execute(
                    """
                    INSERT INTO home_builder.phase (
                        project_id, phase_template_id, name, sequence_index,
                        status, planned_start_date, planned_end_date,
                        default_duration_days, project_override_duration_days
                    ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id::text AS id
                    """,
                    (
                        project_uuid,
                        _phase_template_slug(p.template.name),
                        p.name,
                        p.sequence_index,
                        p.status,
                        p.planned_start_date,
                        p.planned_end_date,
                        p.template.default_duration_days,
                        p.duration_days if p.duration_days != p.template.default_duration_days else None,
                    ),
                )
                phase_row = cur.fetchone()
                phase_id_by_seq[p.sequence_index] = phase_row["id"]

            # 3. Insert Milestones — link to Phase by name match where possible
            for m in schedule.milestones:
                # Engine milestones reference phase via Phase.id like "phase-XX";
                # extract the sequence_index to map to the new DB phase UUID
                target_phase_uuid = None
                if m.phase_id and m.phase_id.startswith("phase-"):
                    try:
                        seq = int(m.phase_id.replace("phase-", ""))
                        target_phase_uuid = phase_id_by_seq.get(seq)
                    except ValueError:
                        target_phase_uuid = None

                cur.execute(
                    """
                    INSERT INTO home_builder.milestone (
                        project_id, phase_id, name, planned_date, status
                    ) VALUES (%s::uuid, %s::uuid, %s, %s, %s)
                    """,
                    (
                        project_uuid,
                        target_phase_uuid,
                        m.name,
                        m.planned_date,
                        m.status,
                    ),
                )

        if own_conn:
            conn.commit()
        return project_uuid

    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def _phase_template_slug(name: str) -> str:
    """Convert phase name → stable slug for phase_template_id.

    Per Q-E in the migration review: phase_template_id is TEXT slug
    (e.g. 'precon', 'foundation'), not a UUID FK. The slug stays stable
    if the template table is recreated.
    """
    return (
        name.lower()
        .replace(" & ", "-and-")
        .replace(" + ", "-plus-")
        .replace(" ", "-")
        .replace("/", "-")
    )


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
# Checklist read/write paths (migration 005)
# ---------------------------------------------------------------------------
#
# Checklist + ChecklistItem are the gate per canonical-data-model.md §§ 6/7.
# Read path feeds view_models.checklist_gates_view; write path is the
# reconcile pass dispatching UserAction:checklist-tick to update item state.
# Engine-side dataclasses live in scheduling/checklists.py; templates
# (24 phase JSON files) live in scheduling/checklist_templates/.
#
# All three functions take an optional `conn` so the reconcile pass can
# wrap multi-step state changes in a single transaction.

def load_checklists_for_project(
    project_id: str,
    *,
    conn: psycopg.Connection | None = None,
) -> dict[str, Checklist]:
    """Load every Checklist for a Project, keyed by phase_id (UUID string).

    Empty dict means the project's phases haven't been seeded with
    checklists yet (fresh project, or seed_checklist_for_phase hasn't run
    against this project's phases). The view-model layer treats absent
    checklists as "no checklist data yet" — degraded-mode rendering.
    """
    cl_sql = """
        SELECT
            c.id::text             AS id,
            c.phase_id::text       AS phase_id,
            c.template_name,
            c.template_version
        FROM home_builder.checklist c
        JOIN home_builder.phase p ON p.id = c.phase_id
        WHERE p.project_id = %s::uuid
        ORDER BY p.sequence_index
    """
    cl_rows = _query_many(cl_sql, (project_id,), conn=conn)
    if not cl_rows:
        return {}

    cl_ids = [r["id"] for r in cl_rows]
    item_sql = """
        SELECT
            id::text               AS id,
            checklist_id::text     AS checklist_id,
            category,
            label,
            sort_index,
            is_complete,
            completed_by::text     AS completed_by,
            completed_at,
            notes
        FROM home_builder.checklist_item
        WHERE checklist_id = ANY(%s::uuid[])
        ORDER BY checklist_id, category, sort_index
    """
    item_rows = _query_many(item_sql, (cl_ids,), conn=conn)

    items_by_cl: dict[str, list[dict]] = {}
    for r in item_rows:
        items_by_cl.setdefault(r["checklist_id"], []).append(r)

    out: dict[str, Checklist] = {}
    for r in cl_rows:
        cl = Checklist(
            id=r["id"],
            phase_id=r["phase_id"],
            template_version=r["template_version"],
            items=[
                ChecklistItem(
                    id=it["id"],
                    category=it["category"],
                    label=it["label"],
                    is_complete=it["is_complete"],
                    completed_by=it.get("completed_by"),
                    completed_at=(
                        it["completed_at"].date()
                        if it.get("completed_at") is not None
                        and hasattr(it["completed_at"], "date")
                        else it.get("completed_at")
                    ),
                    notes=it.get("notes"),
                )
                for it in items_by_cl.get(r["id"], [])
            ],
        )
        out[r["phase_id"]] = cl
    return out


def seed_checklist_for_phase(
    phase_id: str,
    template_name: str,
    *,
    conn: psycopg.Connection | None = None,
) -> str:
    """Idempotently create a Checklist + items for a Phase from its template.

    Returns the checklist UUID (existing or newly inserted). If a checklist
    row already exists for `phase_id`, this is a no-op — the existing
    UUID is returned without touching items. Re-seeding to a newer
    template version is intentionally not handled here; that's a separate
    flow (regenerate_checklist_to_template_version).

    Stub case (template not on disk): inserts a Checklist row with no items.
    The empty-items-closes semantic in the engine treats this as a passthrough
    gate — same as canonical-data-model.md § 6 fallback.
    """
    existing = _query_one(
        "SELECT id::text FROM home_builder.checklist WHERE phase_id = %s::uuid",
        (phase_id,),
        conn=conn,
    )
    if existing:
        return existing["id"]

    template = load_template(template_name)
    template_version = (
        template.get("template_version", STUB_TEMPLATE_VERSION)
        if template is not None
        else STUB_TEMPLATE_VERSION
    )

    cl_row = _query_one(
        """
        INSERT INTO home_builder.checklist
            (phase_id, template_name, template_version)
        VALUES (%s::uuid, %s, %s)
        RETURNING id::text AS id
        """,
        (phase_id, template_name, template_version),
        conn=conn,
    )
    cl_id = cl_row["id"]

    if template is None:
        return cl_id  # stub — no items

    # Bulk-insert items.
    item_params: list[tuple] = []
    for cat in template.get("categories", []):
        cat_name = cat.get("name", "Uncategorized")
        for idx, label in enumerate(cat.get("items", [])):
            item_params.append((cl_id, cat_name, label, idx))

    if not item_params:
        return cl_id

    insert_sql = """
        INSERT INTO home_builder.checklist_item
            (checklist_id, category, label, sort_index)
        VALUES (%s::uuid, %s, %s, %s)
    """
    if conn is not None:
        with conn.cursor() as cur:
            cur.executemany(insert_sql, item_params)
    else:
        with connection(application_name="hb-engine-checklist-seed") as c:
            with c.cursor() as cur:
                cur.executemany(insert_sql, item_params)

    return cl_id


def update_checklist_item(
    item_id: str,
    *,
    is_complete: bool | None = None,
    completed_by: str | None = None,
    notes: str | None = None,
    conn: psycopg.Connection | None = None,
) -> bool:
    """Mutate a single ChecklistItem.

    Used by the reconcile pass on UserAction:checklist-tick. At least one
    of is_complete / notes must be supplied. If is_complete=True,
    completed_at is set to NOW() (or preserved if already set);
    if False, both completed_at and completed_by are cleared.

    Returns True if a row was updated, False if item_id wasn't found.
    """
    sets: list[str] = []
    params: list = []

    if is_complete is not None:
        sets.append("is_complete = %s")
        params.append(is_complete)
        if is_complete:
            sets.append("completed_at = COALESCE(completed_at, NOW())")
            if completed_by is not None:
                sets.append("completed_by = %s::uuid")
                params.append(completed_by)
        else:
            sets.append("completed_at = NULL")
            sets.append("completed_by = NULL")

    if notes is not None:
        sets.append("notes = %s")
        params.append(notes)

    if not sets:
        return False

    params.append(item_id)
    sql = f"""
        UPDATE home_builder.checklist_item
        SET {", ".join(sets)},
            updated_at = NOW()
        WHERE id = %s::uuid
    """

    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.rowcount > 0

    with connection(application_name="hb-engine-checklist-write") as c:
        with c.cursor() as cur:
            cur.execute(sql, tuple(params))
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


# ---------------------------------------------------------------------------
# Engine activity audit log — read-side
# ---------------------------------------------------------------------------
#
# hb-router is the sole writer to home_builder.engine_activity (Rule 3).
# Readers — hb-ask's get_recent_activity tool today, the iOS Activity tab
# route tomorrow — go through this loader so the column projection and
# ordering stay consistent.

def load_recent_engine_activity(
    *,
    project_id: str | None = None,
    actor_user_id: str | None = None,
    since_hours: int | None = None,
    limit: int = 25,
    conn: psycopg.Connection | None = None,
) -> list[dict]:
    """Read recent rows from home_builder.engine_activity, newest first.

    Args:
        project_id:     If set, filter to actions on this project.
        actor_user_id:  If set, filter to actions triggered by this user.
        since_hours:    If set, only include rows from the last N hours.
        limit:          Max rows to return (default 25).
        conn:           Reuse an existing connection; opens its own otherwise.

    Returns dicts with the engine_activity columns as keys, plus
    `created_at` rendered as a tz-aware ISO string for downstream
    display. Sorted by created_at DESC.
    """
    where: list[str] = []
    params: list = []

    if project_id is not None:
        where.append("project_id = %s::uuid")
        params.append(project_id)
    if actor_user_id is not None:
        where.append("actor_user_id = %s::uuid")
        params.append(actor_user_id)
    if since_hours is not None and since_hours > 0:
        where.append("created_at >= NOW() - (%s || ' hours')::interval")
        params.append(str(since_hours))

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT
            id::text                    AS id,
            actor_user_id::text         AS actor_user_id,
            project_id::text            AS project_id,
            surface,
            invoked_agent,
            user_intent,
            classified_command_type,
            parameters,
            outcome,
            result_summary,
            affected_entity_type,
            affected_entity_id::text    AS affected_entity_id,
            cost_usd,
            duration_ms,
            error_message,
            created_at
        FROM home_builder.engine_activity
        {where_sql}
        ORDER BY created_at DESC
        LIMIT %s
    """
    params.append(limit)

    rows = _query_many(sql, tuple(params), conn=conn)

    # Normalize: ISO-format created_at, decimal→float for cost.
    for r in rows:
        if r.get("created_at") is not None:
            r["created_at"] = r["created_at"].isoformat()
        if r.get("cost_usd") is not None:
            r["cost_usd"] = float(r["cost_usd"])
    return rows
