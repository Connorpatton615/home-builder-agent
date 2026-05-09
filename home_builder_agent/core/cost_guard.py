"""cost_guard.py — daily spend cap + cost telemetry.

Production-hardening (2026-05-09): the system had no protection against
runaway API spend. A bug in iOS retry loop or an Opus tool-loop that
hits MAX_TOOL_LOOP_ITERATIONS could 100x daily cost silently. This
module records every paid Claude call to `.cost_log.jsonl` and refuses
expensive calls when configurable daily caps are exceeded.

Design:
  - Append-only JSONL persistence (one row per call)
  - Two caps: per-day total + per-day Opus-only (Opus is the expensive tier)
  - `check_budget(model_tier=...)` is called by callers BEFORE making
    expensive calls. Returns (allowed, reason). On allowed=False the
    caller decides whether to skip, fall back to a cheaper model, or
    surface an error to Chad.
  - Single-tenant in V1. `tenant_id` plumbing is in the schema for V2;
    when multi-tenant lands, caps become per-tenant.

Default caps: $5/day Opus, $10/day total. Configurable via env:
  HB_DAILY_OPUS_CAP_USD
  HB_DAILY_TOTAL_CAP_USD

Best-effort persistence: a write failure to `.cost_log.jsonl` does not
break the caller. We'd rather lose audit than break a morning brief.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


COST_LOG_PATH = Path(__file__).resolve().parent.parent.parent / ".cost_log.jsonl"

DEFAULT_DAILY_OPUS_CAP_USD = float(os.environ.get("HB_DAILY_OPUS_CAP_USD", "5.00"))
DEFAULT_DAILY_TOTAL_CAP_USD = float(os.environ.get("HB_DAILY_TOTAL_CAP_USD", "10.00"))


def record_cost(
    *,
    agent: str,
    model: str,
    cost_usd: float,
    project_id: str | None = None,
    tenant_id: str | None = None,
    note: str | None = None,
) -> None:
    """Append one cost entry to `.cost_log.jsonl`. Best-effort.

    Callers wrap this around any paid Claude call:

        cost = sonnet_cost(response.usage)["total"]
        record_cost(agent="hb-brief", model=WRITER_MODEL, cost_usd=cost,
                    project_id=project_id)

    Zero-cost calls are skipped (no useful audit signal).
    Errors writing to the log file are swallowed (logged at WARNING).
    """
    if not cost_usd or cost_usd <= 0:
        return
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent": agent,
        "model": model,
        "cost_usd": round(float(cost_usd), 6),
        "project_id": project_id,
        "tenant_id": tenant_id,
        "note": note,
    }
    try:
        with open(COST_LOG_PATH, "a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception as e:
        logger.warning(
            "cost_log_write_failed",
            extra={
                "event": "cost_log_write_failed",
                "exception_type": type(e).__name__,
                "agent": agent,
                "cost_usd": cost_usd,
            },
        )


def _read_today_costs(
    *,
    tenant_id: str | None = None,
    today: date | None = None,
) -> tuple[float, float]:
    """Return (today_total_usd, today_opus_usd) by streaming
    `.cost_log.jsonl`. Tenant filter applied if provided. Returns
    (0.0, 0.0) if the file is missing or unreadable."""
    today = today or date.today()
    today_iso = today.isoformat()
    total = 0.0
    opus = 0.0
    try:
        with open(COST_LOG_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = r.get("ts", "")
                if not ts.startswith(today_iso):
                    continue
                if tenant_id is not None and r.get("tenant_id") != tenant_id:
                    continue
                try:
                    cost = float(r.get("cost_usd", 0) or 0)
                except (TypeError, ValueError):
                    continue
                total += cost
                model = (r.get("model") or "").lower()
                if "opus" in model:
                    opus += cost
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(
            "cost_log_read_failed",
            extra={
                "event": "cost_log_read_failed",
                "exception_type": type(e).__name__,
            },
        )
    return total, opus


def check_budget(
    model_tier: str = "any",
    *,
    tenant_id: str | None = None,
    opus_cap_usd: float | None = None,
    total_cap_usd: float | None = None,
) -> tuple[bool, str]:
    """Check whether we're under the daily spend cap.

    Returns (allowed, reason). On allowed=False, `reason` is a
    human-readable string callers can surface to the user (and to
    structured logs).

    `model_tier`:
      "opus" — checks both Opus and total caps
      "sonnet" / "haiku" / "any" — checks only the total cap

    Override caps via env vars HB_DAILY_OPUS_CAP_USD / HB_DAILY_TOTAL_CAP_USD
    or via kwargs (mostly for tests).
    """
    opus_cap = opus_cap_usd if opus_cap_usd is not None else DEFAULT_DAILY_OPUS_CAP_USD
    total_cap = total_cap_usd if total_cap_usd is not None else DEFAULT_DAILY_TOTAL_CAP_USD

    total, opus = _read_today_costs(tenant_id=tenant_id)

    if total >= total_cap:
        return False, (
            f"Daily total cap reached: ${total:.4f} of ${total_cap:.2f} "
            "across all models. Set HB_DAILY_TOTAL_CAP_USD to raise."
        )
    if model_tier.lower() == "opus" and opus >= opus_cap:
        return False, (
            f"Daily Opus cap reached: ${opus:.4f} of ${opus_cap:.2f}. "
            "Falling back to ask_chad / dispatch_action only "
            "(no Opus persona-master turn). Set HB_DAILY_OPUS_CAP_USD to raise."
        )
    return True, ""


def today_summary(*, tenant_id: str | None = None) -> dict:
    """Return today's cost-log summary as a dict for telemetry / status CLIs."""
    total, opus = _read_today_costs(tenant_id=tenant_id)
    return {
        "date": date.today().isoformat(),
        "total_usd": round(total, 4),
        "opus_usd": round(opus, 4),
        "opus_cap_usd": DEFAULT_DAILY_OPUS_CAP_USD,
        "total_cap_usd": DEFAULT_DAILY_TOTAL_CAP_USD,
        "opus_pct_of_cap": round(100.0 * opus / DEFAULT_DAILY_OPUS_CAP_USD, 1) if DEFAULT_DAILY_OPUS_CAP_USD else 0.0,
        "total_pct_of_cap": round(100.0 * total / DEFAULT_DAILY_TOTAL_CAP_USD, 1) if DEFAULT_DAILY_TOTAL_CAP_USD else 0.0,
    }
