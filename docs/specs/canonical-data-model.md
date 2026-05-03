# Canonical Data Model

> One-line summary: shared object model and ownership rules across the home-builder-agent stack — the contract every layer reads and writes against so renderers, engines, ingestion, and storage stay in sync without coupling.

**Status:** Spec — conceptual contract, not yet scheduled.
**Phase:** cross-cutting (informs Phase 2 + Phase 3).
**Owner:** CP.
**Last updated:** 2026-05-02.
**Cross-references:** [`scheduling-engine.md`](scheduling-engine.md), [`vendor-intelligence-system.md`](vendor-intelligence-system.md).

## What this is, and what it isn't

This is a **conceptual contract**: the entities, relationships, and ownership rules that every layer of the system agrees on. It is the thing that prevents the Vendor Intelligence System, the Scheduling Engine, the desktop UX, the mobile UX, the supplier-email watcher, the checklist system, and the Tracker integration from drifting apart as each ships independently.

This is **not**:

- A database schema. No SQL types, no indexes, no migrations.
- A JSON / API spec. No wire format, no field naming conventions beyond the conceptual ones used here.
- An implementation plan. No library choices, no service boundaries, no transport mechanism.

Implementation specs (per-layer) consume this document; this document does not consume them.

## Architectural principles (anchored)

These three principles are repeated across the stack's specs because every layer's design choices have to honor them. They are not negotiable inside this document.

1. **Layered separation.** Ingestion (supplier-email watcher, vendor scrapers) is upstream of the source-of-truth (Vendor Intelligence System, Scheduling Engine), which is upstream of the operational consumers (renderers, notification dispatcher). Each layer has its own job and does not reach across.
2. **Fallback-first.** Every layer must be incrementally useful without waiting for perfect upstream dependencies. Vendor Intelligence not live → Scheduling Engine uses Chad's category-default lead-time table. Normalized vendor schema not live → supplier watcher writes to `KNOWLEDGE BASE/baldwin_county_supplier_research.md`. Automation unavailable → manual entry in the Tracker remains valid. Every entity defined below carries an explicit fallback behavior.
3. **Surfaces are read-or-input-only.** Renderers (desktop, mobile, Tracker view) do not mutate canonical state directly. They render projections out, and emit user actions in. The engine owns business logic and the resulting state transitions.

If a proposed change to any spec violates one of these, the spec changes — not the principle.

---

## Entities

Each entity below carries the same six headings. "Source-of-truth owner" names the layer whose copy is authoritative. "Fallback behavior" names what happens before that layer is live or when its data is missing. "Ownership classification" is a coarse triad — engine-owned, vendor-owned, renderer-owned — to make cross-layer reasoning fast.

### 1. Project

**Purpose.** Top-level customer engagement. One Project = one home build for one customer.

**Core fields.**
- `id`
- `name`
- `customer_name`
- `address` (job-site location; drives distance-to-vendor and weather-by-ZIP)
- `target_completion_date` (primary backwards-scheduling anchor)
- `target_framing_start_date` (alternate anchor; either-or with target completion)
- `status` (`active` / `on-hold` / `closed` / `archived`)
- `contract_signed_at`
- `created_at`

**Relationships.** → Phases (1:N), → Milestones (1:N), → Vendor preferences (M:N via override table), ← Events.

**Source-of-truth owner.** Scheduling Engine.

**Fallback behavior.** Today, `hb-timeline` produces a Tracker Sheet that functions as the Project record. Until a canonical engine store exists, the Tracker Sheet's metadata tab is the de-facto Project row.

**Ownership classification.** Engine-owned.

### 2. Phase

**Purpose.** A phase of construction within a Project (e.g., Foundation, Framing). Each Project has up to 24 Phases drawn from the standard checklist library; not every project uses all 24.

**Core fields.**
- `id`
- `project_id`
- `phase_template_id` (one of the 24 standard phases — see [`scheduling-engine.md` § Checklist Library](scheduling-engine.md#checklist-library))
- `name`
- `sequence_index` (1–24, drives default linear ordering)
- `status` (`not-started` / `in-progress` / `blocked-on-checklist` / `complete`)
- `planned_start_date`, `planned_end_date`
- `actual_start_date`, `actual_end_date`
- `default_duration_days` (from phase-duration defaults)
- `project_override_duration_days` (nullable)

**Relationships.** → Project, → Checklist (1:1, gating), → Tasks (1:N), → Milestones (1:N), → Dependencies (M:N), ← Deliveries, ← Inspections, ← WeatherImpact, ← Events.

**Source-of-truth owner.** Scheduling Engine.

**Fallback behavior.** Tracker Sheet rows in the Master Schedule tab represent Phases today. The engine projects Phase records out of those rows until a canonical store exists.

**Ownership classification.** Engine-owned.

### 3. Task

**Purpose.** Unit of work within a Phase. More granular than Phase. Optional in V1 — many phases ship without explicit task breakdown.

**Core fields.**
- `id`
- `phase_id`
- `name`
- `planned_date` (or `planned_start_date` + `planned_end_date` for multi-day tasks)
- `planned_duration` (hours or days)
- `actual_completion_at`
- `assigned_resource_ref` (Resource entity arrives in V2; nullable today)
- `status` (`scheduled` / `in-progress` / `complete` / `blocked`)
- `notes`

**Relationships.** → Phase, → Resource (V2), → Dependencies (M:N), ← Events.

**Source-of-truth owner.** Scheduling Engine.

**Fallback behavior.** Many V1 phases have no Task rows at all — the engine treats Phase as the smallest unit and only materializes Tasks when a phase needs sub-day granularity (e.g., daily-view tasks for active phases).

**Ownership classification.** Engine-owned.

### 4. Milestone

**Purpose.** Date-anchored event with no duration. Foundation pour, dry-in, drywall complete, CO. Different from Phase: a Milestone is a moment, not a span.

**Core fields.**
- `id`
- `project_id`
- `phase_id` (optional anchor — most milestones tie to a phase boundary)
- `name`
- `planned_date`, `actual_date`
- `status` (`pending` / `hit` / `missed` / `rescheduled`)

**Relationships.** → Project, → Phase.

**Source-of-truth owner.** Scheduling Engine.

**Fallback behavior.** Tracker Sheet has milestone date columns today; engine reads them.

**Ownership classification.** Engine-owned.

### 5. Dependency

**Purpose.** Predecessor/successor relationship between two Phases or two Tasks, with optional offset (lag/lead). Drives backwards-scheduling and ripple effects.

**Core fields.**
- `id`
- `predecessor_id`, `predecessor_type` (`phase` / `task`)
- `successor_id`, `successor_type`
- `dependency_kind` (`finish-to-start` default; `start-to-start`, `finish-to-finish`, `start-to-finish` for V2 overlap modeling)
- `offset_days` (positive = lag, negative = lead, default 0)

**Relationships.** ← Phase, ← Task.

**Source-of-truth owner.** Scheduling Engine.

**Fallback behavior.** V1 derives implicit linear finish-to-start dependencies from `Phase.sequence_index`. Explicit Dependency rows only appear when the relationship deviates from strict linear (overlap, lead, lag).

**Ownership classification.** Engine-owned.

### 6. Checklist

**Purpose.** Collection of ChecklistItems associated with a Phase. Acts as a **gate** — a Phase cannot transition to `complete` until all items in its Checklist are checked off, which means the next Phase cannot start either.

**Core fields.**
- `id`
- `phase_id` (1:1 with Phase)
- `template_version` (which version of the 24-checklist library this was instantiated from)
- `status` (`open` / `closed`)
- `completed_count`, `total_count` (denormalized for fast view rendering)

**Relationships.** → Phase (1:1), → ChecklistItems (1:N).

**Source-of-truth owner.** Checklist system (logical sub-component of the Scheduling Engine).

**Fallback behavior.** Until the Checklist system is live, phases are gated manually via a Tracker status field. Chad ticks a "phase complete" box in Sheets and the engine treats that as the gate signal.

**Ownership classification.** Engine-owned.

### 7. ChecklistItem

**Purpose.** Individual check within a Checklist. Carries category for the Precon-style 10-category structure (Precon's 44 items / 10 categories is the model template; non-precon checklists vary).

**Core fields.**
- `id`
- `checklist_id`
- `category` (one of 10 for precon: Client & Contract, Plans & Engineering, Selections, Permitting, Site Prep, Subcontractors, Materials, Budget, Schedule, Meetings; smaller set for other phases)
- `label`
- `is_complete`
- `completed_by` (actor ref)
- `completed_at`
- `notes`

**Relationships.** → Checklist.

**Source-of-truth owner.** Checklist system.

**Fallback behavior.** Manual list in a Sheets tab until the system is live. Items are typed by hand from the 24 templates. Chad ticks them in Sheets; the engine reconciles ticks as ChecklistItem state.

**Ownership classification.** Engine-owned (origin of the tick is renderer-input — see UserAction).

### 8. Vendor

**Purpose.** A supplier entity. Name, type, location, Chad's preferred-vendor weight, scrape/email metadata.

**Core fields.**
- `id`
- `name`
- `type` (`paint` / `lumber` / `plumbing` / `electrical` / `tile` / `cabinet` / `appliance` / `hardware` / `other`)
- `address`
- `distance_from_default_jobsite_miles` (cached)
- `preferred_vendor_weight` (Chad's pin: rank or 0–1 weight)
- `tos_status` (`compliant` / `restricted` / `blocked` — for scraper compliance)
- `last_scraped_at`, `last_email_seen_at`

**Relationships.** → VendorItems (1:N), ← UserAction (Chad pinning preferences), ← Events (vendor-published price/lead-time changes).

**Source-of-truth owner.** Vendor Intelligence System.

**Fallback behavior.** Until Vendor Intelligence ships, the vendor list lives in `KNOWLEDGE BASE/baldwin_county_supplier_research.md` as Markdown. The supplier-email watcher (Phase 2 item 3) updates that file as a fallback path; once the normalized schema is live, both the watcher and the catalog scrapers write to it instead.

**Ownership classification.** Vendor-owned.

### 9. VendorItem (SKU)

**Purpose.** A product offered by a Vendor. Links a Vendor to a normalized product so cross-vendor comparison works.

**Core fields.**
- `id`
- `vendor_id`
- `vendor_sku` (the vendor's own SKU code)
- `normalized_product_id` (FK into the normalized-product table; the matching layer's job)
- `name` (vendor-display name, used for fuzzy matching)
- `category`
- `variants` (size / color / finish / dimensions)
- `unit`
- `price`
- `in_stock` (`yes` / `no` / `low` / `unknown`)
- `lead_time_id` (FK to LeadTime — see entity 10)
- `vendor_url`
- `scraped_at`
- `match_confidence` (the matching layer's score for `normalized_product_id`)

**Relationships.** → Vendor, → NormalizedProduct (via fuzzy match), → LeadTime.

**Source-of-truth owner.** Vendor Intelligence System.

**Fallback behavior.** None coherent. VendorItems require the normalized schema to mean anything. Without Vendor Intelligence, "the lumber yard's 2x4 SKU" exists only in Chad's head or his spreadsheet — there is no agent-readable representation.

**Ownership classification.** Vendor-owned.

### 10. LeadTime

**Purpose.** A duration value attached to a SKU, vendor-default, or category-default. The `source` label is load-bearing — consumers must know whether the value is SKU-specific, vendor-default, or category-fallback to weight it correctly.

**Core fields.**
- `id`
- `scope` (`sku` / `vendor` / `category`)
- `scope_id` (FK varies by scope: VendorItem.id, Vendor.id, or category-name)
- `days` (numeric)
- `source` (`vendor-sku-published` / `vendor-default` / `category-default` / `manual-override`)
- `confidence` (0–1; reflects how stale or guess-y this value is)
- `observed_at`

**Relationships.** ← VendorItem (1:N — a SKU can have a history of LeadTime observations; current value is the most recent), ← category-default table.

**Source-of-truth owner.** Vendor Intelligence System.

**Fallback behavior.** When Vendor Intelligence is not live, LeadTime values come from the category-default table in [`vendor-intelligence-system.md` § Lead-Time + Drop-Dead Date Logic](vendor-intelligence-system.md#lead-time--drop-dead-date-logic-from-chads-brief) — windows 10–12wk, flooring 3–4wk, interior doors 2wk, cabinets measure-driven, plumbing/electrical "before framing." The Scheduling Engine resolves lead time with this priority: SKU-published → vendor-default → category-default → manual-override.

**Ownership classification.** Vendor-owned.

### 11. Delivery

**Purpose.** A scheduled material arrival. Links a SKU (or normalized product) to a Project/Phase with delivery status.

**Core fields.**
- `id`
- `project_id`
- `phase_id` (the phase the delivery feeds into)
- `vendor_item_id` (nullable — may be a normalized product reference if vendor not yet selected)
- `scheduled_date`, `actual_date`
- `status` (`scheduled` / `en-route` / `delivered` / `missed`)
- `tracking_ref` (carrier/PO ref where available)
- `source` (`vendor-confirmation` / `email-parse` / `manual-mark`)

**Relationships.** → Project, → Phase, → VendorItem.

**Source-of-truth owner.** Scheduling Engine. The engine consumes vendor confirmations (email-parse via the supplier watcher) or manual marks (Tracker entry) and projects Deliveries onto the schedule.

**Fallback behavior.** Without the supplier-email watcher, Deliveries exist only as manual marks in the Tracker. `material-no-show` events still fire — they fire from the absence of an `actual_date` past `scheduled_date`, regardless of how `scheduled_date` got there.

**Ownership classification.** Engine-owned.

### 12. Inspection

**Purpose.** Gating event with status. Tied to a Phase. A failed inspection re-opens the phase and ripples downstream — the engine recomputes the schedule from the failure date.

**Core fields.**
- `id`
- `project_id`
- `phase_id`
- `inspection_type` (`foundation` / `rough-in` / `final` / `framing` / `electrical-rough` / etc.)
- `inspector_authority` (`baldwin-county` / `private` / `other`)
- `scheduled_date`, `actual_date`
- `status` (`scheduled` / `passed` / `failed` / `reinspect-needed`)
- `failure_notes`
- `reinspect_date` (populated when `status = reinspect-needed`)
- `source` (`permit-portal` / `manual-mark`)

**Relationships.** → Project, → Phase.

**Source-of-truth owner.** Scheduling Engine.

**Fallback behavior.** All inspections are marked manually in the Tracker until Baldwin County permit-portal integration lands. The Open Question on portal data source ([`scheduling-engine.md` § Open Questions](scheduling-engine.md#open-questions)) is unresolved; canonical model is forward-compatible whichever way it resolves.

**Ownership classification.** Engine-owned.

### 13. Notification

**Purpose.** A surfaced Event delivered to Chad through a renderer. Notification = Event + delivery metadata. Don't conflate the two: an Event is what happened in the system; a Notification is a specific delivery of that event to a specific surface.

**Core fields.**
- `id`
- `event_id` (FK to Event — entity 17)
- `channel` (`in-app` / `push` / `email` / `sms`)
- `surface_target` (`daily-view` / `weekly-view` / `master-schedule` / `notification-feed` / `banner`)
- `push_id` (provider's reference; nullable)
- `delivered_at`, `viewed_at`, `dismissed_at`
- `click_action` (deep-link to entity — Project/Phase/Task/Vendor/SKU)

**Relationships.** → Event (1:1 wrapper).

**Source-of-truth owner.** Notification dispatcher (logical sub-component of the Scheduling Engine).

**Fallback behavior.** Without a push provider, Notifications surface only on the in-app feed when Chad opens the UX. The Event still fires; only the channel/surface degrades.

**Ownership classification.** Engine-owned.

### 14. ScheduleView

**Purpose.** A derived projection over schedule data — daily, weekly, monthly, master. Engine-owned, renderer-consumed. The view-model contract section below specifies the field shapes; this entity is the registry of which views exist and which inputs they project from.

**Core fields.**
- `id` (or scope key — view_type + project_scope + date_window)
- `view_type` (`daily` / `weekly` / `monthly` / `master`)
- `project_scope` (`single` with `project_id`, or `all-active`)
- `date_window_start`, `date_window_end`
- `generated_at`
- `payload` (the rendered shape; see view-model contract section)

**Relationships.** ← Phases, ← Tasks, ← Milestones, ← Deliveries, ← Inspections, ← Events, ← WeatherImpact.

**Source-of-truth owner.** Scheduling Engine. ScheduleView is computed; it is not stored authoritatively beyond a cache for fast renderer reads.

**Fallback behavior.** The Tracker Dashboard tab is today's proxy for ScheduleView. The engine writes Sheet cells that approximate the daily/weekly/monthly payloads. As the canonical store comes online, ScheduleView becomes a server-side projection and the Sheet becomes one renderer among several.

**Ownership classification.** Engine-owned (read-only to renderers).

### 15. WeatherImpact

**Purpose.** Forecast-driven impact assessment on a Phase or Task. Each weather-sensitive activity (concrete pour, framing, roofing, exterior paint) has its own thresholds; the engine emits WeatherImpact when forecasts cross those thresholds.

**Core fields.**
- `id`
- `project_id`
- `phase_id` (optional)
- `task_id` (optional)
- `forecast_window_start`, `forecast_window_end`
- `severity` (`info` / `warning` / `critical`)
- `affected_activity` (`concrete-pour` / `framing` / `roofing` / `exterior-paint` / etc.)
- `forecast_snapshot` (the forecast data at emission time — precipitation, wind, temperature, source)
- `source_provider` (NOAA / Tomorrow.io / OpenWeather)
- `observed_at`

**Relationships.** → Project, → Phase, → Task, ← Events (WeatherImpact emits a `weather-delay` Event when severity ≥ warning).

**Source-of-truth owner.** Scheduling Engine.

**Fallback behavior.** Without a weather API, WeatherImpact does not exist as data. Chad watches weather himself and manually flags slipped phases. The Open Question on weather API choice ([`scheduling-engine.md` § Open Questions](scheduling-engine.md#open-questions)) is unresolved; canonical model carries `source_provider` so the choice is reversible.

**Ownership classification.** Engine-owned.

### 16. UserAction (FieldUpdate)

**Purpose.** Input from Chad. Sub on-site Y/N, material delivered Y/N, inspection result, manual schedule override, checklist tick. Origin is renderer (desktop or mobile); once the engine processes it, the resulting state lives in the appropriate canonical entity.

**Core fields.**
- `id`
- `actor_user_id`
- `surface` (`desktop` / `mobile` / `tracker-edit`)
- `action_type` (`sub-checkin` / `material-delivery-confirm` / `inspection-result` / `schedule-override` / `checklist-tick` / `vendor-pin` / etc.)
- `target_entity_type` (the entity being mutated)
- `target_entity_id`
- `payload` (action-type-specific data)
- `recorded_at` (when Chad performed the action)
- `synced_at` (when the action reached the engine — differs from `recorded_at` for offline mobile)
- `conflict_resolution` (where the engine had to choose between server and client values — see future-proofing § offline mobile sync)

**Relationships.** → varies by `target_entity_type`.

**Source-of-truth owner.** *Origin* is renderer-owned (the action originates on a surface). *Persisted state* is engine-owned — once the engine applies the action, the resulting entity (e.g., a Delivery's `actual_date`) is the source of truth, and the UserAction record becomes audit history.

**Fallback behavior.** Manual entry in the Tracker Sheet is the original UserAction-equivalent. The engine reconciles Sheet edits via a periodic pass and treats them as UserActions for audit purposes.

**Ownership classification.** Renderer-owned at origin; engine-owned post-write.

### 17. Event

**Purpose.** Base type underlying Notification. Normalized structure for selection deadlines, weather delays, material no-show, sub no-show, inspection failure, schedule slip, and any future event types.

**Core fields.**
- `id`
- `type` (`selection-deadline` / `weather-delay` / `material-no-show` / `sub-no-show` / `inspection-failure` / `schedule-slip` / extensible)
- `severity` (`info` / `warning` / `critical` / `blocking`)
- `status` (`open` / `acknowledged` / `resolved`)
- `created_at`, `acknowledged_at`, `resolved_at`
- `project_id` (nullable per type)
- `phase_id` (nullable per type)
- `task_id` (nullable per type)
- `vendor_id` (nullable per type)
- `sku_id` (nullable per type)
- `payload` (type-specific; see Event + notification model section below)
- `source` (which layer emitted: `scheduling-engine` / `vendor-intelligence` / `supplier-email-watcher` / `weather-monitor` / `permit-portal`)
- `acknowledgement_actor` (user or agent that acknowledged/resolved)

**Relationships.** → Project / Phase / Task / Vendor / SKU (varies by type), ← Notifications (1:N — one Event can produce multiple Notifications across surfaces/channels).

**Source-of-truth owner.** Whichever layer emits is the originator; the canonical Event record is engine-owned post-emission. Vendor Intelligence emits stock/price-change Events; the supplier-email watcher emits ETA-change Events; the Scheduling Engine emits schedule-slip and selection-deadline Events; the weather monitor emits weather-delay Events; the permit-portal integration (when live) emits inspection-failure Events.

**Fallback behavior.** Without a Notification dispatcher, Events accumulate in an append-only log readable by Chad on the dashboard. The Event always exists; only delivery degrades.

**Ownership classification.** Engine-owned (multiple emitters, single owner of the canonical record).

---

## State ownership boundaries

Per layer: what it owns truth for, what it consumes read-only, and what it mutates (always via the engine for entities it does not own). The principle that no renderer mutates engine state directly is enforced by giving renderers no write APIs against canonical entities — they emit UserAction records instead, which the engine processes.

### Vendor Intelligence System

- **Owns truth for.** Vendor, VendorItem (SKU), LeadTime, NormalizedProduct (the matching layer's product registry, not separately listed above as it's an implementation concern within VI).
- **Consumes read-only.** Project (for `address` → distance computation).
- **Mutates via engine.** None outside its own entities.
- **Emits Events.** Stock-change, price-change, lead-time-change.

### Supplier-email watcher

- **Owns truth for.** Nothing — pure ingestion layer.
- **Consumes read-only.** Vendor and VendorItem (to know which SKU an inbound email refers to).
- **Mutates via engine.** Writes lead-time and availability updates into VendorItem and LeadTime through Vendor Intelligence's write API. Never reaches into engine state directly.
- **Fallback path.** Until Vendor Intelligence's normalized schema is live, the watcher's keyword-driven writes land in `KNOWLEDGE BASE/baldwin_county_supplier_research.md`. KB markdown is not engine state — it is a degraded-mode source that the engine ignores once Vendor Intelligence is live.
- **Emits Events.** ETA-change, backorder-detected.

### Scheduling Engine

- **Owns truth for.** Project, Phase, Task, Milestone, Dependency, Delivery, Inspection, ScheduleView, WeatherImpact, Event.
- **Consumes read-only.** Vendor, VendorItem, LeadTime (to compute drop-dead dates), ChecklistItem.is_complete (gating signal — engine reads but does not mutate).
- **Mutates via engine.** N/A — it is the engine.
- **Emits Events.** Selection-deadline, schedule-slip, sub-no-show, material-no-show, inspection-failure (post permit-portal integration).

### Tracker integration

- **Owns truth for.** Nothing canonically. Sheets are presentation and human edit surface, not source of truth.
- **Consumes read-only.** ScheduleView, Project, Phase, Milestone, Delivery, Inspection.
- **Mutates via engine.** Edits Chad makes in the Sheet round-trip back through the engine as UserActions via the reconcile pass — they are not direct writes to canonical state.

### Checklist system

- **Owns truth for.** Checklist, ChecklistItem.
- **Consumes read-only.** Phase (to know which template to instantiate; phases stay in the engine).
- **Mutates via engine.** Phase status — when a Checklist closes, the engine flips the Phase from `blocked-on-checklist` to `complete`. The Checklist system signals; the engine commits.

### Chad UX (desktop)

- **Owns truth for.** Nothing — pure renderer.
- **Consumes read-only.** ScheduleView (all view types), Project, Phase, Checklist, ChecklistItem, Notification, Event.
- **Mutates via engine.** Emits UserAction records (checklist ticks, inspection results, schedule overrides, vendor pins). Engine processes.

### Mobile UX

- **Owns truth for.** Nothing canonically. Holds a local cache of last-known-good ScheduleView for offline read; holds a local UserAction queue for offline write.
- **Consumes read-only.** ScheduleView (daily-view payload primary; weekly secondary), Notification (push-delivered when desktop is closed).
- **Mutates via engine.** Emits UserAction records (sub-checkin, material-delivery-confirm, inspection-result). Queued offline; synced when signal returns. See future-proofing § offline mobile sync for conflict resolution rules.

### Notification dispatcher

- **Owns truth for.** Notification.
- **Consumes read-only.** Event (subscribes; one Event → one or more Notifications based on routing rules and surface targets).
- **Mutates via engine.** Notification own-state only.

### Hard rule

**No renderer mutates engine state directly.** Every state change is either (a) the engine acting on its own logic, (b) Vendor Intelligence updating vendor-owned entities through its own API, or (c) a renderer emitting a UserAction that the engine processes. There is no fourth path. If a future feature seems to need one, the answer is to model it as a UserAction, not to grant the renderer write access.

---

## Event + notification model

Six event types ship in V1, plus extensibility for V2+ (resource conflicts, supplier going out of business, customer change orders affecting the build sequence).

### Common Event structure

Already specified in entity 17. The common shape is `{id, type, severity, status, timestamps, related-entity-ids, payload, source, acknowledgement_actor}`. Severity drives default channel (info → in-app feed only; warning → in-app + push; critical → in-app + push + email; blocking → all channels + dashboard banner).

### Per-type payload contract

Each event type carries a structured payload. This is the contract — consumers (Notification dispatcher, renderers) rely on these fields being present for that type. Fields not listed are not allowed in V1 payloads; add an event type rather than overload an existing one.

| Event type | Required entity refs | Payload fields |
|---|---|---|
| `selection-deadline` | `project_id`, `phase_id`, `sku_id` (or category) | `category`, `drop_dead_date`, `warning_window_days`, `lead_time_days`, `lead_time_source` (sku/vendor/category), `selections_open` (count) |
| `weather-delay` | `project_id`, `phase_id` | `affected_activity`, `forecast_window_start`, `forecast_window_end`, `forecast_snapshot`, `threshold_breached` (precip-mm or wind-mph), `source_provider` |
| `material-no-show` | `project_id`, `phase_id`, `sku_id` (or `vendor_id` if SKU unknown) | `delivery_id`, `scheduled_date`, `days_overdue`, `last_known_status` |
| `sub-no-show` | `project_id`, `phase_id` | `subcontractor_ref`, `scheduled_date`, `expected_check_in_time`, `last_check_in_at` (nullable) |
| `inspection-failure` | `project_id`, `phase_id` | `inspection_id`, `inspection_type`, `inspector_authority`, `failure_notes`, `reinspect_date` (nullable until set) |
| `schedule-slip` | `project_id` | `affected_phases` (list), `slip_days`, `cause_event_id` (the upstream event that caused the slip — chains for ripple effects), `new_estimated_completion_date`, `prior_estimated_completion_date` |

### Notification, separately

Notification wraps an Event with delivery metadata. **Don't conflate.** The Notification adds `channel`, `surface_target`, `push_id`, `delivered_at`, `viewed_at`, `dismissed_at`, `click_action`. One Event can produce multiple Notifications (e.g., a `selection-deadline` Event becomes a daily-view Notification + a push Notification + a banner Notification — three Notifications, one Event).

Routing rules belong to the Notification dispatcher and are out of scope here. The contract guarantee is that Notifications never invent fields — they always trace back to a real Event.

---

## Schedule persistence strategy

Expanded version of [`scheduling-engine.md` § Open Questions](scheduling-engine.md#open-questions) — "Where does the schedule live?" Three options compared, with a recommendation that is not yet final.

### Option A — Google Sheets

- **Pros.** Chad already lives in Sheets. Free, no infra. Easy human edits. Mobile read access via the Sheets app on his phone. Renders formatted views inline (banding, conditional formatting). Familiar to anyone we sell to next.
- **Cons.** Not transactional. No foreign keys or referential integrity. Race conditions when multiple agents (timeline-generator, status-updater, dashboard-watcher, supplier-email-watcher) write simultaneously. Brittle as a queryable engine store — multi-project queries become awful spreadsheet formulas. Formulas drift over time as Chad edits cells. Performance cliff once row count crosses ~10k or concurrent users cross ~5. Audit trail / event log is hard to model in a 2D grid. Refactor cost grows fast once schema needs strict typing.

### Option B — SQLite

- **Pros.** Transactional, queryable, single-file portability, zero-ops. Easy local backup. Type-safe schema. Good enough for single-tenant Chad now.
- **Cons.** Single-writer concurrency model. No remote multi-machine access without a sync layer. Mobile access requires a server in front (which is fine, but adds a hop). Backups are file copies — fine, but not as automatic as managed Postgres.

### Option C — Postgres

- **Pros.** Multi-writer. Mature. Real foreign keys + constraints. Scales cleanly to multi-tenant when Patton AI starts onboarding other customers. Row-level security for tenant isolation. Audit trail via triggers or logical replication.
- **Cons.** Ops overhead — even managed Postgres (RDS, Neon, Supabase) costs money and needs a connection pooler at scale. Overkill until multi-project / multi-tenant. Backup, migration, schema-change discipline required from day one.

### Risks of Sheets-as-engine-store (called out explicitly)

These are the failure modes that make Sheets unsuitable as the *primary* engine store, even though it remains valuable as a presentation/edit surface:

- **Race conditions** when multiple agents write simultaneously. Today the dashboard watcher writes at 60s intervals; status-updater writes on demand; timeline-generator writes on cold creation. Add a supplier-email watcher and a notification dispatcher and the collision rate climbs.
- **No transactions or referential integrity.** A "delete this Phase" operation has to be hand-coded across multiple tabs, with no rollback if step 3 fails after steps 1–2.
- **Performance cliff** above roughly 10k rows or 5 concurrent users. Multi-project rollups already strain this.
- **Audit log is hard to model.** Sheet history is per-cell, not per-event. Reconstructing "what fired when" requires log-scraping across tabs.
- **Refactor cost** grows quickly once the schema needs strict typing — adding a `lead_time_source` enum to a sheet column is a manual cleanup of every existing row.

### Recommendation (preliminary)

A bridge architecture, not a single store:

1. **Engine state in relational storage.** SQLite for single-tenant Chad now; Postgres when Patton AI multi-tenant kicks in. The engine reads and writes the relational store as canonical truth.
2. **Sheets as presentation/edit layer.** The engine *renders out* to Sheets for human-facing display — formatted Tracker tabs, dashboard summaries, the same Sheets Chad already uses. Chad edits Sheets when he wants to.
3. **Reconcile pass.** A periodic engine job reads Sheets for human edits and ingests them as UserAction records. The relational store is canonical; the Sheet is a working surface.

This honors the architectural principles: ingestion (Sheet edits) is upstream of source-of-truth (relational store); renderers (Sheets-as-display) consume the engine projection; surfaces never mutate canonical state directly — they emit UserActions that the reconcile pass turns into engine writes.

The decision is not final. Open questions: when does single-tenant SQLite stop being enough? (Best guess: when Patton AI signs customer two.) Where does the SQLite file live for cross-machine access? (Mac Mini today; might need a small server later.) Does the reconcile pass run on a fixed cadence or trigger on Sheets edit notifications? (Cadence first, push-driven later if latency hurts.)

---

## View-model contract

One engine payload → multiple renderers. Desktop and mobile consume the **same view-model**; they differ only in layout and input affordances. This is the contract that prevents desktop and mobile from forking.

### Principles

- **Derived, not stored.** A ScheduleView is computed from canonical entities on demand (with cache). It is never the source of truth for what it contains.
- **One projection per view type.** `daily`, `weekly`, `monthly`, `master`, `checklist-gate`, `notification-feed`. Each view type has a fixed shape; renderers do not request "daily but with extra fields" — they get the daily payload and render what fits.
- **Rendering decisions stay on the surface.** The view-model says *what* data to show; the renderer decides *how* (typography, density, drill-down affordance, tap-to-edit vs. read-only).

### Daily view

**Projected from.** Phase, Task, Milestone, Delivery, Inspection, Event (open + today's), drop-dead-date table per Project, sub/material no-show flags from yesterday.

**Filter.** Activities where `planned_date == today` OR `actual_date == today` OR `drop_dead_date == today` OR open Event with severity ≥ warning created within the last 24h.

**Grouping.** By `project_id` first, then by activity kind (tasks, deliveries, inspections, drop-dead dates, no-show flags).

**Exposed fields per item.** Activity name, project name, phase name, scheduled time/date, status, severity (if event-derived), `tap_action` ref (the entity to deep-link into).

**Mobile primary; desktop also rendered.** Same payload.

### Weekly view

**Projected from.** Phase, Task, Milestone, Delivery, drop-dead dates, milestone meetings, WeatherImpact rolling 7-day.

**Filter.** Activities within the upcoming 7-day window.

**Grouping.** By `project_id`, then by day of week.

**Exposed fields per item.** Same shape as daily, plus `forecast_summary` (when WeatherImpact is in).

**Mobile secondary (look-ahead); desktop primary surface.**

### Monthly view

**Projected from.** Phase, Task, Milestone, Delivery, Inspection, drop-dead-dates for open selections, Phase status (for % complete vs plan).

**Filter.** Activities within the upcoming 30-day window.

**Grouping.** By `project_id`, then by week.

**Exposed fields per item.** Activity name, scheduled date, status; plus per-project rollup: `pct_complete_vs_plan` (earned-time style — phases complete weighted by duration), `selections_open_count`, `next_drop_dead_date`.

**Desktop-class only.** Mobile renders read-only.

### Master schedule

**Projected from.** Phase, Dependency, Milestone, drop-dead-dates per category, estimated completion date.

**Filter.** Full project, all phases.

**Grouping.** By `project_id`, then by `phase.sequence_index`.

**Exposed fields per item.** Phase name, planned start/end, actual start/end, duration, dependencies, milestone anchors within the phase, drop-dead dates aligned to the phase, status.

**Desktop-class only.** Renders as Gantt or equivalent timeline. Mobile may show a compressed timeline strip; not the full Gantt.

### Checklist gates

**Projected from.** Checklist, ChecklistItem.

**Filter.** Active phases only (`status IN (in-progress, blocked-on-checklist)`).

**Grouping.** By `project_id`, then by `phase`, then by ChecklistItem `category` (Precon's 10 categories where applicable).

**Exposed fields per item.** ChecklistItem label, category, `is_complete`, `completed_at`, `completed_by`, `tap_action` (toggle complete — emits UserAction).

**Desktop primary** (full authoring); **mobile read-only or single-tap toggle** (`tap_action` is the only mutation a renderer offers; full text editing of items is desktop-class).

### Notification feed

**Projected from.** Event (status = `open` OR `acknowledged`), Notification (delivery metadata).

**Filter.** Events visible to the current user, sorted by `severity` then `created_at`.

**Grouping.** By severity (blocking → critical → warning → info) then by `project_id`.

**Exposed fields per item.** Event type, severity, summary (rendered from type-specific payload), related-entity links, age, status, `acknowledge_action` and `resolve_action` refs.

**Both surfaces, equal weight.** Mobile receives push; desktop polls.

### Naming conventions

Field names in view-model payloads use snake_case. Entity-reference fields end with `_id`. Status fields use lowercase-hyphenated enums (`in-progress`, `not-started`, `blocked-on-checklist`). Severity uses lowercase single words (`info`, `warning`, `critical`, `blocking`). These conventions exist so renderers across desktop, mobile, and Tracker bridge can deserialize identically.

---

## Future-proofing

Areas where the canonical model is intentionally extensible. None of these is V1 work; all are pre-budgeted in the design so V2 doesn't require a rewrite.

### Overlapping phase logic

V1 ships strict linear sequencing with manual overlap overrides per project (Dependency entity carries `dependency_kind` and `offset_days` already, so overrides are expressible). V2 models overlap natively: roofing + siding overlap once dry-in is achieved; trim/paint/flooring run in parallel. The canonical model already supports it via `dependency_kind ∈ {SS, FF, SF}` and `offset_days`. What V2 adds is engine logic to compute parallel phase tracks and resolve resource conflicts.

### Critical path support

Once durations and dependencies are explicit, critical path falls out of standard CPM (Critical Path Method) computation. The canonical model needs one addition for V2: an optional `float_time_days` field on Task (and computed-field on Phase). Critical path is the set of activities where `float_time_days = 0`. The engine computes it; renderers may visualize it (a red Gantt bar) or ignore it.

### Resource constraints

Same crew can't be in two places. V2+ adds a Resource entity (crew member, subcontractor, piece of equipment) and assignment relationships (Task ↔ Resource). Conflict detection becomes an engine job: given the schedule, are any Resources double-booked? Conflicts emit a new event type (`resource-conflict`).

### Multi-project coordination

The daily-view-across-all-active-jobs is already in V1's view-model. The future addition is **cross-project events** — a sub no-show on Project A may bump Project B if the sub is shared. Modeled as an Event with `project_id` set to the originating project plus a `cascading_to` array of related project IDs. The Notification dispatcher routes the event to all affected projects.

### Offline mobile sync

Mobile holds a local cache of the last-known-good ScheduleView and a local UserAction queue. When signal returns, queued UserActions sync to the engine.

Conflict resolution rules:

- **Server wins on schedule changes.** If the engine recomputed the schedule while Chad was offline (say, an inspection failure rippled into Phase dates), the server's schedule is canonical. Mobile's cached schedule is overwritten on sync.
- **Client wins on field updates.** If Chad logged "material delivered at 9:14am" from the truck, that timestamp is preserved even if the server received a different status update from another channel — Chad's on-site observation is treated as more authoritative than a vendor portal status. The conflicting server-side update becomes an Event for Chad to reconcile.
- **Last-write-wins on simple toggles.** Checklist ticks and similar idempotent state changes resolve by `recorded_at` (per UserAction). The engine records the conflict resolution decision in the UserAction's `conflict_resolution` field for audit.

### Multi-tenant for Patton AI

Once Patton AI signs a second customer, every entity gains a `tenant_id`. The override tables (Vendor preferences, lead-time defaults, phase-duration defaults) all become per-tenant. Postgres schema implication: row-level security keyed on `tenant_id`, or schema-per-tenant. Decision deferred until the actual second customer exists — schema-per-tenant is operationally heavier but harder to leak data across; row-level security is lighter but a single bug becomes a cross-tenant exposure.

The canonical model is forward-compatible: adding `tenant_id` to every entity is a schema migration, not a model rewrite.

---

## Cross-references

- [`scheduling-engine.md`](scheduling-engine.md) — operational consumer of this model. Phase durations, drop-dead date math, notification triggers.
- [`vendor-intelligence-system.md`](vendor-intelligence-system.md) — owns Vendor / VendorItem / LeadTime. The supplier-email watcher and the catalog scrapers both write into this layer's normalized schema.
- `CLAUDE.md` Phase 2 backlog dependency-map note — names this document as the shared contract that prevents drift across the dependency graph.
