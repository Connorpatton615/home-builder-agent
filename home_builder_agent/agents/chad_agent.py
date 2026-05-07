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

from anthropic import Anthropic

from home_builder_agent.core.chad_context import get_chad_context
from home_builder_agent.core.chad_voice import (
    CUSTOMER_NAME,
    chad_voice_system,
)
from home_builder_agent.core.claude_client import make_client


# ---------------------------------------------------------------------------
# Model + loop config
# ---------------------------------------------------------------------------

CHAD_MODEL = "claude-opus-4-7"
CHAD_MAX_TOKENS = 4096
MAX_TOOL_LOOP_ITERATIONS = 8  # safety guard

# Pricing per 1M tokens for cost reporting (claude-opus-4-7).
# Mirrors the constants in claude_client; duplicated here so a single import
# lights up the cost line without pulling in the helper's full surface.
OPUS_INPUT_PER_M = 15.00
OPUS_OUTPUT_PER_M = 75.00


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
  2. Decide whether the turn needs retrieval (call ask_chad), action
     (call dispatch_action), both, or neither.
  3. Compose a response in Chad-voice prose. If you took an action,
     report it concretely. If you drafted something for Chad to send,
     say so and tell him where it is (Gmail Drafts, etc.).
  4. Suggest the obvious follow-up if there is one. Don't fabricate
     follow-ups when none is needed.

Style rules:
  • Tight, operator prose. No hype, no AI hedging.
  • If you don't know something and ask_chad won't tell you, say so plainly.
  • Outbound communications (emails to clients/subs/vendors) are
    drafted only — never auto-sent. Always tell Chad where the draft
    landed and what to do next.
  • The tools below are the only way to read truth or change state.
    Don't invent project status, costs, or schedule data — call
    ask_chad if you need it.
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
            "citations."
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
            "create a change order, log a site entry, log an inspection, "
            "log a lien waiver, etc. Delegates to hb-router which classifies "
            "the NL command, dispatches the right specialist agent, and "
            "writes the engine_activity audit row. Use this for anything "
            "that should mutate Tracker / Cost Tracker / Drive / engine "
            "state. Each call is logged."
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
# The agent loop
# ---------------------------------------------------------------------------

def chad_turn(user_input: str, *, dry_run: bool = False, verbose: bool = False) -> dict:
    """Run one Chad-agent turn. Returns a dict shaped for CLI / API use.

    Shape:
      {
        "input":         "<original user input>",
        "answer":        "<Chad-voice prose>",
        "actions_taken": [ ... summary of dispatch_action calls ... ],
        "tool_log":      [ {name, input, duration_ms, cost} ... ],
        "model":         "claude-opus-4-7",
        "cost_usd":      0.123,           # opus turns + downstream specialists
        "duration_ms":   2345,
      }
    """
    started_at = time.time()
    client: Anthropic = make_client()

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

    for iteration in range(MAX_TOOL_LOOP_ITERATIONS):
        if verbose:
            print(f"[iter {iteration + 1}] calling Opus...", file=sys.stderr)

        response = client.messages.create(
            model=CHAD_MODEL,
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

    opus_cost = (
        total_input_tokens * OPUS_INPUT_PER_M / 1_000_000
        + total_output_tokens * OPUS_OUTPUT_PER_M / 1_000_000
    )
    duration_ms = int((time.time() - started_at) * 1000)

    return {
        "input": user_input,
        "answer": final_text.strip(),
        "actions_taken": actions_taken,
        "tool_log": tool_log,
        "model": CHAD_MODEL,
        "cost_usd": round(opus_cost + downstream_cost, 4),
        "opus_cost_usd": round(opus_cost, 4),
        "downstream_cost_usd": round(downstream_cost, 4),
        "duration_ms": duration_ms,
        "iterations": iteration + 1,
    }


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
        "--verbose", action="store_true",
        help="Print tool-loop progress to stderr.",
    )
    args = parser.parse_args()

    result = chad_turn(args.input, dry_run=args.dry_run, verbose=args.verbose)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_pretty(result)


if __name__ == "__main__":
    main()
