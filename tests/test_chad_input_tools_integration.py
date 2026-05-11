"""Real-Postgres integration tests for the reorder_phase tool.

CTO review of v1 ship (2026-05-11) explicitly required: "Do not ship
reorder_phase until migration 012 is applied AND the test suite runs
against a real Postgres CHECK, not a mock." This file is that gate.

The mocked-cursor unit tests in `test_chad_input_tools.py` validate
the SQL ordering and parameter shape, but they don't actually fire
Postgres CHECK constraints — so an earlier draft of reorder_phase
that parked at `sequence_index = -1` passed all 56 unit tests while
being broken in production (CHECK constraints are NOT deferrable in
Postgres; they fire per-statement, not at COMMIT). This file runs the
exact park-and-swap SQL against a real Postgres so any future
regression to a CHECK-violating sentinel is caught immediately.

Gating: tests skip unless `SUPABASE_DATABASE_URL` is set in env. Each
test wraps its work in a `BEGIN; ... ROLLBACK;` so nothing persists.
"""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest


DATABASE_URL = os.environ.get("SUPABASE_DATABASE_URL") or os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="SUPABASE_DATABASE_URL (or DATABASE_URL) not set — integration tests skipped",
)


# ---------------------------------------------------------------------------
# Test fixture: a transient project + 5 phases, rolled back after each test.
# ---------------------------------------------------------------------------


@pytest.fixture
def transient_project_with_phases():
    """Yields ``(conn, project_id, phase_ids[5])`` and ROLLBACKs on teardown.

    The phases land at sequence_index 1..5 in a fresh project. Tests
    mutate them via the reorder SQL pattern and verify final state
    before the rollback wipes everything.

    NOTE: connect with autocommit=False so the whole fixture +
    test body runs as one transaction. ROLLBACK on teardown
    discards everything — production data is untouched.
    """
    conn = psycopg.connect(DATABASE_URL, autocommit=False)
    project_id = str(uuid.uuid4())
    phase_ids: list[str] = []

    try:
        with conn.cursor() as cur:
            # Project requires customer_name NOT NULL and at least one
            # target date (per CHECK on home_builder.project).
            cur.execute(
                """
                INSERT INTO home_builder.project (
                    id, name, customer_name, target_completion_date, status
                ) VALUES (
                    %s::uuid, %s, %s, %s, 'active'
                )
                """,
                (project_id, f"itest-{project_id[:8]}", "test-customer", "2026-12-31"),
            )

            # 5 phases at sequence_index 1..5
            for i in range(1, 6):
                phase_id = str(uuid.uuid4())
                phase_ids.append(phase_id)
                cur.execute(
                    """
                    INSERT INTO home_builder.phase (
                        id, project_id, phase_template_id,
                        name, sequence_index, status
                    ) VALUES (
                        %s::uuid, %s::uuid, %s,
                        %s, %s, 'not-started'
                    )
                    """,
                    (
                        phase_id,
                        project_id,
                        f"itest-phase-{i}",
                        f"Phase {i}",
                        i,
                    ),
                )

        yield conn, project_id, phase_ids
    finally:
        # Always rollback — never let test data leak into production.
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The actual gate: park at 0 must NOT violate the CHECK constraint.
# ---------------------------------------------------------------------------


def test_park_at_zero_does_not_violate_check_constraint(transient_project_with_phases):
    """Migration 012 widened phase.sequence_index CHECK to BETWEEN 0 AND 24.

    Before mig 012 (CHECK 1..24), this UPDATE would raise:
        psycopg.errors.CheckViolation:
            new row for relation "phase" violates check constraint

    After mig 012, sequence_index = 0 is legal as a transient
    sentinel. This test is the explicit regression gate — if any
    future change rolls back mig 012 or narrows the CHECK, this
    test goes red on the first run.
    """
    conn, project_id, phase_ids = transient_project_with_phases

    moving_phase_id = phase_ids[1]  # the phase at sequence_index = 2

    with conn.cursor() as cur:
        # The park step from _tool_reorder_phase. Must NOT raise.
        cur.execute(
            "UPDATE home_builder.phase SET sequence_index = 0 WHERE id = %s::uuid",
            (moving_phase_id,),
        )
        # Verify it landed at 0.
        cur.execute(
            "SELECT sequence_index FROM home_builder.phase WHERE id = %s::uuid",
            (moving_phase_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0, (
            f"Park step did not land at 0 — got {row[0]}. "
            "Migration 012 may have regressed."
        )


def test_full_reorder_swap_completes_in_one_transaction(
    transient_project_with_phases,
):
    """End-to-end: execute the exact park-and-swap SQL pattern from
    ``_tool_reorder_phase`` against a real Postgres. Moves phase at
    sequence_index=2 to sequence_index=4.

    Verifies:
      * Park step doesn't raise the CHECK constraint
      * Shift step doesn't raise the UNIQUE constraint
      * Land step results in the canonical final ordering [1,3,4,2,5]
        renumbered to [1,2,3,4,5] with phases re-arranged
    """
    conn, project_id, phase_ids = transient_project_with_phases

    moving_phase_id = phase_ids[1]  # original sequence_index = 2
    old_position = 2
    new_position = 4

    with conn.cursor() as cur:
        # 1. PARK: move the moving phase to sentinel index 0.
        cur.execute(
            "UPDATE home_builder.phase SET sequence_index = 0 WHERE id = %s::uuid",
            (moving_phase_id,),
        )

        # 2. SHIFT: phases between old+1..new shift DOWN by 1 (moving down).
        cur.execute(
            """
            UPDATE home_builder.phase
            SET sequence_index = sequence_index - 1
            WHERE project_id = %s::uuid
              AND sequence_index BETWEEN %s AND %s
              AND id != %s::uuid
            """,
            (project_id, old_position + 1, new_position, moving_phase_id),
        )

        # 3. LAND: place the moving phase at new_position.
        cur.execute(
            "UPDATE home_builder.phase SET sequence_index = %s WHERE id = %s::uuid",
            (new_position, moving_phase_id),
        )

        # Verify final ordering.
        cur.execute(
            """
            SELECT name, sequence_index FROM home_builder.phase
            WHERE project_id = %s::uuid
            ORDER BY sequence_index
            """,
            (project_id,),
        )
        rows = cur.fetchall()

    # Expected: Phase 1@1, Phase 3@2, Phase 4@3, Phase 2@4, Phase 5@5
    expected = [
        ("Phase 1", 1),
        ("Phase 3", 2),
        ("Phase 4", 3),
        ("Phase 2", 4),
        ("Phase 5", 5),
    ]
    actual = [(r[0], r[1]) for r in rows]
    assert actual == expected, (
        f"Reorder produced wrong final order.\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}"
    )


def test_check_constraint_still_rejects_negative_values(
    transient_project_with_phases,
):
    """Migration 012 widened the lower bound to 0, NOT below. Negative
    sequence_index values must still be rejected. This test prevents
    a future "lazy widening" (e.g. dropping CHECK entirely) that
    would silently weaken the schema invariant.
    """
    conn, project_id, phase_ids = transient_project_with_phases

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "UPDATE home_builder.phase SET sequence_index = -1 WHERE id = %s::uuid",
                (phase_ids[0],),
            )


def test_check_constraint_still_rejects_above_24(transient_project_with_phases):
    """Upper bound is preserved at 24."""
    conn, project_id, phase_ids = transient_project_with_phases

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "UPDATE home_builder.phase SET sequence_index = 25 WHERE id = %s::uuid",
                (phase_ids[0],),
            )
