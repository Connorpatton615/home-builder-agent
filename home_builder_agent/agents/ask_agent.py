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
    "list_projects":           _tool_list_projects,
    "get_project_status":      _tool_get_project_status,
    "search_drive":            _tool_search_drive,
    "read_drive_file":         _tool_read_drive_file,
    "read_knowledge_base":     _tool_read_knowledge_base,
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
  - search_drive: keyword-search his Google Drive
  - read_drive_file: open a specific file (THIS counts as a citation)
  - read_knowledge_base: Baldwin County codes, supplier list, Chad's voice rules

PROCESS:
  1. Decide which tools to call. Most questions need get_project_status
     OR search_drive + read_drive_file. Don't over-retrieve — 1-3 tool
     calls is normal; 5+ is a sign you're going down a rabbit hole.
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
