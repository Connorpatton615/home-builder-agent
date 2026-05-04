"""ledger_agent.py — Natural-language financial entry for the Cost Tracker.

Chad speaks; the ledger writes. Examples:

  hb-ledger "paid framing crew $45,000"
  hb-ledger "got invoice from ABC Electric $8,400 due May 15"
  hb-ledger "billed the Smiths $150,000 for draw 2"
  hb-ledger "sent PO to Anderson Windows, $22,000 deposit"
  hb-ledger "HVAC rough-in done, owed Coastal Air $18,500"
  hb-ledger          ← interactive prompt

Each update is parsed by Sonnet, confirmed with Chad, then written to the
right column in the Cost Tracker.  Every transaction is also appended to the
Actuals Log tab so there's a full audit trail.

Transaction routing:
  actual_paid      → Cost Tracker Actual ($) column E + Actuals Log
  billed_client    → Cost Tracker Billed ($) column G + Actuals Log
  invoice_received → Invoices tab (status: Received)
  commitment       → Invoices tab (status: Commitment)

CLI: hb-ledger ["natural language update"]
Cost: ~$0.01 per run (one Sonnet call)
"""

import argparse
import json
import re
from datetime import date

from googleapiclient.discovery import build

from home_builder_agent.config import (
    FINANCE_FOLDER_PATH,
    FINANCE_PROJECT_NAME,
    WRITER_MODEL,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import make_client, sonnet_cost
from home_builder_agent.integrations.drive import find_folder_by_path
from home_builder_agent.integrations.finance import (
    add_actuals_log_row,
    add_invoice_row,
    find_cost_tracker,
    read_finance_summary,
    update_cost_tracker_actual,
    update_cost_tracker_billed,
)

# ── Cost sections must match the sheet's column A headers exactly ──────────

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

# ── Section mapping hints injected into the prompt ─────────────────────────

_TRADE_MAP = """
Trade → section hints:
  plumber / plumbing / pipes          → Mechanical Systems
  electrician / electric / wiring     → Mechanical Systems
  HVAC / AC / heat / Carrier / Trane  → Mechanical Systems
  framing crew / lumber / LVL         → Framing
  roofer / shingles / roofing         → Roofing & Gutters
  painter / paint / stain             → Wall Coverings & Paint
  flooring / hardwood / tile floors   → Flooring
  cabinet / countertop / granite      → Cabinets & Countertops
  landscaping / irrigation / grading  → Landscaping & Site Improvements
  windows / doors / Anderson          → Windows & Exterior Doors
  concrete / slab / flatwork          → Concrete Slabs
  drywall / insulation / sheetrock    → Insulation & Drywall
  trim / millwork / stairs / railing  → Interior Trim & Stairs
  appliances / refrigerator / range   → Appliance Package
  permits / fees / survey             → Permits & Fees
  structural steel / beam             → Structural Steel
  fireplace / hearth / mantle         → Fireplace / Hearth / Mantle
  brick / stone / siding / stucco     → Exterior Veneer
  cleanup / dumpster / haul           → Clean-Up
  contingency / reserve               → Contingency
  anything else / general             → General Conditions
"""


# ── Parser ──────────────────────────────────────────────────────────────────

def _parse_ledger_entry(client, text, project_name, today_str):
    """Extract structured financial transactions from natural language.

    Returns (list of transaction dicts, usage).
    Each dict has: type, amount, vendor, section, date, due_date, notes, confidence.
    """
    prompt = f"""You are parsing a financial update for a custom home construction project.

Project: {project_name}
Today: {today_str}

Chad's update:
"{text}"

Extract ALL financial transactions mentioned. Return a JSON array.

Each transaction object:
{{
  "type": "actual_paid" | "billed_client" | "invoice_received" | "commitment",
  "amount": <number, no $ or commas>,
  "vendor": "<who is paid or who sent invoice, or empty string>",
  "section": "<exactly one section from the list below>",
  "date": "<YYYY-MM-DD, use {today_str} if not stated>",
  "due_date": "<YYYY-MM-DD if mentioned, else empty string>",
  "notes": "<any extra context from Chad's message, or empty string>",
  "confidence": "high" | "low"
}}

Type rules:
  actual_paid      = Chad paid money OUT (paid, wrote check, sent wire, settled up)
  billed_client    = Chad billed or invoiced the homeowner
  invoice_received = An invoice arrived that hasn't been paid yet
  commitment       = Deposit sent, PO issued, money committed but work not done

Valid sections:
{_SECTIONS_BLOCK}

{_TRADE_MAP}

Confidence: "high" if section is clear from context, "low" if you guessed.

Return ONLY a JSON array — no preamble, no markdown fence.
Return [] if no financial transaction is present."""

    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)

    try:
        txns = json.loads(raw)
        if not isinstance(txns, list):
            txns = []
    except json.JSONDecodeError:
        txns = []

    # Defensive coercion — never let a bad Claude response crash the agent
    clean = []
    for t in txns:
        if not isinstance(t, dict):
            continue
        try:
            t["amount"] = float(t.get("amount", 0) or 0)
        except (ValueError, TypeError):
            t["amount"] = 0.0
        t.setdefault("vendor", "")
        t.setdefault("section", "General Conditions")
        t.setdefault("date", today_str)
        t.setdefault("due_date", "")
        t.setdefault("notes", "")
        t.setdefault("confidence", "high")
        t.setdefault("type", "actual_paid")
        if t["amount"] > 0:         # skip zero-dollar noise
            clean.append(t)

    return clean, response.usage


# ── Display helpers ─────────────────────────────────────────────────────────

_TYPE_LABELS = {
    "actual_paid":      "ACTUAL PAID        (money out → sub/supplier)",
    "billed_client":    "BILLED TO CLIENT   (draw / progress invoice)",
    "invoice_received": "INVOICE RECEIVED   (not yet paid)",
    "commitment":       "COMMITMENT         (deposit / PO sent)",
}


def _display_transactions(txns):
    for i, t in enumerate(txns, 1):
        label = _TYPE_LABELS.get(t["type"], t["type"].upper())
        flag  = "  ⚠️  low confidence — verify section" if t["confidence"] == "low" else ""
        print(f"  [{i}] {label}{flag}")
        print(f"       Amount:   ${t['amount']:>12,.0f}")
        if t["vendor"]:
            print(f"       Vendor:   {t['vendor']}")
        print(f"       Section:  {t['section']}")
        print(f"       Date:     {t['date']}")
        if t["due_date"]:
            print(f"       Due:      {t['due_date']}")
        if t["notes"]:
            print(f"       Notes:    {t['notes']}")
        print()


# ── Apply one transaction ────────────────────────────────────────────────────

def _apply(sheets_svc, sheet_id, t, project_name):
    """Write one transaction to the Cost Tracker. Returns a result line."""
    tx_type  = t["type"]
    amount   = t["amount"]
    section  = t["section"]
    vendor   = t["vendor"]
    tx_date  = t["date"]
    due_date = t["due_date"]
    notes    = t["notes"]

    if tx_type == "actual_paid":
        _, new_total = update_cost_tracker_actual(
            sheets_svc, sheet_id, section, amount
        )
        add_actuals_log_row(sheets_svc, sheet_id, {
            "date": tx_date, "vendor": vendor, "amount": amount,
            "section": section, "receipt_link": "",
            "notes": f"hb-ledger: {notes}" if notes else "hb-ledger",
        })
        return (f"✅  {section}  →  Actual +${amount:,.0f}  "
                f"(section subtotal now ${new_total:,.0f})")

    if tx_type == "billed_client":
        _, new_total = update_cost_tracker_billed(
            sheets_svc, sheet_id, section, amount
        )
        add_actuals_log_row(sheets_svc, sheet_id, {
            "date": tx_date, "vendor": f"Billed client — {vendor}" if vendor else "Billed client",
            "amount": amount, "section": section, "receipt_link": "",
            "notes": f"hb-ledger billed_client: {notes}" if notes else "hb-ledger billed_client",
        })
        return (f"✅  {section}  →  Billed +${amount:,.0f}  "
                f"(section subtotal now ${new_total:,.0f})")

    if tx_type in ("invoice_received", "commitment"):
        status = "Received" if tx_type == "invoice_received" else "Commitment"
        add_invoice_row(sheets_svc, sheet_id, {
            "invoice_number": "",
            "vendor":         vendor or project_name,
            "description":    f"{section} — {notes}" if notes else section,
            "amount":         amount,
            "invoice_date":   tx_date,
            "due_date":       due_date,
            "status":         status,
            "job":            project_name,
            "source":         "hb-ledger",
            "notes":          notes,
        })
        return (f"✅  Invoices tab  →  {status}: {vendor or section}  "
                f"${amount:,.0f}" + (f"  due {due_date}" if due_date else ""))

    return f"⚠️  Unknown transaction type '{tx_type}' — skipped"


# ── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Log financial entries to the Cost Tracker in plain English.",
        epilog='hb-ledger "paid framing crew $45,000"',
    )
    parser.add_argument(
        "update",
        nargs="?",
        metavar="UPDATE",
        help="Financial update in plain English (omit for interactive prompt).",
    )
    args = parser.parse_args()

    # ── 1. Get input ────────────────────────────────────────────────────
    if args.update:
        nl_text = args.update.strip()
    else:
        print("Chad's Finance Ledger")
        print("─────────────────────────────────────────────────────")
        print("Examples:")
        print('  "paid framing crew $45,000"')
        print('  "got invoice from ABC Electric $8,400 due May 15"')
        print('  "billed the Smiths $150,000 for draw 2"')
        print('  "sent deposit to Anderson Windows $12,000"')
        print()
        nl_text = input("Update: ").strip()

    if not nl_text:
        print("Nothing entered — exiting.")
        return

    # ── 2. Parse ────────────────────────────────────────────────────────
    print("\nReading update...")
    client   = make_client()
    today    = date.today().isoformat()
    txns, usage = _parse_ledger_entry(client, nl_text, FINANCE_PROJECT_NAME, today)
    cost     = sonnet_cost(usage)

    if not txns:
        print("\nNo financial transactions found in that update.")
        print("Be specific: 'paid [who] $[amount] for [what]'")
        print(f"Parse cost: ${cost['total']:.4f}")
        return

    # ── 3. Confirm ──────────────────────────────────────────────────────
    plural = "transaction" if len(txns) == 1 else "transactions"
    print(f"\nFound {len(txns)} {plural}:\n")
    _display_transactions(txns)

    confirm = input("Apply to Cost Tracker? [y/n]: ").strip().lower()
    if confirm != "y":
        print("Aborted — nothing written.")
        print(f"Parse cost: ${cost['total']:.4f}")
        return

    # ── 4. Connect to Google ────────────────────────────────────────────
    print("\nConnecting...")
    try:
        creds      = get_credentials()
        drive_svc  = build("drive",  "v3", credentials=creds)
        sheets_svc = build("sheets", "v4", credentials=creds)
    except Exception as e:
        print(f"Google auth failed: {e}")
        return

    try:
        folder_id = find_folder_by_path(drive_svc, FINANCE_FOLDER_PATH)
        tracker   = find_cost_tracker(drive_svc, folder_id, FINANCE_PROJECT_NAME)
    except Exception as e:
        print(f"Could not find Finance Office: {e}")
        print("Run hb-finance first.")
        return

    if not tracker:
        print(f"No Cost Tracker found for '{FINANCE_PROJECT_NAME}'. Run hb-finance first.")
        return

    sheet_id = tracker["id"]

    # ── 5. Apply ────────────────────────────────────────────────────────
    print()
    for t in txns:
        try:
            print(" ", _apply(sheets_svc, sheet_id, t, FINANCE_PROJECT_NAME))
        except Exception as e:
            print(f"  ❌  Error: {e}")

    # ── 6. Quick balance snapshot ────────────────────────────────────────
    try:
        s = read_finance_summary(sheets_svc, sheet_id)
        if s["contract_actual"] > 0 or s["contract_billed"] > 0:
            remaining = s["budget_remaining"]
            sign = "-" if remaining < 0 else ""
            print()
            print(f"  Budget remaining:  {sign}${abs(remaining):,.0f}  "
                  f"({s['pct_spent']:.1f}% of ${s['contract_budget']:,.0f} used)")
    except Exception:
        pass  # balance snapshot is best-effort

    print(f"\nParse cost: ${cost['total']:.4f}")


if __name__ == "__main__":
    main()
