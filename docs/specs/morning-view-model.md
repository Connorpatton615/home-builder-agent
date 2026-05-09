# Morning view-model — Chad's coffee-cup landing

> One-line summary: the home-builder vertical's contribution to the
> "morning coffee work station" surface. A curated digest projected from
> daily activity + notification feed + voice-of-Chad synthesis + a new
> judgment-queue entity, returned as a single JSON payload from
> `/v1/turtles/home-builder/views/morning/{project_id}`.

**Status:** Approved (vertical contract) 2026-05-09 — implementation can proceed. One field (cold-launch stale-cache behavior) flagged for platform-thread finalization; vertical recommendation is baked in.
**Author:** Claude (drafted 2026-05-09).
**Scope:** Vertical content + contract only. The renderer (native Mac, mobile, web shell, future surfaces) and the auth/transport layer are platform concerns, defined elsewhere.
**Cross-refs:** [`canonical-data-model.md`](canonical-data-model.md) (entities, View-model contract), [`scheduling-engine.md`](scheduling-engine.md) (engine state ownership), [`chad-agent.md`](chad-agent.md) (voice synthesis layer), [`view_models_schema.json`](view_models_schema.json) (sibling view-model wire formats).

---

## Why this view exists

The five existing view-models (`master`, `daily`, `weekly`, `monthly`, `checklist-gates`, `notification-feed`) are **operational** — they answer "what's on the schedule?" and "what's the queue?". The morning view is **digestive** — it answers a different question:

> Chad sits down with coffee at 7 AM. He has 20 minutes before his first call. What does the screen show, in what order, so the next 90 minutes go right?

The daily view is too granular for that moment (it's a feed of today's individual site-floor items). The notification feed is too undifferentiated (events arrive in severity order, not in "Chad's morning attention" order). Chad doesn't want to triage two surfaces and synthesize them himself before he's awake. He wants the synthesis pre-built.

This view is the synthesis. It is the surface the platform's morning landing renders on cold-launch.

**It is not** a new entity. It is a derived projection over existing entities + one new entity (DraftAction, see § Open dependencies). Like every other view-model, it is computed on demand and never the source of truth for what it contains.

---

## What it projects from

| Source | Used for | Existing? |
|---|---|---|
| `home_builder.event` (open + acknowledged, last ~14h, severity ≥ warning) | Overnight events section | Yes |
| Notification-feed view-model (subset filter) | Same — read via existing projection rather than re-projecting Event | Yes |
| Daily view-model (subset filter, today only, kinds: `phase-active`, `delivery`, `inspection`) | Today on site section | Yes |
| Master view-model drop-dead dates (today + next 14d, urgency-banded) | Today's drop-deads section | Yes |
| NOAA / Open-Meteo forecast (existing `agents/morning_brief.py:fetch_weather`) | Weather section | Yes — lift into engine module |
| Weather-risk computation (existing `morning_brief.py:weather_risk_check`) | Weather risk pinned to top | Yes — lift into engine module |
| `home_builder.draft_action` (new) | Judgment queue section | **No — see § Open dependencies** |
| `chad_voice("narrator")` + `chad_context` loaders + this payload as input | `voice_brief` and `action_items` | Yes |

The morning view does not introduce any new source data. It re-uses entities + view-models that already exist, plus one new entity (DraftAction) that the move-B commit lands.

---

## Exposed shape

```jsonc
{
  "view_type": "morning",
  "project_id": "uuid",
  "project_name": "Whitfield Residence",
  "generated_at": "2026-05-09T12:30:00Z",            // server-side compute timestamp
  "as_of_local_date": "2026-05-09",                  // Chad's local date (project tz)
  "tz": "America/Chicago",                           // IANA tz from user_profile.working_hours

  // 1. Voice brief — hb-chad narrator synthesis, ~3-5 sentences.
  //    Composed in same Anthropic call as action_items so they're consistent.
  "voice_brief": {
    "text": "Rain Wednesday and Thursday is going to push trim back two days; Mason hasn't confirmed Friday's cabinet install yet. Two windows ordered last week shipped — those are off your plate. Inspection passes the framing, you're cleared to start drywall Monday.",
    "model": "claude-sonnet-4.5",
    "cost_usd": 0.018,
    "duration_ms": 2400
  },

  // 2. Weather — flagged at top if any risk_phases present.
  "weather": {
    "summary_today": "Sunny, high 78°F, light south wind",
    "summary_tomorrow": "Rain likely, high 71°F, 80% chance of thunderstorms after 2pm",
    "risk_phases": [
      {
        "phase_id": "uuid",
        "phase_name": "Trim",
        "risk_kind": "rain",                          // rain | wind | extreme-cold | extreme-heat
        "detail": "Wed-Thu rain conflicts with exterior trim install",
        "severity": "warning"                         // warning | critical
      }
    ]
  },

  // 3. Judgment queue — drafts pending Chad's review/edit/discard.
  //    This is the highest-leverage real estate on the morning view.
  //    Sourced from home_builder.draft_action (entity #18, added in move B).
  "judgment_queue": {
    "count": 4,
    "items": [
      {
        "draft_action_id": "uuid",
        "kind": "gmail-reply-draft",                  // see § DraftKind below
        "subject_line": "Re: Cabinet install Friday — confirm?",
        "from_or_to": "Mason Cabinets <orders@masoncabs.com>",
        "summary": "Vendor asks if Friday 8am install still works. Drafted 'yes, gate code 4421' in your voice.",
        "created_at": "2026-05-09T05:42:00Z",         // when the agent drafted it
        "originating_agent": "hb-supplier-email",     // which agent produced the draft
        "approve_action": "draft_action.approve",     // UserAction emit target
        "edit_action": "draft_action.edit",
        "discard_action": "draft_action.discard",
        "click_action": "draft://uuid"                // deep-link to inline edit surface
      }
    ]
  },

  // 4. Today on site — phase work, deliveries, inspections expected today.
  //    Subset of daily view filtered to (project_id, today, kinds in {phase-active, delivery, inspection}).
  //    Each item carries an `urgency_band` so the renderer can mute calm
  //    items and emphasize urgent ones; per-kind rules in § urgency_band semantics.
  "today_on_site": {
    "items": [
      {
        "kind": "phase-active",                       // mirrors DailyItemKind
        "phase_id": "uuid",
        "phase_name": "Framing",
        "day_n": 12,
        "of_total": 14,
        "urgency_band": "watch",                      // calm | watch | urgent
        "urgency_reason": "1 day behind plan",        // optional, plain-English chip
        "tap_action": "phase://uuid"
      },
      {
        "kind": "delivery",
        "material_category": "windows",
        "urgency_band": "urgent",
        "urgency_reason": "Install date is today and truck hasn't arrived",
        "tap_action": "delivery://uuid"
      },
      {
        "kind": "inspection",
        "phase_name": "Framing",
        "urgency_band": "calm",
        "urgency_reason": null,
        "tap_action": "inspection://uuid"
      }
    ]
  },

  // 5. Today's drop-deads — selection deadlines hitting today or imminent.
  //    Subset of master view drop-deads, urgency-banded.
  "todays_drop_deads": {
    "items": [
      {
        "material_category": "cabinets",
        "install_phase_name": "Trim",
        "install_date": "2026-06-05",
        "drop_dead_date": "2026-05-09",
        "lead_time_days": 27,
        "urgency_band": "ORDER NOW",                  // OVERDUE | ORDER NOW | THIS WEEK | UPCOMING
        "tap_action": "drop-dead://cabinets"
      }
    ]
  },

  // 6. Overnight events — Events fired since previous evening, severity ≥ warning.
  //    Subset of notification-feed view, filtered to (created_at > now - 14h, severity in {warning, critical, blocking}).
  "overnight_events": {
    "items": [
      {
        "event_id": "uuid",
        "notification_id": "uuid",
        "type": "supplier-email-detected-eta-change", // mirrors notification-feed types
        "severity": "warning",
        "summary": "Window vendor pushed ETA for kitchen window from May 22 → May 29 (7d slip)",
        "age_seconds": 28800,                         // 8h
        "acknowledge_action": "event.acknowledge",
        "click_action": "event://uuid"
      }
    ]
  },

  // 7. Action items — the concrete things Chad should do today, composed by
  //    hb-chad in the same call as voice_brief. Plain text, imperative. Length
  //    is 1–5, soft-picked by hb-chad based on the day's actual surface area.
  //    Quiet days produce fewer items; busy days more. Never empty (synthesis
  //    must produce at least one, even if it's "review tomorrow's plans").
  "action_items": [
    "Order cabinets today — drop-dead is today, you're already at the wire",
    "Confirm Friday cabinet install with Mason (draft is ready, just approve)",
    "Pull tomorrow's exterior trim crew off-site — rain is going to soak it"
  ]
}
```

**Field naming follows existing view-model conventions** (snake_case, `_id` suffix on entity refs, lowercase-hyphenated enums, `severity ∈ {info, warning, critical, blocking}` per `EventSeverity`). The `view_type` discriminator is `"morning"`.

---

## Section ordering — what the renderer should display, top to bottom

The view-model says *what*, the renderer says *how*. But because the morning surface is curatorial, the **order** is part of the contract — the engine signals priority through field ordering and the renderer is expected to honor it.

1. **Weather risk** (only if `weather.risk_phases` non-empty) — pinned to top when phases are at risk. Skipped section header otherwise.
2. **Voice brief** — the orienting paragraph. Three lines max in compact density; full text in expanded.
3. **Judgment queue** — highest-leverage section. Inline approve / edit / discard affordances per item. Empty state is explicit ("Inbox is clear.") so Chad sees confirmation, not a missing section.
4. **Today on site** — phase work, deliveries, inspections expected today.
5. **Today's drop-deads** — only items in `OVERDUE` or `ORDER NOW` band on the morning surface. `THIS WEEK` and `UPCOMING` belong on the daily/weekly surfaces, not here.
6. **Overnight events** — events with severity ≥ warning fired in the last ~14h. Already-acknowledged events are excluded.
7. **Action items** — the hb-chad-composed 1–5 imperative items, length picked by the day.

When a section is empty:
- Weather risk → omit the section entirely (don't render an empty header)
- Voice brief → never empty (always composes; if compose fails, render the fallback summary the engine produces)
- Judgment queue → render with explicit empty state ("Inbox is clear")
- Today on site → render with empty state ("Quiet day on site")
- Today's drop-deads → render with empty state ("Nothing imminent")
- Overnight events → omit the section
- Action items → never empty (always composes; fallback as above)

The rationale for the empty-state asymmetry: judgment queue / today on site / drop-deads benefit from an explicit "all clear" signal because Chad's eye expects them; weather and overnight events are exception-pattern surfaces where absence is the norm and a "no events overnight" header would be visual debt.

---

## Source mapping — per field, where the data comes from

| Payload field | Engine source | Compute cost | Notes |
|---|---|---|---|
| `voice_brief.text` | One Sonnet call via `chad_voice("narrator")` with the rest of the payload as the context bundle | ~$0.02 | Same call composes `action_items`; one round-trip |
| `weather.summary_today/tomorrow` | NOAA `gridpoints/forecast` (cached URL per gridpoint), Open-Meteo fallback | $0 | Lift `agents/morning_brief.py:fetch_weather` into `scheduling/weather.py` |
| `weather.risk_phases` | `agents/morning_brief.py:weather_risk_check(phases, weather, today)` against the morning view's project's active phases | $0 | Lift into engine |
| `judgment_queue.items` | `SELECT * FROM home_builder.draft_action WHERE project_id = $1 AND status = 'pending' ORDER BY created_at DESC LIMIT 50` | $0 | **Source entity does not exist yet — see § Open dependencies** |
| `today_on_site.items` | Daily view-model projection, filtered by `project_id` and `kind ∈ {phase-active, delivery, inspection}` | $0 | Re-project daily; do not re-implement |
| `todays_drop_deads.items` | Master view-model drop-dead overlay, filtered by `urgency_band ∈ {OVERDUE, ORDER NOW}` | $0 | Re-project master |
| `overnight_events.items` | Notification-feed projection, filtered by `created_at > now - interval '14 hours'` and `severity IN ('warning','critical','blocking')` | $0 | Re-project notification-feed |
| `action_items` | Composed in the same Sonnet call as `voice_brief` (system prompt requests both as JSON) | (counted in voice_brief cost) | One call, two deliverables |

**Total marginal cost per morning view fetch: ~$0.02** (one Sonnet call). All other fields are projection over engine state already loaded for sibling view-models.

**Caching:** payload is cached for 5 min keyed on `(project_id, as_of_local_date)`. Pull-to-refresh forces recompute. The Sonnet-call portion (voice_brief + action_items) is cached for 30 min within the same `(project_id, as_of_local_date)` because the inputs change much more slowly than the projection layer — the renderer can swap in fresh projection data without re-billing the synthesis call when nothing material has changed.

**Cold-launch with stale cache (>24h old).** *Vertical recommendation; final decision is platform-owned.* Render the stale payload immediately and refresh in background, with a freshness banner ("Last refreshed N hours ago — refreshing…") at the top of the surface. Rationale: the morning surface's UX promise is *the app always opens fast and always has something to read with your coffee.* Blocking on a fresh fetch when the network is gone is the worst possible 7 AM moment in a subscription product — it's the screen that costs you the customer. Stale data is honest if the banner is honest. The 24h freshness threshold matches the cadence of the surface itself (it's literally a *morning* view); anything fresher than that is "today's brief" by definition. Renderer surfaces (native Mac, iOS, web shell) are expected to honor this contract; the platform's offline-tolerance spec finalizes whether that's a global rule or per-view-model.

---

## `urgency_band` semantics

`today_on_site.items[].urgency_band` and `todays_drop_deads.items[].urgency_band` are both three-value enums (`calm | watch | urgent`), but the per-kind rules differ. The engine computes them; the renderer mutes/emphasizes accordingly.

| Kind | calm | watch | urgent |
|---|---|---|---|
| `phase-active` | On plan or ahead | 1–2 days behind plan | 3+ days behind, or behind AND on the project's longest path |
| `delivery` | Within scheduled window | Past expected window by 1 day, or install date >2 days out and not yet shipped | Install date is today/tomorrow and material is not on site, or >2 days late |
| `inspection` | Scheduled, permit healthy | Permit expiry <30 days and this inspection is required for a future phase | Permit expiry <14 days, or last attempt failed and reinspect not yet scheduled |
| `drop-dead` (in `todays_drop_deads`) | n/a (only OVERDUE/ORDER NOW reach this view) | n/a | All items here are urgent by definition |

Critical-path-aware urgency (the "longest path" qualifier on phase-active/urgent) is best-effort in V1 — until the engine ships CPM (per `canonical-data-model.md § Critical path support`), V1 falls back to "behind plan by 3+ days = urgent" without the longest-path test. V2 tightens this once `float_time_days` lands.

`urgency_reason` is an optional plain-English chip the renderer surfaces in expanded density — "1 day behind plan" / "Permit expires in 22 days" / "Installer waiting at gate" — so Chad sees *why* an item is yellow or red without drilling in. Null for calm items.

---

## DraftKind — the new vocabulary the judgment queue introduces

The `judgment_queue.items[].kind` discriminator is new. v1 vocabulary:

| `kind` | What it represents | Originating agent |
|---|---|---|
| `gmail-reply-draft` | Drafted reply to an inbound email (homeowner, sub, vendor, inspector) | `gmail_followup`, `supplier_email_watcher` |
| `change-order-approval` | Drafted change-order doc + email to homeowner pending Chad's send | `change_order_agent` |
| `lien-waiver-followup` | Drafted nudge to a sub/vendor for a missing waiver on a >$500 payment | `lien_waiver_agent` (new draft path) |
| `client-update-email` | Drafted weekly homeowner update pending Chad's send | `client_update_agent` |
| `vendor-eta-confirmation` | Drafted vendor follow-up after a supplier-email ETA detection | `supplier_email_watcher` |
| `inspection-scheduling-request` | Drafted email to building dept requesting next inspection slot | `inspection_tracker` (future) |

Vocabulary is open-enum at the DB layer; the renderer matches on `kind` to pick the right inline-edit affordance (text body for email kinds, structured form for inspection-scheduling-request, etc.). Adding a new kind = update this table + the renderer's switch + the originating agent. No DB migration.

---

## Open dependencies (blocking real implementation)

This spec defines the contract; three things still have to land before the engine can return a non-empty `judgment_queue`:

1. **`home_builder.draft_action` entity** (canonical-data-model.md entity #18, migration 007). Schema sketch:
   ```sql
   CREATE TABLE home_builder.draft_action (
     id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
     project_id      uuid REFERENCES home_builder.project(id) ON DELETE CASCADE,
     kind            text NOT NULL,            -- DraftKind
     status          text NOT NULL DEFAULT 'pending',  -- pending | approved | edited-then-approved | discarded
     originating_agent text NOT NULL,
     subject_line    text,
     summary         text NOT NULL,
     body_payload    jsonb NOT NULL,           -- per-kind structure
     external_ref    text,                     -- e.g. Gmail draft id, Drive doc id
     from_or_to      text,
     created_at      timestamptz NOT NULL DEFAULT NOW(),
     decided_at      timestamptz,
     decided_by      uuid REFERENCES auth.users(id)
   );
   ```
   This is move B in the original three-option plan. ~1 day to land entity + migration + adapter wiring on the four existing drafting agents.

2. **Reconcile dispatch handlers for `draft_action.approve / edit / discard` UserActions.** When Chad taps Approve / Edit / Discard in the renderer, it emits a UserAction; the reconcile pass routes it to the originating-agent's confirm path (e.g. `gmail-reply-draft.approve` → `gmail.send` via the stored Gmail draft id). New action types in `agents/reconcile_agent.py` dispatch table.

3. **Existing drafting agents adapt to write rows.** `gmail_followup`, `change_order_agent`, `client_update_agent`, `lien_waiver_agent`, `supplier_email_watcher` each gain a `_create_draft_action(...)` call when they produce a draft. This is mostly mechanical — wherever they currently print "Drafted reply, see Gmail Drafts" or write a Drive doc, they additionally insert a draft_action row referencing it.

Until move B lands, the morning view returns `judgment_queue: { count: 0, items: [] }` and the rest of the payload works fine — the existing agents continue to drop drafts in their current locations. Chad just doesn't get the consolidated inbox until move B ships. The view-model contract doesn't change.

---

## What this spec does not cover

These belong in adjacent or upstream specs, not here:

- **The renderer's typography, color, density** — the platform's design-language spec governs how this payload is drawn. The view-model is data, not UI.
- **Auth and transport** — `/v1/turtles/home-builder/views/morning/{project_id}` is the endpoint; the platform's turtle contract governs how it's reached, authenticated, and cached on the wire.
- **Cross-project rollup** — if Chad is running multiple active projects (he isn't today, but will be), the morning view as specified is *per-project*. A future `views/morning/all` may aggregate; defer until the second active project exists.
- **Push notification sequencing** — the morning view is a pull surface. Whatever push notifications fire overnight surface here as `overnight_events`, but the push channel itself is platform-owned.
- **The `chad_voice` / `chad_context` internals** — `chad-agent.md` governs persona; this spec just says "call the narrator with this context."

---

## Cross-references

This view-model joins the canonical contract:

```
canonical-data-model.md § View-model contract
  ├── master         (existing)
  ├── daily          (existing)
  ├── weekly         (existing)
  ├── monthly        (existing)
  ├── checklist-gates (existing — V2)
  ├── notification-feed (existing — V2)
  └── morning        (this spec — V2, depends on draft_action entity #18)
```

When this spec lands its implementation:
- Add a `Morning view` subsection to `canonical-data-model.md § View-model contract` mirroring the Daily / Weekly / Monthly format
- Add a `MorningView` definition to `view_models_schema.json` so the iOS / Mac / web Codable types can be generated identically
- Add `MorningViewPayload` to `home_builder_agent/scheduling/schemas.py` (Pydantic model — the source from which `view_models_schema.json` is regenerated)
- Add a `morning` projection function to `home_builder_agent/scheduling/view_models.py`
- Add `/v1/turtles/home-builder/views/morning/{project_id}` to the FastAPI shell-backend route table

None of those happen in this spec — they happen in the implementation commits that follow Connor's redline.

---

## Resolved decisions (Connor redline 2026-05-09)

1. **Weather risk pinned to the top.** Weather is the one input that can rewrite the whole day's plan, so it earns the top slot above the voice brief. Voice brief synthesizes everything *else*.
2. **Action items: 1–5, soft-picked by hb-chad based on the day's actual surface area.** Quiet days produce fewer items; busy days more. Synthesis must produce at least one (no empty list); the renderer can show 1 without visually penalizing the day.
3. **`urgency_band` lives on `today_on_site` items in V1.** Per-kind rules in § urgency_band semantics. Renderer uses `calm | watch | urgent` to drive visual emphasis; `urgency_reason` chip surfaces *why* in expanded density.
4. **Originating-agent attribution stays surfaced** on judgment queue items. The renderer shows "drafted by hb-supplier-email" / etc. as a chip. Trust signal: Chad sees which agent did the drafting work, builds confidence in delegating more of it over time.
5. **Cold-launch with stale cache: render stale + banner.** *Final decision is platform-owned* — the offline-tolerance contract applies to all six view-models, not just morning. The vertical's recommendation is baked into § Caching above and the platform thread will finalize whether it generalizes.
