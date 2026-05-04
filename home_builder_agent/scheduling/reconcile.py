"""reconcile.py — engine reconcile pass for UserAction → entity dispatch.

Customer-side write loop, engine half:

  iOS shell → POST /v1/turtles/home-builder/actions
       ↓
  home_builder.user_action  ← shell insert (idempotency-key-deduped)
       ↓
  hb-reconcile (this module) ← scans new rows, dispatches per action_type
       ↓
  home_builder.{phase, delivery, inspection, ...}  ← engine entity writes
       ↓
  next /views/{view_type} fetch reflects the change

Watermark: JSON file at WATERMARK_PATH stores last_processed_synced_at as
ISO timestamp. Same pattern as `.watcher_state.json` /
`.inbox_watcher_state.json`. Single-instance for Phase A; will move to
DB-backed watermark or distributed lock in Phase B when reconcile runs
on Modal/Railway workers.

Idempotency: dispatch handlers are idempotent (e.g., setting
phase.status='complete' twice is a no-op). The watermark prevents
re-processing the same row, but the safety net is the handlers themselves.

Out of v0 scope (handlers degrade to 'skipped: not yet implemented'):
  - checklist-tick      (needs checklist_item write path; lands later)
  - material-delivery-confirm (needs delivery write path; lands later)
  - sub-checkin         (needs Resource entity; V2)
  - vendor-pin          (needs Vendor write path; lands with VI System)

V0 supported:
  - inspection-result   → updates Inspection + may flip Phase status
  - schedule-override   → updates Phase planned_*_date and/or status
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Callable

import psycopg

from home_builder_agent.integrations.postgres import connection
from home_builder_agent.scheduling.store_postgres import save_phase_status_change


# ---------------------------------------------------------------------------
# Watermark storage
# ---------------------------------------------------------------------------

WATERMARK_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".reconcile_watermark.json")
)


def _load_watermark() -> datetime | None:
    """Read the last-processed synced_at from disk. Returns None on first run."""
    try:
        with open(WATERMARK_PATH) as f:
            data = json.load(f)
        ts = data.get("last_processed_synced_at")
        if not ts:
            return None
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, ValueError):
        # Corrupt watermark → treat as first run, re-scan everything
        # (handlers are idempotent, so this is safe)
        return None


def _save_watermark(ts: datetime) -> None:
    """Persist the latest processed synced_at."""
    with open(WATERMARK_PATH, "w") as f:
        json.dump({"last_processed_synced_at": ts.isoformat()}, f)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class DispatchOutcome(str, Enum):
    APPLIED = "applied"               # Handler ran successfully
    SKIPPED = "skipped"               # Action type known but write path not yet implemented
    UNKNOWN = "unknown-action-type"   # action_type not in handler registry
    ERROR = "error"                   # Handler raised; row will be retried next run


@dataclass
class DispatchResult:
    action_id: str
    action_type: str
    target_entity_type: str
    target_entity_id: str
    outcome: DispatchOutcome
    notes: str = ""
    synced_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "target_entity_type": self.target_entity_type,
            "target_entity_id": self.target_entity_id,
            "outcome": self.outcome.value,
            "notes": self.notes,
            "synced_at": self.synced_at.isoformat() if self.synced_at else None,
        }


@dataclass
class ReconcileReport:
    started_at: datetime
    finished_at: datetime
    watermark_before: datetime | None
    watermark_after: datetime | None
    actions_scanned: int = 0
    results: list[DispatchResult] = field(default_factory=list)

    def summary_counts(self) -> dict[str, int]:
        counts = {o.value: 0 for o in DispatchOutcome}
        for r in self.results:
            counts[r.outcome.value] += 1
        return counts

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "watermark_before": self.watermark_before.isoformat() if self.watermark_before else None,
            "watermark_after": self.watermark_after.isoformat() if self.watermark_after else None,
            "actions_scanned": self.actions_scanned,
            "summary": self.summary_counts(),
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Action scan
# ---------------------------------------------------------------------------

def _scan_unprocessed_actions(
    since: datetime | None,
    conn: psycopg.Connection,
) -> list[dict]:
    """Return user_action rows with synced_at > `since`, ordered ASC.

    Uses idx_hb_user_action_synced (cf. migration_002 Q-G index).
    """
    if since is None:
        # First run — pull everything
        sql = """
            SELECT
                id::text AS id,
                actor_user_id::text AS actor_user_id,
                project_id::text AS project_id,
                surface,
                action_type,
                target_entity_type,
                target_entity_id::text AS target_entity_id,
                payload,
                recorded_at,
                synced_at,
                idempotency_key::text AS idempotency_key
            FROM home_builder.user_action
            ORDER BY synced_at ASC
            LIMIT 1000
        """
        params: tuple = ()
    else:
        sql = """
            SELECT
                id::text AS id,
                actor_user_id::text AS actor_user_id,
                project_id::text AS project_id,
                surface,
                action_type,
                target_entity_type,
                target_entity_id::text AS target_entity_id,
                payload,
                recorded_at,
                synced_at,
                idempotency_key::text AS idempotency_key
            FROM home_builder.user_action
            WHERE synced_at > %s
            ORDER BY synced_at ASC
            LIMIT 1000
        """
        params = (since,)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


# ---------------------------------------------------------------------------
# Dispatchers — one per action_type
# ---------------------------------------------------------------------------

def _dispatch_inspection_result(
    action: dict,
    conn: psycopg.Connection,
) -> DispatchResult:
    """Apply an inspection-result UserAction to the engine state.

    Expected payload shape (see canonical-data-model.md § entity 17 + § Per-type
    payload contract for inspection-failure events):
        {
            "result": "passed" | "failed" | "reinspect-needed",
            "actual_date": "YYYY-MM-DD" (optional),
            "phase_status_after": "complete" | "in-progress" | "blocked-on-checklist" (optional),
            "notes": "..." (optional)
        }

    V0 behavior: if `phase_status_after` is in the payload AND
    target_entity_type == 'phase', flip the phase status. Engine intentionally
    does NOT auto-derive phase status from inspection result in v0 — the iOS
    shell decides what status the phase should have post-inspection and
    sends it explicitly. (V1+ may add server-side mapping rules.)
    """
    payload = action.get("payload") or {}
    target_type = action["target_entity_type"]
    target_id = action["target_entity_id"]
    phase_status_after = payload.get("phase_status_after")
    actual_date_raw = payload.get("actual_date")

    base = DispatchResult(
        action_id=action["id"],
        action_type="inspection-result",
        target_entity_type=target_type,
        target_entity_id=target_id,
        outcome=DispatchOutcome.SKIPPED,
        synced_at=action["synced_at"],
    )

    if target_type != "phase":
        base.notes = f"target_entity_type={target_type!r}; v0 only handles phase targets"
        return base

    if not phase_status_after:
        base.notes = "no phase_status_after in payload; engine doesn't auto-derive in v0"
        return base

    # Parse actual_date if present
    actual_date_obj: date | None = None
    if actual_date_raw:
        try:
            actual_date_obj = date.fromisoformat(actual_date_raw)
        except ValueError:
            base.outcome = DispatchOutcome.ERROR
            base.notes = f"invalid actual_date: {actual_date_raw!r} (need YYYY-MM-DD)"
            return base

    # Apply: if status flips to 'complete', stamp actual_end_date
    actual_end = actual_date_obj if phase_status_after == "complete" else None
    actual_start = actual_date_obj if phase_status_after == "in-progress" else None

    ok = save_phase_status_change(
        phase_id=target_id,
        status=phase_status_after,
        actual_start_date=actual_start,
        actual_end_date=actual_end,
        conn=conn,
    )

    if ok:
        base.outcome = DispatchOutcome.APPLIED
        base.notes = (
            f"phase {target_id[:8]}… → status={phase_status_after!r}"
            + (f", actual_end={actual_end}" if actual_end else "")
            + (f", actual_start={actual_start}" if actual_start else "")
        )
    else:
        base.outcome = DispatchOutcome.ERROR
        base.notes = f"save_phase_status_change returned False — phase id not found?"
    return base


def _dispatch_schedule_override(
    action: dict,
    conn: psycopg.Connection,
) -> DispatchResult:
    """Apply a schedule-override UserAction.

    Expected payload:
        {
            "phase_status": "..." (optional),
            "actual_start_date": "YYYY-MM-DD" (optional),
            "actual_end_date": "YYYY-MM-DD" (optional),
            "reason": "..." (optional)
        }
    """
    payload = action.get("payload") or {}
    target_type = action["target_entity_type"]
    target_id = action["target_entity_id"]

    base = DispatchResult(
        action_id=action["id"],
        action_type="schedule-override",
        target_entity_type=target_type,
        target_entity_id=target_id,
        outcome=DispatchOutcome.SKIPPED,
        synced_at=action["synced_at"],
    )

    if target_type != "phase":
        base.notes = f"target_entity_type={target_type!r}; v0 only handles phase"
        return base

    new_status = payload.get("phase_status")
    actual_start = _parse_iso_date(payload.get("actual_start_date"))
    actual_end = _parse_iso_date(payload.get("actual_end_date"))

    if not (new_status or actual_start or actual_end):
        base.notes = "no actionable fields in payload (phase_status / actual_*_date all empty)"
        return base

    # save_phase_status_change requires status; if not given, leave it
    # by re-applying current status. Cheap workaround for v0.
    if new_status is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM home_builder.phase WHERE id = %s::uuid",
                (target_id,),
            )
            row = cur.fetchone()
            if not row:
                base.outcome = DispatchOutcome.ERROR
                base.notes = f"phase {target_id[:8]}… not found"
                return base
            new_status = row["status"]

    ok = save_phase_status_change(
        phase_id=target_id,
        status=new_status,
        actual_start_date=actual_start,
        actual_end_date=actual_end,
        conn=conn,
    )

    if ok:
        base.outcome = DispatchOutcome.APPLIED
        notes_parts = [f"phase {target_id[:8]}…"]
        if payload.get("phase_status"):
            notes_parts.append(f"status={new_status!r}")
        if actual_start:
            notes_parts.append(f"actual_start={actual_start}")
        if actual_end:
            notes_parts.append(f"actual_end={actual_end}")
        base.notes = " ".join(notes_parts)
    else:
        base.outcome = DispatchOutcome.ERROR
        base.notes = "save_phase_status_change returned False"
    return base


def _dispatch_unknown(action_type: str) -> Callable[[dict, psycopg.Connection], DispatchResult]:
    """Factory for action types we know about but haven't implemented yet."""
    def _handler(action: dict, conn: psycopg.Connection) -> DispatchResult:
        return DispatchResult(
            action_id=action["id"],
            action_type=action_type,
            target_entity_type=action["target_entity_type"],
            target_entity_id=action["target_entity_id"],
            outcome=DispatchOutcome.SKIPPED,
            notes=f"{action_type!r} dispatcher not yet implemented in v0",
            synced_at=action["synced_at"],
        )
    return _handler


# Registry — extend as new write paths come online
DISPATCHERS: dict[str, Callable[[dict, psycopg.Connection], DispatchResult]] = {
    "inspection-result":         _dispatch_inspection_result,
    "schedule-override":         _dispatch_schedule_override,
    "checklist-tick":            _dispatch_unknown("checklist-tick"),
    "material-delivery-confirm": _dispatch_unknown("material-delivery-confirm"),
    "sub-checkin":               _dispatch_unknown("sub-checkin"),
    "vendor-pin":                _dispatch_unknown("vendor-pin"),
}


def _parse_iso_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Reconcile pass
# ---------------------------------------------------------------------------

def reconcile_pass(
    *,
    since_override: datetime | None = None,
    dry_run: bool = False,
) -> ReconcileReport:
    """Run one reconcile pass.

    Args:
        since_override:  Override the persisted watermark for this run.
                         Useful for replaying a window or starting fresh.
        dry_run:         Read + dispatch (within transaction), but rollback
                         instead of commit. No persistence; no watermark update.

    Returns:
        ReconcileReport with per-action outcomes.
    """
    started_at = datetime.now(timezone.utc)
    watermark_before = since_override if since_override is not None else _load_watermark()

    report = ReconcileReport(
        started_at=started_at,
        finished_at=started_at,        # filled in at end
        watermark_before=watermark_before,
        watermark_after=watermark_before,
    )

    with connection(application_name="hb-reconcile") as conn:
        try:
            actions = _scan_unprocessed_actions(watermark_before, conn=conn)
            report.actions_scanned = len(actions)

            latest_synced_at = watermark_before

            for action in actions:
                handler = DISPATCHERS.get(action["action_type"])
                if handler is None:
                    result = DispatchResult(
                        action_id=action["id"],
                        action_type=action["action_type"],
                        target_entity_type=action["target_entity_type"],
                        target_entity_id=action["target_entity_id"],
                        outcome=DispatchOutcome.UNKNOWN,
                        notes=f"no handler registered for action_type={action['action_type']!r}",
                        synced_at=action["synced_at"],
                    )
                else:
                    try:
                        result = handler(action, conn)
                    except Exception as e:
                        result = DispatchResult(
                            action_id=action["id"],
                            action_type=action["action_type"],
                            target_entity_type=action["target_entity_type"],
                            target_entity_id=action["target_entity_id"],
                            outcome=DispatchOutcome.ERROR,
                            notes=f"handler raised: {type(e).__name__}: {e}",
                            synced_at=action["synced_at"],
                        )

                report.results.append(result)

                # Advance candidate watermark to this row's synced_at if we
                # didn't error out — this is what "processed" means: the
                # engine evaluated it and either applied, skipped (with
                # reason), or marked unknown. Errors do NOT advance the
                # watermark, so they get retried next run.
                if result.outcome != DispatchOutcome.ERROR:
                    if latest_synced_at is None or action["synced_at"] > latest_synced_at:
                        latest_synced_at = action["synced_at"]

            if dry_run:
                conn.rollback()
                report.watermark_after = watermark_before
            else:
                conn.commit()
                if latest_synced_at is not None and latest_synced_at != watermark_before:
                    _save_watermark(latest_synced_at)
                    report.watermark_after = latest_synced_at
                else:
                    report.watermark_after = watermark_before

        except Exception:
            conn.rollback()
            raise

    report.finished_at = datetime.now(timezone.utc)
    return report
