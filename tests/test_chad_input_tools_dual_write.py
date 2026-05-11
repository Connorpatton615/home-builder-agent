"""Tests for v1.2 dual-write behavior of the hb-chad input tools.

Per ADR 2026-05-11 v1.2 ("Google Sheets canonical, Postgres query store"):
the three Tracker-canonicalization tools (update_customer_info,
update_schedule_date, reorder_phase) must dual-write — Postgres UPDATE
+ Tracker write inside one atomic boundary. The Postgres write happens
first (uncommitted); the Sheets mirror runs next; only if the mirror
succeeds does the connection() context manager COMMIT. If the mirror
raises, the Postgres UPDATE is rolled back — both stores are guaranteed
to stay in sync.

This module covers what test_chad_input_tools.py deliberately skips
(it neutralizes the mirror via an autouse fixture):

  - Unit tests for the 3 new write helpers in integrations/sheets.py
  - For each tool: SUCCESS path — Sheets API receives the right
    payload, both Postgres and Sheets fire
  - For each tool: SHEETS-FAILURE path — when a Sheets write raises
    SheetsWriteError, the Postgres connection rolls back AND the
    tool returns a Chad-voice error to the caller
  - The exact API calls + atomicity (one batch for reorder, etc.)

We mock at three boundaries:
  - postgres.connection() — same MagicMock-CM pattern as
    test_chad_input_tools.py, so we can assert on cursor.execute
  - chad_agent._open_sheets_service_for_tool — returns a (creds,
    sheets_svc, drive_svc) tuple of mocks
  - chad_agent._find_tracker_id_for_project — returns a known
    tracker_id, or raises _DualWriteSheetsFailure for the
    'no tracker' branch
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest


# ===========================================================================
# Section 1 — unit tests for integrations/sheets.py write helpers
# ===========================================================================
#
# Each helper takes a real Sheets service object in production. Here we
# inject a MagicMock and assert on the API call shape — we never hit
# real Google APIs.

class TestUpdateProjectInfoField:
    """Helper: locate row by column A == field_name, write to column B."""

    def _build_get_response(self, rows):
        """Build the dict shape that values().get().execute() returns."""
        return {"values": rows}

    def test_finds_field_and_writes_to_column_b(self):
        from home_builder_agent.integrations.sheets import (
            update_project_info_field,
        )

        sheets_svc = MagicMock()
        # Project Info tab content: header row + 7 field rows
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = self._build_get_response(
            [
                ["Field", "Value"],
                ["Customer Name", ""],
                ["Customer Email", "old@example.com"],
                ["Customer Phone", ""],
                ["Project Address", ""],
                ["Job Code", ""],
                ["Builder", "Palmetto Custom Homes"],
                ["Notes", ""],
            ]
        )

        update_project_info_field(
            sheets_svc, "sheet-id-1", "Customer Email", "new@example.com"
        )

        # Get was called for Project Info!A1:B100
        get_call = sheets_svc.spreadsheets.return_value.values.return_value.get.call_args
        assert get_call.kwargs["spreadsheetId"] == "sheet-id-1"
        assert "Project Info" in get_call.kwargs["range"]

        # Update was called for B3 (Customer Email is the 3rd row = row 3).
        update_call = sheets_svc.spreadsheets.return_value.values.return_value.update.call_args
        assert update_call.kwargs["spreadsheetId"] == "sheet-id-1"
        assert update_call.kwargs["range"] == "Project Info!B3"
        assert update_call.kwargs["valueInputOption"] == "USER_ENTERED"
        assert update_call.kwargs["body"] == {
            "values": [["new@example.com"]]
        }

    def test_idempotent_rewrite_of_same_value(self):
        """Re-writing the same value is allowed — Sheets API still fires
        but the tool doesn't error.
        """
        from home_builder_agent.integrations.sheets import (
            update_project_info_field,
        )

        sheets_svc = MagicMock()
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = self._build_get_response(
            [
                ["Field", "Value"],
                ["Customer Name", "Bradford"],
            ]
        )
        # Should not raise.
        update_project_info_field(
            sheets_svc, "sheet-id", "Customer Name", "Bradford"
        )

    def test_unknown_field_raises_sheets_write_error(self):
        from home_builder_agent.integrations.sheets import (
            update_project_info_field,
            SheetsWriteError,
        )

        sheets_svc = MagicMock()
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = self._build_get_response(
            [
                ["Field", "Value"],
                ["Customer Name", ""],
            ]
        )

        with pytest.raises(SheetsWriteError) as exc_info:
            update_project_info_field(
                sheets_svc, "sheet-id", "Job Code", "BRAD-2026"
            )
        assert "Job Code" in str(exc_info.value)
        # No update call should have fired.
        sheets_svc.spreadsheets.return_value.values.return_value.update.assert_not_called()

    def test_empty_tab_raises_sheets_write_error(self):
        from home_builder_agent.integrations.sheets import (
            update_project_info_field,
            SheetsWriteError,
        )

        sheets_svc = MagicMock()
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = self._build_get_response(
            []
        )

        with pytest.raises(SheetsWriteError) as exc_info:
            update_project_info_field(
                sheets_svc, "sheet-id", "Customer Name", "Bradford"
            )
        assert "empty" in str(exc_info.value).lower()

    def test_sheets_api_error_wrapped_as_sheets_write_error(self):
        from home_builder_agent.integrations.sheets import (
            update_project_info_field,
            SheetsWriteError,
        )

        sheets_svc = MagicMock()
        # Simulate an API failure on the read call.
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = RuntimeError(
            "rate limited"
        )

        with pytest.raises(SheetsWriteError) as exc_info:
            update_project_info_field(
                sheets_svc, "sheet-id", "Customer Name", "Bradford"
            )
        assert "rate limited" in str(exc_info.value)


class TestUpdateMasterScheduleCell:
    """Helper: locate row by column A == sequence_index, locate column
    by header text, write to that cell.
    """

    HEADERS = ["#", "Phase", "Weeks", "Start", "End", "Status", "Dependencies"]

    def _build_master_schedule(self, *rows):
        return {"values": [self.HEADERS, *rows]}

    def test_writes_start_column_at_correct_cell(self):
        from home_builder_agent.integrations.sheets import (
            update_master_schedule_cell,
        )

        sheets_svc = MagicMock()
        # 3 phase rows: #=1,2,3
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = self._build_master_schedule(
            ["1", "Excavation", "2", "Apr 28, 2026", "May 12, 2026", "Not Started", ""],
            ["2", "Foundation", "3", "May 13, 2026", "Jun 3, 2026", "Not Started", ""],
            ["3", "Framing", "4", "Jun 4, 2026", "Jul 2, 2026", "Not Started", ""],
        )

        update_master_schedule_cell(
            sheets_svc, "sheet-id", 2, "Start", "May 20, 2026"
        )

        update_call = sheets_svc.spreadsheets.return_value.values.return_value.update.call_args
        # Foundation is at sheet row 3 (header=row 1, #=1 is row 2, #=2 is row 3).
        # Start column is D (4th col, index 3 → letter D).
        assert update_call.kwargs["range"] == "Master Schedule!D3"
        assert update_call.kwargs["body"] == {"values": [["May 20, 2026"]]}
        assert update_call.kwargs["valueInputOption"] == "USER_ENTERED"

    def test_writes_end_column_at_correct_cell(self):
        from home_builder_agent.integrations.sheets import (
            update_master_schedule_cell,
        )

        sheets_svc = MagicMock()
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = self._build_master_schedule(
            ["1", "Excavation", "2", "Apr 28, 2026", "May 12, 2026", "Not Started", ""],
        )

        update_master_schedule_cell(
            sheets_svc, "sheet-id", 1, "End", "May 15, 2026"
        )

        update_call = sheets_svc.spreadsheets.return_value.values.return_value.update.call_args
        # End is column E (index 4).
        assert update_call.kwargs["range"] == "Master Schedule!E2"
        assert update_call.kwargs["body"] == {"values": [["May 15, 2026"]]}

    def test_unknown_sequence_index_raises_sheets_write_error(self):
        from home_builder_agent.integrations.sheets import (
            update_master_schedule_cell,
            SheetsWriteError,
        )

        sheets_svc = MagicMock()
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = self._build_master_schedule(
            ["1", "Excavation", "2", "Apr 28, 2026", "May 12, 2026", "Not Started", ""],
        )

        with pytest.raises(SheetsWriteError) as exc_info:
            update_master_schedule_cell(
                sheets_svc, "sheet-id", 99, "Start", "May 1, 2026"
            )
        assert "99" in str(exc_info.value)
        sheets_svc.spreadsheets.return_value.values.return_value.update.assert_not_called()

    def test_unknown_column_header_raises_sheets_write_error(self):
        from home_builder_agent.integrations.sheets import (
            update_master_schedule_cell,
            SheetsWriteError,
        )

        sheets_svc = MagicMock()
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = self._build_master_schedule(
            ["1", "Excavation", "2", "Apr 28, 2026", "May 12, 2026", "Not Started", ""],
        )

        with pytest.raises(SheetsWriteError) as exc_info:
            update_master_schedule_cell(
                sheets_svc, "sheet-id", 1, "NotAColumn", "x"
            )
        assert "NotAColumn" in str(exc_info.value)

    def test_empty_master_schedule_raises(self):
        from home_builder_agent.integrations.sheets import (
            update_master_schedule_cell,
            SheetsWriteError,
        )

        sheets_svc = MagicMock()
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": []
        }

        with pytest.raises(SheetsWriteError):
            update_master_schedule_cell(
                sheets_svc, "sheet-id", 1, "Start", "May 1, 2026"
            )


class TestUpdateMasterScheduleSequenceIndices:
    """Helper: batch-update the # column for multiple rows, matched by
    phase name in column B.
    """

    HEADERS = ["#", "Phase", "Weeks", "Start", "End", "Status", "Dependencies"]

    def _build_master_schedule(self, *rows):
        return {"values": [self.HEADERS, *rows]}

    def test_single_batch_update_for_multiple_phases(self):
        from home_builder_agent.integrations.sheets import (
            update_master_schedule_sequence_indices,
        )

        sheets_svc = MagicMock()
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = self._build_master_schedule(
            ["1", "Excavation", "2", "", "", "", ""],
            ["2", "Foundation", "3", "", "", "", ""],
            ["3", "Framing", "4", "", "", "", ""],
            ["4", "Drywall", "2", "", "", "", ""],
        )

        update_master_schedule_sequence_indices(
            sheets_svc,
            "sheet-id",
            {"Foundation": 4, "Framing": 2, "Drywall": 3},
        )

        # One batchUpdate call carrying all three writes.
        bu_call = sheets_svc.spreadsheets.return_value.values.return_value.batchUpdate.call_args
        assert bu_call.kwargs["spreadsheetId"] == "sheet-id"
        body = bu_call.kwargs["body"]
        assert body["valueInputOption"] == "USER_ENTERED"

        # Each data entry rewrites column A for the matched row.
        ranges = [d["range"] for d in body["data"]]
        values = [d["values"] for d in body["data"]]
        # Foundation is at sheet row 3 (#2), Framing at row 4 (#3),
        # Drywall at row 5 (#4).
        assert "Master Schedule!A3" in ranges  # Foundation → new # 4
        assert "Master Schedule!A4" in ranges  # Framing → new # 2
        assert "Master Schedule!A5" in ranges  # Drywall → new # 3

        # Pair up by range so we can assert on the new sequence value.
        by_range = dict(zip(ranges, values))
        assert by_range["Master Schedule!A3"] == [[4]]
        assert by_range["Master Schedule!A4"] == [[2]]
        assert by_range["Master Schedule!A5"] == [[3]]

    def test_unknown_phase_raises_before_writing(self):
        from home_builder_agent.integrations.sheets import (
            update_master_schedule_sequence_indices,
            SheetsWriteError,
        )

        sheets_svc = MagicMock()
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = self._build_master_schedule(
            ["1", "Excavation", "2", "", "", "", ""],
            ["2", "Foundation", "3", "", "", "", ""],
        )

        with pytest.raises(SheetsWriteError) as exc_info:
            update_master_schedule_sequence_indices(
                sheets_svc,
                "sheet-id",
                {"Excavation": 2, "NotAPhase": 1},
            )
        assert "NotAPhase" in str(exc_info.value)
        # CRUCIALLY: NO batchUpdate fired — atomicity preserved.
        sheets_svc.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()

    def test_empty_input_is_noop(self):
        from home_builder_agent.integrations.sheets import (
            update_master_schedule_sequence_indices,
        )

        sheets_svc = MagicMock()
        update_master_schedule_sequence_indices(sheets_svc, "sheet-id", {})
        # No API calls at all.
        sheets_svc.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()
        sheets_svc.spreadsheets.return_value.values.return_value.get.assert_not_called()

    def test_batch_api_error_wrapped_as_sheets_write_error(self):
        from home_builder_agent.integrations.sheets import (
            update_master_schedule_sequence_indices,
            SheetsWriteError,
        )

        sheets_svc = MagicMock()
        sheets_svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = self._build_master_schedule(
            ["1", "Excavation", "2", "", "", "", ""],
        )
        sheets_svc.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.side_effect = RuntimeError(
            "API blew up"
        )

        with pytest.raises(SheetsWriteError) as exc_info:
            update_master_schedule_sequence_indices(
                sheets_svc, "sheet-id", {"Excavation": 1}
            )
        assert "API blew up" in str(exc_info.value)


class TestColumnLetterHelper:
    """Internal: 0-indexed col → A1-style letter."""

    @pytest.mark.parametrize(
        "idx,expected",
        [
            (0, "A"),
            (1, "B"),
            (25, "Z"),
            (26, "AA"),
            (27, "AB"),
            (51, "AZ"),
            (52, "BA"),
        ],
    )
    def test_letter_conversion(self, idx, expected):
        from home_builder_agent.integrations.sheets import (
            _column_index_to_letter,
        )

        assert _column_index_to_letter(idx) == expected


# ===========================================================================
# Section 2 — Tool-level dual-write tests (Postgres + Sheets together)
# ===========================================================================
#
# Mock the postgres.connection() context manager AND the Sheets-side
# auth boundary (chad_agent._open_sheets_service_for_tool) +
# chad_agent._find_tracker_id_for_project. That lets us assert on
# both halves of the dual-write contract:
#
#   1. Success: the right Sheets API calls fire after the Postgres
#      UPDATE succeeds; the connection() context exits cleanly
#      (commit fires).
#   2. Failure: when a Sheets call raises SheetsWriteError, the
#      tool returns a Chad-voice error AND the connection() context
#      exits with the exception (so its rollback path runs).

def _mock_connection_with_cursor(cursor):
    """Same pattern as test_chad_input_tools._mock_connection_with_cursor."""
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cursor_cm)
    return conn


def _mock_google_services():
    """Build (creds, sheets_svc, drive_svc) mocks for the helper."""
    creds = MagicMock(name="creds")
    sheets_svc = MagicMock(name="sheets_svc")
    drive_svc = MagicMock(name="drive_svc")
    return creds, sheets_svc, drive_svc


# ---- update_customer_info ------------------------------------------------


class TestUpdateCustomerInfoDualWrite:
    """Postgres + Sheets together. The cursor side_effect feeds (in order):
      1. UPDATE ... RETURNING name → fetchone returns {"name": project}
      2. SELECT name FROM project (inside the mirror) → fetchone
         returns {"name": project}
    """

    def _wire(self, *, project_name="Whitfield"):
        cursor = MagicMock()
        cursor.fetchone = MagicMock(
            side_effect=[
                {"name": project_name, "drive_folder_id": "drive-folder-test-id"},  # RETURNING from UPDATE
                {"name": project_name, "drive_folder_id": "drive-folder-test-id"},  # mirror's project-name lookup
            ]
        )
        conn = _mock_connection_with_cursor(cursor)
        return cursor, conn

    def test_success_writes_postgres_then_sheets(self):
        from home_builder_agent.agents.chad_agent import (
            _tool_update_customer_info,
        )
        from home_builder_agent.integrations import sheets as _sheets_mod

        cursor, conn = self._wire()
        _, sheets_svc, drive_svc = _mock_google_services()

        with (
            patch(
                "home_builder_agent.integrations.postgres.connection",
                return_value=conn,
            ),
            patch(
                "home_builder_agent.agents.chad_agent._open_sheets_service_for_tool",
                return_value=(MagicMock(), sheets_svc, drive_svc),
            ),
            patch(
                "home_builder_agent.agents.chad_agent._find_tracker_id_for_project",
                return_value="tracker-sheet-id",
            ),
            patch.object(
                _sheets_mod, "update_project_info_field"
            ) as mock_write,
        ):
            msg, cost = _tool_update_customer_info(
                "abc-12345-aaaa-bbbb-cccc",
                customer_email="jane@example.com",
                customer_name="Bradford Family",
            )

        # Sheets was called twice — once per provided field, with the
        # tracker_id resolved by the mocked find helper.
        assert mock_write.call_count == 2
        called_fields = sorted(c.args[2] for c in mock_write.call_args_list)
        assert called_fields == ["Customer Email", "Customer Name"]
        # Every call used the correct tracker_id.
        for call in mock_write.call_args_list:
            assert call.args[1] == "tracker-sheet-id"

        # Chad-voice success message — no Tracker-error wording.
        assert "Updated Bradford" not in msg  # project_name is "Whitfield"
        assert "Updated Whitfield" in msg
        assert "couldn't update the Tracker" not in msg
        assert cost == 0.0

    def test_sheets_failure_returns_chad_voice_error(self):
        """When update_project_info_field raises SheetsWriteError, the
        tool catches _DualWriteSheetsFailure (its internal wrapping)
        and returns a Chad-voice error. The connection() __exit__ is
        called WITH the exception args (i.e. rollback path).
        """
        from home_builder_agent.agents.chad_agent import (
            _tool_update_customer_info,
        )
        from home_builder_agent.integrations import sheets as _sheets_mod

        cursor, conn = self._wire()
        _, sheets_svc, drive_svc = _mock_google_services()

        with (
            patch(
                "home_builder_agent.integrations.postgres.connection",
                return_value=conn,
            ),
            patch(
                "home_builder_agent.agents.chad_agent._open_sheets_service_for_tool",
                return_value=(MagicMock(), sheets_svc, drive_svc),
            ),
            patch(
                "home_builder_agent.agents.chad_agent._find_tracker_id_for_project",
                return_value="tracker-sheet-id",
            ),
            patch.object(
                _sheets_mod,
                "update_project_info_field",
                side_effect=_sheets_mod.SheetsWriteError(
                    "field 'Customer Email' not found"
                ),
            ),
        ):
            msg, cost = _tool_update_customer_info(
                "abc-123", customer_email="jane@example.com"
            )

        assert "couldn't update the Tracker" in msg
        assert "Nothing was changed" in msg
        # The Postgres UPDATE was emitted before the Sheets failure —
        # rollback responsibility belongs to connection().__exit__,
        # which we mocked. Asserting on __exit__'s call args confirms
        # the exception was propagated through the inner `with conn`
        # block (exc_type is not None).
        exit_call = conn.__exit__.call_args
        assert exit_call is not None
        exc_type = exit_call.args[0]
        assert exc_type is not None, (
            "connection.__exit__ was called with no exception — that "
            "means the rollback path never fired and Postgres + Sheets "
            "would diverge."
        )

    def test_no_tracker_returns_chad_voice_error(self):
        from home_builder_agent.agents.chad_agent import (
            _tool_update_customer_info,
            _DualWriteSheetsFailure,
        )

        cursor, conn = self._wire()
        _, sheets_svc, drive_svc = _mock_google_services()

        with (
            patch(
                "home_builder_agent.integrations.postgres.connection",
                return_value=conn,
            ),
            patch(
                "home_builder_agent.agents.chad_agent._open_sheets_service_for_tool",
                return_value=(MagicMock(), sheets_svc, drive_svc),
            ),
            patch(
                "home_builder_agent.agents.chad_agent._find_tracker_id_for_project",
                side_effect=_DualWriteSheetsFailure(
                    "no Tracker found for project 'Whitfield'"
                ),
            ),
        ):
            msg, cost = _tool_update_customer_info(
                "abc-123", customer_name="Bradford"
            )

        assert "couldn't update the Tracker" in msg
        assert "Whitfield" in msg


# ---- update_schedule_date ------------------------------------------------


class TestUpdateScheduleDateDualWrite:
    """Cursor side_effect for the success path:
      1. SELECT phase row → fetchone returns the phase
      2. Cascade SELECT (next phase) → fetchone returns None
      3. SELECT name FROM project (mirror) → fetchone returns project row
    """

    def _wire(self, *, project_name="Whitfield"):
        phase_row = {
            "id": "phase-id",
            "name": "Framing",
            "sequence_index": 4,
            "planned_start_date": date(2026, 5, 1),
            "planned_end_date": date(2026, 5, 30),
        }
        cursor = MagicMock()
        cursor.fetchone = MagicMock(
            side_effect=[
                phase_row,
                None,  # cascade — no next phase
                {"name": project_name, "drive_folder_id": "drive-folder-test-id"},  # mirror project lookup
            ]
        )
        conn = _mock_connection_with_cursor(cursor)
        return cursor, conn

    def test_success_writes_both_dates_to_sheets(self):
        from home_builder_agent.agents.chad_agent import (
            _tool_update_schedule_date,
        )
        from home_builder_agent.integrations import sheets as _sheets_mod

        cursor, conn = self._wire()
        _, sheets_svc, drive_svc = _mock_google_services()

        with (
            patch(
                "home_builder_agent.integrations.postgres.connection",
                return_value=conn,
            ),
            patch(
                "home_builder_agent.agents.chad_agent._open_sheets_service_for_tool",
                return_value=(MagicMock(), sheets_svc, drive_svc),
            ),
            patch(
                "home_builder_agent.agents.chad_agent._find_tracker_id_for_project",
                return_value="tracker-sheet-id",
            ),
            patch.object(
                _sheets_mod, "update_master_schedule_cell"
            ) as mock_write,
        ):
            msg, cost = _tool_update_schedule_date(
                "abc-123",
                phase_sequence_index=4,
                planned_start_date="2026-06-01",
                planned_end_date="2026-06-30",
            )

        # Two writes — Start + End. Both targeted by sequence_index=4.
        assert mock_write.call_count == 2
        columns_written = sorted(c.args[3] for c in mock_write.call_args_list)
        assert columns_written == ["End", "Start"]
        for call in mock_write.call_args_list:
            assert call.args[1] == "tracker-sheet-id"
            assert call.args[2] == 4  # sequence_index

        # Date values are formatted in the Tracker's display style.
        start_call = next(
            c for c in mock_write.call_args_list if c.args[3] == "Start"
        )
        end_call = next(
            c for c in mock_write.call_args_list if c.args[3] == "End"
        )
        assert start_call.args[4] == "Jun 01, 2026"
        assert end_call.args[4] == "Jun 30, 2026"

        assert "couldn't update the Tracker" not in msg
        assert cost == 0.0

    def test_only_end_supplied_writes_only_end_to_sheets(self):
        from home_builder_agent.agents.chad_agent import (
            _tool_update_schedule_date,
        )
        from home_builder_agent.integrations import sheets as _sheets_mod

        cursor, conn = self._wire()
        _, sheets_svc, drive_svc = _mock_google_services()

        with (
            patch(
                "home_builder_agent.integrations.postgres.connection",
                return_value=conn,
            ),
            patch(
                "home_builder_agent.agents.chad_agent._open_sheets_service_for_tool",
                return_value=(MagicMock(), sheets_svc, drive_svc),
            ),
            patch(
                "home_builder_agent.agents.chad_agent._find_tracker_id_for_project",
                return_value="tracker-sheet-id",
            ),
            patch.object(
                _sheets_mod, "update_master_schedule_cell"
            ) as mock_write,
        ):
            _tool_update_schedule_date(
                "abc-123",
                phase_sequence_index=4,
                planned_end_date="2026-06-15",
            )

        # Only ONE call — the End cell.
        assert mock_write.call_count == 1
        assert mock_write.call_args.args[3] == "End"

    def test_sheets_failure_rolls_back(self):
        from home_builder_agent.agents.chad_agent import (
            _tool_update_schedule_date,
        )
        from home_builder_agent.integrations import sheets as _sheets_mod

        cursor, conn = self._wire()
        _, sheets_svc, drive_svc = _mock_google_services()

        with (
            patch(
                "home_builder_agent.integrations.postgres.connection",
                return_value=conn,
            ),
            patch(
                "home_builder_agent.agents.chad_agent._open_sheets_service_for_tool",
                return_value=(MagicMock(), sheets_svc, drive_svc),
            ),
            patch(
                "home_builder_agent.agents.chad_agent._find_tracker_id_for_project",
                return_value="tracker-sheet-id",
            ),
            patch.object(
                _sheets_mod,
                "update_master_schedule_cell",
                side_effect=_sheets_mod.SheetsWriteError(
                    "no row with #=4 found"
                ),
            ),
        ):
            # Use a start that's still ≤ existing end (2026-05-30) so
            # date validation doesn't short-circuit the UPDATE+mirror.
            msg, _ = _tool_update_schedule_date(
                "abc-123",
                phase_sequence_index=4,
                planned_start_date="2026-05-15",
            )

        assert "couldn't update the Tracker" in msg
        assert "Nothing was changed" in msg
        exit_call = conn.__exit__.call_args
        assert exit_call.args[0] is not None, (
            "connection.__exit__ did not see an exception — rollback "
            "path did not fire."
        )


# ---- reorder_phase --------------------------------------------------------


class TestReorderPhaseDualWrite:
    """reorder_phase calls one batch update; here we verify it receives
    the full {phase_name: new_sequence_index} dict.

    cursor.fetchone side_effect order (success path, move-down 3→7):
      1. SELECT moving phase row
      2. SELECT MAX(sequence_index)
      3. SELECT name FROM project (mirror)

    cursor.fetchall side_effect order:
      1. (mirror's affected-phases SELECT) — returns the affected rows
    """

    def _wire(self, *, moving_idx=3, max_idx=10, project_name="Whitfield"):
        moving_row = {
            "id": "moving-id",
            "name": "Framing",
            "sequence_index": moving_idx,
            "planned_start_date": None,
            "planned_end_date": None,
        }
        cursor = MagicMock()
        cursor.fetchone = MagicMock(
            side_effect=[
                moving_row,
                {"max_idx": max_idx},
                {"name": project_name, "drive_folder_id": "drive-folder-test-id"},
            ]
        )
        # affected phases between old=3 and new=7 (after park/shift/land):
        # Framing now at 7, plus phases that shifted from 4..7 down to 3..6.
        cursor.fetchall = MagicMock(
            return_value=[
                {"name": "Foundation", "sequence_index": 3},
                {"name": "Slab", "sequence_index": 4},
                {"name": "MEP Rough", "sequence_index": 5},
                {"name": "Roof", "sequence_index": 6},
                {"name": "Framing", "sequence_index": 7},
            ]
        )
        cursor.rowcount = 4
        conn = _mock_connection_with_cursor(cursor)
        return cursor, conn

    def test_success_batch_update_carries_all_affected_phases(self):
        from home_builder_agent.agents.chad_agent import _tool_reorder_phase
        from home_builder_agent.integrations import sheets as _sheets_mod

        cursor, conn = self._wire()
        _, sheets_svc, drive_svc = _mock_google_services()

        with (
            patch(
                "home_builder_agent.integrations.postgres.connection",
                return_value=conn,
            ),
            patch(
                "home_builder_agent.agents.chad_agent._open_sheets_service_for_tool",
                return_value=(MagicMock(), sheets_svc, drive_svc),
            ),
            patch(
                "home_builder_agent.agents.chad_agent._find_tracker_id_for_project",
                return_value="tracker-sheet-id",
            ),
            patch.object(
                _sheets_mod, "update_master_schedule_sequence_indices"
            ) as mock_batch,
        ):
            msg, _ = _tool_reorder_phase(
                "abc-123",
                7,
                phase_sequence_index=3,
            )

        # Exactly one batch call.
        assert mock_batch.call_count == 1
        # Inspect the dict it received.
        args = mock_batch.call_args.args
        assert args[1] == "tracker-sheet-id"
        name_to_seq = args[2]
        # Every affected phase appears with its new position.
        assert name_to_seq == {
            "Foundation": 3,
            "Slab": 4,
            "MEP Rough": 5,
            "Roof": 6,
            "Framing": 7,
        }

        assert "Moved Framing" in msg
        assert "couldn't update the Tracker" not in msg

    def test_sheets_failure_rolls_back(self):
        from home_builder_agent.agents.chad_agent import _tool_reorder_phase
        from home_builder_agent.integrations import sheets as _sheets_mod

        cursor, conn = self._wire()
        _, sheets_svc, drive_svc = _mock_google_services()

        with (
            patch(
                "home_builder_agent.integrations.postgres.connection",
                return_value=conn,
            ),
            patch(
                "home_builder_agent.agents.chad_agent._open_sheets_service_for_tool",
                return_value=(MagicMock(), sheets_svc, drive_svc),
            ),
            patch(
                "home_builder_agent.agents.chad_agent._find_tracker_id_for_project",
                return_value="tracker-sheet-id",
            ),
            patch.object(
                _sheets_mod,
                "update_master_schedule_sequence_indices",
                side_effect=_sheets_mod.SheetsWriteError(
                    "phase 'Framing' not found in Master Schedule column B"
                ),
            ),
        ):
            msg, _ = _tool_reorder_phase(
                "abc-123",
                7,
                phase_sequence_index=3,
            )

        assert "couldn't update the Tracker" in msg
        assert "Nothing was changed" in msg
        exit_call = conn.__exit__.call_args
        assert exit_call.args[0] is not None, (
            "connection.__exit__ did not see an exception — rollback "
            "path did not fire after reorder Sheets failure."
        )

    def test_dry_run_does_not_call_sheets(self):
        """dry_run path returns before park/shift/land — and before any
        Sheets call. Verified by the mirror being uncalled.
        """
        from home_builder_agent.agents.chad_agent import _tool_reorder_phase
        from home_builder_agent.integrations import sheets as _sheets_mod

        cursor, conn = self._wire()
        _, sheets_svc, drive_svc = _mock_google_services()

        with (
            patch(
                "home_builder_agent.integrations.postgres.connection",
                return_value=conn,
            ),
            patch(
                "home_builder_agent.agents.chad_agent._open_sheets_service_for_tool",
                return_value=(MagicMock(), sheets_svc, drive_svc),
            ),
            patch(
                "home_builder_agent.agents.chad_agent._find_tracker_id_for_project",
                return_value="tracker-sheet-id",
            ),
            patch.object(
                _sheets_mod, "update_master_schedule_sequence_indices"
            ) as mock_batch,
        ):
            msg, _ = _tool_reorder_phase(
                "abc-123",
                7,
                phase_sequence_index=3,
                dry_run=True,
            )

        mock_batch.assert_not_called()
        assert "(dry-run)" in msg


# ===========================================================================
# Section 3 — Atomicity invariants (Postgres + Sheets stay in sync)
# ===========================================================================


class TestAtomicity:
    """The dual-write atomicity contract:

      * On success: Postgres UPDATE fires, mirror fires, connection
        exits cleanly (commit) — both stores converge on the same
        new value.
      * On Sheets failure: Postgres UPDATE fires (uncommitted),
        mirror raises SheetsWriteError, connection's __exit__ is
        invoked WITH the exception → rollback path runs. No commit.

    These tests assert specifically on the __exit__ exc_type so a
    future refactor that accidentally swallows the Sheets exception
    (and therefore commits stale Postgres state) will go red.
    """

    def test_customer_info_postgres_rolled_back_on_sheets_failure(self):
        """The Postgres UPDATE was emitted; if the mirror raises, the
        with-conn block sees the exception → rollback. We check this
        by asserting __exit__ was called with a real exception class.
        """
        from home_builder_agent.agents.chad_agent import (
            _tool_update_customer_info,
        )
        from home_builder_agent.integrations import sheets as _sheets_mod

        cursor = MagicMock()
        cursor.fetchone = MagicMock(
            side_effect=[
                {"name": "Whitfield"},  # RETURNING
                {"name": "Whitfield"},  # mirror's project-name lookup
            ]
        )
        conn = _mock_connection_with_cursor(cursor)

        with (
            patch(
                "home_builder_agent.integrations.postgres.connection",
                return_value=conn,
            ),
            patch(
                "home_builder_agent.agents.chad_agent._open_sheets_service_for_tool",
                return_value=(MagicMock(), MagicMock(), MagicMock()),
            ),
            patch(
                "home_builder_agent.agents.chad_agent._find_tracker_id_for_project",
                return_value="tracker-id",
            ),
            patch.object(
                _sheets_mod,
                "update_project_info_field",
                side_effect=_sheets_mod.SheetsWriteError("boom"),
            ),
        ):
            _tool_update_customer_info(
                "abc-123", customer_name="Bradford"
            )

        # The connection's __exit__ saw an exception type → real
        # connection() context manager would call rollback().
        exit_args = conn.__exit__.call_args.args
        assert exit_args[0] is not None
        # The exception class is our dual-write failure marker.
        from home_builder_agent.agents.chad_agent import (
            _DualWriteSheetsFailure,
        )
        assert issubclass(exit_args[0], _DualWriteSheetsFailure)

    def test_schedule_date_postgres_rolled_back_on_sheets_failure(self):
        from home_builder_agent.agents.chad_agent import (
            _tool_update_schedule_date,
            _DualWriteSheetsFailure,
        )
        from home_builder_agent.integrations import sheets as _sheets_mod

        phase_row = {
            "id": "phase-id",
            "name": "Framing",
            "sequence_index": 4,
            "planned_start_date": date(2026, 5, 1),
            "planned_end_date": date(2026, 5, 30),
        }
        cursor = MagicMock()
        cursor.fetchone = MagicMock(
            side_effect=[
                phase_row,
                None,  # cascade — no next phase
                {"name": "Whitfield"},  # mirror project lookup
            ]
        )
        conn = _mock_connection_with_cursor(cursor)

        with (
            patch(
                "home_builder_agent.integrations.postgres.connection",
                return_value=conn,
            ),
            patch(
                "home_builder_agent.agents.chad_agent._open_sheets_service_for_tool",
                return_value=(MagicMock(), MagicMock(), MagicMock()),
            ),
            patch(
                "home_builder_agent.agents.chad_agent._find_tracker_id_for_project",
                return_value="tracker-id",
            ),
            patch.object(
                _sheets_mod,
                "update_master_schedule_cell",
                side_effect=_sheets_mod.SheetsWriteError("nope"),
            ),
        ):
            _tool_update_schedule_date(
                "abc-123",
                phase_sequence_index=4,
                planned_start_date="2026-05-15",  # ≤ existing end 2026-05-30
            )

        exit_args = conn.__exit__.call_args.args
        assert exit_args[0] is not None
        assert issubclass(exit_args[0], _DualWriteSheetsFailure)

    def test_reorder_phase_postgres_rolled_back_on_sheets_failure(self):
        from home_builder_agent.agents.chad_agent import (
            _tool_reorder_phase,
            _DualWriteSheetsFailure,
        )
        from home_builder_agent.integrations import sheets as _sheets_mod

        moving_row = {
            "id": "moving-id",
            "name": "Framing",
            "sequence_index": 3,
            "planned_start_date": None,
            "planned_end_date": None,
        }
        cursor = MagicMock()
        cursor.fetchone = MagicMock(
            side_effect=[
                moving_row,
                {"max_idx": 10},
                {"name": "Whitfield"},
            ]
        )
        cursor.fetchall = MagicMock(
            return_value=[
                {"name": "Framing", "sequence_index": 5},
            ]
        )
        cursor.rowcount = 2
        conn = _mock_connection_with_cursor(cursor)

        with (
            patch(
                "home_builder_agent.integrations.postgres.connection",
                return_value=conn,
            ),
            patch(
                "home_builder_agent.agents.chad_agent._open_sheets_service_for_tool",
                return_value=(MagicMock(), MagicMock(), MagicMock()),
            ),
            patch(
                "home_builder_agent.agents.chad_agent._find_tracker_id_for_project",
                return_value="tracker-id",
            ),
            patch.object(
                _sheets_mod,
                "update_master_schedule_sequence_indices",
                side_effect=_sheets_mod.SheetsWriteError("nope"),
            ),
        ):
            _tool_reorder_phase("abc-123", 5, phase_sequence_index=3)

        exit_args = conn.__exit__.call_args.args
        assert exit_args[0] is not None
        assert issubclass(exit_args[0], _DualWriteSheetsFailure)


# ===========================================================================
# CTO scope 2026-05-11: NULL drive_folder_id MUST raise loud error
# ===========================================================================
#
# A silent fall-through to "Postgres-only" would create Sheets/Postgres
# divergence with no alert. _resolve_project_for_dual_write enforces a
# loud failure that triggers Postgres rollback.

class TestNullDriveFolderIdLoudError:
    """Per CTO v1.2 review — refuse to dual-write when the project's
    drive_folder_id is NULL/empty in Postgres. Each of the 3 tools must
    raise _DualWriteSheetsFailure (→ rollback) instead of falling back
    to a silent Postgres-only write.
    """

    def _cursor_with_null_drive_folder(self, second_row):
        """Cursor whose mirror-lookup row has name set but drive_folder_id
        empty. The Postgres UPDATE / SELECTs upstream of the mirror return
        whatever ``second_row`` supplies (caller-provided so it matches
        the tool's expected SQL order)."""
        cursor = MagicMock()
        cursor.fetchone = MagicMock(side_effect=second_row)
        return cursor, _mock_connection_with_cursor(cursor)

    def test_update_customer_info_null_drive_folder_id_rolls_back(self):
        from home_builder_agent.agents.chad_agent import (
            _tool_update_customer_info,
            _DualWriteSheetsFailure,
        )

        # update_customer_info: 1 RETURNING row + 1 mirror lookup row.
        # Mirror row has name but NULL drive_folder_id.
        cursor, conn = self._cursor_with_null_drive_folder([
            {"name": "Whitfield"},
            {"name": "Whitfield", "drive_folder_id": None},
        ])

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, cost = _tool_update_customer_info(
                "abc-12345-aaaa-bbbb-cccc",
                customer_email="jane@example.com",
            )

        # Tool returns Chad-voice error rather than crashing.
        assert "couldn't update the Tracker" in msg.lower() or "tracker" in msg.lower()
        # Rollback path engaged.
        exit_args = conn.__exit__.call_args.args
        assert exit_args[0] is not None
        assert issubclass(exit_args[0], _DualWriteSheetsFailure)
        # Error message explains the cause clearly.
        assert "drive_folder_id" in str(exit_args[1])
        assert "hb-bridge" in str(exit_args[1])

    def test_update_schedule_date_null_drive_folder_id_rolls_back(self):
        from home_builder_agent.agents.chad_agent import (
            _tool_update_schedule_date,
            _DualWriteSheetsFailure,
        )

        # update_schedule_date: phase SELECT, optional cascade SELECT,
        # then mirror SELECT. With phase_sequence_index lookup and no
        # cascade trigger, that's 2 rows then the mirror row.
        phase_row = {
            "id": "phase-id",
            "name": "Framing",
            "sequence_index": 4,
            "planned_start_date": date(2026, 5, 1),
            "planned_end_date": date(2026, 5, 30),
        }
        cursor, conn = self._cursor_with_null_drive_folder([
            phase_row,
            None,
            {"name": "Whitfield", "drive_folder_id": ""},  # empty string also caught
        ])

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_update_schedule_date(
                "abc-12345-aaaa-bbbb-cccc",
                phase_sequence_index=4,
                planned_end_date="2026-06-15",
            )

        exit_args = conn.__exit__.call_args.args
        assert exit_args[0] is not None
        assert issubclass(exit_args[0], _DualWriteSheetsFailure)
        assert "drive_folder_id" in str(exit_args[1])

    def test_reorder_phase_null_drive_folder_id_rolls_back(self):
        from home_builder_agent.agents.chad_agent import (
            _tool_reorder_phase,
            _DualWriteSheetsFailure,
        )

        moving_row = {
            "id": "phase-id-3",
            "name": "Framing",
            "sequence_index": 3,
            "planned_start_date": None,
            "planned_end_date": None,
        }
        # reorder_phase calls cur.fetchall once (affected phases) and
        # cur.fetchone three times (moving row, max_idx, project
        # lookup). Wire each appropriately.
        cursor = MagicMock()
        cursor.fetchone = MagicMock(side_effect=[
            moving_row,
            {"max_idx": 10},
            {"name": "Whitfield", "drive_folder_id": None},
        ])
        cursor.fetchall = MagicMock(return_value=[
            {"name": "Framing", "sequence_index": 5},
        ])
        conn = _mock_connection_with_cursor(cursor)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            _tool_reorder_phase(
                "abc-12345-aaaa-bbbb-cccc",
                5,
                phase_sequence_index=3,
            )

        exit_args = conn.__exit__.call_args.args
        assert exit_args[0] is not None, (
            "Expected the Postgres txn to roll back via _DualWriteSheetsFailure "
            "when drive_folder_id is NULL"
        )
        assert issubclass(exit_args[0], _DualWriteSheetsFailure)
        assert "drive_folder_id" in str(exit_args[1])
