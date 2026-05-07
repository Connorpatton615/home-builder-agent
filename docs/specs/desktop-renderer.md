# Desktop Renderer (Phase 2 #9)

> One-line summary: a native macOS SwiftUI application that renders the scheduling engine's view-models with desktop-class affordances — full Gantt, monthly Earned-Time view, 24 checklist authoring surfaces, live notification feed, drop-spec onboarding — sharing 90%+ of its code with the iOS shell and backed by the same FastAPI + engine + Postgres stack.

**Status:** Spec — un-parked 2026-05-07 at Connor's direction. Form factor + architecture locked; build queued.
**Phase:** 2 (co-equal with item #10 mobile per CLAUDE.md, sharing the engine's view-model contract).
**Owner:** CP.
**Last updated:** 2026-05-07.
**Lives in:** `~/Projects/patton-ai-ios/` (new `ios/PattonAIShellMac/` target alongside the existing `PattonAIShell/` iOS target).
**Cross-references:**
- [`scheduling-engine.md`](scheduling-engine.md) — produces the view-models this renders
- [`canonical-data-model.md`](canonical-data-model.md) — entity contract + view-model schemas
- [`chad-agent.md`](chad-agent.md) — `hb-chad` is the conversational surface; this is the visual surface
- `view_models_schema.json` — Codable contract iOS already pre-generates from
- `~/Projects/patton-ai-ios/CLAUDE.md` — backend + iOS conventions, must be honored

---

## What this is, what it isn't

The mobile surface (item #10) is **truck-cab** — what's happening today, tap-to-update field inputs, push notifications. It's the surface Chad uses *on the move*.

The desktop surface (item #9) is **desk-and-coffee** — full Gantt, monthly earned-time view, 24-phase checklist authoring, multi-project rolldown, notification feed history. It's the surface Chad uses *when he's planning*, and the surface Connor / a future PM uses for *operating the system*.

The two surfaces share an engine, an authentication layer, a backend, and Codable types. They differ in **layout density** (desktop = more on screen) and **input affordances** (desktop = keyboard shortcuts, multi-window, drag-drop, file pickers; mobile = thumb taps, swipes, voice).

It is **not** a Terminal TUI. It is **not** a static HTML report. It is **not** a Google Sheet. It is **not** a web app. Those were the cheap-renderer options the parked decision was holding for; the un-park decision (2026-05-07) takes the long-hard-technical route.

---

## Architectural decisions (load-bearing)

### D1 — Form factor: native macOS SwiftUI, not Catalyst, not Electron, not web

**Decision:** Native macOS SwiftUI target in the existing `PattonAIShell.xcodeproj`. Targets macOS 14+ (Sonoma), bundle id `com.pattonai.shell.mac`.

**Rationale:**
- **Code reuse with iOS is high without going Catalyst.** SwiftUI's cross-platform layer covers ~70% of view code; Networking, Codable, Auth, Keychain wrappers are 100% portable. Catalyst would give us 95% but at the cost of permanent "this looks like an iPad app on a Mac" feel — which is exactly the *not-solid-product* failure mode.
- **The Gantt is the hard problem.** Custom Canvas-based SwiftUI rendering performs well on Mac (60fps for 1000+ phases), is impossible to do cleanly in Catalyst, and would require WebView wrapping in Electron.
- **Mac native idioms matter for "solid product feel."** Multi-window, sidebar with collapsing sections, toolbar with traffic-light spacing, NSDocument-style state restoration, native menu bar, Quick Look, keyboard shortcuts, Time Machine-style scrubbing on the Gantt. None of this exists in Catalyst at quality bar.
- **Distribution is App Store + notarized DMG sideload.** Catalyst can do this too, but the binary is uglier.
- **Rejected alternatives:**
  - Catalyst: 95% reuse, 60% feel. Wrong tradeoff at this stage.
  - Electron: cross-platform but introduces a Node toolchain, ships at 200MB, native idioms via custom CSS forever. No.
  - Web app: requires hosting, requires login flow that diverges from iOS, no offline. Not the call when the iOS auth is already solid.
  - Flutter desktop: single codebase but introduces Dart and the Flutter rendering engine. Connor's already learning Swift; piling Dart on top is wrong.
  - Tauri: lighter than Electron but still web-tech-in-a-window. Same feel problem.

### D2 — Cohabits in `PattonAIShell.xcodeproj`, not a new project

New target `PattonAIShellMac` inside the existing project. Shared schemes for shared code. `Models/`, `Networking/`, `Auth/` directories become target-membership-shared. Per-platform views live under `iOS/Views/` and `Mac/Views/` respectively. No code duplication.

`project.yml` (xcodegen) extends accordingly:

```yaml
targets:
  PattonAIShell:        # iOS app, exists today
    platform: iOS
    deploymentTarget: "17.0"
  PattonAIShellMac:     # NEW
    platform: macOS
    deploymentTarget: "14.0"
    sources:
      - Mac/
      - Shared/
```

`Shared/` is the new home for `Models/`, `Networking/`, `Auth/` — pulled out of `PattonAIShell/` (iOS-only sources) so both targets compile against the same Codable types and HTTP layer.

### D3 — View-model contract is the data spine

The desktop renderer reads from the same six view-models the engine already produces (per [`scheduling-engine.md` § Schedule view-model outputs](scheduling-engine.md)):

1. `master` — full Gantt timeline + drop-dead overlay
2. `daily` — today across all active projects
3. `weekly` — next 7 days
4. `monthly` — next 30 days + % complete
5. `checklist-gates` — **V2** — needs Checklist entity wired (see D5)
6. `notification-feed` — **V2** — needs Event store wired (see D5)

iOS already pre-generates Swift Codable types from the published JSON Schema (`docs/specs/view_models_schema.json`). The Mac target re-uses those exactly. **Zero new Codable code on the Mac side.** This is what the canonical-data-model.md insistence on "spec the view-model contract first" pays off.

### D4 — Backend is unchanged

Same FastAPI shell-backend, same `/v1/turtles/home-builder/views/{type}/{project_id}` endpoints, same JWT auth (the path-B Apple Sign-In + Supabase exchange that shipped today). The Mac target's only addition: `Cmd+R` refresh keyboard shortcut → triggers the same view fetch the iOS pull-to-refresh does.

If anything, the backend gets **simpler** — the Mac client adds zero new endpoints. The whole point of the engine-as-view-model-producer architecture is that any number of renderers fan out without backend churn.

### D5 — V2 view-models are the prerequisite for "solid"

The mobile surface ships fine with master/daily/weekly/monthly. The desktop surface **does not** — its differentiator is the 24-phase checklist authoring (Precon's 44-item / 10-category template as the model) and the live notification feed. Both are V2 view-models; both need engine-side wiring before the desktop UX is real.

This is the home-builder-agent prerequisite work. See § "Build phases" below — phase 2 of this initiative is exclusively engine-side V2 view-model wiring.

### D6 — Authoring vs rendering split

The desktop is a **rendering** surface for engine state. It is **not** an authoring surface for the engine itself.

Specifically:
- Phase durations, dependencies, lead times → owned by the engine config + per-project overrides via `hb-update` / iOS `UserAction` writes / direct DB writes by the engine
- Checklist *templates* (the 24 with Precon's structure) → owned by a JSON / YAML in `home_builder_agent/scheduling/checklist_templates/`
- Checklist *instances* (a project's tickable items) → owned by `home_builder_agent.scheduling.engine` once the Checklist entity lands; mutations via the existing `UserAction` write loop
- Notification routing rules → owned by the canonical-data-model's Notification dispatcher (see canonical-data-model.md § State ownership boundaries)

The desktop's **only** write path is the same `POST /v1/turtles/home-builder/actions` the iOS shell uses today. No new writes, no new backend routes. Everything funnels through `UserAction` → reconcile → engine entity write.

This is the "engine state owns truth, specialists are tools" principle from chad-agent.md applied to UI: **the desktop is also a tool, not the product.** The product is Chad-the-agent + the engine.

---

## The six surfaces

Each is a SwiftUI view in `Mac/Views/`. Each consumes one view-model exclusively. Each has a corresponding (smaller, denser) iOS counterpart already shipping or planned.

### S1 — Master schedule (Gantt)

Custom SwiftUI `Canvas`-based Gantt. Horizontal timeline (months across the top, phases stacked rows). Each phase row shows planned start→end as a rounded bar with phase color, status overlay (not-started = outlined, in-progress = striped, complete = filled, blocked = red border). Drop-dead order dates render as inverted triangles below the corresponding install phase, color-coded by urgency (>30d = green, 14–30d = amber, <14d = red, hit-today = pulsing).

Interactions:
- Hover any phase → tooltip with name, dates, status, duration vs default
- Click phase → side-pane opens with phase detail (linked checklist, recent activity, notes)
- Click drop-dead triangle → side-pane opens with material category, lead time, install date, suggested vendors (when Vendor Intelligence lands)
- Cmd-Scroll → zoom in/out (week / month / quarter)
- Cmd-T → "today" indicator scroll-to
- Drag-select a phase range → batch operations menu (apply override, mark blocked-on-checklist, etc.)

The mobile version of this is read-only and zoomed to the current week. Desktop is the canonical authoring view.

### S2 — Daily view

Three-pane layout:
- **Left:** active project list (Whitfield, Pelican Point, etc.). Click to filter to one.
- **Center:** today's items, grouped by category (Tasks / Deliveries / Installs / Inspections / Drop-deads hitting today)
- **Right:** activity timeline (engine_activity rolling window, last 24h)

Each item is interactive: click → context. Tasks have a "complete" button. Deliveries have a "confirmed" button (writes a `UserAction:material-delivery-confirm`). Inspections have Pass/Fail/Reinspect buttons.

This mirrors the iOS daily payload exactly (same view-model). Mac just shows it bigger and adds the activity right pane.

### S3 — Weekly view

Vertical 7-column layout (Mon–Sun), one row per active project. Each cell shows phases active that day, drop-deads hitting that day, milestones. Click a cell → drill-down list. Cmd-arrow → nav to previous/next week.

A "this week's drop-deads" summary header pins to the top.

Mobile version is read-only and stripped to one column at a time.

### S4 — Monthly view + % completion

Calendar grid (rows = weeks, cols = days). Per-day badge counts (deliveries, installs, inspections, drop-deads). Per-project earned-time bar at the top:

```
Whitfield Residence — 38% complete vs plan (target Jan 30, 2027 holds)
[████████████░░░░░░░░░░░░░░░░░░░░░░░░░░] 4 mo. ahead on framing, on-pace overall
```

Click any day → daily view pre-filtered to that date. Cmd-arrow → nav between months.

Mobile: read-only, stripped to a 7-day strip + the project bar.

### S5 — Checklist authoring (24 phases × Precon template) — **V2**

This is the surface that doesn't exist on mobile and is the desktop's differentiator. Precon's 44-item / 10-category template is the structural model:

```
Precon (44 items / 10 categories)
├── Client & Contract        ✅ 4/4
├── Plans & Engineering      ✅ 5/5
├── Selections               🟡 3/8 (5 open, 1 critical)
│    ├── Tile package        ⏰ drop-dead 2026-06-15 (28 days)
│    ├── ...
├── Permitting              🟡 2/4
├── Site Prep               ⬜ 0/3
├── Subcontractors          ⬜ 0/5
├── Materials               ⬜ 0/4
├── Budget                  ⬜ 0/3
├── Schedule                ⬜ 0/2
└── Meetings                ⬜ 0/2
```

Each phase has its own checklist (the 23 non-Precon ones generated from industry standards + Chad's prefs, then redlined by Chad). Each item ticks off independently. A phase cannot mark complete (and its successor cannot start) until all items are checked — the gate semantic.

Authoring: Chad clicks an unchecked item → modal with notes field, optional "blocked on" reference (links to a sub, vendor, or another checklist item), optional drop-dead date. Notes flow into the engine's audit trail.

Multi-select: drag-select items → batch edit (assign owner, set due date, add note).

This UI does not exist anywhere else (mobile renders the V2 view-model read-only when it lands).

**Engine prerequisite:** the `Checklist` and `ChecklistItem` entities (per canonical-data-model.md § entities 6 & 7) need to be wired up. The view-model `checklist-gates` returns empty until then. See § Build phases phase 2.

### S6 — Notification feed — **V2**

Reverse-chronological list of Events (drop-dead approaching, weather impact, sub no-show, material no-show, inspection failure / reinspect required, schedule slip, payment/lien risk). Each row has:
- Severity icon (info / warning / critical)
- Timestamp + age
- One-line subject
- Two-line body
- Affected project + phase + entity links (clicking takes you to the relevant view)
- Quick actions (acknowledge / snooze 1d / dismiss / take action)

Filters: severity, project, age, type. Search by keyword.

A persistent counter in the macOS menu bar shows unack'd critical count.

**Engine prerequisite:** the `Event` and `Notification` entities (canonical-data-model.md § entities 13 & 17) need wiring, plus the `Notification dispatcher` per the same spec. The view-model `notification-feed` returns empty until then.

---

## Onboarding + input affordances (desktop-only)

Two affordances the mobile surface shouldn't carry:

### O1 — Drop-spec onboarding

Drag a `spec.md` file onto the app icon (or into a sidebar drop zone) → the file is uploaded to `POST /v1/turtles/home-builder/onboard` (new endpoint) → backend invokes `hb-timeline` → user gets a notification when the Drive Doc + Tracker Sheet are ready → click notification → main window jumps to the new project's master schedule.

This is the "drop-spec-into-folder onboarding" the CLAUDE.md spec mentions for #9. Backend route is new (previously `hb-timeline` was Terminal-only).

### O2 — Bulk checklist edit

Multi-window: open the master schedule in window 1, open phase 5 (Framing) checklist in window 2, drag-select 12 items, batch-assign to a sub. Productivity that doesn't fit on a phone.

### O3 — Multi-project rolldown

When more than one project is active, sidebar shows project list with quick metrics (% complete, next drop-dead, unack'd notifications). Click cycles through them. Cmd-1 / Cmd-2 / Cmd-3 jumps to project 1/2/3 respectively. Mobile would need a separate menu screen for this; desktop just has it.

---

## Build phases

Each phase has explicit exit criteria. Phase numbers match the standard glide path used by `proj_002`.

### Phase 1 — Research (3 days)

- Audit Apple's Mac SwiftUI Canvas perf for 1000+ phase Gantt rendering — proof-of-concept spike
- Audit `xcodegen` multi-target setup with shared sources (existing `project.yml` + new Mac target)
- Decide: Mac App Store distribution path now, or stay sideloaded-DMG until pilot validates? (Lean: stay sideloaded for v1, App Store at v1.5)
- Decide: SwiftUI native vs SwiftUI + AppKit interop for the Gantt? (Lean: native — `Canvas` is enough, AppKit only if the spike fails the perf bar)
- Decide: Quick Look extension for `.spec.md` files? (Lean: defer — nice but not blocking)

Exit: spike Gantt renders 200 phases at 60fps. project.yml multi-target compiles both. Distribution decision logged in `patton-os/data/decisions.md`.

### Phase 2 — Engine V2 view-models (5 days, home-builder-agent repo)

The desktop surface cannot ship as "solid" without these. Sequenced first.

- **Checklist entity wiring** — `home_builder_agent.scheduling.engine.Checklist`, `ChecklistItem`, populate templates for the 24 phases (Precon's 44-item is the canonical template; the other 23 generated by Sonnet from industry standards + Chad's KB, redlined manually). Persist via new migration 005 or extend 002. Wire the `checklist-gates` view-model to project these.
- **Event store wiring** — `engine.Event` + `engine.Notification` entities, dispatcher routing logic, persist via migration 005/006. Wire the `notification-feed` view-model. Hook all trigger sources from scheduling-engine.md § Notification Triggers (drop-dead approaching, weather, sub no-show, material no-show, inspection result).
- Update `view_models_schema.json` — extend the published JSON Schema for the two V2 view-models. iOS regenerates Codable types automatically.
- Engine tests: `tests/test_checklist_gate.py` (a phase cannot complete until all items checked); `tests/test_event_dispatch.py` (a triggered Event creates a Notification with the right severity).

Exit: `python3 -m home_builder_agent.scheduling.engine` against the Whitfield project produces non-empty `checklist-gates` and `notification-feed` view-model JSON.

### Phase 3 — Design (5 days, patton-ai-ios repo)

- Figma-equivalent specs for all six surfaces (master / daily / weekly / monthly / checklist authoring / notification feed) at @1x and @2x retina
- Mac-native interaction specs: keyboard shortcuts table, multi-window behavior, drag-drop targets, context menus
- Auth flow for Mac (same Sign in with Apple via `ASAuthorizationAppleIDProvider`, same Supabase exchange — proven on iOS, just need Mac entitlement)
- Asset catalog: Mac App Icon (1024x1024 with proper macOS layered look), menu bar icon, Quick Look thumbnail
- A new `Mac/` source directory layout proposed, reviewed against `iOS/`-side conventions

Exit: design package signed off by Connor + (optionally) the patton-os CTO via `patton-cto review`. Asset catalog populated.

### Phase 4 — Build (14 days)

In order:
1. xcodegen multi-target setup compiles both PattonAIShell (iOS) + PattonAIShellMac (Mac)
2. `Shared/` directory: pull `Models/`, `Networking/`, `Auth/` out of iOS-only and into shared
3. Mac auth flow: Sign in with Apple → Supabase exchange → JWT in Keychain (Mac variant)
4. Master schedule (S1) — Canvas Gantt + side-pane
5. Daily view (S2) — three-pane
6. Monthly view (S4) — calendar grid + earned-time bar
7. Weekly view (S3) — 7-column
8. Checklist authoring (S5) — *V2 view-model required, blocks here if phase 2 isn't done*
9. Notification feed (S6) — *V2 view-model required*
10. Drop-spec onboarding (O1) — backend route + Mac drop zone
11. Polish: keyboard shortcuts, multi-window, menu bar item, Quick Look, settings window

Exit: all six surfaces render against the Whitfield project, Cmd+R refreshes, Sign in with Apple works, drop-spec onboarding ends-to-end completes a new project.

### Phase 5 — Test (5 days)

- Real-device smoke (Connor's Mac mini + a Mac laptop borrowed from the network)
- Compare side-by-side with the iOS app for any data divergence
- Performance: 1000+ phase project (synthetic data), Gantt scrolls at 60fps, daily view filters in <100ms
- Auth: cold start with no Keychain → Sign in flow; warm start → token refresh on 401; logged out → all views render placeholder gracefully
- Drop-spec onboarding: malformed spec, oversized file, network interrupt mid-upload — all handled
- Accessibility: VoiceOver pass on all six surfaces, keyboard-only nav

Exit: zero data divergence between iOS and Mac for the same project. Connor uses it for a full operating day on Whitfield without falling back to Terminal.

### Phase 6 — Ship (3 days)

- Mac App Icon final pass (1024x1024 layered, dark mode variant)
- Notarized DMG sideload bundle hosted at `getpattonai.com/mac/PattonAIShell.dmg`
- Privacy policy supplement covering Mac-specific data access (Files entitlement, multi-window window-state persistence)
- Internal TestFlight equivalent for Mac (or sideload to Connor's two machines + Chad's Mac if he has one)
- Release notes / quick-start in `patton-ai-ios/docs/mac-quick-start.md`

Exit: Mac binary installable on three machines, all six surfaces work, Connor + Chad each used it once for a real operational task.

**Total: 35 days (5 weeks of focused work).** Can compress to 4 weeks if engine V2 view-models slip parallel to design phase, but the unblock dependency is hard.

---

## Cost envelope

The Mac surface inherits the iOS cost model: zero per-render Claude calls, the only Claude touchpoints are server-side (`hb-chad` if invoked from a chat surface; otherwise pure FastAPI → Postgres → view-model fetch). Render is free. **A Mac running this all day costs $0** in Anthropic spend. The only marginal cost is the engine-side actions Chad triggers via the buttons — which existed already.

---

## Open questions

- **Mac App Store vs sideloaded DMG vs both** — App Store gets discovery, costs $99/yr (Apple Dev membership already paid), gates on review (~3-day cycle for first submission). Sideload is faster but requires explicit user trust for the Developer ID-signed DMG. Lean: sideload for v1 pilot (Chad doesn't need discovery), App Store at v1.5 for sales surface.
- **Multi-tenant on Mac before iOS gets there** — the Mac surface, if positioned as Connor's operator dashboard, may need to see *all* tenants' projects (Patton AI master role). The iOS shell already has a master role pattern (per `proj_001` task_018). Reuse, don't fork.
- **Window restoration across reboot** — macOS supports state restoration natively but requires explicit work. Worth it for "feel like a solid product" — open Mac → it's where you left it. Add to Phase 4.
- **Touch Bar / function-key shortcuts** — Touch Bar is dead but virtual function-key bar via menu bar is alive. Lean: skip.
- **iCloud sync for window layout / preferences** — nice-to-have, defer to v1.5.
- **Quick Look extension for `.spec.md` files** — preview the parsed timeline + cost rollup in Finder Quick Look. Cool. Defer.
- **Spotlight integration** — index project names + checklist items so Cmd+Space "whitfield framing" shows the right phase. Defer to v1.5.
- **Keyboard shortcut for `hb-chad`** — Cmd+/ opens a global pop-over Ask interface anywhere in the app. The chat agent right at the user's fingertips on every screen. **Should be in v1** — this is what makes it feel integrated rather than two products.

---

## Why this is the right time to un-park

Three reasons #9 was correctly parked earlier today and three reasons it's right to un-park now:

**Why parked then:**
1. iOS-first was correct for a single-customer pilot — Chad needs his phone first, his laptop second.
2. The engine view-models hadn't shipped yet. Building a renderer with no data to render would have been theater.
3. JWT auth wasn't proven. A Mac client without auth is a demo, not a product.

**Why un-park now:**
1. **Engine view-models exist.** Master / daily / weekly / monthly all return real data against Whitfield Residence today.
2. **JWT auth is proven on real hardware.** The exact Sign in with Apple → Supabase → backend exchange we'd reuse on Mac shipped this morning.
3. **`hb-chad` works end-to-end.** The chat surface is the Mac's natural Cmd+/ pop-over. Building Mac without `hb-chad` would have been a different product; building Mac *with* `hb-chad` is "Patton AI on a desktop."

The technical floor (auth + engine + persona layer) is high enough now that what would have been a 3-month build six weeks ago is a 5-week build today.

---

## Cross-references — what this depends on, what depends on it

```
Desktop Renderer (this spec)
  ├── consumes:   Scheduling Engine                  (item #8)  — SHIPPED
  │               Engine V2 view-models              (this spec § Phase 2) — TBD
  │               JWT auth + Supabase exchange       (proj_002 sprint 1 #1) — SHIPPED
  │               hb-chad master agent               (chad-agent.md) — SHIPPED steps 1–3
  │               Notification dispatcher            (canonical-data-model.md § entities 13/17) — TBD
  ├── shares with: PattonAIShell iOS                 (item #10) — SHIPPED substantially
  │                FastAPI shell-backend             — SHIPPED
  │                view_models_schema.json           — SHIPPED (extended in phase 2)
  └── unlocks:    Patton AI's "we have a real desktop product" sales surface
                  Chad's planning ritual moves off the Tracker Sheet onto the app
                  Connor's operator role gets a proper dashboard instead of Terminal
```
