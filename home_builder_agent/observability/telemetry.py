"""telemetry.py — canonical event emission for the home-builder worker.

Per ADR 2026-05-09 Multi-Tenant Telemetry Architecture
(~/Projects/patton-os/data/decisions.md). Sync sibling of
patton-ai-ios/backend/app/services/telemetry.py — same contract,
same `platform.event` table, same v1 event taxonomy. Used by
home-builder-agent's launchd jobs and CLI agents to log canonical
agent.* events.

Design rules (from the ADR):
  • NEVER raise. A failed event must not break the producing agent.
  • No-op when PATTON_TENANT_ID is empty/unset (CI, local dev,
    pre-cutover Connor environment).
  • One INSERT per call. ~5-20ms; OK inline for non-hot-path emitters.
  • Uses the same DATABASE_URL the engine adapter uses; no separate
    pool / connection.

v1 event taxonomy is fixed (see ADR § 3). Add new types only when a
downstream consumer requires them. Update the ADR alongside.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


VALID_EVENT_TYPES = frozenset({
    "client.signed_in",
    "client.viewed_project",
    "client.opened_projects_list",
    "client.submitted_quick_action",
    "agent.morning_brief_sent",
    "agent.inbox_reply_drafted",
    "agent.inbox_reply_sent",
    "agent.alert_paged",
    "agent.tts_played",
})


def _resolve_tenant() -> str:
    """Return the configured tenant slug for this worker (empty string
    if not configured — emit_event then no-ops)."""
    return os.environ.get("PATTON_TENANT_ID", "").strip()


def emit_event(
    *,
    event_type: str,
    source: str,
    actor_id: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    tenant_id: str | None = None,
) -> None:
    """Insert one row into platform.event. Never raises.

    Args:
      event_type: one of VALID_EVENT_TYPES (see ADR v1 taxonomy)
      source: short identifier of the emitter, e.g. 'home-builder-agent.morning_brief'
      actor_id: human user uuid as text, or None for autonomous events
      subject_type: what was acted on (e.g. 'project', 'thread', 'phase')
      subject_id: identifier of subject (UUID, thread id, etc.)
      metadata: per-event-type freeform JSON context (cost, duration,
        recipient, vendor name, etc.)
      tenant_id: override the env-default tenant (rare; jobs processing
        events for multiple tenants).

    Failure mode: any exception is caught + logged at WARNING; the
    caller continues. Telemetry MUST NOT break the morning brief or
    the inbox watcher.
    """
    tenant = tenant_id or _resolve_tenant()
    if not tenant:
        # Pre-cutover or unconfigured environment. No-op.
        return

    if event_type not in VALID_EVENT_TYPES:
        logger.warning(
            "emit_event_unknown_type",
            extra={
                "event": "emit_event_unknown_type",
                "event_type": event_type,
                "source": source,
            },
        )

    payload_json = json.dumps(metadata or {}, default=str)

    try:
        # Lazy import — keeps module-load cheap for callers that no-op on
        # empty PATTON_TENANT_ID.
        from home_builder_agent.integrations.postgres import connection
        with connection(application_name="hb-telemetry") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO platform.event
                      (tenant_id, source, event_type, actor_id,
                       subject_type, subject_id, metadata)
                    VALUES
                      (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        tenant,
                        source,
                        event_type,
                        actor_id,
                        subject_type,
                        subject_id,
                        payload_json,
                    ),
                )
    except Exception as e:
        logger.warning(
            "emit_event_failed",
            extra={
                "event": "emit_event_failed",
                "event_type": event_type,
                "source": source,
                "exception_type": type(e).__name__,
                "message": str(e)[:200],
            },
        )
