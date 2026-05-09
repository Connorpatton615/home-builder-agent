"""morning_view_agent.py — hb-morning CLI for the morning view-model.

Per docs/specs/morning-view-model.md. Assembles the morning surface
payload end-to-end: load project from Postgres, compute drop-deads,
load overnight events + pending drafts, fetch weather, synthesize
voice_brief + action_items via hb-chad narrator-mode, and print the
result.

CLI:
  hb-morning <project>                      pretty-print payload
  hb-morning <project> --json               machine-readable JSON
  hb-morning <project> --no-synth           skip the Sonnet call
                                             (voice_brief=null,
                                              action_items=[])
  hb-morning <project> --today YYYY-MM-DD   override today (for testing)

Project resolution: UUID → exact-name → case-insensitive substring
(matches the hb-triggers / hb-schedule pattern).

Cost: ~$0.02/run (one Sonnet call when --no-synth not set).
$0/run with --no-synth.

This is the vertical-side caller for the morning view-model. The
platform thread's HTTP route handler at
/v1/turtles/home-builder/views/morning/{project_id} will be a thin
wrapper around this same logic.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone

from home_builder_agent.config import (
    BRIEF_SITE_ADDRESS,
    BRIEF_SITE_LAT,
    BRIEF_SITE_LNG,
)
from home_builder_agent.core.claude_client import make_client
from home_builder_agent.observability.json_log import configure_json_logging
from home_builder_agent.scheduling.lead_times import compute_drop_dead_dates
from home_builder_agent.scheduling.morning_synth import (
    compose_voice_brief_and_actions,
)
from home_builder_agent.scheduling.schemas import (
    MorningWeatherPayload,
    MorningWeatherRiskPhasePayload,
    Severity,
)
from home_builder_agent.scheduling.view_models import morning_view
from home_builder_agent.scheduling.weather import (
    fetch_weather,
    weather_risk_check,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Project resolution (mirrors triggers_agent._resolve_project)
# ---------------------------------------------------------------------------


def _resolve_project(name_or_id: str):
    """Return (project_id, project_name) for a name-substring or UUID arg.

    Resolution order: UUID → exact name → case-insensitive substring.
    """
    from home_builder_agent.scheduling.store_postgres import (
        load_active_projects,
        load_project_by_id,
        load_project_by_name,
    )

    try:
        _uuid.UUID(name_or_id)
        row = load_project_by_id(name_or_id)
        if row:
            return str(row["id"]), row.get("name")
    except (ValueError, KeyError):
        pass

    row = load_project_by_name(name_or_id)
    if row:
        return str(row["id"]), row.get("name")

    needle = name_or_id.lower()
    try:
        for p in load_active_projects():
            name = p.get("name") or ""
            if needle in name.lower():
                return str(p["id"]), name
    except Exception:
        pass

    return None, None


# ---------------------------------------------------------------------------
# Adapters: raw weather → MorningWeatherPayload
# ---------------------------------------------------------------------------


def _build_weather_payload(
    weather_raw: dict,
    schedule_phases: list,
    today: date,
) -> MorningWeatherPayload | None:
    """Convert fetch_weather() output + run weather_risk_check.

    Returns None if weather fetch failed (no periods). The morning
    surface omits the section in that case per spec § Section ordering.
    """
    if not weather_raw or not weather_raw.get("periods"):
        return None

    periods = weather_raw["periods"]

    def _summarize(p: dict) -> str:
        short = p.get("shortForecast", "")
        temp = p.get("temperature", "?")
        unit = p.get("temperatureUnit", "F")
        wind = p.get("windSpeed", "")
        parts = [short, f"{temp}°{unit}"]
        if wind:
            parts.append(wind)
        return ", ".join(parts).rstrip(", ")

    summary_today = _summarize(periods[0])
    summary_tomorrow = _summarize(periods[1]) if len(periods) > 1 else None

    risks = weather_risk_check(schedule_phases, weather_raw, today)
    risk_payloads: list[MorningWeatherRiskPhasePayload] = []
    for r in risks:
        # Heuristic risk_kind extraction from the risk string
        kind = "rain"
        risk_str = (r.get("risk") or "").lower()
        if "wind" in risk_str:
            kind = "wind"
        elif "low temp" in risk_str:
            kind = "extreme-cold"
        elif "high temp" in risk_str:
            kind = "extreme-heat"

        # Severity heuristic — high rain pct or extreme temps → critical, else warning
        sev = Severity.WARNING
        if "100" in risk_str or "low temp" in risk_str:
            sev = Severity.CRITICAL

        risk_payloads.append(
            MorningWeatherRiskPhasePayload(
                phase_id=None,                     # phase id not threaded through legacy risk-check shape
                phase_name=r.get("phase", "(unknown)"),
                risk_kind=kind,
                detail=f"{r.get('risk', '')} — {r.get('detail', '')}".strip(" —"),
                severity=sev,
            )
        )

    return MorningWeatherPayload(
        summary_today=summary_today,
        summary_tomorrow=summary_tomorrow,
        risk_phases=risk_payloads,
    )


# ---------------------------------------------------------------------------
# Schedule snapshot helper
# ---------------------------------------------------------------------------


def _schedule_snapshot(schedule, today: date) -> dict:
    """Compute a compact snapshot for morning_synth's prompt context."""
    if schedule is None:
        return {}

    current_phase = None
    for p in schedule.phases:
        if p.planned_start_date <= today <= p.planned_end_date:
            current_phase = p.name
            break

    total_duration = sum(p.duration_days for p in schedule.phases) or 1
    completed_duration = sum(
        p.duration_days for p in schedule.phases if (p.status or "").lower() == "complete"
    )
    pct_complete = round(100.0 * completed_duration / total_duration, 1)

    return {
        "current_phase": current_phase or "(between phases)",
        "pct_complete": pct_complete,
        "estimated_completion_date": (
            schedule.estimated_completion_date.isoformat()
            if schedule.estimated_completion_date
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Pretty terminal output
# ---------------------------------------------------------------------------


def _print_pretty(payload) -> None:
    line = "═" * 72
    print()
    print(line)
    print(f" MORNING VIEW — {payload.project_name}")
    print(f" {payload.as_of_local_date}  ({payload.tz})")
    print(line)

    # 1. Weather
    if payload.weather:
        print("\n  ☁  Weather")
        print(f"     Today:    {payload.weather.summary_today}")
        if payload.weather.summary_tomorrow:
            print(f"     Tomorrow: {payload.weather.summary_tomorrow}")
        for r in payload.weather.risk_phases:
            icon = "🚨" if r.severity.value == "critical" else "⚠️ "
            print(f"     {icon} {r.phase_name}: {r.detail}")

    # 2. Voice brief
    if payload.voice_brief and payload.voice_brief.text:
        print("\n  💬 Brief")
        for line_ in _wrap(payload.voice_brief.text, 68):
            print(f"     {line_}")
        if payload.voice_brief.cost_usd is not None:
            print(f"     [model={payload.voice_brief.model} cost=${payload.voice_brief.cost_usd:.4f}]")

    # 3. Judgment queue
    print(f"\n  📨 Judgment queue ({payload.judgment_queue.count})")
    if payload.judgment_queue.count == 0:
        print("     Inbox is clear.")
    else:
        for it in payload.judgment_queue.items:
            print(f"     • [{it.kind.value}] {it.summary[:60]}")
            print(f"       drafted by {it.originating_agent}")

    # 4. Today on site
    print("\n  🛠  Today on site")
    if not payload.today_on_site.items:
        print("     Quiet day on site.")
    else:
        for it in payload.today_on_site.items:
            chip = f" [{it.urgency_band.value.upper()}]" if it.urgency_band.value != "calm" else ""
            reason = f" — {it.urgency_reason}" if it.urgency_reason else ""
            label = it.phase_name or it.material_category or it.kind.value
            print(f"     • {label}{chip}{reason}")

    # 5. Drop-deads
    print("\n  🚨 Today's drop-deads")
    if not payload.todays_drop_deads.items:
        print("     Nothing imminent.")
    else:
        for it in payload.todays_drop_deads.items:
            days = (it.drop_dead_date - payload.as_of_local_date).days
            when = "TODAY" if days == 0 else (
                f"{days}d out" if days > 0 else f"{abs(days)}d OVERDUE"
            )
            print(
                f"     • {it.material_category} ({it.install_phase_name}) — "
                f"{it.drop_dead_date} ({when})"
            )

    # 6. Overnight events
    if payload.overnight_events.items:
        print("\n  🌙 Overnight events")
        for it in payload.overnight_events.items:
            hrs = max(1, it.age_seconds // 3600)
            print(f"     • [{it.severity.value}] {it.summary[:80]}")
            print(f"       {hrs}h ago, type={it.type}")

    # 7. Action items
    print("\n  ✅ Action items")
    if not payload.action_items:
        print("     (none)")
    else:
        for i, item in enumerate(payload.action_items, 1):
            print(f"     {i}. {item}")

    print()
    print(line)
    print()


def _wrap(text: str, width: int) -> list[str]:
    """Tiny word wrapper for the terminal brief render."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    configure_json_logging("hb-morning")

    parser = argparse.ArgumentParser(
        description="Render Chad's morning view payload for a project.",
        epilog=(
            "Examples:\n"
            "  hb-morning Whitfield                  # full payload (Sonnet synthesis on)\n"
            "  hb-morning Whitfield --no-synth       # skip Sonnet (voice_brief=null)\n"
            "  hb-morning Whitfield --json           # machine-readable JSON\n"
            "  hb-morning Whitfield --today 2026-05-09\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "project",
        help="Project name (substring OK), exact name, or UUID",
    )
    parser.add_argument(
        "--today", default=None,
        help="Override today's date (YYYY-MM-DD). For testing / replay.",
    )
    parser.add_argument(
        "--no-synth", action="store_true",
        help="Skip the Sonnet call. voice_brief stays null and "
             "action_items stays empty. Useful for cost-free smoke tests.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of pretty terminal output.",
    )
    parser.add_argument(
        "--lat", type=float, default=BRIEF_SITE_LAT,
        help=f"Job-site latitude for weather fetch (default: {BRIEF_SITE_LAT})",
    )
    parser.add_argument(
        "--lng", type=float, default=BRIEF_SITE_LNG,
        help=f"Job-site longitude for weather fetch (default: {BRIEF_SITE_LNG})",
    )
    parser.add_argument(
        "--site-address", default=BRIEF_SITE_ADDRESS,
        help="Job-site address for the Sonnet prompt (cosmetic only)",
    )
    args = parser.parse_args()

    today = (
        datetime.strptime(args.today, "%Y-%m-%d").date()
        if args.today else date.today()
    )
    correlation_id = _uuid.uuid4().hex
    logger.info(
        "morning_view_starting",
        extra={
            "event": "morning_view_starting",
            "correlation_id": correlation_id,
            "project_arg": args.project,
            "today": today.isoformat(),
            "synth_enabled": not args.no_synth,
        },
    )

    # ── Resolve project ──────────────────────────────────────────────────
    from home_builder_agent.integrations.postgres import PostgresConfigError
    try:
        project_id, project_name = _resolve_project(args.project)
    except PostgresConfigError as e:
        print(f"❌ Postgres config error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Postgres connection failed: {type(e).__name__}: {e}")
        sys.exit(1)

    if not project_id:
        print(f"❌ No project found matching '{args.project}'")
        sys.exit(1)

    # ── Load schedule + drop-deads ───────────────────────────────────────
    from home_builder_agent.scheduling.store_postgres import (
        compose_schedule_from_db,
        list_draft_actions_for_project,
        load_recent_events_for_project,
    )
    schedule = compose_schedule_from_db(project_id)
    drop_dead_dates = compute_drop_dead_dates(schedule) if schedule else []

    # ── Overnight events: last 14h, severity ≥ warning ──────────────────
    raw_events = load_recent_events_for_project(
        project_id=project_id,
        since_hours=14,
        limit=20,
    )
    overnight_events = [
        e for e in raw_events
        if (e.severity or "info") in ("warning", "critical", "blocking")
    ]

    # ── Pending drafts ───────────────────────────────────────────────────
    # If migration 007 hasn't been applied yet (the table doesn't exist in
    # Supabase), gracefully fall back to empty queue. The morning view
    # itself stays valid; the renderer's empty-state path covers it.
    try:
        pending_drafts = list_draft_actions_for_project(
            project_id=project_id,
            limit=50,
        )
    except Exception as e:
        msg = str(e).lower()
        if "does not exist" in msg and "draft_action" in msg:
            print(
                "ℹ️  home_builder.draft_action table not yet present "
                "(migration 007 pending — judgment_queue will render empty)",
                file=sys.stderr,
            )
            pending_drafts = []
        else:
            raise

    # ── Weather ──────────────────────────────────────────────────────────
    weather_raw = fetch_weather(args.lat, args.lng)
    weather_payload = _build_weather_payload(
        weather_raw,
        schedule.phases if schedule else [],
        today,
    )

    # ── Initial projection (without voice_brief/action_items) ────────────
    payload = morning_view(
        project_id=project_id,
        project_name=project_name,
        schedule=schedule,
        drop_dead_dates=drop_dead_dates,
        overnight_events=overnight_events,
        pending_drafts=pending_drafts,
        weather=weather_payload,
        today=today,
    )

    # ── Synthesize voice_brief + action_items (one Sonnet call) ──────────
    if not args.no_synth:
        try:
            client = make_client()
            vb, action_items, _usage = compose_voice_brief_and_actions(
                client=client,
                project_name=project_name,
                today=today,
                judgment_queue=payload.judgment_queue,
                today_on_site=payload.today_on_site,
                todays_drop_deads=payload.todays_drop_deads,
                overnight_events=payload.overnight_events,
                weather=payload.weather,
                schedule_summary=_schedule_snapshot(schedule, today),
                site_address=args.site_address,
            )
            payload = payload.model_copy(update={
                "voice_brief": vb,
                "action_items": action_items,
            })
        except Exception as e:
            logger.warning(
                "morning_synth_failed",
                extra={
                    "event": "morning_synth_failed",
                    "correlation_id": correlation_id,
                    "exception_type": type(e).__name__,
                    "message": str(e),
                },
            )
            print(f"⚠️  Sonnet synthesis failed: {type(e).__name__}: {e}", file=sys.stderr)
            print(f"   Continuing with empty voice_brief / action_items.", file=sys.stderr)

    # ── Output ────────────────────────────────────────────────────────────
    if args.json:
        print(payload.model_dump_json(indent=2, exclude_none=False))
    else:
        _print_pretty(payload)

    logger.info(
        "morning_view_complete",
        extra={
            "event": "morning_view_complete",
            "correlation_id": correlation_id,
            "project_id": project_id,
            "judgment_queue_count": payload.judgment_queue.count,
            "today_on_site_count": len(payload.today_on_site.items),
            "drop_deads_count": len(payload.todays_drop_deads.items),
            "overnight_events_count": len(payload.overnight_events.items),
            "action_items_count": len(payload.action_items),
            "synthesized": not args.no_synth and payload.voice_brief is not None,
        },
    )


if __name__ == "__main__":
    main()
