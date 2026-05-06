"""bridge.py — Drive Tracker → Postgres sync.

The "real Chad data" pipe. Phase A architecture has Drive Tracker sheets
as the canonical source of truth (where Chad and his agents have been
writing for weeks/months). Postgres is the engine state store the iOS
app reads from. This module bridges the two: read all Tracker sheets in
Drive, upsert their content into home_builder.* tables.

Idempotent — re-running against the same Trackers updates rather than
duplicates. Match key: home_builder.project.drive_folder_id.

V0 scope:
  - Project upsert (name, drive_folder_id, drive_folder_path,
    target_completion_date inferred from last phase's end)
  - Phase upsert (one row per Master Schedule row, mapped through the
    Drive→Postgres status enum + sequence_index uniqueness)

Out of v0 scope (deferred to bridge v1+):
  - Milestones (Tracker doesn't have a dedicated milestone tab today;
    derived in the engine from phase endpoints)
  - Change orders, inspections, deliveries, lien waivers from Tracker
    auxiliary tabs (these will sync individually as their tabs stabilize)
  - Cost Tracker → Postgres (separate sheet, separate sync)
  - Bidirectional sync (write-back from Postgres to Drive); per
    canonical-data-model § Schedule persistence strategy this is
    a Phase B concern
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import psycopg

from home_builder_agent.integrations import drive, sheets
from home_builder_agent.integrations.postgres import connection
from home_builder_agent.scheduling.store_postgres import _phase_template_slug


# ---------------------------------------------------------------------------
# Drive → Postgres status mapping
# ---------------------------------------------------------------------------
#
# Tracker sheets use Chad's vocabulary. Postgres uses canonical-data-model
# enum values. Map between them.
#
DRIVE_STATUS_TO_DB = {
    "done":         "complete",
    "completed":    "complete",
    "in progress":  "in-progress",
    "active":       "in-progress",
    "started":      "in-progress",
    "not started":  "not-started",
    "pending":      "not-started",
    "":             "not-started",
    "blocked":      "blocked-on-checklist",
    "delayed":      "blocked-on-checklist",
}


def _normalize_status(raw: str) -> str:
    s = (raw or "").strip().lower()
    return DRIVE_STATUS_TO_DB.get(s, "not-started")


def _parse_iso_date(s: str) -> date | None:
    """Parse a date string from a Tracker. Tries multiple formats because
    different Trackers use different display styles:
      - "2026-08-26"     ← canonical ISO
      - "Apr 28, 2026"   ← Whitfield-style (default Sheets short-month-name)
      - "April 28, 2026" ← long form
      - "4/28/2026"      ← US format
      - "Apr 8, 2026"    ← single-digit day
    """
    if not s:
        return None
    raw = str(s).strip()
    if not raw:
        return None

    formats = (
        "%Y-%m-%d",      # 2026-08-26
        "%b %d, %Y",     # Apr 28, 2026
        "%B %d, %Y",     # April 28, 2026
        "%m/%d/%Y",      # 4/28/2026
        "%-m/%-d/%Y",    # 4/8/2026 (POSIX-only on macOS, fine here)
    )
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _phase_duration_days(start: date | None, end: date | None) -> int | None:
    if not start or not end:
        return None
    days = (end - start).days + 1
    return days if days > 0 else None


# ---------------------------------------------------------------------------
# Per-tracker sync result types
# ---------------------------------------------------------------------------

@dataclass
class PhaseSyncOutcome:
    sequence_index: int
    name: str
    outcome: str  # 'inserted' | 'updated' | 'unchanged' | 'error'
    notes: str = ""


@dataclass
class TrackerSyncResult:
    tracker_name: str
    project_name: str
    drive_folder_id: str | None
    project_id: str | None  # the Postgres UUID
    project_outcome: str    # 'inserted' | 'updated' | 'unchanged' | 'error'
    phase_count: int = 0
    phase_outcomes: list[PhaseSyncOutcome] = field(default_factory=list)
    error: str | None = None

    def summary_counts(self) -> dict[str, int]:
        counts = {"inserted": 0, "updated": 0, "unchanged": 0, "error": 0}
        for p in self.phase_outcomes:
            counts[p.outcome] = counts.get(p.outcome, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Project upsert (find by drive_folder_id; insert if missing, update if found)
# ---------------------------------------------------------------------------

def _upsert_project(
    conn: psycopg.Connection,
    project_name: str,
    drive_folder_id: str | None,
    drive_folder_path: str | None,
    target_completion_date: date | None,
    target_framing_start_date: date | None,
) -> tuple[str, str]:
    """Returns (project_uuid, outcome) where outcome is 'inserted' | 'updated' | 'unchanged'."""
    with conn.cursor() as cur:
        # Try to find by drive_folder_id first (the most stable key)
        existing = None
        if drive_folder_id:
            cur.execute(
                "SELECT id::text AS id, target_completion_date, target_framing_start_date "
                "FROM home_builder.project WHERE drive_folder_id = %s LIMIT 1",
                (drive_folder_id,),
            )
            existing = cur.fetchone()

        # Fall back to name match (legacy projects without drive_folder_id)
        if not existing:
            cur.execute(
                "SELECT id::text AS id, target_completion_date, target_framing_start_date "
                "FROM home_builder.project "
                "WHERE name = %s AND status != 'archived' "
                "ORDER BY created_at DESC LIMIT 1",
                (project_name,),
            )
            existing = cur.fetchone()

        if existing:
            project_id = existing["id"]
            # UPDATE — refresh metadata, keep status / contract dates
            cur.execute(
                """
                UPDATE home_builder.project
                SET name = %s,
                    drive_folder_id = COALESCE(%s, drive_folder_id),
                    drive_folder_path = COALESCE(%s, drive_folder_path),
                    target_completion_date = COALESCE(%s, target_completion_date),
                    target_framing_start_date = COALESCE(%s, target_framing_start_date),
                    updated_at = now()
                WHERE id = %s::uuid
                """,
                (
                    project_name,
                    drive_folder_id,
                    drive_folder_path,
                    target_completion_date,
                    target_framing_start_date,
                    project_id,
                ),
            )
            return project_id, "updated"

        # INSERT — new project
        # Customer name is required NOT NULL per the schema, so default to 'TBD'
        # Bridge can't infer it from Tracker; Project Info tab will fill it later.
        cur.execute(
            """
            INSERT INTO home_builder.project (
                name, customer_name,
                drive_folder_id, drive_folder_path,
                target_completion_date, target_framing_start_date,
                status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'active')
            RETURNING id::text AS id
            """,
            (
                project_name,
                "TBD",  # customer_name fallback
                drive_folder_id,
                drive_folder_path,
                target_completion_date,
                target_framing_start_date,
            ),
        )
        return cur.fetchone()["id"], "inserted"


# ---------------------------------------------------------------------------
# Phase upsert (uniqueness on project_id + sequence_index)
# ---------------------------------------------------------------------------

def _upsert_phase(
    conn: psycopg.Connection,
    project_id: str,
    sequence_index: int,
    name: str,
    status: str,
    planned_start_date: date | None,
    planned_end_date: date | None,
) -> str:
    """Returns 'inserted' | 'updated' | 'unchanged'."""
    duration = _phase_duration_days(planned_start_date, planned_end_date)
    template_id = _phase_template_slug(name)

    with conn.cursor() as cur:
        # Check existing
        cur.execute(
            """
            SELECT id::text AS id, name, status, planned_start_date, planned_end_date
            FROM home_builder.phase
            WHERE project_id = %s::uuid AND sequence_index = %s
            """,
            (project_id, sequence_index),
        )
        existing = cur.fetchone()

        if existing:
            # Detect changes
            unchanged = (
                existing["name"] == name
                and existing["status"] == status
                and existing["planned_start_date"] == planned_start_date
                and existing["planned_end_date"] == planned_end_date
            )
            if unchanged:
                return "unchanged"
            cur.execute(
                """
                UPDATE home_builder.phase
                SET name = %s,
                    phase_template_id = %s,
                    status = %s,
                    planned_start_date = %s,
                    planned_end_date = %s,
                    default_duration_days = COALESCE(%s, default_duration_days),
                    updated_at = now()
                WHERE id = %s::uuid
                """,
                (name, template_id, status, planned_start_date, planned_end_date,
                 duration, existing["id"]),
            )
            return "updated"

        # INSERT
        cur.execute(
            """
            INSERT INTO home_builder.phase (
                project_id, phase_template_id, name, sequence_index,
                status, planned_start_date, planned_end_date,
                default_duration_days
            ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s)
            """,
            (project_id, template_id, name, sequence_index, status,
             planned_start_date, planned_end_date, duration),
        )
        return "inserted"


# ---------------------------------------------------------------------------
# Top-level: sync one tracker
# ---------------------------------------------------------------------------

def sync_tracker(
    drive_svc,
    sheets_svc,
    tracker: dict,
    *,
    dry_run: bool = False,
) -> TrackerSyncResult:
    """Sync one Drive Tracker into Postgres. Returns a TrackerSyncResult.

    Args:
        drive_svc:    authenticated Drive service
        sheets_svc:   authenticated Sheets service
        tracker:      dict from drive.find_*_trackers — has 'id', 'name'
        dry_run:      read everything, compute outcome, but don't COMMIT

    The tracker dict is the spreadsheet metadata. We read its Master
    Schedule tab to get phase rows.
    """
    project_name = drive.extract_project_name(tracker["name"])
    result = TrackerSyncResult(
        tracker_name=tracker["name"],
        project_name=project_name,
        drive_folder_id=None,
        project_id=None,
        project_outcome="error",
    )

    try:
        # Ask Drive for the parent folder of this Tracker — that's the
        # project folder, the stable identifier we anchor on
        meta = drive_svc.files().get(
            fileId=tracker["id"],
            fields="id,name,parents,webViewLink",
        ).execute()
        parents = meta.get("parents") or []
        drive_folder_id = parents[0] if parents else None
        result.drive_folder_id = drive_folder_id

        drive_folder_path = None
        if drive_folder_id:
            try:
                folder_meta = drive_svc.files().get(
                    fileId=drive_folder_id, fields="name"
                ).execute()
                drive_folder_path = folder_meta.get("name")
            except Exception:
                pass

        # Read Master Schedule from the Tracker
        phase_rows = sheets.read_master_schedule(sheets_svc, tracker["id"])
        result.phase_count = len(phase_rows)

        if not phase_rows:
            result.error = "Master Schedule tab is empty or missing"
            return result

        # Compute target_completion_date = last phase's End date
        last_end = None
        for row in reversed(phase_rows):
            d = _parse_iso_date(row.get("End", ""))
            if d:
                last_end = d
                break

        # Compute target_framing_start_date = framing phase's Start date (if found)
        framing_start = None
        for row in phase_rows:
            phase_name_lc = (row.get("Phase") or "").strip().lower()
            if "framing" in phase_name_lc and "rough" not in phase_name_lc:
                framing_start = _parse_iso_date(row.get("Start", ""))
                if framing_start:
                    break

        with connection(application_name="hb-bridge") as conn:
            project_id, project_outcome = _upsert_project(
                conn,
                project_name=project_name,
                drive_folder_id=drive_folder_id,
                drive_folder_path=drive_folder_path,
                target_completion_date=last_end,
                target_framing_start_date=framing_start,
            )
            result.project_id = project_id
            result.project_outcome = project_outcome

            # Phases
            for row in phase_rows:
                seq_raw = row.get("#", "")
                try:
                    seq_index = int(seq_raw) if seq_raw else None
                except (ValueError, TypeError):
                    seq_index = None
                if seq_index is None:
                    result.phase_outcomes.append(
                        PhaseSyncOutcome(
                            sequence_index=-1,
                            name=row.get("Phase", "(no name)"),
                            outcome="error",
                            notes=f"non-integer # column: {seq_raw!r}",
                        )
                    )
                    continue

                name = (row.get("Phase") or "").strip()
                if not name:
                    continue

                status = _normalize_status(row.get("Status", ""))
                start = _parse_iso_date(row.get("Start", ""))
                end = _parse_iso_date(row.get("End", ""))

                try:
                    outcome = _upsert_phase(
                        conn,
                        project_id=project_id,
                        sequence_index=seq_index,
                        name=name,
                        status=status,
                        planned_start_date=start,
                        planned_end_date=end,
                    )
                    result.phase_outcomes.append(
                        PhaseSyncOutcome(
                            sequence_index=seq_index,
                            name=name,
                            outcome=outcome,
                        )
                    )
                except Exception as e:
                    result.phase_outcomes.append(
                        PhaseSyncOutcome(
                            sequence_index=seq_index,
                            name=name,
                            outcome="error",
                            notes=f"{type(e).__name__}: {e}",
                        )
                    )

            if dry_run:
                conn.rollback()
                result.project_outcome = result.project_outcome + "(dry-run)"
            else:
                conn.commit()

    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"

    return result


# ---------------------------------------------------------------------------
# Top-level: sync all trackers
# ---------------------------------------------------------------------------

def sync_all_trackers(
    drive_svc,
    sheets_svc,
    folder_path: list[str],
    *,
    dry_run: bool = False,
    name_filter: str | None = None,
) -> list[TrackerSyncResult]:
    """Sync every Tracker in `folder_path` to Postgres.

    If name_filter is provided, only sync trackers whose project_name
    contains it (case-insensitive substring).
    """
    trackers = drive.find_all_trackers(drive_svc, folder_path)
    results = []

    for tracker in trackers:
        project_name = drive.extract_project_name(tracker["name"])
        if name_filter and name_filter.lower() not in project_name.lower():
            continue
        result = sync_tracker(drive_svc, sheets_svc, tracker, dry_run=dry_run)
        results.append(result)

    return results
