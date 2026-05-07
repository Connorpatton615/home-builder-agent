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
