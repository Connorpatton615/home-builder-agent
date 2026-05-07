"""profile_agent.py — hb-profile: build a Chad preference profile from existing signals.

CLI:
  hb-profile                                 Build profile, print proposed JSON (dry-run)
  hb-profile --days 30                       Use last 30 days of signals (default 14)
  hb-profile --user <uuid>                   Build for one user (default: all engine_activity rows)
  hb-profile --save                          Save proposed profile to local JSON file
                                             (~/.hb-profile-proposed.json) until migration 004
                                             is applied to Supabase, then we flip to UPSERT
                                             into home_builder.user_profile.

Why this exists (stub stage):
  Migration 004 (user_signal + user_profile tables) is drafted but NOT yet
  cut to Supabase — it's awaiting CTO review of Q-A through Q-G in
  docs/specs/migration_004_review.md. Even pre-cut, we already have rich
  signal sources sitting unused:
    - home_builder.engine_activity     (Claude actions Chad triggered)
    - .inbox_watcher_state.json        (Gmail classifications + counts)
    - Drive recent file activity       (what Chad's been editing)
    - Project list + status            (which projects exist, target dates)

  This agent reads those, prompts Sonnet to synthesize an HBUserProfileV1
  JSON, validates with Pydantic, prints. Once 004 lands the only delta is
  the final write — flip the local-file save to a DB UPSERT.

  Lets the home-builder track ship the personalization pipeline end-to-end
  in advance of iOS instrumentation. When iOS instrumentation arrives,
  the profile-builder gets richer signals automatically (it'll start
  reading home_builder.user_signal too).

Cost: ~$0.05/run (Sonnet, ~5k input + 1k output for the profile JSON).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from home_builder_agent.config import WRITER_MODEL
from home_builder_agent.core.claude_client import make_client, sonnet_cost
from home_builder_agent.scheduling.schemas import HBUserProfileV1
from home_builder_agent.scheduling.store_postgres import load_recent_engine_activity


# ---------------------------------------------------------------------------
# Signal loaders — pull from sources we already have
# ---------------------------------------------------------------------------

def load_engine_activity_signals(actor_user_id: str | None, days: int) -> list[dict]:
    """Load recent engine_activity rows. None actor_user_id = all rows."""
    hours = days * 24
    return load_recent_engine_activity(
        actor_user_id=actor_user_id,
        since_hours=hours,
        limit=500,
    )


def load_inbox_watcher_signals() -> dict | None:
    """Read the inbox watcher's accumulated classification state if present."""
    state_path = Path.home() / "Projects" / "home-builder-agent" / ".inbox_watcher_state.json"
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text())
    except Exception:
        return None


def load_drive_activity_signals() -> list[dict]:
    """Recent Drive files Chad has touched. Best-effort — return [] on failure
    so the profile still builds even if Drive auth glitches."""
    try:
        from home_builder_agent.core.auth import get_credentials
        from home_builder_agent.integrations.drive import drive_service
        creds = get_credentials()
        ds = drive_service(creds)
        # 30 most recent files modified by anyone with access
        result = ds.files().list(
            pageSize=30,
            orderBy="modifiedTime desc",
            fields="files(id, name, mimeType, modifiedTime, lastModifyingUser/emailAddress)",
            q="trashed = false",
        ).execute()
        return result.get("files", [])
    except Exception as e:
        print(f"  WARNING: Drive activity load failed: {e}", file=sys.stderr)
        return []


def load_project_signals() -> list[dict]:
    """Active projects from Postgres — feeds attention_weights inference."""
    try:
        from home_builder_agent.integrations.postgres import connection
        with connection(application_name="hb-profile-read") as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id::text AS id, name, status, target_completion_date
                    FROM home_builder.project
                    ORDER BY created_at DESC
                """)
                return list(cur.fetchall())
    except Exception as e:
        print(f"  WARNING: Project list load failed: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Claude prompt — synthesize the profile
# ---------------------------------------------------------------------------

PROFILE_BUILDER_SYSTEM = """You are the profile-builder agent for the Patton AI home-builder ecosystem.

Your job: read the operator's recent signals (autonomous AI actions taken
on his behalf, recent inbox classifications, Drive activity, project
list) and synthesize a structured preference profile that other agents
will consume to personalize their behavior.

The operator is Chad — a luxury custom home builder in Baldwin County,
Alabama. Most signals reflect his job context: subcontractors, schedules,
permits, materials, money. He's a busy GC, not a software user.

OUTPUT: a single JSON object matching the HBUserProfileV1 schema below.
No prose, no markdown fences — just the JSON object.

GUIDANCE:
- Only fill fields you have real signal for. Leave others null/empty.
- Be conservative. Prefer "I don't know yet" to a confident bad guess.
- vocabulary.preferred_terms: short phrases the operator actually uses
  (extracted from his NL inputs in engine_activity.user_intent).
- working_hours: infer from `created_at` timestamps on his actions.
- attention_weights: distribute 0..1 across project_ids by frequency
  of mention in user_intent or by project recency.
- decision_patterns.common_vendors: extract from receipt logs / change
  orders ("Lowes for framing materials" → {framing_materials: "Lowes"}).
- ignored_alert_types: leave empty for v0 (we don't have notification
  signals yet — iOS will instrument this).
- answer_style: only set fields where the action history shows a clear
  pattern (e.g. mostly short prompts → length_preference="short").

If signals are too thin to determine ANY field, return:
  {"version": 1}
That's a valid profile too — the system uses defaults.
"""


PROFILE_BUILDER_PROMPT_TEMPLATE = """Build the v1 preference profile from these signals.

==============================================================
ENGINE ACTIVITY (autonomous AI actions Chad triggered, last {days} days)
==============================================================
{activity_block}

==============================================================
PROJECTS (active in Postgres)
==============================================================
{project_block}

==============================================================
INBOX WATCHER STATE
==============================================================
{inbox_block}

==============================================================
DRIVE RECENT FILES (last 30 modified)
==============================================================
{drive_block}

==============================================================
SCHEMA — HBUserProfileV1 (target output shape)
==============================================================
{schema_block}

Return ONLY the JSON object. No markdown, no explanation, no code fences.
"""


# ---------------------------------------------------------------------------
# Signal rendering helpers (compact text, easier on Claude than raw JSON)
# ---------------------------------------------------------------------------

def _render_activity_block(rows: list[dict]) -> str:
    if not rows:
        return "(no engine_activity rows in window — Chad hasn't used hb-router recently)"
    lines = [f"({len(rows)} rows)"]
    for r in rows:
        ts = (r.get("created_at") or "")[:16].replace("T", " ")
        cmd = r.get("classified_command_type") or "?"
        outcome = r.get("outcome") or "?"
        intent = (r.get("user_intent") or "").replace("\n", " ")[:120]
        cost = r.get("cost_usd") or 0.0
        lines.append(f"  [{ts}] {cmd:<22} {outcome:<8} ${cost:.4f}  intent: {intent}")
    return "\n".join(lines)


def _render_project_block(rows: list[dict]) -> str:
    if not rows:
        return "(no projects in Postgres yet)"
    lines = [f"({len(rows)} projects)"]
    for r in rows:
        target = r.get("target_completion_date")
        target_str = target.isoformat() if hasattr(target, "isoformat") else str(target or "—")
        lines.append(f"  {r['id'][:8]} | {r['name']:<40} | status={r.get('status', '?')} | target={target_str}")
    return "\n".join(lines)


def _render_inbox_block(state: dict | None) -> str:
    if not state:
        return "(no inbox watcher state file present)"
    # The watcher tracks last_history_id + classification counts. Render
    # whatever's there compactly.
    lines = []
    for k, v in state.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            lines.append(f"  {k}: {v}")
        else:
            # Nested objects — json-dump them
            lines.append(f"  {k}: {json.dumps(v)[:200]}")
    return "\n".join(lines) if lines else "(state file present but empty)"


def _render_drive_block(files: list[dict]) -> str:
    if not files:
        return "(no Drive activity available)"
    lines = [f"({len(files)} recent files)"]
    for f in files[:30]:
        mt = f.get("modifiedTime", "")[:16].replace("T", " ")
        mime = f.get("mimeType", "").rsplit(".", 1)[-1] or "?"
        modifier = (f.get("lastModifyingUser") or {}).get("emailAddress", "?")
        lines.append(f"  [{mt}] {mime:<12} | {f.get('name', '?')[:50]} | by {modifier}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Build + validate
# ---------------------------------------------------------------------------

def build_profile(
    actor_user_id: str | None,
    days: int,
) -> tuple[HBUserProfileV1, dict]:
    """Build a v1 profile + return cost/duration metadata.

    Returns (profile, meta) where meta = {cost_usd, duration_ms,
    input_tokens, output_tokens, signal_counts}.
    """
    print(f"Loading signals (window: {days} days, user: {actor_user_id or 'ALL'})...")
    activity = load_engine_activity_signals(actor_user_id, days)
    inbox = load_inbox_watcher_signals()
    drive_files = load_drive_activity_signals()
    projects = load_project_signals()

    signal_counts = {
        "engine_activity_rows": len(activity),
        "inbox_state_present": inbox is not None,
        "drive_recent_files": len(drive_files),
        "projects": len(projects),
    }
    print(f"  signals: {signal_counts}")

    schema_block = json.dumps(
        HBUserProfileV1.model_json_schema(), indent=2,
    )[:3000]  # truncate; the schema is bigger than Claude needs to see exhaustively

    prompt = PROFILE_BUILDER_PROMPT_TEMPLATE.format(
        days=days,
        activity_block=_render_activity_block(activity),
        project_block=_render_project_block(projects),
        inbox_block=_render_inbox_block(inbox),
        drive_block=_render_drive_block(drive_files),
        schema_block=schema_block,
    )

    print(f"\nCalling Sonnet to synthesize profile...")
    started = datetime.utcnow()
    client = make_client()
    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=2000,
        system=PROFILE_BUILDER_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    duration_ms = int((datetime.utcnow() - started).total_seconds() * 1000)

    raw_text = response.content[0].text.strip()
    # Strip code fences if Claude added them despite instructions
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text
        if raw_text.endswith("```"):
            raw_text = raw_text.rsplit("```", 1)[0]
        raw_text = raw_text.strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"\nERROR: Sonnet returned non-JSON: {e}", file=sys.stderr)
        print(f"Raw response:\n{raw_text[:1000]}", file=sys.stderr)
        sys.exit(1)

    profile = HBUserProfileV1.model_validate(parsed)

    cost = sonnet_cost(response.usage)

    meta = {
        "cost_usd": cost,
        "duration_ms": duration_ms,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "signal_counts": signal_counts,
    }
    return profile, meta


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

LOCAL_SAVE_PATH = Path.home() / ".hb-profile-proposed.json"


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Build a Chad preference profile from existing signals. "
            "Pre-migration-004 stub: prints proposed profile JSON. "
            "After 004 lands, flip the save target to home_builder.user_profile."
        ),
    )
    parser.add_argument(
        "--days", type=int, default=14,
        help="Days of engine_activity history to read (default: 14).",
    )
    parser.add_argument(
        "--user", type=str, default=None,
        help="UUID of the actor_user_id to filter to. Default: ALL rows "
             "(useful pre-iOS when there's effectively one user).",
    )
    parser.add_argument(
        "--save", action="store_true",
        help="Save the proposed profile to ~/.hb-profile-proposed.json. "
             "Pre-migration-004 fallback — once the table exists, the save "
             "target flips to home_builder.user_profile UPSERT.",
    )
    args = parser.parse_args()

    profile, meta = build_profile(args.user, args.days)

    print()
    print("=" * 60)
    print("PROPOSED PROFILE (HBUserProfileV1)")
    print("=" * 60)
    print(json.dumps(profile.model_dump(mode="json"), indent=2))
    print()
    print("=" * 60)
    print(f"  Model:           {WRITER_MODEL}")
    print(f"  Cost:            ${meta['cost_usd']:.4f}")
    print(f"  Tokens:          {meta['input_tokens']} in / {meta['output_tokens']} out")
    print(f"  Duration:        {meta['duration_ms']}ms")
    print(f"  Signals seen:    {meta['signal_counts']}")
    print("=" * 60)

    if args.save:
        LOCAL_SAVE_PATH.write_text(
            json.dumps(profile.model_dump(mode="json"), indent=2) + "\n"
        )
        print(f"\nSaved to {LOCAL_SAVE_PATH}")
        print("(post-004: this will UPSERT to home_builder.user_profile instead)")


if __name__ == "__main__":
    main()
