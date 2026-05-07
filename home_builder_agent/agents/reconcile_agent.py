"""reconcile_agent.py — hb-reconcile CLI for the engine reconcile pass.

CLI:
  hb-reconcile                          One-shot scan + dispatch
  hb-reconcile --dry-run                Same, but rollback instead of commit
  hb-reconcile --json                   JSON output suitable for log piping
  hb-reconcile --since YYYY-MM-DD       Override the watermark for this run
  hb-reconcile --reset-watermark        Wipe the watermark file (next run scans everything)

The customer-side write loop:
  iOS shell  →  POST /v1/turtles/home-builder/actions  →  home_builder.user_action
                                                                  ↓
                                                        hb-reconcile (this)
                                                                  ↓
                                                        engine entity writes
                                                                  ↓
                                                        next /views/* fetch reflects it

Cost: $0/run. No Claude calls. Runs on Mac Mini in Phase A; moves to
Modal/Railway worker in Phase B with the same call site.

Future work:
  - launchd plist for periodic invocation (every 30s? 60s? polling vs.
    LISTEN/NOTIFY when reconcile latency starts to matter)
  - Phase B: DB-backed watermark / distributed lock when multi-instance
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

from home_builder_agent.core.heartbeat import beat_on_success
from home_builder_agent.observability.json_log import configure_json_logging
from home_builder_agent.scheduling.reconcile import (
    DispatchOutcome,
    WATERMARK_PATH,
    reconcile_pass,
)

logger = logging.getLogger(__name__)


def _parse_since(raw: str) -> datetime:
    """Parse --since arg. Accepts YYYY-MM-DD (assumed UTC midnight) or full ISO."""
    raw = raw.strip()
    if "T" not in raw:
        raw = raw + "T00:00:00+00:00"
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _print_pretty(report) -> None:
    counts = report.summary_counts()
    print(f"\n{'='*64}")
    print(f"RECONCILE PASS — {report.started_at.isoformat(timespec='seconds')}")
    print(f"{'='*64}")
    print(f"  Watermark before: {report.watermark_before or '(none — first run)'}")
    print(f"  Watermark after:  {report.watermark_after or '(unchanged)'}")
    print(f"  Actions scanned:  {report.actions_scanned}")
    print(f"")
    print(f"  Outcomes:")
    print(f"    ✅ applied:           {counts.get('applied', 0)}")
    print(f"    ⏭️  skipped:           {counts.get('skipped', 0)}")
    print(f"    ❓ unknown action:    {counts.get('unknown-action-type', 0)}")
    print(f"    🚨 error (will retry): {counts.get('error', 0)}")

    if not report.results:
        print(f"\n  (no actions to process)")
    else:
        print(f"\n  Per-action detail:")
        for r in report.results:
            icon = {
                "applied": "✅", "skipped": "⏭️ ",
                "unknown-action-type": "❓", "error": "🚨",
            }.get(r.outcome.value, "?")
            print(
                f"    {icon} [{r.outcome.value:12}] "
                f"{r.action_type:24} "
                f"target={r.target_entity_type}:{r.target_entity_id[:8]}…"
            )
            if r.notes:
                print(f"        {r.notes}")
    print(f"\n{'='*64}\n")


@beat_on_success("reconcile", stale_after_seconds=300, success_codes=(0, 3))
def main():
    # Stderr → JSON when launchd captures; default text when human-run in TTY.
    configure_json_logging("hb-reconcile")

    parser = argparse.ArgumentParser(
        description="Run one engine reconcile pass over home_builder.user_action."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Read + dispatch within a transaction, then rollback. No watermark update.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object instead of pretty terminal output. "
             "Useful for piping into log aggregators.",
    )
    parser.add_argument(
        "--since", default=None,
        help="Override the persisted watermark for this run. "
             "Accepts YYYY-MM-DD or full ISO timestamp.",
    )
    parser.add_argument(
        "--reset-watermark", action="store_true",
        help="Delete the watermark file before running (next run scans all rows).",
    )
    args = parser.parse_args()

    if args.reset_watermark:
        try:
            os.remove(WATERMARK_PATH)
            print(f"✓ Removed watermark file: {WATERMARK_PATH}")
        except FileNotFoundError:
            print(f"  (watermark file already absent: {WATERMARK_PATH})")

    since_override = None
    if args.since:
        try:
            since_override = _parse_since(args.since)
        except ValueError as e:
            print(f"❌ Invalid --since value: {e}")
            logger.error(
                "since_parse_failed",
                extra={"event": "since_parse_failed", "raw_since": args.since,
                       "error": str(e)},
            )
            sys.exit(1)

    correlation_id = uuid.uuid4().hex
    logger.info(
        "pass_starting",
        extra={
            "event": "pass_starting",
            "correlation_id": correlation_id,
            "dry_run": bool(args.dry_run),
            "since_override": since_override.isoformat() if since_override else None,
        },
    )

    try:
        report = reconcile_pass(
            since_override=since_override,
            dry_run=args.dry_run,
        )
    except Exception as e:
        logger.exception(
            "pass_failed",
            extra={
                "event": "pass_failed",
                "correlation_id": correlation_id,
                "exception_type": type(e).__name__,
            },
        )
        if args.json:
            print(json.dumps({
                "error": True,
                "type": type(e).__name__,
                "message": str(e),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2))
        else:
            print(f"\n🚨 reconcile_pass failed: {type(e).__name__}: {e}")
        sys.exit(2)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_pretty(report)

    # Exit codes:
    #   0 — pass completed (applied / skipped / unknown rows are all "ok")
    #   3 — pass completed but had handler errors that will be retried
    error_count = sum(
        1 for r in report.results if r.outcome == DispatchOutcome.ERROR
    )
    logger.info(
        "pass_complete",
        extra={
            "event": "pass_complete",
            "correlation_id": correlation_id,
            "rows_total": len(report.results),
            "applied": sum(1 for r in report.results if r.outcome == DispatchOutcome.APPLIED),
            "skipped": sum(1 for r in report.results if r.outcome == DispatchOutcome.SKIPPED),
            "unknown": sum(1 for r in report.results if r.outcome == DispatchOutcome.UNKNOWN),
            "errors": error_count,
            "dry_run": bool(args.dry_run),
        },
    )
    sys.exit(3 if error_count > 0 else 0)


if __name__ == "__main__":
    main()
