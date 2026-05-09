"""draft_actions.py — DraftAction entity for Chad's judgment queue.

Per canonical-data-model.md § 18 and morning-view-model.md § Open dependencies.

The model: a DraftAction is a piece of work an agent has prepared and is
holding for Chad's confirmation — a drafted email reply, a change-order
approval pending send, a weekly client update awaiting Chad's eyes, a
lien-waiver follow-up nudge. Every drafting agent (gmail_followup,
change_order_agent, client_update_agent, lien_waiver_agent,
supplier_email_watcher) writes a row when it produces something pending
review. The morning view-model's `judgment_queue` section reads from
this table.

Six DraftKind values ship in V1 (extensible; add a kind rather than
overload an existing payload):

  gmail-reply-draft               Drafted reply to an inbound email
  change-order-approval           Drafted change-order doc + email to homeowner
  lien-waiver-followup            Drafted nudge for a missing waiver
  client-update-email             Drafted weekly homeowner update
  vendor-eta-confirmation         Drafted vendor follow-up after ETA detection
  inspection-scheduling-request   Drafted email to building dept for next slot

Status lifecycle: pending → (approved | edited-then-approved | discarded).
Once decided, the row is preserved for audit; queries filter on
`status='pending'` for the active queue.

This module is pure-Python (engine-side data model + helpers). Persistence
adapter lives in store_postgres.py. View-model projection lives in
view_models.py (when the morning view-model implementation lands).
Reconcile dispatch for approve/edit/discard lives in reconcile.py (also
follow-on commit).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enums (mirror schemas.py wire-format types, kept here for engine-side use)
# ---------------------------------------------------------------------------


class DraftKind(str, Enum):
    """V1 vocabulary of judgment-queue items.

    Open-enum at the DB layer per draft_action CHECK constraint; pinned
    here so engine + iOS + Mac agree on the names. Adding a new kind =
    update this enum + the SQL CHECK + the renderer's switch + the
    originating agent. Per morning-view-model.md § DraftKind.
    """

    GMAIL_REPLY_DRAFT = "gmail-reply-draft"
    CHANGE_ORDER_APPROVAL = "change-order-approval"
    LIEN_WAIVER_FOLLOWUP = "lien-waiver-followup"
    CLIENT_UPDATE_EMAIL = "client-update-email"
    VENDOR_ETA_CONFIRMATION = "vendor-eta-confirmation"
    INSPECTION_SCHEDULING_REQUEST = "inspection-scheduling-request"


class DraftStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    EDITED_THEN_APPROVED = "edited-then-approved"
    DISCARDED = "discarded"


# ---------------------------------------------------------------------------
# Entity (canonical-data-model.md § 18)
# ---------------------------------------------------------------------------


@dataclass
class DraftAction:
    """Engine-side DraftAction record. Mirrors entity 18.

    A draft pending Chad's review. Drafting agents construct this via
    `make_draft_action(...)`, persist via
    `store_postgres.insert_draft_action(...)`. Renderer never writes
    this directly — Chad's tap-to-approve / edit / discard flows
    through UserAction → reconcile → service-role update.
    """

    id: str
    project_id: str
    kind: str                              # DraftKind value
    status: str                            # DraftStatus value
    originating_agent: str
    summary: str
    created_at: datetime

    subject_line: str | None = None
    body_payload: dict[str, Any] = field(default_factory=dict)
    external_ref: str | None = None
    from_or_to: str | None = None

    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_notes: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == DraftStatus.PENDING.value

    def age_seconds(self, now: datetime | None = None) -> int:
        """Seconds since created_at (UTC). For queue-card 'created N hours ago' chip."""
        ref = now or datetime.now(timezone.utc)
        delta = ref - self.created_at
        return max(0, int(delta.total_seconds()))


# ---------------------------------------------------------------------------
# Builders + helpers
# ---------------------------------------------------------------------------


def make_draft_action(
    *,
    project_id: str,
    kind: DraftKind | str,
    originating_agent: str,
    summary: str,
    subject_line: str | None = None,
    body_payload: dict | None = None,
    external_ref: str | None = None,
    from_or_to: str | None = None,
    draft_action_id: str | None = None,
    created_at: datetime | None = None,
) -> DraftAction:
    """Build a fresh DraftAction in `pending` status.

    Caller persists separately via store_postgres.insert_draft_action().
    Pure helper; no I/O.

    Per-kind body_payload contract (renderer + reconcile dispatch
    expects these shapes; not enforced at this layer):

      gmail-reply-draft:
        { thread_id, original_subject, original_from, draft_subject,
          draft_body, recipient }
      change-order-approval:
        { co_number, change_summary, line_items: [...], total_delta_usd,
          drive_doc_id, recipient_homeowner_email }
      lien-waiver-followup:
        { actuals_log_row_id, vendor, amount_usd, payment_date,
          followup_email_body, recipient }
      client-update-email:
        { week_ending, project_status_summary, photos: [...],
          recipient_homeowner_email, draft_body }
      vendor-eta-confirmation:
        { vendor, sku_or_category, prior_eta, new_eta, draft_followup_body,
          recipient }
      inspection-scheduling-request:
        { inspection_type, target_window_start, target_window_end,
          permit_number, recipient_dept_email, draft_body }

    Adding a new kind: extend DraftKind, document the body_payload shape
    here, update the SQL CHECK constraint, update renderer's switch.
    """

    kind_str = kind.value if isinstance(kind, DraftKind) else kind

    return DraftAction(
        id=draft_action_id or str(uuid.uuid4()),
        project_id=project_id,
        kind=kind_str,
        status=DraftStatus.PENDING.value,
        originating_agent=originating_agent,
        summary=summary,
        subject_line=subject_line,
        body_payload=body_payload or {},
        external_ref=external_ref,
        from_or_to=from_or_to,
        created_at=created_at or datetime.now(timezone.utc),
    )
