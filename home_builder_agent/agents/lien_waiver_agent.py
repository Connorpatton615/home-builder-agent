"""lien_waiver_agent.py — lien waiver tracking + missing-waiver detection.

CLI:
  hb-waiver                          Status check: payments without waivers
  hb-waiver log "<NL update>"        Log a waiver in plain English (Haiku parse)
  hb-waiver --report                 Same as no-args (status report)
  hb-waiver log "..." --dry-run      Preview parse without writing

Examples:
  hb-waiver log "got conditional waiver from ABC Plumbing for $5,200 paid May 1"
  hb-waiver log "Unconditional waiver from XYZ Framing for May 15 payment of $12,500"
  hb-waiver log "Coastal Concrete signed waiver today for the foundation pour"

Why this exists:
  Even after Chad pays a sub through the GC chain, that sub can still file a
  mechanic's lien on the homeowner's property if there's a payment dispute.
  Standard protection: get a signed lien waiver from every paid sub.
  Conditional waiver = "I waive lien rights ONCE this payment clears."
  Unconditional waiver = "I waive lien rights for this payment, paid."
  Missing waiver = potential lien risk. This agent finds the gaps.

Cross-reference logic:
  For each Actuals Log entry over LIEN_WAIVER_THRESHOLD:
    Look for a waiver with matching vendor (case-insensitive) AND amount
    within ±LIEN_WAIVER_AMOUNT_TOLERANCE AND filed within
    LIEN_WAIVER_MATCH_WINDOW_DAYS of the payment.
    If no match: FLAG.

Cost: ~$0.005/log (Haiku parse); $0/status.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta

from home_builder_agent.config import (
    CLASSIFIER_MODEL,
    FINANCE_FOLDER_PATH,
    FINANCE_PROJECT_NAME,
    LIEN_WAIVER_AMOUNT_TOLERANCE,
    LIEN_WAIVER_MATCH_WINDOW_DAYS,
    LIEN_WAIVER_THRESHOLD,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import haiku_cost, make_client
from home_builder_agent.integrations import drive, sheets
from home_builder_agent.integrations.finance import (
    add_lien_waiver_row,
    find_cost_tracker,
    read_actuals_log,
    read_lien_waivers,
)
from home_builder_agent.integrations.drive import find_folder_by_path


# ---------------------------------------------------------------------------
# Date / amount helpers
# ---------------------------------------------------------------------------

def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _parse_amount(s) -> float | None:
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    cleaned = re.sub(r"[^\d.\-]", "", str(s))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _normalize_vendor(s: str) -> str:
    """Lowercase + strip suffix words for fuzzy vendor matching."""
    s = (s or "").lower().strip()
    s = re.sub(r"[,.]", "", s)
    s = re.sub(r"\s+(inc|llc|co|corp|company|ltd)\.?$", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# NL parse (Haiku)
# ---------------------------------------------------------------------------

PARSE_PROMPT = """You are a parser for a custom home builder's lien waiver log.
Convert the builder's plain-English note into structured JSON.

WAIVER TYPES:
  - "Conditional"   — sub waives lien rights ONCE the payment clears
  - "Unconditional" — sub waives lien rights for a payment already received

Return ONLY valid JSON, no markdown fence, no explanation:
{
  "vendor": "<vendor/sub name>",
  "amount": <number or null>,
  "waiver_type": "Conditional" | "Unconditional",
  "payment_date": "YYYY-MM-DD or empty string",
  "date_filed": "YYYY-MM-DD or 'today'",
  "payment_reference": "<check #, invoice #, or empty>",
  "notes": "<any extra detail>"
}

If waiver type is unspecified, default to "Conditional".
If filed date is unspecified, default to "today".

TODAY: {TODAY}
"""


def parse_waiver_update(client, text: str, today: date) -> tuple[dict, object]:
    prompt = PARSE_PROMPT.replace("{TODAY}", today.isoformat())

    response = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=400,
        messages=[{"role": "user",
                   "content": f'{prompt}\n\nBUILDER\'S NOTE: "{text}"'}],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)

    try:
        parsed = json.loads(raw)
    except Exception as e:
        raise ValueError(f"Parse failed. Raw: {raw[:300]}\nError: {e}")

    # Normalize
    if parsed.get("date_filed") in ("today", "", None):
        parsed["date_filed"] = today.isoformat()
    else:
        d = _parse_date(parsed.get("date_filed"))
        parsed["date_filed"] = d.isoformat() if d else today.isoformat()

    if parsed.get("payment_date"):
        d = _parse_date(parsed.get("payment_date"))
        parsed["payment_date"] = d.isoformat() if d else ""

    return parsed, response.usage


# ---------------------------------------------------------------------------
# Cross-reference: payments without waivers
# ---------------------------------------------------------------------------

def find_unwaived_payments(
    actuals: list[dict],
    waivers: list[dict],
    threshold: float = LIEN_WAIVER_THRESHOLD,
    amt_tolerance: float = LIEN_WAIVER_AMOUNT_TOLERANCE,
    window_days: int = LIEN_WAIVER_MATCH_WINDOW_DAYS,
    today: date | None = None,
) -> dict:
    """Return a dict with 'unwaived', 'waived', 'below_threshold' lists of payments."""
    if today is None:
        today = date.today()

    # Pre-process waivers: normalized vendor + amount + filed date
    waiver_index = []
    for w in waivers:
        amt = _parse_amount(w.get("Amount ($)"))
        filed = _parse_date(w.get("Date Filed"))
        waiver_index.append({
            "vendor_norm": _normalize_vendor(w.get("Vendor", "")),
            "amount": amt,
            "date_filed": filed,
            "raw": w,
        })

    unwaived = []
    waived = []
    below_threshold = []

    for actual in actuals:
        amt = _parse_amount(actual.get("Amount ($)"))
        if amt is None or amt <= 0:
            continue

        if amt < threshold:
            below_threshold.append(actual)
            continue

        vendor_norm = _normalize_vendor(actual.get("Vendor", ""))
        pay_date = _parse_date(actual.get("Date"))

        match = None
        for w in waiver_index:
            if w["vendor_norm"] != vendor_norm:
                continue
            if w["amount"] is None or abs(w["amount"] - amt) > amt_tolerance:
                continue
            if pay_date and w["date_filed"]:
                # Waiver should be on or after payment, within window
                delta = (w["date_filed"] - pay_date).days
                if delta < -3 or delta > window_days:
                    continue
            match = w
            break

        if match:
            waived.append({"payment": actual, "waiver": match["raw"]})
        else:
            unwaived.append(actual)

    return {
        "unwaived": unwaived,
        "waived": waived,
        "below_threshold": below_threshold,
        "total_payments": len(actuals),
    }


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def print_status(report: dict, project_name: str, today: date) -> None:
    print(f"\n{'='*60}")
    print(f"LIEN WAIVER STATUS — {project_name}")
    print(f"{'='*60}")
    print(f"As of: {today.strftime('%B %-d, %Y')}\n")

    n_total = report["total_payments"]
    n_unwaived = len(report["unwaived"])
    n_waived = len(report["waived"])
    n_below = len(report["below_threshold"])

    print(f"  Total payments logged:  {n_total}")
    print(f"  ✅ Waived:              {n_waived}")
    print(f"  ⚠️  Unwaived (>${LIEN_WAIVER_THRESHOLD:.0f}):     {n_unwaived}")
    print(f"  ➖ Under threshold:     {n_below} (waiver not required)\n")

    if n_unwaived == 0:
        print("  🎉 All payments above threshold are waived. No lien risk.\n")
    else:
        print(f"  🚨 {n_unwaived} unwaived payment(s) — POTENTIAL LIEN RISK:\n")
        for p in report["unwaived"]:
            date_str = p.get("Date", "—")
            vendor = p.get("Vendor", "—")
            amt = _parse_amount(p.get("Amount ($)"))
            section = p.get("Section", "—")
            amt_str = f"${amt:>10,.2f}" if amt else "         —"
            print(f"     {date_str:12} | {vendor:30} | {amt_str} | {section}")
        print()
        print("  → Get a signed lien waiver from each of these subs.")
        print(f"  → Log it: hb-waiver log \"got waiver from <vendor> for <amount>\"\n")

    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Lien waiver tracker — find unwaived payments, log new waivers."
    )
    parser.add_argument(
        "args", nargs="*",
        help='Sub-command. Use "log" + text to record a waiver. No args = status.'
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview waiver parse without writing")
    parser.add_argument("--report", action="store_true",
                        help="Force report mode (same as no args)")
    parsed_args = parser.parse_args()

    today = date.today()

    # Determine mode
    raw = parsed_args.args
    mode = "status"
    log_text = ""
    if raw and raw[0].lower() == "log":
        mode = "log"
        log_text = " ".join(raw[1:]).strip()
    elif raw and not parsed_args.report:
        # Allow `hb-waiver "got waiver..."` without explicit "log" prefix
        # if first word doesn't look like a sub-command
        joined = " ".join(raw).strip()
        if joined and not joined.lower().startswith(("status", "report")):
            mode = "log"
            log_text = joined

    print("Authenticating...")
    creds = get_credentials()
    drive_svc = drive.drive_service(creds)
    sheets_svc = sheets.sheets_service(creds)
    client = make_client() if mode == "log" else None

    print(f"Finding Cost Tracker for '{FINANCE_PROJECT_NAME}'...")
    folder_id = find_folder_by_path(drive_svc, FINANCE_FOLDER_PATH)
    tracker = find_cost_tracker(drive_svc, folder_id, FINANCE_PROJECT_NAME)
    if not tracker:
        print(f"  No Cost Tracker found. Run hb-finance first.")
        sys.exit(1)
    print(f"  Sheet: {tracker['name']}\n")

    # ── Log mode ──────────────────────────────────────────────────────────
    if mode == "log":
        if not log_text:
            print('Usage: hb-waiver log "<waiver description>"')
            print('Example: hb-waiver log "got conditional waiver from ABC Plumbing for $5,200"')
            sys.exit(1)

        print(f"Parsing: \"{log_text}\"")
        record, usage = parse_waiver_update(client, log_text, today)
        usd = haiku_cost(usage)

        print(f"\n  Parsed waiver:")
        print(f"    Vendor:       {record.get('vendor', '?')}")
        print(f"    Amount:       ${record.get('amount', '—')}")
        print(f"    Type:         {record.get('waiver_type', '?')}")
        if record.get("payment_date"):
            print(f"    Payment date: {record['payment_date']}")
        if record.get("payment_reference"):
            print(f"    Ref:          {record['payment_reference']}")
        print(f"    Filed:        {record['date_filed']}")
        if record.get("notes"):
            print(f"    Notes:        {record['notes']}")

        if parsed_args.dry_run:
            print(f"\n[DRY RUN — nothing written to sheet]")
            print(f"Cost: ${usd:.4f}")
            return

        print("\nWriting to Lien Waivers tab...")
        add_lien_waiver_row(sheets_svc, tracker["id"], record)
        print("  Done.")

        # Refresh + show updated status
        actuals = read_actuals_log(sheets_svc, tracker["id"])
        waivers = read_lien_waivers(sheets_svc, tracker["id"])
        report = find_unwaived_payments(actuals, waivers, today=today)
        print_status(report, FINANCE_PROJECT_NAME, today)
        print(f"Cost: ${usd:.4f}\n")
        return

    # ── Status mode ───────────────────────────────────────────────────────
    print("Reading Actuals Log + Lien Waivers...")
    actuals = read_actuals_log(sheets_svc, tracker["id"])
    waivers = read_lien_waivers(sheets_svc, tracker["id"])
    print(f"  {len(actuals)} payment(s) logged")
    print(f"  {len(waivers)} waiver(s) on file")

    report = find_unwaived_payments(actuals, waivers, today=today)
    print_status(report, FINANCE_PROJECT_NAME, today)


if __name__ == "__main__":
    main()
