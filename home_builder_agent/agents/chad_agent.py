"""chad_agent.py — hb-chad: the Chad Lynch master agent.

Step 3 of the Chad Agent build path (docs/specs/chad-agent.md § Build order).

This is the persona layer on top of the 21 specialists. Chad doesn't
choose a tool — he just talks. hb-chad figures out what to do, in his
voice.

Architecture (v0):
  • Opus + tool-calling
  • System prompt    = chad_voice_system("narrator") + chad_context block
  • Tools            = ask_chad (read) + dispatch_action (write)
                       — both delegate to existing specialists
  • Memory           = per-conversation (no cross-turn memory yet — step 5)
  • Channel          = terminal (step 4 wires iOS Ask tab)
  • Output           = Chad-voice prose answer + summary of actions taken

CLI:
  hb-chad "what's the status of Whitfield framing?"
  hb-chad "log a $400 receipt for Wholesale Plumbing"
  hb-chad "framing pushed a week — draft a homeowner update"
  hb-chad --json "<input>"
  hb-chad --dry-run "<input>"      → classify + plan, don't dispatch writes

What hb-chad doesn't do (yet):
  • Multi-turn conversation memory          → step 5, blocked on migration 004
  • Auto-send outbound emails               → always drafts, Chad sends
  • Channel-aware verbosity                 → step 4
  • Native voice in/out                     → handled by iOS shell layer

Cost: ~$0.05–0.30/turn depending on tool-call depth (Opus + Sonnet
specialists below). The dispatch_action tool's downstream agent has its
own cost on top.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from typing import Iterator

from anthropic import Anthropic

from home_builder_agent.core.chad_context import get_chad_context
from home_builder_agent.core.chad_voice import (
    CUSTOMER_NAME,
    chad_voice_system,
)
from home_builder_agent.core.claude_client import make_client
from home_builder_agent.core.cost_guard import check_budget, record_cost


# ---------------------------------------------------------------------------
# Model + loop config
# ---------------------------------------------------------------------------

CHAD_MODEL = "claude-opus-4-7"
CHAD_FALLBACK_MODEL = "claude-sonnet-4-6"   # fallback when Opus daily cap is hit
CHAD_MAX_TOKENS = 4096
MAX_TOOL_LOOP_ITERATIONS = 8  # safety guard

# Pricing per 1M tokens for cost reporting (claude-opus-4-7 + claude-sonnet-4-6).
# Mirrors the constants in claude_client; duplicated here so a single import
# lights up the cost line without pulling in the helper's full surface.
OPUS_INPUT_PER_M = 15.00
OPUS_OUTPUT_PER_M = 75.00
SONNET_INPUT_PER_M = 3.00
SONNET_OUTPUT_PER_M = 15.00


# ---------------------------------------------------------------------------
# Persona prompt — voice + context + role
# ---------------------------------------------------------------------------

PERSONA_SUFFIX = """

You are hb-chad, Chad Lynch's AI extension. You hold his voice, his
preferences, and his judgment. You don't operate on Chad — you operate
*as* him, or *for* him.

Your job each turn:
  1. Understand what Chad wants. Don't ask clarifying questions when
     reasonable inference works. Chad is busy.
  2. Pick the right tool — see the tool selection guide below.
  3. Compose a response in Chad-voice prose. If you took an action,
     report it concretely. If you drafted something for Chad to send,
     say so and tell him where it is (Gmail Drafts, etc.).
  4. Suggest the obvious follow-up if there is one. Don't fabricate
     follow-ups when none is needed.

Tool selection guide — pick the most specific tool, fall back to ask_chad:

  Chad asks…                         Use…
  ─────────────────────────────────  ──────────────────────────────
  "what's pending in my queue?"      list_pending_drafts
  "what does morning look like?"     read_morning_view
  "what drop-deads are coming?"      list_drop_deads
  "what's overdue?"                  list_drop_deads (bands=OVERDUE+ORDER NOW)
  "what happened overnight?"         list_overnight_events
  "what's left on Foundation?"       list_checklist_items_for_phase
  "log a site note: rain pushed…"    log_site_note  (verbatim — no rephrasing)
  "approve the Mason reply"          approve_draft_action  (after preview)
  "is the system OK?"                system_status
  "are you alive?"                   system_status
  "how much have we spent today?"    system_status
  "log a $400 receipt for X"         dispatch_action
  "create a CO for cabinet upgrade"  dispatch_action
  "push framing two weeks"           dispatch_action
  "what was framing's cost?"         ask_chad  (cross-cutting RAG)
  "find emails about windows"        ask_chad

Style rules:
  • Tight, operator prose. No hype, no AI hedging.
  • If you don't know something and the tools won't tell you, say so plainly.
  • Outbound communications drafted-only — never auto-sent until Chad
    explicitly approves (approve_draft_action or sends from Gmail himself).
  • Before approving anything, PREVIEW first via list_pending_drafts so
    you can tell Chad what's about to be sent. Don't approve sight-unseen.
  • The tools above are the only way to read truth or change state.
    Don't invent project status, costs, or schedule data.
  • When a tool returns "Postgres unavailable" or "table not present",
    surface that to Chad plainly — don't hide infrastructure issues
    behind Chad-voice fiction. He needs to know when the system is degraded.
"""


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic tool-use schema)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "ask_chad",
        "description": (
            "Query for read-only information about projects, costs, schedule, "
            "site logs, inspections, vendors, or anything else in Chad's "
            "system. Delegates to hb-ask (RAG over Drive + Postgres). Use "
            "this whenever you need a fact you don't already have in context "
            "— current state, recent activity beyond the prompt window, "
            "files Chad referenced, etc. Returns a polished answer with "
            "citations. PREFER THE STRUCTURED TOOLS BELOW (list_pending_drafts, "
            "read_morning_view, list_drop_deads, etc.) for their specific "
            "domains — they're cheaper, faster, and more accurate. Fall back "
            "to ask_chad only for cross-cutting questions or anything those "
            "tools don't cover."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "The question to answer. Phrase it naturally — "
                        "ask_chad understands NL. Be specific: include "
                        "project name if relevant."
                    ),
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "dispatch_action",
        "description": (
            "Take a state-changing action: log a receipt, update a phase, "
            "create a change order, log an inspection, log a lien waiver, "
            "etc. Delegates to hb-router which classifies the NL command, "
            "dispatches the right specialist agent, and writes the "
            "engine_activity audit row. Use this for anything that should "
            "mutate Tracker / Cost Tracker / Drive / engine state. Each "
            "call is logged. PREFER log_site_note for site log entries and "
            "approve_draft_action for queue approvals — they're more direct."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nl_command": {
                    "type": "string",
                    "description": (
                        "The action as Chad would say it, e.g. "
                        "'log a $400 receipt for Wholesale Plumbing on "
                        "Whitfield', 'push framing two weeks', 'create a "
                        "change order for the cabinet upgrade'."
                    ),
                },
                "project_id": {
                    "type": "string",
                    "description": (
                        "Optional UUID of the project this action belongs "
                        "to. Most commands self-identify; pass this only "
                        "when you've confirmed the project from context."
                    ),
                },
            },
            "required": ["nl_command"],
        },
    },
    {
        "name": "list_pending_drafts",
        "description": (
            "List drafts pending Chad's review for a project — Gmail reply "
            "drafts, change-order approvals to homeowners, weekly client "
            "updates, supplier follow-ups. The judgment queue. Each row "
            "includes the kind, the originating agent, a one-line summary, "
            "the recipient, age, and the draft_action UUID Chad needs to "
            "approve/edit/discard. Newest-first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "UUID of the project. Required.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return. Default 20.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "read_morning_view",
        "description": (
            "Return today's morning view payload for a project — Chad's "
            "coffee-cup landing surface. Includes the voice_brief paragraph, "
            "weather + risk phases, judgment queue count, today on site, "
            "today's drop-deads, overnight events, and the action items. "
            "Reads from the launchd-pre-computed cache when fresh "
            "(.morning_cache/<project_id>.json); returns clear staleness "
            "warning when stale. Use this whenever Chad asks 'what's the "
            "morning view' or 'what should I focus on today'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "UUID of the project. Required.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "list_drop_deads",
        "description": (
            "Return drop-dead order dates for a project's selections, "
            "urgency-banded (OVERDUE / ORDER NOW / THIS WEEK / UPCOMING). "
            "Use this whenever Chad asks 'what's coming up' or 'what's "
            "overdue' or 'what selections need to land'. Each row includes "
            "material category, install phase, install date, drop-dead "
            "date, lead time, and the urgency band."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "UUID of the project. Required.",
                },
                "bands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of bands to include. Default: "
                        "['OVERDUE', 'ORDER NOW', 'THIS WEEK', 'UPCOMING']. "
                        "Pass ['OVERDUE', 'ORDER NOW'] for urgent-only."
                    ),
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "list_overnight_events",
        "description": (
            "Return recent Events for a project (or system-wide) at "
            "severity ≥ warning. Use this whenever Chad asks 'what's "
            "happened overnight' or 'what's the latest' or 'any events "
            "I should know about'. Each row includes the type "
            "(eta-change, weather-delay, supplier-email-detected, etc.), "
            "severity, summary, age, and acknowledgement status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": (
                        "UUID of the project. Optional — omit for system-wide."
                    ),
                },
                "since_hours": {
                    "type": "integer",
                    "description": "Lookback window in hours. Default 14.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_checklist_items_for_phase",
        "description": (
            "Return the checklist for a specific phase on a project — "
            "items grouped by category, with photo_required flags + "
            "completion state. Use this whenever Chad asks about a phase's "
            "gates, what's left to close, what photos are still needed, "
            "or what the next step is on a particular phase."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "UUID of the project. Required.",
                },
                "phase_name": {
                    "type": "string",
                    "description": (
                        "Phase name (case-insensitive substring OK). "
                        "Examples: 'foundation', 'framing', 'precon'."
                    ),
                },
            },
            "required": ["project_id", "phase_name"],
        },
    },
    {
        "name": "log_site_note",
        "description": (
            "Append a verbatim site log entry to a project's Drive site log. "
            "Append-only legal record — Chad's words preserved exactly, no "
            "AI rephrasing. Routes through hb-router so the engine_activity "
            "audit row gets written. Use this whenever Chad says 'log a "
            "site note', 'add to the site log', or otherwise wants a "
            "verbatim entry recorded for the day."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "The site log entry text — verbatim, Chad's words. "
                        "Don't rephrase or summarize."
                    ),
                },
                "project_id": {
                    "type": "string",
                    "description": (
                        "Optional UUID. Omit if Chad named the project in "
                        "context — hb-router resolves from text."
                    ),
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "approve_draft_action",
        "description": (
            "Approve a pending draft (Gmail reply, CO approval, weekly "
            "client update, vendor follow-up). Flips the draft_action row "
            "to 'approved' and fires the per-kind confirm hook (e.g. send "
            "the Gmail draft for gmail-reply-draft kind). Use this when "
            "Chad explicitly says 'approve [draft]', 'send the Mason "
            "reply', etc. PREVIEW first via list_pending_drafts so you "
            "tell Chad what's about to be sent before doing it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "draft_action_id": {
                    "type": "string",
                    "description": (
                        "UUID of the draft_action row. Get this from "
                        "list_pending_drafts."
                    ),
                },
                "decision_notes": {
                    "type": "string",
                    "description": (
                        "Optional Chad-supplied note about the decision "
                        "(e.g., 'changed gate code from 4421 to 4435')."
                    ),
                },
            },
            "required": ["draft_action_id"],
        },
    },
    {
        "name": "system_status",
        "description": (
            "Report current system health to Chad: which background jobs "
            "are running, today's AI spend vs caps, queue depths, any "
            "stale heartbeats, recent errors. Use whenever Chad asks "
            "'is everything running?', 'are you alive?', 'what's going on "
            "with the system?', 'how much have we spent today?', 'any "
            "alerts I should know about?'. Returns a tight Chad-voice "
            "summary. Costs nothing — pure filesystem + DB reads."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_ask_chad(question: str) -> tuple[str, float]:
    """Call hb-ask's ask_question(); return (text_for_claude, cost_usd)."""
    from home_builder_agent.agents.ask_agent import ask_question

    try:
        result = ask_question(question, verbose=False)
    except Exception as e:
        return f"ask_chad failed: {type(e).__name__}: {e}", 0.0

    answer = result.get("answer", "(no answer)")
    citations = result.get("citations") or []
    cost = float(result.get("cost_usd") or 0.0)

    if citations:
        cite_str = "\n".join(
            f"  - {c.get('name', '?')} ({c.get('webViewLink', '')})"
            for c in citations[:5]
        )
        return f"{answer}\n\nCitations:\n{cite_str}", cost
    return answer, cost


def _tool_dispatch_action(
    nl_command: str,
    project_id: str | None = None,
    dry_run: bool = False,
) -> tuple[str, float]:
    """Call hb-router's route_command(); return (text_for_claude, cost_usd)."""
    from home_builder_agent.agents.router_agent import route_command

    try:
        result = route_command(
            user_intent=nl_command,
            project_id=project_id,
            surface="cli",
            dry_run=dry_run,
        )
    except Exception as e:
        return f"dispatch_action failed: {type(e).__name__}: {e}", 0.0

    cost = float(getattr(result, "cost_usd", 0.0) or 0.0)
    parts = [
        f"command_type: {result.classified_command_type}",
        f"agent: {result.invoked_agent or '(none)'}",
        f"outcome: {result.outcome}",
    ]
    if result.parameters:
        parts.append(f"parameters: {json.dumps(result.parameters)[:300]}")
    summary = getattr(result, "result_summary", "") or ""
    if summary:
        parts.append(f"summary: {summary[:300]}")
    err = getattr(result, "error_message", "") or ""
    if err:
        parts.append(f"error: {err[:300]}")
    if dry_run:
        parts.insert(0, "(dry-run — not actually dispatched)")
    return "\n".join(parts), cost


# ---------------------------------------------------------------------------
# Structured read tools (lower latency + cost than ask_chad for these domains)
# ---------------------------------------------------------------------------

# All return tuple[str, float] — (text_for_claude, cost_usd). Cost is 0.0 for
# DB-backed reads (no Claude calls). All catch exceptions defensively and
# return a clear error string rather than raising — Opus surfaces the error
# in its next turn rather than the tool loop crashing.


def _tool_list_pending_drafts(
    project_id: str,
    limit: int | None = None,
) -> tuple[str, float]:
    """List pending draft_actions for a project (judgment queue)."""
    if not project_id:
        return "list_pending_drafts requires a project_id (UUID).", 0.0
    try:
        from home_builder_agent.scheduling.store_postgres import (
            list_draft_actions_for_project,
        )
        rows = list_draft_actions_for_project(
            project_id=project_id,
            limit=limit or 20,
        )
    except Exception as e:
        msg = str(e).lower()
        if "does not exist" in msg and "draft_action" in msg:
            return (
                "Judgment queue empty: home_builder.draft_action table not yet "
                "applied to this database (migration 007). Drafting agents are "
                "still running but the queue isn't persisted yet.",
                0.0,
            )
        return f"list_pending_drafts failed: {type(e).__name__}: {e}", 0.0

    if not rows:
        return f"No pending drafts for project {project_id[:8]}…  (inbox is clear).", 0.0

    lines = [f"{len(rows)} pending drafts for project {project_id[:8]}… (newest first):"]
    for r in rows:
        age = ""
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            ca = r.get("created_at")
            if ca:
                hrs = max(1, int((now - ca).total_seconds() // 3600))
                age = f" ({hrs}h ago)"
        except Exception:
            pass
        agent = r.get("originating_agent", "?")
        kind = r.get("kind", "?")
        summary = (r.get("summary") or "")[:140]
        recipient = r.get("from_or_to") or ""
        lines.append(
            f"  • [{kind}] {summary}{age}\n"
            f"    drafted by {agent}{' · ' + recipient if recipient else ''}\n"
            f"    draft_action_id: {r['id']}"
        )
    return "\n".join(lines), 0.0


def _tool_read_morning_view(project_id: str) -> tuple[str, float]:
    """Return today's morning view payload (cache-or-empty, no synthesis)."""
    if not project_id:
        return "read_morning_view requires a project_id (UUID).", 0.0

    import os
    cache_path = os.path.normpath(
        os.path.join(
            os.path.dirname(__file__), "..", "..", ".morning_cache",
            f"{project_id}.json",
        )
    )
    if not os.path.exists(cache_path):
        return (
            f"No morning-view cache for project {project_id[:8]}…\n"
            f"Expected at: {cache_path}\n"
            "The 6:05 AM launchd job (com.chadhomes.morning-view) hasn't "
            "produced a cache for this project yet. To force a fresh "
            "compute, dispatch_action with 'compute morning view for "
            f"<project>' or run hb-morning <project> --cache from terminal.",
            0.0,
        )

    try:
        with open(cache_path) as f:
            payload = json.load(f)
        cache_mtime = os.path.getmtime(cache_path)
        from datetime import datetime, timezone
        age_hours = max(0, int((datetime.now().timestamp() - cache_mtime) // 3600))
    except Exception as e:
        return f"read_morning_view failed reading cache: {type(e).__name__}: {e}", 0.0

    project_name = payload.get("project_name", "?")
    as_of = payload.get("as_of_local_date", "?")

    lines = [f"Morning view for {project_name} (as_of={as_of}, cache age {age_hours}h):"]
    if age_hours > 24:
        lines.append(
            "  ⚠️  STALE: cache is older than 24h — voice_brief and action_items "
            "may not reflect today's reality. Surface the staleness to Chad."
        )

    weather = payload.get("weather")
    if weather:
        lines.append(f"\n  Weather:")
        lines.append(f"    Today:    {weather.get('summary_today', '?')}")
        if weather.get("summary_tomorrow"):
            lines.append(f"    Tomorrow: {weather['summary_tomorrow']}")
        for r in weather.get("risk_phases", []):
            lines.append(
                f"    ⚠️  {r.get('phase_name','?')}: {r.get('detail','')} "
                f"(severity={r.get('severity','?')})"
            )

    vb = payload.get("voice_brief")
    if vb and vb.get("text"):
        lines.append(f"\n  Voice brief:")
        lines.append(f"    {vb['text']}")

    queue = payload.get("judgment_queue", {})
    lines.append(f"\n  Judgment queue: {queue.get('count', 0)} pending")
    for it in (queue.get("items") or [])[:10]:
        lines.append(
            f"    • [{it.get('kind','?')}] {(it.get('summary') or '')[:100]} "
            f"(by {it.get('originating_agent','?')})"
        )

    today = payload.get("today_on_site", {})
    site_items = today.get("items") or []
    if site_items:
        lines.append(f"\n  Today on site ({len(site_items)}):")
        for it in site_items:
            chip = ""
            if it.get("urgency_band") and it.get("urgency_band") != "calm":
                chip = f" [{it['urgency_band'].upper()}]"
            reason = f" — {it['urgency_reason']}" if it.get("urgency_reason") else ""
            label = it.get("phase_name") or it.get("material_category") or it.get("kind", "?")
            lines.append(f"    • {label}{chip}{reason}")

    drop_deads = (payload.get("todays_drop_deads") or {}).get("items") or []
    if drop_deads:
        lines.append(f"\n  Today's drop-deads ({len(drop_deads)}):")
        for it in drop_deads:
            lines.append(
                f"    🚨 {it.get('material_category','?')} "
                f"({it.get('install_phase_name','?')}) — "
                f"drop-dead {it.get('drop_dead_date','?')}, "
                f"{it.get('lead_time_days','?')}d lead"
            )

    overnight = (payload.get("overnight_events") or {}).get("items") or []
    if overnight:
        lines.append(f"\n  Overnight events ({len(overnight)}):")
        for it in overnight[:6]:
            lines.append(
                f"    [{it.get('severity','?')}] {(it.get('summary') or '')[:120]}"
            )

    actions = payload.get("action_items") or []
    if actions:
        lines.append(f"\n  Action items ({len(actions)}):")
        for i, a in enumerate(actions, 1):
            lines.append(f"    {i}. {a}")

    return "\n".join(lines), 0.0


def _tool_list_drop_deads(
    project_id: str,
    bands: list[str] | None = None,
) -> tuple[str, float]:
    """Drop-dead order dates with urgency bands."""
    if not project_id:
        return "list_drop_deads requires a project_id (UUID).", 0.0
    if bands is None:
        bands = ["OVERDUE", "ORDER NOW", "THIS WEEK", "UPCOMING"]
    bands_upper = {b.upper().strip() for b in bands}

    try:
        from home_builder_agent.scheduling.store_postgres import (
            compose_schedule_from_db,
        )
        from home_builder_agent.scheduling.lead_times import compute_drop_dead_dates
        schedule = compose_schedule_from_db(project_id)
    except Exception as e:
        return f"list_drop_deads failed loading schedule: {type(e).__name__}: {e}", 0.0
    if schedule is None:
        return f"No schedule found for project {project_id[:8]}…  (run hb-bridge?).", 0.0

    drop_deads = compute_drop_dead_dates(schedule)
    if not drop_deads:
        return f"No drop-dead dates computed for {schedule.project_name} (no selections wired).", 0.0

    from datetime import date as _date
    today = _date.today()

    def _band(dd) -> str:
        days = (dd.drop_dead_date - today).days
        if days <= 0:
            return "OVERDUE" if days < 0 else "ORDER NOW"
        if days <= 3:
            return "ORDER NOW"
        if days <= 7:
            return "THIS WEEK"
        if days <= 30:
            return "UPCOMING"
        return "FUTURE"

    filtered = [(dd, _band(dd)) for dd in drop_deads]
    filtered = [(dd, b) for dd, b in filtered if b in bands_upper]
    filtered.sort(key=lambda x: x[0].drop_dead_date)

    if not filtered:
        return (
            f"No drop-deads for {schedule.project_name} in bands "
            f"{sorted(bands_upper)}. (Total drop-deads: {len(drop_deads)} — "
            "all in non-matching bands.)",
            0.0,
        )

    lines = [
        f"{len(filtered)} drop-deads for {schedule.project_name} "
        f"(bands: {sorted(bands_upper)}):"
    ]
    for dd, band in filtered:
        days = (dd.drop_dead_date - today).days
        when = "TODAY" if days == 0 else (f"in {days}d" if days > 0 else f"{abs(days)}d OVERDUE")
        lines.append(
            f"  [{band}] {dd.material_category} ({dd.install_phase_name}) — "
            f"drop-dead {dd.drop_dead_date} ({when}, {dd.lead_time_days}d lead, "
            f"install {dd.install_date})"
        )
    return "\n".join(lines), 0.0


def _tool_list_overnight_events(
    project_id: str | None = None,
    since_hours: int | None = None,
) -> tuple[str, float]:
    """Recent Events at severity ≥ warning."""
    since_hours = since_hours or 14
    try:
        from home_builder_agent.scheduling.store_postgres import (
            load_recent_events_for_project,
        )
        events = load_recent_events_for_project(
            project_id=project_id,
            since_hours=since_hours,
            limit=30,
        )
    except Exception as e:
        return f"list_overnight_events failed: {type(e).__name__}: {e}", 0.0

    filtered = [
        e for e in events
        if (getattr(e, "severity", "info") or "info") in ("warning", "critical", "blocking")
    ]

    if not filtered:
        scope = f"project {project_id[:8]}…" if project_id else "all projects"
        return (
            f"No events at severity ≥ warning in the last {since_hours}h "
            f"for {scope}. Quiet overnight.",
            0.0,
        )

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    scope = f"project {project_id[:8]}…" if project_id else "all projects"
    lines = [f"{len(filtered)} events (last {since_hours}h, severity≥warning, {scope}):"]
    for e in filtered:
        try:
            hrs = max(1, int((now - e.created_at).total_seconds() // 3600))
        except Exception:
            hrs = "?"
        lines.append(
            f"  [{e.severity}] {e.summary()[:140]}  "
            f"({hrs}h ago, type={e.type}, status={e.status})"
        )
    return "\n".join(lines), 0.0


def _tool_list_checklist_items_for_phase(
    project_id: str,
    phase_name: str,
) -> tuple[str, float]:
    """Return checklist items for a phase, grouped by category."""
    if not project_id or not phase_name:
        return "list_checklist_items_for_phase requires project_id + phase_name.", 0.0

    try:
        from home_builder_agent.scheduling.store_postgres import (
            load_checklists_for_project,
        )
        checklists = load_checklists_for_project(project_id)
    except Exception as e:
        return f"list_checklist_items_for_phase failed: {type(e).__name__}: {e}", 0.0

    # Match by phase name in the checklist's id (which encodes phase) or
    # via instantiate_checklist as a fallback if no DB row exists yet.
    needle = phase_name.lower().strip()
    matched = None
    for cl in checklists:
        # checklist ids look like "{project_id}:{phase_id}:checklist"
        # Try matching on a phase-name attribute if exposed; fall back to substring.
        cl_phase = getattr(cl, "phase_name", "") or ""
        if needle in cl_phase.lower():
            matched = cl
            break

    if matched is None:
        # Fall back to in-memory template instantiation so we can still answer
        # "what does the Foundation checklist look like?" before any phase is
        # actually instantiated for this project.
        from home_builder_agent.scheduling.checklists import instantiate_checklist
        matched = instantiate_checklist(
            phase_id=f"preview-{needle}",
            phase_name=phase_name,
        )
        if matched.total_count == 0:
            return (
                f"No checklist found for phase {phase_name!r} on project "
                f"{project_id[:8]}…  (and no template file matched). Phase "
                "name may be misspelled or the phase has no template.",
                0.0,
            )
        prefix_note = (
            f"(Showing TEMPLATE for {phase_name} — no instantiated checklist "
            f"on this project yet.)\n"
        )
    else:
        prefix_note = ""

    by_cat: dict[str, list] = {}
    for it in matched.items:
        by_cat.setdefault(it.category, []).append(it)

    photo_total = sum(1 for it in matched.items if it.photo_required)
    lines = [
        prefix_note +
        f"{phase_name} checklist: {matched.completed_count}/{matched.total_count} complete · "
        f"📷 {photo_total} photo-required · status={matched.status}"
    ]
    for cat, items in by_cat.items():
        cat_done = sum(1 for it in items if it.is_complete)
        cat_photo = sum(1 for it in items if it.photo_required)
        lines.append(f"\n  {cat} ({cat_done}/{len(items)} done, {cat_photo}📷)")
        for it in items[:25]:
            tick = "☑" if it.is_complete else "☐"
            cam = "📷" if it.photo_required else "  "
            lines.append(f"    {tick} {cam} {it.label[:90]}")
    return "\n".join(lines), 0.0


# ---------------------------------------------------------------------------
# Structured write tools — preserve audit trail discipline
# ---------------------------------------------------------------------------


def _tool_log_site_note(
    text: str,
    project_id: str | None = None,
    dry_run: bool = False,
) -> tuple[str, float]:
    """Append a verbatim site log entry. Routes through hb-router so the
    engine_activity audit row gets written (Rule 3)."""
    if not text or not text.strip():
        return "log_site_note requires text — Chad's verbatim words.", 0.0
    nl_command = f"log: {text.strip()}"
    return _tool_dispatch_action(
        nl_command=nl_command,
        project_id=project_id,
        dry_run=dry_run,
    )


def _tool_system_status() -> tuple[str, float]:
    """Render a Chad-voice summary of system health from the same data
    `hb-status` shows. Distills the snapshot into 6-10 lines of plain
    English: jobs running, spend vs cap, queues, alerts. No raw JSON.
    """
    try:
        from home_builder_agent.agents.status_agent import collect_snapshot
        snapshot = collect_snapshot()
    except Exception as e:
        return f"system_status failed to collect: {type(e).__name__}: {e}", 0.0

    lines: list[str] = []

    # 1. Jobs
    launchd = snapshot.get("launchd", {})
    if "_error" in launchd:
        lines.append(f"⚠️  launchd unreachable: {launchd['_error']}")
    else:
        n_jobs = len(launchd)
        bad = [
            label.replace("com.chadhomes.", "")
            for label, info in launchd.items()
            if info.get("last_exit_status") not in (0, "-", -1, None)
        ]
        if bad:
            lines.append(f"Jobs: {n_jobs} loaded; {len(bad)} with non-zero exit: {', '.join(bad)}")
        else:
            lines.append(f"Jobs: all {n_jobs} loaded and last-run clean.")

    # 2. Heartbeats — call out staleness specifically
    heartbeats = snapshot.get("heartbeats", [])
    stale = [hb["job"] for hb in heartbeats if hb.get("is_stale")]
    if stale:
        lines.append(f"⚠️  Stale heartbeats: {', '.join(stale)} — these jobs aren't beating on their schedule.")
    elif heartbeats:
        lines.append(f"Heartbeats: {len(heartbeats)} jobs all fresh.")

    # 3. Cost
    cost = snapshot.get("cost", {})
    if cost.get("log_present") and "_error" not in cost:
        opus_pct = cost.get("opus_pct", 0)
        total = cost.get("total_usd", 0)
        opus = cost.get("opus_usd", 0)
        opus_cap = cost.get("opus_cap_usd", 5.0)
        if opus_pct >= 80:
            lines.append(
                f"💸 Spend: ${total:.4f} today (${opus:.4f} Opus, "
                f"{opus_pct}% of ${opus_cap:.0f}/day cap — close to cap)."
            )
        else:
            lines.append(
                f"Spend: ${total:.4f} today (${opus:.4f} Opus, "
                f"{opus_pct}% of cap)."
            )

    # 4. Engine queues — surface actionable items only
    q = snapshot.get("engine_queues", {})
    if isinstance(q.get("pending_drafts"), int) and q["pending_drafts"] > 0:
        lines.append(f"📨 {q['pending_drafts']} drafts pending your review.")
    if isinstance(q.get("open_events_critical"), int) and q["open_events_critical"] > 0:
        lines.append(
            f"🚨 {q['open_events_critical']} critical events open "
            "(probably overdue drop-deads — check list_drop_deads)."
        )

    # 5. Caches
    caches = snapshot.get("morning_caches", [])
    stale_caches = [c for c in caches if c.get("is_stale")]
    if stale_caches:
        lines.append(f"⚠️  Morning view cache stale on {len(stale_caches)} project(s).")

    # 6. Recent errors
    errs = snapshot.get("recent_errors", {})
    if errs:
        lines.append(f"🔥 Recent stderr noise on: {', '.join(errs.keys())}.")
    elif heartbeats and not stale:
        lines.append("✨ No recent errors across jobs.")

    if not lines:
        return "Nothing's reporting state right now. System might not be set up.", 0.0

    return "\n".join(lines), 0.0


def _tool_approve_draft_action(
    draft_action_id: str,
    decision_notes: str | None = None,
) -> tuple[str, float]:
    """Flip a draft_action to 'approved' + fire the per-kind confirm hook.

    Direct adapter call (does NOT go through hb-router) — matches the iOS
    UserAction → reconcile pattern. The per-kind hook is the load-bearing
    side effect (e.g., gmail.send_draft for gmail-reply-draft kind). Hook
    failures are caught and surfaced; status flip stays applied (matches
    reconcile.py:_dispatch_draft_action_approve semantics).
    """
    if not draft_action_id:
        return "approve_draft_action requires draft_action_id.", 0.0

    try:
        from home_builder_agent.scheduling.draft_actions import DraftStatus
        from home_builder_agent.scheduling.store_postgres import (
            load_draft_action_by_id,
            update_draft_action_status,
        )
        from home_builder_agent.scheduling.reconcile import DRAFT_CONFIRM_HOOKS
    except Exception as e:
        return f"approve_draft_action failed (import): {type(e).__name__}: {e}", 0.0

    try:
        draft_row = load_draft_action_by_id(draft_action_id)
    except Exception as e:
        msg = str(e).lower()
        if "does not exist" in msg and "draft_action" in msg:
            return (
                "Cannot approve: home_builder.draft_action table not present "
                "(migration 007 pending in this DB).",
                0.0,
            )
        return f"approve_draft_action failed reading row: {type(e).__name__}: {e}", 0.0

    if draft_row is None:
        return f"Draft {draft_action_id[:8]}…  not found.", 0.0

    if draft_row.get("status") != "pending":
        return (
            f"Draft {draft_action_id[:8]}…  is already in state "
            f"{draft_row.get('status')!r}; cannot approve. Pull the queue "
            "again with list_pending_drafts to see what's actually pending.",
            0.0,
        )

    try:
        ok = update_draft_action_status(
            draft_action_id=draft_action_id,
            new_status=DraftStatus.APPROVED,
            decision_notes=decision_notes,
        )
    except Exception as e:
        return f"approve_draft_action failed updating status: {type(e).__name__}: {e}", 0.0

    if not ok:
        return (
            f"Draft {draft_action_id[:8]}…  status flip rejected (race? "
            "row may have been decided in another tab).",
            0.0,
        )

    # Fire per-kind confirm hook. Best-effort — status is already approved.
    kind = draft_row.get("kind", "?")
    hook = DRAFT_CONFIRM_HOOKS.get(kind)
    hook_note = ""
    if hook is None:
        hook_note = (
            f" (kind={kind!r}: no confirm hook registered yet — manual send "
            "required; the underlying Gmail draft is still in Drafts)"
        )
    else:
        try:
            # Fabricate the action shape the hook expects (matches the
            # reconcile dispatcher's interface).
            fake_action = {
                "id": "hb-chad-direct-approve",
                "actor_user_id": None,
                "target_entity_type": "draft-action",
                "target_entity_id": draft_action_id,
                "payload": {"decision_notes": decision_notes},
                "synced_at": None,
            }
            hook(fake_action, draft_row, None)
            hook_note = f" (kind={kind!r} confirm hook fired)"
        except Exception as e:
            hook_note = (
                f" — confirm hook failed: {type(e).__name__}: {e} "
                "(status is approved; manual recovery may be needed)"
            )

    summary = (draft_row.get("summary") or "")[:100]
    return (
        f"Approved draft_action {draft_action_id[:8]}…\n"
        f"  kind: {kind}\n"
        f"  summary: {summary}\n"
        f"  result:{hook_note}",
        0.0,
    )


# ---------------------------------------------------------------------------
# The agent loop
# ---------------------------------------------------------------------------

def chad_turn(user_input: str, *, dry_run: bool = False, verbose: bool = False) -> dict:
    """Run one Chad-agent turn. Returns a dict shaped for CLI / API use.

    Shape on success:
      {
        "input":         "<original user input>",
        "answer":        "<Chad-voice prose>",
        "actions_taken": [ ... summary of dispatch_action calls ... ],
        "tool_log":      [ {name, input, duration_ms, cost} ... ],
        "model":         "claude-opus-4-7",
        "cost_usd":      0.123,           # opus turns + downstream specialists
        "duration_ms":   2345,
      }

    Shape on Anthropic API failure (auth / rate-limit / connection) — the
    function does NOT raise; it returns a structured error dict so any
    caller (CLI, future direct backend callers, etc.) gets a graceful
    response rather than a stack trace:
      {
        "input":      "<original user input>",
        "answer":     "<short Chad-voice fallback message>",
        "error":      {"type": "AuthenticationError", "message": "..."},
        "model":      "claude-opus-4-7",
        "cost_usd":   0.0,
        "duration_ms": 12,
        "actions_taken": [],
        "tool_log":   [],
        "iterations": 0,
      }

    The iOS Ask tab uses chad_turn_stream() which has its own SSE-style
    error handling; this wrapper is for the synchronous path (terminal
    hb-chad, future backend callers).
    """
    started_at = time.time()

    # Imports are lazy so we surface auth errors cleanly even if the env
    # is missing the API key entirely.
    from anthropic import (
        APIConnectionError,
        APIError,
        AuthenticationError,
        BadRequestError,
        RateLimitError,
    )

    # Daily-spend circuit breaker with TIER FALLBACK.
    # If Opus cap is hit but the total cap isn't, fall back to Sonnet
    # automatically — Chad still gets an answer, just on the cheaper
    # tier. Only refuse the turn entirely if BOTH caps are hit.
    active_model = CHAD_MODEL
    fallback_used = False
    fallback_reason = ""
    allowed_opus, reason_opus = check_budget("opus")
    if not allowed_opus:
        # Opus cap hit. Check if the total cap allows a Sonnet turn.
        allowed_total, reason_total = check_budget("sonnet")
        if allowed_total:
            active_model = CHAD_FALLBACK_MODEL
            fallback_used = True
            fallback_reason = reason_opus
        else:
            # Both caps hit. Refuse the turn entirely.
            return {
                "input": user_input,
                "answer": (
                    f"I'm holding off this turn — {reason_total} "
                    "Both caps are tapped today. Connor will see this in "
                    "the structured log and can raise the caps or let "
                    "things cool down until tomorrow."
                ),
                "error": {"type": "DailyBudgetExceeded", "message": reason_total},
                "actions_taken": [],
                "tool_log": [],
                "model": CHAD_MODEL,
                "cost_usd": 0.0,
                "opus_cost_usd": 0.0,
                "downstream_cost_usd": 0.0,
                "duration_ms": int((time.time() - started_at) * 1000),
                "iterations": 0,
            }

    try:
        client: Anthropic = make_client()
    except Exception as e:
        return {
            "input": user_input,
            "answer": (
                "I can't reach Anthropic right now — likely a missing or "
                f"invalid API key ({type(e).__name__}). Connor needs to "
                "check ANTHROPIC_API_KEY in .env before I can answer."
            ),
            "error": {"type": type(e).__name__, "message": str(e)[:500]},
            "actions_taken": [],
            "tool_log": [],
            "model": CHAD_MODEL,
            "cost_usd": 0.0,
            "opus_cost_usd": 0.0,
            "downstream_cost_usd": 0.0,
            "duration_ms": int((time.time() - started_at) * 1000),
            "iterations": 0,
        }

    voice = chad_voice_system("narrator")
    ctx = get_chad_context().to_prompt_block()
    system = f"{voice}\n\n{ctx}\n{PERSONA_SUFFIX}\nTODAY: {date.today().strftime('%A, %B %-d, %Y')}"

    messages: list[dict] = [{"role": "user", "content": user_input}]
    final_text = ""
    tool_log: list[dict] = []
    actions_taken: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    downstream_cost = 0.0
    iteration = 0  # initialize so the error path can reference it before the loop

    try:
        for iteration in range(MAX_TOOL_LOOP_ITERATIONS):
            if verbose:
                print(f"[iter {iteration + 1}] calling {active_model}...", file=sys.stderr)

            response = client.messages.create(
                model=active_model,
                max_tokens=CHAD_MAX_TOKENS,
                system=system,
                tools=TOOLS,
                messages=messages,
            )

            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if block.type == "text":
                        final_text += block.text
                break

            if response.stop_reason != "tool_use":
                # Unexpected — capture what we have and stop
                for block in response.content:
                    if block.type == "text":
                        final_text += block.text
                break

            # Execute each requested tool, append results, loop
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_started = time.time()
                name = block.name
                inputs = block.input or {}

                if name == "ask_chad":
                    output, cost = _tool_ask_chad(inputs.get("question", ""))
                elif name == "dispatch_action":
                    output, cost = _tool_dispatch_action(
                        inputs.get("nl_command", ""),
                        inputs.get("project_id"),
                        dry_run=dry_run,
                    )
                    if not dry_run:
                        actions_taken.append(inputs.get("nl_command", ""))
                elif name == "list_pending_drafts":
                    output, cost = _tool_list_pending_drafts(
                        inputs.get("project_id", ""),
                        inputs.get("limit"),
                    )
                elif name == "read_morning_view":
                    output, cost = _tool_read_morning_view(inputs.get("project_id", ""))
                elif name == "list_drop_deads":
                    output, cost = _tool_list_drop_deads(
                        inputs.get("project_id", ""),
                        inputs.get("bands"),
                    )
                elif name == "list_overnight_events":
                    output, cost = _tool_list_overnight_events(
                        inputs.get("project_id"),
                        inputs.get("since_hours"),
                    )
                elif name == "list_checklist_items_for_phase":
                    output, cost = _tool_list_checklist_items_for_phase(
                        inputs.get("project_id", ""),
                        inputs.get("phase_name", ""),
                    )
                elif name == "log_site_note":
                    output, cost = _tool_log_site_note(
                        inputs.get("text", ""),
                        inputs.get("project_id"),
                        dry_run=dry_run,
                    )
                    if not dry_run:
                        actions_taken.append(f"site-log: {inputs.get('text', '')[:80]}")
                elif name == "approve_draft_action":
                    if dry_run:
                        output = (
                            "(dry-run) Would approve draft "
                            f"{inputs.get('draft_action_id', '')[:8]}…  + fire confirm hook"
                        )
                        cost = 0.0
                    else:
                        output, cost = _tool_approve_draft_action(
                            inputs.get("draft_action_id", ""),
                            inputs.get("decision_notes"),
                        )
                        actions_taken.append(
                            f"approved draft {inputs.get('draft_action_id', '')[:8]}…"
                        )
                elif name == "system_status":
                    output, cost = _tool_system_status()
                else:
                    output = f"Unknown tool: {name}"
                    cost = 0.0

                duration_ms = int((time.time() - tool_started) * 1000)
                downstream_cost += cost
                tool_log.append({
                    "name": name,
                    "input": inputs,
                    "duration_ms": duration_ms,
                    "cost_usd": cost,
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
                if verbose:
                    print(f"  → {name} ({duration_ms}ms, ${cost:.4f})", file=sys.stderr)

            messages.append({"role": "user", "content": tool_results})

        # Compute the Opus-or-Sonnet cost based on which tier we actually used.
        if active_model == CHAD_MODEL:
            model_cost = (
                total_input_tokens * OPUS_INPUT_PER_M / 1_000_000
                + total_output_tokens * OPUS_OUTPUT_PER_M / 1_000_000
            )
        else:
            model_cost = (
                total_input_tokens * SONNET_INPUT_PER_M / 1_000_000
                + total_output_tokens * SONNET_OUTPUT_PER_M / 1_000_000
            )
        duration_ms = int((time.time() - started_at) * 1000)

        # Record per-turn cost to .cost_log.jsonl for circuit-breaker
        # accounting + retrospective audit. Tier tracked separately so
        # daily caps differentiate Opus vs Sonnet.
        if model_cost > 0:
            record_cost(
                agent="hb-chad",
                model=active_model,
                cost_usd=model_cost,
                note=(
                    f"chad_turn iter={iteration + 1}"
                    + (" (fallback to Sonnet — Opus cap hit)" if fallback_used else "")
                ),
            )

        # If we fell back, prepend a one-line note so Chad knows the
        # turn ran on the cheaper tier. Connor sees the same in
        # structured logs via .cost_log.jsonl.
        answer = final_text.strip()
        if fallback_used and answer:
            answer = (
                "(running on Sonnet today — Opus cap reached. Same tools, "
                "slightly less reasoning depth.)\n\n" + answer
            )

        return {
            "input": user_input,
            "answer": answer,
            "actions_taken": actions_taken,
            "tool_log": tool_log,
            "model": active_model,
            "model_fallback_used": fallback_used,
            "fallback_reason": fallback_reason if fallback_used else None,
            "cost_usd": round(model_cost + downstream_cost, 4),
            "opus_cost_usd": round(model_cost, 4) if active_model == CHAD_MODEL else 0.0,
            "sonnet_cost_usd": round(model_cost, 4) if active_model == CHAD_FALLBACK_MODEL else 0.0,
            "downstream_cost_usd": round(downstream_cost, 4),
            "duration_ms": duration_ms,
            "iterations": iteration + 1,
        }

    except (AuthenticationError, RateLimitError, APIConnectionError, BadRequestError, APIError) as e:
        # Anthropic API errors mid-loop — return graceful error dict.
        # iOS Ask tab uses chad_turn_stream() which has its own SSE error
        # path; this is for the synchronous CLI + future direct callers.
        if active_model == CHAD_MODEL:
            model_cost = (
                total_input_tokens * OPUS_INPUT_PER_M / 1_000_000
                + total_output_tokens * OPUS_OUTPUT_PER_M / 1_000_000
            )
        else:
            model_cost = (
                total_input_tokens * SONNET_INPUT_PER_M / 1_000_000
                + total_output_tokens * SONNET_OUTPUT_PER_M / 1_000_000
            )
        return {
            "input": user_input,
            "answer": (
                f"I hit an Anthropic API error mid-thought "
                f"({type(e).__name__}). Connor: check the key / rate "
                "limits / network and try again. "
                f"Already-completed tool calls in this turn: {len(tool_log)}."
            ),
            "error": {"type": type(e).__name__, "message": str(e)[:500]},
            "actions_taken": actions_taken,
            "tool_log": tool_log,
            "model": active_model,
            "model_fallback_used": fallback_used,
            "fallback_reason": fallback_reason if fallback_used else None,
            "cost_usd": round(model_cost + downstream_cost, 4),
            "opus_cost_usd": round(model_cost, 4) if active_model == CHAD_MODEL else 0.0,
            "sonnet_cost_usd": round(model_cost, 4) if active_model == CHAD_FALLBACK_MODEL else 0.0,
            "downstream_cost_usd": round(downstream_cost, 4),
            "duration_ms": int((time.time() - started_at) * 1000),
            "iterations": iteration + 1,
        }


# ---------------------------------------------------------------------------
# Streaming agent loop
# ---------------------------------------------------------------------------
# SSE event contract — same shape as ask_question_stream so iOS can swap
# between hb-ask and hb-chad without changing its event handler. The route
# handler in patton-ai-ios serializes (event_id, event_type, payload) tuples
# to wire format; the engine emits them.
#
# Event types:
#   text_delta       — incremental Opus tokens for the final answer
#   tool_use         — Opus invokes ask_chad or dispatch_action
#   tool_result      — tool returned (one-line summary + duration)
#   citation_added   — a Drive file referenced by the underlying ask_chad
#                      pass (forwarded from the nested ask_question result)
#   message_complete — terminal: full answer + citations + cost + tools_called
#   error            — terminal: error envelope


def chad_turn_stream(
    user_input: str,
    *,
    verbose: bool = False,
) -> Iterator[tuple[int, str, dict]]:
    """Streaming version of chad_turn for the iOS Ask tab and other SSE surfaces.

    Yields (event_id, event_type, payload) tuples. Event IDs are monotonic
    per stream, starting at 1. Stream is terminated by either a
    `message_complete` or an `error` event — caller breaks out of iteration
    after either.

    Wire-compatible with ask_question_stream: the iOS backend can call
    either and the same SSE event handler works.

    Note: the underlying ask_chad tool currently calls ask_question (the
    non-streaming variant). Citations land in a single batch when ask_chad
    returns rather than incrementally during its sub-call. That's a
    deliberate v1.0 simplification — the iOS user still sees citation
    events as discrete SSE messages.
    """
    started_at = time.time()
    event_id_counter = 0

    def emit(event_type: str, payload: dict) -> tuple[int, str, dict]:
        nonlocal event_id_counter
        event_id_counter += 1
        return (event_id_counter, event_type, payload)

    # Setup — kept inline so setup failures surface as error events rather
    # than raising before the stream opens.
    try:
        client = make_client()
        voice = chad_voice_system("narrator")
        ctx_block = get_chad_context().to_prompt_block()
        system = (
            f"{voice}\n\n{ctx_block}\n{PERSONA_SUFFIX}\n"
            f"TODAY: {date.today().strftime('%A, %B %-d, %Y')}"
        )
    except Exception as e:
        yield emit("error", {
            "type": type(e).__name__,
            "message": f"setup failed: {e}",
        })
        return

    messages: list[dict] = [{"role": "user", "content": user_input}]
    tool_log: list[dict] = []
    citations: list[dict] = []
    seen_citation_ids: set[str] = set()
    actions_taken: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    downstream_cost = 0.0

    try:
        for iteration in range(MAX_TOOL_LOOP_ITERATIONS):
            if verbose:
                print(f"[stream iter {iteration + 1}] calling Opus...", file=sys.stderr)

            with client.messages.stream(
                model=CHAD_MODEL,
                max_tokens=CHAD_MAX_TOKENS,
                system=system,
                tools=TOOLS,
                messages=messages,
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield emit("text_delta", {"delta": event.delta.text})

                final_message = stream.get_final_message()

            total_input_tokens += final_message.usage.input_tokens
            total_output_tokens += final_message.usage.output_tokens

            if final_message.stop_reason == "end_turn":
                final_text = ""
                for block in final_message.content:
                    if block.type == "text":
                        final_text += block.text

                opus_cost = (
                    total_input_tokens * OPUS_INPUT_PER_M / 1_000_000
                    + total_output_tokens * OPUS_OUTPUT_PER_M / 1_000_000
                )

                yield emit("message_complete", {
                    "answer": final_text.strip() or "(no answer produced)",
                    "citations": citations,
                    "tools_called": tool_log,
                    "actions_taken": actions_taken,
                    "model": CHAD_MODEL,
                    "cost_usd": round(opus_cost + downstream_cost, 4),
                    "opus_cost_usd": round(opus_cost, 4),
                    "downstream_cost_usd": round(downstream_cost, 4),
                    "duration_ms": int((time.time() - started_at) * 1000),
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "iterations": iteration + 1,
                })
                return

            if final_message.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": final_message.content})
                tool_results_for_next_turn = []

                for block in final_message.content:
                    if block.type != "tool_use":
                        continue

                    inputs = block.input or {}

                    yield emit("tool_use", {
                        "id": block.id,
                        "name": block.name,
                        "input": inputs,
                    })

                    tool_started = time.time()

                    if block.name == "ask_chad":
                        # Inlined ask_chad call so we can extract structured
                        # citations and emit them as discrete events. _tool_ask_chad
                        # flattens citations into the text block, which is what
                        # Opus needs for its next turn but not what the iOS
                        # client wants for citation chip rendering.
                        from home_builder_agent.agents.ask_agent import ask_question

                        try:
                            result = ask_question(
                                inputs.get("question", ""), verbose=False
                            )
                            answer = result.get("answer", "(no answer)")
                            tool_citations = result.get("citations") or []
                            cost = float(result.get("cost_usd") or 0.0)
                        except Exception as e:
                            output = f"ask_chad failed: {type(e).__name__}: {e}"
                            tool_citations = []
                            cost = 0.0
                        else:
                            if tool_citations:
                                cite_str = "\n".join(
                                    f"  - {c.get('name', '?')} ({c.get('webViewLink', '')})"
                                    for c in tool_citations[:5]
                                )
                                output = f"{answer}\n\nCitations:\n{cite_str}"
                            else:
                                output = answer

                        for c in tool_citations:
                            cid = c.get("id") or c.get("file_id") or c.get("name")
                            if cid and cid not in seen_citation_ids:
                                seen_citation_ids.add(cid)
                                citations.append(c)
                                yield emit("citation_added", c)

                    elif block.name == "dispatch_action":
                        # Streaming surface is real iOS user — never dry-run.
                        output, cost = _tool_dispatch_action(
                            inputs.get("nl_command", ""),
                            inputs.get("project_id"),
                            dry_run=False,
                        )
                        actions_taken.append(inputs.get("nl_command", ""))
                    elif block.name == "list_pending_drafts":
                        output, cost = _tool_list_pending_drafts(
                            inputs.get("project_id", ""),
                            inputs.get("limit"),
                        )
                    elif block.name == "read_morning_view":
                        output, cost = _tool_read_morning_view(
                            inputs.get("project_id", "")
                        )
                    elif block.name == "list_drop_deads":
                        output, cost = _tool_list_drop_deads(
                            inputs.get("project_id", ""),
                            inputs.get("bands"),
                        )
                    elif block.name == "list_overnight_events":
                        output, cost = _tool_list_overnight_events(
                            inputs.get("project_id"),
                            inputs.get("since_hours"),
                        )
                    elif block.name == "list_checklist_items_for_phase":
                        output, cost = _tool_list_checklist_items_for_phase(
                            inputs.get("project_id", ""),
                            inputs.get("phase_name", ""),
                        )
                    elif block.name == "log_site_note":
                        output, cost = _tool_log_site_note(
                            inputs.get("text", ""),
                            inputs.get("project_id"),
                            dry_run=False,  # iOS user — real action
                        )
                        actions_taken.append(
                            f"site-log: {inputs.get('text', '')[:80]}"
                        )
                    elif block.name == "approve_draft_action":
                        output, cost = _tool_approve_draft_action(
                            inputs.get("draft_action_id", ""),
                            inputs.get("decision_notes"),
                        )
                        actions_taken.append(
                            f"approved draft {inputs.get('draft_action_id', '')[:8]}…"
                        )
                    elif block.name == "system_status":
                        output, cost = _tool_system_status()
                    else:
                        output = f"Unknown tool: {block.name}"
                        cost = 0.0

                    duration_ms = int((time.time() - tool_started) * 1000)
                    downstream_cost += cost
                    tool_log.append({
                        "name": block.name,
                        "input": inputs,
                        "duration_ms": duration_ms,
                        "cost_usd": cost,
                    })

                    summary = output.split("\n")[0][:160]
                    yield emit("tool_result", {
                        "id": block.id,
                        "name": block.name,
                        "duration_ms": duration_ms,
                        "summary": summary,
                    })

                    tool_results_for_next_turn.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    })

                messages.append({"role": "user", "content": tool_results_for_next_turn})
                continue

            # Unexpected stop reason — surface as error and terminate
            yield emit("error", {
                "type": "UnexpectedStopReason",
                "message": f"Stream ended with stop_reason={final_message.stop_reason!r}",
            })
            return

        # Exited loop without end_turn — hit MAX_TOOL_LOOP_ITERATIONS
        yield emit("error", {
            "type": "MaxIterationsReached",
            "message": (
                f"Tool loop exceeded {MAX_TOOL_LOOP_ITERATIONS} iterations "
                "without Opus composing a final answer"
            ),
        })

    except Exception as e:
        yield emit("error", {
            "type": type(e).__name__,
            "message": str(e),
        })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_pretty(result: dict) -> None:
    print()
    print(result["answer"])
    print()
    if result["actions_taken"]:
        print("Actions:")
        for a in result["actions_taken"]:
            print(f"  • {a}")
        print()
    print(
        f"--- {result['model']} | "
        f"{result['iterations']} iter | "
        f"{result['duration_ms']}ms | "
        f"opus ${result['opus_cost_usd']:.4f} + "
        f"downstream ${result['downstream_cost_usd']:.4f} = "
        f"${result['cost_usd']:.4f}"
    )


def _print_stream(user_input: str, *, verbose: bool) -> None:
    """Consume chad_turn_stream and render to the terminal.

    Renders text_delta inline, tool/citation/error events as labeled lines,
    and a final cost summary on message_complete. Used for smoke-testing
    parity with the iOS SSE surface.
    """
    print()
    final_payload: dict | None = None
    for event_id, event_type, payload in chad_turn_stream(user_input, verbose=verbose):
        if event_type == "text_delta":
            sys.stdout.write(payload.get("delta", ""))
            sys.stdout.flush()
        elif event_type == "tool_use":
            print(f"\n[tool_use:{event_id}] {payload.get('name')} {json.dumps(payload.get('input') or {})[:200]}", file=sys.stderr)
        elif event_type == "tool_result":
            print(f"[tool_result:{event_id}] {payload.get('name')} ({payload.get('duration_ms')}ms): {payload.get('summary')}", file=sys.stderr)
        elif event_type == "citation_added":
            print(f"[citation:{event_id}] {payload.get('name')} ({payload.get('webViewLink', '')})", file=sys.stderr)
        elif event_type == "error":
            print(f"\n[error:{event_id}] {payload.get('type')}: {payload.get('message')}", file=sys.stderr)
            return
        elif event_type == "message_complete":
            final_payload = payload

    print()
    if final_payload:
        print()
        if final_payload.get("actions_taken"):
            print("Actions:")
            for a in final_payload["actions_taken"]:
                print(f"  • {a}")
            print()
        print(
            f"--- {final_payload.get('model')} | "
            f"{final_payload.get('iterations')} iter | "
            f"{final_payload.get('duration_ms')}ms | "
            f"opus ${final_payload.get('opus_cost_usd', 0):.4f} + "
            f"downstream ${final_payload.get('downstream_cost_usd', 0):.4f} = "
            f"${final_payload.get('cost_usd', 0):.4f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"hb-chad — {CUSTOMER_NAME}'s AI extension. "
                    f"Speaks in his voice, knows his projects, dispatches the right specialist.",
    )
    parser.add_argument("input", help="What Chad is saying or asking")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Plan tool calls without dispatching writes via dispatch_action.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object instead of pretty terminal output.",
    )
    parser.add_argument(
        "--stream", action="store_true",
        help="Use the streaming code path (chad_turn_stream). Smoke-tests "
             "the surface that powers the iOS Ask tab. Ignores --dry-run "
             "and --json.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print tool-loop progress to stderr.",
    )
    args = parser.parse_args()

    if args.stream:
        _print_stream(args.input, verbose=args.verbose)
        return

    result = chad_turn(args.input, dry_run=args.dry_run, verbose=args.verbose)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_pretty(result)


if __name__ == "__main__":
    main()
