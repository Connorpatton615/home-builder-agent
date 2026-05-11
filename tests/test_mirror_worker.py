"""Tests for home_builder_agent.scheduling.mirror_worker.

Covers the pure-function builders (status map, row composition,
header text) and the per-project orchestration loop with mocked
Postgres, Drive, and Sheets clients. No live API calls.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from home_builder_agent.scheduling import mirror_worker
from home_builder_agent.scheduling.bridge import DRIVE_STATUS_TO_DB


# ---------------------------------------------------------------------------
# Status map (DB → Drive) — pure function
# ---------------------------------------------------------------------------

class TestDbStatusToDrive:
    def test_every_enum_value_maps_to_a_label(self):
        """Every Postgres enum value used by `home_builder.phase.status`
        must round-trip back to a non-empty Drive label."""
        for db_status in (
            "complete", "in-progress", "not-started", "blocked-on-checklist",
        ):
            label = mirror_worker.db_status_to_drive(db_status)
            assert label, f"DB status {db_status!r} maps to empty Drive label"

    def test_unknown_status_renders_empty(self):
        assert mirror_worker.db_status_to_drive("totally-bogus") == ""

    def test_null_renders_empty(self):
        assert mirror_worker.db_status_to_drive(None) == ""
        assert mirror_worker.db_status_to_drive("") == ""

    def test_db_to_drive_round_trip_through_drive_to_db(self):
        """Every label we emit must be one DRIVE_STATUS_TO_DB recognizes
        (case-insensitive, since that's how bridge normalizes)."""
        for db_status, drive_label in mirror_worker.DB_STATUS_TO_DRIVE.items():
            normalized = drive_label.strip().lower()
            assert normalized in DRIVE_STATUS_TO_DB, (
                f"Drive label {drive_label!r} (for DB {db_status!r}) is not "
                f"recognized by DRIVE_STATUS_TO_DB — the round trip is broken"
            )
            # And the round trip must land back where it started.
            assert DRIVE_STATUS_TO_DB[normalized] == db_status, (
                f"Round trip {db_status} -> {drive_label} -> "
                f"{DRIVE_STATUS_TO_DB[normalized]} did not preserve enum"
            )


# ---------------------------------------------------------------------------
# Project Info row builder — pure function
# ---------------------------------------------------------------------------

class TestBuildProjectInfoValues:
    def test_full_row_renders_in_canonical_order(self):
        row = {
            "customer_name": "Jane Whitfield",
            "customer_email": "jane@example.com",
            "customer_phone": "+12515551234",
            "address": "100 Whitfield Way",
            "job_code": "WHIT-2026",
            "notes": "Family of five, expecting twins in Q3.",
        }

        values = mirror_worker.build_project_info_values(row)

        # 7 fields total — matches sheets.PROJECT_INFO_FIELDS length.
        assert len(values) == 7
        # Each row is a single-cell list (column B).
        for v in values:
            assert isinstance(v, list)
            assert len(v) == 1

        # Order check — matches PROJECT_INFO_FIELD_ORDER.
        assert values[0] == ["Jane Whitfield"]
        assert values[1] == ["jane@example.com"]
        assert values[2] == ["+12515551234"]
        assert values[3] == ["100 Whitfield Way"]
        assert values[4] == ["WHIT-2026"]
        # Builder is the hardcoded constant — Palmetto for v1.
        assert values[5] == [mirror_worker.PROJECT_INFO_BUILDER_CONSTANT]
        assert values[6] == ["Family of five, expecting twins in Q3."]

    def test_null_fields_render_as_empty_strings_not_none(self):
        """A NULL customer_email must serialize as '' so Sheets doesn't
        write the literal string 'None' into the cell."""
        row = {
            "customer_name": "Test",
            "customer_email": None,
            "customer_phone": None,
            "address": None,
            "job_code": None,
            "notes": None,
        }
        values = mirror_worker.build_project_info_values(row)
        # All non-Builder rows that came in as None render as "".
        assert values[0] == ["Test"]
        for i in range(1, 5):
            assert values[i] == [""]
        # Builder is still the constant even when row is mostly NULL.
        assert values[5] == [mirror_worker.PROJECT_INFO_BUILDER_CONSTANT]
        # Notes is None too.
        assert values[6] == [""]


# ---------------------------------------------------------------------------
# Master Schedule row builder — pure function
# ---------------------------------------------------------------------------

class TestBuildMasterScheduleRow:
    def test_full_phase_renders_seven_columns(self):
        phase = {
            "sequence_index": 7,
            "name": "Framing",
            "status": "in-progress",
            "planned_start_date": date(2026, 6, 1),
            "planned_end_date": date(2026, 6, 28),
        }

        row = mirror_worker.build_master_schedule_row(phase)

        # Sheets contract is 7 columns: #, Phase, Weeks, Start, End, Status, Dependencies
        assert len(row) == 7
        assert row[0] == "7"
        assert row[1] == "Framing"
        assert row[2] == ""  # Weeks — not Postgres-owned
        assert row[3] == "2026-06-01"
        assert row[4] == "2026-06-28"
        assert row[5] == "In Progress"
        assert row[6] == ""  # Dependencies — not Postgres-owned

    def test_null_dates_render_empty(self):
        phase = {
            "sequence_index": 1,
            "name": "Pre-Construction",
            "status": "not-started",
            "planned_start_date": None,
            "planned_end_date": None,
        }
        row = mirror_worker.build_master_schedule_row(phase)
        assert row[3] == ""
        assert row[4] == ""
        assert row[5] == "Not Started"

    def test_datetime_input_is_truncated_to_iso_date(self):
        """psycopg can hand back a datetime for a DATE column in some
        configurations. The builder must coerce to ISO date string."""
        phase = {
            "sequence_index": 3,
            "name": "Foundation",
            "status": "complete",
            "planned_start_date": datetime(2026, 4, 1, 9, 30),
            "planned_end_date": datetime(2026, 4, 15, 17, 0),
        }
        row = mirror_worker.build_master_schedule_row(phase)
        assert row[3] == "2026-04-01"
        assert row[4] == "2026-04-15"
        assert row[5] == "Done"


# ---------------------------------------------------------------------------
# Header banner — pure function
# ---------------------------------------------------------------------------

class TestMirrorHeaderText:
    def test_uses_provided_timestamp(self):
        ts = datetime(2026, 5, 11, 14, 30, 0, tzinfo=timezone.utc)
        header = mirror_worker.mirror_header_text(ts)
        assert "Read-only mirror" in header
        assert "edit via hb-chad" in header
        assert "2026-05-11T14:30:00+00:00" in header

    def test_defaults_to_now(self):
        before = datetime.now(timezone.utc)
        header = mirror_worker.mirror_header_text()
        after = datetime.now(timezone.utc)
        # Header text contains some ISO timestamp between before/after.
        assert "Read-only mirror" in header
        # Pull the timestamp suffix and assert it's in the window.
        suffix = header.split("Last sync: ", 1)[1]
        parsed = datetime.fromisoformat(suffix)
        assert before.replace(microsecond=0) <= parsed <= after.replace(microsecond=0) + (after - before)


# ---------------------------------------------------------------------------
# mirror_one_project — orchestration, mocked clients
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_conn():
    """A psycopg.Connection stand-in that returns no phases by default.
    Tests that need phases override the cursor's fetchall."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda self: cur
    cur.__exit__ = lambda *a: None
    cur.fetchall.return_value = []
    conn.cursor.return_value = cur
    return conn


@pytest.fixture
def folder_path():
    return ["Drive", "Root", "GENERATED TIMELINES"]


class TestMirrorOneProject:
    def test_skips_project_with_null_drive_folder_id(self, mock_conn, folder_path):
        """A project whose drive_folder_id is NULL has no Tracker to
        mirror to. The worker must skip with the documented outcome
        and NOT call Drive or Sheets at all."""
        drive_svc = MagicMock()
        sheets_svc = MagicMock()

        project = {
            "id": "00000000-0000-0000-0000-000000000001",
            "name": "Orphan Project",
            "customer_name": "TBD",
            "customer_email": None,
            "customer_phone": None,
            "address": None,
            "job_code": None,
            "notes": None,
            "drive_folder_id": None,           # <-- the trigger
            "drive_folder_path": None,
        }

        outcome = mirror_worker.mirror_one_project(
            mock_conn, drive_svc, sheets_svc, project, folder_path,
        )

        assert outcome.outcome == "skipped_no_drive_folder"
        # Critically, no Drive or Sheets calls fired.
        assert not drive_svc.method_calls
        assert not sheets_svc.method_calls

    def test_tracker_not_found_logs_and_continues(
        self, monkeypatch, mock_conn, folder_path,
    ):
        """If Drive has no Tracker matching the project name, we record
        a 'skipped_no_tracker' outcome and continue the pass — we do
        NOT raise (per the ADR scope-out, the worker is best-effort and
        per-project failures are isolated)."""
        drive_svc = MagicMock()
        sheets_svc = MagicMock()

        # Patch find_tracker_by_project to return None.
        monkeypatch.setattr(
            "home_builder_agent.scheduling.mirror_worker.drive.find_tracker_by_project",
            lambda *a, **kw: None,
        )

        project = {
            "id": "00000000-0000-0000-0000-000000000002",
            "name": "Missing Tracker Project",
            "customer_name": "Some Customer",
            "customer_email": None,
            "customer_phone": None,
            "address": None,
            "job_code": None,
            "notes": None,
            "drive_folder_id": "drive-folder-abc",
            "drive_folder_path": "GENERATED TIMELINES / Missing Tracker Project",
        }

        outcome = mirror_worker.mirror_one_project(
            mock_conn, drive_svc, sheets_svc, project, folder_path,
        )

        assert outcome.outcome == "skipped_no_tracker"
        assert outcome.error is not None
        assert "No Tracker spreadsheet found" in outcome.error
        # No Sheets writes happened (the function should bail before
        # ensure_project_info_tab).
        assert not sheets_svc.method_calls

    def test_happy_path_writes_both_tabs_and_records_outcome(
        self, monkeypatch, folder_path,
    ):
        """Full pass: tracker found, both tabs written, protections
        attempted, outcome marked 'mirrored' with phase_count set."""
        drive_svc = MagicMock()
        sheets_svc = MagicMock()

        # Tracker discovery returns a fake spreadsheet.
        monkeypatch.setattr(
            "home_builder_agent.scheduling.mirror_worker.drive.find_tracker_by_project",
            lambda *a, **kw: {
                "id": "sheet-xyz",
                "name": "Tracker – Whitfield Residence",
                "modifiedTime": "2026-05-11T12:00:00Z",
            },
        )
        # ensure_project_info_tab returns a tab id.
        monkeypatch.setattr(
            "home_builder_agent.scheduling.mirror_worker.sheets.ensure_project_info_tab",
            lambda *a, **kw: 111,
        )
        # _find_tab_id returns the Master Schedule tab id.
        monkeypatch.setattr(
            "home_builder_agent.scheduling.mirror_worker._find_tab_id",
            lambda svc, sid, title: 222 if title == "Master Schedule" else None,
        )
        # Stub the actual writers so we don't have to mock the full
        # Sheets request shape; the writers themselves are tested
        # via the pure builders above.
        wrote_project_info = MagicMock()
        wrote_master_schedule = MagicMock(return_value=3)
        wrote_protections = MagicMock()
        monkeypatch.setattr(
            "home_builder_agent.scheduling.mirror_worker.write_project_info_tab",
            wrote_project_info,
        )
        monkeypatch.setattr(
            "home_builder_agent.scheduling.mirror_worker.write_master_schedule_tab",
            wrote_master_schedule,
        )
        monkeypatch.setattr(
            "home_builder_agent.scheduling.mirror_worker.ensure_warning_protections",
            wrote_protections,
        )

        # Three phases returned by Postgres.
        fake_phases = [
            {
                "id": "p1", "sequence_index": 1, "name": "Pre-Construction",
                "status": "complete",
                "planned_start_date": date(2026, 4, 1),
                "planned_end_date": date(2026, 4, 15),
            },
            {
                "id": "p2", "sequence_index": 2, "name": "Site Prep",
                "status": "in-progress",
                "planned_start_date": date(2026, 4, 16),
                "planned_end_date": date(2026, 4, 30),
            },
            {
                "id": "p3", "sequence_index": 3, "name": "Foundation",
                "status": "not-started",
                "planned_start_date": date(2026, 5, 1),
                "planned_end_date": date(2026, 5, 15),
            },
        ]
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        cur.fetchall.return_value = fake_phases
        conn.cursor.return_value = cur

        project = {
            "id": "00000000-0000-0000-0000-000000000003",
            "name": "Whitfield Residence",
            "customer_name": "Whitfield",
            "customer_email": "w@example.com",
            "customer_phone": None,
            "address": "100 Whitfield Way",
            "job_code": "WHIT-2026",
            "notes": None,
            "drive_folder_id": "drive-folder-w",
            "drive_folder_path": "GENERATED TIMELINES / Whitfield Residence",
        }

        outcome = mirror_worker.mirror_one_project(
            conn, drive_svc, sheets_svc, project, folder_path,
        )

        assert outcome.outcome == "mirrored"
        assert outcome.phase_count == 3
        assert outcome.tracker_id == "sheet-xyz"

        # Sanity: each writer fired exactly once with the project /
        # phases we set up.
        wrote_project_info.assert_called_once()
        wrote_master_schedule.assert_called_once()
        wrote_protections.assert_called_once()

    def test_exception_during_mirror_is_captured_not_raised(
        self, monkeypatch, mock_conn, folder_path,
    ):
        """A blow-up inside the Sheets writer must be captured in the
        outcome so the pass continues to the next project."""
        drive_svc = MagicMock()
        sheets_svc = MagicMock()

        monkeypatch.setattr(
            "home_builder_agent.scheduling.mirror_worker.drive.find_tracker_by_project",
            lambda *a, **kw: {"id": "sheet-1", "name": "Tracker – X"},
        )
        monkeypatch.setattr(
            "home_builder_agent.scheduling.mirror_worker.sheets.ensure_project_info_tab",
            lambda *a, **kw: 111,
        )
        monkeypatch.setattr(
            "home_builder_agent.scheduling.mirror_worker._find_tab_id",
            lambda *a, **kw: 222,
        )

        def boom(*a, **kw):
            raise RuntimeError("Sheets API rate-limited")

        monkeypatch.setattr(
            "home_builder_agent.scheduling.mirror_worker.write_project_info_tab",
            boom,
        )

        project = {
            "id": "00000000-0000-0000-0000-000000000004",
            "name": "Boom Project",
            "customer_name": "X",
            "customer_email": None,
            "customer_phone": None,
            "address": None,
            "job_code": None,
            "notes": None,
            "drive_folder_id": "drive-folder-boom",
            "drive_folder_path": None,
        }

        outcome = mirror_worker.mirror_one_project(
            mock_conn, drive_svc, sheets_svc, project, folder_path,
        )

        assert outcome.outcome == "error"
        assert "RuntimeError" in outcome.error
        assert "rate-limited" in outcome.error


# ---------------------------------------------------------------------------
# Postgres adapter — verifies the query shape filters to active +
# orders deterministically. (No live DB — just checks the SQL we pass.)
# ---------------------------------------------------------------------------

class TestFetchActiveProjects:
    def test_query_filters_to_active_status(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur

        mirror_worker.fetch_active_projects(conn)

        executed_sql = cur.execute.call_args[0][0]
        # Filter must restrict to active projects.
        assert "status = 'active'" in executed_sql
        # Must select drive_folder_id (so the worker can decide skip vs mirror).
        assert "drive_folder_id" in executed_sql
        # Must include the new migration-011 columns.
        assert "customer_email" in executed_sql
        assert "customer_phone" in executed_sql
        assert "job_code" in executed_sql
        assert "notes" in executed_sql
