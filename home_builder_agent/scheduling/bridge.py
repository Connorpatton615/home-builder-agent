"""bridge.py — Drive Tracker → Postgres canonical sync.

The "real Chad data" pipe. Per ADR 2026-05-11 (superseded), an earlier
attempt flipped Postgres to be canonical with a one-way Postgres → Sheets
mirror. That broke Chad's actual workflow: Chad lives in Google Sheets
and edits the Master Schedule + Project Info tabs directly. Per the
superseding ADR (also 2026-05-11), the architecture is now:

  - Google Sheets Tracker is canonical for raw editable state
    (phase names/dates/statuses, customer info)
  - Postgres is the canonical query store for the dashboard + every
    field hb-chad generates (drafts, dispatches, events, KPIs).
  - This module syncs Sheets → Postgres every 5 minutes via the
    `com.chadhomes.bridge-sync` launchd job. Chad's manual Sheets edits
    propagate to the dashboard within ~5 min.
  - hb-chad write tools dual-write: they write Sheets FIRST (the
    canonical surface), then Postgres (for instant dashboard
    visibility). See agents/chad_agent.py.

This sync is upsert + selective-delete:
  - Upsert: every Tracker phase row → home_builder.phase, matched
    by (project_id, sequence_index). Missing-from-Tracker rows are
    DELETEd (so Chad's manual row-clears actually clear Postgres).
  - Project: upsert by drive_folder_id (or name as fallback).
  - Idempotent — re-running against the same Trackers is a no-op
    when nothing has changed.

Out of scope (won't change tonight):
  - Milestones (Tracker doesn't have a dedicated milestone tab today)
  - Change orders, inspections, deliveries, lien waivers from Tracker
    auxiliary tabs
  - Cost Tracker → Postgres (separate sheet, separate sync)

`mirror_worker.py` (Postgres → Sheets) is kept on disk as DEAD code
under the superseded ADR — it'll be removed next pass once the
restored direction proves stable.
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
    """Sync one Drive Tracker INTO Postgres (the canonical direction).

    Args:
        drive_svc:    authenticated Drive service
        sheets_svc:   authenticated Sheets service
        tracker:      dict from drive.find_*_trackers — has 'id', 'name'
        dry_run:      read everything, compute outcome, but don't COMMIT

    The tracker dict is the spreadsheet metadata. We read its Master
    Schedule tab to get phase rows, upsert them into home_builder.phase
    matched by (project_id, sequence_index), and DELETE any Postgres
    phase rows whose sequence_index is no longer in the Tracker — so
    Chad's manual row-clears in Sheets propagate as actual deletes.

    Returns a TrackerSyncResult with per-phase outcomes ('inserted',
    'updated', 'unchanged', 'deleted', 'error').
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

        # DEFENSIVE HEADER CHECK — refuse to proceed if the expected
        # columns aren't all present. This is the guard that catches
        # the 2026-05-11 destructive incident: an earlier Postgres →
        # Sheets mirror stamped a "Read-only mirror — last sync …"
        # string into cell A1, which clobbered the "#" header. Without
        # this check, every phase row's "#" lookup returns "" → all
        # rows fail parse → tracker_seq_indices is empty → DELETE-ALL
        # branch fires → every phase row in Postgres gets nuked along
        # with its cascade-attached checklists/tasks/dependencies.
        #
        # If a row doesn't carry the expected keys, the dict-by-header
        # contract is broken — abort the sync for this Tracker. Never
        # DELETE based on a misparsed Sheets read.
        first_row = phase_rows[0]
        required_keys = {"#", "Phase"}
        missing = required_keys - set(first_row.keys())
        if missing:
            result.error = (
                f"Master Schedule header check FAILED — missing required "
                f"column(s): {sorted(missing)}. Found columns: "
                f"{sorted(first_row.keys())}. Refusing to proceed (would "
                f"otherwise interpret missing-# as 'delete every phase'). "
                "Open the Tracker and confirm row 1 has the canonical "
                "headers: #, Phase, Weeks, Start, End, Status, Dependencies."
            )
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

            # Phases — collect the set of sequence_indices present in
            # the Tracker so we can DELETE any Postgres orphans below.
            tracker_seq_indices: set[int] = set()
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

                tracker_seq_indices.add(seq_index)
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

            # DELETE orphan phases — rows in Postgres whose sequence_index
            # isn't in the Tracker anymore (Chad manually cleared/deleted
            # the row from Master Schedule). Cascades through the
            # ON DELETE CASCADE FKs to checklist + checklist_item + task
            # + dependency rows attached to those phases.
            #
            # SAFETY: refuse to delete more than 50% of phases in a
            # single sync — too destructive to do silently. If that
            # bound is exceeded, log + return error and leave Postgres
            # state untouched (transaction rolls back). The header
            # check above is the first line of defense; this is the
            # second. Both have to fail for nuclear deletes to land.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM home_builder.phase
                    WHERE project_id = %s::uuid AND sequence_index != 0
                    """,
                    (project_id,),
                )
                row = cur.fetchone()
                postgres_phase_count = row["n"] if row else 0
                would_delete_count = postgres_phase_count - len(tracker_seq_indices)
                if (
                    postgres_phase_count >= 4
                    and would_delete_count > postgres_phase_count // 2
                ):
                    result.error = (
                        f"DELETE safety guard tripped — sync would remove "
                        f"{would_delete_count} of {postgres_phase_count} "
                        f"phases (>50%). Refusing to proceed. Confirm the "
                        f"Tracker's Master Schedule tab has the expected "
                        f"rows; rollback any accidental deletes; re-run."
                    )
                    raise RuntimeError(result.error)  # forces txn rollback

                if tracker_seq_indices:
                    placeholders = ",".join(["%s"] * len(tracker_seq_indices))
                    cur.execute(
                        f"""
                        DELETE FROM home_builder.phase
                        WHERE project_id = %s::uuid
                          AND sequence_index != 0
                          AND sequence_index NOT IN ({placeholders})
                        RETURNING sequence_index, name
                        """,
                        (project_id, *tracker_seq_indices),
                    )
                else:
                    # Tracker has zero phase rows. Only reach this branch
                    # if the >50% guard above didn't trip — i.e. the
                    # project had < 4 phases to start. Delete them all.
                    # sequence_index = 0 is the reorder_phase park-and-
                    # swap sentinel — never delete one of those.
                    cur.execute(
                        """
                        DELETE FROM home_builder.phase
                        WHERE project_id = %s::uuid
                          AND sequence_index != 0
                        RETURNING sequence_index, name
                        """,
                        (project_id,),
                    )
                for deleted_row in cur.fetchall():
                    result.phase_outcomes.append(
                        PhaseSyncOutcome(
                            sequence_index=deleted_row["sequence_index"],
                            name=deleted_row["name"],
                            outcome="deleted",
                            notes="absent from Tracker — propagated as DELETE",
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
    """Sync every Tracker in `folder_path` INTO Postgres.

    If name_filter is provided, only sync trackers whose project_name
    contains it (case-insensitive substring).
    """
    trackers = drive.find_all_trackers(drive_svc, folder_path)
    results = []

    for tracker in trackers:
        project_name = drive.extract_project_name(tracker["name"])
        if name_filter and name_filter.lower() not in project_name.lower():
            continue
        result = sync_tracker(
            drive_svc, sheets_svc, tracker, dry_run=dry_run,
        )
        results.append(result)

    return results
