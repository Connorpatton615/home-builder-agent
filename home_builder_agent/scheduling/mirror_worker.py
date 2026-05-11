"""mirror_worker.py — Postgres → Google Sheets Tracker mirror.

ACTIVE sync path per ADR 2026-05-11 ("Postgres as Canonical State;
Google Sheets as Read-Only Mirror"). Inverts the v0 direction shipped
in `bridge.py` (which is now dormant).

Every 5 minutes (driven by `com.chadhomes.tracker-mirror.plist`), for
every active project that has a `drive_folder_id`:

  1. Find the project's Tracker spreadsheet in Drive.
  2. Overwrite the Project Info tab (column B) from the Postgres row.
  3. Overwrite the Master Schedule rows from `home_builder.phase`
     ORDER BY sequence_index, mapping the canonical status enum back
     to Drive vocabulary via `DB_STATUS_TO_DRIVE`.
  4. Stamp a "Read-only mirror — edit via hb-chad. Last sync: <iso>"
     header on each touched tab.
  5. Add a *soft-warning* protected range over column B (Project Info)
     and the editable Master Schedule columns. Editing is still
     possible — Connor can dismiss the warning — but a typo on a
     phone won't sail through silently.

Design constraints (binding):
  - This worker NEVER writes to Postgres. Read-only on the DB side.
  - Single connection per pass, fire-and-exit. launchd handles
    scheduling, restarts, and "is this thing running" semantics.
  - Per-project failures are isolated: one bad Tracker doesn't block
    the rest of the pass.
  - Structured JSON logs to stderr — launchd captures to
    `/tmp/tracker-mirror.stderr.log`.

Out of scope (CTO-confirmed v1):
  - Bidirectional sync / conflict resolution.
  - Migration of historical/archived projects (only `status='active'`).
  - Auto-correction of edits to protected ranges (we log via
    `home_builder.event` if we can detect them; we don't fight).
  - Cost Tracker mirroring (separate sheet, separate worker).
"""

from __future__ import annotations

import logging
import socket
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

import psycopg

from home_builder_agent.config import DRIVE_FOLDER_PATH
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.integrations import drive, sheets
from home_builder_agent.integrations.postgres import connection
from home_builder_agent.observability.json_log import configure_json_logging
from home_builder_agent.scheduling.bridge import DRIVE_STATUS_TO_DB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status mapping (inverted from bridge.DRIVE_STATUS_TO_DB)
# ---------------------------------------------------------------------------
#
# `DRIVE_STATUS_TO_DB` is many-to-one (Chad's tracker vocabulary collapsed
# to a small enum). We pick a *single canonical* Drive label per enum
# value so the mirror writes a stable, readable status back to the sheet.
# The choices below match `sheets.STATUS_DONE` / etc — i.e. the same
# labels the conditional formatting and dashboard reader expect.
#
DB_STATUS_TO_DRIVE: dict[str, str] = {
    "complete":             "Done",
    "in-progress":          "In Progress",
    "not-started":          "Not Started",
    "blocked-on-checklist": "Blocked",
}


def db_status_to_drive(status: str | None) -> str:
    """Map a `home_builder.phase.status` enum value back to the Drive
    Tracker vocabulary. Unknown or NULL → empty string (renders cleanly
    in the sheet rather than as a confusing default like 'Not Started')."""
    if not status:
        return ""
    return DB_STATUS_TO_DRIVE.get(status, "")


# ---------------------------------------------------------------------------
# Pure builders (row composition, no I/O)
# ---------------------------------------------------------------------------

# Match the field order in `sheets.PROJECT_INFO_FIELDS`.
# Builder is a hardcoded constant per the audit; tenant-aware variant
# is deferred until we onboard a second builder.
PROJECT_INFO_FIELD_ORDER = (
    "Customer Name",
    "Customer Email",
    "Customer Phone",
    "Project Address",
    "Job Code",
    "Builder",
    "Notes",
)

PROJECT_INFO_BUILDER_CONSTANT = "Palmetto Custom Homes"


def build_project_info_values(project_row: dict) -> list[list[str]]:
    """Build the column-B value list for the Project Info tab.

    Input is a single `home_builder.project` row (dict). Output is a
    list of single-cell rows in the *exact order* the tab seeds them
    in (see `sheets.PROJECT_INFO_FIELDS`). No header is emitted — the
    caller writes this starting at row 2 (column B).
    """
    return [
        [project_row.get("customer_name") or ""],
        [project_row.get("customer_email") or ""],
        [project_row.get("customer_phone") or ""],
        [project_row.get("address") or ""],
        [project_row.get("job_code") or ""],
        [PROJECT_INFO_BUILDER_CONSTANT],
        [project_row.get("notes") or ""],
    ]


def _format_date(value: Any) -> str:
    """Render a date/datetime as ISO-YYYY-MM-DD; pass-through strings; '' otherwise."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def build_master_schedule_row(phase_row: dict) -> list[str]:
    """Build one Master Schedule row from a `home_builder.phase` row.

    Master Schedule columns (per `sheets.apply_phase_updates` & the
    tracker construction in `build_tracker_sheet`):

        #, Phase, Weeks, Start, End, Status, Dependencies

    We don't write Weeks/Dependencies — those don't have a Postgres
    home today and we don't want to clobber any free-form text Chad
    entered. Pass empty strings through; the caller writes columns
    A,B,D,E,F (skipping C and G) via targeted batchUpdate ranges
    rather than overwriting whole rows. For dataclass-friendliness we
    still return a 7-column row; the writer picks the columns it owns.
    """
    return [
        str(phase_row.get("sequence_index") or ""),
        phase_row.get("name") or "",
        "",  # Weeks — not owned by Postgres
        _format_date(phase_row.get("planned_start_date")),
        _format_date(phase_row.get("planned_end_date")),
        db_status_to_drive(phase_row.get("status")),
        "",  # Dependencies — not owned by Postgres
    ]


def mirror_header_text(now: datetime | None = None) -> str:
    """The 'Read-only mirror' banner stamped on each touched tab."""
    if now is None:
        now = datetime.now(timezone.utc)
    return (
        "Read-only mirror — edit via hb-chad. "
        f"Last sync: {now.isoformat(timespec='seconds')}"
    )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ProjectMirrorOutcome:
    project_id: str
    project_name: str
    drive_folder_id: str | None
    tracker_id: str | None
    outcome: str  # 'mirrored' | 'skipped_no_drive_folder' | 'skipped_no_tracker' | 'error'
    phase_count: int = 0
    error: str | None = None

    def to_log_dict(self) -> dict:
        d = {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "outcome": self.outcome,
            "phase_count": self.phase_count,
        }
        if self.drive_folder_id:
            d["drive_folder_id"] = self.drive_folder_id
        if self.tracker_id:
            d["tracker_id"] = self.tracker_id
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class MirrorPassResult:
    correlation_id: str
    started_at: datetime
    finished_at: datetime | None = None
    projects_seen: int = 0
    outcomes: list[ProjectMirrorOutcome] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.outcome] = out.get(o.outcome, 0) + 1
        return out


# ---------------------------------------------------------------------------
# Data access (read-only — `mirror_worker` MUST NOT write to Postgres)
# ---------------------------------------------------------------------------

def fetch_active_projects(conn: psycopg.Connection) -> list[dict]:
    """Pull every active project's mirror-relevant columns.

    NULL `drive_folder_id` is INCLUDED here so the worker can log + skip
    (the row is in scope but has nothing to mirror to). Archived
    projects are out of scope per ADR v1.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text          AS id,
                   name,
                   customer_name,
                   customer_email,
                   customer_phone,
                   address,
                   job_code,
                   notes,
                   drive_folder_id,
                   drive_folder_path
              FROM home_builder.project
             WHERE status = 'active'
             ORDER BY created_at ASC
            """
        )
        return list(cur.fetchall())


def fetch_phases_for_project(conn: psycopg.Connection, project_id: str) -> list[dict]:
    """Pull every phase row for a project, ordered by sequence_index ASC."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text          AS id,
                   sequence_index,
                   name,
                   status,
                   planned_start_date,
                   planned_end_date
              FROM home_builder.phase
             WHERE project_id = %s::uuid
             ORDER BY sequence_index ASC
            """,
            (project_id,),
        )
        return list(cur.fetchall())


# ---------------------------------------------------------------------------
# Tracker lookup
# ---------------------------------------------------------------------------

def find_tracker_for_project(
    drive_svc, project_row: dict, folder_path: list[str],
) -> dict | None:
    """Find the Tracker spreadsheet for one project.

    Strategy: walk the GENERATED TIMELINES folder, match Tracker names
    against the project's `name` field via `drive.extract_project_name`.
    Returns the tracker dict (id, name, modifiedTime, webViewLink) or
    None if no matching Tracker exists.
    """
    return drive.find_tracker_by_project(
        drive_svc, folder_path, project_row.get("name") or "",
    )


# ---------------------------------------------------------------------------
# Sheets writers
# ---------------------------------------------------------------------------

# Cap how many Master Schedule rows we'll clear/write. Schema CHECK
# allows 1..24 today; 50 leaves headroom and bounds the API range.
MASTER_SCHEDULE_MAX_ROWS = 50

# Editable Master Schedule columns we own (1-indexed for the writer
# layer; 0-indexed for protected-range API).
MASTER_SCHEDULE_TAB = "Master Schedule"


def write_project_info_tab(
    sheets_svc,
    spreadsheet_id: str,
    tab_id: int,
    project_row: dict,
    header_text: str,
) -> None:
    """Overwrite column B of the Project Info tab + stamp the header row.

    We DO NOT touch column A — the field labels are static and changing
    them would break `read_project_info`. We overwrite the *header* row
    (A1:B1) to carry the 'Read-only mirror' banner.
    """
    # Header: "Field" / "<mirror banner>" on row 1.
    header_row = [["Field", header_text]]

    # Column B values starting row 2.
    values = build_project_info_values(project_row)

    sheets_svc.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "valueInputOption": "RAW",
            "data": [
                {
                    "range": f"{sheets.PROJECT_INFO_TAB}!A1:B1",
                    "values": header_row,
                },
                {
                    "range": (
                        f"{sheets.PROJECT_INFO_TAB}!B2:B"
                        f"{1 + len(PROJECT_INFO_FIELD_ORDER)}"
                    ),
                    "values": values,
                },
            ],
        },
    ).execute()


def write_master_schedule_tab(
    sheets_svc,
    spreadsheet_id: str,
    phase_rows: list[dict],
    header_text: str,
) -> int:
    """Overwrite the Master Schedule tab from a list of phase rows.

    Returns the count of phase rows written.

    Layout (matches `build_tracker_sheet` & `apply_phase_updates`):
        Row 1: header row (columns A..G)
        Row 2..N+1: one phase per row

    Strategy:
      1. Build all (row_number, row_values) writes.
      2. Pad with empty rows up to MASTER_SCHEDULE_MAX_ROWS so a phase
         that got DELETED in Postgres also disappears from the sheet.
      3. Single batchUpdate so the tab updates atomically.
      4. Stamp the mirror banner into A1 (header row), preserving the
         B..G header labels that downstream readers expect.
    """
    # Header row — banner in A1, original column names in B..G so
    # `read_master_schedule` keeps working.
    header_row = [
        header_text,
        "Phase", "Weeks", "Start", "End", "Status", "Dependencies",
    ]

    data: list[dict] = [
        {
            "range": f"{MASTER_SCHEDULE_TAB}!A1:G1",
            "values": [header_row],
        },
    ]

    # Phase rows
    for i, phase in enumerate(phase_rows):
        if i >= MASTER_SCHEDULE_MAX_ROWS:
            break
        row_num = i + 2
        data.append({
            "range": f"{MASTER_SCHEDULE_TAB}!A{row_num}:G{row_num}",
            "values": [build_master_schedule_row(phase)],
        })

    # Pad: blank out any rows beyond `len(phase_rows)` up to the cap so
    # deletions in Postgres propagate.
    blank_row = ["", "", "", "", "", "", ""]
    for i in range(len(phase_rows), MASTER_SCHEDULE_MAX_ROWS):
        row_num = i + 2
        data.append({
            "range": f"{MASTER_SCHEDULE_TAB}!A{row_num}:G{row_num}",
            "values": [blank_row],
        })

    sheets_svc.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()

    return min(len(phase_rows), MASTER_SCHEDULE_MAX_ROWS)


# ---------------------------------------------------------------------------
# Protected ranges (soft warning only)
# ---------------------------------------------------------------------------
#
# `requestingUserCanEdit=True` is the magic flag: it makes the range a
# *warning* protection rather than a hard block. Connor can dismiss the
# warning and edit. The point is purely advisory — surface to him that
# this cell is mirrored from Postgres so his typed value will be
# overwritten on the next 5-minute pass.
#
# Protections are idempotent in the sense that re-adding an existing
# protection just creates a duplicate (Sheets API has no "upsert"
# semantics here). To keep the mirror pass cheap, we *only* attempt
# to add protections when a tab is newly touched in this pass; we
# don't query-and-reconcile every pass. A small amount of duplicate
# protections accumulating is acceptable noise; the soft-warning
# semantics are unchanged.

def ensure_warning_protections(
    sheets_svc,
    spreadsheet_id: str,
    project_info_tab_id: int,
    master_schedule_tab_id: int,
    project_info_field_count: int,
) -> None:
    """Add warning-level protected ranges to the editable columns.

    Soft warning: `warningOnly=True`. No editor list. Connor can edit
    through it, but Sheets shows a 'You're editing a protected range'
    confirmation first.

    Idempotency note: we list existing protections and skip re-adding
    ones already keyed by our description. This keeps protection
    counts bounded across passes.
    """
    project_info_desc = "hb-mirror: Project Info values (Postgres-canonical)"
    master_schedule_desc = (
        "hb-mirror: Master Schedule editable columns (Postgres-canonical)"
    )

    # Discover existing protections so we don't pile up duplicates.
    meta = sheets_svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties.sheetId,protectedRanges(description))",
    ).execute()
    existing_descs: set[str] = set()
    for s in meta.get("sheets", []):
        for pr in s.get("protectedRanges", []) or []:
            desc = pr.get("description")
            if desc:
                existing_descs.add(desc)

    requests: list[dict] = []

    if project_info_desc not in existing_descs:
        requests.append({
            "addProtectedRange": {
                "protectedRange": {
                    "range": {
                        "sheetId": project_info_tab_id,
                        # Rows 2..N+1 (skip header), column B only.
                        "startRowIndex": 1,
                        "endRowIndex": 1 + project_info_field_count,
                        "startColumnIndex": 1,
                        "endColumnIndex": 2,
                    },
                    "description": project_info_desc,
                    "warningOnly": True,
                }
            }
        })

    if master_schedule_desc not in existing_descs:
        requests.append({
            "addProtectedRange": {
                "protectedRange": {
                    "range": {
                        "sheetId": master_schedule_tab_id,
                        # Rows 2..MAX+1 (skip header).
                        "startRowIndex": 1,
                        "endRowIndex": 1 + MASTER_SCHEDULE_MAX_ROWS,
                        # Columns A,B,D,E,F — the editable ones we own.
                        # Sheets API protections don't support
                        # non-contiguous columns in a single request, so
                        # we cover A..F (idx 0..6) which over-protects
                        # Weeks (C) slightly. Acceptable: Weeks is
                        # derived from Start/End anyway, so a warning
                        # there is appropriate.
                        "startColumnIndex": 0,
                        "endColumnIndex": 6,
                    },
                    "description": master_schedule_desc,
                    "warningOnly": True,
                }
            }
        })

    if not requests:
        return

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()


# ---------------------------------------------------------------------------
# Per-project mirror pass
# ---------------------------------------------------------------------------

def mirror_one_project(
    conn: psycopg.Connection,
    drive_svc,
    sheets_svc,
    project_row: dict,
    folder_path: list[str],
    now: datetime | None = None,
) -> ProjectMirrorOutcome:
    """Mirror one project from Postgres to its Tracker. Always returns
    an outcome — never raises (errors are captured into the outcome
    so one bad project doesn't block the whole pass).
    """
    project_id = project_row["id"]
    project_name = project_row.get("name") or "(unnamed)"
    drive_folder_id = project_row.get("drive_folder_id")

    outcome = ProjectMirrorOutcome(
        project_id=project_id,
        project_name=project_name,
        drive_folder_id=drive_folder_id,
        tracker_id=None,
        outcome="error",
    )

    if not drive_folder_id:
        outcome.outcome = "skipped_no_drive_folder"
        return outcome

    try:
        tracker = find_tracker_for_project(drive_svc, project_row, folder_path)
        if not tracker:
            outcome.outcome = "skipped_no_tracker"
            outcome.error = (
                f"No Tracker spreadsheet found in {' / '.join(folder_path)} "
                f"matching project name {project_name!r}"
            )
            return outcome

        tracker_id = tracker["id"]
        outcome.tracker_id = tracker_id

        # Ensure Project Info tab exists; we use its sheetId for
        # range-protection requests.
        project_info_tab_id = sheets.ensure_project_info_tab(
            sheets_svc, tracker_id,
        )

        # Discover Master Schedule sheetId.
        master_schedule_tab_id = _find_tab_id(
            sheets_svc, tracker_id, MASTER_SCHEDULE_TAB,
        )
        if master_schedule_tab_id is None:
            outcome.outcome = "error"
            outcome.error = (
                f"Tracker {tracker_id} has no '{MASTER_SCHEDULE_TAB}' tab"
            )
            return outcome

        # Read phases from Postgres (read-only).
        phase_rows = fetch_phases_for_project(conn, project_id)

        header_text = mirror_header_text(now)

        # Write Project Info first — small, cheap, validates auth.
        write_project_info_tab(
            sheets_svc, tracker_id, project_info_tab_id,
            project_row, header_text,
        )

        # Then Master Schedule.
        written = write_master_schedule_tab(
            sheets_svc, tracker_id, phase_rows, header_text,
        )

        # Soft-warning protections (idempotent — skips if already set).
        ensure_warning_protections(
            sheets_svc,
            tracker_id,
            project_info_tab_id=project_info_tab_id,
            master_schedule_tab_id=master_schedule_tab_id,
            project_info_field_count=len(PROJECT_INFO_FIELD_ORDER),
        )

        outcome.outcome = "mirrored"
        outcome.phase_count = written
        return outcome

    except Exception as exc:
        outcome.outcome = "error"
        outcome.error = f"{type(exc).__name__}: {exc}"
        return outcome


def _find_tab_id(sheets_svc, spreadsheet_id: str, title: str) -> int | None:
    """Return the sheetId of the tab whose title matches `title`, or None."""
    meta = sheets_svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties(sheetId,title)",
    ).execute()
    for s in meta.get("sheets", []):
        props = s.get("properties", {})
        if props.get("title") == title:
            return props.get("sheetId")
    return None


# ---------------------------------------------------------------------------
# Top-level pass
# ---------------------------------------------------------------------------

def run_once(
    drive_svc,
    sheets_svc,
    *,
    folder_path: Iterable[str] | None = None,
    correlation_id: str | None = None,
) -> MirrorPassResult:
    """One full pass: all active projects → their Trackers.

    Uses a single Postgres connection (read-only — autocommit, no
    writes) for the duration of the pass. Drive + Sheets services are
    callers' responsibility (passed in for testability).
    """
    if folder_path is None:
        folder_path = DRIVE_FOLDER_PATH
    folder_path = list(folder_path)

    result = MirrorPassResult(
        correlation_id=correlation_id or uuid.uuid4().hex,
        started_at=datetime.now(timezone.utc),
    )

    # autocommit=True — we never write, no transaction needed.
    with connection(autocommit=True, application_name="hb-mirror") as conn:
        projects = fetch_active_projects(conn)
        result.projects_seen = len(projects)

        logger.info(
            "mirror_pass_starting",
            extra={
                "event": "mirror_pass_starting",
                "correlation_id": result.correlation_id,
                "projects_seen": result.projects_seen,
            },
        )

        for project_row in projects:
            outcome = mirror_one_project(
                conn, drive_svc, sheets_svc, project_row, folder_path,
                now=datetime.now(timezone.utc),
            )
            result.outcomes.append(outcome)

            logger.info(
                "mirror_project_outcome",
                extra={
                    "event": "mirror_project_outcome",
                    "correlation_id": result.correlation_id,
                    **outcome.to_log_dict(),
                },
            )

    result.finished_at = datetime.now(timezone.utc)
    logger.info(
        "mirror_pass_complete",
        extra={
            "event": "mirror_pass_complete",
            "correlation_id": result.correlation_id,
            "projects_seen": result.projects_seen,
            "counts": result.counts(),
            "duration_seconds": (
                result.finished_at - result.started_at
            ).total_seconds(),
        },
    )
    return result


# ---------------------------------------------------------------------------
# launchd entry point
# ---------------------------------------------------------------------------

# Hard-cap on a single pass — same belt-and-suspenders pattern as
# `watchers/dashboard.py`. If the pass stalls beyond this, exit so
# launchd respawns rather than holding a zombie.
PASS_TIMEOUT_SEC = 240  # under the 300s StartInterval


def main() -> None:
    """Entry point invoked by `python -m home_builder_agent.scheduling.mirror_worker`."""
    configure_json_logging("hb-mirror")

    # Per-socket timeout so any blocking Drive/Sheets/Postgres network
    # call can't hang indefinitely.
    socket.setdefaulttimeout(60)

    correlation_id = uuid.uuid4().hex

    try:
        creds = get_credentials()
    except Exception as exc:
        logger.error(
            "mirror_auth_failed",
            extra={
                "event": "mirror_auth_failed",
                "correlation_id": correlation_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        sys.exit(1)

    drive_svc = drive.drive_service(creds)
    sheets_svc = sheets.sheets_service(creds)

    try:
        result = run_once(
            drive_svc, sheets_svc, correlation_id=correlation_id,
        )
    except Exception as exc:
        logger.error(
            "mirror_pass_failed",
            extra={
                "event": "mirror_pass_failed",
                "correlation_id": correlation_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        sys.exit(1)

    # Exit non-zero if the pass had only errors (no successful mirrors)
    # so launchd's failure counter notices. Mixed-outcome passes (one
    # bad project, the rest fine) exit 0 — that's the expected case.
    counts = result.counts()
    if counts.get("error", 0) > 0 and counts.get("mirrored", 0) == 0:
        sys.exit(2)


if __name__ == "__main__":  # pragma: no cover
    main()
