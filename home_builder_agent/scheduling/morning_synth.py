"""morning_synth.py — voice_brief + action_items synthesis for the morning view.

Per docs/specs/morning-view-model.md § Source mapping. The morning view's
voice_brief and action_items are composed in ONE Sonnet call (one
round-trip, two deliverables) using chad_voice_system("narrator") + a
morning-view-specific output contract.

The caller (hb-morning CLI today; future FastAPI route handler) builds
the morning view's projection sections first (judgment_queue,
today_on_site, todays_drop_deads, overnight_events, weather), passes
them as context to compose_voice_brief_and_actions(), receives back a
MorningVoiceBriefPayload + list[str], and splices both into the final
MorningViewPayload via morning_view().

Cost: ~$0.02/run (one Sonnet call with the morning view's compact
context block).

This module is engine-side and pure-Python aside from the Anthropic
client call. No DB I/O. No agent-module imports. The Anthropic client
is passed in (not instantiated here) so the caller controls
authentication + retry policy.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from home_builder_agent.config import BRIEF_MAX_TOKENS, WRITER_MODEL
from home_builder_agent.core.chad_voice import chad_voice_system
from home_builder_agent.core.claude_client import sonnet_cost
from home_builder_agent.core.cost_guard import record_cost
from home_builder_agent.scheduling.schemas import (
    MorningDropDeadsPayload,
    MorningJudgmentQueuePayload,
    MorningOvernightEventsPayload,
    MorningTodayOnSitePayload,
    MorningVoiceBriefPayload,
    MorningWeatherPayload,
)


logger = logging.getLogger(__name__)


# Morning-view-specific output contract appended to the chad_voice
# narrator system prompt. Keeps the voice rules in core/chad_voice.py
# canonical and only adds the structural shape requirement here.
_OUTPUT_CONTRACT = """

Output requirements (morning view):
- JSON only. No markdown fence, no preamble, no commentary.
- Two top-level keys: "voice_brief" (object) and "action_items" (array of strings).
- voice_brief.text is a 3-5 sentence paragraph in Chad's voice synthesizing
  the day. Lead with the single most important thing. If weather risk is
  pinned, mention it once but the renderer pins the weather card separately
  — don't restate the forecast verbatim.
- action_items is an ordered list of 1-5 imperative sentences. Order by
  what should happen first today. Each item ≤ 12 words. No filler
  ("review your inbox", "stay focused"). If the day is genuinely light,
  pick the smallest concrete thing ("Walk the site at 10, no decisions
  needed today.") rather than padding to 5.
"""


def _format_weather_block(weather: MorningWeatherPayload | None) -> str:
    if weather is None:
        return "  (no weather data)"
    out = [f"  Today: {weather.summary_today}"]
    if weather.summary_tomorrow:
        out.append(f"  Tomorrow: {weather.summary_tomorrow}")
    if weather.risk_phases:
        out.append("  AT-RISK PHASES:")
        for r in weather.risk_phases:
            out.append(
                f"    ⚠️  {r.phase_name}: {r.detail} "
                f"(severity={r.severity.value}, kind={r.risk_kind})"
            )
    return "\n".join(out)


def _format_judgment_queue(queue: MorningJudgmentQueuePayload) -> str:
    if queue.count == 0:
        return "  Inbox is clear."
    lines = [f"  {queue.count} drafts pending review:"]
    for item in queue.items[:10]:                  # cap context size
        lines.append(
            f"    • [{item.kind.value}] {item.summary[:120]} "
            f"(drafted by {item.originating_agent})"
        )
    return "\n".join(lines)


def _format_today_on_site(today_on_site: MorningTodayOnSitePayload) -> str:
    if not today_on_site.items:
        return "  Quiet day on site."
    lines = []
    for it in today_on_site.items:
        chip = f" [{it.urgency_band.value.upper()}]" if it.urgency_band.value != "calm" else ""
        reason = f" — {it.urgency_reason}" if it.urgency_reason else ""
        if it.kind.value == "phase-active":
            lines.append(
                f"    • Phase active: {it.phase_name} "
                f"(day {it.day_n}/{it.of_total}){chip}{reason}"
            )
        elif it.kind.value == "delivery":
            lines.append(
                f"    • Delivery expected: {it.material_category}{chip}{reason}"
            )
        elif it.kind.value == "inspection":
            lines.append(
                f"    • Inspection: {it.phase_name}{chip}{reason}"
            )
        else:
            lines.append(f"    • {it.kind.value}{chip}{reason}")
    return "\n".join(lines)


def _format_drop_deads(drop_deads: MorningDropDeadsPayload, today: date) -> str:
    if not drop_deads.items:
        return "  Nothing imminent."
    lines = []
    for it in drop_deads.items:
        days = (it.drop_dead_date - today).days
        when = "TODAY" if days == 0 else (f"in {days}d" if days > 0 else f"{abs(days)}d OVERDUE")
        lines.append(
            f"    🚨 {it.material_category} ({it.install_phase_name}) "
            f"— drop-dead {it.drop_dead_date} ({when}, {it.lead_time_days}d lead time)"
        )
    return "\n".join(lines)


def _format_overnight_events(overnight: MorningOvernightEventsPayload) -> str:
    if not overnight.items:
        return "  Quiet overnight."
    lines = []
    for it in overnight.items[:8]:                 # cap
        hrs = max(1, it.age_seconds // 3600)
        lines.append(
            f"    [{it.severity.value}] {it.summary[:140]} "
            f"({hrs}h ago, type={it.type})"
        )
    return "\n".join(lines)


def _format_schedule_snapshot(schedule_summary: dict | None) -> str:
    if not schedule_summary:
        return "  (no schedule data)"
    lines = []
    if "current_phase" in schedule_summary:
        lines.append(f"  Current phase: {schedule_summary['current_phase']}")
    if "pct_complete" in schedule_summary:
        lines.append(
            f"  Project: {int(schedule_summary['pct_complete'])}% complete"
        )
    if "estimated_completion_date" in schedule_summary:
        lines.append(
            f"  Projected completion: {schedule_summary['estimated_completion_date']}"
        )
    return "\n".join(lines) or "  (no schedule snapshot)"


def _strip_json_fence(raw: str) -> str:
    """Remove leading/trailing ```json … ``` fences from a Sonnet response."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)
    return raw.strip()


def compose_voice_brief_and_actions(
    *,
    client,                                          # Anthropic client
    project_name: str,
    today: date,
    judgment_queue: MorningJudgmentQueuePayload,
    today_on_site: MorningTodayOnSitePayload,
    todays_drop_deads: MorningDropDeadsPayload,
    overnight_events: MorningOvernightEventsPayload,
    weather: MorningWeatherPayload | None = None,
    schedule_summary: dict | None = None,
    site_address: str | None = None,
    model: str = WRITER_MODEL,
    max_tokens: int = BRIEF_MAX_TOKENS,
) -> tuple[MorningVoiceBriefPayload, list[str], Any]:
    """Compose the morning view's voice_brief + action_items in one Sonnet call.

    Returns a 3-tuple:
      (voice_brief, action_items, response.usage)

    The caller is responsible for assembling the final MorningViewPayload
    via morning_view() — this function returns just the synthesized
    pieces.

    Failure mode: if the response can't be parsed as JSON, returns a
    fallback voice_brief with the raw text + an empty action_items
    list. The renderer's empty-state path handles the latter; voice_brief
    surfaces SOMETHING rather than nothing so Chad's morning isn't blank.
    """

    system_prompt = chad_voice_system("narrator") + _OUTPUT_CONTRACT

    started = datetime.now(timezone.utc)

    user_prompt = f"""Compose Chad's morning brief for {project_name}.

DATE: {today.strftime('%A, %B %-d, %Y')}{f"  |  SITE: {site_address}" if site_address else ""}

WEATHER:
{_format_weather_block(weather)}

JUDGMENT QUEUE (drafts pending Chad's review):
{_format_judgment_queue(judgment_queue)}

TODAY ON SITE:
{_format_today_on_site(today_on_site)}

TODAY'S DROP-DEADS (selection deadlines today or imminent):
{_format_drop_deads(todays_drop_deads, today)}

OVERNIGHT EVENTS (last ~14h, severity ≥ warning):
{_format_overnight_events(overnight_events)}

PROJECT STATUS:
{_format_schedule_snapshot(schedule_summary)}

Now compose voice_brief + action_items per the output contract above. JSON only."""

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text
    cleaned = _strip_json_fence(raw)

    voice_brief_text = ""
    action_items: list[str] = []
    try:
        parsed = json.loads(cleaned)
        vb = parsed.get("voice_brief")
        if isinstance(vb, dict):
            voice_brief_text = (vb.get("text") or "").strip()
        elif isinstance(vb, str):
            # Tolerant: accept voice_brief as a string (some Sonnet responses
            # collapse the object when there's only one field).
            voice_brief_text = vb.strip()

        items = parsed.get("action_items") or []
        if isinstance(items, list):
            action_items = [
                str(x).strip() for x in items
                if isinstance(x, (str, int, float)) and str(x).strip()
            ][:5]                                  # spec cap
    except (json.JSONDecodeError, ValueError, AttributeError) as e:
        # Fallback path — log + surface raw text as the brief so Chad's
        # morning isn't blank when the model misbehaves.
        logger.warning(
            "morning_synth_parse_failed",
            extra={
                "event": "morning_synth_parse_failed",
                "exception_type": type(e).__name__,
                "raw_preview": cleaned[:200],
            },
        )
        voice_brief_text = cleaned[:600]           # keep it bounded

    if not voice_brief_text:
        voice_brief_text = (
            "Quiet morning. Nothing pinned for today — review the schedule "
            "and use the time to plan tomorrow."
        )

    finished = datetime.now(timezone.utc)
    duration_ms = max(0, int((finished - started).total_seconds() * 1000))

    cost = sonnet_cost(response.usage)
    cost_usd = cost.get("total") if isinstance(cost, dict) else None

    # Record to .cost_log.jsonl for daily-cap accounting.
    if cost_usd:
        record_cost(
            agent="hb-morning",
            model=model,
            cost_usd=cost_usd,
            note=f"voice_brief + action_items synth for {project_name}",
        )

    voice_brief = MorningVoiceBriefPayload(
        text=voice_brief_text,
        model=model,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
    )

    return voice_brief, action_items, response.usage
