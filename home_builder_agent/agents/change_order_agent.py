"""change_order_agent.py — Change Order manager for Palmetto Custom Homes.

Chad speaks; the agent dispatches everything automatically:

  hb-change "Smiths want quartzite counters instead of granite, adds $18,500,
             pushes cabinet install 2 weeks"

What it does in one run:
  1. Parses the NL description → project, section, cost delta, schedule impact
  2. Assigns the next CO number (CO-001, CO-002 ...)
  3. Creates a formal CO approval document in Google Drive (client-ready, with
     signature lines and a clear "work does not begin until signed" clause)
  4. Logs the CO to the Change Orders tab in the Cost Tracker
  5. Updates column C (Change Orders $) on the affected Cost Tracker section row
  6. If a schedule impact is mentioned, delegates to the status updater logic
  7. Drafts a Gmail approval email to the client — Chad reviews and sends
  8. Prints a clean summary of everything dispatched

CLI:
  hb-change "<natural language change order>"
  hb-change "<description>" --client-email smiths@example.com
  hb-change "<description>" --dry-run          # parse + preview, no writes

Cost: ~$0.03–0.05 per run (two Sonnet calls: parse + doc generation)
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime

from googleapiclient.discovery import build

from home_builder_agent.config import (
    CHANGE_ORDERS_DIR,
    CO_CLIENT_EMAIL,
    CO_MAX_TOKENS,
    FINANCE_FOLDER_PATH,
    FINANCE_PROJECT_NAME,
    WRITER_MODEL,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import make_client, sonnet_cost
from home_builder_agent.integrations.drive import (
    find_folder_by_path,
    upload_as_google_doc,
)
from home_builder_agent.integrations.finance import (
    add_change_order_row,
    ensure_change_orders_tab,
    find_cost_tracker,
    get_next_co_number,
    read_finance_summary,
    update_cost_tracker_change_order,
)
from home_builder_agent.integrations.gmail import (
    gmail_service,
    send_email,
)

# ---------------------------------------------------------------------------
# Known cost sections (mirrors ledger_agent.py — must match sheet col A)
# ---------------------------------------------------------------------------

COST_SECTIONS = [
    "Permits & Fees",
    "Site Work",
    "Footings & Foundation",
    "Concrete Slabs",
    "Framing",
    "Structural Steel",
    "Windows & Exterior Doors",
    "Roofing & Gutters",
    "Mechanical Systems",
    "Exterior Veneer",
    "Insulation & Drywall",
    "Cabinets & Countertops",
    "Fireplace / Hearth / Mantle",
    "Interior Trim & Stairs",
    "Flooring",
    "Wall Coverings & Paint",
    "Appliance Package",
    "Landscaping & Site Improvements",
    "Clean-Up",
    "General Conditions",
    "Contingency",
]

_SECTIONS_BLOCK = "\n".join(f"  - {s}" for s in COST_SECTIONS)

# ---------------------------------------------------------------------------
# Step 1 — Parse NL into structured CO data
# ---------------------------------------------------------------------------

def _parse_change_order(client, text, project_name, today_str):
    """Extract structured change order fields from natural language.

    Returns (co_dict, usage).
    """
    prompt = f"""You are parsing a change order description for a luxury custom home
construction project in Baldwin County, Alabama.

Project: {project_name}
Today:   {today_str}

Chad's description:
"{text}"

Extract the change order details and return a single JSON object:

{{
  "description": "<clear one-sentence summary of what is changing>",
  "full_description": "<complete description including materials, scope, reason — 2-4 sentences>",
  "section": "<exactly one section from the list below — the PRIMARY affected cost section>",
  "cost_delta": <number — positive = cost increase, negative = cost decrease, 0 if unknown>,
  "cost_delta_note": "<brief explanation of cost — e.g. 'quartzite vs granite upgrade'>",
  "schedule_impact_days": <integer — calendar days added (positive) or saved (negative), 0 if none>,
  "schedule_update_text": "<if schedule_impact_days != 0, the status update to pass to hb-update, else empty string>",
  "client_name": "<client last name or full name if mentioned, else empty string>",
  "requested_by": "client" | "chad" | "unknown",
  "confidence": "high" | "low"
}}

Valid sections:
{_SECTIONS_BLOCK}

Rules:
- cost_delta must be a plain number (no $ or commas)
- schedule_impact_days must be an integer (14 for "2 weeks", 7 for "1 week", etc.)
- schedule_update_text should be phrased as Chad would say it, e.g.
  "Cabinet install pushed 2 weeks due to countertop upgrade CO-001"
- If cost is not mentioned, use 0 and note "TBD — Chad to confirm"
- Return ONLY the JSON object — no markdown, no preamble"""

    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)

    try:
        co = json.loads(raw)
    except json.JSONDecodeError:
        co = {
            "description": text[:120],
            "full_description": text,
            "section": "General Conditions",
            "cost_delta": 0,
            "cost_delta_note": "TBD — Chad to confirm",
            "schedule_impact_days": 0,
            "schedule_update_text": "",
            "client_name": "",
            "requested_by": "unknown",
            "confidence": "low",
        }

    # Defensive coercion
    try:
        co["cost_delta"] = float(str(co.get("cost_delta", 0) or 0).replace(",", ""))
    except (ValueError, TypeError):
        co["cost_delta"] = 0.0
    try:
        co["schedule_impact_days"] = int(co.get("schedule_impact_days", 0) or 0)
    except (ValueError, TypeError):
        co["schedule_impact_days"] = 0

    co.setdefault("description", text[:120])
    co.setdefault("full_description", text)
    co.setdefault("section", "General Conditions")
    co.setdefault("cost_delta_note", "")
    co.setdefault("schedule_update_text", "")
    co.setdefault("client_name", "")
    co.setdefault("requested_by", "unknown")
    co.setdefault("confidence", "high")

    return co, response.usage


# ---------------------------------------------------------------------------
# Step 2 — Generate formal CO document HTML
# ---------------------------------------------------------------------------

def _generate_co_html(client, co, co_number, project_name, today_str,
                      contract_budget, usage_acc):
    """Generate a professional HTML Change Order document via Claude.

    Returns (html_string, updated_usage_acc).
    """
    sign_positive = co["cost_delta"] >= 0
    delta_sign    = "+" if sign_positive else "-"
    delta_abs     = abs(co["cost_delta"])
    revised_total = contract_budget + co["cost_delta"] if contract_budget else None

    schedule_line = ""
    if co["schedule_impact_days"] != 0:
        direction = "extends" if co["schedule_impact_days"] > 0 else "reduces"
        schedule_line = (
            f"This Change Order {direction} the project schedule by "
            f"{abs(co['schedule_impact_days'])} calendar day"
            f"{'s' if abs(co['schedule_impact_days']) != 1 else ''}."
        )

    prompt = f"""Write a formal, professional Change Order document for a luxury custom home
builder. Output clean HTML only — no markdown, no preamble, no explanation.

Use this exact structure and styling:

<html><body style="font-family: Georgia, serif; max-width: 750px; margin: 40px auto; color: #1a1a2e;">

<div style="text-align: center; border-bottom: 3px solid #1a1a2e; padding-bottom: 20px; margin-bottom: 30px;">
  <h1 style="font-size: 22px; margin: 0; letter-spacing: 2px;">PALMETTO CUSTOM HOMES</h1>
  <p style="margin: 4px 0; font-size: 13px; color: #555;">Baldwin County, Alabama · Excellence in Custom Construction</p>
  <h2 style="font-size: 18px; margin: 16px 0 4px;">CHANGE ORDER</h2>
  <p style="font-size: 15px; font-weight: bold; margin: 0;">{co_number}</p>
</div>

[Build the rest of the document with these data points]

CO Number:        {co_number}
Date:             {today_str}
Project:          {project_name}
Client:           {co.get("client_name") or "________"}
Requested by:     {co["requested_by"].title()}

Description of Change:
{co["full_description"]}

Cost Impact:
  Original Contract Value:   {f"${contract_budget:,.0f}" if contract_budget else "See contract"}
  This Change Order:         {delta_sign}${delta_abs:,.0f}  ({co["cost_delta_note"]})
  Revised Contract Total:    {f"${revised_total:,.0f}" if revised_total else "TBD"}

Affected Cost Section: {co["section"]}

Schedule Impact:
{schedule_line if schedule_line else "This Change Order does not affect the project schedule."}

Include:
1. A professional "Terms & Authorization" section stating clearly:
   "Work described in this Change Order shall not commence until both parties
   have signed below. Verbal authorization is not sufficient."
2. Signature blocks for Owner (printed name, signature, date) and Contractor
   (Palmetto Custom Homes, signature, date)
3. A footer with "Page 1 of 1  |  {co_number}  |  {project_name}"

Use clean table-based layout for signature blocks. Navy (#1a1a2e) for section headers.
Make it look like something a $1M+ homebuilder would actually send.

Output ONLY the complete HTML — start with <html> and end with </html>."""

    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=CO_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    html = response.content[0].text.strip()
    # Strip accidental markdown fencing
    html = re.sub(r"^```(?:html)?\s*\n?", "", html)
    html = re.sub(r"\n?```\s*$", "", html)
    if not html.lower().startswith("<"):
        # Fallback: wrap in minimal HTML
        html = f"<html><body><pre>{html}</pre></body></html>"

    # Merge usage
    usage_acc["input_tokens"]  = usage_acc.get("input_tokens", 0)  + response.usage.input_tokens
    usage_acc["output_tokens"] = usage_acc.get("output_tokens", 0) + response.usage.output_tokens

    return html, usage_acc


# ---------------------------------------------------------------------------
# Step 3 — Ensure CO folder exists in Finance Office
# ---------------------------------------------------------------------------

def _ensure_co_folder(drive_svc, finance_folder_id):
    """Return the Change Orders subfolder id, creating it if needed."""
    query = (
        f"name='{CHANGE_ORDERS_DIR}' "
        f"and '{finance_folder_id}' in parents "
        "and mimeType='application/vnd.google-apps.folder' "
        "and trashed=false"
    )
    files = drive_svc.files().list(
        q=query, fields="files(id,name)", pageSize=5
    ).execute().get("files", [])

    if files:
        return files[0]["id"]

    folder = drive_svc.files().create(body={
        "name": CHANGE_ORDERS_DIR,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [finance_folder_id],
    }, fields="id").execute()
    return folder["id"]


# ---------------------------------------------------------------------------
# Step 4 — Draft client approval email
# ---------------------------------------------------------------------------

def _draft_approval_email(gmail_svc, client_email, co, co_number,
                          project_name, doc_url):
    """Create a Gmail draft for client CO approval. Returns draft message id."""
    client_name = co.get("client_name") or "there"
    delta = co["cost_delta"]
    sign  = "+" if delta >= 0 else "-"

    subject = f"Change Order {co_number} — {project_name} — Approval Required"

    html_body = f"""
<p>Hi {client_name},</p>

<p>I'm writing to request your approval for a Change Order on your project,
<strong>{project_name}</strong>.</p>

<p><strong>Change Order:</strong> {co_number}<br>
<strong>Description:</strong> {co["description"]}<br>
<strong>Cost Impact:</strong> {sign}${abs(delta):,.0f}<br>
{"<strong>Schedule Impact:</strong> " + str(abs(co["schedule_impact_days"])) + " additional calendar day" + ("s" if abs(co["schedule_impact_days"]) != 1 else "") + "<br>" if co["schedule_impact_days"] else ""}
</p>

<p>The full Change Order document is attached and also available here:<br>
<a href="{doc_url}">{co_number} — {project_name}</a></p>

<p><strong>Please review and sign the document at your earliest convenience.
Work cannot begin on this change until we receive your signed approval.</strong></p>

<p>If you have any questions, please don't hesitate to call me directly.</p>

<p>Thank you,<br>
<strong>Chad</strong><br>
Palmetto Custom Homes<br>
Baldwin County, AL</p>
"""

    # Create draft via Gmail API
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    import base64

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"]      = client_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = gmail_svc.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw}},
    ).execute()
    return draft["id"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Create and dispatch a Change Order in plain English.",
        epilog='hb-change "Smiths want quartzite counters, adds $18,500, pushes cabinet install 2 weeks"',
    )
    parser.add_argument(
        "update",
        nargs="?",
        metavar="UPDATE",
        help="Change order description in plain English (omit for interactive prompt).",
    )
    parser.add_argument(
        "--client-email",
        default=CO_CLIENT_EMAIL,
        metavar="EMAIL",
        help="Client email address for the approval draft.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and preview only — no writes to Drive, Sheets, or Gmail.",
    )
    args = parser.parse_args()

    # ── 1. Get input ─────────────────────────────────────────────────────────
    if args.update:
        nl_text = args.update.strip()
    else:
        print("Palmetto Custom Homes — Change Order Manager")
        print("─" * 52)
        print('Example: "Smiths want to upgrade tile, adds $4,200"')
        print()
        nl_text = input("Change order description: ").strip()

    if not nl_text:
        print("Nothing entered — exiting.")
        return

    today = date.today().isoformat()
    client = make_client()

    # ── 2. Parse ──────────────────────────────────────────────────────────────
    print("\nParsing change order...")
    co, usage = _parse_change_order(client, nl_text, FINANCE_PROJECT_NAME, today)
    usage_acc = {
        "input_tokens":  usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }

    # ── 3. Preview ────────────────────────────────────────────────────────────
    delta_sign = "+" if co["cost_delta"] >= 0 else "-"
    print()
    print("┌─ Change Order Preview ──────────────────────────────────────────┐")
    print(f"│  Description:   {co['description'][:58]}")
    print(f"│  Section:       {co['section']}")
    print(f"│  Cost delta:    {delta_sign}${abs(co['cost_delta']):,.0f}  ({co['cost_delta_note']})")
    if co["schedule_impact_days"]:
        direction = "extended" if co["schedule_impact_days"] > 0 else "reduced"
        print(f"│  Schedule:      {abs(co['schedule_impact_days'])} days {direction}")
    print(f"│  Requested by:  {co['requested_by'].title()}")
    if co["confidence"] == "low":
        print("│  ⚠️  Low confidence — please verify section and cost")
    print("└─────────────────────────────────────────────────────────────────┘")

    if args.dry_run:
        print("\n[dry-run] Nothing written.")
        _report_cost(usage_acc)
        return

    confirm = input("\nCreate this change order? [y/n]: ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        _report_cost(usage_acc)
        return

    # ── 4. Connect to Google ──────────────────────────────────────────────────
    print("\nConnecting to Google...")
    try:
        creds      = get_credentials()
        drive_svc  = build("drive",  "v3", credentials=creds)
        sheets_svc = build("sheets", "v4", credentials=creds)
        gmail_svc  = build("gmail",  "v1", credentials=creds)
    except Exception as e:
        print(f"Google auth failed: {e}")
        return

    # ── 5. Find Finance Office and Cost Tracker ───────────────────────────────
    try:
        finance_folder_id = find_folder_by_path(drive_svc, FINANCE_FOLDER_PATH)
        tracker = find_cost_tracker(drive_svc, finance_folder_id, FINANCE_PROJECT_NAME)
    except Exception as e:
        print(f"Could not find Finance Office: {e}")
        print("Run hb-finance first to create it.")
        return

    if not tracker:
        print(f"No Cost Tracker found for '{FINANCE_PROJECT_NAME}'. Run hb-finance first.")
        return

    sheet_id = tracker["id"]

    # ── 6. Assign CO number ───────────────────────────────────────────────────
    ensure_change_orders_tab(sheets_svc, sheet_id)
    co_number = get_next_co_number(sheets_svc, sheet_id)
    print(f"\nAssigned: {co_number}")

    # ── 7. Read current contract total for the CO doc ─────────────────────────
    contract_budget = 0.0
    try:
        summary = read_finance_summary(sheets_svc, sheet_id)
        contract_budget = summary.get("contract_budget", 0.0)
    except Exception:
        pass

    # ── 8. Generate CO document HTML ─────────────────────────────────────────
    print("Generating Change Order document...")
    try:
        co_html, usage_acc = _generate_co_html(
            client, co, co_number, FINANCE_PROJECT_NAME, today,
            contract_budget, usage_acc,
        )
    except Exception as e:
        print(f"Document generation failed: {e}")
        co_html = f"<html><body><h1>{co_number}</h1><p>{co['full_description']}</p></body></html>"

    # ── 9. Upload CO doc to Drive ─────────────────────────────────────────────
    print("Uploading to Drive...")
    doc_url = ""
    try:
        co_folder_id = _ensure_co_folder(drive_svc, finance_folder_id)
        doc_name     = f"{co_number} — {FINANCE_PROJECT_NAME}"
        doc_meta     = upload_as_google_doc(drive_svc, co_html, doc_name, co_folder_id)
        doc_url      = doc_meta.get("webViewLink", "")
        print(f"  ✅  Document: {doc_url}")
    except Exception as e:
        print(f"  ⚠️  Drive upload failed: {e}")

    # ── 10. Log to Change Orders tab ─────────────────────────────────────────
    print("Logging to Change Orders tab...")
    try:
        add_change_order_row(sheets_svc, sheet_id, {
            "co_number":      co_number,
            "date":           today,
            "description":    co["description"],
            "section":        co["section"],
            "cost_delta":     co["cost_delta"],
            "schedule_days":  co["schedule_impact_days"],
            "status":         "Pending Approval",
            "doc_link":       doc_url,
            "notes":          co.get("cost_delta_note", ""),
        })
        print(f"  ✅  Logged to Change Orders tab")
    except Exception as e:
        print(f"  ⚠️  Sheet log failed: {e}")

    # ── 11. Update Cost Tracker column C ────────────────────────────────────
    if co["cost_delta"] != 0:
        print("Updating Cost Tracker (Change Orders column)...")
        try:
            update_cost_tracker_change_order(
                sheets_svc, sheet_id, co["section"], co["cost_delta"]
            )
            delta_sign = "+" if co["cost_delta"] >= 0 else ""
            print(f"  ✅  {co['section']} → Change Orders col {delta_sign}${co['cost_delta']:,.0f}")
        except Exception as e:
            print(f"  ⚠️  Cost Tracker update failed: {e}")

    # ── 12. Update schedule if needed ───────────────────────────────────────
    if co["schedule_impact_days"] and co["schedule_update_text"]:
        print("Updating project schedule...")
        try:
            result = subprocess.run(
                ["hb-update", co["schedule_update_text"]],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                print(f"  ✅  Schedule updated via hb-update")
            else:
                print(f"  ⚠️  hb-update returned error — update schedule manually:")
                print(f"      hb-update \"{co['schedule_update_text']}\"")
        except Exception as e:
            print(f"  ⚠️  Schedule update failed: {e}")
            print(f"      Run manually: hb-update \"{co['schedule_update_text']}\"")

    # ── 13. Draft approval email ────────────────────────────────────────────
    if args.client_email:
        print(f"Drafting approval email to {args.client_email}...")
        try:
            draft_id = _draft_approval_email(
                gmail_svc, args.client_email, co, co_number,
                FINANCE_PROJECT_NAME, doc_url,
            )
            print(f"  ✅  Gmail draft created — open Gmail to review and send")
        except Exception as e:
            print(f"  ⚠️  Email draft failed: {e}")
    else:
        print("  ℹ️  No client email — skipping draft")
        print(f"      (Run with --client-email CLIENT@EMAIL.COM to auto-draft)")

    # ── 14. Summary ─────────────────────────────────────────────────────────
    print()
    print("═" * 60)
    print(f"  {co_number} CREATED — {FINANCE_PROJECT_NAME}")
    print("═" * 60)
    if doc_url:
        print(f"  📄  Document:  {doc_url}")
    delta_str = f"+${co['cost_delta']:,.0f}" if co["cost_delta"] >= 0 else f"-${abs(co['cost_delta']):,.0f}"
    if contract_budget:
        new_total = contract_budget + co["cost_delta"]
        print(f"  💰  Cost:      {delta_str}  (new contract total: ${new_total:,.0f})")
    else:
        print(f"  💰  Cost:      {delta_str}")
    if co["schedule_impact_days"]:
        print(f"  📅  Schedule:  {abs(co['schedule_impact_days'])} days {'added' if co['schedule_impact_days'] > 0 else 'saved'}")
    print(f"  📋  Status:    PENDING CLIENT APPROVAL")
    if args.client_email:
        print(f"  📧  Draft:     approval email in Gmail drafts → review and send")
    print()
    _report_cost(usage_acc)


def _report_cost(usage_acc):
    """Print token usage and USD cost."""
    from home_builder_agent.config import SONNET_INPUT_COST, SONNET_OUTPUT_COST
    inp  = usage_acc.get("input_tokens",  0)
    out  = usage_acc.get("output_tokens", 0)
    cost = (inp * SONNET_INPUT_COST + out * SONNET_OUTPUT_COST) / 1_000_000
    print(f"  Cost: ${cost:.4f}  ({inp:,} in / {out:,} out tokens)")


if __name__ == "__main__":
    main()
