# Scheduling Engine

> One-line summary: a backwards-scheduling engine fed by phase durations + vendor lead times that produces a master build-out schedule with milestones, drop-dead order dates, and a three-tier (daily / weekly / monthly) view per project.

**Status:** Spec — not yet scheduled.
**Phase:** 2.
**Owner:** CP.
**Last updated:** 2026-05-02.
**Source:** Chad's "AI Help List" brief, tab `list of list`. See [`samples/`](../../samples/README.md) for the artifact.
**Data contract:** entities, ownership rules, view-model shapes, and persistence-strategy comparison live in [`canonical-data-model.md`](canonical-data-model.md). This spec is the operational consumer of that model.

## Overview

Today `hb-timeline` produces a one-shot Doc + Tracker Sheet from a project spec. It does not own the schedule after that — the Tracker is the schedule, and updates flow through `hb-update`. That works for one project, manually maintained.

What Chad actually needs across N projects is a real scheduling engine: phase durations as constants, vendor lead times as constants, and a backwards-scheduler that turns a target completion date (or a target framing-start date) into:

1. A master build-out schedule with phase start/end dates.
2. Milestone dates (foundation pour, dry-in, drywall complete, CO).
3. **Drop-dead order dates per material category**, computed from each install date minus the category lead time minus a safety buffer.
4. An estimated completion date that updates as inputs slip.

This engine is the missing layer between Phase 1's per-project Tracker and Phase 3's Vendor Intelligence System: it tells the vendor recommender *when* every order has to be placed.

## Phase Duration Defaults

Days are calendar days unless noted; the engine should optionally treat them as business days behind a per-project flag once weather modeling is in.

| Phase | Default duration |
|---|---|
| Land Clearing | 2–5 days |
| Foundation (footing → slab) | 10 days |
| Framing | 20 days *(cabinets ordered when framing complete)* |
| Roofing | 3 days |
| Rough-in MEP (plumbing, HVAC, electrical) | 15 days |
| Siding | 7 days |
| Insulation | 3 days |
| Drywall (hang, sand, finish) | 6 days |
| Flooring (tile + hardwood) | 15 days |
| Trim | 5 days |
| Painting (interior + exterior) | 15 days |
| Final Grade | 1 day |
| Landscaping & Irrigation prep | 7 days |

These are Chad's trusted defaults — the engine treats them as starting points, not hard truth. Each project can override per phase.

## Lead-Time Defaults

Cross-reference: [`vendor-intelligence-system.md` § Lead-Time + Drop-Dead Date Logic](vendor-intelligence-system.md#lead-time--drop-dead-date-logic-from-chads-brief).

The Vendor Intelligence System is the source-of-truth for per-SKU and per-category lead times. The scheduling engine consumes those values; it does not re-define them. Until that system is live, the engine falls back to the category defaults table in the vendor spec (windows 10–12wk, flooring 3–4wk, interior doors 2wk, cabinets measure-driven, plumbing/electrical "before framing").

## Phase Sequencing & Dependencies

Typical sequence (linear approximation):

```
Land Clearing → Foundation → Framing → Roofing → Rough-in MEP →
Insulation → Drywall → Trim / Paint / Floor (parallel) →
Final Grade → Landscaping
```

Real projects overlap. Examples Chad called out and others worth modeling:

- **Roofing + siding** can overlap once dry-in is achieved.
- **Trim, paint, and flooring** typically run in parallel in different rooms.
- **Cabinets** are ordered at framing-complete but installed after drywall + paint primer.
- **Final grade + landscaping** compress against weather windows.
- **Inspections** are gating events between phases (foundation, rough-in, final) — a failed inspection re-opens a phase and ripples downstream.

V1 ships with strict linear sequencing + manual overlap overrides per project. V2 models overlap natively (see Open Questions).

## Checklist Library

Chad wants 24 phase checklists managed end-to-end. The agent treats each checklist as a **gate**: a phase cannot be marked complete (and therefore the next phase cannot start) until all checklist items are checked off.

Phases:

1. **Precon** — fleshed out by Chad, **44 items across 10 categories**: Client & Contract, Plans & Engineering, Selections, Permitting, Site Prep, Subcontractors, Materials, Budget, Schedule, Meetings. This is the model template.
2. Sitework
3. Foundation
4. Pre-framing
5. Framing
6. Post-framing
7. Plumbing rough
8. HVAC rough
9. Electrical rough
10. Siding & porch
11. Insulation rough
12. Drywall rough
13. Cabinet
14. Countertop
15. Trim & stairs
16. Paint
17. Tile
18. Wood flooring
19. Plumbing set out
20. Electrical setout
21. HVAC trim out
22. Landscape & irrigation
23. Final paint
24. Final punch out

The non-precon 23 are generated from industry standards plus Chad's stated preferences (knowledge base + spec sheet defaults), then reviewed by Chad. They are not transcribed from Chad — they're the agent's first draft, ready for redlines.

## Schedule view-model outputs

The Scheduling Engine projects four shared view-models out of canonical schedule data: daily, weekly, monthly, and master. These are not Tracker tabs — they are the structured payloads defined in [`canonical-data-model.md` § View-model contract](canonical-data-model.md#view-model-contract). Every renderer that shows a schedule consumes one of these payloads.

Two related view-models — checklist gates and the notification feed — are also engine outputs but live with the entities that produce them (the Checklist system and the Event/Notification model respectively, both in `canonical-data-model.md`). All six together are the contract surface between the engine and its renderers.

**Renderer note.** Desktop and mobile are co-equal renderers over these view-models. They differ in layout and input affordances — mobile prioritizes the daily payload and tap-to-update inputs from the field; desktop renders the full master schedule and authors checklists — not in data. The Tracker Sheet is a transitional renderer that may continue as a presentation/edit bridge under the persistence-strategy recommendation in [`canonical-data-model.md` § Schedule persistence strategy](canonical-data-model.md#schedule-persistence-strategy); it is not the engine.

### Daily (Chad's morning view)

What's happening *today* across all active projects:

- Tasks scheduled for today, by job site.
- Today's deliveries and installs.
- Today's inspections (passing? failing? rescheduled?).
- Drop-dead order dates **hitting today** that haven't been actioned.
- Sub no-show / material no-show flags from yesterday that didn't clear overnight.

### Weekly outlook

For each active project:

- Tasks scheduled this week.
- Drop-dead dates this week.
- Milestone meetings.
- Rolling 7-day weather impact (when weather is in).

### Monthly outlook

For each active project:

- Tasks scheduled this month.
- Delivery dates.
- Install dates.
- Inspection dates.
- Drop-dead dates for selections still open.
- **% completion vs plan** (earned-time style — phases complete weighted by duration).

### Master build-out schedule

The artifact, not a view: full phase Gantt with milestones, drop-dead order dates, and an estimated completion date that rolls forward as inputs slip.

## Notification Triggers

Each trigger is `(condition, data source, threshold)`. A fired trigger emits an Event into the canonical Event store; the **Notification dispatcher** (per [`canonical-data-model.md` § State ownership boundaries](canonical-data-model.md#state-ownership-boundaries)) owns the routing decision — which surface to render on, whether to fire push, whether to escalate to email or SMS. Triggers do not pick channels; they emit Events. Renderers consume the resulting Notifications. This split keeps trigger logic in the engine and routing logic in the dispatcher, neither tangled with the other.

| Trigger | Data source | Threshold |
|---|---|---|
| Selection deadline approaching | Drop-dead order date table per project | Today ≥ drop-dead − warning window (default 14 days, per-category override) |
| Weather impact on schedule | Weather API for job-site ZIP | Forecast precipitation or wind exceeds phase tolerance over the next N days (concrete pour, framing, roofing, exterior paint each have their own thresholds) |
| Sub no-show | **V1:** `UserAction:sub-checkin` from mobile (Chad or sub taps a check-in button from the field). **V2+:** SMS bot reply ("reply Y to confirm on-site today"), GPS-based job-site check-in, sub portal. | No check-in by 9am on a scheduled day |
| Material no-show | **V1:** `UserAction:material-delivery-confirm` from mobile or desktop (Chad or crew confirms delivery). **V2+:** vendor-adapter automatic delivery confirmations from Vendor Intelligence once supplier APIs are wired in. | Past scheduled delivery date with no confirmation |
| Inspection failure / re-inspect required | **V1:** `UserAction:inspection-result` from mobile or desktop (Chad logs Pass / Fail / Reinspect after the inspector leaves). **V2+:** Baldwin County permit-portal scrape or feed (when available) emitting automatic `inspection-status` Events. | Status = `Fail` or `Reinspect` |

Each fired trigger emits an Event with the appropriate type and payload (per [`canonical-data-model.md` § Event + notification model](canonical-data-model.md#event--notification-model)). The Notification dispatcher routes the Event to surfaces and channels; renderers (daily / weekly / monthly view, notification feed, push) display the resulting Notifications.

## Open Questions

- **Weather API choice.** Options: NOAA (free, sometimes flaky), Tomorrow.io, OpenWeather. Forecast horizon needed is 14 days for the weekly view, longer for the monthly. Cost vs. accuracy tradeoff TBD.
- **Inspection-status data source.** Baldwin County permit portal is the obvious answer; need to confirm whether it exposes a usable status query (scrape vs. official feed). Until automated, the inspection trigger fires from manual marks in the Tracker.
- **Sub-status check-in mechanism.** Daily phone calls don't scale. Options: SMS bot ("reply Y to confirm on-site today"), sub portal, location-based trigger (ask sub to check in via the app from the job site GPS). Defer choice until the rest of the engine is live.
- **Overlapping phases — V1 or V2?** Sticking with strict linear + per-project overlap overrides for V1 keeps the math simple. V2 models overlap natively (parallel phase tracks, resource-leveling). Decision driven by how often Chad needs to override the linear schedule in V1.
- **Where does the schedule live?** Each project's Tracker Sheet is the natural store today. Multi-project views (daily across all jobs) want a denormalized roll-up sheet — same question as Vendor Intelligence's "Sheets vs. Postgres vs. SQLite." Decide once. Tradeoffs and a preliminary recommendation (Sheets as presentation, relational store for engine state, reconcile-pass bridge) are worked through in [`canonical-data-model.md` § Schedule persistence strategy](canonical-data-model.md#schedule-persistence-strategy).
- **Holiday / non-working-day calendar.** Builders don't work Sundays or major holidays in Baldwin County. Build a calendar layer or assume Chad enters working days manually.
- **Critical-path identification.** Is V1 a Gantt that visually shows critical path, or just a sequence with drop-dead dates? Probably the latter — critical path falls out naturally once durations + dependencies are explicit.
