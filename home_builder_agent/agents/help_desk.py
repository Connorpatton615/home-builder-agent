"""help_desk.py — AI Help Desk agent.

Answers questions about the Chad's Custom Homes AI Agent system.
Knows every slash command, agent behavior, and common troubleshooting step.
When an answer is informative enough to be reusable, it automatically appends
the Q&A to the living FAQ Google Doc in Drive.

Two audiences:
  Now:  Connor (technical operator) — needs specific commands, paths, log
        locations for debugging.
  Soon: Chad (business owner, non-technical) — needs plain-English guidance
        from his phone on a job site.

The system prompt writes answers accessible to Chad while complete enough for
Connor to act on — no need to switch modes.

CLI:
  hb-help "the watcher isn't showing up in launchctl"
  hb-help how do I run the inbox checklist
  hb-help "what does the Dashboard tab show" --no-faq

Cost: ~$0.02–0.05 per question (Sonnet, short context)
"""

import argparse
import json
import os
import re

from home_builder_agent.config import (
    CLAUDE_COMMANDS_DIR,
    HELP_DESK_DOC_FOLDER,
    HELP_DESK_STATE_FILE,
    WRITER_MODEL,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import make_client, sonnet_cost
from home_builder_agent.integrations import docs as docs_int
from home_builder_agent.integrations import drive

FAQ_DOC_NAME = "Agent FAQ & Troubleshooting Guide"
FAQ_INITIAL_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body>
<h1>Agent FAQ &amp; Troubleshooting Guide</h1>
<p><em>Chad's Custom Homes — maintained automatically by the Help Desk Agent</em></p>
<hr>
<p>Questions and answers are added here as they come up. Run <strong>/help</strong> any time you have a question about the system.</p>
<hr>
</body></html>"""


# ------------------------------------------------------------------
# State
# ------------------------------------------------------------------

def load_state():
    if not os.path.exists(HELP_DESK_STATE_FILE):
        return {}
    try:
        with open(HELP_DESK_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = HELP_DESK_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, HELP_DESK_STATE_FILE)


# ------------------------------------------------------------------
# Knowledge base
# ------------------------------------------------------------------

def load_command_docs():
    """Read .claude/commands/*.md and return as one combined string."""
    commands_dir = os.path.abspath(CLAUDE_COMMANDS_DIR)
    if not os.path.isdir(commands_dir):
        return "(command documentation not found)"
    entries = []
    for fname in sorted(os.listdir(commands_dir)):
        if fname.endswith(".md"):
            try:
                content = open(os.path.join(commands_dir, fname)).read()
                entries.append(f"### {fname}\n{content}")
            except Exception:
                pass
    return "\n\n---\n\n".join(entries)


def find_or_create_faq_doc(drive_svc, state):
    """Return FAQ doc ID, creating the doc in Drive on first run."""
    faq_id = state.get("faq_doc_id")
    if faq_id:
        return faq_id
    print("First run: creating FAQ doc in Drive...")
    folder_id = drive.find_folder_by_path(drive_svc, HELP_DESK_DOC_FOLDER)
    result = drive.upload_as_google_doc(
        drive_svc, FAQ_INITIAL_HTML, FAQ_DOC_NAME, folder_id
    )
    faq_id = result["id"]
    state["faq_doc_id"] = faq_id
    save_state(state)
    print(f"  Created: {result['webViewLink']}")
    return faq_id


# ------------------------------------------------------------------
# Sonnet call
# ------------------------------------------------------------------

def answer_question(client, question, command_docs, faq_text):
    """Ask Sonnet to answer the question and decide if the Q&A belongs in FAQ."""
    system = f"""You are the help desk agent for the Chad's Custom Homes AI Agent system.

The system automates back-office work for a luxury custom home builder in Baldwin County, AL. It runs on a Mac Mini and connects to Gmail, Google Drive, Google Docs, and Google Sheets via the Claude API.

AUDIENCE:
- Right now: Connor (the technical operator who built and maintains the system)
- Soon: Chad (the business owner — non-technical, uses it from his phone on job sites)

Write every answer so Chad could follow it without help (plain English, no jargon), while including enough technical detail (commands, file paths, log locations) that Connor can debug without asking follow-up questions.

SLASH COMMAND DOCUMENTATION:
{command_docs}

EXISTING FAQ ENTRIES:
{faq_text or "(none yet — this is the first question)"}

Return ONLY a valid JSON object — no markdown fence, no preamble:
{{
  "answer": "<your full answer — plain English, step-by-step where helpful>",
  "add_to_faq": <true|false>,
  "faq_question": "<concise version of the question for the FAQ doc, or null>",
  "faq_answer": "<clean standalone answer for the FAQ doc, or null>"
}}

Set add_to_faq=true when the answer:
- Reveals non-obvious behavior or a common point of confusion
- Covers a troubleshooting scenario Chad might hit independently
- Contains a workflow tip that improves how the system is used

Do NOT add: obvious questions, one-off state-specific questions, or anything already in the existing FAQ."""

    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": question}],
    )

    text = response.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text), response.usage
    except json.JSONDecodeError:
        return {
            "answer": text,
            "add_to_faq": False,
            "faq_question": None,
            "faq_answer": None,
        }, response.usage


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Ask a question about the AI Agent system."
    )
    parser.add_argument("question", nargs="+",
                        help="Your question — quoted or unquoted")
    parser.add_argument("--no-faq", action="store_true",
                        help="Skip FAQ update even if the answer is worth saving")
    args = parser.parse_args()
    question = " ".join(args.question)

    print("Authenticating...")
    creds = get_credentials()
    drive_svc = drive.drive_service(creds)
    docs_svc = docs_int.docs_service(creds)
    client = make_client()

    state = load_state()
    faq_id = find_or_create_faq_doc(drive_svc, state)

    print("Loading knowledge base...")
    command_docs = load_command_docs()
    faq_text = docs_int.read_doc_text(docs_svc, faq_id)

    print("Thinking...\n")
    result, usage = answer_question(client, question, command_docs, faq_text)

    print("=" * 60)
    print(result["answer"])
    print("=" * 60)

    if result.get("add_to_faq") and result.get("faq_question") and not args.no_faq:
        entry = (
            f"\n\n**Q: {result['faq_question']}**\n\n"
            f"{result['faq_answer']}\n"
        )
        docs_int.append_text_to_doc(docs_svc, faq_id, entry)
        print(f"\n  → Added to FAQ: \"{result['faq_question']}\"")

    cost = sonnet_cost(usage)
    print(f"\nCost: ${cost['total']:.4f}")


if __name__ == "__main__":
    main()
