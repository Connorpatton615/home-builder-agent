"""router_agent.py — hb-router: NL command dispatch + engine_activity chokepoint.

The "Chad says, Claude does" surface. Chad types or speaks a command in
the iOS Ask tab; the router classifies which agent handles it, extracts
parameters, dispatches the agent, and writes one row to
home_builder.engine_activity capturing the whole thing.

LOAD-BEARING per migration_003_review.md Rule 3: hb-router is the ONLY
writer to engine_activity. Direct CLI invocations of agents do NOT
write activity rows. UserAction-driven flows (iOS POST /actions →
reconcile dispatch) do NOT write activity rows. Only Claude-autonomous
flows through hb-router do.

CLI:
  hb-router "log a $400 receipt for Wholesale Plumbing"
  hb-router "framing pushed a week"
  hb-router "schedule the foundation inspection for Tuesday"
  hb-router --json "..."
  hb-router --dry-run "..."   (classifies, extracts params, doesn't dispatch)

Cost: ~$0.005–0.02 per command (Haiku for classification, sometimes
Sonnet for parameter extraction). The agent that gets dispatched has
its own cost on top.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from home_builder_agent.config import CLASSIFIER_MODEL, WRITER_MODEL
from home_builder_agent.core.claude_client import (
    haiku_cost,
    make_client,
    sonnet_cost,
)
from home_builder_agent.integrations.postgres import connection


# ---------------------------------------------------------------------------
# Module-fallback map for subprocess dispatch
# ---------------------------------------------------------------------------
# If the CLI binary isn't on PATH at subprocess time (daemon contexts,
# sandboxed Claude Code sessions, launchd jobs with sanitized PATH —
# every place the hb-chad agent might actually run from), retry the call
# via `python -m <module>` instead. This mirrors the documented fallback
# in CLAUDE.md ("PYTHONPATH … python3 -m home_builder_agent.agents.X")
# and bypasses PATH entirely.
#
# Keep in sync with [project.scripts] in pyproject.toml. Each entry maps
# the CLI command name → its dotted module path (without :main).
AGENT_MODULE_MAP: dict[str, str] = {
    "hb-receipt":       "home_builder_agent.agents.receipt_agent",
    "hb-update":        "home_builder_agent.agents.status_updater",
    "hb-ledger":        "home_builder_agent.agents.ledger_agent",
    "hb-inspect":       "home_builder_agent.agents.inspection_tracker",
    "hb-log":           "home_builder_agent.agents.site_log_agent",
    "hb-waiver":        "home_builder_agent.agents.lien_waiver_agent",
    "hb-change":        "home_builder_agent.agents.change_order_agent",
    "hb-client-update": "home_builder_agent.agents.client_update_agent",
    "hb-project":       "home_builder_agent.agents.project_agent",
}


# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------
# The router classifies user intent into one of these slugs, then dispatches.
# Adding a new command: register it here + add a dispatcher function below.
# This is the load-bearing dispatch table — every Chad-via-Claude action
# routes through here.

COMMAND_REGISTRY: dict[str, dict[str, Any]] = {
    "log-receipt": {
        "agent": "hb-receipt",
        "description": (
            "Log a paid receipt against the Cost Tracker. Use when Chad mentions "
            "paying a vendor, logging an expense, recording a receipt photo, etc."
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {
                "amount_usd": {
                    "type": "number",
                    "description": "Dollar amount of the receipt (e.g., 400.00)",
                },
                "vendor": {
                    "type": "string",
                    "description": "Vendor name (e.g., 'Wholesale Plumbing')",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Cost Tracker category if Chad specified one "
                        "(e.g., 'Plumbing', 'Framing', 'Lumber'). Optional — "
                        "agent infers if missing."
                    ),
                },
                "notes": {"type": "string"},
            },
            "required": ["amount_usd", "vendor"],
        },
    },
    "phase-update": {
        "agent": "hb-update",
        "description": (
            "Update phase status or schedule. Use when Chad says things like "
            "'framing pushed a week', 'foundation done', 'started electrical', "
            "'roofing delayed by 3 days'."
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {
                "nl_text": {
                    "type": "string",
                    "description": (
                        "Pass-through of Chad's update phrase. hb-update has its "
                        "own NL parser; the router shouldn't try to pre-parse this."
                    ),
                },
            },
            "required": ["nl_text"],
        },
    },
    "log-financial-entry": {
        "agent": "hb-ledger",
        "description": (
            "Plain-English financial entry that isn't a receipt. Use for "
            "billed amounts, allowance reconciliations, commitments, "
            "non-receipt actuals."
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {
                "nl_text": {
                    "type": "string",
                    "description": "Pass-through of Chad's ledger entry text.",
                },
            },
            "required": ["nl_text"],
        },
    },
    "log-inspection": {
        "agent": "hb-inspect",
        "description": (
            "Log a Baldwin County permit or inspection event. Use for things "
            "like 'building permit issued today', 'rough framing inspection "
            "passed', 'final inspection failed for X reason'."
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {
                "nl_text": {
                    "type": "string",
                    "description": "Pass-through; hb-inspect parses with its own Haiku call.",
                },
            },
            "required": ["nl_text"],
        },
    },
    "log-site-entry": {
        "agent": "hb-log",
        "description": (
            "Append a timestamped site-log entry. Use when Chad says things "
            "like 'log: framing crew on site 8 hours, weather clear', or "
            "describes what happened on the job site today."
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {
                "entry_text": {
                    "type": "string",
                    "description": (
                        "VERBATIM text of Chad's site note. The agent does NOT "
                        "rephrase — legal record integrity. Pass through exactly."
                    ),
                },
            },
            "required": ["entry_text"],
        },
    },
    "log-lien-waiver": {
        "agent": "hb-waiver",
        "description": (
            "Record a signed lien waiver from a sub. Use for things like "
            "'got conditional waiver from XYZ Framing for $12,500'."
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {
                "nl_text": {
                    "type": "string",
                    "description": "Pass-through; hb-waiver parses with its own Haiku call.",
                },
            },
            "required": ["nl_text"],
        },
    },
    "draft-change-order": {
        "agent": "hb-change",
        "description": (
            "Draft a change order document. Use when Chad describes a scope "
            "or cost change that needs formal client approval. This drafts the "
            "doc + updates Cost Tracker + adjusts schedule + drafts the client "
            "approval email — but DOES NOT send the email (Q1c gating: external "
            "writes require Chad confirmation)."
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {
                "nl_text": {
                    "type": "string",
                    "description": "Pass-through of Chad's CO description.",
                },
                "client_email": {
                    "type": "string",
                    "description": "Optional client email if Chad specifies; agent has a fallback.",
                },
            },
            "required": ["nl_text"],
        },
    },
    "draft-client-update": {
        "agent": "hb-client-update",
        "description": (
            "Draft (not send) a weekly client status email. Use when Chad asks "
            "to send/draft an update to the homeowner. Q1c gating: drafts only; "
            "Chad confirms send."
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "manage-project": {
        "agent": "hb-project",
        "description": (
            "Project lifecycle: archive a finished/cancelled project, create "
            "a new empty project, or clone an existing project's shape "
            "(phases + milestones, fresh status). Use when Chad says things "
            "like 'archive Whitfield', 'start a new project called Maple "
            "Ridge', 'create Pelican Point copying Whitfield's template', "
            "'kill that test project'. Does NOT touch Drive folders or "
            "Tracker sheets in v1 — DB-only flip suffices to remove a "
            "project from active surfaces, and clone copies the schedule "
            "template into a fresh row."
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["archive", "create", "clone"],
                    "description": (
                        "Which lifecycle operation to perform. 'archive' "
                        "flips status to archived. 'create' makes a fresh "
                        "empty shell (requires target_completion_date or "
                        "target_framing_start_date). 'clone' copies a source "
                        "project's phases + milestones with fresh status "
                        "(requires copy_from)."
                    ),
                },
                "project_name": {
                    "type": "string",
                    "description": (
                        "For archive: name (substring OK) or UUID of the "
                        "project to archive. For create/clone: the NEW "
                        "project's name."
                    ),
                },
                "copy_from": {
                    "type": "string",
                    "description": (
                        "Clone-only: source project name (substring OK) or "
                        "UUID to copy phase/milestone shape from."
                    ),
                },
                "customer_name": {
                    "type": "string",
                    "description": "Create/clone: homeowner / customer name.",
                },
                "address": {
                    "type": "string",
                    "description": "Create/clone: project address.",
                },
                "target_completion_date": {
                    "type": "string",
                    "description": "Create/clone: YYYY-MM-DD target completion date.",
                },
                "target_framing_start_date": {
                    "type": "string",
                    "description": "Create/clone: YYYY-MM-DD target framing-start date.",
                },
                "reason": {
                    "type": "string",
                    "description": "Archive-only: optional human-readable reason.",
                },
            },
            "required": ["action", "project_name"],
        },
    },
    "unknown": {
        "agent": None,
        "description": (
            "Catch-all when the command doesn't match any registered agent. "
            "Returns to the user as 'I don't know how to do that — try X or Y.'"
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "suggested_alternatives": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["reason"],
        },
    },
}


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

@dataclass
class RouteResult:
    user_intent: str
    classified_command_type: str
    invoked_agent: str | None
    parameters: dict[str, Any]
    outcome: str  # 'success' | 'partial' | 'error' | 'rejected'
    result_summary: str = ""
    affected_entity_type: str | None = None
    affected_entity_id: str | None = None
    cost_usd: float = 0.0
    duration_ms: int = 0
    error_message: str | None = None
    activity_id: str | None = None  # the engine_activity row UUID

    def to_dict(self) -> dict:
        return {
            "user_intent": self.user_intent,
            "classified_command_type": self.classified_command_type,
            "invoked_agent": self.invoked_agent,
            "parameters": self.parameters,
            "outcome": self.outcome,
            "result_summary": self.result_summary,
            "affected_entity_type": self.affected_entity_type,
            "affected_entity_id": self.affected_entity_id,
            "cost_usd": round(self.cost_usd, 4),
            "duration_ms": self.duration_ms,
            "error_message": self.error_message,
            "activity_id": self.activity_id,
        }


# ---------------------------------------------------------------------------
# Classifier — Haiku to map NL → command_type + parameters
# ---------------------------------------------------------------------------

CLASSIFIER_PROMPT = """You are the command classifier for Chad's AI assistant at Palmetto Custom Homes.

Chad will speak or type natural-language commands. Your job:
1. Classify the command into one of the registered types.
2. Extract structured parameters per the type's schema.

REGISTERED COMMAND TYPES:
{commands}

If the command doesn't match any registered type, classify as "unknown"
with a reason and 1-3 suggested alternatives (other commands Chad might
have meant).

OUTPUT: A single JSON object, no markdown fence:
{{
  "command_type": "<one of the registered slugs>",
  "parameters": {{ ... matching that type's parameter_schema ... }}
}}

CHAD'S COMMAND:
{user_intent}
"""


def _build_classifier_prompt(user_intent: str) -> str:
    cmd_lines = []
    for slug, spec in COMMAND_REGISTRY.items():
        if slug == "unknown":
            continue
        param_keys = list(spec["parameter_schema"].get("properties", {}).keys())
        cmd_lines.append(
            f"  - {slug} → invokes {spec['agent']}\n"
            f"    when: {spec['description']}\n"
            f"    params: {param_keys}"
        )
    cmd_lines.append(
        f"  - unknown → no agent invoked; returns suggested alternatives"
    )
    return CLASSIFIER_PROMPT.format(
        commands="\n".join(cmd_lines),
        user_intent=user_intent,
    )


def _classify_intent(user_intent: str) -> tuple[dict, float]:
    """Returns ({command_type, parameters}, classifier_cost_usd)."""
    client = make_client()
    prompt = _build_classifier_prompt(user_intent)

    response = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        # Defensive: if Haiku returns malformed JSON, treat as unknown
        parsed = {
            "command_type": "unknown",
            "parameters": {
                "reason": f"Classifier returned malformed JSON: {e}",
                "suggested_alternatives": [],
            },
        }

    cost = haiku_cost(response.usage)
    return parsed, cost


# ---------------------------------------------------------------------------
# Dispatcher — invoke the agent, capture result
# ---------------------------------------------------------------------------

def _invoke_agent(
    agent_cmd: str,
    parameters: dict,
    *,
    dry_run: bool,
) -> tuple[str, str, str | None]:
    """Invoke the agent CLI with the extracted parameters.

    Returns (outcome, result_summary, error_message).
    """
    if dry_run:
        return ("success", f"(dry-run: would invoke {agent_cmd} with {parameters})", None)

    # Each agent has its own argparse signature. The router maps
    # standard parameter shapes to the agent's expected CLI args.
    args = _build_agent_args(agent_cmd, parameters)
    if args is None:
        return (
            "error",
            "",
            f"Router couldn't build CLI args for {agent_cmd} from parameters {parameters!r}",
        )

    # Try the CLI binary first (fast path), fall back to `python -m <module>`
    # if it's not on PATH. The fallback is robust across daemon contexts
    # (launchd, iOS HTTP backend, sandboxed Claude Code) where PATH may not
    # include the Python framework's bin dir.
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return ("error", "", f"{agent_cmd} timed out after 120s")
    except FileNotFoundError:
        module = AGENT_MODULE_MAP.get(agent_cmd)
        if not module:
            return (
                "error", "",
                f"{agent_cmd} not found on PATH and no module fallback "
                f"registered (add to AGENT_MODULE_MAP in router_agent.py)",
            )
        fallback_args = [sys.executable, "-m", module, *args[1:]]
        try:
            proc = subprocess.run(
                fallback_args, capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            return ("error", "", f"{agent_cmd} (module fallback) timed out after 120s")
        except FileNotFoundError:
            return (
                "error", "",
                f"{agent_cmd}: python -m {module} also failed — "
                f"home_builder_agent not importable in this interpreter",
            )

    if proc.returncode == 0:
        # Pull a result summary from the agent's stdout (last few non-empty lines)
        summary_lines = [
            ln for ln in proc.stdout.splitlines()
            if ln.strip() and not ln.startswith(("=", "-", " "))
        ]
        summary = " ".join(summary_lines[-3:])[:500] if summary_lines else f"{agent_cmd} completed"
        return ("success", summary, None)
    else:
        err_summary = (proc.stderr or proc.stdout or "(no output)").strip()[:500]
        return ("error", "", f"{agent_cmd} exit {proc.returncode}: {err_summary}")


def _build_agent_args(agent_cmd: str, parameters: dict) -> list[str] | None:
    """Map router parameters → CLI argv for the specific agent.

    Each agent has its own CLI signature; this is where router-shape
    parameters get translated to the args each agent expects.
    """
    if agent_cmd == "hb-receipt":
        # hb-receipt expects a photo path as positional. The router's
        # "log-receipt" command is text-only (no photo), so for now we
        # pass the structured info as a synthetic --notes string and
        # rely on hb-ledger as the actual logging path. This is a v0
        # simplification; v1 wires photo-attachment from iOS through
        # to hb-receipt directly.
        # Return None to signal "router can't dispatch this without a photo";
        # caller should fall through to hb-ledger as the text-only logger.
        return None
    if agent_cmd == "hb-update":
        return [agent_cmd, parameters.get("nl_text", "")]
    if agent_cmd == "hb-ledger":
        text = parameters.get(
            "nl_text",
            f"paid {parameters.get('vendor', '?')} ${parameters.get('amount_usd', 0)}"
            + (f" for {parameters.get('category', '')}" if parameters.get("category") else ""),
        )
        return [agent_cmd, text]
    if agent_cmd == "hb-inspect":
        return [agent_cmd, "log", parameters.get("nl_text", "")]
    if agent_cmd == "hb-log":
        return [agent_cmd, parameters.get("entry_text", "")]
    if agent_cmd == "hb-waiver":
        return [agent_cmd, "log", parameters.get("nl_text", "")]
    if agent_cmd == "hb-change":
        args = [agent_cmd, parameters.get("nl_text", "")]
        if parameters.get("client_email"):
            args.extend(["--client-email", parameters["client_email"]])
        return args
    if agent_cmd == "hb-client-update":
        return [agent_cmd, "--from-tracker"]  # default = draft mode (no --send)
    if agent_cmd == "hb-project":
        action = (parameters.get("action") or "").lower()
        project_name = parameters.get("project_name") or ""
        if action == "archive":
            args = [agent_cmd, "archive", project_name, "--yes"]
            if parameters.get("reason"):
                args.extend(["--reason", parameters["reason"]])
            return args
        if action == "create":
            args = [agent_cmd, "create", "--name", project_name]
            if parameters.get("copy_from"):
                args.extend(["--copy-from", parameters["copy_from"]])
            if parameters.get("customer_name"):
                args.extend(["--customer-name", parameters["customer_name"]])
            if parameters.get("address"):
                args.extend(["--address", parameters["address"]])
            if parameters.get("target_completion_date"):
                args.extend(["--target-completion", parameters["target_completion_date"]])
            if parameters.get("target_framing_start_date"):
                args.extend(["--target-framing-start", parameters["target_framing_start_date"]])
            return args
        if action == "clone":
            # 'clone' is just create with --copy-from required
            if not parameters.get("copy_from"):
                return None
            args = [
                agent_cmd, "create",
                "--name", project_name,
                "--copy-from", parameters["copy_from"],
            ]
            if parameters.get("customer_name"):
                args.extend(["--customer-name", parameters["customer_name"]])
            if parameters.get("address"):
                args.extend(["--address", parameters["address"]])
            if parameters.get("target_completion_date"):
                args.extend(["--target-completion", parameters["target_completion_date"]])
            if parameters.get("target_framing_start_date"):
                args.extend(["--target-framing-start", parameters["target_framing_start_date"]])
            return args
        return None
    return None


# ---------------------------------------------------------------------------
# Activity logger — the chokepoint
# ---------------------------------------------------------------------------
#
# This is the ONLY place in the codebase that writes home_builder.engine_activity.
# Per migration_003_review.md Rule 3.

def _log_activity(
    *,
    actor_user_id: str | None,
    project_id: str | None,
    surface: str,
    invoked_agent: str | None,
    user_intent: str,
    classified_command_type: str,
    parameters: dict,
    outcome: str,
    result_summary: str,
    affected_entity_type: str | None,
    affected_entity_id: str | None,
    cost_usd: float,
    duration_ms: int,
    error_message: str | None,
) -> str | None:
    """Insert one row into home_builder.engine_activity. Returns the row UUID.

    Returns None on failure (e.g., DB unreachable). Failure here should
    NOT crash the caller — the action already succeeded; we just lost
    audit fidelity.
    """
    try:
        with connection(application_name="hb-router-activity") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO home_builder.engine_activity (
                        actor_user_id, project_id, surface, invoked_agent,
                        user_intent, classified_command_type, parameters,
                        outcome, result_summary, affected_entity_type,
                        affected_entity_id, cost_usd, duration_ms, error_message
                    ) VALUES (
                        %s::uuid, %s::uuid, %s, %s, %s, %s, %s::jsonb,
                        %s, %s, %s, %s::uuid, %s, %s, %s
                    ) RETURNING id::text AS id
                    """,
                    (
                        actor_user_id, project_id, surface, invoked_agent,
                        user_intent, classified_command_type,
                        json.dumps(parameters),
                        outcome, result_summary, affected_entity_type,
                        affected_entity_id, cost_usd, duration_ms, error_message,
                    ),
                )
                row = cur.fetchone()
                return row["id"] if row else None
    except Exception as e:
        # Soft-fail. Log to stderr so the operator notices but don't crash.
        print(f"[hb-router] WARNING: activity log write failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Top-level: route a command
# ---------------------------------------------------------------------------

def route_command(
    user_intent: str,
    *,
    actor_user_id: str | None = None,
    project_id: str | None = None,
    surface: str = "cli",
    dry_run: bool = False,
    skip_activity_log: bool = False,
) -> RouteResult:
    """The full router pipeline. Classify → dispatch → log activity → return.

    Args:
        user_intent:        Chad's NL command
        actor_user_id:      UUID of the auth.users row that initiated this
                            (None for direct CLI use). Required for the
                            activity log to attribute correctly.
        project_id:         UUID of the project this action is scoped to.
                            Many commands don't carry an explicit project
                            (e.g., "log a receipt"); the router doesn't try
                            to infer. iOS passes the active project from
                            the project picker.
        surface:            'chat' | 'voice' | 'cli' | 'background'.
                            CLI invocations default to 'cli'; when iOS calls
                            via /actions/dispatch this becomes 'chat' or 'voice'.
        dry_run:            Classify + log, but don't actually invoke the agent.
        skip_activity_log:  Don't write to engine_activity. Useful for unit
                            tests + smoke runs where DB writes would pollute.
    """
    started_at = time.time()
    result = RouteResult(
        user_intent=user_intent,
        classified_command_type="unknown",
        invoked_agent=None,
        parameters={},
        outcome="error",
    )

    # Step 1: classify
    try:
        classified, classify_cost = _classify_intent(user_intent)
        result.cost_usd += classify_cost
        result.classified_command_type = classified.get("command_type", "unknown")
        result.parameters = classified.get("parameters", {}) or {}
    except Exception as e:
        result.outcome = "error"
        result.error_message = f"classifier failed: {type(e).__name__}: {e}"
        result.duration_ms = int((time.time() - started_at) * 1000)
        if not skip_activity_log:
            result.activity_id = _log_activity(
                actor_user_id=actor_user_id, project_id=project_id, surface=surface,
                invoked_agent=None, user_intent=user_intent,
                classified_command_type=result.classified_command_type,
                parameters=result.parameters, outcome=result.outcome,
                result_summary="", affected_entity_type=None, affected_entity_id=None,
                cost_usd=result.cost_usd, duration_ms=result.duration_ms,
                error_message=result.error_message,
            )
        return result

    # Step 2: resolve agent
    spec = COMMAND_REGISTRY.get(result.classified_command_type)
    if not spec or not spec.get("agent"):
        # 'unknown' or genuinely unregistered
        result.invoked_agent = None
        result.outcome = "rejected"
        suggestions = result.parameters.get("suggested_alternatives", [])
        sug_text = (
            "Did you mean: " + ", ".join(suggestions) + "?"
            if suggestions else "Try one of: log a receipt, update a phase, log site notes, etc."
        )
        reason = result.parameters.get("reason", "command not recognized")
        result.result_summary = f"I don't know how to do that. {reason}. {sug_text}"
        result.duration_ms = int((time.time() - started_at) * 1000)
        if not skip_activity_log:
            result.activity_id = _log_activity(
                actor_user_id=actor_user_id, project_id=project_id, surface=surface,
                invoked_agent=None, user_intent=user_intent,
                classified_command_type=result.classified_command_type,
                parameters=result.parameters, outcome=result.outcome,
                result_summary=result.result_summary,
                affected_entity_type=None, affected_entity_id=None,
                cost_usd=result.cost_usd, duration_ms=result.duration_ms,
                error_message=None,
            )
        return result

    result.invoked_agent = spec["agent"]

    # Step 3: dispatch
    outcome, summary, err = _invoke_agent(
        spec["agent"], result.parameters, dry_run=dry_run,
    )
    result.outcome = outcome
    result.result_summary = summary
    result.error_message = err
    result.duration_ms = int((time.time() - started_at) * 1000)

    # Step 4: write activity (the chokepoint)
    if not skip_activity_log:
        result.activity_id = _log_activity(
            actor_user_id=actor_user_id, project_id=project_id, surface=surface,
            invoked_agent=result.invoked_agent,
            user_intent=user_intent,
            classified_command_type=result.classified_command_type,
            parameters=result.parameters,
            outcome=result.outcome,
            result_summary=result.result_summary,
            affected_entity_type=result.affected_entity_type,
            affected_entity_id=result.affected_entity_id,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
            error_message=result.error_message,
        )

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_pretty(r: RouteResult) -> None:
    print(f"\n{'='*64}")
    print(f"COMMAND")
    print(f"{'='*64}")
    print(f"  intent:   {r.user_intent}")
    print(f"  classified: {r.classified_command_type}")
    print(f"  agent:    {r.invoked_agent or '(none — not dispatched)'}")
    if r.parameters:
        print(f"  params:   {json.dumps(r.parameters, indent=2).replace(chr(10), chr(10) + '            ')}")
    print()

    icon = {"success": "✅", "partial": "⚠️ ", "error": "🚨", "rejected": "🚫"}.get(r.outcome, "?")
    print(f"  {icon} outcome:  {r.outcome}")
    if r.result_summary:
        print(f"  summary:  {r.result_summary}")
    if r.error_message:
        print(f"  error:    {r.error_message}")
    print()

    print(f"  classifier cost: ${r.cost_usd:.4f}")
    print(f"  duration:        {r.duration_ms}ms")
    if r.activity_id:
        print(f"  activity_id:     {r.activity_id}")
    print(f"{'='*64}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Route Chad's natural-language command to the right engine agent."
    )
    parser.add_argument(
        "intent", nargs="*", default=None,
        help="Chad's command, in plain English. Quote multi-word inputs.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Classify + log, but don't actually invoke the agent.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit structured JSON instead of pretty terminal output.",
    )
    parser.add_argument(
        "--skip-activity-log", action="store_true",
        help="Don't write to engine_activity. Useful for smoke tests; "
             "should NOT be used by iOS-driven calls.",
    )
    parser.add_argument(
        "--surface", choices=["chat", "voice", "cli", "background"], default="cli",
        help="Where this command originated (default: cli).",
    )
    args = parser.parse_args()

    user_intent = " ".join(args.intent).strip() if args.intent else ""
    if not user_intent:
        parser.error("Provide a command (e.g. hb-router \"log a $400 receipt\")")

    try:
        result = route_command(
            user_intent,
            surface=args.surface,
            dry_run=args.dry_run,
            skip_activity_log=args.skip_activity_log,
        )
    except Exception as e:
        if args.json:
            print(json.dumps({
                "error": True,
                "type": type(e).__name__,
                "message": str(e),
            }, indent=2))
        else:
            print(f"\n🚨 hb-router crashed: {type(e).__name__}: {e}\n", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _print_pretty(result)

    # Exit codes:
    #   0 — success or rejected (clean classify; intent was unknown)
    #   3 — partial or error (something tried to run and broke)
    sys.exit(3 if result.outcome in ("error", "partial") else 0)


if __name__ == "__main__":
    main()
