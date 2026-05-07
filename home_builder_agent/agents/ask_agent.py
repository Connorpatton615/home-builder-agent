"""ask_agent.py — hb-ask: RAG over Drive + Postgres for Chad's questions.

The chat agent. Chad asks a natural-language question; Claude Opus uses
tool-calling to retrieve relevant context from Drive (Tracker sheets,
Cost Tracker, Site Logs, KB), Postgres (engine state, recent activity),
or both, then composes an answer with citations.

CLI:
  hb-ask "what's the status of Whitfield framing?"
  hb-ask "how much have I spent on Pelican Point this month?"
  hb-ask --json "what subs need to start work next week?"

V0 design (locked with Connor):
  - Single-shot Q→A (no conversation memory)
  - Read-only (no write actions; those go through hb-router)
  - Cite-relevant-only (only files Claude actually opened, not files it
    only searched)
  - Keyword retrieval (no vector embeddings yet)
  - Claude Opus with tool use

Output JSON shape (for CTO consumption via /v1/turtles/home-builder/ask):
  {
    "question": "...",
    "answer": "polished prose answer for Chad",
    "citations": [
      {"file_id": "...", "name": "...", "webViewLink": "..."}
    ],
    "tools_called": [{"name": "...", "input": {...}, "duration_ms": 123}],
    "model": "claude-opus-...",
    "cost_usd": 0.12,
    "duration_ms": 4234
  }

Cost: ~$0.05–0.30/question depending on retrieval depth.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date

from anthropic import Anthropic

from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import make_client
from home_builder_agent.core.knowledge_base import (
    load_comm_rules,
    load_construction_reference,
    load_supplier_research,
)
from home_builder_agent.integrations import drive
from home_builder_agent.integrations.drive_search import (
    read_drive_file,
    search_drive_files,
)
from home_builder_agent.integrations.postgres import connection
from home_builder_agent.integrations.sheets import sheets_service


# Use Claude Opus 4.7 for hb-ask — Connor's "best mentality" green-light.
# Per-token cost is higher than Sonnet but the reasoning + retrieval
# planning is meaningfully better for ambiguous questions.
ASK_MODEL = "claude-opus-4-7"
ASK_MAX_TOKENS = 4096
MAX_TOOL_LOOP_ITERATIONS = 8  # safety guard against runaway tool loops


# ---------------------------------------------------------------------------
# Tool definitions for Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_projects",
        "description": (
            "List all active home-building projects. Returns each project's "
            "name, UUID, target completion date, and Drive folder name. "
            "Call this first when a question references a project by name "
            "to resolve the UUID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_project_status",
        "description": (
            "Get the current state of a project: all phases with status + "
            "dates, milestones, drop-dead order dates, estimated completion. "
            "Use this for any question about project progress, schedule, or "
            "what's coming up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project UUID (from list_projects).",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "search_drive",
        "description": (
            "Keyword search across Chad's Google Drive for files matching the "
            "query. Returns file names + IDs + when modified — does NOT return "
            "content (use read_drive_file for that). Best for finding which "
            "doc/sheet might contain the answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword phrase to search.",
                },
                "project_folder_id": {
                    "type": "string",
                    "description": (
                        "Optional Drive folder ID to scope the search. "
                        "Get from get_project_status which returns "
                        "drive_folder_id for a project."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_drive_file",
        "description": (
            "Read the full content of a Drive file by ID. Works for Google "
            "Sheets (returns tab-by-tab structured text), Google Docs (plain "
            "text), and markdown files. Use after search_drive identifies a "
            "candidate file. THIS is what counts as a citation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "Drive file ID (from search_drive results).",
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name": "read_knowledge_base",
        "description": (
            "Read one of three local knowledge base markdown files: "
            "'baldwin_county_construction_reference' (codes, climate, "
            "permitting, soil), 'baldwin_county_supplier_research' (vetted "
            "luxury suppliers), or 'chad_communication_rules' (how Chad "
            "talks). Use for code/permit/supplier questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": [
                        "baldwin_county_construction_reference",
                        "baldwin_county_supplier_research",
                        "chad_communication_rules",
                    ],
                },
            },
            "required": ["topic"],
        },
    },
    {
        "name": "get_inspections_summary",
        "description": (
            "Get a project's inspection + permit health: which permits "
            "are healthy vs aging vs about to expire, the next required "
            "inspection in the Baldwin County sequence, recent passed/"
            "failed/scheduled inspections. Use for ANY question about "
            "inspections, permits, what's next to schedule, permit "
            "expiry risk, or 'where are we in the inspection sequence'. "
            "Permits expire 180 days after issue or last passed "
            "inspection — this tool surfaces expiry exposure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": (
                        "Project name — e.g. 'Whitfield Residence'. Get "
                        "from list_projects if ambiguous."
                    ),
                },
            },
            "required": ["project_name"],
        },
    },
    {
        "name": "get_lien_waivers_summary",
        "description": (
            "Check lien waiver status for a project: which payments above "
            "the threshold have a matching signed waiver and which don't. "
            "Unwaived payments are LIEN RISK — even after Chad pays, that "
            "sub can file a mechanic's lien on the homeowner's property "
            "until the waiver is signed. Use for ANY question about lien "
            "waivers, payment risk, missing waivers, who Chad still needs "
            "to chase for a signed waiver, or 'are we covered legally on "
            "what we paid out?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": (
                        "Project name — e.g. 'Whitfield Residence'. Get from "
                        "list_projects if ambiguous."
                    ),
                },
            },
            "required": ["project_name"],
        },
    },
    {
        "name": "get_recent_activity",
        "description": (
            "Get recent autonomous actions Chad's AI took on his behalf "
            "via hb-router (logging receipts, updating phases, drafting "
            "change orders, etc.). Returns rows newest-first with the "
            "user intent, what was actually done, outcome, cost, and "
            "duration. Use for ANY question about what the AI did, "
            "what's been logged recently, audit-trail / accountability "
            "questions, or 'what happened with X' on a specific project. "
            "Optional project_id to scope to one project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": (
                        "Optional. Project UUID to filter to one project. "
                        "Omit to see activity across ALL projects."
                    ),
                },
                "since_hours": {
                    "type": "integer",
                    "description": (
                        "Optional. Only include actions from the last N hours "
                        "(e.g. 24 for 'today', 168 for 'this week'). Omit "
                        "for the most recent rows regardless of age."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max rows to return. Default 25. Cap 100."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_procurement_alerts",
        "description": (
            "Get a project's currently actionable procurement alerts: "
            "what materials need to be ordered NOW or soon to avoid "
            "delaying their install phase. Returns alerts grouped by "
            "urgency (OVERDUE / ORDER NOW / THIS WEEK / UPCOMING) with "
            "drop-dead order dates, install phases, and lead times. "
            "Use this for ANY question about ordering, lead times, "
            "what's overdue, what to order this week, or supply-chain "
            "risk on a project. Skips materials whose install phase "
            "is months out (not yet actionable)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": (
                        "Project UUID — get this from list_projects if "
                        "the user names the project ambiguously."
                    ),
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_cost_tracker_summary",
        "description": (
            "Get the project's Cost Tracker summary as structured data: "
            "section-by-section budget vs actual vs billed, line items "
            "with vendor names, grand totals, % spent. Use this for ANY "
            "question about money, budgets, costs, spending, vendors, "
            "or how a project is performing financially. Much cheaper "
            "than search_drive + read_drive_file for cost questions "
            "because it pre-aggregates the totals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": (
                        "Project name — e.g., 'Whitfield Residence'. Get "
                        "this from list_projects if the user references "
                        "a project ambiguously."
                    ),
                },
            },
            "required": ["project_name"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

@dataclass
class AskContext:
    """Shared state across tool calls during one question."""
    drive_svc: object
    sheets_svc: object
    cited_files: dict[str, dict] = field(default_factory=dict)
    tool_log: list[dict] = field(default_factory=list)


def _tool_list_projects(ctx: AskContext, **_) -> str:
    """Return a compact text listing of all active projects."""
    with connection(application_name="hb-ask-list-projects") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text AS id, name, target_completion_date,
                       target_framing_start_date, drive_folder_id, drive_folder_path
                FROM home_builder.project
                WHERE status = 'active'
                ORDER BY target_completion_date ASC NULLS LAST, name ASC
                """
            )
            rows = cur.fetchall()

    if not rows:
        return "(no active projects in Postgres yet — run hb-bridge to sync from Drive)"

    lines = []
    for r in rows:
        lines.append(
            f"- {r['name']} (id={r['id']}, target_completion={r['target_completion_date']}, "
            f"drive_folder_id={r['drive_folder_id']})"
        )
    return "Active projects:\n" + "\n".join(lines)


def _tool_get_project_status(ctx: AskContext, *, project_id: str) -> str:
    """Return the master view-model for a project as compact text."""
    from home_builder_agent.scheduling.lead_times import compute_drop_dead_dates
    from home_builder_agent.scheduling.store_postgres import compose_schedule_from_db
    from home_builder_agent.scheduling.view_models import project_master_view

    schedule = compose_schedule_from_db(project_id)
    if not schedule:
        return f"No project found with id={project_id}, or project has no phases."

    drop_deads = compute_drop_dead_dates(schedule)
    payload = project_master_view(schedule, drop_deads)
    payload_dict = payload.model_dump(mode="json", exclude_none=True)

    # Compact text rendering — easier on Claude's context vs raw JSON
    lines = []
    lines.append(f"PROJECT: {payload_dict['project_name']}")
    lines.append(f"  project_id: {payload_dict['project_id']}")
    lines.append(f"  estimated_completion_date: {payload_dict['estimated_completion_date']}")
    if payload_dict.get("target_completion_date"):
        lines.append(f"  target_completion_date: {payload_dict['target_completion_date']}")
    if payload_dict.get("target_framing_start_date"):
        lines.append(f"  target_framing_start_date: {payload_dict['target_framing_start_date']}")

    lines.append(f"\nPHASES ({len(payload_dict['phases'])}):")
    for p in payload_dict["phases"]:
        actual_part = ""
        if p.get("actual_start_date"):
            actual_part += f", actual_start={p['actual_start_date']}"
        if p.get("actual_end_date"):
            actual_part += f", actual_end={p['actual_end_date']}"
        lines.append(
            f"  {p['sequence_index']:>2}. {p['name']} | status={p['status']} | "
            f"planned {p['planned_start_date']} → {p['planned_end_date']}"
            f" ({p['duration_days']}d){actual_part}"
        )

    if payload_dict.get("milestones"):
        lines.append(f"\nMILESTONES:")
        for m in payload_dict["milestones"]:
            lines.append(f"  {m['planned_date']} — {m['name']} (status={m['status']})")

    if payload_dict.get("drop_dead_dates"):
        lines.append(f"\nDROP-DEAD ORDER DATES:")
        for dd in payload_dict["drop_dead_dates"][:15]:
            lines.append(
                f"  {dd['drop_dead_date']} — order {dd['material_category']} "
                f"({dd['lead_time_days']}d lead, install {dd['install_date']} "
                f"phase={dd['install_phase_name']})"
            )
        if len(payload_dict["drop_dead_dates"]) > 15:
            lines.append(f"  (+{len(payload_dict['drop_dead_dates']) - 15} more)")

    return "\n".join(lines)


def _tool_search_drive(
    ctx: AskContext, *, query: str, project_folder_id: str | None = None
) -> str:
    """Keyword search Drive. Returns file refs only (not content)."""
    files = search_drive_files(
        ctx.drive_svc,
        query=query,
        parent_folder_id=project_folder_id,
        max_results=15,
    )
    if not files:
        return f"No Drive files matched '{query}'" + (
            f" in folder {project_folder_id}" if project_folder_id else ""
        )

    lines = [f"Found {len(files)} file(s) matching '{query}':"]
    for f in files:
        lines.append(
            f"  - {f['name']} | id={f['id']} | type={f['mimeType'].split('.')[-1]} "
            f"| modified={f.get('modifiedTime', '?')[:10]}"
        )
    return "\n".join(lines)


def _tool_read_drive_file(ctx: AskContext, *, file_id: str) -> str:
    """Read a Drive file. Records the file as a citation."""
    result = read_drive_file(
        ctx.drive_svc,
        file_id=file_id,
        sheets_svc=ctx.sheets_svc,
        max_chars=30_000,
    )
    # Track for citations
    ctx.cited_files[file_id] = {
        "file_id": file_id,
        "name": result["name"],
        "webViewLink": result["webViewLink"],
    }

    header = (
        f"FILE: {result['name']}\n"
        f"  type: {result['mimeType']}\n"
        f"  modified: {result['modifiedTime']}\n"
        f"  webViewLink: {result['webViewLink']}\n"
    )
    if result["content_unavailable"]:
        return header + f"\n  CONTENT UNAVAILABLE: {result['content']}"
    if result["truncated"]:
        return header + f"\n  (content truncated to 30k chars)\n\n{result['content']}"
    return header + "\n" + result["content"]


def _tool_get_inspections_summary(ctx: AskContext, *, project_name: str) -> str:
    """Return inspection + permit health for a project as compact text."""
    from datetime import date as _date
    from home_builder_agent.config import DRIVE_FOLDER_PATH
    from home_builder_agent.integrations.drive import find_tracker_by_project
    from home_builder_agent.integrations.sheets import read_inspections
    from home_builder_agent.agents.inspection_tracker import compute_permit_health

    try:
        tracker = find_tracker_by_project(
            ctx.drive_svc, DRIVE_FOLDER_PATH, project_name,
        )
    except Exception as e:
        return f"ERROR locating Tracker for '{project_name}': {type(e).__name__}: {e}"

    if not tracker:
        return f"No Tracker sheet found for project '{project_name}'. Run hb-timeline to generate one, or check the project name."

    try:
        records = read_inspections(ctx.sheets_svc, tracker["id"])
    except Exception as e:
        return f"ERROR reading Inspections tab: {type(e).__name__}: {e}"

    today = _date.today()
    permits = compute_permit_health(records, today=today)

    # Track citation
    if tracker.get("webViewLink"):
        ctx.cited_files[f"tracker-{project_name}"] = {
            "file_id": f"tracker-{project_name}",
            "name": f"{project_name} — Tracker (Inspections)",
            "webViewLink": tracker["webViewLink"],
        }

    lines = []
    lines.append(f"INSPECTION STATUS — {project_name}")
    lines.append(f"  today: {today.isoformat()}  |  inspection rows: {len(records)}")
    lines.append(f"  tracker: {tracker.get('webViewLink', '')}")

    if not permits:
        lines.append("\n(no permits logged yet — Inspections tab is empty or has no permit issuance rows)")
        return "\n".join(lines)

    # Health summary
    by_health = {"OK": 0, "WARNING": 0, "CRITICAL": 0, "EXPIRED": 0, "UNKNOWN": 0}
    for p in permits:
        h = p.get("health", "UNKNOWN")
        by_health[h] = by_health.get(h, 0) + 1

    lines.append(
        f"  health: OK={by_health['OK']} | WARNING={by_health['WARNING']} | "
        f"CRITICAL={by_health['CRITICAL']} | EXPIRED={by_health['EXPIRED']} | "
        f"UNKNOWN={by_health['UNKNOWN']}"
    )

    # Sort: most urgent first (EXPIRED → CRITICAL → WARNING → OK → UNKNOWN)
    health_priority = {"EXPIRED": 0, "CRITICAL": 1, "WARNING": 2, "OK": 3, "UNKNOWN": 4}
    sorted_permits = sorted(
        permits,
        key=lambda p: (health_priority.get(p.get("health", "UNKNOWN"), 9),
                       p.get("days_until_expiry") if p.get("days_until_expiry") is not None else 9999),
    )

    lines.append(f"\nPERMITS ({len(sorted_permits)}):")
    for p in sorted_permits:
        pnum = p.get("permit_number", "?")
        ptype = p.get("permit_type", "?")
        health = p.get("health", "?")
        days_until = p.get("days_until_expiry")
        expiry = p.get("expiry_date")
        next_insp = p.get("next_inspection") or "(sequence complete or not started)"

        if days_until is None:
            timing = "no anchor date"
        elif days_until < 0:
            timing = f"EXPIRED {-days_until}d ago"
        else:
            timing = f"{days_until}d until expiry"
        expiry_str = expiry.isoformat() if expiry else "?"

        lines.append(
            f"\n  [{health:<8}] {ptype} #{pnum}"
        )
        lines.append(
            f"      {timing} (expires {expiry_str})"
        )
        lines.append(
            f"      next inspection: {next_insp}"
        )
        passed = p.get("passed_inspections", [])
        failed = p.get("failed_inspections", [])
        scheduled = p.get("scheduled_inspections", [])
        if passed:
            passed_str = ", ".join(passed[-5:])
            extra = f" (+{len(passed) - 5} more)" if len(passed) > 5 else ""
            lines.append(f"      passed ({len(passed)}): {passed_str}{extra}")
        if failed:
            lines.append(f"      ⚠️  failed ({len(failed)}): {', '.join(failed)}")
        if scheduled:
            sched_str = ", ".join(
                f"{s.get('type', '?')} on {s['date'].isoformat() if s.get('date') else '?'}"
                for s in scheduled
            )
            lines.append(f"      scheduled: {sched_str}")

    return "\n".join(lines)


def _tool_get_lien_waivers_summary(ctx: AskContext, *, project_name: str) -> str:
    """Return waiver-coverage status for a project's payments as compact text."""
    from datetime import date as _date
    from home_builder_agent.config import (
        FINANCE_FOLDER_PATH, LIEN_WAIVER_THRESHOLD,
    )
    from home_builder_agent.integrations.drive import find_folder_by_path
    from home_builder_agent.integrations.finance import (
        find_cost_tracker, read_actuals_log, read_lien_waivers,
    )
    from home_builder_agent.agents.lien_waiver_agent import find_unwaived_payments

    try:
        folder_id = find_folder_by_path(ctx.drive_svc, FINANCE_FOLDER_PATH)
    except Exception as e:
        return f"ERROR locating Finance Office folder: {type(e).__name__}: {e}"

    tracker = find_cost_tracker(ctx.drive_svc, folder_id, project_name)
    if not tracker:
        return f"No Cost Tracker found for project '{project_name}'. Run hb-finance to create one, or check the project name."

    try:
        actuals = read_actuals_log(ctx.sheets_svc, tracker["id"])
        waivers = read_lien_waivers(ctx.sheets_svc, tracker["id"])
    except Exception as e:
        return f"ERROR reading actuals/waivers: {type(e).__name__}: {e}"

    today = _date.today()
    report = find_unwaived_payments(actuals, waivers, today=today)

    # Track citation
    if tracker.get("webViewLink"):
        ctx.cited_files[f"cost-tracker-{project_name}"] = {
            "file_id": f"cost-tracker-{project_name}",
            "name": f"{project_name} — Cost Tracker (Lien Waivers)",
            "webViewLink": tracker["webViewLink"],
        }

    unwaived = report["unwaived"]
    waived = report["waived"]
    below = report["below_threshold"]

    lines = []
    lines.append(f"LIEN WAIVER STATUS — {project_name}")
    lines.append(f"  threshold: ${LIEN_WAIVER_THRESHOLD:,.0f}  |  today: {today.isoformat()}")
    lines.append(
        f"  totals: {report['total_payments']} payments | "
        f"{len(waived)} waived | {len(unwaived)} UNWAIVED (lien risk) | "
        f"{len(below)} below threshold"
    )
    lines.append(f"  cost tracker: {tracker.get('webViewLink', '')}")

    # Unwaived = the actionable list. Lead with this — most urgent first.
    if unwaived:
        # Sort: oldest payment first (longest exposure)
        def _date_key(p):
            from home_builder_agent.agents.lien_waiver_agent import _parse_date
            d = _parse_date(p.get("Date"))
            return d or _date.min
        sorted_unwaived = sorted(unwaived, key=_date_key)

        lines.append(f"\n🚨 UNWAIVED PAYMENTS ({len(unwaived)}):")
        from home_builder_agent.agents.lien_waiver_agent import _parse_date, _parse_amount
        total_unwaived_amt = 0.0
        for p in sorted_unwaived:
            vendor = (p.get("Vendor") or "?").strip()
            amt = _parse_amount(p.get("Amount ($)"))
            pay_date = _parse_date(p.get("Date"))
            amt_str = f"${amt:,.0f}" if amt else "?"
            if amt:
                total_unwaived_amt += amt
            if pay_date:
                age_days = (today - pay_date).days
                date_str = f"{pay_date.isoformat()} ({age_days}d ago)"
            else:
                date_str = "(no date)"
            category = (p.get("Category") or p.get("Section") or "").strip()
            cat_part = f" | {category}" if category else ""
            lines.append(f"  - {vendor:<28} {amt_str:>10}  paid {date_str}{cat_part}")
        lines.append(f"  total unwaived exposure: ${total_unwaived_amt:,.0f}")
    else:
        lines.append("\n✓ No unwaived payments — every payment above threshold has a matching waiver.")

    if waived:
        lines.append(f"\nWAIVED ({len(waived)}):")
        for entry in waived[:10]:
            p = entry["payment"]
            w = entry["waiver"]
            vendor = (p.get("Vendor") or "?").strip()
            from home_builder_agent.agents.lien_waiver_agent import _parse_amount
            amt = _parse_amount(p.get("Amount ($)"))
            amt_str = f"${amt:,.0f}" if amt else "?"
            wtype = (w.get("Waiver Type") or "?").strip()
            lines.append(f"  - {vendor:<28} {amt_str:>10}  waiver: {wtype}")
        if len(waived) > 10:
            lines.append(f"  (+{len(waived) - 10} more)")

    return "\n".join(lines)


def _tool_get_recent_activity(
    ctx: AskContext,
    *,
    project_id: str | None = None,
    since_hours: int | None = None,
    limit: int = 25,
) -> str:
    """Return recent engine_activity rows as compact text."""
    from home_builder_agent.scheduling.store_postgres import load_recent_engine_activity

    # Cap the limit so we don't blow context if Claude asks for 1000.
    limit = max(1, min(int(limit or 25), 100))

    try:
        rows = load_recent_engine_activity(
            project_id=project_id,
            since_hours=since_hours,
            limit=limit,
        )
    except Exception as e:
        return f"ERROR reading engine_activity: {type(e).__name__}: {e}"

    scope_bits = []
    if project_id:
        scope_bits.append(f"project_id={project_id}")
    if since_hours:
        scope_bits.append(f"last {since_hours}h")
    scope_str = " | ".join(scope_bits) if scope_bits else "all projects, no time filter"

    lines = [f"RECENT ACTIVITY ({scope_str}, limit {limit})"]

    if not rows:
        lines.append("  (no activity rows match these filters)")
        return "\n".join(lines)

    # Aggregate stats up top — Chad-relevant summary.
    outcomes = {"success": 0, "partial": 0, "error": 0, "rejected": 0}
    total_cost = 0.0
    by_command: dict[str, int] = {}
    for r in rows:
        outcome = (r.get("outcome") or "").lower()
        if outcome in outcomes:
            outcomes[outcome] += 1
        if r.get("cost_usd"):
            total_cost += r["cost_usd"]
        cmd = r.get("classified_command_type") or "unknown"
        by_command[cmd] = by_command.get(cmd, 0) + 1

    lines.append(
        f"  totals: {len(rows)} rows | "
        f"success={outcomes['success']} | partial={outcomes['partial']} | "
        f"error={outcomes['error']} | rejected={outcomes['rejected']} | "
        f"AI cost ${total_cost:.4f}"
    )
    if by_command:
        cmd_str = ", ".join(f"{c}={n}" for c, n in sorted(by_command.items(), key=lambda x: -x[1]))
        lines.append(f"  commands: {cmd_str}")

    lines.append("")
    for r in rows:
        ts = r.get("created_at") or ""
        # Trim ISO to YYYY-MM-DD HH:MM for compactness
        ts_short = ts.replace("T", " ")[:16] if ts else "?"
        outcome = (r.get("outcome") or "?").upper()
        cmd = r.get("classified_command_type") or "?"
        cost = r.get("cost_usd")
        dur = r.get("duration_ms")
        intent = (r.get("user_intent") or "").strip().replace("\n", " ")
        if len(intent) > 100:
            intent = intent[:97] + "..."
        summary = (r.get("result_summary") or "").strip().replace("\n", " ")
        if len(summary) > 100:
            summary = summary[:97] + "..."

        cost_str = f"${cost:.4f}" if cost is not None else "—"
        dur_str = f"{dur}ms" if dur is not None else "—"

        lines.append(
            f"  [{ts_short}] {outcome:<8} {cmd:<22} {cost_str:>9} {dur_str:>7}"
        )
        lines.append(f"      intent: {intent}")
        if summary:
            lines.append(f"      result: {summary}")
        if r.get("error_message"):
            err = r["error_message"].strip().replace("\n", " ")
            if len(err) > 120:
                err = err[:117] + "..."
            lines.append(f"      error:  {err}")

    return "\n".join(lines)


def _tool_get_procurement_alerts(ctx: AskContext, *, project_id: str) -> str:
    """Return live procurement alerts for a project as compact text."""
    from home_builder_agent.scheduling.lead_times import compute_live_procurement_alerts

    try:
        result = compute_live_procurement_alerts(project_id)
    except Exception as e:
        return f"ERROR computing procurement alerts: {type(e).__name__}: {e}"

    if result is None:
        return f"No schedule found in Postgres for project_id={project_id}. Run hb-bridge to sync, or check the project_id."

    lines = []
    lines.append(f"PROCUREMENT ALERTS — {result['project_name']}")
    lines.append(f"  today: {result['today']}  |  upcoming window: {result['upcoming_window_days']}d")

    totals = result["totals"]
    lines.append(
        f"  totals: OVERDUE={totals['OVERDUE']} | "
        f"ORDER NOW={totals['ORDER NOW']} | "
        f"THIS WEEK={totals['THIS WEEK']} | "
        f"UPCOMING={totals['UPCOMING']}"
    )

    alerts = result["alerts"]
    if not alerts:
        lines.append("\n(no actionable alerts — every material's drop-dead is months out, or none defined)")
        return "\n".join(lines)

    lines.append(f"\nALERTS ({len(alerts)}):")
    for a in alerts:
        days = a["days_until_drop_dead"]
        if days < 0:
            timing = f"{-days}d OVERDUE"
        elif days == 0:
            timing = "ORDER TODAY"
        else:
            timing = f"order in {days}d"
        lines.append(
            f"  [{a['band']:<10}] {a['material_category']:<10} | {timing:<14} | "
            f"drop-dead {a['drop_dead_date']} | install {a['install_date']} ({a['install_phase_name']}) | "
            f"lead {a['lead_time_days']}d"
        )

    return "\n".join(lines)


def _tool_get_cost_tracker_summary(ctx: AskContext, *, project_name: str) -> str:
    """Read the project's Cost Tracker and return a structured summary."""
    from home_builder_agent.config import FINANCE_FOLDER_PATH
    from home_builder_agent.integrations.drive import find_folder_by_path
    from home_builder_agent.integrations.finance import read_cost_tracker_summary

    try:
        folder_id = find_folder_by_path(ctx.drive_svc, FINANCE_FOLDER_PATH)
    except Exception as e:
        return f"ERROR locating Finance Office folder: {type(e).__name__}: {e}"

    try:
        summary = read_cost_tracker_summary(
            ctx.drive_svc, ctx.sheets_svc, folder_id, project_name,
        )
    except Exception as e:
        return f"ERROR reading Cost Tracker for '{project_name}': {type(e).__name__}: {e}"

    if summary is None:
        return f"No Cost Tracker found for project '{project_name}'. Run hb-finance to create one, or check the project name."

    # Track as a citation since this opens the Cost Tracker
    if summary.get("cost_tracker_url"):
        ctx.cited_files[f"cost-tracker-{project_name}"] = {
            "file_id": f"cost-tracker-{project_name}",
            "name": f"{project_name} — Cost Tracker",
            "webViewLink": summary["cost_tracker_url"],
        }

    # Compact text rendering — easier on Claude's context vs raw JSON
    lines = []
    lines.append(f"COST TRACKER: {summary['project_name']}")
    lines.append(f"  url: {summary['cost_tracker_url']}")

    gt = summary.get("grand_totals") or {}
    if gt:
        lines.append("")
        lines.append("GRAND TOTALS:")
        lines.append(f"  Budget:   ${gt.get('budget', 0):>14,.2f}")
        lines.append(f"  Actual:   ${gt.get('actual', 0):>14,.2f}")
        lines.append(f"  Billed:   ${gt.get('billed', 0):>14,.2f}")
        lines.append(f"  Diff:     ${gt.get('diff_vs_budget', 0):>14,.2f}  (Budget - Actual)")
        lines.append(f"  % Spent:  {gt.get('pct_spent', 0):>5}%")

    sections = summary.get("sections", [])
    if not sections:
        lines.append("\n(no sections yet — Cost Tracker may be empty)")
        return "\n".join(lines)

    lines.append(f"\nSECTIONS ({len(sections)}):")
    for s in sections:
        diff = s.get("diff_vs_budget", 0)
        sign = "✓" if diff >= 0 else "⚠"
        lines.append(
            f"\n  {s['name']:<40} budget=${s.get('budget', 0):>10,.0f} | "
            f"actual=${s.get('actual', 0):>10,.0f} | "
            f"billed=${s.get('billed', 0):>10,.0f} | "
            f"diff=${diff:>+10,.0f} {sign}"
        )
        # Show top 3 line items per section to keep context manageable
        items = s.get("items", [])
        for item in items:
            actual = item.get("actual")
            budget = item.get("budget")
            vendor = item.get("vendor") or ""
            actual_str = f"${actual:,.0f}" if actual is not None else "—"
            budget_str = f"${budget:,.0f}" if budget is not None else "—"
            line = f"      - {item['name']:<40} budget={budget_str:>9} actual={actual_str:>9}"
            if vendor:
                line += f" | vendor: {vendor}"
            lines.append(line)

    return "\n".join(lines)


def _tool_read_knowledge_base(ctx: AskContext, *, topic: str) -> str:
    """Read one of three local KB markdown files."""
    if topic == "baldwin_county_construction_reference":
        content = load_construction_reference()
    elif topic == "baldwin_county_supplier_research":
        content = load_supplier_research()
    elif topic == "chad_communication_rules":
        content = load_comm_rules()
    else:
        return f"Unknown KB topic: {topic!r}"

    if not content:
        return f"KB topic {topic!r} is empty or missing on disk."

    # Truncate to keep context manageable
    if len(content) > 25_000:
        return content[:25_000] + "\n\n... (truncated; ~{} more chars)".format(len(content) - 25_000)
    return content


TOOL_DISPATCH = {
    "list_projects":              _tool_list_projects,
    "get_project_status":         _tool_get_project_status,
    "search_drive":               _tool_search_drive,
    "read_drive_file":            _tool_read_drive_file,
    "read_knowledge_base":        _tool_read_knowledge_base,
    "get_cost_tracker_summary":   _tool_get_cost_tracker_summary,
    "get_procurement_alerts":     _tool_get_procurement_alerts,
    "get_recent_activity":        _tool_get_recent_activity,
    "get_lien_waivers_summary":   _tool_get_lien_waivers_summary,
    "get_inspections_summary":    _tool_get_inspections_summary,
}


# ---------------------------------------------------------------------------
# RAG loop
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Chad's AI assistant for Palmetto Custom Homes, a luxury custom home builder in Baldwin County, Alabama.

Chad is the owner-operator. He's a busy GC asking real-world questions in
the truck between job sites. He needs:
  - Direct answers (no preamble like "Great question!")
  - Concrete numbers, dates, names — never vague
  - Clear next-action implications when relevant
  - Brief: 2–5 sentences for most questions; longer only when truly needed

You have tools to retrieve context from his project data:
  - list_projects: see active projects
  - get_project_status: full master schedule for one project
  - get_cost_tracker_summary: budget vs actual vs billed by section + grand totals
    (use for ANY money/budget/cost/vendor/spending question — much cheaper
    than search+read for cost questions because totals are pre-aggregated)
  - get_procurement_alerts: actionable order alerts grouped by urgency
    (use for ANY question about ordering, lead times, what's overdue,
    what to order this week, supply-chain risk)
  - get_recent_activity: recent autonomous actions the AI took on Chad's
    behalf via hb-router (receipts logged, phases updated, change orders
    drafted, etc.) — use for ANY "what did the AI do" / accountability /
    audit-trail question. Optional project_id + since_hours filters.
  - get_lien_waivers_summary: which payments still need a signed lien
    waiver (= lien risk). Use for ANY question about waivers, lien risk,
    "who do I still need to chase for a waiver", legal coverage on
    payments paid out.
  - get_inspections_summary: permit health + next required inspection +
    permit expiry exposure (180-day rule). Use for ANY inspection /
    permit / "what's next to schedule" / "any permits about to expire"
    question.
  - search_drive: keyword-search his Google Drive
  - read_drive_file: open a specific file (THIS counts as a citation)
  - read_knowledge_base: Baldwin County codes, supplier list, Chad's voice rules

PROCESS:
  1. Decide which tools to call. Most questions need get_project_status,
     get_cost_tracker_summary (for money questions), OR search_drive +
     read_drive_file. Don't over-retrieve — 1-3 tool calls is normal;
     5+ is a sign you're going down a rabbit hole.
  2. After you have enough context, give Chad his answer.
  3. If a project name is mentioned but ambiguous, call list_projects
     first to resolve the UUID.
  4. If you can't answer with high confidence, say so plainly. Don't
     make up numbers or dates.

CITATIONS:
  Files you OPEN with read_drive_file count as citations and Chad sees
  them. Files you only search via search_drive don't show up. Read what
  you need to actually answer; don't read files you don't end up using.

TODAY'S DATE: {today}
"""


def ask_question(question: str, *, verbose: bool = False) -> dict:
    """Run the RAG loop for one question. Returns the answer dict."""
    started_at = time.time()

    creds = get_credentials()
    drive_svc = drive.drive_service(creds)
    ss = sheets_service(creds)
    ctx = AskContext(drive_svc=drive_svc, sheets_svc=ss)

    client = make_client()

    system = SYSTEM_PROMPT.replace("{today}", date.today().strftime("%A, %B %-d, %Y"))
    messages = [{"role": "user", "content": question}]

    final_text = ""
    total_input_tokens = 0
    total_output_tokens = 0

    for iteration in range(MAX_TOOL_LOOP_ITERATIONS):
        if verbose:
            print(f"\n[iter {iteration + 1}] calling Claude...", file=sys.stderr)

        response = client.messages.create(
            model=ASK_MODEL,
            max_tokens=ASK_MAX_TOKENS,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        # Did Claude give a text answer or want to use tools?
        if response.stop_reason == "end_turn":
            # Final text answer
            for block in response.content:
                if block.type == "text":
                    final_text += block.text
            break

        if response.stop_reason == "tool_use":
            # Append assistant turn (with tool_use blocks) to messages
            messages.append({"role": "assistant", "content": response.content})

            # Execute each tool call
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_started = time.time()
                    tool_name = block.name
                    tool_input = block.input or {}

                    if verbose:
                        print(f"  → tool: {tool_name}({tool_input})", file=sys.stderr)

                    handler = TOOL_DISPATCH.get(tool_name)
                    if handler is None:
                        result_text = f"ERROR: unknown tool {tool_name!r}"
                    else:
                        try:
                            result_text = handler(ctx, **tool_input)
                        except Exception as e:
                            result_text = f"ERROR: {type(e).__name__}: {e}"

                    duration_ms = int((time.time() - tool_started) * 1000)
                    ctx.tool_log.append({
                        "name": tool_name,
                        "input": tool_input,
                        "duration_ms": duration_ms,
                    })

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })

            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason
        if verbose:
            print(f"  → stop_reason={response.stop_reason}, breaking", file=sys.stderr)
        break

    # Cost calculation (Opus 4 pricing)
    cost = (total_input_tokens / 1e6) * 15.0 + (total_output_tokens / 1e6) * 75.0

    return {
        "question": question,
        "answer": final_text.strip() or "(no answer produced)",
        "citations": list(ctx.cited_files.values()),
        "tools_called": ctx.tool_log,
        "model": ASK_MODEL,
        "cost_usd": round(cost, 4),
        "duration_ms": int((time.time() - started_at) * 1000),
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
    }


# ---------------------------------------------------------------------------
# Streaming generator — for SSE consumption by /v1/turtles/home-builder/ask
# ---------------------------------------------------------------------------
#
# Per migration_003_review.md SSE stream contract: the engine yields
# (event_id, event_type, payload) tuples. The route handler is responsible
# for serializing to SSE wire format AND maintaining the Redis 5-min TTL
# replay buffer for Last-Event-ID reconnection.
#
# Engine doesn't know about Redis or SSE wire format — clean boundary.
#
# Event types emitted (from spec):
#   text_delta       — token batches as Claude composes answer text
#   tool_use         — Claude invokes a tool (after full input is known)
#   tool_result      — tool returned (with summary + duration)
#   citation_added   — file opened via read_drive_file
#   message_complete — terminal: full answer + citations + cost + duration
#   error            — terminal: error envelope

from typing import Iterator


def ask_question_stream(
    question: str,
    *,
    verbose: bool = False,
) -> Iterator[tuple[int, str, dict]]:
    """Streaming version of ask_question.

    Yields (event_id, event_type, payload) tuples. Event IDs are monotonic
    per stream, starting at 1. Stream is terminated by either a
    `message_complete` or an `error` event — caller breaks out of iteration
    after either.

    Usage (from a FastAPI route handler):

        async def ask_stream(question: str):
            for event_id, event_type, payload in ask_question_stream(question):
                yield f"id: {event_id}\\n"
                yield f"event: {event_type}\\n"
                yield f"data: {json.dumps(payload)}\\n\\n"
                # plus Redis buffer write per the reconnection spec
    """
    started_at = time.time()
    event_id_counter = 0

    def emit(event_type: str, payload: dict) -> tuple[int, str, dict]:
        nonlocal event_id_counter
        event_id_counter += 1
        return (event_id_counter, event_type, payload)

    # Setup — same as ask_question but kept inline so we can yield error
    # events on setup failure (instead of raising)
    try:
        creds = get_credentials()
        drive_svc = drive.drive_service(creds)
        ss = sheets_service(creds)
        ctx = AskContext(drive_svc=drive_svc, sheets_svc=ss)
        client = make_client()
    except Exception as e:
        yield emit("error", {
            "type": type(e).__name__,
            "message": f"setup failed: {e}",
        })
        return

    system = SYSTEM_PROMPT.replace("{today}", date.today().strftime("%A, %B %-d, %Y"))
    messages: list[dict] = [{"role": "user", "content": question}]

    total_input_tokens = 0
    total_output_tokens = 0

    try:
        for iteration in range(MAX_TOOL_LOOP_ITERATIONS):
            if verbose:
                print(f"\n[stream iter {iteration + 1}] calling Claude...", file=sys.stderr)

            # Stream the response. Claude SDK's stream context manager
            # yields content_block events; we surface text_deltas to the
            # caller and accumulate the full message for tool_use processing
            # at message_stop.
            with client.messages.stream(
                model=ASK_MODEL,
                max_tokens=ASK_MAX_TOKENS,
                system=system,
                tools=TOOLS,
                messages=messages,
            ) as stream:
                for event in stream:
                    # The SDK emits multiple event types. We only forward
                    # text_delta to the caller — tool_use events are
                    # emitted from the FINAL message after the stream
                    # completes (see below).
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield emit("text_delta", {"delta": event.delta.text})

                final_message = stream.get_final_message()

            total_input_tokens += final_message.usage.input_tokens
            total_output_tokens += final_message.usage.output_tokens

            # Branch on stop_reason
            if final_message.stop_reason == "end_turn":
                # Compose final answer text
                final_text = ""
                for block in final_message.content:
                    if block.type == "text":
                        final_text += block.text

                cost = (total_input_tokens / 1e6) * 15.0 + (total_output_tokens / 1e6) * 75.0

                yield emit("message_complete", {
                    "answer": final_text.strip() or "(no answer produced)",
                    "citations": list(ctx.cited_files.values()),
                    "tools_called": ctx.tool_log,
                    "model": ASK_MODEL,
                    "cost_usd": round(cost, 4),
                    "duration_ms": int((time.time() - started_at) * 1000),
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                })
                return

            if final_message.stop_reason == "tool_use":
                # Append the assistant turn (with tool_use blocks) to messages
                messages.append({"role": "assistant", "content": final_message.content})

                tool_results_for_next_turn = []
                for block in final_message.content:
                    if block.type != "tool_use":
                        continue

                    # Emit tool_use event with full input now that the
                    # message has streamed completely
                    yield emit("tool_use", {
                        "id": block.id,
                        "name": block.name,
                        "input": block.input or {},
                    })

                    # Execute the tool
                    tool_started = time.time()
                    handler = TOOL_DISPATCH.get(block.name)
                    if handler is None:
                        result_text = f"ERROR: unknown tool {block.name!r}"
                    else:
                        try:
                            result_text = handler(ctx, **(block.input or {}))
                        except Exception as e:
                            result_text = f"ERROR: {type(e).__name__}: {e}"

                    duration_ms = int((time.time() - tool_started) * 1000)
                    ctx.tool_log.append({
                        "name": block.name,
                        "input": block.input or {},
                        "duration_ms": duration_ms,
                    })

                    # If this tool was read_drive_file, the file just got
                    # added to ctx.cited_files. Emit a citation_added event
                    # so iOS renders the citation chip immediately rather
                    # than waiting for message_complete.
                    if block.name == "read_drive_file":
                        file_id = (block.input or {}).get("file_id")
                        if file_id and file_id in ctx.cited_files:
                            yield emit("citation_added", ctx.cited_files[file_id])

                    # Emit tool_result with a one-line summary (full result
                    # goes into messages for Claude's next turn but iOS
                    # only needs a short status line)
                    summary = result_text.split("\n")[0][:160]
                    yield emit("tool_result", {
                        "id": block.id,
                        "name": block.name,
                        "duration_ms": duration_ms,
                        "summary": summary,
                    })

                    tool_results_for_next_turn.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
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
                "without Claude composing a final answer"
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
    print(f"\n{'='*64}")
    print(f"QUESTION")
    print(f"{'='*64}")
    print(result["question"])
    print()
    print(f"{'='*64}")
    print(f"ANSWER")
    print(f"{'='*64}")
    print(result["answer"])
    print()
    if result["citations"]:
        print(f"{'='*64}")
        print(f"CITATIONS ({len(result['citations'])})")
        print(f"{'='*64}")
        for c in result["citations"]:
            print(f"  • {c['name']}")
            print(f"    {c['webViewLink']}")
    print()
    print(f"{'='*64}")
    print(f"  Model:           {result['model']}")
    print(f"  Tools called:    {len(result['tools_called'])} ({', '.join(t['name'] for t in result['tools_called'])})")
    print(f"  Cost:            ${result['cost_usd']:.4f}")
    print(f"  Tokens:          {result['input_tokens']} in / {result['output_tokens']} out")
    print(f"  Duration:        {result['duration_ms']}ms")
    print(f"{'='*64}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Ask Chad's AI assistant a question. Retrieves from Drive + Postgres."
    )
    parser.add_argument(
        "question", nargs="*", default=None,
        help="The question to ask. Quote it on the shell.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit structured JSON instead of pretty terminal output.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show tool-call activity on stderr while Claude reasons.",
    )
    args = parser.parse_args()

    question = " ".join(args.question).strip() if args.question else ""
    if not question:
        parser.error("Provide a question (e.g. hb-ask \"what's the status of Whitfield framing?\")")

    try:
        result = ask_question(question, verbose=args.verbose)
    except Exception as e:
        if args.json:
            print(json.dumps({"error": True, "type": type(e).__name__, "message": str(e)}, indent=2))
        else:
            print(f"\n❌ hb-ask failed: {type(e).__name__}: {e}\n", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_pretty(result)


if __name__ == "__main__":
    main()
