# Permit Office Watcher

> One-line summary: a per-builder background agent that polls the builder's county permit portal on a schedule, pushes a notification the moment a tracked permit's status changes, and writes the change as a `platform.event` for downstream reporting. Replaces "did the permit status change?" — the most universally-hated workflow in residential construction.

**Status:** Spec — Phase-2 priority #5 (ADR 2026-05-11 Phase-2 backlog).
**Phase:** Construction-turtle weeks 9-16 backlog.
**Owner:** CP.
**Last updated:** 2026-05-11.

**Why this first (of the top 5):** smallest scope. Backend-only — no iOS UI changes. Universal across every home builder (luxury custom, production, remodeling, commercial). Demoable as "your phone tells you when the permit status moves, without you ever visiting the county website."

## Overview

Every active project depends on a chain of permits: building permit, electrical, plumbing, mechanical, low-voltage, sometimes more per phase. Each lives on a different county website with a different UI, different update cadence, and zero notifications. Builders refresh those pages a half-dozen times a day across all their active jobs.

This watcher does the refreshing. Per builder, per county, per active permit, on a schedule. Status changes push to the builder's phone and emit a `platform.event` so the morning brief + dashboard can surface them.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  home_builder_agent/watchers/permit_watcher.py                   │
│                                                                  │
│   1. load_tracked_permits()                                      │
│        Returns rows of (project_id, permit_number, county,       │
│        permit_type, last_known_status, last_checked_at)          │
│                                                                  │
│   2. for each permit:                                            │
│        adapter = COUNTY_ADAPTERS[county]                         │
│        current = adapter.fetch_status(permit_number)             │
│        if current.status != last_known_status:                   │
│            update Postgres                                       │
│            emit_event("permit.status_changed", ...)              │
│            send_push(...) via APNs                               │
│                                                                  │
│   3. update last_checked_at on every permit (so we know          │
│      when the polling cycle ran cleanly)                         │
└──────────────────────────────────────────────────────────────────┘

         ▲                                  │
         │                                  ▼
   launchd plist                  iOS push notification:
   every 30 min                   "Pelican Pt building permit
                                   moved to ISSUED — tap to open"
```

Same shape as the existing `notification_triggers.py` and `inbox_watcher.py`. Same launchd plist pattern. Same `@beat_on_success` heartbeat. Same telemetry contract.

## Schema additions

### New table: `home_builder.permit`

```sql
CREATE TABLE IF NOT EXISTS home_builder.permit (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id          UUID NOT NULL REFERENCES home_builder.project(id) ON DELETE CASCADE,
    county              TEXT NOT NULL,           -- 'Baldwin County, AL', 'Davidson County, TN', etc.
    permit_number       TEXT NOT NULL,           -- the number the county assigns
    permit_type         TEXT NOT NULL,           -- 'building' | 'electrical' | 'plumbing' | 'mechanical' | 'low-voltage' | 'other'
    description         TEXT,                    -- free-text from the builder for context
    last_known_status   TEXT,                    -- per-county canonical state (see § Status normalization)
    last_known_at       TIMESTAMPTZ,             -- when the status was last observed (matches portal's update time, not our check time)
    last_checked_at     TIMESTAMPTZ,             -- when WE last polled (separate from last_known_at)
    portal_url          TEXT,                    -- deep link the user can tap to verify
    is_archived         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (county, permit_number)
);

CREATE INDEX IF NOT EXISTS permit_project_idx ON home_builder.permit (project_id);
CREATE INDEX IF NOT EXISTS permit_polling_idx
    ON home_builder.permit (last_checked_at NULLS FIRST)
    WHERE is_archived = false;
```

Migration file: `backend/migrations/015_permit.sql` (TODO — not yet written; this spec is design only).

### Event types added to `platform.event` taxonomy

- `permit.tracked_added` — builder adds a new permit to track (manual via CLI or future iOS surface)
- `permit.status_changed` — watcher detects status moved (the high-leverage event)
- `permit.poll_failed` — adapter raised; surfaces as a warning, doesn't crash the watcher

All emit with `tenant_id = <builder slug>` per the multi-tenant telemetry ADR.

## Per-county adapters

Each county portal is a unique snowflake. We model adapters as small Python classes implementing one interface:

```python
class CountyAdapter(Protocol):
    @property
    def county_name(self) -> str: ...

    def fetch_status(self, permit_number: str) -> PermitStatus: ...
        # PermitStatus dataclass: status (normalized), raw_status (county-specific), updated_at, portal_url

    def portal_deep_link(self, permit_number: str) -> str: ...
        # Deep link the iOS push notification opens
```

Phase 1: ship the adapter for **Baldwin County, AL** (Chad's county) only. Implementation strategies:

1. **Requests + BeautifulSoup** — if the portal is a static HTML form-post site. Cheap, fast, but breaks on portal redesigns.
2. **Playwright headless** — for JavaScript-rendered portals. Heavier but robust. Use only when (1) fails.
3. **Official API** — some counties (rare) have a permit lookup API. Best of all worlds when available.

Per ADR-style discipline: implement the simplest strategy that works, document failures, and only escalate to Playwright when (1) demonstrably can't read the page.

### Adapter rollout order (per phase-2 priority)

| County | Builder | Strategy guess | Priority |
|---|---|---|---|
| Baldwin County, AL | Palmetto Custom Homes (Chad) | requests+BS | 1 (ships with the feature) |
| Davidson County, TN | RPM Filter / future Nashville builders | TBD on first portal scrape | 2 (ships when first non-Baldwin builder signs) |
| Williamson County, TN | future builders | TBD | 3 |
| (others) | as needed | TBD | per-client basis |

### Status normalization

Each county uses different language. We normalize to a canonical set:

| Canonical | County-language examples |
|---|---|
| `applied` | "Application Received", "Submitted", "Pending" |
| `in_review` | "Under Review", "Plans Examination", "Routed" |
| `corrections_required` | "Corrections", "Plan Check Comments", "Resubmit" |
| `issued` | "Issued", "Approved", "Permit Granted" |
| `inspection_scheduled` | "Inspection Scheduled", "Inspector Assigned" |
| `inspection_passed` | "Passed", "Approved Inspection" |
| `inspection_failed` | "Failed Inspection", "Re-inspection Required" |
| `closed` | "Final", "Closed", "CO Issued" (if it's the CO permit) |
| `revoked` | "Revoked", "Cancelled" |
| `unknown` | anything we can't map — surfaces as a warning |

`platform.event` rows carry both the canonical status AND the raw county string so we don't lose information.

## Notification UX

When status changes:

1. **Push notification** — APNs push to the builder's iPhone:
   - Title: `<permit_type> permit at <project_name>`
   - Body: `Moved from <prior> → <current>. Tap to verify.`
   - Deep link: opens the county portal URL directly (we don't try to render permit detail in-app for v1)

2. **Morning brief inclusion** — the next morning's Today/morning brief mentions yesterday's permit changes naturally ("Whitfield's electrical permit issued yesterday; you can now schedule the rough-in.").

3. **Dashboard surface (Phase-2.5)** — a small "Permits" card on the project detail screen showing each tracked permit + last-known status. Deferred until iOS surface lands.

## Failure modes

| Failure | Handling |
|---|---|
| County portal down | Adapter raises → emit `permit.poll_failed` → log → continue with next permit. No notification to builder unless 3+ consecutive failures (then ONE notification, "couldn't reach the county portal — likely a county-side outage"). |
| Portal redesign breaks the scraper | Same as above. The 3-failure threshold gives us time to update the adapter before alerting. |
| Permit number invalid (deleted county-side) | Mark `is_archived = true` after 7 days of "permit not found" responses. Stop polling. |
| Tenant has no permits | Watcher exits clean; heartbeat fires; no work to do. |

## Tenant isolation

Per ADR-POS-001 (single-builder construction turtles): each builder's home-builder-agent instance polls only its own county/counties and writes only its own permits. The multi-tenant `platform.event` table receives events from all builders but each row carries the originating `tenant_id`.

When a new builder signs up:
1. Add their county to `COUNTY_ADAPTERS`.
2. Implement the adapter (Phase 1 = the long-pole step).
3. Builder adds their permits via `hb-permits add ...` or future iOS surface.

## Scheduling

| When | Why |
|---|---|
| Every 30 min during business hours (7 AM - 6 PM local) | Permit status changes happen during the day; off-hours polling is wasteful |
| Every 2 hours overnight | Catches late-night status updates (rare but happens) |
| On-demand via `hb-permits poll` | Manual fire for testing |

launchd plist: `com.chadhomes.permit-watcher.plist` (mirrors the existing `com.chadhomes.notification-triggers.plist`).

Heartbeat threshold: 2 hours (catches both the business-hours and overnight cadences with grace).

## CLI surface

```bash
hb-permits add --project "Whitfield" \
                --county "Baldwin County, AL" \
                --number "BC2026-12345" \
                --type building \
                --portal-url "https://baldwin.gov/permits/lookup/BC2026-12345"

hb-permits list                  # all tracked permits with current status
hb-permits list --project X     # filtered
hb-permits archive <permit-id>  # stop polling without deleting history
hb-permits poll                  # manual fire (testing)
hb-permits poll --permit ID      # poll a single permit
```

## Acceptance criteria

v1 done when:

1. Migration 015 applied to Supabase.
2. `home_builder.permit` table has at least one tracked permit (Whitfield's building permit).
3. Baldwin County adapter implemented + unit-tested against a saved HTML fixture of the portal.
4. launchd `com.chadhomes.permit-watcher.plist` installed + heartbeat firing.
5. Manually marking a Baldwin County permit as moved (e.g. by editing `last_known_status` in the DB) → next poll detects "change" → push notification fires → `permit.status_changed` event lands in `platform.event`.
6. `hb-permits` CLI surface works.

v2 (deferred):

- iOS surface (permits card on project detail).
- Additional county adapters per-builder.
- Smart polling cadence (more frequent during active phases, less when permit is closed).
- "Why is this taking so long?" — AI looks at average issue time for the county vs current age, surfaces if the permit is unusually slow.

## Cross-references

- ADR: `~/Projects/patton-os/data/decisions.md` — 2026-05-11 Phase-2 backlog locked, item #5.
- Existing watcher pattern: `home_builder_agent/scheduling/notification_triggers.py` + `com.chadhomes.notification-triggers.plist`.
- Heartbeat decorator: `home_builder_agent/core/heartbeat.py` — `@beat_on_success("permit-watcher", stale_after_seconds=7200)`.
- Telemetry: `~/Projects/patton-ai-ios/backend/migrations/009_platform_event.sql` (already deployed). Three new event types added to taxonomy (no schema change — `event_type` is a free-text column with format CHECK).
