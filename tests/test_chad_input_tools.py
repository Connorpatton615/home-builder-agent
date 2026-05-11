"""Tests for chad_agent's Tracker-canonicalization input tools.

Per ADR 2026-05-11 (Postgres-canonical home-builder project state):
the three tools `update_customer_info`, `update_schedule_date`, and
`reorder_phase` replace Connor's manual Google Sheets Tracker edits.
This module verifies:

  - Each tool's input-validation fast paths (empty/missing fields,
    bad date formats, malformed email/phone, etc.)
  - Each tool's happy-path DB write — SQL shape + Chad-voice
    confirmation
  - `update_customer_info` only writes fields explicitly provided
    (never null-out unprovided), rejects empty-string ("looks like a
    typo" guard from spec)
  - `update_schedule_date` validates end ≥ start, emits cascade
    warning when shifting end pushes against the next phase
  - `reorder_phase` park-and-swap pattern: SQL ordering preserves
    the (project_id, sequence_index) UNIQUE constraint mid-swap,
    move-up / move-down / no-op all correct
  - The TOOLS registry exposes all three with the spec'd schemas
  - The shared dispatch path is exercised by importing each tool
    function directly

We mock at the postgres.connection() boundary (not at psycopg) so
tests run offline and we can assert on the SQL the tool emitted.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — mock the postgres.connection() context manager
# ---------------------------------------------------------------------------


def _mock_connection_with_cursor(cursor: MagicMock) -> MagicMock:
    """Build a MagicMock that behaves like the `connection()` context
    manager: `with connection(...) as conn: with conn.cursor() as cur:`.

    Both layers of context-manager support __enter__/__exit__; the
    cursor returned from `conn.cursor()` is the supplied MagicMock.
    """
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cursor_cm)
    return conn


# ---------------------------------------------------------------------------
# Tool registry shape
# ---------------------------------------------------------------------------


class TestToolsRegistry:
    """Per spec: three new top-level tools registered in TOOLS."""

    def test_all_three_tools_registered(self):
        from home_builder_agent.agents.chad_agent import TOOLS

        names = [t["name"] for t in TOOLS]
        assert "update_customer_info" in names
        assert "update_schedule_date" in names
        assert "reorder_phase" in names

    def test_update_customer_info_schema(self):
        from home_builder_agent.agents.chad_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "update_customer_info")
        props = tool["input_schema"]["properties"]
        # Required: project_id only. The "at least one of …" constraint
        # lives in the handler at runtime (Anthropic input_schema
        # rejects top-level anyOf/oneOf/allOf).
        assert tool["input_schema"]["required"] == ["project_id"]
        for f in (
            "project_id",
            "customer_name",
            "customer_email",
            "customer_phone",
            "address",
            "job_code",
            "notes",
        ):
            assert f in props, f"{f} missing from update_customer_info schema"
        # No anyOf/oneOf/allOf at the top level — Anthropic 400s on those.
        assert "anyOf" not in tool["input_schema"]
        assert "oneOf" not in tool["input_schema"]
        assert "allOf" not in tool["input_schema"]

    def test_update_schedule_date_schema(self):
        from home_builder_agent.agents.chad_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "update_schedule_date")
        props = tool["input_schema"]["properties"]
        assert tool["input_schema"]["required"] == ["project_id"]
        for f in (
            "project_id",
            "phase_sequence_index",
            "phase_name",
            "planned_start_date",
            "planned_end_date",
        ):
            assert f in props
        # Description must explain the cascade-aware (non-)behavior so
        # Opus knows to surface the warning to Chad.
        desc = tool["description"].lower()
        assert "cascade" in desc
        # No top-level anyOf/oneOf/allOf.
        assert "anyOf" not in tool["input_schema"]
        assert "oneOf" not in tool["input_schema"]
        assert "allOf" not in tool["input_schema"]

    def test_reorder_phase_schema(self):
        from home_builder_agent.agents.chad_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "reorder_phase")
        props = tool["input_schema"]["properties"]
        assert set(tool["input_schema"]["required"]) == {"project_id", "new_position"}
        for f in ("project_id", "phase_sequence_index", "phase_name", "new_position"):
            assert f in props
        # No top-level anyOf/oneOf/allOf.
        assert "anyOf" not in tool["input_schema"]
        assert "oneOf" not in tool["input_schema"]
        assert "allOf" not in tool["input_schema"]


# ---------------------------------------------------------------------------
# update_customer_info — validation fast paths
# ---------------------------------------------------------------------------


class TestUpdateCustomerInfoValidation:
    def test_empty_project_id_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_update_customer_info

        msg, cost = _tool_update_customer_info("")
        assert "project_id is required" in msg
        assert cost == 0.0

    def test_no_fields_provided_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_update_customer_info

        msg, cost = _tool_update_customer_info("abc-123")
        assert "at least one" in msg
        assert cost == 0.0

    def test_empty_string_field_rejected_as_typo(self):
        """Per spec: empty-string is a typo, not 'clear the field'.
        Clearing requires passing null (None on the Python side).
        """
        from home_builder_agent.agents.chad_agent import _tool_update_customer_info

        msg, cost = _tool_update_customer_info("abc-123", customer_name="")
        assert "looks like a typo" in msg
        assert "customer_name" in msg

    def test_malformed_email_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_update_customer_info

        msg, cost = _tool_update_customer_info(
            "abc-123", customer_email="not-an-email"
        )
        assert "doesn't look like an email" in msg

    def test_phone_with_under_10_digits_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_update_customer_info

        msg, cost = _tool_update_customer_info(
            "abc-123", customer_phone="555-0142"
        )
        assert "doesn't have 10 digits" in msg

    def test_phone_with_extra_digits_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_update_customer_info

        msg, cost = _tool_update_customer_info(
            "abc-123", customer_phone="1-251-555-0142"
        )
        assert "doesn't have 10 digits" in msg

    def test_dry_run_does_not_write(self):
        from home_builder_agent.agents.chad_agent import _tool_update_customer_info

        with patch(
            "home_builder_agent.integrations.postgres.connection"
        ) as mock_conn:
            msg, cost = _tool_update_customer_info(
                "abc-123",
                customer_name="Bradford Family",
                dry_run=True,
            )
        mock_conn.assert_not_called()
        assert "(dry-run)" in msg
        assert "customer_name" in msg


class TestUpdateCustomerInfoHappyPath:
    def test_single_field_update_writes_one_column(self):
        from home_builder_agent.agents.chad_agent import _tool_update_customer_info

        cursor = MagicMock()
        cursor.fetchone = MagicMock(return_value={"name": "Whitfield"})
        conn = _mock_connection_with_cursor(cursor)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, cost = _tool_update_customer_info(
                "abc-12345-aaaa-bbbb-cccc",
                customer_email="jane@example.com",
            )

        # SQL was issued exactly once and ONLY updates customer_email.
        assert cursor.execute.call_count == 1
        sql_arg, params_arg = cursor.execute.call_args[0]
        assert "customer_email = %s" in sql_arg
        # Other unprovided columns must NOT appear in the SET clause.
        for other in (
            "customer_name = %s",
            "customer_phone = %s",
            "address = %s",
            "job_code = %s",
            "notes = %s",
        ):
            assert other not in sql_arg
        # First positional in params is the email; trailing is the id.
        assert params_arg[0] == "jane@example.com"
        assert params_arg[-1] == "abc-12345-aaaa-bbbb-cccc"

        assert "Updated Whitfield" in msg
        assert "email" in msg
        assert "[updated_fields:" in msg
        assert cost == 0.0

    def test_multi_field_update_all_columns_in_set_clause(self):
        from home_builder_agent.agents.chad_agent import _tool_update_customer_info

        cursor = MagicMock()
        cursor.fetchone = MagicMock(return_value={"name": "Whitfield"})
        conn = _mock_connection_with_cursor(cursor)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, cost = _tool_update_customer_info(
                "abc-12345",
                customer_name="Bradford Family",
                customer_email="brad@example.com",
                customer_phone="(251) 555-0142",
                job_code="BRAD-2026",
            )

        sql_arg, params_arg = cursor.execute.call_args[0]
        for col in (
            "customer_name = %s",
            "customer_email = %s",
            "customer_phone = %s",
            "job_code = %s",
        ):
            assert col in sql_arg
        assert "address = %s" not in sql_arg
        assert "notes = %s" not in sql_arg
        # Phone normalized — non-digits stripped, must end up as 10 digits.
        assert "2515550142" in params_arg

    def test_phone_normalized_strips_punctuation(self):
        from home_builder_agent.agents.chad_agent import _tool_update_customer_info

        cursor = MagicMock()
        cursor.fetchone = MagicMock(return_value={"name": "Whitfield"})
        conn = _mock_connection_with_cursor(cursor)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            _tool_update_customer_info(
                "abc-12345", customer_phone="(251) 555-0142"
            )

        _sql, params = cursor.execute.call_args[0]
        assert "2515550142" in params

    def test_no_matching_project_returns_helpful_error(self):
        from home_builder_agent.agents.chad_agent import _tool_update_customer_info

        cursor = MagicMock()
        cursor.fetchone = MagicMock(return_value=None)
        conn = _mock_connection_with_cursor(cursor)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, cost = _tool_update_customer_info(
                "doesnotexist", customer_name="Bradford"
            )
        assert "no project matched" in msg
        assert cost == 0.0


# ---------------------------------------------------------------------------
# update_schedule_date — validation + cascade behavior
# ---------------------------------------------------------------------------


class TestUpdateScheduleDateValidation:
    def test_empty_project_id_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        msg, _ = _tool_update_schedule_date("", planned_start_date="2026-06-01")
        assert "project_id is required" in msg

    def test_no_dates_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        msg, _ = _tool_update_schedule_date("abc-123", phase_sequence_index=3)
        assert "at least one of planned_start_date or planned_end_date" in msg

    def test_invalid_start_date_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        msg, _ = _tool_update_schedule_date(
            "abc-123",
            phase_sequence_index=3,
            planned_start_date="not-a-date",
        )
        assert "planned_start_date" in msg
        assert "valid YYYY-MM-DD" in msg

    def test_invalid_end_date_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        msg, _ = _tool_update_schedule_date(
            "abc-123",
            phase_sequence_index=3,
            planned_end_date="2026/06/15",
        )
        assert "planned_end_date" in msg
        assert "valid YYYY-MM-DD" in msg


class TestUpdateScheduleDateResolution:
    def test_phase_not_found_by_index(self):
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        cursor = MagicMock()
        # First call: phase lookup → no rows
        cursor.fetchone = MagicMock(return_value=None)
        conn = _mock_connection_with_cursor(cursor)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_update_schedule_date(
                "abc-123",
                phase_sequence_index=99,
                planned_start_date="2026-06-01",
            )
        assert "no phase at position 99" in msg

    def test_phase_name_ambiguous_returns_candidate_list(self):
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        cursor = MagicMock()
        cursor.fetchall = MagicMock(
            return_value=[
                {
                    "id": "p1",
                    "name": "Framing",
                    "sequence_index": 4,
                    "planned_start_date": None,
                    "planned_end_date": None,
                },
                {
                    "id": "p2",
                    "name": "Rough Framing",
                    "sequence_index": 5,
                    "planned_start_date": None,
                    "planned_end_date": None,
                },
            ]
        )
        conn = _mock_connection_with_cursor(cursor)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_update_schedule_date(
                "abc-123",
                phase_name="framing",
                planned_start_date="2026-06-01",
            )
        assert "ambiguous" in msg
        assert "Framing" in msg
        assert "Rough Framing" in msg
        # The candidate list shows their sequence_index so Chad/Opus can
        # disambiguate.
        assert "#4" in msg
        assert "#5" in msg

    def test_both_identifiers_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        cursor = MagicMock()
        conn = _mock_connection_with_cursor(cursor)
        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_update_schedule_date(
                "abc-123",
                phase_sequence_index=3,
                phase_name="Framing",
                planned_start_date="2026-06-01",
            )
        assert "OR" in msg
        assert "not both" in msg

    def test_no_identifier_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        cursor = MagicMock()
        conn = _mock_connection_with_cursor(cursor)
        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_update_schedule_date(
                "abc-123", planned_start_date="2026-06-01"
            )
        assert "phase_sequence_index or phase_name is required" in msg


class TestUpdateScheduleDateRanges:
    """End-before-start rejection + cascade warning."""

    def test_end_before_start_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        cursor = MagicMock()
        cursor.fetchone = MagicMock(
            return_value={
                "id": "p1",
                "name": "Framing",
                "sequence_index": 4,
                "planned_start_date": date(2026, 5, 1),
                "planned_end_date": date(2026, 5, 30),
            }
        )
        conn = _mock_connection_with_cursor(cursor)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_update_schedule_date(
                "abc-123",
                phase_sequence_index=4,
                planned_start_date="2026-06-15",
                planned_end_date="2026-06-01",
            )
        assert "before start date" in msg

    def test_only_end_supplied_validates_against_db_start(self):
        """When only end is passed, fetch the current start from DB and
        verify the new end ≥ existing start. Spec: 'if only one
        supplied, fetch the other from DB and validate'.
        """
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        cursor = MagicMock()
        # First call → phase row with existing start = June 1
        # Second (cascade) call → no next phase
        cursor.fetchone = MagicMock(
            side_effect=[
                {
                    "id": "p1",
                    "name": "Framing",
                    "sequence_index": 4,
                    "planned_start_date": date(2026, 6, 1),
                    "planned_end_date": date(2026, 6, 30),
                },
                None,
            ]
        )
        conn = _mock_connection_with_cursor(cursor)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_update_schedule_date(
                "abc-123",
                phase_sequence_index=4,
                planned_end_date="2026-05-15",  # before existing start
            )
        assert "before start date" in msg

    def test_happy_path_no_cascade_warning(self):
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        cursor = MagicMock()
        cursor.fetchone = MagicMock(
            side_effect=[
                {
                    "id": "p1",
                    "name": "Framing",
                    "sequence_index": 4,
                    "planned_start_date": date(2026, 5, 1),
                    "planned_end_date": date(2026, 5, 30),
                },
                # next phase starts AFTER new end → no cascade
                {
                    "id": "p2",
                    "name": "Rough MEP",
                    "sequence_index": 5,
                    "planned_start_date": date(2026, 7, 1),
                },
            ]
        )
        conn = _mock_connection_with_cursor(cursor)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_update_schedule_date(
                "abc-123",
                phase_sequence_index=4,
                planned_end_date="2026-06-12",
            )
        assert "Framing" in msg
        assert "Heads up" not in msg  # no cascade warning
        assert "[update_schedule_date:" in msg

    def test_happy_path_with_cascade_warning(self):
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        cursor = MagicMock()
        cursor.fetchone = MagicMock(
            side_effect=[
                {
                    "id": "p1",
                    "name": "Framing",
                    "sequence_index": 4,
                    "planned_start_date": date(2026, 5, 1),
                    "planned_end_date": date(2026, 5, 30),
                },
                # next phase starts BEFORE new end → cascade warning
                {
                    "id": "p2",
                    "name": "Rough MEP",
                    "sequence_index": 5,
                    "planned_start_date": date(2026, 6, 1),
                },
            ]
        )
        conn = _mock_connection_with_cursor(cursor)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_update_schedule_date(
                "abc-123",
                phase_sequence_index=4,
                planned_end_date="2026-06-15",
            )
        assert "Heads up" in msg
        assert "Rough MEP" in msg
        # Critical: should NOT have auto-cascaded — only 2 fetchone calls,
        # one UPDATE (the moving phase), no UPDATE on the next phase.
        assert "Did NOT auto-shift" in msg

    def test_dry_run_does_not_write(self):
        from home_builder_agent.agents.chad_agent import _tool_update_schedule_date

        cursor = MagicMock()
        cursor.fetchone = MagicMock(
            return_value={
                "id": "p1",
                "name": "Framing",
                "sequence_index": 4,
                "planned_start_date": date(2026, 5, 1),
                "planned_end_date": date(2026, 5, 30),
            }
        )
        conn = _mock_connection_with_cursor(cursor)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_update_schedule_date(
                "abc-123",
                phase_sequence_index=4,
                planned_end_date="2026-06-12",
                dry_run=True,
            )
        # Only the SELECT for resolution should have run — no UPDATE.
        update_calls = [
            c for c in cursor.execute.call_args_list
            if c[0][0].strip().upper().startswith("UPDATE")
        ]
        assert update_calls == []
        assert "(dry-run)" in msg


# ---------------------------------------------------------------------------
# reorder_phase — park-and-swap pattern
# ---------------------------------------------------------------------------


class TestReorderPhaseValidation:
    def test_empty_project_id_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_reorder_phase

        msg, _ = _tool_reorder_phase("", 3, phase_sequence_index=4)
        assert "project_id is required" in msg

    def test_new_position_below_one_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_reorder_phase

        msg, _ = _tool_reorder_phase(
            "abc-123", 0, phase_sequence_index=4
        )
        assert "must be ≥ 1" in msg

    def test_new_position_non_integer_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_reorder_phase

        msg, _ = _tool_reorder_phase(
            "abc-123", "five", phase_sequence_index=4  # type: ignore[arg-type]
        )
        assert "integer" in msg


class TestReorderPhaseSwapSql:
    """The load-bearing test class — verifies the park-and-swap SQL order
    that the UNIQUE (project_id, sequence_index) constraint depends on.
    """

    @staticmethod
    def _setup_cursor(
        *,
        moving_row: dict,
        max_idx: int,
    ) -> tuple[MagicMock, MagicMock]:
        """Build a cursor that returns `moving_row` for the first SELECT
        (phase resolution) and `max_idx` for the second (MAX bounds check).
        Subsequent UPDATEs return rowcount = 3 (arbitrary, non-zero).
        """
        cursor = MagicMock()
        cursor.fetchone = MagicMock(
            side_effect=[moving_row, {"max_idx": max_idx}]
        )
        cursor.rowcount = 3
        conn = _mock_connection_with_cursor(cursor)
        return cursor, conn

    def test_move_down_correct_shift_sql(self):
        """Moving from position 3 → 7 should shift positions 4..7 DOWN by 1
        (sequence_index = sequence_index - 1), and the park step
        (sequence_index = 0 on moving_id) must precede the shift.
        """
        from home_builder_agent.agents.chad_agent import _tool_reorder_phase

        moving_row = {
            "id": "phase-id-3",
            "name": "Framing",
            "sequence_index": 3,
            "planned_start_date": None,
            "planned_end_date": None,
        }
        cursor, conn = self._setup_cursor(moving_row=moving_row, max_idx=10)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_reorder_phase(
                "abc-123",
                7,
                phase_sequence_index=3,
            )

        # Collect SQL strings in order.
        sqls = [c[0][0] for c in cursor.execute.call_args_list]

        # First SELECT — phase lookup.
        assert "SELECT" in sqls[0]
        assert "FROM home_builder.phase" in sqls[0]
        # Second SELECT — MAX(sequence_index) bounds check.
        assert "MAX(sequence_index)" in sqls[1]
        # Third — PARK (sequence_index = 0)
        assert "sequence_index = 0" in sqls[2]
        # Fourth — SHIFT in the moving-down direction
        assert "sequence_index - 1" in sqls[3]
        # The shift must EXCLUDE the moving row (we set it to -1 above)
        assert "id != %s::uuid" in sqls[3]
        # Fifth — LAND at new_position
        assert "sequence_index = %s" in sqls[4]

        # Critical ordering: park BEFORE shift BEFORE land. Otherwise
        # the UNIQUE (project_id, sequence_index) constraint would be
        # violated mid-transaction.
        park_idx = next(i for i, s in enumerate(sqls) if "sequence_index = 0" in s)
        shift_idx = next(
            i for i, s in enumerate(sqls) if "sequence_index - 1" in s or "sequence_index + 1" in s
        )
        land_idx = next(
            i for i, s in enumerate(sqls[shift_idx + 1:], start=shift_idx + 1)
            if "sequence_index = %s" in s
        )
        assert park_idx < shift_idx < land_idx

        # Shift range: old+1 (= 4) through new (= 7)
        shift_params = cursor.execute.call_args_list[3][0][1]
        # params: (project_id, 4, 7, moving_id)
        assert shift_params[1] == 4
        assert shift_params[2] == 7

        assert "Moved Framing" in msg
        assert "3 → 7" in msg
        assert "down" in msg

    def test_move_up_correct_shift_sql(self):
        """Moving from position 7 → 3 should shift positions 3..6 UP by 1
        (sequence_index = sequence_index + 1).
        """
        from home_builder_agent.agents.chad_agent import _tool_reorder_phase

        moving_row = {
            "id": "phase-id-7",
            "name": "Trim",
            "sequence_index": 7,
            "planned_start_date": None,
            "planned_end_date": None,
        }
        cursor, conn = self._setup_cursor(moving_row=moving_row, max_idx=10)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_reorder_phase(
                "abc-123",
                3,
                phase_sequence_index=7,
            )

        sqls = [c[0][0] for c in cursor.execute.call_args_list]
        # Park step present.
        assert any("sequence_index = 0" in s for s in sqls)
        # Shift step uses + 1 (moving up direction).
        assert any("sequence_index + 1" in s for s in sqls)
        # No - 1 shift in this direction.
        assert not any("sequence_index - 1" in s for s in sqls)

        # Shift range: new (= 3) through old-1 (= 6)
        shift_call = next(
            c for c in cursor.execute.call_args_list
            if "sequence_index + 1" in c[0][0]
        )
        shift_params = shift_call[0][1]
        assert shift_params[1] == 3
        assert shift_params[2] == 6

        assert "Moved Trim" in msg
        assert "7 → 3" in msg
        assert "up" in msg

    def test_no_op_same_position(self):
        """new_position == old_position → no DB writes, just a no-op
        confirmation. Spec: 'If new_position == old_position, return
        no-op'.
        """
        from home_builder_agent.agents.chad_agent import _tool_reorder_phase

        moving_row = {
            "id": "phase-id-5",
            "name": "Drywall",
            "sequence_index": 5,
            "planned_start_date": None,
            "planned_end_date": None,
        }
        cursor, conn = self._setup_cursor(moving_row=moving_row, max_idx=10)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_reorder_phase(
                "abc-123",
                5,
                phase_sequence_index=5,
            )

        # Only the SELECT phase lookup + MAX bounds check ran.
        # No UPDATE statements at all.
        update_calls = [
            c for c in cursor.execute.call_args_list
            if c[0][0].strip().upper().startswith("UPDATE")
        ]
        assert update_calls == []

        assert "already at position 5" in msg
        assert "No change" in msg

    def test_new_position_above_max_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_reorder_phase

        moving_row = {
            "id": "phase-id-3",
            "name": "Framing",
            "sequence_index": 3,
            "planned_start_date": None,
            "planned_end_date": None,
        }
        cursor, conn = self._setup_cursor(moving_row=moving_row, max_idx=10)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_reorder_phase(
                "abc-123",
                15,  # > max_idx of 10
                phase_sequence_index=3,
            )
        assert "above the project's highest phase position" in msg
        assert "[1, 10]" in msg

        # No park / shift / land should have run.
        update_calls = [
            c for c in cursor.execute.call_args_list
            if c[0][0].strip().upper().startswith("UPDATE")
        ]
        assert update_calls == []

    def test_dry_run_skips_park_shift_land(self):
        from home_builder_agent.agents.chad_agent import _tool_reorder_phase

        moving_row = {
            "id": "phase-id-3",
            "name": "Framing",
            "sequence_index": 3,
            "planned_start_date": None,
            "planned_end_date": None,
        }
        cursor, conn = self._setup_cursor(moving_row=moving_row, max_idx=10)

        with patch(
            "home_builder_agent.integrations.postgres.connection",
            return_value=conn,
        ):
            msg, _ = _tool_reorder_phase(
                "abc-123",
                7,
                phase_sequence_index=3,
                dry_run=True,
            )

        # Only the two SELECTs (resolution + bounds) — no UPDATEs.
        update_calls = [
            c for c in cursor.execute.call_args_list
            if c[0][0].strip().upper().startswith("UPDATE")
        ]
        assert update_calls == []
        assert "(dry-run)" in msg
        assert "Framing" in msg


# ---------------------------------------------------------------------------
# reorder_phase — parameterized full-suite of moves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old,new,direction,expected_shift_op,expected_low,expected_high",
    [
        (3, 7, "down", "sequence_index - 1", 4, 7),   # move-down
        (7, 3, "up", "sequence_index + 1", 3, 6),     # move-up
        (1, 10, "down", "sequence_index - 1", 2, 10), # to end
        (10, 1, "up", "sequence_index + 1", 1, 9),    # to start
        (5, 6, "down", "sequence_index - 1", 6, 6),   # adjacent down
        (6, 5, "up", "sequence_index + 1", 5, 5),     # adjacent up
    ],
)
def test_reorder_phase_parameterized(
    old, new, direction, expected_shift_op, expected_low, expected_high
):
    """Drives the park-and-swap path across every interesting (old, new)
    pair to confirm the SQL direction + range parameters track correctly.
    """
    from home_builder_agent.agents.chad_agent import _tool_reorder_phase

    moving_row = {
        "id": "moving-id",
        "name": "TestPhase",
        "sequence_index": old,
        "planned_start_date": None,
        "planned_end_date": None,
    }
    cursor = MagicMock()
    cursor.fetchone = MagicMock(
        side_effect=[moving_row, {"max_idx": 24}]
    )
    cursor.rowcount = abs(new - old)
    conn = _mock_connection_with_cursor(cursor)

    with patch(
        "home_builder_agent.integrations.postgres.connection",
        return_value=conn,
    ):
        msg, _ = _tool_reorder_phase(
            "abc-123", new, phase_sequence_index=old
        )

    sqls = [c[0][0] for c in cursor.execute.call_args_list]
    # Park step always present.
    assert any("sequence_index = 0" in s for s in sqls), \
        f"park step missing for {old}→{new}"
    # Shift uses the correct direction operator.
    shift_sqls = [s for s in sqls if expected_shift_op in s]
    assert len(shift_sqls) == 1, \
        f"expected exactly one {expected_shift_op} shift for {old}→{new}, got {len(shift_sqls)}"

    # Shift range params match.
    shift_call = next(
        c for c in cursor.execute.call_args_list
        if expected_shift_op in c[0][0]
    )
    shift_params = shift_call[0][1]
    assert shift_params[1] == expected_low, \
        f"expected low={expected_low}, got {shift_params[1]} for {old}→{new}"
    assert shift_params[2] == expected_high, \
        f"expected high={expected_high}, got {shift_params[2]} for {old}→{new}"

    assert f"{old} → {new}" in msg
    assert direction in msg


# ---------------------------------------------------------------------------
# Helpers — exercise the loose validators directly
# ---------------------------------------------------------------------------


class TestPhoneNormalizer:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("(251) 555-0142", "2515550142"),
            ("251-555-0142", "2515550142"),
            ("2515550142", "2515550142"),
            ("251.555.0142", "2515550142"),
            ("+1 251 555 0142", None),     # 11 digits — rejected (spec: 10)
            ("555-0142", None),            # 7 digits
            ("", None),
        ],
    )
    def test_normalize(self, raw, expected):
        from home_builder_agent.agents.chad_agent import _normalize_phone

        assert _normalize_phone(raw) == expected


class TestEmailRegex:
    @pytest.mark.parametrize(
        "raw, ok",
        [
            ("jane@example.com", True),
            ("foo.bar+tag@sub.example.co", True),
            ("not-an-email", False),
            ("@example.com", False),
            ("jane@", False),
            ("jane@example", False),  # no TLD
            ("", False),
        ],
    )
    def test_match(self, raw, ok):
        from home_builder_agent.agents.chad_agent import _EMAIL_RE

        assert bool(_EMAIL_RE.match(raw)) is ok
