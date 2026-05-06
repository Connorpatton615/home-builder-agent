"""client_update_agent.py — weekly homeowner project status email.

CLI: hb-client-update --to EMAIL --client-name "John & Mary Smith" [options]

Pipeline:
  1. Read Master Schedule → project snapshot (phases, %, current, upcoming)
  2. Read Change Orders tab → any recent COs to surface
  3. Generate polished homeowner-facing email via Claude Sonnet (Chad's voice,
     but warm and client-appropriate — not the operator-tight internal style)
  4. Create Gmail draft by default; --send to send immediately

Default: draft mode. Chad reviews and sends from Gmail. Use --send only when
the email is fully trusted / you want fully automated weekly delivery.

Cost: ~$0.02–0.03/run (one Sonnet call).
"""

import argparse
import sys
from datetime import date, datetime, timedelta

from home_builder_agent.config import (
    DRIVE_FOLDER_PATH,
    WRITER_MODEL,
    UPDATE_SUMMARY_MAX_TOKENS,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import make_client, sonnet_cost
from home_builder_agent.integrations import drive, gmail, sheets


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _fmt(d):
    """Format a date as 'May 12' style."""
    if not d:
        return "TBD"
    return d.strftime("%B %-d")


def _weeks_out(d, today):
    if not d:
        return None
    delta = (d - today).days
    if delta < 0:
        return None
    weeks = delta // 7
    days = delta % 7
    if weeks == 0:
        return f"{days} day{'s' if days != 1 else ''}"
    if days == 0:
        return f"{weeks} week{'s' if weeks != 1 else ''}"
    return f"{weeks}w {days}d"


def build_project_snapshot(phases, today=None):
    """Distill phases into a homeowner-readable snapshot dict."""
    if today is None:
        today = date.today()

    n_total = len(phases)
    n_done = sum(1 for p in phases if (p.get("Status") or "").strip() == "Done")
    n_in_progress = sum(
        1 for p in phases if (p.get("Status") or "").strip() == "In Progress"
    )
    pct = round((n_done + n_in_progress * 0.5) / n_total * 100) if n_total else 0

    # Current phase (first non-Done)
    current = next(
        (p for p in phases if (p.get("Status") or "").strip() != "Done"), None
    )
    # Upcoming = phase after current
    current_idx = phases.index(current) if current else None
    upcoming_phases = (
        [p for p in phases[current_idx + 1 : current_idx + 4]
         if (p.get("Status") or "").strip() == "Not Started"]
        if current_idx is not None else []
    )

    # Recently completed (Done phases with End within last 14 days)
    recently_done = [
        p for p in phases
        if (p.get("Status") or "").strip() == "Done"
        and _parse_date(p.get("End"))
        and (today - _parse_date(p.get("End"))).days <= 14
    ]

    # Overall completion date
    completion_date = _parse_date(phases[-1].get("End")) if phases else None

    # Any blocked phases
    blocked = [p for p in phases if (p.get("Status") or "").strip() == "Blocked"]

    return {
        "n_total": n_total,
        "n_done": n_done,
        "pct": pct,
        "current": current,
        "upcoming_phases": upcoming_phases,
        "recently_done": recently_done,
        "completion_date": completion_date,
        "blocked": blocked,
        "today": today,
    }


def read_recent_change_orders(sheets_svc, sheet_id, days_back=30):
    """Read Change Orders tab and return COs from the last N days."""
    try:
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Change Orders!A1:I200",
        ).execute()
        rows = result.get("values", [])
        if not rows or len(rows) < 2:
            return []
        headers = rows[0]
        cutoff = date.today() - timedelta(days=days_back)
        cos = []
        for row in rows[1:]:
            padded = list(row) + [""] * (len(headers) - len(row))
            co = dict(zip(headers, padded))
            co_date = _parse_date(co.get("Date"))
            if co_date and co_date >= cutoff and co.get("CO #"):
                cos.append(co)
        return cos
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Email generation
# ---------------------------------------------------------------------------

def generate_client_email(
    client,
    project_name: str,
    client_name: str,
    snapshot: dict,
    recent_cos: list[dict],
    today: date,
) -> tuple[str, str, object]:
    """Call Sonnet to write the homeowner email. Returns (subject, html_body, usage)."""

    current = snapshot["current"]
    current_name = current.get("Phase", "") if current else "Final stages"
    current_status = current.get("Status", "") if current else ""
    current_start = _fmt(_parse_date(current.get("Start"))) if current else ""
    current_end = _fmt(_parse_date(current.get("End"))) if current else ""

    upcoming_lines = "\n".join(
        f"  - {p.get('Phase', '')} — starts {_fmt(_parse_date(p.get('Start')))}"
        for p in snapshot["upcoming_phases"]
    ) or "  (no further phases scheduled yet)"

    recently_done_lines = "\n".join(
        f"  - {p.get('Phase', '')} — completed {_fmt(_parse_date(p.get('End')))}"
        for p in snapshot["recently_done"]
    ) or "  (none in the last 2 weeks)"

    blocked_note = ""
    if snapshot["blocked"]:
        blocked_note = "BLOCKED PHASES (mention sensitively if relevant):\n" + "\n".join(
            f"  - {p.get('Phase', '')}" for p in snapshot["blocked"]
        )

    co_lines = ""
    if recent_cos:
        co_lines = "RECENT CHANGE ORDERS (last 30 days):\n" + "\n".join(
            f"  - {co.get('CO #')} ({co.get('Date')}): {co.get('Description', '')} "
            f"[${co.get('Cost Delta ($)', '0')}]"
            for co in recent_cos
        )

    completion_str = _fmt(snapshot["completion_date"]) if snapshot["completion_date"] else "TBD"
    weeks_out = _weeks_out(snapshot["completion_date"], today)
    completion_note = f"{completion_str} ({weeks_out} out)" if weeks_out else completion_str

    system_prompt = """You are the communication agent for Palmetto Custom Homes, a luxury custom home builder in Baldwin County, Alabama.

You write weekly homeowner update emails in Chad Whitfield's voice. Chad is the owner — professional, warm, straight-talking. These clients are spending $600k–$1.5M on their homes. The tone is:
- Warm and personal, not corporate
- Confident and reassuring, not over-promising
- Brief and scannable — busy clients, mobile readers
- No jargon, no builder-speak
- No hollow enthusiasm ("exciting progress!" etc.)
- One short paragraph per section max

You output clean HTML suitable for Gmail. Use inline styles sparingly. Structure:
- Warm greeting by first name if possible, otherwise "Hi [Name]"
- Brief opening (1 sentence — where things stand)
- "Progress This Week" section
- "What's Coming Next" section
- Schedule note (honest, not over-rosy)
- Any change orders or important notes (only if relevant)
- Warm close + Chad's signature block
"""

    user_prompt = f"""Write the weekly homeowner update email for:

PROJECT: {project_name}
CLIENT: {client_name}
DATE: {today.strftime("%B %-d, %Y")}

PROJECT DATA:
- Overall: {snapshot['pct']}% complete ({snapshot['n_done']} of {snapshot['n_total']} phases done)
- Current phase: {current_name} ({current_status}) — {current_start} through {current_end}
- Target completion: {completion_note}

RECENTLY COMPLETED (last 2 weeks):
{recently_done_lines}

COMING UP NEXT:
{upcoming_lines}

{blocked_note}
{co_lines}

OUTPUT REQUIREMENTS:
1. Subject line on its own line, prefixed with "Subject: "
2. Then a blank line
3. Then the full HTML email body (everything inside <body>...</body>, no <html>/<head> tags)

The HTML body should use a clean, minimal layout. Max width 600px, centered, white background, dark text (#1a1a1a). Section headers bold, 16px. Body text 15px, line-height 1.6. Keep it mobile-friendly — no complex tables.

Chad's signature block:
Chad Whitfield
Palmetto Custom Homes
Baldwin County, Alabama
(251) 555-0100  |  chad@palmettocustomhomes.com
"""

    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=UPDATE_SUMMARY_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()

    # Parse subject line out
    lines = raw.split("\n")
    subject = f"{project_name} — Weekly Update | {today.strftime('%B %-d, %Y')}"
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("subject:"):
            subject = line.strip()[len("subject:"):].strip()
            body_start = i + 1
            break

    html_body = "\n".join(lines[body_start:]).strip()

    # Wrap in minimal container if model didn't
    if "<body" not in html_body.lower() and "<div" not in html_body.lower():
        html_body = f"""<div style="max-width:600px;margin:0 auto;font-family:Georgia,serif;color:#1a1a1a;font-size:15px;line-height:1.6;">
{html_body}
</div>"""

    return subject, html_body, response.usage


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate and draft a homeowner weekly project update email."
    )
    parser.add_argument(
        "--to", default=None,
        help="Homeowner email address (or use --from-tracker)"
    )
    parser.add_argument(
        "--client-name", default=None, dest="client_name",
        help='Homeowner name(s), e.g. "John & Mary Smith" (or use --from-tracker)'
    )
    parser.add_argument(
        "--from-tracker", action="store_true", dest="from_tracker",
        help="Read recipient + name from the Tracker's Project Info tab "
             "(use this when running from a cron — no flags needed)"
    )
    parser.add_argument(
        "--send", action="store_true",
        help="Send immediately instead of creating a draft"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print email to stdout, no Gmail action"
    )
    args = parser.parse_args()

    today = date.today()

    # Validate recipient source — must have either explicit flags OR --from-tracker
    if not args.from_tracker and (not args.to or not args.client_name):
        parser.error(
            "must provide either (--to AND --client-name) OR --from-tracker"
        )

    print("Authenticating...")
    creds = get_credentials()
    drive_svc = drive.drive_service(creds)
    sheets_svc = sheets.sheets_service(creds)
    gmail_svc = gmail.gmail_service(creds)
    client = make_client()

    print("Finding latest Tracker...")
    tracker = drive.find_latest_tracker(drive_svc, DRIVE_FOLDER_PATH)
    project_name = drive.extract_project_name(tracker["name"])
    print(f"  Project: {project_name}")

    # Resolve recipient — flags override Tracker, then fallback to Tracker if --from-tracker
    recipient_email = args.to
    recipient_name = args.client_name
    if args.from_tracker:
        print("Reading Project Info tab...")
        info = sheets.read_project_info(sheets_svc, tracker["id"])
        if not info:
            print(f"  ⚠️  No Project Info tab on Tracker. Run hb-finance-style setup or fill the tab first.")
            print(f"  → Adding empty Project Info tab now; please populate Customer Name + Customer Email and re-run.")
            sheets.ensure_project_info_tab(sheets_svc, tracker["id"])
            sys.exit(1)
        # Fall back to Tracker values when CLI flags weren't passed
        if not recipient_email:
            recipient_email = info.get("Customer Email", "").strip()
        if not recipient_name:
            recipient_name = info.get("Customer Name", "").strip()
        print(f"  Customer Name:  {recipient_name or '(empty)'}")
        print(f"  Customer Email: {recipient_email or '(empty)'}")
        if not recipient_email or not recipient_name:
            print(f"\n❌ Project Info tab is missing Customer Name and/or Customer Email.")
            print(f"   Open the Tracker's 'Project Info' tab and fill column B for both fields, then re-run.")
            sys.exit(1)

    print(f"\nClient update email — {recipient_name} <{recipient_email}>")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'SEND' if args.send else 'DRAFT'}\n")

    print("Reading schedule...")
    phases = sheets.read_master_schedule(sheets_svc, tracker["id"])
    print(f"  {len(phases)} phases loaded")

    print("Building project snapshot...")
    snapshot = build_project_snapshot(phases, today)
    print(
        f"  {snapshot['pct']}% complete | "
        f"current: {snapshot['current'].get('Phase', 'N/A') if snapshot['current'] else 'N/A'} | "
        f"completion: {_fmt(snapshot['completion_date'])}"
    )

    print("Reading change orders...")
    recent_cos = read_recent_change_orders(sheets_svc, tracker["id"])
    if recent_cos:
        print(f"  {len(recent_cos)} CO(s) in last 30 days")
    else:
        print("  No recent change orders")

    print(f"\nGenerating email via {WRITER_MODEL}...")
    subject, html_body, usage = generate_client_email(
        client, project_name, recipient_name, snapshot, recent_cos, today
    )

    usd = sonnet_cost(usage)["total"]

    print(f"\n{'='*60}")
    print("CLIENT UPDATE EMAIL")
    print(f"{'='*60}")
    print(f"To:      {recipient_email}")
    print(f"Subject: {subject}")
    print()

    if args.dry_run:
        # Strip HTML for readable terminal output
        import re
        plain = re.sub(r"<[^>]+>", "", html_body)
        plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
        print(plain)
        print()
        print(f"{'='*60}")
        print(f"[DRY RUN — no email sent]")
        print(f"Cost: ${usd:.4f}")
        return

    sender_name = "Chad Whitfield | Palmetto Custom Homes"

    if args.send:
        print("Sending email...")
        result = gmail.send_email(
            gmail_svc,
            to=recipient_email,
            subject=subject,
            html_body=html_body,
            sender_name=sender_name,
        )
        print(f"  Sent. Message ID: {result.get('id')}")
        print(f"\n{'='*60}")
        print(f"Email sent to {recipient_email}")
    else:
        print("Creating Gmail draft...")
        result = gmail.create_draft(
            gmail_svc,
            to=recipient_email,
            subject=subject,
            html_body=html_body,
            sender_name=sender_name,
        )
        draft_id = result.get("id")
        print(f"  Draft ID: {draft_id}")
        print(f"\n{'='*60}")
        print(f"Draft created — review in Gmail before sending.")
        print(f"Tip: open Gmail → Drafts → review → Send")

    print(f"Cost: ${usd:.4f}")
    print()


if __name__ == "__main__":
    main()
