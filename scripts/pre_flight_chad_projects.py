"""pre_flight_chad_projects.py — Sunday-evening pre-flight for Monday's
`com.chadhomes.client-update` launchd job.

Context: the Monday 7:00 AM Central client-update worker reads
`customer_name` and `customer_email` from each active project and sends
a weekly homeowner-update email. If either field is empty on any active
project, the worker exits 1 and no email goes out. First surfaced by the
Whitfield Residence project on 2026-05-09.

This script gives Connor a 12-hour lead time on that failure mode:

  - Runs Sunday 18:00 Central via launchd job `com.chadhomes.client-update-preflight`
  - Loads every active project from `home_builder.project` (Postgres,
    canonical source per ADR 2026-05-11)
  - Flags any project where `customer_name` or `customer_email` is empty
    or the placeholder string "TBD"
  - On failure: emails Connor at `aiwithconnor@gmail.com` with the list
    of broken projects + missing fields, exits 1, writes a Markdown
    report to `state/pre_flight_chad_<YYYY-MM-DD>.md`
  - On success: exits 0, no email, writes the same Markdown report
    flagged green

Usage:

    # Dry-run (no email send, prints what it would do):
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
      scripts/pre_flight_chad_projects.py --dry-run

    # Real run (sends email if any project fails):
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
      scripts/pre_flight_chad_projects.py

The launchd plist sources `.env` first so DATABASE_URL is available.

Exit codes:
  0 — all active projects have customer_name + customer_email populated
  1 — at least one project is missing a field (Connor was emailed)
  2 — unexpected error (DB unreachable, etc.); also emails Connor
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

from home_builder_agent.config import BRIEF_SENDER_NAME
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.integrations import gmail
from home_builder_agent.integrations.postgres import connection


# Configuration ---------------------------------------------------------------

PREFLIGHT_RECIPIENT_EMAIL = "aiwithconnor@gmail.com"
PREFLIGHT_SUBJECT_PREFIX = "Chad pre-flight FAIL"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"

# Values that count as "missing" — empty/whitespace, or the placeholder
# string that `ensure_project` seeds when a project is first created
# (see store_postgres.ensure_project: `customer_name: str = "TBD"`).
MISSING_SENTINELS = {"", "tbd", "n/a", "none"}


# Helpers ---------------------------------------------------------------------

def _is_missing(value) -> bool:
    """Treat empty/whitespace/TBD/N/A as missing."""
    if value is None:
        return True
    s = str(value).strip().lower()
    return s in MISSING_SENTINELS


def load_active_chad_projects() -> list[dict]:
    """Fetch every active project with the fields we care about.

    Uses status='active' per migration 002 status enum
    (`'active', 'on-hold', 'closed', 'archived'`). We deliberately skip
    on-hold (no weekly email expected) and closed/archived (job done).
    """
    sql = """
        SELECT
            id::text                        AS id,
            name,
            customer_name,
            customer_email,
            status,
            drive_folder_id
        FROM home_builder.project
        WHERE status = 'active'
        ORDER BY name
    """
    with connection(application_name="hb-preflight") as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())


def find_missing(projects: list[dict]) -> list[dict]:
    """Return rows where customer_name or customer_email is missing."""
    out: list[dict] = []
    for p in projects:
        missing_fields = []
        if _is_missing(p.get("customer_name")):
            missing_fields.append("customer_name")
        if _is_missing(p.get("customer_email")):
            missing_fields.append("customer_email")
        if missing_fields:
            out.append({**p, "missing_fields": missing_fields})
    return out


# Report writers --------------------------------------------------------------

def _write_state_report(
    *,
    run_date: date,
    all_projects: list[dict],
    missing: list[dict],
    error: str | None = None,
) -> Path:
    """Persist a Markdown report to state/pre_flight_chad_<YYYY-MM-DD>.md."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"pre_flight_chad_{run_date.isoformat()}.md"

    lines: list[str] = []
    lines.append(f"# Chad pre-flight — {run_date.isoformat()}")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Active projects scanned: **{len(all_projects)}**")

    if error is not None:
        lines.append("")
        lines.append("## STATUS: ERROR")
        lines.append("")
        lines.append("Pre-flight failed before it could finish checking projects.")
        lines.append("")
        lines.append("```")
        lines.append(error)
        lines.append("```")
    elif missing:
        lines.append(f"Failing projects: **{len(missing)}**")
        lines.append("")
        lines.append("## STATUS: FAIL")
        lines.append("")
        lines.append(
            "Monday's `com.chadhomes.client-update` will exit 1 (no email sent) "
            "for each project below until `customer_name` and `customer_email` "
            "are populated in `home_builder.project`. Use hb-chad's "
            "`update_customer_info` tool or update the Tracker Project Info "
            "tab and let the bridge write back to Postgres."
        )
        lines.append("")
        lines.append("| Project | Missing fields | Project ID |")
        lines.append("|---|---|---|")
        for m in missing:
            lines.append(
                f"| {m['name']} | {', '.join(m['missing_fields'])} | `{m['id']}` |"
            )
    else:
        lines.append("")
        lines.append("## STATUS: PASS")
        lines.append("")
        lines.append(
            "Every active Chad project has both `customer_name` and "
            "`customer_email` populated. Monday's client-update worker is "
            "clear to run."
        )

    if all_projects:
        lines.append("")
        lines.append("## All active projects")
        lines.append("")
        lines.append("| Project | customer_name | customer_email |")
        lines.append("|---|---|---|")
        for p in all_projects:
            cn = p.get("customer_name") or "(empty)"
            ce = p.get("customer_email") or "(empty)"
            lines.append(f"| {p['name']} | {cn} | {ce} |")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _build_email_html(missing: list[dict], run_date: date) -> tuple[str, str]:
    """Return (subject, html_body) for the FAIL email."""
    subject = (
        f"{PREFLIGHT_SUBJECT_PREFIX} — {len(missing)} "
        f"project{'s' if len(missing) != 1 else ''} missing customer fields"
    )

    rows_html = "\n".join(
        f"<tr><td style='padding:4px 12px;'>{m['name']}</td>"
        f"<td style='padding:4px 12px;'>{', '.join(m['missing_fields'])}</td>"
        f"<td style='padding:4px 12px;font-family:monospace;font-size:12px;'>{m['id']}</td></tr>"
        for m in missing
    )

    html_body = f"""<div style="max-width:680px;margin:0 auto;font-family:-apple-system,system-ui,sans-serif;color:#1a1a1a;font-size:15px;line-height:1.5;">
  <h2 style="margin:0 0 8px 0;">Chad pre-flight FAIL — {run_date.isoformat()}</h2>
  <p>Monday's <code>com.chadhomes.client-update</code> launchd job will exit 1 (no email sent) for each project below until <code>customer_name</code> and <code>customer_email</code> are populated.</p>
  <p>This pre-flight runs Sundays at 18:00 Central so there's 12+ hours of lead time before the Monday 7:00 AM fire.</p>
  <table style="border-collapse:collapse;border:1px solid #e0e0e0;width:100%;margin:12px 0;">
    <thead>
      <tr style="background:#f6f6f6;">
        <th style="text-align:left;padding:6px 12px;border-bottom:1px solid #e0e0e0;">Project</th>
        <th style="text-align:left;padding:6px 12px;border-bottom:1px solid #e0e0e0;">Missing fields</th>
        <th style="text-align:left;padding:6px 12px;border-bottom:1px solid #e0e0e0;">Project ID</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
  <p><strong>How to fix:</strong> use hb-chad's <code>update_customer_info</code> tool, or update the Tracker Project Info tab and let the bridge write back to Postgres. Re-run <code>scripts/pre_flight_chad_projects.py</code> once fixed to confirm.</p>
  <p style="color:#888;font-size:13px;">Full report: <code>~/Projects/home-builder-agent/state/pre_flight_chad_{run_date.isoformat()}.md</code></p>
</div>"""

    return subject, html_body


def _build_error_email(error: str, run_date: date) -> tuple[str, str]:
    """Return (subject, html_body) for the ERROR email (unexpected failure)."""
    subject = f"{PREFLIGHT_SUBJECT_PREFIX} — pre-flight ERROR (could not run)"
    html_body = f"""<div style="max-width:680px;margin:0 auto;font-family:-apple-system,system-ui,sans-serif;color:#1a1a1a;font-size:15px;line-height:1.5;">
  <h2>Chad pre-flight ERROR — {run_date.isoformat()}</h2>
  <p>The Sunday pre-flight script failed before it could check projects. Monday's client-update may still fire — verify customer fields manually before 7:00 AM Central.</p>
  <pre style="background:#f6f6f6;padding:12px;overflow:auto;font-size:12px;">{error}</pre>
</div>"""
    return subject, html_body


# Mail send -------------------------------------------------------------------

def send_failure_email(missing: list[dict], run_date: date) -> str:
    """Send the failure email via Gmail. Returns Gmail message ID."""
    creds = get_credentials()
    gmail_svc = gmail.gmail_service(creds)
    subject, html_body = _build_email_html(missing, run_date)
    result = gmail.send_email(
        gmail_svc,
        to=PREFLIGHT_RECIPIENT_EMAIL,
        subject=subject,
        html_body=html_body,
        sender_name=BRIEF_SENDER_NAME,
    )
    return result.get("id", "(no-id)")


def send_error_email(error: str, run_date: date) -> str:
    creds = get_credentials()
    gmail_svc = gmail.gmail_service(creds)
    subject, html_body = _build_error_email(error, run_date)
    result = gmail.send_email(
        gmail_svc,
        to=PREFLIGHT_RECIPIENT_EMAIL,
        subject=subject,
        html_body=html_body,
        sender_name=BRIEF_SENDER_NAME,
    )
    return result.get("id", "(no-id)")


# Main ------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sunday pre-flight for the Monday Chad client-update job."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip the Gmail send (still writes the state report and exits with the right code).",
    )
    args = parser.parse_args()

    # Env override for launchd / shell wrapper:
    #   PREFLIGHT_NO_SEND=1 forces dry-run regardless of CLI flag.
    if os.environ.get("PREFLIGHT_NO_SEND") == "1":
        args.dry_run = True

    run_date = date.today()
    print(f"[pre-flight] {run_date.isoformat()} — checking active Chad projects")

    try:
        projects = load_active_chad_projects()
    except Exception as exc:  # pragma: no cover (network / DB error path)
        err = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        print(f"[pre-flight] ERROR loading projects: {exc}", file=sys.stderr)
        report_path = _write_state_report(
            run_date=run_date,
            all_projects=[],
            missing=[],
            error=err,
        )
        print(f"[pre-flight] report: {report_path}")
        if not args.dry_run:
            try:
                msg_id = send_error_email(err, run_date)
                print(f"[pre-flight] error email sent: {msg_id}")
            except Exception as send_exc:
                print(
                    f"[pre-flight] also failed to send error email: {send_exc}",
                    file=sys.stderr,
                )
        else:
            print("[pre-flight] dry-run: skipping error email")
        return 2

    print(f"[pre-flight] loaded {len(projects)} active project(s)")
    for p in projects:
        cn_show = p.get("customer_name") or "(empty)"
        ce_show = p.get("customer_email") or "(empty)"
        print(f"  - {p['name']:40s}  name={cn_show!r:30s} email={ce_show!r}")

    missing = find_missing(projects)
    report_path = _write_state_report(
        run_date=run_date,
        all_projects=projects,
        missing=missing,
    )
    print(f"[pre-flight] report: {report_path}")

    if not missing:
        print("[pre-flight] PASS — every active project has customer_name + customer_email")
        return 0

    print(f"[pre-flight] FAIL — {len(missing)} project(s) missing fields:")
    for m in missing:
        print(f"  - {m['name']}  missing: {', '.join(m['missing_fields'])}")

    if args.dry_run:
        print("[pre-flight] dry-run: skipping email send")
    else:
        msg_id = send_failure_email(missing, run_date)
        print(f"[pre-flight] failure email sent to {PREFLIGHT_RECIPIENT_EMAIL} (id: {msg_id})")

    return 1


if __name__ == "__main__":
    sys.exit(main())
