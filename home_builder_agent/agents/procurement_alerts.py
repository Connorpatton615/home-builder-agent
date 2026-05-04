"""procurement_alerts.py — procurement order-window checker.

Called automatically by hb-update after any schedule change. No separate
command needed — every status update triggers a check on all affected phases.

Logic:
  For each updated phase whose name matches a known material category,
  compute order_by_date = phase.Start - lead_time_weeks.
  If today >= order_by_date - PROCUREMENT_UPCOMING_DAYS:
    - Fire a macOS notification
    - Log a row to the Tracker sheet's "Procurement Alerts" tab

Alert types (in priority order):
  OVERDUE    — order_by_date has already passed
  ORDER NOW  — order_by_date is today
  THIS WEEK  — order_by_date is within 7 days
  UPCOMING   — order_by_date is within PROCUREMENT_UPCOMING_DAYS (default 14 days)
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta
from typing import Optional

from home_builder_agent.config import (
    PROCUREMENT_LEAD_TIMES,
    PROCUREMENT_UPCOMING_DAYS,
)


# ---------------------------------------------------------------------------
# Date helpers (duplicated here to avoid circular imports with status_updater)
# ---------------------------------------------------------------------------

def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Lead-time matching
# ---------------------------------------------------------------------------

def match_lead_time(phase_name: str) -> tuple[str | None, int | None]:
    """Return (keyword, weeks) if the phase name contains a known material keyword.

    Searches PROCUREMENT_LEAD_TIMES in insertion order; the first match wins.
    Returns (None, None) if no keyword matches.
    """
    name_lower = phase_name.lower()
    for keyword, weeks in PROCUREMENT_LEAD_TIMES.items():
        if keyword in name_lower:
            return keyword, weeks
    return None, None


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------

def check_procurement_alerts(
    phases: list[dict],
    updated_indices: set[int] | None = None,
    today: date | None = None,
) -> list[dict]:
    """Check phases for procurement order-window alerts.

    Args:
        phases:          Full phase list from read_master_schedule().
        updated_indices: Indices of phases just modified (None = check all).
        today:           Date override for unit testing.

    Returns:
        List of alert dicts, one per triggered phase. Empty list if none.
    """
    if today is None:
        today = date.today()

    check_set = updated_indices if updated_indices is not None else set(range(len(phases)))
    alerts: list[dict] = []

    for i in check_set:
        phase = phases[i]

        # Skip completed phases — nothing to order
        status = (phase.get("Status") or "").strip()
        if status in ("Done", "Completed"):
            continue

        phase_name = (phase.get("Phase") or "").strip()
        if not phase_name:
            continue

        keyword, lead_weeks = match_lead_time(phase_name)
        if lead_weeks is None:
            continue

        start = _parse_date(phase.get("Start"))
        if not start:
            continue

        # Don't alert on phases that have already started
        if today >= start:
            continue

        order_by = start - timedelta(weeks=lead_weeks)
        days_until_order = (order_by - today).days

        # Only alert within the PROCUREMENT_UPCOMING_DAYS window
        if days_until_order > PROCUREMENT_UPCOMING_DAYS:
            continue

        # Classify
        if today > order_by:
            alert_type = "OVERDUE"
        elif days_until_order == 0:
            alert_type = "ORDER NOW"
        elif days_until_order <= 7:
            alert_type = "THIS WEEK"
        else:
            alert_type = "UPCOMING"

        alerts.append(
            {
                "phase_num": phase.get("#", "?"),
                "phase_name": phase_name,
                "keyword": keyword,
                "lead_weeks": lead_weeks,
                "start": start,
                "order_by": order_by,
                "days_until_order": days_until_order,
                "alert_type": alert_type,
            }
        )

    # Sort: most urgent first (OVERDUE → ORDER NOW → THIS WEEK → UPCOMING)
    _priority = {"OVERDUE": 0, "ORDER NOW": 1, "THIS WEEK": 2, "UPCOMING": 3}
    alerts.sort(key=lambda a: _priority.get(a["alert_type"], 99))

    return alerts


# ---------------------------------------------------------------------------
# macOS notification
# ---------------------------------------------------------------------------

def _notification_body(alert: dict) -> str:
    order_str = alert["order_by"].strftime("%b %-d")
    start_str = alert["start"].strftime("%b %-d")
    days = alert["days_until_order"]

    if alert["alert_type"] == "OVERDUE":
        overdue_days = -days
        return (
            f"{alert['phase_name']}: order window passed {overdue_days}d ago "
            f"(phase starts {start_str}, {alert['lead_weeks']}wk lead)"
        )
    elif alert["alert_type"] == "ORDER NOW":
        return (
            f"{alert['phase_name']}: order TODAY — "
            f"phase starts {start_str}, {alert['lead_weeks']}wk lead"
        )
    else:
        return (
            f"{alert['phase_name']}: order by {order_str} ({days}d) — "
            f"phase starts {start_str}, {alert['lead_weeks']}wk lead"
        )


def fire_macos_notification(alert: dict) -> None:
    """Fire a macOS notification for a single procurement alert."""
    emoji = {"OVERDUE": "🚨", "ORDER NOW": "🚨", "THIS WEEK": "⚠️", "UPCOMING": "📅"}
    title = f"{emoji.get(alert['alert_type'], '📅')} Procurement Alert — {alert['alert_type']}"
    body = _notification_body(alert)
    try:
        subprocess.run(
            [
                "osascript", "-e",
                f'display notification "{body}" with title "{title}"',
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass  # Never let notification failure crash hb-update


def fire_all_notifications(alerts: list[dict]) -> None:
    """Fire macOS notifications for all alerts. One notification per alert."""
    for alert in alerts:
        fire_macos_notification(alert)


# ---------------------------------------------------------------------------
# Console summary (printed by hb-update)
# ---------------------------------------------------------------------------

def print_alert_summary(alerts: list[dict]) -> None:
    """Print a compact procurement alert block to stdout."""
    if not alerts:
        return

    print()
    print("─" * 60)
    print("PROCUREMENT ALERTS")
    print("─" * 60)
    for a in alerts:
        emoji = {"OVERDUE": "🚨", "ORDER NOW": "🚨", "THIS WEEK": "⚠️", "UPCOMING": "📅"}
        icon = emoji.get(a["alert_type"], "📅")
        order_str = a["order_by"].strftime("%b %-d")
        start_str = a["start"].strftime("%b %-d")
        days = a["days_until_order"]
        if a["alert_type"] == "OVERDUE":
            overdue = -days
            print(
                f"  {icon} [{a['alert_type']}] #{a['phase_num']} {a['phase_name']} — "
                f"order window passed {overdue}d ago | phase starts {start_str}"
            )
        elif days == 0:
            print(
                f"  {icon} [{a['alert_type']}] #{a['phase_num']} {a['phase_name']} — "
                f"order TODAY | phase starts {start_str} | {a['lead_weeks']}wk lead"
            )
        else:
            print(
                f"  {icon} [{a['alert_type']}] #{a['phase_num']} {a['phase_name']} — "
                f"order by {order_str} ({days}d) | phase starts {start_str} | {a['lead_weeks']}wk lead"
            )
    print("─" * 60)
