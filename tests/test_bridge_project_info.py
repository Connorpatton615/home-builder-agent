"""Tests for bridge.py Project Info tab sync (v1.2 of ADR 2026-05-11).

The bridge's job is to read Chad's Tracker and propagate edits into
home_builder.project. v1.0 wired Master Schedule -> phase rows. v1.2
adds the "Project Info" tab -> project columns (customer_name,
customer_email, customer_phone, address, job_code, notes).

The hard rule: a blank Tracker cell never overwrites a non-empty
Postgres value. Postgres real data wins over Tracker blanks. The only
exception is the legacy customer_name = "TBD" placeholder that the
bridge stamps onto new project rows — that gets overwritten by any
non-empty Tracker value.

We mock the Sheets read + the Postgres cursor — no live API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — mock the postgres.connection() context manager (matches the
# pattern used in test_chad_input_tools.py)
# ---------------------------------------------------------------------------


def _mock_connection_with_cursor(cursor: MagicMock) -> MagicMock:
    """Build a MagicMock that behaves like `connection()`'s context
    manager: ``with connection(...) as conn: with conn.cursor() as cur:``.

    Same shape as test_chad_input_tools._mock_connection_with_cursor —
    just kept inline here so the test module stands on its own.
    """
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cursor_cm)
    return conn


def _make_cursor(fetchone_returns: list) -> MagicMock:
    """Build a cursor whose fetchone() returns each item from `fetchone_returns`
    in sequence. After the list is exhausted, returns None.

    Useful because `_sync_project_info` issues one SELECT (returning the
    current row), and `sync_tracker` issues prior SELECTs we have to feed
    too.
    """
    cursor = MagicMock()
    cursor.fetchone = MagicMock(side_effect=list(fetchone_returns) + [None] * 10)
    return cursor


# ---------------------------------------------------------------------------
# _sync_project_info unit tests — exercise the helper directly with a
# mock conn. These cover the per-field decision logic that's the load-
# bearing piece of v1.2.
# ---------------------------------------------------------------------------


class TestSyncProjectInfoFieldDecisions:
    """Per-field 'do I write or not?' logic for the Postgres-wins rule."""

    def test_all_tracker_fields_blank_unchanged_no_write(self):
        """Tracker has the tab but every value is blank. Nothing to do."""
        from home_builder_agent.scheduling.bridge import _sync_project_info

        cursor = _make_cursor([
            # Current Postgres row — every column already null/empty
            {
                "customer_name": None,
                "customer_email": None,
                "customer_phone": None,
                "address": None,
                "job_code": None,
                "notes": None,
            },
        ])
        conn = _mock_connection_with_cursor(cursor)

        tracker_info = {
            "Customer Name": "",
            "Customer Email": "",
            "Customer Phone": "",
            "Project Address": "",
            "Job Code": "",
            "Builder": "Palmetto Custom Homes",
            "Notes": "",
        }

        outcome, field_outcomes = _sync_project_info(
            conn, project_id="abc-123", tracker_info=tracker_info,
        )

        assert outcome == "unchanged"
        # All blank-on-both-sides => 'unchanged' per field
        assert field_outcomes == {
            "customer_name": "unchanged",
            "customer_email": "unchanged",
            "customer_phone": "unchanged",
            "address": "unchanged",
            "job_code": "unchanged",
            "notes": "unchanged",
        }
        # Exactly one cursor.execute call — the SELECT. No UPDATE.
        assert cursor.execute.call_count == 1
        first_sql, _ = cursor.execute.call_args_list[0][0]
        assert first_sql.strip().upper().startswith("SELECT")

    def test_one_field_changed_only_that_column_updated(self):
        """Tracker has one new value (email). Only that column gets UPDATEd."""
        from home_builder_agent.scheduling.bridge import _sync_project_info

        cursor = _make_cursor([
            {
                "customer_name": "Whitfield Family",
                "customer_email": None,            # the field that'll change
                "customer_phone": "(251) 555-0142",
                "address": "123 Pelican Way",
                "job_code": "WHIT-2026",
                "notes": "VIP",
            },
        ])
        conn = _mock_connection_with_cursor(cursor)

        tracker_info = {
            "Customer Name": "Whitfield Family",
            "Customer Email": "whitfield@example.com",
            "Customer Phone": "(251) 555-0142",
            "Project Address": "123 Pelican Way",
            "Job Code": "WHIT-2026",
            "Builder": "Palmetto Custom Homes",
            "Notes": "VIP",
        }

        outcome, field_outcomes = _sync_project_info(
            conn, project_id="abc-123", tracker_info=tracker_info,
        )

        assert outcome == "updated"
        assert field_outcomes["customer_email"] == "updated"
        for unchanged_col in (
            "customer_name", "customer_phone", "address", "job_code", "notes",
        ):
            assert field_outcomes[unchanged_col] == "unchanged"

        # Two execute calls: SELECT + UPDATE
        assert cursor.execute.call_count == 2
        update_sql, update_params = cursor.execute.call_args_list[1][0]
        assert update_sql.upper().startswith("UPDATE HOME_BUILDER.PROJECT")
        assert "customer_email = %s" in update_sql
        # No other columns in the SET clause
        for other in (
            "customer_name = %s",
            "customer_phone = %s",
            "address = %s",
            "job_code = %s",
            "notes = %s",
        ):
            assert other not in update_sql
        # Params: [new_email, project_id]
        assert update_params[0] == "whitfield@example.com"
        assert update_params[-1] == "abc-123"

    def test_tbd_customer_name_overwritten_by_tracker(self):
        """Bridge legacy default — 'TBD' is treated as empty and gets
        overwritten by any non-empty Tracker value."""
        from home_builder_agent.scheduling.bridge import _sync_project_info

        cursor = _make_cursor([
            {
                "customer_name": "TBD",           # legacy default
                "customer_email": None,
                "customer_phone": None,
                "address": None,
                "job_code": None,
                "notes": None,
            },
        ])
        conn = _mock_connection_with_cursor(cursor)

        tracker_info = {
            "Customer Name": "Whitfield Family",
            "Customer Email": "",
            "Customer Phone": "",
            "Project Address": "",
            "Job Code": "",
            "Builder": "Palmetto Custom Homes",
            "Notes": "",
        }

        outcome, field_outcomes = _sync_project_info(
            conn, project_id="abc-123", tracker_info=tracker_info,
        )

        assert outcome == "updated"
        assert field_outcomes["customer_name"] == "updated"
        # All other Tracker fields blank, all Postgres blank => unchanged
        assert field_outcomes["customer_email"] == "unchanged"

        # UPDATE only writes customer_name
        update_sql, update_params = cursor.execute.call_args_list[1][0]
        assert "customer_name = %s" in update_sql
        assert "customer_email = %s" not in update_sql
        assert update_params[0] == "Whitfield Family"
        assert update_params[-1] == "abc-123"

    def test_postgres_real_value_preserved_against_blank_tracker(self):
        """The HARD CONSTRAINT: tracker blank + postgres has real data =>
        Postgres preserved. This is the safety rule from the cutover."""
        from home_builder_agent.scheduling.bridge import _sync_project_info

        cursor = _make_cursor([
            {
                # Postgres has real data on every column
                "customer_name": "Whitfield Family",
                "customer_email": "whitfield@example.com",
                "customer_phone": "(251) 555-0142",
                "address": "123 Pelican Way",
                "job_code": "WHIT-2026",
                "notes": "VIP — owner architect",
            },
        ])
        conn = _mock_connection_with_cursor(cursor)

        # Tracker has BLANK in every field (e.g. Chad just created the
        # tab and hasn't filled in column B yet)
        tracker_info = {
            "Customer Name": "",
            "Customer Email": "",
            "Customer Phone": "",
            "Project Address": "",
            "Job Code": "",
            "Builder": "Palmetto Custom Homes",
            "Notes": "",
        }

        outcome, field_outcomes = _sync_project_info(
            conn, project_id="abc-123", tracker_info=tracker_info,
        )

        assert outcome == "unchanged"
        # Every column is 'kept' — Postgres real value wins
        assert field_outcomes == {
            "customer_name": "kept",
            "customer_email": "kept",
            "customer_phone": "kept",
            "address": "kept",
            "job_code": "kept",
            "notes": "kept",
        }
        # NO UPDATE issued — only the initial SELECT.
        assert cursor.execute.call_count == 1
        assert cursor.execute.call_args_list[0][0][0].strip().upper().startswith(
            "SELECT"
        )

    def test_whitespace_only_tracker_value_counts_as_blank(self):
        """A Tracker cell containing only whitespace should be treated
        the same as empty — preserves Postgres real data."""
        from home_builder_agent.scheduling.bridge import _sync_project_info

        cursor = _make_cursor([
            {
                "customer_name": "Whitfield Family",
                "customer_email": "real@example.com",
                "customer_phone": None,
                "address": None,
                "job_code": None,
                "notes": None,
            },
        ])
        conn = _mock_connection_with_cursor(cursor)

        tracker_info = {
            "Customer Name": "   ",       # whitespace only
            "Customer Email": "\t\n",     # whitespace only
            "Customer Phone": "",
            "Project Address": "",
            "Job Code": "",
            "Builder": "Palmetto Custom Homes",
            "Notes": "",
        }

        outcome, field_outcomes = _sync_project_info(
            conn, project_id="abc-123", tracker_info=tracker_info,
        )

        # customer_name and customer_email are 'kept' (Postgres wins
        # against tracker whitespace). Others are 'unchanged'.
        assert field_outcomes["customer_name"] == "kept"
        assert field_outcomes["customer_email"] == "kept"
        assert outcome == "unchanged"
        assert cursor.execute.call_count == 1  # no UPDATE


# ---------------------------------------------------------------------------
# sync_tracker integration tests — verify the full path including:
#   - tab missing returns 'tab_missing' cleanly
#   - the rest of the sync still commits
# We mock sheets.read_project_info + sheets.read_master_schedule + the
# postgres connection() so no live calls happen.
# ---------------------------------------------------------------------------


def _make_drive_svc(folder_id: str = "drive-folder-xyz"):
    """Minimal mock Drive service: files().get().execute() returns a
    parent folder id, then a folder-name lookup."""
    files_get_meta = MagicMock()
    # First call: tracker meta (returns parents)
    # Second call: parent folder meta (returns name)
    files_get_meta.execute = MagicMock(
        side_effect=[
            {"id": "sheet-1", "name": "Tracker — Whitfield",
             "parents": [folder_id], "webViewLink": "https://example/x"},
            {"id": folder_id, "name": "Whitfield Residence"},
        ]
    )

    files_obj = MagicMock()
    files_obj.get = MagicMock(return_value=files_get_meta)

    drive_svc = MagicMock()
    drive_svc.files = MagicMock(return_value=files_obj)
    return drive_svc


def _make_master_schedule_rows():
    """One-phase Master Schedule — enough to satisfy header check + non-
    empty, but minimal so phase sync isn't the focus here."""
    return [
        {"#": "1", "Phase": "Site Prep", "Weeks": "2",
         "Start": "Apr 1, 2026", "End": "Apr 14, 2026",
         "Status": "not started", "Dependencies": ""},
    ]


class TestSyncTrackerProjectInfoIntegration:
    """End-to-end paths through sync_tracker that exercise the Project
    Info tab integration."""

    def test_tab_missing_clean_skip_no_write(self):
        """sheets.read_project_info returning {} => project_info_outcome
        is 'tab_missing', no project-info write, but rest of sync still
        commits."""
        from home_builder_agent.scheduling import bridge

        # Cursor returns:
        #   1) _upsert_project's SELECT (drive_folder_id lookup) — None (no existing project)
        #      [fallback name-based SELECT also returns None]
        #   2) INSERT RETURNING id
        #   3) _upsert_phase's SELECT — None (new phase)
        #   4) DELETE-orphan-phases SELECT COUNT — 0
        # We don't need _sync_project_info path because read_project_info
        # returns {} (tab missing) — so _sync_project_info is never called.
        cursor = MagicMock()
        cursor.fetchone = MagicMock(side_effect=[
            None,                       # SELECT by drive_folder_id (no row)
            None,                       # fallback SELECT by name (no row)
            {"id": "new-proj-uuid"},    # INSERT RETURNING id
            None,                       # _upsert_phase SELECT (insert path)
            {"n": 0},                   # COUNT for orphan-delete guard
        ])
        cursor.fetchall = MagicMock(return_value=[])  # no deleted rows
        conn = _mock_connection_with_cursor(cursor)

        drive_svc = _make_drive_svc()
        sheets_svc = MagicMock()

        tracker = {"id": "sheet-1", "name": "Tracker — Whitfield"}

        with patch.object(
            bridge.sheets, "read_master_schedule",
            return_value=_make_master_schedule_rows(),
        ), patch.object(
            bridge.sheets, "read_project_info",
            return_value={},  # tab missing
        ), patch.object(
            bridge, "connection", return_value=conn,
        ):
            result = bridge.sync_tracker(
                drive_svc, sheets_svc, tracker, dry_run=False,
            )

        assert result.error is None
        assert result.project_info_outcome == "tab_missing"
        # No per-field outcomes since we never got into _sync_project_info
        assert result.project_info_field_outcomes == {}
        # Commit happened (NOT rollback) — sync still succeeded overall
        assert conn.commit.called
        assert not conn.rollback.called

    def test_tab_missing_does_not_break_phase_sync(self):
        """Even when the Project Info tab is missing, Master Schedule
        sync should still complete and the result.project_outcome should
        be 'inserted'/'updated' (not error)."""
        from home_builder_agent.scheduling import bridge

        cursor = MagicMock()
        cursor.fetchone = MagicMock(side_effect=[
            None,
            None,
            {"id": "new-proj-uuid"},
            None,
            {"n": 0},
        ])
        cursor.fetchall = MagicMock(return_value=[])
        conn = _mock_connection_with_cursor(cursor)

        drive_svc = _make_drive_svc()
        sheets_svc = MagicMock()

        tracker = {"id": "sheet-1", "name": "Tracker — Whitfield"}

        with patch.object(
            bridge.sheets, "read_master_schedule",
            return_value=_make_master_schedule_rows(),
        ), patch.object(
            bridge.sheets, "read_project_info",
            return_value={},
        ), patch.object(
            bridge, "connection", return_value=conn,
        ):
            result = bridge.sync_tracker(
                drive_svc, sheets_svc, tracker, dry_run=False,
            )

        # Phase sync still happened
        assert result.project_outcome == "inserted"
        assert result.phase_count == 1
        # Project info reported missing
        assert result.project_info_outcome == "tab_missing"

    def test_tracker_sync_result_includes_new_fields(self):
        """TrackerSyncResult has the v1.2 fields with sensible defaults
        even when sync errors out before project info runs."""
        from home_builder_agent.scheduling.bridge import TrackerSyncResult

        r = TrackerSyncResult(
            tracker_name="x", project_name="y",
            drive_folder_id=None, project_id=None,
            project_outcome="error",
        )
        # Defaults
        assert r.project_info_outcome == "unchanged"
        assert r.project_info_field_outcomes == {}


# ---------------------------------------------------------------------------
# Sanity-check the field map matches the migration 011 / ADR cutover spec.
# Builder is intentionally absent (always "Palmetto Custom Homes").
# ---------------------------------------------------------------------------


def test_project_info_field_map_matches_spec():
    from home_builder_agent.scheduling.bridge import PROJECT_INFO_FIELD_MAP

    assert PROJECT_INFO_FIELD_MAP == [
        ("Customer Name", "customer_name"),
        ("Customer Email", "customer_email"),
        ("Customer Phone", "customer_phone"),
        ("Project Address", "address"),
        ("Job Code", "job_code"),
        ("Notes", "notes"),
    ]
    # Builder NEVER appears — it's a constant, not a stored column
    assert all(t[0] != "Builder" for t in PROJECT_INFO_FIELD_MAP)
