"""backfill_project_info_2026-05-11.py — one-time Tracker → Postgres backfill.

Context: migration 011 (2026-05-11) added customer_email, customer_phone,
job_code, and notes columns to `home_builder.project`. Per ADR
2026-05-11, Postgres is now the canonical store; a Postgres → Sheets
mirror worker is being built in parallel that will overwrite the
Tracker Project Info tab on its first run with whatever's in Postgres.

For active projects whose Tracker Project Info tab has values that
Postgres doesn't (customer_email, customer_phone, address, job_code,
notes), Postgres must be backfilled BEFORE the mirror's first run, or
those Tracker values get blanked out.

Scope:
  - Active projects only (status = 'active' AND drive_folder_id IS NOT NULL).
  - Project Info tab only — no Master Schedule, Cost Tracker, or
    historical phase data.
  - Builder field is SKIPPED (always "Palmetto Custom Homes", not stored).

Hard constraint: never overwrite a non-empty non-TBD Postgres value
with an empty Tracker value. Postgres data wins when present.

Idempotency: re-running --apply is a no-op once values match. The UPDATE
only fires when at least one column is changing.

Usage:
    # Dry-run (default) — print planned writes, no commit:
    PYTHONPATH=/Users/connorpatton/Projects/home-builder-agent \\
      /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \\
      scripts/backfill_project_info_2026-05-11.py --dry-run

    # Apply for real:
    PYTHONPATH=/Users/connorpatton/Projects/home-builder-agent \\
      /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \\
      scripts/backfill_project_info_2026-05-11.py --apply
"""

from __future__ import annotations

import argparse
import sys

from home_builder_agent.config import DRIVE_FOLDER_PATH
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.integrations import drive, sheets
from home_builder_agent.integrations.postgres import connection


# Tracker field name -> Postgres column name.
# Builder is intentionally absent (always "Palmetto Custom Homes", not stored).
FIELD_MAP: list[tuple[str, str]] = [
    ("Customer Name", "customer_name"),
    ("Customer Email", "customer_email"),
    ("Customer Phone", "customer_phone"),
    ("Project Address", "address"),
    ("Job Code", "job_code"),
    ("Notes", "notes"),
]


def _is_postgres_empty(col: str, value) -> bool:
    """A Postgres cell counts as 'empty' (eligible for backfill) if it's
    NULL, an empty string, or — only for customer_name — the literal
    'TBD' fallback that the bridge inserts when a project has no
    Project Info tab seeded.
    """
    if value is None:
        return True
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return True
        if col == "customer_name" and s.upper() == "TBD":
            return True
    return False


def _normalize_tracker_value(raw) -> str:
    """Strip whitespace and treat empty as truly empty. Returns ''
    when the Tracker doesn't have a useful value to copy.
    """
    if raw is None:
        return ""
    return str(raw).strip()


def _plan_updates_for_project(
    pg_row: dict,
    tracker_info: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Compute (sets, skip_log) for one project.

    sets is {postgres_column: new_value} for every column that will be
    written. skip_log is a list of human-readable reasons for fields
    NOT being written.
    """
    sets: dict[str, str] = {}
    skip_log: list[str] = []

    for tracker_field, pg_col in FIELD_MAP:
        tracker_val = _normalize_tracker_value(tracker_info.get(tracker_field))
        pg_val = pg_row.get(pg_col)
        pg_empty = _is_postgres_empty(pg_col, pg_val)

        if not tracker_val:
            if pg_empty:
                skip_log.append(f"{pg_col}: tracker blank, postgres blank — no-op")
            else:
                # HARD CONSTRAINT: never blank out a real Postgres value.
                skip_log.append(
                    f"{pg_col}: tracker blank, postgres has {pg_val!r} — KEEP postgres"
                )
            continue

        if not pg_empty:
            skip_log.append(
                f"{pg_col}: tracker={tracker_val!r}, postgres={pg_val!r} — KEEP postgres"
            )
            continue

        sets[pg_col] = tracker_val

    return sets, skip_log


def _apply_updates(
    conn,
    project_id: str,
    sets: dict[str, str],
    *,
    dry_run: bool,
) -> None:
    """Issue a single UPDATE for the project. Caller is responsible for
    the surrounding transaction; we only execute. Empty `sets` is a
    no-op (we never issue a no-column UPDATE).
    """
    if not sets:
        return
    cols = sorted(sets.keys())
    assignments = ", ".join(f"{c} = %s" for c in cols)
    params: list = [sets[c] for c in cols]
    params.append(project_id)
    sql = (
        f"UPDATE home_builder.project SET {assignments}, updated_at = now() "
        f"WHERE id = %s::uuid"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _select_candidates(conn) -> list[dict]:
    """Fetch every active project that has a drive_folder_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text AS id,
                   name,
                   drive_folder_id,
                   customer_name,
                   customer_email,
                   customer_phone,
                   address,
                   job_code,
                   notes
            FROM home_builder.project
            WHERE status = 'active' AND drive_folder_id IS NOT NULL
            ORDER BY name
            """
        )
        return list(cur.fetchall())


def run(*, apply: bool) -> int:
    """Returns a process exit code. 0 on success, 1 on any tracker
    resolution failure (per-project failures are logged but do not
    abort the whole pass, except they bump the exit code).
    """
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"=== Project Info backfill — mode: {mode} ===")
    print()

    creds = get_credentials()
    drive_svc = drive.drive_service(creds)
    sheets_svc = sheets.sheets_service(creds)

    any_error = False
    total_projects = 0
    total_writes = 0
    total_columns_written = 0

    with connection(application_name="hb-backfill-project-info-2026-05-11") as conn:
        candidates = _select_candidates(conn)

        if not candidates:
            print("No active projects with drive_folder_id. Nothing to do.")
            return 0

        print(f"Found {len(candidates)} candidate project(s).")
        print()

        for pg_row in candidates:
            total_projects += 1
            name = pg_row["name"]
            project_id = pg_row["id"]
            print(f"--- {name} (id={project_id}) ---")

            # Find the Tracker. `find_tracker_by_project` walks the GENERATED
            # TIMELINES folder and matches on extract_project_name() — same
            # helper bridge.py/client-update use.
            try:
                tracker = drive.find_tracker_by_project(
                    drive_svc, DRIVE_FOLDER_PATH, name
                )
            except Exception as e:
                print(f"  ERROR finding tracker: {type(e).__name__}: {e}")
                any_error = True
                print()
                continue

            if not tracker:
                print(f"  SKIP: no Tracker found in {' / '.join(DRIVE_FOLDER_PATH)}")
                print()
                continue

            print(f"  Tracker: {tracker['name']} (id={tracker['id']})")

            try:
                tracker_info = sheets.read_project_info(sheets_svc, tracker["id"])
            except Exception as e:
                print(f"  ERROR reading Project Info tab: {type(e).__name__}: {e}")
                any_error = True
                print()
                continue

            if not tracker_info:
                print("  SKIP: Tracker has no Project Info tab (or empty)")
                print()
                continue

            # Log the raw view for debuggability
            for f, _ in FIELD_MAP:
                print(f"    tracker[{f!r}] = {tracker_info.get(f, '<missing>')!r}")

            sets, skip_log = _plan_updates_for_project(pg_row, tracker_info)

            for line in skip_log:
                print(f"  skip: {line}")
            for col, val in sorted(sets.items()):
                print(f"  WRITE: {col} := {val!r}")

            if not sets:
                print("  (no columns to write)")
            else:
                _apply_updates(conn, project_id, sets, dry_run=not apply)
                total_writes += 1
                total_columns_written += len(sets)

            print()

        # Commit / rollback
        if apply:
            conn.commit()
            print(f"=== APPLIED. Projects updated: {total_writes}, columns written: {total_columns_written}. ===")
        else:
            conn.rollback()
            print(f"=== DRY-RUN complete. Would update {total_writes} project(s), {total_columns_written} column(s). No commit. ===")

    if any_error:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "One-time backfill of home_builder.project columns from "
            "Tracker Project Info tabs (migration 011)."
        )
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned writes; do NOT commit. Default if neither flag is set.",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Commit the writes to Postgres.",
    )
    args = parser.parse_args(argv)

    # Default is dry-run when neither flag is given.
    apply = bool(args.apply)
    return run(apply=apply)


if __name__ == "__main__":
    sys.exit(main())
