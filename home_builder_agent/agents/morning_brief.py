"""morning_brief.py — Daily morning email for Chad.

CLI:  hb-brief [--to EMAIL] [--dry-run]

Runs automatically each morning via launchd
(see com.chadhomes.morning-brief.plist at the repo root).

What it sends:
  • Weather today + tomorrow (NOAA free API — no key required)
  • Weather-risk phases: any phases scheduled this week that conflict
    with adverse conditions (rain, wind, extreme temps)
  • Project snapshot: current phase, days to completion, schedule health
  • Outstanding invoices due within 7 days
  • Overnight high-urgency emails from the inbox watcher
  • Top 2-3 action items for the day

Cost: ~$0.03–0.05/run (one Sonnet call).
"""

import argparse
import json
import logging
import os
import re
import sys
import uuid
from datetime import date, datetime, timedelta

from home_builder_agent.config import (
    BRIEF_MAX_TOKENS,
    BRIEF_RECIPIENT_EMAIL,
    BRIEF_SENDER_NAME,
    BRIEF_SITE_ADDRESS,
    BRIEF_SITE_LAT,
    BRIEF_SITE_LNG,
    CLASSIFIER_MODEL,
    DRIVE_FOLDER_PATH,
    FINANCE_FOLDER_PATH,
    FINANCE_PROJECT_NAME,
    INBOX_WATCHER_LOG_FILE,
    WRITER_MODEL,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.chad_voice import chad_voice_system
from home_builder_agent.core.claude_client import make_client, sonnet_cost
from home_builder_agent.core.cost_guard import record_cost
from home_builder_agent.core.heartbeat import beat_on_success
from home_builder_agent.integrations import drive, sheets
from home_builder_agent.integrations import gmail as gmail_int
from home_builder_agent.integrations.finance import get_aging_report
from home_builder_agent.observability.json_log import configure_json_logging
from home_builder_agent.scheduling.weather import (
    fetch_weather,
    weather_risk_check,
)

logger = logging.getLogger(__name__)

try:
    from googleapiclient.discovery import build as _goog_build
except ImportError:
    _goog_build = None

# ---------------------------------------------------------------------------
# Weather helpers (lifted to scheduling/weather.py for non-agent consumers)
# ---------------------------------------------------------------------------
# fetch_weather + weather_risk_check are imported above. The morning view-
# model fetches via the same helpers without needing to import an agent
# module. Tracker-shape phase dicts and engine.Phase dataclasses are both
# accepted by weather_risk_check (see _extract_phase_fields in the lifted
# module).


# ---------------------------------------------------------------------------
# Invoice helper  (invoices due within N days)
# ---------------------------------------------------------------------------

def get_due_soon_invoices(sheets_svc, sheet_id: str,
                           days_ahead: int = 7) -> list[dict]:
    """Return non-paid invoices whose due date falls within `days_ahead`."""
    try:
        report = get_aging_report(sheets_svc, sheet_id)
    except Exception:
        return []

    today = date.today()
    cutoff = today + timedelta(days=days_ahead)
    due_soon = []

    for inv in report.get("all_invoices", []):
        if (inv.get("status") or "").lower() == "paid":
            continue
        due_raw = inv.get("due_date", "")
        if not due_raw:
            continue
        try:
            due_dt = datetime.strptime(str(due_raw).strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if today <= due_dt <= cutoff:
            due_soon.append(inv)

    return sorted(due_soon, key=lambda x: x["due_date"])


# ---------------------------------------------------------------------------
# Overnight high-urgency email parser
# ---------------------------------------------------------------------------

def get_overnight_alerts(log_file: str = None, hours: int = 14) -> list[str]:
    """Parse the inbox watcher log for HIGH-urgency lines from the last N hours.

    Returns list of human-readable strings like:
      "HIGH | Anderson Supply | Invoice #4412 overdue"
    """
    log_file = log_file or os.path.abspath(INBOX_WATCHER_LOG_FILE)
    if not os.path.exists(log_file):
        return []

    cutoff = datetime.now() - timedelta(hours=hours)
    alerts = []
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if "HIGH |" not in line:
                    continue
                # Extract timestamp from front: [2026-04-28T06:30:00]
                ts_match = re.match(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\]", line)
                if ts_match:
                    try:
                        ts = datetime.fromisoformat(ts_match.group(1))
                        if ts < cutoff:
                            continue
                    except ValueError:
                        pass
                # Strip timestamp bracket, keep the rest
                clean = re.sub(r"^\[.*?\]\s*", "", line)
                alerts.append(clean)
    except Exception:
        pass
    return alerts


# ---------------------------------------------------------------------------
# Compose the brief via Sonnet
# ---------------------------------------------------------------------------

def _format_weather_block(weather: dict) -> str:
    """Convert NOAA periods to a compact text block for the prompt."""
    if weather.get("error") or not weather.get("periods"):
        return f"Weather unavailable ({weather.get('error', 'no data')})"

    lines = []
    for p in weather["periods"][:4]:
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
        lines.append(
            f"  {p['name']}: {p['temperature']}°{p['temperatureUnit']}, "
            f"{p['shortForecast']}, wind {p.get('windSpeed','?')}, "
            f"rain {pop}%"
        )
    return "\n".join(lines)


def compose_brief(
    client,
    weather: dict,
    phases: list,
    weather_risks: list,
    due_invoices: list,
    overnight_alerts: list,
    project_name: str,
    site_address: str,
    today: date,
    inspection_alerts: list | None = None,
    unwaived_payments: list | None = None,
) -> tuple[str, str, object]:
    """Compose the morning brief. Returns (subject, html_body, usage)."""

    # --- Build phase snapshot ---
    in_progress = [p for p in phases
                   if (p.get("Status") or "").strip().lower() == "in progress"]
    next_phases  = [p for p in phases
                    if (p.get("Status") or "").strip().lower() == "not started"][:3]
    done_count   = sum(1 for p in phases
                       if (p.get("Status") or "").strip().lower() == "done")
    total_phases = len(phases)

    last_phase_end = ""
    for p in reversed(phases):
        if p.get("End"):
            last_phase_end = p["End"]
            break

    phase_snapshot = f"""  Total phases: {total_phases} | Done: {done_count} | Remaining: {total_phases - done_count}
  Projected completion: {last_phase_end}
  In Progress:
""" + "".join(f"    - Phase #{p.get('#','?')} {p.get('Phase','')} (ends {p.get('End','')})\n"
               for p in in_progress) + """  Coming up next:
""" + "".join(f"    - Phase #{p.get('#','?')} {p.get('Phase','')} (starts {p.get('Start','')})\n"
               for p in next_phases)

    # --- Build invoice block ---
    if due_invoices:
        inv_text = "\n".join(
            f"  - {inv.get('vendor','?')} | "
            f"${float(inv.get('amount',0)):,.0f} | "
            f"due {inv.get('due_date','?')} | "
            f"{inv.get('description','')[:60]}"
            for inv in due_invoices
        )
    else:
        inv_text = "  None due in next 7 days."

    # --- Build weather risk block ---
    if weather_risks:
        risk_text = "\n".join(
            f"  ⚠️  {r['phase']}: {r['risk']} ({r['detail']})"
            for r in weather_risks
        )
    else:
        risk_text = "  No weather conflicts with scheduled work."

    # --- Build overnight alerts block ---
    alerts_text = "\n".join(f"  {a}" for a in overnight_alerts) if overnight_alerts else "  None."

    # Voice from core.chad_voice (narrator mode — speaking TO Chad).
    # Brief-specific rules + output format are appended below.
    system_prompt = chad_voice_system("narrator") + """

Brief-specific:
- This is the morning brief email. If weather puts a phase at risk, that goes at the top.

Output requirements:
- JSON with two keys: "subject" (email subject line) and "html" (complete HTML email body).
- The HTML should be clean, mobile-friendly, inline-styled. Use a white background, dark text.
  Use a thin colored left border on each section header (color: #2c5f8a).
  Keep the markup simple — no external CSS, no classes, inline styles only.
- Do NOT include <html>, <head>, or <body> tags. Just the inner content."""

    # --- Load one-shot system announcements (cleared after use) ---
    announcements_text = ""
    _ann_file = os.path.join(os.path.dirname(__file__), "..", "..", ".brief_announcements.json")
    _ann_file = os.path.normpath(_ann_file)
    try:
        if os.path.exists(_ann_file):
            with open(_ann_file) as _f:
                _ann_data = json.load(_f)
            _items = _ann_data.get("announcements", [])
            if _items:
                announcements_text = "\n".join(f"  • {a}" for a in _items)
            # Clear after reading so it only fires once
            with open(_ann_file, "w") as _f:
                json.dump({"announcements": []}, _f)
    except Exception:
        pass

    # --- Build inspection alert block ---
    insp_text = ""
    if inspection_alerts:
        lines = []
        for p in inspection_alerts:
            days = p.get("days_until_expiry")
            pnum = p.get("permit_number", "")
            ptype = p.get("permit_type", "Building")
            health = p.get("health", "")
            if health == "EXPIRED":
                lines.append(f"  🚨 EXPIRED: {ptype} Permit {pnum} — contact building dept immediately")
            elif health == "CRITICAL":
                lines.append(f"  🚨 CRITICAL: {ptype} Permit {pnum} — only {days} days until expiry, schedule inspection NOW")
            elif health == "WARNING":
                lines.append(f"  ⚠️  WARNING: {ptype} Permit {pnum} — {days} days until expiry, schedule next inspection soon")
        insp_text = "\n".join(lines)

    user_prompt = f"""Generate a morning brief for Chad.

DATE: {today.strftime('%A, %B %-d, %Y')}
PROJECT: {project_name}
JOB SITE: {site_address}

WEATHER FORECAST:
{_format_weather_block(weather)}

WEATHER RISK PHASES:
{risk_text}

PROJECT STATUS:
{phase_snapshot}

INVOICES DUE WITHIN 7 DAYS:
{inv_text}

OVERNIGHT HIGH-URGENCY EMAILS:
{alerts_text}
{f'''
PERMIT EXPIRY ALERTS (include prominently — permit expiry is a serious compliance risk):
{insp_text}
''' if insp_text else ''}
{f'''
LIEN WAIVER ALERTS — payments without signed waivers (potential lien risk):
{chr(10).join(f"  ⚠️  {p.get('Vendor','?')} — ${p.get('Amount ($)','?')} on {p.get('Date','?')} (no waiver on file)" for p in (unwaived_payments or [])[:10])}
''' if unwaived_payments else ''}
{f'''
SYSTEM ANNOUNCEMENTS (include a "What's New" section in the brief):
{announcements_text}
''' if announcements_text else ''}
Generate the morning brief. Lead with any weather risks or permit expiry alerts if present.
Output ONLY a JSON object with keys "subject" and "html". No markdown fence, no preamble."""

    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=BRIEF_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    # Strip code fence if present
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)

    try:
        parsed = json.loads(raw)
        subject = parsed.get("subject", f"Morning Brief — {project_name} — {today.strftime('%b %-d')}")
        html = parsed.get("html", raw)
    except json.JSONDecodeError:
        # Fallback: treat the whole thing as HTML
        subject = f"Morning Brief — {project_name} — {today.strftime('%b %-d')}"
        html = raw

    return subject, html, response.usage


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@beat_on_success("morning-brief", stale_after_seconds=90000)
def main():
    configure_json_logging("hb-brief")
    correlation_id = uuid.uuid4().hex
    logger.info("pass_starting", extra={"event": "pass_starting", "correlation_id": correlation_id})

    parser = argparse.ArgumentParser(
        description="Send the daily morning brief to Chad."
    )
    parser.add_argument(
        "--to", default=BRIEF_RECIPIENT_EMAIL,
        help="Recipient email (default: BRIEF_RECIPIENT_EMAIL from config)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compose and print the email but don't send it"
    )
    args = parser.parse_args()

    today = date.today()
    print(f"Morning Brief — {today.isoformat()}")
    print(f"  Project: {FINANCE_PROJECT_NAME}")
    print(f"  Site: {BRIEF_SITE_ADDRESS}")

    # ── Auth ──────────────────────────────────────────────────────────────────
    print("\nAuthenticating...")
    creds = get_credentials()
    drive_svc  = drive.drive_service(creds)
    sheets_svc = sheets.sheets_service(creds)
    gmail_svc  = gmail_int.gmail_service(creds)
    client     = make_client()

    # ── Weather ───────────────────────────────────────────────────────────────
    print(f"\nFetching NOAA weather ({BRIEF_SITE_LAT}, {BRIEF_SITE_LNG})...")
    weather = fetch_weather(BRIEF_SITE_LAT, BRIEF_SITE_LNG)
    if weather["error"]:
        print(f"  WARNING: weather fetch failed: {weather['error']}")
    else:
        period_names = [p["name"] for p in weather["periods"]]
        print(f"  Got {len(weather['periods'])} periods: {', '.join(period_names)}")

    # ── Project tracker ───────────────────────────────────────────────────────
    print("\nFinding latest Tracker...")
    try:
        tracker = drive.find_latest_tracker(drive_svc, DRIVE_FOLDER_PATH)
        project_name = drive.extract_project_name(tracker["name"])
        print(f"  {project_name}")
        phases = sheets.read_master_schedule(sheets_svc, tracker["id"])
        print(f"  {len(phases)} phases loaded")
    except Exception as e:
        print(f"  WARNING: could not load tracker: {e}")
        phases = []
        project_name = FINANCE_PROJECT_NAME

    # ── Weather risk check ────────────────────────────────────────────────────
    weather_risks = weather_risk_check(phases, weather, today)
    if weather_risks:
        print(f"\n  ⚠️  {len(weather_risks)} weather-risk phase(s) this week:")
        for r in weather_risks:
            print(f"     • {r['phase']}: {r['risk']}")
    else:
        print("\n  No weather conflicts this week.")

    # ── Invoices ──────────────────────────────────────────────────────────────
    print("\nChecking invoices due within 7 days...")
    due_invoices = []
    try:
        finance_folder_id = drive.find_folder_by_path(drive_svc, FINANCE_FOLDER_PATH)
        cost_tracker_files = drive.find_files_by_name_pattern(
            drive_svc, "Cost Tracker", finance_folder_id,
            mime_type="application/vnd.google-apps.spreadsheet",
        )
        if cost_tracker_files:
            ct_id = cost_tracker_files[0]["id"]
            due_invoices = get_due_soon_invoices(sheets_svc, ct_id)
            print(f"  {len(due_invoices)} invoice(s) due within 7 days")
        else:
            print("  No Cost Tracker found; skipping invoices.")
    except Exception as e:
        print(f"  WARNING: invoice lookup failed: {e}")

    # ── Overnight alerts ──────────────────────────────────────────────────────
    overnight_alerts = get_overnight_alerts()
    if overnight_alerts:
        print(f"\n  {len(overnight_alerts)} overnight high-urgency email(s)")
    else:
        print("\n  No overnight high-urgency emails.")

    # ── Inspection / permit expiry check ─────────────────────────────────────
    print("\nChecking permit expiry...")
    inspection_alerts = []
    try:
        from home_builder_agent.agents.inspection_tracker import compute_permit_health, fire_expiry_notification
        insp_records = sheets.read_inspections(sheets_svc, tracker["id"]) if tracker else []
        permit_health = compute_permit_health(insp_records, today)
        inspection_alerts = [p for p in permit_health
                             if p["health"] in ("WARNING", "CRITICAL", "EXPIRED")]
        if inspection_alerts:
            print(f"  ⚠️  {len(inspection_alerts)} permit(s) need attention:")
            for p in inspection_alerts:
                print(f"     • {p['permit_type']} {p['permit_number']} — "
                      f"{p['health']} ({p.get('days_until_expiry', '?')} days until expiry)")
            for p in inspection_alerts:
                fire_expiry_notification(p)
        else:
            print("  All permits OK.")
    except Exception as e:
        print(f"  WARNING: permit check failed: {e}")

    # ── Lien waiver check ────────────────────────────────────────────────────
    print("\nChecking lien waivers...")
    unwaived_payments = []
    try:
        from home_builder_agent.agents.lien_waiver_agent import find_unwaived_payments
        from home_builder_agent.integrations.finance import (
            read_actuals_log as _read_actuals,
            read_lien_waivers as _read_waivers,
        )
        if cost_tracker_files:
            actuals = _read_actuals(sheets_svc, ct_id)
            waivers = _read_waivers(sheets_svc, ct_id)
            wreport = find_unwaived_payments(actuals, waivers, today=today)
            unwaived_payments = wreport["unwaived"]
            if unwaived_payments:
                print(f"  🚨 {len(unwaived_payments)} unwaived payment(s) — lien risk")
            else:
                print("  All payments waived.")
        else:
            print("  No Cost Tracker; skipping waiver check.")
    except Exception as e:
        print(f"  WARNING: waiver check failed: {e}")

    # ── Compose ───────────────────────────────────────────────────────────────
    print(f"\nComposing brief via {WRITER_MODEL}...")
    subject, html_body, usage = compose_brief(
        client=client,
        weather=weather,
        phases=phases,
        weather_risks=weather_risks,
        due_invoices=due_invoices,
        overnight_alerts=overnight_alerts,
        project_name=project_name,
        site_address=BRIEF_SITE_ADDRESS,
        today=today,
        inspection_alerts=inspection_alerts,
        unwaived_payments=unwaived_payments,
    )
    usd = sonnet_cost(usage)["total"]

    # Record to .cost_log.jsonl for daily-cap accounting.
    record_cost(
        agent="hb-brief",
        model=WRITER_MODEL,
        cost_usd=usd,
        note=f"morning brief for {project_name}",
    )
    print(f"  Subject: {subject}")
    print(f"  Cost: ${usd:.4f}")

    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN — email NOT sent")
        print("=" * 60)
        print(f"\nTo: {args.to}")
        print(f"Subject: {subject}")
        print()
        # Strip tags for terminal preview
        preview = re.sub(r"<[^>]+>", "", html_body)
        preview = re.sub(r"\n{3,}", "\n\n", preview).strip()
        print(preview[:3000])
        print()
        return

    # ── Send ──────────────────────────────────────────────────────────────────
    print(f"\nSending to {args.to}...")
    sent = gmail_int.send_email(
        svc=gmail_svc,
        to=args.to,
        subject=subject,
        html_body=html_body,
        sender_name=BRIEF_SENDER_NAME,
    )
    print(f"  ✅  Sent — message ID: {sent.get('id', '?')}")
    print(f"  Cost: ${usd:.4f}")
    logger.info(
        "pass_complete",
        extra={
            "event": "pass_complete",
            "correlation_id": correlation_id,
            "recipient": args.to,
            "message_id": sent.get("id"),
            "cost_usd": round(usd, 4),
        },
    )


if __name__ == "__main__":
    main()
