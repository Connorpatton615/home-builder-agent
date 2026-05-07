"""chad_context.py — context loaders for the Chad Agent system prompt.

Step 2 of the Chad Agent build path (docs/specs/chad-agent.md § Build order).
Pulls compact JSON snapshots from Postgres + the local profile stub that
the future hb-chad agent embeds in its system prompt to ground every
conversation turn in current state.

Two public entry points:

  load_recent_activity(hours, actor, limit) → list[dict]
      Compact engine_activity rolling window. Each row trimmed to
      timestamp / command / outcome / summary — strips verbose fields
      (parameters JSONB, error_message stack, internal IDs) that bloat
      the prompt without informing Chad's decisions.

  get_chad_context(actor_user_id, ...) → ChadContext
      One-shot composer: active projects + recent activity + profile.
      Returns a dataclass with .to_dict() for prompt embedding and
      .to_prompt_block() for human-readable inspection.

Defensive behavior: if Postgres is unreachable or the profile file
doesn't exist, the context still composes — empty fields rather than
raising. The agent above can decide whether the missing data is
actionable; the loader doesn't gate the conversation.

Used by:
  • hb-chad (planned, step 3 of build path) — system-prompt grounding
  • Future debugging surfaces (`hb-chad --inspect-context`)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from home_builder_agent.scheduling.store_postgres import (
    load_active_projects as _load_active_projects,
    load_recent_engine_activity,
)


# ---------------------------------------------------------------------------
# Compact shapes
# ---------------------------------------------------------------------------

@dataclass
class ProjectSummary:
    """Compact projection of home_builder.project for prompt embedding."""

    id: str
    name: str
    status: str
    customer_name: str | None = None
    target_completion_date: str | None = None
    target_framing_start_date: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class ChadContext:
    """The compact snapshot the Chad Agent embeds in its system prompt.

    Three parts:
      • active_projects   — what's in flight (what Chad is responsible for now)
      • recent_activity   — what's happened lately (what Chad already knows)
      • profile_summary   — Chad's preferences (how Chad would decide)

    All optional. A None or empty value means "data not available right
    now"; the agent above interprets accordingly.
    """

    generated_at: str
    actor_user_id: str | None
    active_projects: list[ProjectSummary] = field(default_factory=list)
    recent_activity: list[dict] = field(default_factory=list)
    profile_summary: dict | None = None

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "actor_user_id": self.actor_user_id,
            "active_projects": [p.to_dict() for p in self.active_projects],
            "recent_activity": self.recent_activity,
            "profile_summary": self.profile_summary,
        }

    def to_prompt_block(self) -> str:
        """Render as plain-text for prompt embedding or human inspection.

        The agent can use either form — this text rendering is for places
        where a JSON dump would feel jarring (system prompt prose, debug
        terminal output)."""
        lines = [f"=== CONTEXT (generated {self.generated_at}) ==="]

        if self.active_projects:
            lines.append(f"\nACTIVE PROJECTS ({len(self.active_projects)}):")
            for p in self.active_projects:
                bits = [p.name, p.status]
                # Skip placeholder customer_names ("TBD", "?", "Smoke Test...")
                if p.customer_name and p.customer_name not in ("TBD", "?", ""):
                    if not p.customer_name.lower().startswith("smoke test"):
                        bits.append(p.customer_name)
                if p.target_completion_date:
                    bits.append(f"target {p.target_completion_date}")
                lines.append(f"  • {' — '.join(bits)}")
        else:
            lines.append("\nACTIVE PROJECTS: (none)")

        if self.recent_activity:
            lines.append(f"\nRECENT ACTIVITY (last window, {len(self.recent_activity)} actions):")
            for a in self.recent_activity:
                ts = (a.get("ts") or "")[:16]  # "2026-05-07T15:48"
                cmd = a.get("command") or "?"
                outcome = a.get("outcome") or "?"
                summary = (a.get("summary") or "").strip()
                line = f"  • {ts}  {cmd:<20} {outcome:<6}"
                if summary:
                    line += f"  {summary}"
                lines.append(line)
        else:
            lines.append("\nRECENT ACTIVITY: (none in window)")

        if self.profile_summary:
            lines.append("\nCHAD'S PROFILE:")
            for k, v in self.profile_summary.items():
                if isinstance(v, (str, int, float, bool)):
                    lines.append(f"  • {k}: {v}")
                elif isinstance(v, list) and v:
                    lines.append(f"  • {k}: {', '.join(str(x) for x in v[:5])}"
                                 f"{' …' if len(v) > 5 else ''}")
        else:
            lines.append("\nCHAD'S PROFILE: (not yet built)")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_recent_activity(
    hours: int = 24,
    actor_user_id: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Compact engine_activity rolling window for prompt embedding.

    Trimmed shape: ts, command, outcome, summary. Drops verbose fields
    (parameters, error_message, internal IDs) that the agent doesn't
    need in its system prompt.

    Defensive: returns [] if the engine_activity table is unreachable.
    """
    try:
        rows = load_recent_engine_activity(
            actor_user_id=actor_user_id,
            since_hours=hours,
            limit=limit,
        )
    except Exception:
        return []

    out = []
    for r in rows:
        ts = r.get("created_at")
        if hasattr(ts, "isoformat"):
            ts = ts.isoformat(timespec="seconds")
        out.append(
            {
                "ts": ts,
                "command": r.get("classified_command_type") or r.get("invoked_agent"),
                "outcome": r.get("outcome"),
                "summary": (r.get("result_summary") or "")[:200],
            }
        )
    return out


def load_active_projects_summary(tenant_id: str | None = None) -> list[ProjectSummary]:
    """Defensive wrapper around store_postgres.load_active_projects."""
    try:
        rows = _load_active_projects(tenant_id=tenant_id)
    except Exception:
        return []

    out: list[ProjectSummary] = []
    for r in rows:
        target = r.get("target_completion_date")
        framing = r.get("target_framing_start_date")
        out.append(
            ProjectSummary(
                id=str(r.get("id")),
                name=str(r.get("name") or "?"),
                status=str(r.get("status") or "active"),
                customer_name=r.get("customer_name"),
                target_completion_date=target.isoformat() if hasattr(target, "isoformat") else (str(target) if target else None),
                target_framing_start_date=framing.isoformat() if hasattr(framing, "isoformat") else (str(framing) if framing else None),
            )
        )
    return out


def load_profile_summary(actor_user_id: str | None = None) -> dict | None:
    """Load Chad's profile summary.

    Stub stage: reads from ~/.hb-profile-proposed.json (the file
    hb-profile --save writes pre-migration-004). Once migration 004
    is cut, swap to a Postgres SELECT against home_builder.user_profile.
    """
    p = Path.home() / ".hb-profile-proposed.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None

    # If hb-profile saved a full HBUserProfileV1 record, surface only the
    # most prompt-relevant top-level fields. Otherwise return as-is.
    if isinstance(data, dict):
        compact = {k: v for k, v in data.items() if k != "raw_signals"}
        return compact
    return None


def get_chad_context(
    actor_user_id: str | None = None,
    activity_hours: int = 24,
    activity_limit: int = 20,
    tenant_id: str | None = None,
) -> ChadContext:
    """One-shot composer for hb-chad's system-prompt grounding.

    Pulls active projects, recent activity, and profile summary. Each
    sub-loader is defensive — failures yield empty fields, not raises.
    """
    return ChadContext(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        actor_user_id=actor_user_id,
        active_projects=load_active_projects_summary(tenant_id=tenant_id),
        recent_activity=load_recent_activity(
            hours=activity_hours,
            actor_user_id=actor_user_id,
            limit=activity_limit,
        ),
        profile_summary=load_profile_summary(actor_user_id=actor_user_id),
    )
