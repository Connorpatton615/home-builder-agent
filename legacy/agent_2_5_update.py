"""
agent_2_5_update.py — Project Status Update Agent (Stages 2-3 of Agent 2.5).

Takes a natural-language status update and applies it to the project tracker:

  python3 agent_2_5_update.py "Phase 3 pushed 1 week — I-joist lead time"

Pipeline:
  1. Parse the update text into a structured change (Claude Haiku, ~$0.005)
  2. Compute cascade impact (Python, $0)
  3. Apply changes to Master Schedule tab via Sheets API
  4. Refresh Dashboard tab with new metrics
  5. Generate a Chad-style summary (Claude Sonnet w/ comm rules, ~$0.02)

Total cost per update: ~$0.02-0.05.

Reuses helpers from agent_2_5_dashboard.py (must be in same directory).
"""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta

# Allow OAuth scope flexibility (set BEFORE oauth libs load)
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from anthropic import Anthropic
from dotenv import load_dotenv
from googleapiclient.discovery import build

# Reuse Stage 1 helpers
from agent_2_5_dashboard import (
    DRIVE_FOLDER_PATH,
    compute_dashboard_metrics,
    ensure_dashboard_tab,
    extract_project_name,
    find_latest_tracker,
    get_credentials,
    read_master_schedule,
    write_dashboard,
)


# --- Config ----------------------------------------------------------

WORKSPACE = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-Connorpatton615@icloud.com/"
    "My Drive/Home Building Agent V.1/Home Builder Agent V.1"
)
KNOWLEDGE_BASE_DIR = "KNOWLEDGE BASE"
COMM_RULES_FILE = "chad_communication_rules.md"

# Models — Haiku for parsing (cheap, structured), Sonnet for summary (Chad voice)
PARSER_MODEL = "claude-haiku-4-5-20251001"
SUMMARY_MODEL = "claude-sonnet-4-6"
PARSER_MAX_TOKENS = 500
SUMMARY_MAX_TOKENS = 1500

# Pricing (USD per million tokens)
HAIKU_INPUT_COST = 1.0
HAIKU_OUTPUT_COST = 5.0
SONNET_INPUT_COST = 3.0
SONNET_OUTPUT_COST = 15.0


# --- Step 1: Parse update text -------------------------------------

def parse_update_text(client, update_text, phases):
    """Parse a natural-language status update into structured JSON.

    Uses Claude Haiku (small, cheap) since this is a structured extraction task,
    not a creative writing task.
    """
    phase_list = "\n".join(
        f"  {p.get('#', '?')}. {p.get('Phase', '')} — {p.get('Status', '')}"
        for p in phases
    )

    prompt = f"""You are a parser. Convert a builder's status update into structured JSON.

PROJECT PHASES (number, name, current status):
{phase_list}

UPDATE FROM CHAD:
"{update_text}"

Return ONLY a JSON object (no markdown fence, no explanation, no preamble).

Schema:
{{
  "phase_number": <int>,
  "change_type": "<delay|completed|started|blocked|unblocked|status_change>",
  "magnitude_weeks": <float>,
  "reason": "<brief reason>",
  "new_status": "<Done|In Progress|Not Started|Blocked|Delayed|null>"
}}

Field rules:
- phase_number: identify which phase by number, from the list above. If Chad mentioned a name like "framing", match to the phase whose name contains that word.
- change_type: pick the best match. "delay"=schedule slip; "completed"=done; "started"=now in progress; "blocked"=cannot proceed; "unblocked"=resumes after block; "status_change"=other status update.
- magnitude_weeks: positive=delay, negative=ahead-of-schedule. Use 0 for pure status changes (no date impact).
- new_status: which Status dropdown value to set. Use "null" (string null, not the JSON value) if no status change implied.

Examples:
  Input: "Phase 3 pushed 1 week"
  Output: {{"phase_number": 3, "change_type": "delay", "magnitude_weeks": 1.0, "reason": "schedule slip", "new_status": "Delayed"}}

  Input: "Foundation done"
  Output: {{"phase_number": 3, "change_type": "completed", "magnitude_weeks": 0, "reason": "phase complete", "new_status": "Done"}}

  Input: "Started framing"
  Output: {{"phase_number": 4, "change_type": "started", "magnitude_weeks": 0, "reason": "began work", "new_status": "In Progress"}}

  Input: "Phase 5 finished 3 days early"
  Output: {{"phase_number": 5, "change_type": "completed", "magnitude_weeks": -0.43, "reason": "ahead of schedule", "new_status": "Done"}}
"""

    response = client.messages.create(
        model=PARSER_MODEL,
        max_tokens=PARSER_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    # Strip code fence if Haiku added one
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)

    try:
        change = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Could not parse parser output as JSON. Raw output:\n{text[:500]}\n"
            f"Error: {e}"
        )

    # Normalize: convert string "null" to None
    if change.get("new_status") in ("null", "None", ""):
        change["new_status"] = None

    return change, response.usage


# --- Step 2: Cascade computation ------------------------------------

def parse_iso_date(s):
    """Parse YYYY-MM-DD into a date object. Return None if invalid."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def fmt_date(d):
    return d.isoformat() if d else ""


def find_phase_index_by_number(phases, target_num):
    """Find the list index of a phase by its '#' column value."""
    for i, p in enumerate(phases):
        try:
            if int(p.get("#", -1)) == int(target_num):
                return i
        except (ValueError, TypeError):
            continue
    return None


def compute_cascade(phases, change):
    """Apply a structured change to phases. Return list of (idx, updated_phase)."""
    target_num = change["phase_number"]
    magnitude = float(change.get("magnitude_weeks", 0) or 0)
    new_status = change.get("new_status")

    target_idx = find_phase_index_by_number(phases, target_num)
    if target_idx is None:
        raise ValueError(
            f"Phase #{target_num} not found in schedule "
            f"(available: {[p.get('#') for p in phases]})"
        )

    updates = []  # list of (idx, updated_phase_dict)

    # 1. Update the target phase (status + maybe end date)
    target = dict(phases[target_idx])
    if new_status:
        target["Status"] = new_status
    if magnitude != 0:
        end = parse_iso_date(target.get("End"))
        if end:
            target["End"] = fmt_date(end + timedelta(weeks=magnitude))
    updates.append((target_idx, target))

    # 2. Cascade to downstream phases (only if there's a date change)
    # Linear shift: every later phase moves by the same magnitude.
    # Edge case: parallel-track phases (e.g., Pool) shouldn't cascade. We
    # detect those by their Dependencies column not naming the target phase
    # in the dependency chain — but for v1 we apply the simple linear model
    # and flag this for refinement.
    if magnitude != 0:
        for i in range(target_idx + 1, len(phases)):
            p = dict(phases[i])
            start = parse_iso_date(p.get("Start"))
            end = parse_iso_date(p.get("End"))
            if start:
                p["Start"] = fmt_date(start + timedelta(weeks=magnitude))
            if end:
                p["End"] = fmt_date(end + timedelta(weeks=magnitude))
            updates.append((i, p))

    return updates


# --- Step 3: Apply changes to Master Schedule -----------------------

def apply_to_master_schedule(sheets_service, sheet_id, updates):
    """Push updated phase rows back to the Master Schedule tab.

    Master Schedule columns (in order): #, Phase, Weeks, Start, End, Status, Dependencies
    """
    if not updates:
        return

    data = []
    for phase_idx, phase in updates:
        # Header is row 1, phase data starts row 2 (so phase_idx 0 = row 2)
        row_num = phase_idx + 2
        row_values = [
            phase.get("#", ""),
            phase.get("Phase", ""),
            phase.get("Weeks", ""),
            phase.get("Start", ""),
            phase.get("End", ""),
            phase.get("Status", ""),
            phase.get("Dependencies", ""),
        ]
        data.append({
            "range": f"Master Schedule!A{row_num}:G{row_num}",
            "values": [row_values],
        })

    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()


# --- Step 5: Generate summary in Chad's voice -----------------------

def load_comm_rules():
    """Load Chad's communication rules from the knowledge base."""
    path = os.path.join(WORKSPACE, KNOWLEDGE_BASE_DIR, COMM_RULES_FILE)
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return ""


def generate_summary(client, change, updates, original_phases, project_name, today=None):
    """Generate a Chad-style summary of the applied change."""
    if today is None:
        today = date.today()

    comm_rules = load_comm_rules()

    target_idx = find_phase_index_by_number(original_phases, change["phase_number"])
    target_phase_name = original_phases[target_idx].get("Phase", "") if target_idx is not None else ""

    # Compute cascade impact for the summary
    n_phases_affected = len(updates)
    last_phase_old = original_phases[-1] if original_phases else {}
    last_phase_new_tuple = next(
        (u for u in updates if u[0] == len(original_phases) - 1), None
    )
    last_phase_new = last_phase_new_tuple[1] if last_phase_new_tuple else last_phase_old

    original_completion = last_phase_old.get("End", "")
    revised_completion = last_phase_new.get("End", "")

    target_old = original_phases[target_idx] if target_idx is not None else {}
    target_new = next((u[1] for u in updates if u[0] == target_idx), {})

    system_prompt = f"""You are a project communication agent for Chad's Custom Homes (Baldwin County, AL luxury custom homes).

Apply Chad's preferred communication style strictly:

<chad_communication_rules>
{comm_rules}
</chad_communication_rules>

You write status updates that reflect a change just applied to the project schedule. Output is a brief Markdown block, status-led, scannable. No greetings, no walls of text, no enthusiasm or hype. If a question is asked, answer it; otherwise no questions."""

    user_prompt = f"""Generate a status update for Chad based on the change just applied.

PROJECT: {project_name}
DATE: {today.isoformat()}

CHANGE APPLIED:
- Target phase: #{change['phase_number']} — {target_phase_name}
- Change type: {change.get('change_type', '')}
- Magnitude: {change.get('magnitude_weeks', 0)} weeks
- Reason: {change.get('reason', '')}
- New status: {change.get('new_status', '—')}

PHASE-LEVEL DATES:
- Target phase end: was {target_old.get('End', '?')} → now {target_new.get('End', '?')}

CASCADE:
- {n_phases_affected} total phases updated
- Project completion: was {original_completion} → now {revised_completion}

OUTPUT FORMAT (Markdown):
- Lead line: status-led, 1 sentence (e.g. "Status: Phase 3 pushed 1 week. Schedule updated.")
- Brief table or bullets showing key dates that moved
- 1-2 sentence cascade summary
- Risks (if any worth flagging)
- Next action (1-2 bullets, recommended actions)

Keep it operator-tight."""

    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=SUMMARY_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return response.content[0].text, response.usage


# --- Main ------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 agent_2_5_update.py \"<status update text>\"")
        print("\nExample updates:")
        print('  python3 agent_2_5_update.py "Phase 3 pushed 1 week — I-joist lead time"')
        print('  python3 agent_2_5_update.py "Foundation done"')
        print('  python3 agent_2_5_update.py "Started framing"')
        sys.exit(1)

    update_text = " ".join(sys.argv[1:])
    print(f"Status update received:")
    print(f'  "{update_text}"\n')

    # 1. Auth + services
    print("Authenticating...")
    creds = get_credentials()
    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)

    load_dotenv()
    anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # 2. Find the latest Tracker
    print("\nFinding latest Tracker...")
    tracker = find_latest_tracker(drive_service, DRIVE_FOLDER_PATH)
    project_name = extract_project_name(tracker["name"])
    print(f"  Project: {project_name}")
    print(f"  Sheet: {tracker['name']}")

    # 3. Read current state
    print("\nReading current schedule...")
    phases = read_master_schedule(sheets_service, tracker["id"])
    print(f"  {len(phases)} phases loaded")

    # 4. Parse update via Haiku
    print(f"\nParsing update via {PARSER_MODEL}...")
    change, parser_usage = parse_update_text(anthropic_client, update_text, phases)
    print(f"  Phase #{change.get('phase_number')}, "
          f"type={change.get('change_type')}, "
          f"magnitude={change.get('magnitude_weeks')} weeks, "
          f"new_status={change.get('new_status')}")

    # 5. Compute cascade
    print("\nComputing cascade...")
    updates = compute_cascade(phases, change)
    print(f"  {len(updates)} phase rows will update")

    # 6. Apply to Master Schedule
    print("\nApplying to Master Schedule...")
    apply_to_master_schedule(sheets_service, tracker["id"], updates)

    # 7. Refresh Dashboard tab
    print("\nRefreshing Dashboard...")
    refreshed_phases = read_master_schedule(sheets_service, tracker["id"])
    metrics = compute_dashboard_metrics(refreshed_phases)
    dashboard_sheet_id = ensure_dashboard_tab(sheets_service, tracker["id"])
    write_dashboard(sheets_service, tracker["id"], dashboard_sheet_id,
                    metrics, project_name)

    # 8. Generate summary via Sonnet (with Chad's voice)
    print(f"\nGenerating summary via {SUMMARY_MODEL}...")
    summary, summary_usage = generate_summary(
        anthropic_client, change, updates, phases, project_name
    )

    # 9. Cost reporting
    parser_cost = (
        parser_usage.input_tokens * HAIKU_INPUT_COST / 1_000_000
        + parser_usage.output_tokens * HAIKU_OUTPUT_COST / 1_000_000
    )
    summary_cost = (
        summary_usage.input_tokens * SONNET_INPUT_COST / 1_000_000
        + summary_usage.output_tokens * SONNET_OUTPUT_COST / 1_000_000
    )

    # 10. Output
    print("\n" + "=" * 60)
    print("STATUS UPDATE APPLIED")
    print("=" * 60)
    print()
    print(summary)
    print()
    print("=" * 60)
    print(f"Sheet:  {tracker['webViewLink']}")
    print(f"Cost:   parse=${parser_cost:.4f}, summary=${summary_cost:.4f}, "
          f"total=${parser_cost + summary_cost:.4f}")
    print()


if __name__ == "__main__":
    main()
