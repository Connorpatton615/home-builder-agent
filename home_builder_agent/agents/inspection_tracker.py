"""inspection_tracker.py — Baldwin County inspection sequence + permit expiry tracker.

CLI:
  hb-inspect                          Show full status: permit health, 180-day countdowns,
                                       next required inspection, any warnings.
  hb-inspect log "<NL update>"        Log an inspection or permit event in plain English.
  hb-inspect --dry-run "<NL update>"  Preview parse result without writing to sheet.

Examples:
  hb-inspect log "Building permit BP-2026-1234 issued April 15"
  hb-inspect log "Rough 4-way inspection passed today, inspector Mike Jones"
  hb-inspect log "Footing inspection failed, rebar spacing issue, reschedule next week"
  hb-inspect log "Final inspection scheduled for June 12"
  hb-inspect   (no args — show status dashboard)

The 180-day rule:
  Baldwin County (and most cities) void a permit if no inspection is scheduled
  within 180 days of the permit issue date OR the last inspection date.
  Warning thresholds:
    > 150 days → ⚠️  WARNING  (macOS notification + shown in status)
    > 165 days → 🚨 CRITICAL  (macOS notification + shown in status)

Inspection sequence (Baldwin County IRC / standard residential):
  1.  Temporary Power
  2.  Land Disturbance / Erosion  (if applicable)
  3.  Pilings / Pier Holes         (V-zone / Coastal A pile jobs)
  4.  Footing
  5.  Foundation / Stem Wall
  6.  Under-Slab Plumbing
  7.  Slab
  8.  Termite Pre-Treatment
  9.  Rough 4-Way (Framing + MEP)
  10. Insulation
  11. Elevation Certificate — Under Construction  (SFHA only)
  12. Final 4-Way + Final EC → Certificate of Occupancy

Cost: ~$0.005/log (Haiku parse); $0 for status view.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import date, datetime, timedelta

from home_builder_agent.config import (
    CLASSIFIER_MODEL,
    DRIVE_FOLDER_PATH,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import haiku_cost, make_client
from home_builder_agent.integrations import drive, sheets


# ---------------------------------------------------------------------------
# Baldwin County inspection sequence
# ---------------------------------------------------------------------------

INSPECTION_SEQUENCE = [
    "Temporary Power",
    "Land Disturbance / Erosion",
    "Pilings / Pier Holes",
    "Footing",
    "Foundation / Stem Wall",
    "Under-Slab Plumbing",
    "Slab",
    "Termite Pre-Treatment",
    "Rough 4-Way (Framing + MEP)",
    "Insulation",
    "Elevation Certificate — Under Construction",
    "Final 4-Way + CO",
]

# Aliases Haiku might return → canonical sequence name
INSPECTION_ALIASES: dict[str, str] = {
    "temporary power": "Temporary Power",
    "saw service": "Temporary Power",
    "land disturbance": "Land Disturbance / Erosion",
    "erosion": "Land Disturbance / Erosion",
    "piling": "Pilings / Pier Holes",
    "pier hole": "Pilings / Pier Holes",
    "footing": "Footing",
    "footer": "Footing",
    "foundation": "Foundation / Stem Wall",
    "stem wall": "Foundation / Stem Wall",
    "under-slab plumbing": "Under-Slab Plumbing",
    "under slab plumbing": "Under-Slab Plumbing",
    "slab": "Slab",
    "pre-pour": "Slab",
    "termite": "Termite Pre-Treatment",
    "rough 4-way": "Rough 4-Way (Framing + MEP)",
    "rough four way": "Rough 4-Way (Framing + MEP)",
    "4-way": "Rough 4-Way (Framing + MEP)",
    "framing": "Rough 4-Way (Framing + MEP)",
    "rough framing": "Rough 4-Way (Framing + MEP)",
    "rough mep": "Rough 4-Way (Framing + MEP)",
    "rough mechanical": "Rough 4-Way (Framing + MEP)",
    "rough electrical": "Rough 4-Way (Framing + MEP)",
    "rough plumbing": "Rough 4-Way (Framing + MEP)",
    "insulation": "Insulation",
    "elevation certificate": "Elevation Certificate — Under Construction",
    "under construction ec": "Elevation Certificate — Under Construction",
    "final": "Final 4-Way + CO",
    "final 4-way": "Final 4-Way + CO",
    "certificate of occupancy": "Final 4-Way + CO",
    "co": "Final 4-Way + CO",
}

# 180-day expiry warning thresholds
WARN_DAYS = 150
CRITICAL_DAYS = 165


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _normalize_inspection_type(raw: str) -> str:
    low = raw.lower().strip()
    for alias, canonical in INSPECTION_ALIASES.items():
        if alias in low:
            return canonical
    # Title-case the raw value if no alias matched
    return raw.strip().title()


# ---------------------------------------------------------------------------
# NL parsing via Haiku
# ---------------------------------------------------------------------------

PARSE_PROMPT = f"""You are a parser for a custom home builder's inspection log.
Convert the builder's plain-English note into structured JSON.

RECORD TYPES:
- "permit"      — a permit was issued by the county
- "inspection"  — an inspection was scheduled, passed, or failed

INSPECTION TYPES (use the closest match):
{chr(10).join(f'  - {s}' for s in INSPECTION_SEQUENCE)}

PERMIT TYPES: Building, Electrical, Plumbing, Mechanical, FORTIFIED, Other

STATUS values:
  For permits:      "Issued", "Expired", "Closed"
  For inspections:  "Passed", "Failed", "Scheduled"

Return ONLY valid JSON, no markdown fence, no explanation:
{{
  "record_type": "permit" | "inspection",
  "date": "YYYY-MM-DD or today",
  "permit_number": "<permit # or empty string>",
  "permit_type": "<Building|Electrical|Plumbing|Mechanical|FORTIFIED|Other>",
  "inspection_type": "<type from list above, or empty if record_type=permit>",
  "status": "<Issued|Passed|Failed|Scheduled>",
  "inspector": "<inspector name or empty>",
  "notes": "<any extra detail>"
}}

TODAY: {{TODAY}}
"""


def parse_inspection_update(client, text: str, today: date) -> tuple[dict, object]:
    prompt = PARSE_PROMPT.replace("{TODAY}", today.isoformat())

    response = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": f'{prompt}\n\nBUILDER\'S NOTE: "{text}"'}],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)

    try:
        parsed = __import__("json").loads(raw)
    except Exception as e:
        raise ValueError(f"Parse failed. Raw: {raw[:300]}\nError: {e}")

    # Normalize date
    if parsed.get("date") in ("today", "", None):
        parsed["date"] = today.isoformat()
    else:
        d = _parse_date(parsed.get("date"))
        parsed["date"] = d.isoformat() if d else today.isoformat()

    # Normalize inspection type
    if parsed.get("inspection_type"):
        parsed["inspection_type"] = _normalize_inspection_type(parsed["inspection_type"])

    return parsed, response.usage


# ---------------------------------------------------------------------------
# 180-day expiry analysis
# ---------------------------------------------------------------------------

def compute_permit_health(records: list[dict], today: date | None = None) -> list[dict]:
    """Compute per-permit health from the inspection log records.

    Returns a list of permit health dicts, one per unique permit number.
    """
    if today is None:
        today = date.today()

    # Group by permit number
    permits: dict[str, dict] = {}
    for rec in records:
        pnum = (rec.get("Permit #") or "").strip()
        if not pnum:
            continue

        if pnum not in permits:
            permits[pnum] = {
                "permit_number": pnum,
                "permit_type": rec.get("Permit Type", "Building"),
                "issued_date": None,
                "last_passed_date": None,
                "passed_inspections": [],
                "failed_inspections": [],
                "scheduled_inspections": [],
            }

        rec_date = _parse_date(rec.get("Date"))
        status = (rec.get("Status") or "").strip()
        rec_type = (rec.get("Record Type") or "").strip().lower()
        insp_type = (rec.get("Inspection Type") or "").strip()

        if rec_type == "permit" and status == "Issued":
            if rec_date:
                permits[pnum]["issued_date"] = rec_date
        elif rec_type == "inspection":
            if status == "Passed":
                permits[pnum]["passed_inspections"].append(insp_type)
                if rec_date and (permits[pnum]["last_passed_date"] is None
                                 or rec_date > permits[pnum]["last_passed_date"]):
                    permits[pnum]["last_passed_date"] = rec_date
            elif status == "Failed":
                permits[pnum]["failed_inspections"].append(insp_type)
            elif status == "Scheduled":
                permits[pnum]["scheduled_inspections"].append(
                    {"type": insp_type, "date": rec_date}
                )

    results = []
    for pnum, info in permits.items():
        # Anchor date: most recent activity (issued or last passed)
        anchor = info["issued_date"]
        if info["last_passed_date"]:
            if anchor is None or info["last_passed_date"] > anchor:
                anchor = info["last_passed_date"]

        days_since = (today - anchor).days if anchor else None
        days_until_expiry = (180 - days_since) if days_since is not None else None
        expiry_date = (anchor + timedelta(days=180)) if anchor else None

        # Status classification
        if days_since is None:
            health = "UNKNOWN"
        elif days_since >= 180:
            health = "EXPIRED"
        elif days_since >= CRITICAL_DAYS:
            health = "CRITICAL"
        elif days_since >= WARN_DAYS:
            health = "WARNING"
        else:
            health = "OK"

        # Next expected inspection
        passed_set = set(info["passed_inspections"])
        next_inspection = None
        for step in INSPECTION_SEQUENCE:
            if step not in passed_set:
                next_inspection = step
                break

        results.append({
            **info,
            "anchor_date": anchor,
            "days_since": days_since,
            "days_until_expiry": days_until_expiry,
            "expiry_date": expiry_date,
            "health": health,
            "next_inspection": next_inspection,
        })

    return results


# ---------------------------------------------------------------------------
# macOS notifications
# ---------------------------------------------------------------------------

def fire_expiry_notification(permit: dict) -> None:
    days = permit.get("days_until_expiry")
    pnum = permit.get("permit_number", "")
    health = permit.get("health", "")

    if health == "CRITICAL":
        title = f"🚨 Permit CRITICAL — {pnum}"
        body = f"Only {days} days until permit expires. Schedule an inspection NOW."
    elif health == "WARNING":
        title = f"⚠️ Permit Warning — {pnum}"
        body = f"{days} days until permit expires. Schedule next inspection soon."
    elif health == "EXPIRED":
        title = f"🚨 Permit EXPIRED — {pnum}"
        body = "This permit has lapsed. Contact the building department immediately."
    else:
        return

    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def print_status(permit_health: list[dict], project_name: str, today: date) -> None:
    print(f"\n{'='*60}")
    print(f"INSPECTION STATUS — {project_name}")
    print(f"{'='*60}")
    print(f"As of: {today.strftime('%B %-d, %Y')}\n")

    if not permit_health:
        print("  No permits logged yet.")
        print()
        print("  Log a permit:      hb-inspect log \"Building permit BP-2026-1234 issued today\"")
        print("  Log an inspection: hb-inspect log \"Footing inspection passed\"")
        print(f"{'='*60}\n")
        return

    for p in permit_health:
        health_icon = {
            "OK": "✅", "WARNING": "⚠️ ", "CRITICAL": "🚨", "EXPIRED": "🚨", "UNKNOWN": "❓"
        }.get(p["health"], "❓")

        ptype = p.get("permit_type", "Building")
        pnum = p.get("permit_number", "—")
        print(f"  {health_icon} {ptype} Permit — {pnum}")

        if p.get("issued_date"):
            print(f"     Issued:          {p['issued_date'].strftime('%B %-d, %Y')}")
        if p.get("anchor_date"):
            print(f"     Last activity:   {p['anchor_date'].strftime('%B %-d, %Y')} "
                  f"({p['days_since']} days ago)")
        if p.get("expiry_date"):
            if p["health"] in ("EXPIRED",):
                print(f"     ❌ EXPIRED:      {p['expiry_date'].strftime('%B %-d, %Y')}")
            else:
                print(f"     Expires:         {p['expiry_date'].strftime('%B %-d, %Y')} "
                      f"({p['days_until_expiry']} days)")

        passed = p.get("passed_inspections", [])
        if passed:
            print(f"     Passed ({len(passed)}):      {', '.join(passed)}")

        failed = p.get("failed_inspections", [])
        if failed:
            print(f"     ⚠️  Failed:        {', '.join(failed)}")

        scheduled = p.get("scheduled_inspections", [])
        if scheduled:
            for s in scheduled:
                date_str = s["date"].strftime("%b %-d") if s.get("date") else "TBD"
                print(f"     📅 Scheduled:     {s['type']} — {date_str}")

        if p.get("next_inspection"):
            print(f"     ➡️  Next required:  {p['next_inspection']}")

        if p["health"] in ("WARNING", "CRITICAL", "EXPIRED"):
            days = p.get("days_until_expiry", 0)
            if p["health"] == "EXPIRED":
                print(f"\n     🚨 PERMIT EXPIRED — contact building dept immediately")
            elif p["health"] == "CRITICAL":
                print(f"\n     🚨 CRITICAL: only {days} days until expiry — schedule inspection NOW")
            else:
                print(f"\n     ⚠️  WARNING: {days} days until expiry — schedule next inspection")

        print()

    # Show full Baldwin County sequence for reference
    print("  Baldwin County Standard Inspection Sequence:")
    for i, step in enumerate(INSPECTION_SEQUENCE, 1):
        print(f"    {i:2}. {step}")
    print(f"\n{'='*60}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today = date.today()

    # Determine mode: status (no args) or log (first arg is NL text or "log")
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    # Strip "log" sub-command if provided
    if args and args[0].lower() == "log":
        args = args[1:]

    log_text = " ".join(args).strip() if args else ""
    mode = "log" if log_text else "status"

    print("Authenticating...")
    creds = get_credentials()
    drive_svc = drive.drive_service(creds)
    sheets_svc = sheets.sheets_service(creds)
    client = make_client() if mode == "log" else None

    print("Finding latest Tracker...")
    tracker = drive.find_latest_tracker(drive_svc, DRIVE_FOLDER_PATH)
    project_name = drive.extract_project_name(tracker["name"])
    print(f"  Project: {project_name}\n")

    if mode == "log":
        print(f"Parsing: \"{log_text}\"")
        record, usage = parse_inspection_update(client, log_text, today)
        usd = haiku_cost(usage)

        print(f"\n  Parsed record:")
        print(f"    Type:            {record['record_type'].title()}")
        print(f"    Date:            {record['date']}")
        if record.get("permit_number"):
            print(f"    Permit #:        {record['permit_number']}")
        print(f"    Permit type:     {record['permit_type']}")
        if record.get("inspection_type"):
            print(f"    Inspection type: {record['inspection_type']}")
        print(f"    Status:          {record['status']}")
        if record.get("inspector"):
            print(f"    Inspector:       {record['inspector']}")
        if record.get("notes"):
            print(f"    Notes:           {record['notes']}")

        if dry_run:
            print(f"\n[DRY RUN — nothing written to sheet]")
            print(f"Cost: ${usd:.4f}")
            return

        print("\nWriting to Inspections tab...")
        sheets.log_inspection_record(sheets_svc, tracker["id"], record)
        print("  Done.")

        # Refresh health after write
        all_records = sheets.read_inspections(sheets_svc, tracker["id"])
        permit_health = compute_permit_health(all_records, today)

        # Fire notifications for any warnings
        for p in permit_health:
            if p["health"] in ("WARNING", "CRITICAL", "EXPIRED"):
                fire_expiry_notification(p)

        print_status(permit_health, project_name, today)
        print(f"Cost: ${usd:.4f}\n")

    else:  # status mode
        print("Reading Inspections tab...")
        all_records = sheets.read_inspections(sheets_svc, tracker["id"])
        permit_health = compute_permit_health(all_records, today)

        # Fire macOS notifications for any permits in warning/critical/expired
        for p in permit_health:
            if p["health"] in ("WARNING", "CRITICAL", "EXPIRED"):
                fire_expiry_notification(p)

        print_status(permit_health, project_name, today)


if __name__ == "__main__":
    main()
