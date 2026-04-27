"""status_updater.py — natural-language project status updates.

CLI: hb-update "Phase 3 pushed 1 week — I-joist lead time"

Pipeline:
  1. Parse the update text into structured JSON (Claude Haiku, ~$0.005)
  2. Compute cascade impact through the dependency graph (Python, $0)
  3. Apply changes to Master Schedule tab
  4. Refresh Dashboard tab with new metrics
  5. Generate a Chad-voice summary (Claude Sonnet, ~$0.02)

Total: ~$0.02–0.05 per update.

The cascade walks the actual phase dependency graph — parallel-track phases
(e.g. "Pool — separate contract — parallel track") whose Dependencies don't
reference the shifted phase stay put. Linear-chain phases shift together.
"""

import json
import re
import sys
from datetime import date, datetime, timedelta

from home_builder_agent.config import (
    CLASSIFIER_MODEL,
    DRIVE_FOLDER_PATH,
    UPDATE_PARSER_MAX_TOKENS,
    UPDATE_SUMMARY_MAX_TOKENS,
    WRITER_MODEL,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import (
    haiku_cost,
    make_client,
    sonnet_cost,
)
from home_builder_agent.core.knowledge_base import load_comm_rules
from home_builder_agent.integrations import drive, sheets


# ---------------------------------------------------------------------
# Step 1: Parse update text via Haiku
# ---------------------------------------------------------------------

def parse_update_text(client, update_text, phases):
    """Parse a NL update into structured JSON. Returns (change_dict, usage)."""
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
        model=CLASSIFIER_MODEL,
        max_tokens=UPDATE_PARSER_MAX_TOKENS,
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
            f"Parser output not parseable as JSON. Raw:\n{text[:500]}\nError: {e}"
        )

    if change.get("new_status") in ("null", "None", ""):
        change["new_status"] = None

    return change, response.usage


# ---------------------------------------------------------------------
# Step 2: Cascade computation (pure Python)
# ---------------------------------------------------------------------

def parse_iso_date(s):
    """Parse YYYY-MM-DD into a date. Return None if invalid."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def fmt_date(d):
    return d.isoformat() if d else ""


def find_phase_index_by_number(phases, target_num):
    """Find list index of a phase by its '#' column value."""
    for i, p in enumerate(phases):
        try:
            if int(p.get("#", -1)) == int(target_num):
                return i
        except (ValueError, TypeError):
            continue
    return None


def compute_cascade(phases, change):
    """Apply a structured change to phases. Returns list of (idx, updated_phase).

    Walks the actual dependency graph from the target phase forward (BFS).
    Only phases whose dependency chain references the target shift; parallel
    tracks stay put.
    """
    target_num = change["phase_number"]
    magnitude = float(change.get("magnitude_weeks", 0) or 0)
    new_status = change.get("new_status")

    target_idx = find_phase_index_by_number(phases, target_num)
    if target_idx is None:
        raise ValueError(
            f"Phase #{target_num} not found in schedule "
            f"(available: {[p.get('#') for p in phases]})"
        )

    updates = []

    # 1. Update the target phase (status + maybe end date)
    target = dict(phases[target_idx])
    if new_status:
        target["Status"] = new_status
    if magnitude != 0:
        end = parse_iso_date(target.get("End"))
        if end:
            target["End"] = fmt_date(end + timedelta(weeks=magnitude))
    updates.append((target_idx, target))

    # 2. BFS forward through dependents
    if magnitude != 0:
        def normalize(s):
            return (s or "").strip().lower()

        deps_by_idx = {}
        for i, p in enumerate(phases):
            deps_str = p.get("Dependencies", "") or ""
            deps_by_idx[i] = [
                normalize(d) for d in deps_str.split(",") if d.strip()
            ]

        target_name = normalize(phases[target_idx].get("Phase"))
        affected = set()
        frontier = [target_name]
        while frontier:
            current = frontier.pop(0)
            for i, deps in deps_by_idx.items():
                if i == target_idx or i in affected:
                    continue
                if current in deps:
                    affected.add(i)
                    frontier.append(normalize(phases[i].get("Phase")))

        # Apply magnitude shift to every affected phase's Start AND End
        for i in sorted(affected):
            p = dict(phases[i])
            start = parse_iso_date(p.get("Start"))
            end = parse_iso_date(p.get("End"))
            if start:
                p["Start"] = fmt_date(start + timedelta(weeks=magnitude))
            if end:
                p["End"] = fmt_date(end + timedelta(weeks=magnitude))
            updates.append((i, p))

    return updates


# ---------------------------------------------------------------------
# Step 5: Generate Chad-voice summary
# ---------------------------------------------------------------------

def generate_summary(client, change, updates, original_phases, project_name, today=None):
    """Generate a Chad-style summary of the applied change."""
    if today is None:
        today = date.today()

    comm_rules = load_comm_rules()

    target_idx = find_phase_index_by_number(original_phases, change["phase_number"])
    target_phase_name = (
        original_phases[target_idx].get("Phase", "") if target_idx is not None else ""
    )

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

    system_prompt = f"""You are a project communication agent for Palmetto Custom Homes (Baldwin County, AL luxury custom homes).

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
        model=WRITER_MODEL,
        max_tokens=UPDATE_SUMMARY_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return response.content[0].text, response.usage


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print('Usage: hb-update "<status update text>"')
        print()
        print("Example updates:")
        print('  hb-update "Phase 3 pushed 1 week — I-joist lead time"')
        print('  hb-update "Foundation done"')
        print('  hb-update "Started framing"')
        sys.exit(1)

    update_text = " ".join(sys.argv[1:])
    print("Status update received:")
    print(f'  "{update_text}"\n')

    print("Authenticating...")
    creds = get_credentials()
    drive_svc = drive.drive_service(creds)
    sheets_svc = sheets.sheets_service(creds)

    client = make_client()

    print("\nFinding latest Tracker...")
    tracker = drive.find_latest_tracker(drive_svc, DRIVE_FOLDER_PATH)
    project_name = drive.extract_project_name(tracker["name"])
    print(f"  Project: {project_name}")
    print(f"  Sheet: {tracker['name']}")

    print("\nReading current schedule...")
    phases = sheets.read_master_schedule(sheets_svc, tracker["id"])
    print(f"  {len(phases)} phases loaded")

    print(f"\nParsing update via {CLASSIFIER_MODEL}...")
    change, parser_usage = parse_update_text(client, update_text, phases)
    print(f"  Phase #{change.get('phase_number')}, "
          f"type={change.get('change_type')}, "
          f"magnitude={change.get('magnitude_weeks')} weeks, "
          f"new_status={change.get('new_status')}")

    print("\nComputing cascade...")
    updates = compute_cascade(phases, change)
    print(f"  {len(updates)} phase rows will update")

    print("\nApplying to Master Schedule...")
    sheets.apply_phase_updates(sheets_svc, tracker["id"], updates)

    print("\nRefreshing Dashboard...")
    refreshed_phases = sheets.read_master_schedule(sheets_svc, tracker["id"])
    metrics = sheets.compute_dashboard_metrics(refreshed_phases)
    dashboard_sheet_id = sheets.ensure_dashboard_tab(sheets_svc, tracker["id"])
    sheets.write_dashboard(sheets_svc, tracker["id"], dashboard_sheet_id,
                           metrics, project_name)

    print(f"\nGenerating summary via {WRITER_MODEL}...")
    summary, summary_usage = generate_summary(
        client, change, updates, phases, project_name
    )

    parser_usd = haiku_cost(parser_usage)
    summary_usd = sonnet_cost(summary_usage)["total"]

    print("\n" + "=" * 60)
    print("STATUS UPDATE APPLIED")
    print("=" * 60)
    print()
    print(summary)
    print()
    print("=" * 60)
    print(f"Sheet:  {tracker['webViewLink']}")
    print(f"Cost:   parse=${parser_usd:.4f}, summary=${summary_usd:.4f}, "
          f"total=${parser_usd + summary_usd:.4f}")
    print()


if __name__ == "__main__":
    main()
