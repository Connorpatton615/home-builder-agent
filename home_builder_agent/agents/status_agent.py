"""status_agent.py — hb-status: one-shot health check for the system.

A single command that answers "is everything OK right now?" — pulling
together signals from all the places they currently live:

  • launchd jobs   — which are loaded, last exit status
  • heartbeats     — `.heartbeats/*.json` per-job freshness
  • cost ledger    — today's spend + cap utilization (.cost_log.jsonl)
  • engine queues  — pending draft_actions + recent unack'd events
  • cache state    — morning view payload age per project
  • errors         — recent stderr.log lines per launchd job

CLI:
  hb-status                # pretty terminal output (default)
  hb-status --json         # machine-readable JSON for ops dashboards

Cost: $0/run (no Claude calls — pure filesystem + Postgres reads).

Use cases:
  • Connor's morning sanity-check before his coffee
  • A future ops dashboard polls this every minute
  • Chad asks "are you working today?" → terminal answer in <2 sec
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HEARTBEATS_DIR = REPO_ROOT / ".heartbeats"
MORNING_CACHE_DIR = REPO_ROOT / ".morning_cache"
COST_LOG_PATH = REPO_ROOT / ".cost_log.jsonl"

# Per-job watchdog thresholds (seconds). Mirrors the conventions in
# CLAUDE.md "Active background processes" — 5x cadence + grace.
JOB_THRESHOLDS_SEC = {
    "dashboard-watcher": 300,        # cadence 60s
    "reconcile":         300,        # cadence 60s
    "inbox-watcher":     1500,       # cadence 5m
    "morning-brief":     90000,      # cadence 24h
    "morning-view":      90000,      # cadence 24h
    "client-update":     691200,     # cadence weekly
    "notification-triggers": 90000,  # cadence 24h
}


# ---------------------------------------------------------------------------
# Signal collectors
# ---------------------------------------------------------------------------


def _read_launchctl() -> dict:
    """Return {label: {pid, last_exit_status}} for chadhomes-* jobs."""
    out = {}
    try:
        result = subprocess.check_output(["launchctl", "list"], text=True, timeout=5)
    except Exception as e:
        return {"_error": f"launchctl failed: {type(e).__name__}: {e}"}
    for line in result.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pid_str, exit_str, label = parts[0], parts[1], parts[2]
        if "chadhomes" not in label:
            continue
        out[label] = {
            "pid": None if pid_str == "-" else int(pid_str),
            "last_exit_status": int(exit_str) if exit_str.lstrip("-").isdigit() else exit_str,
        }
    return out


def _read_heartbeats() -> list[dict]:
    """Per-job heartbeat freshness + threshold breach detection."""
    rows = []
    if not HEARTBEATS_DIR.exists():
        return rows
    now = datetime.now(timezone.utc)
    for f in sorted(HEARTBEATS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception as e:
            rows.append({
                "job": f.stem,
                "error": f"read failed: {type(e).__name__}: {e}",
            })
            continue
        ts_str = data.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_sec = max(0, int((now - ts).total_seconds()))
        except Exception:
            age_sec = -1
        threshold = data.get("stale_after_seconds") or JOB_THRESHOLDS_SEC.get(data.get("job", ""), 0)
        is_stale = age_sec > threshold if (age_sec >= 0 and threshold > 0) else False
        rows.append({
            "job": data.get("job", f.stem),
            "ts": ts_str,
            "age_sec": age_sec,
            "threshold_sec": threshold,
            "is_stale": is_stale,
            "status": data.get("status", "?"),
        })
    return rows


def _read_today_costs() -> dict:
    """Today's total + Opus spend from .cost_log.jsonl."""
    today_iso = date.today().isoformat()
    total = 0.0
    opus = 0.0
    n_calls = 0
    by_agent: dict[str, float] = {}
    if not COST_LOG_PATH.exists():
        return {
            "date": today_iso,
            "total_usd": 0.0,
            "opus_usd": 0.0,
            "n_calls": 0,
            "by_agent": {},
            "log_path": str(COST_LOG_PATH),
            "log_present": False,
        }
    try:
        with open(COST_LOG_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not r.get("ts", "").startswith(today_iso):
                    continue
                cost = float(r.get("cost_usd", 0) or 0)
                total += cost
                model = (r.get("model") or "").lower()
                if "opus" in model:
                    opus += cost
                n_calls += 1
                agent = r.get("agent") or "unknown"
                by_agent[agent] = by_agent.get(agent, 0.0) + cost
    except Exception as e:
        return {"_error": f"cost log read failed: {type(e).__name__}: {e}"}
    # Caps come from cost_guard
    try:
        from home_builder_agent.core.cost_guard import (
            DEFAULT_DAILY_OPUS_CAP_USD,
            DEFAULT_DAILY_TOTAL_CAP_USD,
        )
    except Exception:
        DEFAULT_DAILY_OPUS_CAP_USD = 5.0
        DEFAULT_DAILY_TOTAL_CAP_USD = 10.0
    return {
        "date": today_iso,
        "total_usd": round(total, 4),
        "opus_usd": round(opus, 4),
        "opus_cap_usd": DEFAULT_DAILY_OPUS_CAP_USD,
        "total_cap_usd": DEFAULT_DAILY_TOTAL_CAP_USD,
        "opus_pct": round(100.0 * opus / DEFAULT_DAILY_OPUS_CAP_USD, 1) if DEFAULT_DAILY_OPUS_CAP_USD else 0.0,
        "total_pct": round(100.0 * total / DEFAULT_DAILY_TOTAL_CAP_USD, 1) if DEFAULT_DAILY_TOTAL_CAP_USD else 0.0,
        "n_calls": n_calls,
        "by_agent": {k: round(v, 4) for k, v in sorted(by_agent.items(), key=lambda x: -x[1])},
        "log_path": str(COST_LOG_PATH),
        "log_present": True,
    }


def _read_morning_caches() -> list[dict]:
    """Per-project morning view cache age."""
    rows = []
    if not MORNING_CACHE_DIR.exists():
        return rows
    now = datetime.now()
    for f in sorted(MORNING_CACHE_DIR.glob("*.json")):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            age_sec = max(0, int((now - mtime).total_seconds()))
            size_b = f.stat().st_size
        except Exception:
            continue
        rows.append({
            "project_id": f.stem,
            "age_sec": age_sec,
            "age_h": round(age_sec / 3600, 1),
            "size_bytes": size_b,
            "is_stale": age_sec > 86400,  # >24h
        })
    return rows


def _read_engine_queues() -> dict:
    """Postgres queue depths — pending drafts + open events."""
    try:
        from home_builder_agent.scheduling.store_postgres import _query_one
    except Exception as e:
        return {"_error": f"adapter import failed: {type(e).__name__}: {e}"}

    out = {}
    queries = {
        "pending_drafts": (
            "SELECT count(*) AS n FROM home_builder.draft_action WHERE status = 'pending'"
        ),
        "open_events": (
            "SELECT count(*) AS n FROM home_builder.event WHERE status = 'open'"
        ),
        "open_events_critical": (
            "SELECT count(*) AS n FROM home_builder.event "
            "WHERE status = 'open' AND severity IN ('critical','blocking')"
        ),
        "unprocessed_user_actions": (
            "SELECT count(*) AS n FROM home_builder.user_action ua "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM home_builder.engine_activity ea "
            "  WHERE ea.affected_entity_id::text = ua.id::text"
            ")"
        ),
        "active_projects": (
            "SELECT count(*) AS n FROM home_builder.project WHERE status != 'archived'"
        ),
    }
    for key, sql in queries.items():
        try:
            row = _query_one(sql)
            out[key] = int(row["n"]) if row else 0
        except Exception as e:
            msg = str(e).lower()
            # Migrations-not-applied case shouldn't block the rest
            if "does not exist" in msg:
                out[key] = "table-missing"
            else:
                out[key] = f"err:{type(e).__name__}"
    return out


def _recent_errors(per_job_lines: int = 20) -> dict:
    """Tail each job's stderr.log for ERROR / WARNING / Traceback entries."""
    out = {}
    log_dir = Path("/tmp")
    for f in sorted(log_dir.glob("*.stderr.log")):
        # Only home-builder jobs
        name = f.stem.replace(".stderr", "")
        if name not in ("dashboard-watcher", "reconcile", "inbox-watcher",
                        "morning-brief", "morning-view", "client-update",
                        "notification-triggers", "watchdog"):
            continue
        try:
            with open(f) as fh:
                lines = fh.readlines()[-per_job_lines:]
        except Exception:
            continue
        errors = [
            line.strip()[:200] for line in lines
            if any(sig in line for sig in ("ERROR", "Traceback", '"level": "ERROR"', '"level": "WARNING"'))
        ]
        if errors:
            out[name] = errors[-3:]  # last 3 only
    return out


# ---------------------------------------------------------------------------
# Pretty rendering
# ---------------------------------------------------------------------------


def _fmt_age(sec: int) -> str:
    if sec < 0:
        return "?"
    if sec < 60:
        return f"{sec}s ago"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m ago"
    return f"{sec // 86400}d{(sec % 86400) // 3600:02d}h ago"


def _print_pretty(snapshot: dict) -> None:
    line = "═" * 72
    print()
    print(line)
    print(f" hb-status — {datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(line)

    # launchd
    launchd = snapshot.get("launchd", {})
    if "_error" in launchd:
        print(f"\n  ⚠️  launchd: {launchd['_error']}")
    else:
        print(f"\n  📋 launchd jobs ({len(launchd)}):")
        for label, info in sorted(launchd.items()):
            short = label.replace("com.chadhomes.", "")
            ok = info.get("last_exit_status", -1)
            mark = "✅" if ok == 0 else f"⚠️ exit={ok}"
            pid = info.get("pid")
            pid_str = f"PID {pid}" if pid else "(idle)"
            print(f"     {mark}  {short:24} {pid_str}")

    # heartbeats
    print(f"\n  💓 Heartbeats:")
    for hb in snapshot.get("heartbeats", []):
        if "error" in hb:
            print(f"     ⚠️  {hb['job']:24} {hb['error']}")
            continue
        mark = "🚨" if hb["is_stale"] else "✅"
        print(
            f"     {mark}  {hb['job']:24} "
            f"{_fmt_age(hb['age_sec']):>14}  "
            f"(threshold {hb['threshold_sec'] // 60}m)"
        )

    # cost
    cost = snapshot.get("cost", {})
    print(f"\n  💸 Today's spend ({cost.get('date','?')})")
    if not cost.get("log_present"):
        print(f"     ⚠️  Cost log not present at {cost.get('log_path','?')}")
    elif "_error" in cost:
        print(f"     ⚠️  {cost['_error']}")
    else:
        opus_bar = _bar(cost["opus_pct"], 20)
        total_bar = _bar(cost["total_pct"], 20)
        print(f"     Opus:  ${cost['opus_usd']:>7.4f} / ${cost['opus_cap_usd']:>5.2f} {opus_bar} {cost['opus_pct']}%")
        print(f"     Total: ${cost['total_usd']:>7.4f} / ${cost['total_cap_usd']:>5.2f} {total_bar} {cost['total_pct']}%")
        print(f"     Calls: {cost['n_calls']}")
        if cost.get("by_agent"):
            for agent, amount in list(cost["by_agent"].items())[:5]:
                print(f"       {agent:<24} ${amount:.4f}")

    # caches
    caches = snapshot.get("morning_caches", [])
    if caches:
        print(f"\n  📦 Morning view caches ({len(caches)}):")
        for c in caches:
            mark = "🚨" if c["is_stale"] else "✅"
            print(
                f"     {mark}  {c['project_id'][:8]}…  "
                f"age {c['age_h']}h, {c['size_bytes']} bytes"
            )

    # queues
    q = snapshot.get("engine_queues", {})
    if q:
        print(f"\n  🗄  Engine queues:")
        if "_error" in q:
            print(f"     ⚠️  {q['_error']}")
        else:
            print(f"     active_projects:           {q.get('active_projects', '?')}")
            print(f"     pending_drafts:            {q.get('pending_drafts', '?')}")
            print(f"     open_events (any):         {q.get('open_events', '?')}")
            crit = q.get("open_events_critical", 0)
            mark = "🚨" if isinstance(crit, int) and crit > 0 else "  "
            print(f"     open_events (critical):  {mark} {crit}")
            print(f"     unprocessed_user_actions:  {q.get('unprocessed_user_actions', '?')}")

    # errors
    errs = snapshot.get("recent_errors", {})
    if errs:
        print(f"\n  🔥 Recent errors / warnings (last 3 per job):")
        for job, lines in errs.items():
            print(f"     [{job}]")
            for ln in lines:
                print(f"       {ln}")
    else:
        print(f"\n  ✨ No recent errors / warnings across launchd jobs.")

    print()
    print(line)
    print()


def _bar(pct: float, width: int = 20) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int((pct / 100.0) * width)
    return "[" + "█" * filled + "·" * (width - filled) + "]"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def collect_snapshot() -> dict:
    """Build the full status snapshot. Pure-Python; no Claude calls."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "launchd": _read_launchctl(),
        "heartbeats": _read_heartbeats(),
        "cost": _read_today_costs(),
        "morning_caches": _read_morning_caches(),
        "engine_queues": _read_engine_queues(),
        "recent_errors": _recent_errors(),
    }


def main():
    parser = argparse.ArgumentParser(
        description="One-shot health check for the home-builder system."
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of pretty terminal output.",
    )
    args = parser.parse_args()

    snapshot = collect_snapshot()

    if args.json:
        print(json.dumps(snapshot, indent=2, default=str))
        return

    _print_pretty(snapshot)

    # Exit code: 0 if everything healthy, 1 if any stale heartbeat, 2 if
    # critical events open. Useful for `hb-status && echo OK`.
    stale = any(hb.get("is_stale") for hb in snapshot.get("heartbeats", []))
    crit = snapshot.get("engine_queues", {}).get("open_events_critical")
    if isinstance(crit, int) and crit > 0:
        sys.exit(2)
    if stale:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
