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
import base64
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from anthropic import Anthropic

from home_builder_agent.agents import conversation_store
from home_builder_agent.core.chad_context import get_chad_context
from home_builder_agent.core.chad_voice import (
    CUSTOMER_NAME,
    chad_voice_system,
)
from home_builder_agent.core.claude_client import (
    PROMPT_CACHING_BETA_HEADER,
    cached_system_block,
    make_client,
    tools_with_cache,
)
from home_builder_agent.core.cost_guard import check_budget, record_cost


# ---------------------------------------------------------------------------
# Image attachments (vision)
# ---------------------------------------------------------------------------

# Allowlist mirrors the route-level validation in patton-ai-ios's
# turtle_ask_stream POST handler. Defense-in-depth: even if the route
# accidentally lets something else through, the engine refuses.
_ALLOWED_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png"}


@dataclass(frozen=True)
class ImageInput:
    """A single image attachment headed for Claude vision.

    The route validates magic numbers + size limits before constructing
    one of these. The engine trusts the media_type but still asserts
    membership in the allowlist as a sanity check.
    """

    media_type: str
    data: bytes


def _build_user_content(
    user_input: str,
    images: list[ImageInput] | None,
) -> str | list[dict]:
    """Assemble the first ``messages[0]`` user content.

    With no images: returns a bare string (preserves the historical
    shape, keeps the existing tool-loop wire format unchanged for
    text-only turns).

    With images: returns a list of content blocks — text first, then
    one base64 ``image`` block per attachment. Order follows Anthropic's
    vision examples (text followed by images works, but images-first
    can confuse some prompt patterns).
    """
    if not images:
        return user_input
    content: list[dict] = [{"type": "text", "text": user_input}]
    for img in images:
        if img.media_type not in _ALLOWED_IMAGE_MEDIA_TYPES:
            raise ValueError(
                f"Unsupported image media_type: {img.media_type!r}. "
                f"Allowed: {sorted(_ALLOWED_IMAGE_MEDIA_TYPES)}"
            )
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": base64.b64encode(img.data).decode("ascii"),
                },
            }
        )
    return content


def _prior_turns_as_messages(turns: list[dict]) -> list[dict]:
    """Convert conversation_store rows into Anthropic messages.

    Strips any structured tool/action metadata — the model only needs
    the rendered text per role. Tool-use round-trip details from
    earlier turns are NOT replayed; the model sees only the assistant's
    final answer text. This keeps prior-turn payload predictable + cheap
    and avoids replaying tool_use/tool_result blocks whose tool_use_ids
    no longer match anything in the current request.
    """
    out: list[dict] = []
    for t in turns:
        role = t.get("role")
        content = (t.get("content") or "").strip()
        if not role or not content:
            continue
        if role not in ("user", "assistant"):
            continue
        out.append({"role": role, "content": content})
    return out


def _system_with_summary(base_system: str, rolling_summary: str | None) -> str:
    """Prefix the system prompt with the rolling summary if any."""
    if not rolling_summary:
        return base_system
    return (
        f"Earlier conversation context (one-line summary): {rolling_summary}\n\n"
        f"{base_system}"
    )


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
OPUS_CACHE_WRITE_PER_M = 18.75    # base × 1.25 (write-through penalty)
OPUS_CACHE_READ_PER_M = 1.50      # base × 0.10 (90% off, ~5-min TTL)
SONNET_INPUT_PER_M = 3.00
SONNET_OUTPUT_PER_M = 15.00
SONNET_CACHE_WRITE_PER_M = 3.75   # base × 1.25
SONNET_CACHE_READ_PER_M = 0.30    # base × 0.10


# ---------------------------------------------------------------------------
# Persona prompt — voice + context + role
# ---------------------------------------------------------------------------

PERSONA_SUFFIX = """

You are Chad Lynch's assistant. You hold his voice, his preferences,
and his judgment. You don't operate on Chad — you operate *as* him,
or *for* him.

Your job each turn:
  1. Understand what Chad wants. Don't ask clarifying questions when
     reasonable inference works. Chad is busy.
  2. Pick the right tool — see the tool selection guide below.
  3. Compose a response in Chad-voice prose. If you took an action,
     report it concretely. If you drafted something for Chad to send,
     say so and tell him where it is (Gmail Drafts, etc.).
  4. Suggest the obvious follow-up if there is one. Don't fabricate
     follow-ups when none is needed.

Tool selection guide — pick the most specific tool, fall back to ask_chad.

IMPORTANT: the tool names below (and any other code-shaped identifiers
in this section) are internal references for YOUR tool selection only.
Never echo them in your prose reply to Chad. When you took an action
via a tool, describe it by what it accomplished in plain Chad-voice
language — not by tool name. Bad: "I called dispatch_action to log the
receipt." Good: "Logged the $400 receipt against Whitfield."

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
  "archive Whitfield"                archive_project
  "kill that test project"           archive_project
  "start a new project Maple Ridge"  create_project
  "clone Whitfield as Pelican Pt"    clone_project
  "create X like Pelican Point"      clone_project  (NOT create_project)
  "set customer email on Whitfield"  update_customer_info
  "phone for Bradfords is 251-555…"  update_customer_info
  "job code Pelican Point is PP-26"  update_customer_info
  "address for Maple Ridge is …"     update_customer_info
  "note on Whitfield: Tuesday walk"  update_customer_info  (project-level, NOT log_site_note)
  "push framing end to June 12"      update_schedule_date
  "foundation starts Monday"         update_schedule_date
  "move drywall to 2026-09-08"       update_schedule_date
  "put trim before painting"         reorder_phase
  "move HVAC to position 8"          reorder_phase
  "make framing the first phase"     reorder_phase
  "log a $400 receipt for X"         dispatch_action
  "create a CO for cabinet upgrade"  dispatch_action
  "push framing two weeks"           dispatch_action  (use update_schedule_date if Chad cites exact dates)
  "save this chat to project folder" write_to_drive
  "export this conversation to .md"  write_to_drive
  "put that spec in Whitfield's"     write_to_drive
  "look up that FEMA panel"          web_fetch
  "what does this URL say?"          web_fetch
  "read this code section for me"    web_fetch
  "draft an email to the homeowner"  create_email_draft  (NEVER sends)
  "send a note to my framer"         create_email_draft  (NEVER sends)
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
  • When a tool returns a system-level error (database unavailable, a
    record store missing, an integration timed out, etc.), surface it
    to Chad in plain Chad-voice — don't hide infrastructure problems
    behind a fake-success reply. Translate the error into what it means
    for him ("the project records aren't reachable right now"); don't
    echo raw error strings or technology names verbatim.
"""


# ---------------------------------------------------------------------------
# Onboarding interview — first-launch flow
# ---------------------------------------------------------------------------
#
# On first launch, iOS routes Chad to the Ask tab where he taps a pre-staged
# "What do you know about me?" prompt. The agent responds with the existing
# profile snapshot, then methodically interviews him through the 10 fields
# below. Each answer is saved via save_profile_fact, which bumps
# onboarding.current_step in ~/.hb-profile-proposed.json and flips
# onboarding.complete=true when current_step >= 10. iOS reads the state from
# each message_complete event and updates its @AppStorage mirror, which
# controls the default tab on subsequent cold launches (Ask → Dashboard).

ONBOARDING_QUESTIONS: list[tuple[str, str]] = [
    ("daily_rhythm",
     "When do you start most days and when do you tap out? Weekends — on or off?"),
    ("comms_style",
     "Want me to push you on stuff — follow up, nag if you ghost me — "
     "or only answer when you ask? More direct or softer?"),
    ("top_projects",
     "Which 2–3 projects are eating most of your headspace this week?"),
    ("trusted_subs",
     "Who are your 3–4 most-reliable subs by trade — the ones you'd call "
     "first without shopping around?"),
    ("subs_to_avoid",
     "Anyone you've burned with and won't use again? Don't have to name "
     "names — just trade if it's awkward."),
    ("material_vendors",
     "Where do you usually source windows, framing lumber, and roofing? "
     "Same place every time or you shop it?"),
    ("change_order_style",
     "When an owner pushes back on a quote or wants a scope change — "
     "what's your usual move? Up-front pricing, soft no first, or sit on it?"),
    ("permit_gotchas",
     "Baldwin County permits — anything that bites first-timers but you "
     "know to handle? Any inspectors you actively dodge?"),
    ("cost_anomaly_thresholds",
     "Rough numbers — fair permit fee on a 4,000 sq ft custom build? "
     "'Something's off' framing-labor number on a job that size?"),
    ("quiet_hours",
     "What times or days should I leave you alone? Kids' stuff, church, "
     "family dinner — anything I should treat as quiet hours?"),
]

_PROFILE_PATH = Path.home() / ".hb-profile-proposed.json"


def _read_profile_raw() -> dict:
    """Read the local profile JSON. Returns {} on any failure (best-effort)."""
    try:
        if _PROFILE_PATH.exists():
            return json.loads(_PROFILE_PATH.read_text())
    except Exception:
        pass
    return {}


def _onboarding_state(profile: dict | None = None) -> dict:
    """Return current onboarding state. Always returns a dict with both keys.

    Shape: {"complete": bool, "current_step": int}. Missing/malformed
    profile yields {"complete": False, "current_step": 0} so iOS always
    has a definite answer to render against.
    """
    if profile is None:
        profile = _read_profile_raw()
    ob = profile.get("onboarding") or {}
    return {
        "complete": bool(ob.get("complete", False)),
        "current_step": int(ob.get("current_step", 0)),
    }


def _build_onboarding_suffix(profile: dict) -> str | None:
    """System-prompt suffix that puts the agent in interview mode.

    Returns None when onboarding is complete — caller appends nothing.
    Otherwise builds a checklist of the 10 fields (✓ = saved this profile)
    plus tight rules: one question per turn, save before replying, no
    bundling.
    """
    state = _onboarding_state(profile)
    if state["complete"]:
        return None

    question_lines = []
    for i, (field, prompt) in enumerate(ONBOARDING_QUESTIONS, start=1):
        marker = "✓" if profile.get(field) else " "
        question_lines.append(f"  {marker} {i}. {field} — {prompt}")
    questions_block = "\n".join(question_lines)

    return f"""

[ONBOARDING MODE — first-time interview, {state["current_step"]}/10 saved]

This is Chad's first conversation with you in this app. Your job this
thread is to fill out the 10 profile fields below by interviewing him —
one question per turn. After onboarding completes, iOS routes him to
the Dashboard by default.

The 10 fields and the question to ask each (✓ = already saved):

{questions_block}

Hard rules:
  • Ask EXACTLY ONE question per turn from the list above. Never bundle two.
  • Pick the next question by skipping any already saved (✓). Order doesn't
    matter — choose what flows naturally from Chad's previous answer.
  • After Chad answers, call save_profile_fact(field=<the field id>,
    value=<his answer distilled — under 200 chars>) BEFORE writing your
    next text reply. Multiple save_profile_fact calls in one turn are OK
    if Chad volunteered info that maps to multiple fields.
  • Be warm. Briefly acknowledge his answer, then move on. Don't lecture.
  • If Chad pushes back or wants to skip, acknowledge and move on. Don't
    pressure him to answer.
  • When all 10 are saved, your next reply is a short wrap-up — e.g.
    "Got it, that's enough to work with. Tap Dashboard when you're ready."
    — and you STOP. Do NOT call save_profile_fact after step 10.
  • Do NOT discuss app features, other tabs, or unrelated topics during
    onboarding. Stay focused on the interview.

First-turn special case:
  If Chad's opening message is some variation of "what do you know
  about me", lead with a tight summary of the CHAD'S PROFILE block
  above (identity, company, operating context, communication style),
  then transition with: "Now let me return the favor — 10 quick
  questions so I can be useful from day one." Then ask the first
  unfilled question.
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
    {
        "name": "archive_project",
        "description": (
            "Soft-archive a project on home_builder.project — flips status "
            "to 'archived' so it disappears from active surfaces (morning "
            "view, drop-deads, judgment queue) while preserving full audit "
            "history. Use when Chad says 'archive Whitfield', 'kill that "
            "test project', 'we're done with that one', 'put the Wilson "
            "project to bed', 'shelve Pelican Point', 'mothball the "
            "Hartley build'. v1 is DB-only — does NOT rename Drive folders, "
            "does NOT touch Tracker sheets. "
            "TWO-STEP CONFIRMATION (mandatory): always call this tool FIRST "
            "without confirm=true to get a preview of what would be archived "
            "(project name, customer, target date, current phase). Surface "
            "that preview to the user and ask them to explicitly confirm. "
            "Only call again with confirm=true after the user says 'yes', "
            "'confirmed', 'go ahead', or similar in the SAME conversation. "
            "Don't assume the user's initial 'archive Whitfield' is a "
            "confirmation — that's the request, not the approval. Returns "
            "a preview string when confirm is omitted; returns a "
            "completion confirmation string when confirm=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": (
                        "Name (substring OK) or UUID of the project to "
                        "archive. Required."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short human-readable note recorded with the "
                        "archive (e.g., 'closeout complete', 'on hold "
                        "indefinitely'). Optional."
                    ),
                },
                "confirm": {
                    "type": "boolean",
                    "description": (
                        "Set to true ONLY after the user has explicitly "
                        "approved the archive in this conversation. "
                        "Default false (preview-only). Setting this to "
                        "true on the first call without user approval is "
                        "a contract violation — Chad's projects are real "
                        "data, not test fixtures."
                    ),
                },
            },
            "required": ["project_name"],
        },
    },
    {
        "name": "create_project",
        "description": (
            "Create a fresh empty project on home_builder.project, seeded "
            "from migration 010's blank template (24 phases, 923 checklist "
            "items). Use when Chad says 'start a new project called Maple "
            "Ridge', 'spin up a fresh project for the Smith family', "
            "'add Pelican Point to my list', 'open a new project for "
            "Hartley', 'kick off a build for the Bradfords'. NOT for "
            "cloning — if Chad says 'create X like Y', 'X cloned from Y', "
            "'a new one shaped like Pelican Point', use clone_project "
            "instead. Requires either target_completion_date or "
            "target_framing_start_date so the schedule can be seeded later. "
            "Returns a confirmation string."
        ),
        "input_schema": {
            # NOTE: Anthropic's tool input_schema does NOT support
            # top-level `anyOf` / `oneOf` / `allOf` constraints — they
            # 400 the whole tools list. So the "at least one date"
            # constraint lives in the dispatch handler at runtime, not
            # in the schema. Description text + handler error make
            # this clear to Claude on retry.
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Name for the new project. Required.",
                },
                "customer_name": {
                    "type": "string",
                    "description": (
                        "Homeowner / customer name. Optional — defaults to "
                        "'TBD' if omitted."
                    ),
                },
                "target_completion_date": {
                    "type": "string",
                    "description": (
                        "YYYY-MM-DD target completion date. EITHER this OR "
                        "target_framing_start_date is required (handler "
                        "errors with a clear message if neither is provided)."
                    ),
                },
                "target_framing_start_date": {
                    "type": "string",
                    "description": (
                        "YYYY-MM-DD target framing-start date. Alternative "
                        "to target_completion_date."
                    ),
                },
            },
            "required": ["project_name"],
        },
    },
    {
        "name": "clone_project",
        "description": (
            "Create a new project by structurally cloning an existing one — "
            "copies phases, checklist items, and vendor list from the "
            "source project, with fresh status / NULL actuals. Explicitly "
            "excludes events, drafts, photos, and site logs. Use when Chad "
            "says 'create Pelican Point cloned from Whitfield', 'spin up a "
            "fresh test project from the Whitfield shape', 'start a new "
            "one like Pelican Point', 'copy Whitfield as Hartley', 'a new "
            "build shaped like the Bradford project'. NOT for fresh "
            "blank-template projects — if Chad just says 'start a new "
            "project called X' with no source, use create_project. "
            "Returns a confirmation string."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "copy_from": {
                    "type": "string",
                    "description": (
                        "Source project: name (substring OK) or UUID. "
                        "Required."
                    ),
                },
                "new_name": {
                    "type": "string",
                    "description": "Name for the new cloned project. Required.",
                },
                "customer_name": {
                    "type": "string",
                    "description": (
                        "Homeowner / customer name for the new project. "
                        "Optional — defaults to 'TBD' if omitted."
                    ),
                },
            },
            "required": ["copy_from", "new_name"],
        },
    },
    {
        "name": "write_to_drive",
        "description": (
            "Save a file to a Google Drive folder. Use when Chad says "
            "'save this to the project folder', 'export this chat', "
            "'put that in Drive', 'write this spec to Whitfield's folder', "
            "or when the iOS app's 'save chat to project folder' flow "
            "invokes you. Creates a NEW file — never overwrites or "
            "deletes existing ones. Returns the Drive web URL so Chad "
            "can open it. If you don't know the folder_id, look it up "
            "first via ask_chad (e.g., 'what's the drive_folder_id for "
            "Whitfield?') or read_morning_view (which exposes it on the "
            "project record)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "folder_id": {
                    "type": "string",
                    "description": (
                        "Google Drive folder ID where the file should be "
                        "created. For a project's chat export, use the "
                        "project's drive_folder_id from the project "
                        "record. Required."
                    ),
                },
                "file_name": {
                    "type": "string",
                    "description": (
                        "File name including extension. Examples: "
                        "'Chad chat 2026-05-10.md', 'Whitfield spec.md', "
                        "'Site notes — week of 2026-05-10.md'. Required."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Full file body, as a string. For chat exports, "
                        "this is the markdown-formatted transcript. "
                        "Required."
                    ),
                },
                "mime_type": {
                    "type": "string",
                    "description": (
                        "MIME type. Defaults to 'text/markdown' for .md "
                        "files. Use 'text/plain' for .txt, 'text/html' "
                        "for raw HTML. Drive stores the file as-is — no "
                        "auto-conversion to Google Doc format. Optional."
                    ),
                },
            },
            "required": ["folder_id", "file_name", "content"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch a web page or text URL and return its cleaned readable "
            "text content. Use when Chad asks you to look something up on "
            "a specific website, pull a public document (FEMA FIRM panel "
            "page, Baldwin County code section, supplier product page, "
            "municipal records lookup), or read an article whose URL Chad "
            "shared. Returns plain text with HTML tags stripped. Capped "
            "at 50 KB to protect context. Returns an error string on "
            "failure — never throws. Read-only: this tool only fetches, "
            "it never submits forms or follows redirects to login pages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "Full http:// or https:// URL to fetch. Required."
                    ),
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "create_email_draft",
        "description": (
            "Create a Gmail draft from Chad's account. Does NOT send — "
            "the draft lands in Chad's Drafts folder, he opens it, "
            "reviews, edits, and hits Send himself. Use when Chad says "
            "'draft an email to the homeowner about X', 'put together a "
            "note to my framer about the schedule slip', 'draft a "
            "response to that supplier RFQ'. Returns the draft URL so "
            "Chad can jump straight to it. Always prefer drafts over "
            "sends — per Patton AI's outbound-comms rule, outbound "
            "communications stay drafted-only until Chad explicitly "
            "approves them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": (
                        "Primary recipient email address. Must contain "
                        "'@'. Required."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line. Required.",
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Email body. Plain text or HTML (auto-detected — "
                        "if the body contains <p>/<br>/<div>/<html> tags "
                        "it's treated as HTML, otherwise plain text). "
                        "Required."
                    ),
                },
                "cc": {
                    "type": "string",
                    "description": (
                        "Optional CC recipient. Single email address. "
                        "Must contain '@' if provided."
                    ),
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "save_profile_fact",
        "description": (
            "Save a single field of Chad's profile during the first-launch "
            "onboarding interview. Writes the value to "
            "~/.hb-profile-proposed.json and bumps onboarding.current_step. "
            "When current_step reaches 10, onboarding.complete flips to "
            "true and iOS will route Chad to the Dashboard on next launch. "
            "Use this ONLY during onboarding mode (see [ONBOARDING MODE] "
            "block in the system prompt). Never invent values — only save "
            "what Chad actually told you. Distill his answer to under 200 "
            "chars but keep the substance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "description": (
                        "The profile field ID. Must be one of: daily_rhythm, "
                        "comms_style, top_projects, trusted_subs, "
                        "subs_to_avoid, material_vendors, change_order_style, "
                        "permit_gotchas, cost_anomaly_thresholds, quiet_hours."
                    ),
                },
                "value": {
                    "type": "string",
                    "description": (
                        "Chad's answer, distilled to under 200 chars. Plain "
                        "text. Strip filler, keep the substance."
                    ),
                },
            },
            "required": ["field", "value"],
        },
    },
    {
        "name": "update_customer_info",
        "description": (
            "Update one or more customer-info fields on a project — "
            "customer_name, customer_email, customer_phone, address, "
            "job_code, or notes. Postgres-canonical replacement for "
            "Connor's manual Tracker Project Info edits (per ADR "
            "2026-05-11). Only fields you explicitly pass are written — "
            "unprovided fields are NEVER nulled-out. Use when Chad says "
            "'set the customer email on Whitfield to jane@example.com', "
            "'phone for the Bradfords is 251-555-0142', 'job code for "
            "Pelican Point is PP-2026', 'address for Maple Ridge is 123 "
            "Main St', 'note on Whitfield: client wants Tuesday walkthroughs'. "
            "Empty-string values are rejected (looks like a typo) — to "
            "intentionally clear a field, pass null. Returns a "
            "Chad-voice confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": (
                        "UUID of the project to update. Required. Resolve "
                        "from name via ask_chad / read_morning_view first "
                        "if Chad named the project but you don't have the "
                        "UUID."
                    ),
                },
                "customer_name": {
                    "type": "string",
                    "description": (
                        "Customer / homeowner name. Optional. Overwrites "
                        "the legacy 'TBD' default cleanly."
                    ),
                },
                "customer_email": {
                    "type": "string",
                    "description": (
                        "Primary customer email. Loose 'something@something' "
                        "validation — refuses obviously malformed input."
                    ),
                },
                "customer_phone": {
                    "type": "string",
                    "description": (
                        "Primary customer phone. Accepts '(251) 555-0142' "
                        "or '2515550142' — non-digits stripped, must yield "
                        "10 digits."
                    ),
                },
                "address": {
                    "type": "string",
                    "description": "Project street address. Free-form text.",
                },
                "job_code": {
                    "type": "string",
                    "description": (
                        "Optional short identifier (e.g. 'WHIT-2026') used "
                        "in invoices, change orders, Tracker filenames."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "Free-form project-level notes. Not for site notes "
                        "(those go through log_site_note)."
                    ),
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "update_schedule_date",
        "description": (
            "Update planned_start_date and/or planned_end_date on a "
            "single phase. Postgres-canonical replacement for manual "
            "Tracker Master Schedule edits (per ADR 2026-05-11). "
            "Identify the phase by EITHER phase_sequence_index (1–24) OR "
            "phase_name (case-insensitive substring) — not both. Use "
            "when Chad says 'push framing end to June 12', 'foundation "
            "starts Monday', 'move drywall back a week to 2026-09-08'. "
            "End date must be ≥ start date (if you pass only one, the "
            "other is fetched from DB and validated). If shifting end "
            "would push the next phase's start earlier than its current "
            "value, you'll get a cascade_warning back — surface that to "
            "Chad and ASK whether he wants the downstream phases shifted "
            "(do NOT auto-cascade; that's a separate explicit operation)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "UUID of the project. Required.",
                },
                "phase_sequence_index": {
                    "type": "integer",
                    "description": (
                        "Phase position 1–24. Use this when Chad cited a "
                        "phase by number ('move phase 3 to…')."
                    ),
                },
                "phase_name": {
                    "type": "string",
                    "description": (
                        "Phase name (case-insensitive substring OK — "
                        "'framing' matches 'Framing'). Use this when Chad "
                        "named the phase. If the substring matches >1 "
                        "phase, the handler returns an 'ambiguous' error "
                        "listing candidates."
                    ),
                },
                "planned_start_date": {
                    "type": "string",
                    "description": (
                        "YYYY-MM-DD planned start date. Optional but at "
                        "least one of start / end is required."
                    ),
                },
                "planned_end_date": {
                    "type": "string",
                    "description": (
                        "YYYY-MM-DD planned end date. Optional but at "
                        "least one of start / end is required."
                    ),
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "reorder_phase",
        "description": (
            "Move a single phase to a new position in the project's "
            "schedule. Park-and-swap pattern, transaction-wrapped — "
            "respects the (project_id, sequence_index) UNIQUE "
            "constraint without a schema change. Postgres-canonical "
            "replacement for drag-and-drop reordering Connor used to do "
            "in the Tracker (per ADR 2026-05-11). Identify the moving "
            "phase by phase_sequence_index OR phase_name (not both); "
            "pass new_position as the integer target (1–N, where N is "
            "the highest existing sequence_index for the project — no "
            "gaps allowed). Use when Chad says 'move trim before "
            "painting', 'put framing first', 'shift HVAC to position 8'. "
            "If new_position equals the current position, returns a "
            "no-op confirmation without touching the DB. Bulk reorders "
            "are out of scope — call this once per phase."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "UUID of the project. Required.",
                },
                "phase_sequence_index": {
                    "type": "integer",
                    "description": (
                        "Current position of the phase to move (1–24)."
                    ),
                },
                "phase_name": {
                    "type": "string",
                    "description": (
                        "Phase name (case-insensitive substring OK). "
                        "Ambiguous matches return an error listing "
                        "candidates."
                    ),
                },
                "new_position": {
                    "type": "integer",
                    "description": (
                        "Target sequence_index, 1 ≤ new_position ≤ "
                        "max_seq_index_for_project. Required."
                    ),
                },
            },
            "required": ["project_id", "new_position"],
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


def _parse_iso_date(s: str | None):
    """Parse a YYYY-MM-DD string into a date, or return None if input is empty.

    Returns the sentinel object `_DATE_PARSE_ERROR` on malformed input so
    callers can distinguish "not provided" from "provided but invalid".
    """
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        return _DATE_PARSE_ERROR


_DATE_PARSE_ERROR = object()


def _tool_archive_project(
    project_name: str,
    *,
    reason: str | None = None,
    confirm: bool = False,
    dry_run: bool = False,
) -> tuple[str, float]:
    """Archive a project — flip status to 'archived' via archive_project_in_db.

    Two-step confirmation: when `confirm=False` (the default), this returns
    a PREVIEW of what would be archived without touching the DB. Only when
    `confirm=True` does the actual write happen. The system-prompt contract
    instructs Claude to never set confirm=True without an explicit user
    approval in the same conversation — Chad's projects are real data
    and an accidental archive is a high-friction recovery (re-flip status,
    explain to Chad, regain trust).

    Direct adapter call (NOT through hb-router) so the tool can return a
    structured confirmation string in one round-trip — same pattern
    `approve_draft_action` uses. Engine_activity audit happens at the
    chad_turn level via the persona-agent invocation log; the per-action
    DB write carries its own updated_at trigger trail (migration 007a).

    Returns (human_readable_message, cost_usd_=0).
    """
    if not project_name or not project_name.strip():
        return "archive_project: project_name is required.", 0.0

    # Lazy imports — keep cold-start path fast.
    try:
        from home_builder_agent.agents.project_agent import _resolve_project
        from home_builder_agent.scheduling.store_postgres import (
            archive_project_in_db,
        )
    except Exception as e:
        return (
            f"archive_project: import failed — {type(e).__name__}: {e}",
            0.0,
        )

    proj = _resolve_project(project_name)
    if not proj:
        return (
            f"archive_project: no project matched {project_name!r}. "
            "Try the full name or list active projects first.",
            0.0,
        )
    if proj.get("status") == "archived":
        return (
            f"{proj['name']} is already archived. No-op.",
            0.0,
        )
    if dry_run:
        suffix = f" (reason: {reason})" if reason else ""
        return (
            f"(dry-run) Would archive {proj['name']} "
            f"(id {proj['id'][:8]}…){suffix}.",
            0.0,
        )
    # Two-step confirmation: preview first, execute on the second call.
    if not confirm:
        # Surface enough detail that Chad can decide. Use only fields
        # known-present on the resolved project; degrade gracefully on
        # missing optionals.
        customer = proj.get("customer_name") or "unknown customer"
        target = (
            proj.get("target_completion_date")
            or proj.get("target_framing_start_date")
            or "no target date set"
        )
        reason_part = f" Reason given: {reason!r}." if reason else ""
        return (
            f"PREVIEW (no DB write yet): archive_project would "
            f"soft-archive {proj['name']!r} (id {proj['id'][:8]}…, "
            f"customer: {customer}, target: {target}). Phase / event / "
            f"draft history would be preserved; the project would just "
            f"disappear from active surfaces and morning view.{reason_part} "
            "ASK THE USER TO EXPLICITLY CONFIRM (e.g. 'yes, archive it' / "
            "'go ahead') BEFORE calling archive_project again with "
            "confirm=true. Do not assume the user's original archive "
            "request is itself the confirmation — that was the intent, "
            "not the approval.",
            0.0,
        )
    try:
        ok = archive_project_in_db(proj["id"], reason=reason)
    except Exception as e:
        return (
            f"archive_project: DB write failed — "
            f"{type(e).__name__}: {e}",
            0.0,
        )
    if not ok:
        return (
            f"archive_project: update returned False for "
            f"{proj['name']} (race? already archived?).",
            0.0,
        )
    suffix = f" Reason: {reason}." if reason else ""
    return (
        f"Archived {proj['name']} (id {proj['id'][:8]}…). "
        f"Phase / event / draft history preserved; active surfaces no "
        f"longer show this project.{suffix}",
        0.0,
    )


def _tool_create_project(
    project_name: str,
    *,
    customer_name: str | None = None,
    target_completion_date: str | None = None,
    target_framing_start_date: str | None = None,
    dry_run: bool = False,
) -> tuple[str, float]:
    """Create a fresh empty project via create_project_in_db.

    Per ADR (2026-05-09), `create_project` does NOT accept a copy_from —
    if Chad wants to clone, he calls `clone_project`. Keeping the tool
    surfaces cleanly separated improves Claude's routing confidence.

    Returns (human_readable_message, cost_usd_=0).
    """
    if not project_name or not project_name.strip():
        return "create_project: project_name is required.", 0.0

    # Lazy imports — keep cold-start path fast.
    try:
        from home_builder_agent.scheduling.store_postgres import (
            create_project_in_db,
        )
    except Exception as e:
        return (
            f"create_project: import failed — {type(e).__name__}: {e}",
            0.0,
        )

    target_completion = _parse_iso_date(target_completion_date)
    target_framing = _parse_iso_date(target_framing_start_date)
    if target_completion is _DATE_PARSE_ERROR:
        return (
            f"create_project: target_completion_date "
            f"{target_completion_date!r} is not a valid YYYY-MM-DD.",
            0.0,
        )
    if target_framing is _DATE_PARSE_ERROR:
        return (
            f"create_project: target_framing_start_date "
            f"{target_framing_start_date!r} is not a valid YYYY-MM-DD.",
            0.0,
        )

    if not (target_completion or target_framing):
        return (
            "create_project: requires target_completion_date or "
            "target_framing_start_date so the schedule can be seeded later.",
            0.0,
        )
    if dry_run:
        return (
            f"(dry-run) Would create empty project {project_name!r} "
            f"(target_completion={target_completion_date}, "
            f"target_framing_start={target_framing_start_date}).",
            0.0,
        )
    try:
        new_id = create_project_in_db(
            name=project_name,
            customer_name=customer_name or "TBD",
            address=None,
            target_completion_date=target_completion,
            target_framing_start_date=target_framing,
        )
    except Exception as e:
        return (
            f"create_project: DB write failed — "
            f"{type(e).__name__}: {e}",
            0.0,
        )
    seed_target = target_completion_date or target_framing_start_date
    return (
        f"Created empty project {project_name} (id {new_id[:8]}…). "
        f"No phases yet — run hb-schedule \"{project_name}\" "
        f"--target-completion {seed_target} --seed-postgres to instantiate "
        "the 13-phase template.",
        0.0,
    )


def _tool_clone_project(
    copy_from: str,
    new_name: str,
    *,
    customer_name: str | None = None,
    dry_run: bool = False,
) -> tuple[str, float]:
    """Clone a project via clone_project_in_db.

    Copies phases + milestones from the source project with fresh status /
    NULL actuals. Per ADR (2026-05-09), this is a separate top-level tool
    from `create_project` — kept distinct so Claude's routing stays clean
    when Chad says "start a new one like Pelican Point".

    Returns (human_readable_message, cost_usd_=0).
    """
    if not copy_from or not copy_from.strip():
        return (
            "clone_project: copy_from is required (source project name or UUID).",
            0.0,
        )
    if not new_name or not new_name.strip():
        return "clone_project: new_name is required.", 0.0

    # Lazy imports — keep cold-start path fast.
    try:
        from home_builder_agent.agents.project_agent import _resolve_project
        from home_builder_agent.scheduling.store_postgres import (
            clone_project_in_db,
        )
    except Exception as e:
        return (
            f"clone_project: import failed — {type(e).__name__}: {e}",
            0.0,
        )

    source = _resolve_project(copy_from)
    if not source:
        return (
            f"clone_project: no source project matched {copy_from!r}.",
            0.0,
        )
    if dry_run:
        return (
            f"(dry-run) Would clone {source['name']} → "
            f"new project {new_name!r}, "
            f"copying phases + milestones with fresh status.",
            0.0,
        )
    try:
        new_id = clone_project_in_db(
            source["id"],
            new_name=new_name,
            customer_name=customer_name,
            address=None,
            target_completion_date=None,
            target_framing_start_date=None,
        )
    except Exception as e:
        return (
            f"clone_project: DB write failed — "
            f"{type(e).__name__}: {e}",
            0.0,
        )
    next_step = (
        f" Phases copied with original dates; run hb-schedule "
        f"\"{new_name}\" --target-completion <YYYY-MM-DD> "
        "--seed-postgres to re-seed planned dates if you want a fresh schedule."
    )
    return (
        f"Cloned {source['name']} → {new_name} "
        f"(id {new_id[:8]}…). Phases + milestones copied "
        f"(status=not-started, NULL actuals).{next_step}",
        0.0,
    )


# ---------------------------------------------------------------------------
# Tracker-canonicalization input tools (ADR 2026-05-11)
# ---------------------------------------------------------------------------
#
# `update_customer_info`, `update_schedule_date`, and `reorder_phase` are
# the input surface that replaces Connor's manual Google Sheets Tracker
# edits now that Postgres is canonical for home-builder project state.
#
# All three:
#   - operate directly on home_builder.{project,phase} via the existing
#     postgres.connection() context manager (autocommit off → atomic
#     commit on clean exit, rollback on exception)
#   - return (chad_voice_text, cost_usd_=0) like the rest of the tool
#     family (no Claude call inside; pure DB writes)
#   - return a SECOND structured-result payload appended to the text so
#     the agent has a machine-readable summary for its own awareness.
#     The agent NEVER echoes the structured block to Chad verbatim —
#     persona rules below the PERSONA_SUFFIX section enforce that.

# Loose email regex — same posture as the rest of the suite: accept
# anything that looks like x@y, reject obviously malformed input. We are
# NOT trying to RFC-validate addresses; we're catching typos.
import re as _re

_EMAIL_RE = _re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _normalize_phone(raw: str) -> str | None:
    """Strip non-digits from raw; return the digit-only string if it has
    exactly 10 digits, else None. Accepts '(251) 555-0142', '251-555-0142',
    '2515550142', etc.
    """
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return digits
    return None


# Allowlist of mutable customer-info fields. Order matters for the
# Chad-voice confirmation suffix (fields are listed in this order).
_CUSTOMER_INFO_FIELDS: tuple[str, ...] = (
    "customer_name",
    "customer_email",
    "customer_phone",
    "address",
    "job_code",
    "notes",
)


def _tool_update_customer_info(
    project_id: str,
    *,
    customer_name: str | None = None,
    customer_email: str | None = None,
    customer_phone: str | None = None,
    address: str | None = None,
    job_code: str | None = None,
    notes: str | None = None,
    dry_run: bool = False,
) -> tuple[str, float]:
    """Update customer-info fields on home_builder.project.

    Only fields explicitly passed (not None) are written. Empty-string
    values are rejected — to intentionally clear a field, the caller
    should pass null (Python None). Anthropic tool inputs distinguish
    "absent" (key not in dict, default None on this side) from "present
    but empty" (key present, value ""), so we can enforce the rule
    without ambiguity.

    Per ADR 2026-05-11 (Postgres-canonical home-builder project state),
    this is the replacement for Connor's manual Tracker Project Info
    edits.
    """
    if not project_id or not str(project_id).strip():
        return "update_customer_info: project_id is required.", 0.0

    # Collect provided fields. Critical contract: caller passes None for
    # "I didn't touch this"; caller passes "" only as a mistake — reject.
    provided: dict[str, str | None] = {}
    raw_inputs = {
        "customer_name": customer_name,
        "customer_email": customer_email,
        "customer_phone": customer_phone,
        "address": address,
        "job_code": job_code,
        "notes": notes,
    }
    for fname, val in raw_inputs.items():
        if val is None:
            continue  # absent → leave DB row alone
        if isinstance(val, str) and val == "":
            return (
                f"update_customer_info: {fname} was empty — to clear a "
                "field, send null explicitly; empty looks like a typo.",
                0.0,
            )
        provided[fname] = val

    if not provided:
        return (
            "update_customer_info: at least one of customer_name, "
            "customer_email, customer_phone, address, job_code, or notes "
            "must be provided.",
            0.0,
        )

    # Validation.
    if "customer_email" in provided:
        email_val = provided["customer_email"]
        if not _EMAIL_RE.match(email_val.strip()):
            return (
                f"update_customer_info: customer_email "
                f"{email_val!r} doesn't look like an email "
                "(expecting something@something.tld).",
                0.0,
            )
        provided["customer_email"] = email_val.strip()

    if "customer_phone" in provided:
        phone_val = provided["customer_phone"]
        normalized = _normalize_phone(phone_val)
        if normalized is None:
            return (
                f"update_customer_info: customer_phone "
                f"{phone_val!r} doesn't have 10 digits. Accepted: "
                "'(251) 555-0142', '251-555-0142', '2515550142'.",
                0.0,
            )
        provided["customer_phone"] = normalized

    if dry_run:
        fields_str = ", ".join(sorted(provided.keys()))
        return (
            f"(dry-run) Would update {fields_str} on project "
            f"{project_id[:8]}…",
            0.0,
        )

    # Lazy import — keep cold-start fast and avoid pulling psycopg into
    # tooling that doesn't need it.
    try:
        from home_builder_agent.integrations.postgres import connection
    except Exception as e:
        return (
            f"update_customer_info: import failed — {type(e).__name__}: {e}",
            0.0,
        )

    set_clauses = ", ".join(f"{f} = %s" for f in provided.keys())
    params: list = list(provided.values())
    params.append(project_id)

    try:
        with connection(application_name="hb-chad-update-customer-info") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE home_builder.project
                    SET {set_clauses}
                    WHERE id = %s::uuid
                    RETURNING name
                    """,
                    tuple(params),
                )
                row = cur.fetchone()
                if not row:
                    return (
                        f"update_customer_info: no project matched id "
                        f"{project_id[:8]}…",
                        0.0,
                    )
                project_name_db = row["name"]
    except Exception as e:
        return (
            f"update_customer_info: DB write failed — "
            f"{type(e).__name__}: {e}",
            0.0,
        )

    # Chad-voice confirmation — concrete, status-led, no tool names.
    # List the updated fields in canonical order for readability.
    in_order = [f for f in _CUSTOMER_INFO_FIELDS if f in provided]
    pretty = {
        "customer_name": "name",
        "customer_email": "email",
        "customer_phone": "phone",
        "address": "address",
        "job_code": "job code",
        "notes": "notes",
    }
    label_list = [pretty[f] for f in in_order]
    if len(label_list) == 1:
        labels = label_list[0]
    elif len(label_list) == 2:
        labels = f"{label_list[0]} + {label_list[1]}"
    else:
        labels = ", ".join(label_list[:-1]) + f", + {label_list[-1]}"

    summary = (
        f"Updated {project_name_db} — {labels} on file."
        f"\n\n[updated_fields: {in_order}]"
    )
    return summary, 0.0


def _resolve_phase_for_project(
    cur,
    project_id: str,
    *,
    phase_sequence_index: int | None,
    phase_name: str | None,
) -> tuple[dict | None, list[dict], str | None]:
    """Look up a single phase row inside an open cursor.

    Returns (row, candidates, error_message):
      - row: the matched phase row dict, or None
      - candidates: when phase_name matched >1 row, the candidate list
        (used to compose an "ambiguous" error). Empty otherwise.
      - error_message: a Chad-voice error string when resolution fails,
        else None. If error_message is set, row will be None.

    All callers must be inside a transaction (so the SELECT and any
    subsequent UPDATE see a consistent snapshot).
    """
    if phase_sequence_index is None and not phase_name:
        return (
            None,
            [],
            "phase_sequence_index or phase_name is required to identify the phase.",
        )
    if phase_sequence_index is not None and phase_name:
        return (
            None,
            [],
            "pass phase_sequence_index OR phase_name — not both.",
        )

    if phase_sequence_index is not None:
        cur.execute(
            """
            SELECT id::text AS id, name, sequence_index,
                   planned_start_date, planned_end_date
            FROM home_builder.phase
            WHERE project_id = %s::uuid
              AND sequence_index = %s
            """,
            (project_id, phase_sequence_index),
        )
        row = cur.fetchone()
        if not row:
            return (
                None,
                [],
                f"no phase at position {phase_sequence_index} on this project.",
            )
        return row, [], None

    # phase_name lookup — ILIKE substring, up to 5 candidates surfaced.
    cur.execute(
        """
        SELECT id::text AS id, name, sequence_index,
               planned_start_date, planned_end_date
        FROM home_builder.phase
        WHERE project_id = %s::uuid
          AND name ILIKE %s
        ORDER BY sequence_index ASC
        LIMIT 5
        """,
        (project_id, f"%{phase_name}%"),
    )
    rows = list(cur.fetchall())
    if not rows:
        return (
            None,
            [],
            f"no phase on this project matches {phase_name!r}.",
        )
    if len(rows) > 1:
        return None, rows, "ambiguous phase_name — multiple matches."
    return rows[0], [], None


def _tool_update_schedule_date(
    project_id: str,
    *,
    phase_sequence_index: int | None = None,
    phase_name: str | None = None,
    planned_start_date: str | None = None,
    planned_end_date: str | None = None,
    dry_run: bool = False,
) -> tuple[str, float]:
    """Update planned_start_date and/or planned_end_date on a single phase.

    Cascade-aware: if end_date moves later and pushes against the next
    phase's planned_start_date, we INCLUDE a warning in the response.
    We do NOT auto-cascade — Chad has to make that call explicitly.
    """
    if not project_id or not str(project_id).strip():
        return "update_schedule_date: project_id is required.", 0.0
    if planned_start_date is None and planned_end_date is None:
        return (
            "update_schedule_date: at least one of planned_start_date or "
            "planned_end_date is required.",
            0.0,
        )

    new_start = _parse_iso_date(planned_start_date)
    new_end = _parse_iso_date(planned_end_date)
    if new_start is _DATE_PARSE_ERROR:
        return (
            f"update_schedule_date: planned_start_date "
            f"{planned_start_date!r} is not a valid YYYY-MM-DD.",
            0.0,
        )
    if new_end is _DATE_PARSE_ERROR:
        return (
            f"update_schedule_date: planned_end_date "
            f"{planned_end_date!r} is not a valid YYYY-MM-DD.",
            0.0,
        )

    try:
        from home_builder_agent.integrations.postgres import connection
    except Exception as e:
        return (
            f"update_schedule_date: import failed — {type(e).__name__}: {e}",
            0.0,
        )

    try:
        with connection(application_name="hb-chad-update-schedule-date") as conn:
            with conn.cursor() as cur:
                row, candidates, err = _resolve_phase_for_project(
                    cur,
                    project_id,
                    phase_sequence_index=phase_sequence_index,
                    phase_name=phase_name,
                )
                if err:
                    if candidates:
                        opts = ", ".join(
                            f"{c['name']} (#{c['sequence_index']})"
                            for c in candidates
                        )
                        return (
                            f"update_schedule_date: ambiguous phase_name "
                            f"{phase_name!r}. Candidates: {opts}. Pass "
                            "phase_sequence_index to disambiguate.",
                            0.0,
                        )
                    return f"update_schedule_date: {err}", 0.0

                old_start = row["planned_start_date"]
                old_end = row["planned_end_date"]
                effective_start = new_start if new_start is not None else old_start
                effective_end = new_end if new_end is not None else old_end

                if (
                    effective_start is not None
                    and effective_end is not None
                    and effective_end < effective_start
                ):
                    return (
                        f"update_schedule_date: end date "
                        f"{effective_end} is before start date "
                        f"{effective_start}. Refusing to write inverted "
                        "range.",
                        0.0,
                    )

                # Cascade check: if we changed end_date, look at the next
                # phase. Warn if its current planned_start_date is now
                # earlier than effective_end (the next phase would
                # nominally start before this one wraps).
                cascade_warning: str | None = None
                if new_end is not None:
                    cur.execute(
                        """
                        SELECT id::text AS id, name, sequence_index,
                               planned_start_date
                        FROM home_builder.phase
                        WHERE project_id = %s::uuid
                          AND sequence_index > %s
                        ORDER BY sequence_index ASC
                        LIMIT 1
                        """,
                        (project_id, row["sequence_index"]),
                    )
                    next_row = cur.fetchone()
                    if (
                        next_row
                        and next_row.get("planned_start_date") is not None
                        and effective_end is not None
                        and next_row["planned_start_date"] < effective_end
                    ):
                        cascade_warning = (
                            f"next phase '{next_row['name']}' "
                            f"(#{next_row['sequence_index']}) currently "
                            f"starts {next_row['planned_start_date']} — "
                            f"that's earlier than this phase's new end "
                            f"{effective_end}. Did NOT auto-shift "
                            "downstream phases."
                        )

                if dry_run:
                    return (
                        f"(dry-run) Would update {row['name']} "
                        f"(#{row['sequence_index']}): "
                        f"start {old_start}→{effective_start}, "
                        f"end {old_end}→{effective_end}.",
                        0.0,
                    )

                # Build the SET clause from only the fields the caller
                # actually passed; never null-out unprovided fields.
                set_parts: list[str] = []
                params: list = []
                if new_start is not None:
                    set_parts.append("planned_start_date = %s")
                    params.append(new_start)
                if new_end is not None:
                    set_parts.append("planned_end_date = %s")
                    params.append(new_end)
                params.append(row["id"])

                cur.execute(
                    f"""
                    UPDATE home_builder.phase
                    SET {', '.join(set_parts)}
                    WHERE id = %s::uuid
                    """,
                    tuple(params),
                )
    except Exception as e:
        return (
            f"update_schedule_date: DB write failed — "
            f"{type(e).__name__}: {e}",
            0.0,
        )

    # Chad-voice confirmation.
    parts: list[str] = []
    if new_start is not None:
        parts.append(f"start {old_start}→{effective_start}")
    if new_end is not None:
        parts.append(f"end {old_end}→{effective_end}")
    change_str = ", ".join(parts)

    body = (
        f"{row['name']} (#{row['sequence_index']}): {change_str}."
    )
    if cascade_warning:
        body += f" Heads up — {cascade_warning}"

    structured = {
        "phase_name": row["name"],
        "sequence_index": row["sequence_index"],
        "old_dates": {
            "planned_start_date": str(old_start) if old_start else None,
            "planned_end_date": str(old_end) if old_end else None,
        },
        "new_dates": {
            "planned_start_date": str(effective_start) if effective_start else None,
            "planned_end_date": str(effective_end) if effective_end else None,
        },
        "cascade_warning": cascade_warning,
    }
    return f"{body}\n\n[update_schedule_date: {structured}]", 0.0


def _tool_reorder_phase(
    project_id: str,
    new_position: int,
    *,
    phase_sequence_index: int | None = None,
    phase_name: str | None = None,
    dry_run: bool = False,
) -> tuple[str, float]:
    """Move a single phase to a new position.

    Park-and-swap pattern wrapped in the transaction the postgres
    `connection()` context manager already gives us (autocommit off
    by default, commits on clean exit, rolls back on exception):

      1. resolve moving_id + old_position from inputs
      2. validate new_position is in [1, max_seq_index_for_project]
      3. park the moving row at sequence_index = 0
      4. shift the rows in between by ±1, direction-aware
      5. land the moving row at new_position

    The single-cursor / single-transaction execution + sentinel parking
    is what makes this safe under the
    `UNIQUE (project_id, sequence_index)` constraint without needing a
    schema-level DEFERRABLE. ADR 2026-05-11 lists it as the prescribed
    approach.

    Migration 012 (2026-05-11) widened the table's CHECK constraint
    from BETWEEN 1 AND 24 to BETWEEN 0 AND 24 so that 0 is a legal
    transient sentinel. Without that, the park step would raise per
    Postgres's per-statement CHECK enforcement (CHECK is not
    deferrable). Final state of any committed transaction still
    lands in [1, 24].
    """
    if not project_id or not str(project_id).strip():
        return "reorder_phase: project_id is required.", 0.0
    if not isinstance(new_position, int):
        return (
            f"reorder_phase: new_position must be an integer "
            f"(got {type(new_position).__name__}).",
            0.0,
        )
    if new_position < 1:
        return (
            f"reorder_phase: new_position must be ≥ 1 (got {new_position}).",
            0.0,
        )

    try:
        from home_builder_agent.integrations.postgres import connection
    except Exception as e:
        return (
            f"reorder_phase: import failed — {type(e).__name__}: {e}",
            0.0,
        )

    try:
        with connection(application_name="hb-chad-reorder-phase") as conn:
            with conn.cursor() as cur:
                row, candidates, err = _resolve_phase_for_project(
                    cur,
                    project_id,
                    phase_sequence_index=phase_sequence_index,
                    phase_name=phase_name,
                )
                if err:
                    if candidates:
                        opts = ", ".join(
                            f"{c['name']} (#{c['sequence_index']})"
                            for c in candidates
                        )
                        return (
                            f"reorder_phase: ambiguous phase_name "
                            f"{phase_name!r}. Candidates: {opts}. Pass "
                            "phase_sequence_index to disambiguate.",
                            0.0,
                        )
                    return f"reorder_phase: {err}", 0.0

                old_position = row["sequence_index"]
                moving_id = row["id"]

                # Validate new_position is within the project's existing
                # range — no gaps, no above-max moves.
                cur.execute(
                    """
                    SELECT MAX(sequence_index) AS max_idx
                    FROM home_builder.phase
                    WHERE project_id = %s::uuid
                    """,
                    (project_id,),
                )
                max_row = cur.fetchone()
                max_idx = (max_row or {}).get("max_idx") or 0
                if new_position > max_idx:
                    return (
                        f"reorder_phase: new_position {new_position} is "
                        f"above the project's highest phase position "
                        f"({max_idx}). Don't create a gap — pick a "
                        f"position in [1, {max_idx}].",
                        0.0,
                    )

                if new_position == old_position:
                    # No-op — don't touch the DB at all.
                    return (
                        f"{row['name']} is already at position "
                        f"{old_position}. No change."
                        f"\n\n[reorder_phase: "
                        f"{{'phase_name': {row['name']!r}, "
                        f"'old_position': {old_position}, "
                        f"'new_position': {new_position}, "
                        f"'shifted_phases': []}}]",
                        0.0,
                    )

                if dry_run:
                    return (
                        f"(dry-run) Would move {row['name']} from "
                        f"position {old_position} → {new_position}.",
                        0.0,
                    )

                # Park the moving row at sequence_index = 0, the
                # CHECK-constraint-legal parking sentinel.
                #
                # CTO review 2026-05-11 caught that an earlier draft
                # used -1 as the sentinel based on a wrong assumption
                # about Postgres CHECK constraint deferral. CHECK
                # constraints are NOT deferrable in Postgres — they
                # fire per-statement, not at COMMIT. So `-1` would
                # have raised the moment the UPDATE ran. Migration
                # 012 widened the constraint from BETWEEN 1 AND 24 to
                # BETWEEN 0 AND 24 so that 0 is a legal transient
                # sentinel; final state of any committed transaction
                # still lands in [1, 24].
                #
                # The UNIQUE (project_id, sequence_index) constraint
                # has no row at 0 to collide with — the parking row
                # is alone in that slot, then we shift the in-between
                # phases, then land the parked row at new_position.
                cur.execute(
                    """
                    UPDATE home_builder.phase
                    SET sequence_index = 0
                    WHERE id = %s::uuid
                    """,
                    (moving_id,),
                )

                # Shift the in-between rows.
                if new_position > old_position:
                    # Moving down — phases between old+1 and new shift up by 1
                    cur.execute(
                        """
                        UPDATE home_builder.phase
                        SET sequence_index = sequence_index - 1
                        WHERE project_id = %s::uuid
                          AND sequence_index BETWEEN %s AND %s
                          AND id != %s::uuid
                        """,
                        (project_id, old_position + 1, new_position, moving_id),
                    )
                else:
                    # Moving up — phases between new and old-1 shift down by 1
                    cur.execute(
                        """
                        UPDATE home_builder.phase
                        SET sequence_index = sequence_index + 1
                        WHERE project_id = %s::uuid
                          AND sequence_index BETWEEN %s AND %s
                          AND id != %s::uuid
                        """,
                        (project_id, new_position, old_position - 1, moving_id),
                    )
                shifted_count = cur.rowcount

                # Land the moving row.
                cur.execute(
                    """
                    UPDATE home_builder.phase
                    SET sequence_index = %s
                    WHERE id = %s::uuid
                    """,
                    (new_position, moving_id),
                )
    except Exception as e:
        return (
            f"reorder_phase: DB write failed — {type(e).__name__}: {e}",
            0.0,
        )

    direction = "down" if new_position > old_position else "up"
    body = (
        f"Moved {row['name']}: position {old_position} → "
        f"{new_position} ({direction}). {shifted_count} other phase"
        f"{'s' if shifted_count != 1 else ''} shifted to make room."
    )
    structured = {
        "phase_name": row["name"],
        "old_position": old_position,
        "new_position": new_position,
        "shifted_phases": shifted_count,
    }
    return f"{body}\n\n[reorder_phase: {structured}]", 0.0


def _tool_write_to_drive(
    folder_id: str,
    file_name: str,
    content: str,
    mime_type: str = "text/markdown",
    *,
    dry_run: bool = False,
) -> tuple[str, float]:
    """Write a new file to a Google Drive folder.

    Wraps integrations.drive.upload_binary_file with input validation
    and a Chad-voice confirmation. Creates a new file every time —
    never overwrites or deletes. If the folder already contains a file
    with the same name, Drive will create a second one (Drive itself
    allows name collisions; the file_id is what's unique).

    Returns (confirmation_text, cost_usd_=0). cost is 0 because this
    is a direct Drive API call, not a Claude API call.
    """
    # ---- validation -------------------------------------------------
    if not folder_id or not folder_id.strip():
        return "write_to_drive: folder_id is required.", 0.0
    if not file_name or not file_name.strip():
        return "write_to_drive: file_name is required.", 0.0
    if content is None or content == "":
        return (
            "write_to_drive: content is empty — refusing to write a "
            "zero-byte file. If you intended an empty file, pass a "
            "single space.",
            0.0,
        )
    if not mime_type:
        mime_type = "text/markdown"

    if dry_run:
        size_kb = len(content.encode("utf-8")) / 1024
        return (
            f"(dry-run) Would write {file_name!r} ({size_kb:.1f} KB, "
            f"{mime_type}) to Drive folder {folder_id[:12]}…",
            0.0,
        )

    # ---- imports + auth (lazy) --------------------------------------
    try:
        from home_builder_agent.core.auth import get_credentials
        from home_builder_agent.integrations.drive import (
            drive_service,
            upload_binary_file,
        )
    except Exception as e:
        return (
            f"write_to_drive: import failed — {type(e).__name__}: {e}",
            0.0,
        )

    try:
        creds = get_credentials()
        svc = drive_service(creds)
    except Exception as e:
        return (
            f"write_to_drive: Google auth failed — {type(e).__name__}: "
            f"{e}. Check credentials.json + token.json on the Mac Mini.",
            0.0,
        )

    # ---- write ------------------------------------------------------
    try:
        result = upload_binary_file(
            svc,
            file_bytes=content.encode("utf-8"),
            file_name=file_name,
            mime_type=mime_type,
            parent_folder_id=folder_id,
        )
    except Exception as e:
        return (
            f"write_to_drive: upload failed — {type(e).__name__}: {e}",
            0.0,
        )

    size_kb = len(content.encode("utf-8")) / 1024
    url = result.get("webViewLink", "(no URL returned)")
    return (
        f"Saved {file_name!r} ({size_kb:.1f} KB) to Drive.\n"
        f"URL: {url}",
        0.0,
    )


def _tool_web_fetch(url: str) -> tuple[str, float]:
    """Fetch a URL and return cleaned readable text.

    Uses stdlib only — no new dependency. urllib for the GET,
    html.parser for tag stripping. Response is capped at 50 KB
    decoded text to protect Claude's context budget. Errors are
    returned as text, never raised.

    Returns (text_or_error_message, cost_usd_=0).
    """
    import urllib.error
    import urllib.parse
    import urllib.request
    from html.parser import HTMLParser

    MAX_BYTES_FETCH = 1_000_000   # 1 MB cap on raw download
    MAX_CHARS_RETURN = 50 * 1024  # 50 KB cap on returned text
    TIMEOUT_SEC = 10

    # ---- validation -------------------------------------------------
    if not url or not isinstance(url, str):
        return "web_fetch: url is required (string).", 0.0
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return (
            f"web_fetch: only http/https URLs are supported. Got "
            f"scheme={parsed.scheme!r}.",
            0.0,
        )
    if not parsed.netloc:
        return f"web_fetch: malformed URL (no host): {url!r}", 0.0

    # ---- fetch ------------------------------------------------------
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "home-builder-agent/0.2 (Palmetto Custom Homes; "
                "+ Patton AI)"
            ),
            "Accept": "text/html,text/plain,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read(MAX_BYTES_FETCH + 1)
            truncated_raw = len(raw) > MAX_BYTES_FETCH
            raw = raw[:MAX_BYTES_FETCH]
    except urllib.error.HTTPError as e:
        return (
            f"web_fetch: HTTP {e.code} from {parsed.netloc} "
            f"({e.reason})",
            0.0,
        )
    except urllib.error.URLError as e:
        return f"web_fetch: connection error — {e.reason}", 0.0
    except Exception as e:
        return (
            f"web_fetch: unexpected error — {type(e).__name__}: {e}",
            0.0,
        )

    # ---- non-HTML short-circuit -------------------------------------
    if "html" not in content_type and "xml" not in content_type:
        if content_type.startswith("text/"):
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = raw.decode("latin-1", errors="replace")
        else:
            return (
                f"web_fetch: non-text content "
                f"({content_type or 'unknown type'}, {len(raw)} bytes). "
                f"web_fetch only returns readable text.",
                0.0,
            )
    else:
        # ---- HTML → text ---------------------------------------------
        try:
            html = raw.decode("utf-8", errors="replace")
        except Exception:
            html = raw.decode("latin-1", errors="replace")

        class _TextExtractor(HTMLParser):
            """Strip tags + collapse whitespace.

            Skips <script>, <style>, <head>, <noscript> blocks. Adds
            line breaks on block-level tags so the output isn't one wall.
            """
            SKIP = {"script", "style", "head", "noscript", "svg", "iframe"}
            BLOCK = {
                "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4",
                "h5", "h6", "section", "article", "header", "footer",
                "nav", "aside", "blockquote", "pre",
            }

            def __init__(self) -> None:
                super().__init__()
                self.parts: list[str] = []
                self.skip_depth = 0

            def handle_starttag(self, tag, attrs):
                if tag in self.SKIP:
                    self.skip_depth += 1
                elif tag in self.BLOCK:
                    self.parts.append("\n")

            def handle_endtag(self, tag):
                if tag in self.SKIP and self.skip_depth > 0:
                    self.skip_depth -= 1
                elif tag in self.BLOCK:
                    self.parts.append("\n")

            def handle_data(self, data):
                if self.skip_depth == 0 and data.strip():
                    self.parts.append(data)

        parser = _TextExtractor()
        try:
            parser.feed(html)
        except Exception as e:
            return (
                f"web_fetch: HTML parse failed — {type(e).__name__}: {e}",
                0.0,
            )

        text = "".join(parser.parts)
        # Collapse runs of whitespace, but preserve paragraph breaks
        import re as _re
        text = _re.sub(r"[ \t]+", " ", text)
        text = _re.sub(r"\n[ \t]+", "\n", text)
        text = _re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()

    # ---- cap return size --------------------------------------------
    if not text:
        return (
            f"web_fetch: fetched {parsed.netloc} but extracted no "
            f"readable text (page may be JS-rendered or empty).",
            0.0,
        )

    truncated_text = len(text) > MAX_CHARS_RETURN
    if truncated_text:
        text = text[:MAX_CHARS_RETURN]

    notes = []
    if truncated_raw:
        notes.append(f"raw download capped at {MAX_BYTES_FETCH} bytes")
    if truncated_text:
        notes.append(f"output capped at {MAX_CHARS_RETURN} chars")
    note_line = (
        f"\n\n[web_fetch: {parsed.netloc} • {len(text)} chars"
        + (f" • {'; '.join(notes)}" if notes else "")
        + "]"
    )

    return (text + note_line, 0.0)


def _tool_create_email_draft(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
) -> tuple[str, float]:
    """Create a Gmail draft (does not send).

    Wraps the existing Gmail integration and adds optional CC support.
    Uses the existing gmail.compose OAuth scope already granted in
    config.GOOGLE_SCOPES — does NOT request new scopes.

    Per Patton AI's outbound-comms rule (CLAUDE.md): drafts only.
    Sending requires explicit Chad action in Gmail itself.

    Returns (confirmation_text_with_url, cost_usd_=0).
    """
    # ---- validation -------------------------------------------------
    if not to or "@" not in (to or ""):
        return (
            f"create_email_draft: 'to' must be a valid email address. "
            f"Got {to!r}.",
            0.0,
        )
    if not subject or not subject.strip():
        return "create_email_draft: subject is required.", 0.0
    if not body or not body.strip():
        return "create_email_draft: body is required.", 0.0
    if cc is not None and cc.strip() and "@" not in cc:
        return (
            f"create_email_draft: 'cc' must be a valid email address "
            f"or omitted. Got {cc!r}.",
            0.0,
        )

    # ---- imports + auth (lazy) --------------------------------------
    try:
        from home_builder_agent.core.auth import get_credentials
        from home_builder_agent.integrations.gmail import gmail_service
    except Exception as e:
        return (
            f"create_email_draft: import failed — {type(e).__name__}: {e}",
            0.0,
        )

    try:
        creds = get_credentials()
        svc = gmail_service(creds)
    except Exception as e:
        return (
            f"create_email_draft: Google auth failed — "
            f"{type(e).__name__}: {e}. Check credentials.json + "
            f"token.json on the Mac Mini.",
            0.0,
        )

    # ---- build MIME message inline (the existing gmail.create_draft
    # ---- helper doesn't expose a cc parameter; we attach it here) --
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    # If body looks like HTML, send as HTML; otherwise as plain text.
    is_html = "<" in body and ">" in body and (
        "<p" in body.lower() or "<br" in body.lower()
        or "<div" in body.lower() or "<html" in body.lower()
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"] = to
    if cc and cc.strip():
        msg["Cc"] = cc.strip()

    if is_html:
        # Plain-text fallback derived from HTML
        import re as _re
        text_fallback = _re.sub(r"<[^>]+>", "", body)
        text_fallback = _re.sub(r"\n{3,}", "\n\n", text_fallback).strip()
        msg.attach(MIMEText(text_fallback, "plain", "utf-8"))
        msg.attach(MIMEText(body, "html", "utf-8"))
    else:
        msg.attach(MIMEText(body, "plain", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        draft = svc.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()
    except Exception as e:
        return (
            f"create_email_draft: Gmail API call failed — "
            f"{type(e).__name__}: {e}",
            0.0,
        )

    message_id = (draft.get("message") or {}).get("id", "")
    # Gmail draft URL format — opens directly in the compose view.
    draft_url = (
        f"https://mail.google.com/mail/u/0/#drafts/{message_id}"
        if message_id else
        f"https://mail.google.com/mail/u/0/#drafts"
    )

    cc_line = f"\nCC:      {cc.strip()}" if cc and cc.strip() else ""
    return (
        f"Draft created (not sent).\n"
        f"To:      {to}{cc_line}\n"
        f"Subject: {subject}\n"
        f"Open in Gmail: {draft_url}\n"
        f"(Review and click Send in Gmail when ready.)",
        0.0,
    )


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


def _tool_save_profile_fact(field: str, value: str) -> tuple[str, float]:
    """Save a single profile field. Used during the onboarding interview.

    Writes ~/.hb-profile-proposed.json, increments onboarding.current_step,
    and sets onboarding.complete=true when step hits 10. Returns
    (status_text, cost_usd=0) — no Claude call here, just disk I/O.
    """
    valid_fields = {f for f, _ in ONBOARDING_QUESTIONS}
    if field not in valid_fields:
        return (
            f"save_profile_fact: unknown field {field!r}. Valid: "
            f"{sorted(valid_fields)}",
            0.0,
        )

    # Clamp to 500 chars even though the prompt asks for 200 — defense
    # against a chatty model bloating the profile.
    value = (value or "").strip()[:500]
    if not value:
        return ("save_profile_fact: empty value, not saved.", 0.0)

    try:
        data = (
            json.loads(_PROFILE_PATH.read_text())
            if _PROFILE_PATH.exists()
            else {}
        )
    except Exception as e:
        return (
            f"save_profile_fact: profile read failed — "
            f"{type(e).__name__}: {e}",
            0.0,
        )

    data[field] = value
    ob = data.setdefault("onboarding", {"complete": False, "current_step": 0})
    ob["current_step"] = min(int(ob.get("current_step", 0)) + 1, 10)
    if ob["current_step"] >= 10:
        ob["complete"] = True

    try:
        _PROFILE_PATH.write_text(json.dumps(data, indent=2) + "\n")
    except Exception as e:
        return (
            f"save_profile_fact: profile write failed — "
            f"{type(e).__name__}: {e}",
            0.0,
        )

    status = "complete" if ob["complete"] else "in progress"
    return (
        f"Saved {field!r}. Onboarding {ob['current_step']}/10 ({status}).",
        0.0,
    )


def _int_attr(obj, name: str) -> int:
    """Read an int attribute, returning 0 when missing or non-numeric.

    Anthropic Usage objects expose cache_creation_input_tokens /
    cache_read_input_tokens as ints (zero when caching wasn't engaged).
    But test fixtures and older SDK versions may auto-create those
    attributes as MagicMock — which is truthy and would poison the
    arithmetic chain below. Coerce defensively so the cost code never
    flows non-numeric values into model_cost / SQLite.
    """
    val = getattr(obj, name, 0) or 0
    return val if isinstance(val, int) else 0


# ---------------------------------------------------------------------------
# Trivial-input early return — skip the Opus round trip on greetings, thanks,
# yes/no acknowledgments. Each match returns a canned reply at $0 cost. Long
# inputs, anything with a question mark, or anything beyond the curated
# patterns always falls through to the real chad_turn loop.
# ---------------------------------------------------------------------------

import random as _random
import re as _re


_TRIVIAL_INPUT_PATTERNS: list[tuple[_re.Pattern[str], list[str]]] = [
    (_re.compile(
        r"^(hi+|hey+|hello+|howdy+|yo+|sup|"
        r"good morning|good afternoon|good evening|"
        r"morning|afternoon|evening)[!.\s]*$",
        _re.IGNORECASE,
    ), [
        "Hey Chad — what's up?",
        "Hey. What's on your mind?",
        "Howdy. Need anything?",
    ]),
    (_re.compile(
        r"^(thanks|thank you|thx|ty|"
        r"appreciate it|much appreciated|"
        r"thanks man|thanks chad)[!.\s]*$",
        _re.IGNORECASE,
    ), [
        "Anytime.",
        "You got it.",
        "Glad I could help.",
    ]),
    (_re.compile(
        r"^(yes|yep|yeah|ok|okay|sure|sounds good|"
        r"got it|k|alright|cool|nice|right|copy|copy that|"
        r"perfect|good deal)[!.\s]*$",
        _re.IGNORECASE,
    ), [
        "Got it.",
        "Standing by.",
        "On standby — holler when you need me.",
    ]),
    (_re.compile(
        r"^(no|nope|nah|not really|never mind|nm|"
        r"hold off|forget it)[!.\s]*$",
        _re.IGNORECASE,
    ), [
        "Standing by.",
        "Got it — no action.",
        "Understood.",
    ]),
]


def _trivial_reply(user_input: str) -> str | None:
    """Return a canned Chad-voice reply for conversational filler, or None.

    Triggers only for short non-question inputs that match the curated
    greeting / thanks / yes / no patterns. Anything ambiguous falls through
    to the real chad_turn loop. Saves the static system + tools input cost
    on turns that don't need any reasoning at all.
    """
    s = user_input.strip()
    if not s or len(s) > 30 or "?" in s:
        return None
    for pattern, replies in _TRIVIAL_INPUT_PATTERNS:
        if pattern.match(s):
            return _random.choice(replies)
    return None


# ---------------------------------------------------------------------------
# The agent loop
# ---------------------------------------------------------------------------

def chad_turn(
    user_input: str,
    *,
    conversation_id: str | None = None,
    images: list[ImageInput] | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
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

    When ``conversation_id`` is supplied, prior turns + a rolling summary
    are loaded from SQLite and injected into the prompt. After a
    successful (or graceful-error) turn, the user input and the
    assistant's final answer are persisted via ``conversation_store``.
    Pruning runs on write; see ``conversation_store.prune``.

    When ``images`` are supplied, each is attached to the new user
    message as a Claude vision content block.

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

    # ─── Trivial-input free path ──────────────────────────────────────────
    # Greetings, thanks, yes/no, "got it" — answer with a canned Chad-voice
    # reply and skip the API entirely. iOS still gets the full chad_turn
    # success shape (answer + cost_usd=0 + iterations=0). The audit entry
    # in .cost_log.jsonl tags these as model="trivial" so they're visible
    # but don't move the daily caps.
    canned = _trivial_reply(user_input)
    if canned:
        if conversation_id:
            try:
                conversation_store.get_or_create(
                    conversation_id, user_id=user_id, project_id=project_id
                )
                conversation_store.append_message(
                    conversation_id, "user", user_input,
                    user_id=user_id, project_id=project_id,
                )
                conversation_store.append_message(
                    conversation_id, "assistant", canned,
                    tool_log=[],
                    actions_taken=[],
                    cost_usd=0.0,
                    user_id=user_id, project_id=project_id,
                )
            except Exception as e:
                print(
                    f"[chad_turn] memory write failed for {conversation_id}: "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )
        record_cost(
            agent="hb-chad",
            model="trivial",
            cost_usd=0.0,
            note="canned reply (trivial input)",
        )
        return {
            "input": user_input,
            "answer": canned,
            "actions_taken": [],
            "tool_log": [],
            "model": "trivial",
            "model_fallback_used": False,
            "fallback_reason": None,
            "cost_usd": 0.0,
            "opus_cost_usd": 0.0,
            "sonnet_cost_usd": 0.0,
            "downstream_cost_usd": 0.0,
            "duration_ms": int((time.time() - started_at) * 1000),
            "iterations": 0,
        }

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

    # ─── Memory injection (conversation_id) ────────────────────────────────
    # Load prior turns + rolling summary from SQLite if a conversation_id
    # was supplied. The route forwards this from iOS; the engine treats
    # it as a soft hint — no error if it's missing or unknown.
    prior_messages: list[dict] = []
    if conversation_id:
        try:
            conversation_store.get_or_create(
                conversation_id, user_id=user_id, project_id=project_id
            )
            recent = conversation_store.load_recent_turns(conversation_id, n=16)
            prior_messages = _prior_turns_as_messages(recent)
            summary = conversation_store.get_summary(conversation_id)
            system = _system_with_summary(system, summary)
        except Exception as e:
            # Memory load is best-effort. If SQLite is unavailable we
            # continue with a stateless turn and warn loudly.
            print(
                f"[chad_turn] memory load failed for {conversation_id}: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

    new_user_content = _build_user_content(user_input, images)
    messages: list[dict] = [
        *prior_messages,
        {"role": "user", "content": new_user_content},
    ]
    final_text = ""
    tool_log: list[dict] = []
    actions_taken: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_write_tokens = 0
    total_cache_read_tokens = 0
    downstream_cost = 0.0
    iteration = 0  # initialize so the error path can reference it before the loop

    # Prompt caching — biggest single cost lever.
    # The system prompt (persona + project context + tool-selection guide) and
    # the TOOLS array (~600 lines of schemas) are both static across the
    # whole conversation, so we mark them for ephemeral cache and reuse
    # them on every iteration of the tool loop AND every subsequent turn
    # within the ~5-min TTL. First request pays a 1.25× write-through
    # penalty; every read after that costs 10% of the input rate.
    cached_system = cached_system_block(system)
    cached_tools = tools_with_cache(TOOLS)

    try:
        for iteration in range(MAX_TOOL_LOOP_ITERATIONS):
            if verbose:
                print(f"[iter {iteration + 1}] calling {active_model}...", file=sys.stderr)

            response = client.messages.create(
                model=active_model,
                max_tokens=CHAD_MAX_TOKENS,
                system=cached_system,
                tools=cached_tools,
                messages=messages,
                extra_headers=PROMPT_CACHING_BETA_HEADER,
            )

            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
            total_cache_write_tokens += _int_attr(response.usage, "cache_creation_input_tokens")
            total_cache_read_tokens += _int_attr(response.usage, "cache_read_input_tokens")

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
                elif name == "archive_project":
                    confirm_flag = bool(inputs.get("confirm", False))
                    output, cost = _tool_archive_project(
                        project_name=inputs.get("project_name", ""),
                        reason=inputs.get("reason"),
                        confirm=confirm_flag,
                        dry_run=dry_run,
                    )
                    # Only count as a taken action when the archive
                    # actually executed (confirm=True). Previews are
                    # read-only.
                    if confirm_flag and not dry_run:
                        actions_taken.append(
                            f"archive project "
                            f"{inputs.get('project_name', '?')!r}"
                        )
                elif name == "create_project":
                    output, cost = _tool_create_project(
                        project_name=inputs.get("project_name", ""),
                        customer_name=inputs.get("customer_name"),
                        target_completion_date=inputs.get("target_completion_date"),
                        target_framing_start_date=inputs.get("target_framing_start_date"),
                        dry_run=dry_run,
                    )
                    actions_taken.append(
                        f"create project {inputs.get('project_name', '?')!r}"
                    )
                elif name == "clone_project":
                    output, cost = _tool_clone_project(
                        copy_from=inputs.get("copy_from", ""),
                        new_name=inputs.get("new_name", ""),
                        customer_name=inputs.get("customer_name"),
                        dry_run=dry_run,
                    )
                    actions_taken.append(
                        f"clone project {inputs.get('copy_from', '?')!r} → "
                        f"{inputs.get('new_name', '?')!r}"
                    )
                elif name == "update_customer_info":
                    output, cost = _tool_update_customer_info(
                        project_id=inputs.get("project_id", ""),
                        customer_name=inputs.get("customer_name"),
                        customer_email=inputs.get("customer_email"),
                        customer_phone=inputs.get("customer_phone"),
                        address=inputs.get("address"),
                        job_code=inputs.get("job_code"),
                        notes=inputs.get("notes"),
                        dry_run=dry_run,
                    )
                    if not dry_run:
                        actions_taken.append(
                            f"updated customer info on project "
                            f"{inputs.get('project_id', '?')[:8]}…"
                        )
                elif name == "update_schedule_date":
                    output, cost = _tool_update_schedule_date(
                        project_id=inputs.get("project_id", ""),
                        phase_sequence_index=inputs.get("phase_sequence_index"),
                        phase_name=inputs.get("phase_name"),
                        planned_start_date=inputs.get("planned_start_date"),
                        planned_end_date=inputs.get("planned_end_date"),
                        dry_run=dry_run,
                    )
                    if not dry_run:
                        ident = (
                            f"#{inputs['phase_sequence_index']}"
                            if inputs.get("phase_sequence_index") is not None
                            else repr(inputs.get("phase_name", "?"))
                        )
                        actions_taken.append(
                            f"updated schedule date on phase {ident}"
                        )
                elif name == "reorder_phase":
                    output, cost = _tool_reorder_phase(
                        project_id=inputs.get("project_id", ""),
                        new_position=inputs.get("new_position", 0),
                        phase_sequence_index=inputs.get("phase_sequence_index"),
                        phase_name=inputs.get("phase_name"),
                        dry_run=dry_run,
                    )
                    if not dry_run:
                        ident = (
                            f"#{inputs['phase_sequence_index']}"
                            if inputs.get("phase_sequence_index") is not None
                            else repr(inputs.get("phase_name", "?"))
                        )
                        actions_taken.append(
                            f"reordered phase {ident} → position "
                            f"{inputs.get('new_position', '?')}"
                        )
                elif name == "write_to_drive":
                    output, cost = _tool_write_to_drive(
                        folder_id=inputs.get("folder_id", ""),
                        file_name=inputs.get("file_name", ""),
                        content=inputs.get("content", ""),
                        mime_type=inputs.get("mime_type") or "text/markdown",
                        dry_run=dry_run,
                    )
                    if not dry_run:
                        actions_taken.append(
                            f"wrote {inputs.get('file_name', '?')!r} to Drive"
                        )
                elif name == "web_fetch":
                    output, cost = _tool_web_fetch(inputs.get("url", ""))
                elif name == "create_email_draft":
                    output, cost = _tool_create_email_draft(
                        to=inputs.get("to", ""),
                        subject=inputs.get("subject", ""),
                        body=inputs.get("body", ""),
                        cc=inputs.get("cc"),
                    )
                    actions_taken.append(
                        f"drafted email to {inputs.get('to', '?')}"
                    )
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

        # Compute model cost — input + cache-write + cache-read + output.
        # With prompt caching wired, usage.input_tokens carries only the
        # uncached portion; the static system + tools chunk lands in
        # cache_creation on the first call of a TTL window (billed 1.25×)
        # and cache_read on every subsequent call (billed 0.10×, 90% off).
        if active_model == CHAD_MODEL:
            model_cost = (
                total_input_tokens * OPUS_INPUT_PER_M / 1_000_000
                + total_cache_write_tokens * OPUS_CACHE_WRITE_PER_M / 1_000_000
                + total_cache_read_tokens * OPUS_CACHE_READ_PER_M / 1_000_000
                + total_output_tokens * OPUS_OUTPUT_PER_M / 1_000_000
            )
        else:
            model_cost = (
                total_input_tokens * SONNET_INPUT_PER_M / 1_000_000
                + total_cache_write_tokens * SONNET_CACHE_WRITE_PER_M / 1_000_000
                + total_cache_read_tokens * SONNET_CACHE_READ_PER_M / 1_000_000
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

        total_cost = round(model_cost + downstream_cost, 4)

        # ─── Memory write-back ────────────────────────────────────────────
        # Persist BOTH user + assistant on a successful turn so the next
        # request sees the full prior exchange. Pruning runs inside
        # append_message — no extra call needed here.
        if conversation_id:
            try:
                conversation_store.append_message(
                    conversation_id, "user", user_input,
                    user_id=user_id, project_id=project_id,
                )
                conversation_store.append_message(
                    conversation_id, "assistant", answer,
                    tool_log=tool_log,
                    actions_taken=actions_taken,
                    cost_usd=total_cost,
                    user_id=user_id, project_id=project_id,
                )
            except Exception as e:
                # Memory write is best-effort — never block the response
                # to the caller on a SQLite write failure. Log and move on.
                print(
                    f"[chad_turn] memory write failed for {conversation_id}: "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )

        return {
            "input": user_input,
            "answer": answer,
            "actions_taken": actions_taken,
            "tool_log": tool_log,
            "model": active_model,
            "model_fallback_used": fallback_used,
            "fallback_reason": fallback_reason if fallback_used else None,
            "cost_usd": total_cost,
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
                + total_cache_write_tokens * OPUS_CACHE_WRITE_PER_M / 1_000_000
                + total_cache_read_tokens * OPUS_CACHE_READ_PER_M / 1_000_000
                + total_output_tokens * OPUS_OUTPUT_PER_M / 1_000_000
            )
        else:
            model_cost = (
                total_input_tokens * SONNET_INPUT_PER_M / 1_000_000
                + total_cache_write_tokens * SONNET_CACHE_WRITE_PER_M / 1_000_000
                + total_cache_read_tokens * SONNET_CACHE_READ_PER_M / 1_000_000
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
    conversation_id: str | None = None,
    images: list[ImageInput] | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> Iterator[tuple[int, str, dict]]:
    """Streaming version of chad_turn for the iOS Ask tab and other SSE surfaces.

    Yields (event_id, event_type, payload) tuples. Event IDs are monotonic
    per stream, starting at 1. Stream is terminated by either a
    `message_complete` or an `error` event — caller breaks out of iteration
    after either.

    Wire-compatible with ask_question_stream: the iOS backend can call
    either and the same SSE event handler works.

    When ``conversation_id`` is supplied, the last 8 turns + a rolling
    summary are loaded from SQLite and prepended to the request. After
    a successful ``message_complete``, both the user input and the
    assistant answer are written back to the store; pruning runs on
    write per the 2026-05-09 ADR (token-bloat ceiling).

    When ``images`` are supplied (validated upstream by the route — JPEG/
    PNG only, size + count caps enforced there), each is attached to
    ``messages[0]`` as a Claude vision content block.

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

    # ─── Trivial-input free path ──────────────────────────────────────────
    # Mirrors chad_turn() — greetings / thanks / yes / no get a canned
    # Chad-voice reply with zero API cost. iOS sees the same SSE shape
    # (one text_delta + one message_complete) so the renderer needs no
    # special-case code.
    canned = _trivial_reply(user_input)
    if canned:
        if conversation_id:
            try:
                conversation_store.get_or_create(
                    conversation_id, user_id=user_id, project_id=project_id
                )
                conversation_store.append_message(
                    conversation_id, "user", user_input,
                    user_id=user_id, project_id=project_id,
                )
                conversation_store.append_message(
                    conversation_id, "assistant", canned,
                    tool_log=[],
                    actions_taken=[],
                    cost_usd=0.0,
                    user_id=user_id, project_id=project_id,
                )
            except Exception as e:
                print(
                    f"[chad_turn_stream] memory write failed for "
                    f"{conversation_id}: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
        record_cost(
            agent="hb-chad",
            model="trivial",
            cost_usd=0.0,
            note="canned reply (trivial input)",
        )
        yield emit("text_delta", {"delta": canned})
        yield emit("message_complete", {
            "answer": canned,
            "citations": [],
            "tools_called": [],
            "actions_taken": [],
            "model": "trivial",
            "cost_usd": 0.0,
            "opus_cost_usd": 0.0,
            "downstream_cost_usd": 0.0,
            "duration_ms": int((time.time() - started_at) * 1000),
            "input_tokens": 0,
            "output_tokens": 0,
            "iterations": 0,
            "onboarding_state": _onboarding_state(),
        })
        return

    # Setup — kept inline so setup failures surface as error events rather
    # than raising before the stream opens.
    try:
        client = make_client()
        voice = chad_voice_system("narrator")
        profile_data = _read_profile_raw()
        ctx_block = get_chad_context().to_prompt_block()
        system = (
            f"{voice}\n\n{ctx_block}\n{PERSONA_SUFFIX}\n"
            f"TODAY: {date.today().strftime('%A, %B %-d, %Y')}"
        )
        onboarding_suffix = _build_onboarding_suffix(profile_data)
        if onboarding_suffix:
            system += onboarding_suffix
    except Exception as e:
        yield emit("error", {
            "type": type(e).__name__,
            "message": f"setup failed: {e}",
        })
        return

    # ─── Memory injection (conversation_id) ────────────────────────────────
    # Mirrors chad_turn(); see that function for context. Best-effort
    # load — if SQLite is unhappy we run stateless and warn.
    prior_messages: list[dict] = []
    if conversation_id:
        try:
            conversation_store.get_or_create(
                conversation_id, user_id=user_id, project_id=project_id
            )
            recent = conversation_store.load_recent_turns(conversation_id, n=16)
            prior_messages = _prior_turns_as_messages(recent)
            summary = conversation_store.get_summary(conversation_id)
            system = _system_with_summary(system, summary)
        except Exception as e:
            print(
                f"[chad_turn_stream] memory load failed for {conversation_id}: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

    new_user_content = _build_user_content(user_input, images)
    messages: list[dict] = [
        *prior_messages,
        {"role": "user", "content": new_user_content},
    ]
    tool_log: list[dict] = []
    citations: list[dict] = []
    seen_citation_ids: set[str] = set()
    actions_taken: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_write_tokens = 0
    total_cache_read_tokens = 0
    downstream_cost = 0.0

    # Prompt caching — same pattern as chad_turn(). System prompt + TOOLS
    # array are static across the whole conversation; cache once, reuse on
    # every iteration and every subsequent turn within the ~5-min TTL.
    cached_system = cached_system_block(system)
    cached_tools = tools_with_cache(TOOLS)

    try:
        for iteration in range(MAX_TOOL_LOOP_ITERATIONS):
            if verbose:
                print(f"[stream iter {iteration + 1}] calling Opus...", file=sys.stderr)

            with client.messages.stream(
                model=CHAD_MODEL,
                max_tokens=CHAD_MAX_TOKENS,
                system=cached_system,
                tools=cached_tools,
                messages=messages,
                extra_headers=PROMPT_CACHING_BETA_HEADER,
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield emit("text_delta", {"delta": event.delta.text})

                final_message = stream.get_final_message()

            total_input_tokens += final_message.usage.input_tokens
            total_output_tokens += final_message.usage.output_tokens
            total_cache_write_tokens += _int_attr(final_message.usage, "cache_creation_input_tokens")
            total_cache_read_tokens += _int_attr(final_message.usage, "cache_read_input_tokens")

            if final_message.stop_reason == "end_turn":
                final_text = ""
                for block in final_message.content:
                    if block.type == "text":
                        final_text += block.text

                # Local var shadowed the imported opus_cost() helper in the
                # original code; renamed to model_cost so future refactors
                # can reach the helper if they want.
                model_cost = (
                    total_input_tokens * OPUS_INPUT_PER_M / 1_000_000
                    + total_cache_write_tokens * OPUS_CACHE_WRITE_PER_M / 1_000_000
                    + total_cache_read_tokens * OPUS_CACHE_READ_PER_M / 1_000_000
                    + total_output_tokens * OPUS_OUTPUT_PER_M / 1_000_000
                )
                final_answer = final_text.strip() or "(no answer produced)"
                total_cost = round(model_cost + downstream_cost, 4)

                # ─── Memory write-back ────────────────────────────────────
                # Persist BOTH user + assistant before yielding
                # message_complete so the next turn over the same
                # conversation_id sees this exchange. Pruning runs inside
                # append_message; failures are logged but never block the
                # SSE response.
                if conversation_id:
                    try:
                        conversation_store.append_message(
                            conversation_id, "user", user_input,
                            user_id=user_id, project_id=project_id,
                        )
                        conversation_store.append_message(
                            conversation_id, "assistant", final_answer,
                            tool_log=tool_log,
                            actions_taken=actions_taken,
                            cost_usd=total_cost,
                            user_id=user_id, project_id=project_id,
                        )
                    except Exception as e:
                        print(
                            f"[chad_turn_stream] memory write failed for "
                            f"{conversation_id}: {type(e).__name__}: {e}",
                            file=sys.stderr,
                        )

                yield emit("message_complete", {
                    "answer": final_answer,
                    "citations": citations,
                    "tools_called": tool_log,
                    "actions_taken": actions_taken,
                    "model": CHAD_MODEL,
                    "cost_usd": total_cost,
                    "opus_cost_usd": round(model_cost, 4),
                    "downstream_cost_usd": round(downstream_cost, 4),
                    "duration_ms": int((time.time() - started_at) * 1000),
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "iterations": iteration + 1,
                    "onboarding_state": _onboarding_state(),
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
                    elif block.name == "archive_project":
                        confirm_flag = bool(inputs.get("confirm", False))
                        output, cost = _tool_archive_project(
                            project_name=inputs.get("project_name", ""),
                            reason=inputs.get("reason"),
                            confirm=confirm_flag,
                            dry_run=dry_run,
                        )
                        # Only count as a taken action when archive
                        # actually executed (confirm=True). Previews
                        # are read-only.
                        if confirm_flag and not dry_run:
                            actions_taken.append(
                                f"archive project "
                                f"{inputs.get('project_name', '?')!r}"
                            )
                    elif block.name == "create_project":
                        output, cost = _tool_create_project(
                            project_name=inputs.get("project_name", ""),
                            customer_name=inputs.get("customer_name"),
                            target_completion_date=inputs.get("target_completion_date"),
                            target_framing_start_date=inputs.get("target_framing_start_date"),
                            dry_run=dry_run,
                        )
                        actions_taken.append(
                            f"create project "
                            f"{inputs.get('project_name', '?')!r}"
                        )
                    elif block.name == "clone_project":
                        output, cost = _tool_clone_project(
                            copy_from=inputs.get("copy_from", ""),
                            new_name=inputs.get("new_name", ""),
                            customer_name=inputs.get("customer_name"),
                            dry_run=dry_run,
                        )
                        actions_taken.append(
                            f"clone project {inputs.get('copy_from', '?')!r} "
                            f"→ {inputs.get('new_name', '?')!r}"
                        )
                    elif block.name == "update_customer_info":
                        output, cost = _tool_update_customer_info(
                            project_id=inputs.get("project_id", ""),
                            customer_name=inputs.get("customer_name"),
                            customer_email=inputs.get("customer_email"),
                            customer_phone=inputs.get("customer_phone"),
                            address=inputs.get("address"),
                            job_code=inputs.get("job_code"),
                            notes=inputs.get("notes"),
                            dry_run=dry_run,
                        )
                        if not dry_run:
                            actions_taken.append(
                                f"updated customer info on project "
                                f"{inputs.get('project_id', '?')[:8]}…"
                            )
                    elif block.name == "update_schedule_date":
                        output, cost = _tool_update_schedule_date(
                            project_id=inputs.get("project_id", ""),
                            phase_sequence_index=inputs.get("phase_sequence_index"),
                            phase_name=inputs.get("phase_name"),
                            planned_start_date=inputs.get("planned_start_date"),
                            planned_end_date=inputs.get("planned_end_date"),
                            dry_run=dry_run,
                        )
                        if not dry_run:
                            ident = (
                                f"#{inputs['phase_sequence_index']}"
                                if inputs.get("phase_sequence_index") is not None
                                else repr(inputs.get("phase_name", "?"))
                            )
                            actions_taken.append(
                                f"updated schedule date on phase {ident}"
                            )
                    elif block.name == "reorder_phase":
                        output, cost = _tool_reorder_phase(
                            project_id=inputs.get("project_id", ""),
                            new_position=inputs.get("new_position", 0),
                            phase_sequence_index=inputs.get("phase_sequence_index"),
                            phase_name=inputs.get("phase_name"),
                            dry_run=dry_run,
                        )
                        if not dry_run:
                            ident = (
                                f"#{inputs['phase_sequence_index']}"
                                if inputs.get("phase_sequence_index") is not None
                                else repr(inputs.get("phase_name", "?"))
                            )
                            actions_taken.append(
                                f"reordered phase {ident} → position "
                                f"{inputs.get('new_position', '?')}"
                            )
                    elif block.name == "write_to_drive":
                        # Streaming surface is real iOS user — never dry-run.
                        output, cost = _tool_write_to_drive(
                            folder_id=inputs.get("folder_id", ""),
                            file_name=inputs.get("file_name", ""),
                            content=inputs.get("content", ""),
                            mime_type=inputs.get("mime_type") or "text/markdown",
                            dry_run=False,
                        )
                        actions_taken.append(
                            f"wrote {inputs.get('file_name', '?')!r} to Drive"
                        )
                    elif block.name == "web_fetch":
                        output, cost = _tool_web_fetch(inputs.get("url", ""))
                    elif block.name == "create_email_draft":
                        output, cost = _tool_create_email_draft(
                            to=inputs.get("to", ""),
                            subject=inputs.get("subject", ""),
                            body=inputs.get("body", ""),
                            cc=inputs.get("cc"),
                        )
                        actions_taken.append(
                            f"drafted email to {inputs.get('to', '?')}"
                        )
                    elif block.name == "save_profile_fact":
                        output, cost = _tool_save_profile_fact(
                            field=inputs.get("field", ""),
                            value=inputs.get("value", ""),
                        )
                        # Onboarding writes aren't tracked as "actions_taken"
                        # — they're internal state, not visible work.
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
