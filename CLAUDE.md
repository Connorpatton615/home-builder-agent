# CLAUDE.md — Home Builder Agent

This file is what Claude Code reads at session start to understand the project.
Update it as the architecture changes; future sessions are only as smart as
this file plus the code itself.

## CTO governance — actively consult before architectural choices

This repo operates under CTO governance. The Claude Code session reading
this file is *required* to use the CTO loop — not just aware of it.

**At session start (or before any non-trivial code change), read
`~/Projects/patton-os/data/decisions.md`.** That file is the binding ADR
log; its entries are load-bearing on stack, infrastructure, auth,
state-persistence, and deploy choices. If a planned change contradicts
an existing ADR, stop and surface that to the user before proceeding.

**When you hit an architectural fork not already covered by an ADR** —
e.g. "should this be SQLite or Postgres?", "JWT in a header or in a
cookie?", "is this a sub-agent or a tool call?" — consult the CTO via
Bash *before* proposing an answer:

```bash
PYTHONPATH=/Users/connorpatton/Projects/patton-os \
  /Users/connorpatton/Projects/patton-os/.venv/bin/patton-cto scope "<concrete question>"
```

Read the CTO's response, summarize its key points to the user in chat,
then make your recommendation informed by it. Do not silently pick the
answer yourself.

**When the user confirms a binding architectural decision** (e.g. "yes,
use SQLite", "let's go with hand-rolled JWT"), formalize it before
continuing:

```bash
PYTHONPATH=/Users/connorpatton/Projects/patton-os \
  /Users/connorpatton/Projects/patton-os/.venv/bin/patton-cto adr "<decision in one sentence>"
```

That writes a full ADR to `~/Projects/patton-os/data/decisions.md`, so
other Claude sessions in other repos automatically see the same ground
truth on next startup.

**Reserve consultation for genuine architectural forks.** Renames,
small refactors, bug fixes, copy edits — those don't need the CTO. Each
`scope`/`adr` call is an Anthropic API spend; use it for decisions that
set precedent.

For a periodic snapshot of stack + technical debt:

```bash
PYTHONPATH=/Users/connorpatton/Projects/patton-os \
  /Users/connorpatton/Projects/patton-os/.venv/bin/patton-cto tech-brief
```

The CTO's `health` job also runs daily at 8:15 AM and emails Connor on
critical issues, independent of your session.

## Master router — `patton` (human entry point)

For interactive terminal use, the master router is the preferred entry point
to the entire C-suite. Connor types a natural-language intent, Haiku classifies
it, and the right C-suite CLI handles the work:

```bash
patton "what is our legal compliance status"
patton "draft a follow-up to chad about photos"
patton --dry-run "should we use sqlite or postgres"   # show routing only
patton --show-log                                      # last 20 invocations
```

Per-CLI direct invocations (`patton-ceo`, `patton-cto`, etc.) remain on PATH
and are appropriate for scripting or when the right agent is already obvious.
The router self-bootstraps `PYTHONPATH` so it works from any shell with no
setup. Usage logs to `~/.patton-router-log.yaml` for later pattern analysis.

**Audience note:** the router is the *human* entry point. Claude Code sessions
reading this file should still consult the CTO directly via the pattern above
(`patton-cto scope/adr` invoked through Bash), not via the router.

## What this is

Multi-agent AI system for **Palmetto Custom Homes** — Baldwin County, AL luxury
custom home builder. Built in Python on top of Anthropic's Claude API +
Google Drive/Docs/Sheets/Gmail APIs. Runs locally on a Mac Mini. Goal is to
sell to Chad first, then potentially to other custom home builders.

End-state vision: a small AI company inside Chad's business — multiple
agents, each handling a different operational concern, all running in
parallel, sharing context and learning Chad's decision patterns over time.

## Repo layout

```
home-builder-agent/                 ← project root (~/Projects/home-builder-agent/)
├── pyproject.toml                  ← Python project config; `pip install -e .`
├── README.md                       ← public-facing repo description
├── CLAUDE.md                       ← this file
├── .env                            ← ANTHROPIC_API_KEY (gitignored)
├── credentials.json                ← Google OAuth client (gitignored)
├── token.json                      ← Google OAuth token (gitignored)
├── .watcher_state.json             ← watcher's modifiedTime memory (gitignored)
├── watcher.log                     ← watcher's structured log (gitignored)
├── .claude/                        ← Claude Code project settings + slash commands
├── home_builder_agent/             ← the package
│   ├── config.py                   ← single source of truth for paths/scopes/models/pricing
│   ├── core/                       ← cross-cutting: auth, claude_client, knowledge_base
│   ├── integrations/               ← Google API wrappers (drive, docs, sheets, gmail)
│   ├── agents/                     ← user-triggered agents (4 in Phase 1)
│   └── watchers/                   ← long-running poll loops invoked by launchd
└── legacy/                         ← original flat-file agents, kept for reference
```

## Phase 1 agents (shipped)

| Agent | Module | CLI | What it does |
|---|---|---|---|
| Timeline generator | `agents/timeline_generator.py` | `hb-timeline [spec.md]` | Reads a project spec → generates polished Doc + 3-tab Tracker Sheet (~$0.50/run) |
| Status updater | `agents/status_updater.py` | `hb-update "<NL update>"` | Parses NL → cascades through dependency graph → applies to Sheet → refreshes Dashboard (~$0.02/run) |
| Dashboard refresher | `agents/dashboard_refresher.py` | `hb-dashboard` | Reads Master Schedule → writes Dashboard tab + visual formatting ($0/run) |
| Gmail follow-up | `agents/gmail_followup.py` | `hb-inbox [--days N] [--upload]` | Lists threads → Haiku classifies → Sonnet writes Chad-voice checklist (~$0.05/run) |
| Help desk | `agents/help_desk.py` | `hb-help "<question>"` | Answers questions about the system; auto-appends informative Q&A to the FAQ Google Doc (~$0.02–0.05/run) |
| Finance Office | `agents/finance_agent.py` | `hb-finance` | Finds/creates "Chad's Finance Office" folder + Cost Tracker sheet (21 sections, pre-populated allowances, Invoices tab, Allowance Recon tab); writes Finance Summary KPI tab ($0/run) |
| Receipt logger | `agents/receipt_agent.py` | `hb-receipt <photo>` | Photo → Sonnet Vision extracts vendor/amount/category → updates Cost Tracker Actual column + saves receipt to Drive + logs to Actuals Log (~$0.01/run) |
| Finance ledger | `agents/ledger_agent.py` | `hb-ledger "<update>"` | Plain-English financial entry → Sonnet parses → routes to Actual/Billed/Invoice/Commitment in Cost Tracker (~$0.01/run) |
| Morning brief | `agents/morning_brief.py` | `hb-brief [--dry-run]` | Daily 6 AM email: NOAA weather + weather-risk phases + project snapshot + invoices due + overnight high-urgency emails + action items (~$0.02/run) |
| Change Order | `agents/change_order_agent.py` | `hb-change "<NL description>" [--client-email EMAIL] [--dry-run]` | NL → parses CO → assigns CO# → creates formal Drive doc → logs to Change Orders tab → updates Cost Tracker col C → updates schedule (if impact) → drafts Gmail approval to client (~$0.04/run) |
| Procurement alerts | `agents/procurement_alerts.py` | _(auto-runs inside `hb-update`)_ | After every schedule change: checks affected phases for procurement lead-time windows → macOS notification + logs to Tracker "Procurement Alerts" tab. 22 material categories with tunable lead times in `config.py`. ($0/run — no Claude call) |
| Client update email | `agents/client_update_agent.py` | `hb-client-update --from-tracker [--send] [--dry-run]` (or `--to EMAIL --client-name "Name"`) | Weekly homeowner project summary in Chad's voice — reads schedule + COs → Sonnet writes polished email → Gmail draft by default (--send to auto-send). `--from-tracker` reads recipient from Tracker's "Project Info" tab — used by the weekly cron. (~$0.02–0.03/run) |
| Inspection tracker | `agents/inspection_tracker.py` | `hb-inspect` / `hb-inspect log "<NL>"` | Baldwin County 12-step inspection sequence tracker + 180-day permit expiry countdown. NL logging via Haiku. macOS notification at 150/165 days. Hooks into morning brief. (~$0.005/log, $0/status) |
| Site log | `agents/site_log_agent.py` | `hb-log "<entry>"` / `--view` / `--tail [N]` | Timestamped append-only site log per project; lives in Drive `Site Logs/<Project> — Site Log`. No Claude rephrasing — Chad's actual words preserved verbatim for legal record integrity. ($0/run) |
| Lien waiver tracker | `agents/lien_waiver_agent.py` | `hb-waiver` / `hb-waiver log "<NL>"` | Cross-references Cost Tracker Actuals Log against signed waivers; flags payments >$500 missing a waiver (lien risk). NL log via Haiku. Hooks into morning brief. (~$0.005/log, $0/status) |

## Phase 2 shipped — engine + orchestration + observability

The Phase 2 layer that turned the toolkit into a system. All on `main`, all in production.

| Agent / Module | CLI / Hook | What it does |
|---|---|---|
| Scheduling Engine | `hb-schedule [project]` (also imported by everything below) | Pure-Python backwards-scheduler from target completion or framing-start dates → 13-phase Gantt + milestones + drop-dead order dates. 4 of 6 view-models live (master / daily / weekly / monthly); checklist-gates + notification-feed stubbed pending Checklist + Event store. Postgres adapter via `--from-postgres` / `--ping-db` / `--seed-postgres`. Spec: [`docs/specs/scheduling-engine.md`](docs/specs/scheduling-engine.md). |
| Drive→Postgres bridge | `hb-bridge [name?] [--dry-run]` | Syncs Drive Tracker sheets into `home_builder.project` + `home_builder.phase` rows. Idempotent on `drive_folder_id`. Real Chad data (Whitfield Residence) flowing. ($0/run) |
| Engine reconcile pass | `hb-reconcile [--dry-run] [--since]` | Drains `home_builder.user_action` rows since the watermark and dispatches each to the engine. Closes the iOS shell → engine write loop. Runs every 60s via launchd; manual invocation supported. Watermark in `.reconcile_watermark.json`. ($0/run) |
| Ask agent | `hb-ask "<question>"` | Opus + tool-calling RAG over Drive (Tracker, Cost Tracker, Site Logs, KB) and Postgres (engine state, recent activity). Read-only. Returns answer + citations. (~$0.05–0.30/run) |
| Router agent | `hb-router "<NL command>"` | NL command dispatch to specialist agents. Haiku classifier + Sonnet param extraction. **Load-bearing**: the only writer to `home_builder.engine_activity` (the audit-trail chokepoint per migration_003). (~$0.005–0.02/run + downstream agent cost) |
| Profile agent | `hb-profile [--days N] [--save]` | Builds an `HBUserProfileV1` JSON from existing signals (engine_activity, inbox-watcher state, Drive activity, project list). Stub stage: writes to `~/.hb-profile-proposed.json` until migration 004 lands. (~$0.05/run) |
| **Chad Agent (master)** | `hb-chad "<input>"` | The persona layer on top of everything above. Opus + tool-calling, system prompt = chad_voice + chad_context + persona. Two tools: `ask_chad` (delegates to hb-ask) and `dispatch_action` (delegates to hb-router). Speaks in Chad's voice, makes Chad's judgment calls. **The product surface for the iOS Ask tab.** Spec: [`docs/specs/chad-agent.md`](docs/specs/chad-agent.md). (~$0.05–0.30/turn) |
| Heartbeat watchdog | `hb-watchdog` | Reads `.heartbeats/*.json` every 10 min; alerts on staleness via macOS notification + `.heartbeat_alerts.log`. Each launchd job beats on success via `core.heartbeat.beat_on_success` decorator; per-job thresholds = ~5x cadence + grace. ($0/run) |
| Notification triggers | `hb-triggers [--project NAME] [--force]` | Engine-side automatic Event emitter (per scheduling-engine.md § Notification Triggers). V1 ships selection-deadline: scans every active project's drop-dead order dates, emits `selection-deadline` Events into `home_builder.event` for any in OVERDUE / ORDER NOW / THIS WEEK / UPCOMING bands. Idempotent — dedupes by (project, category) over 30-day lookback. Severity = critical/critical/warning/info per band. Runs daily 7:05 AM via `com.chadhomes.notification-triggers` launchd. ($0/run) |
| Morning view-model | `hb-morning <project> [--no-synth] [--json]` | End-to-end CLI for the morning surface (per [`morning-view-model.md`](docs/specs/morning-view-model.md)) — Chad's "morning coffee work station" landing. Loads schedule + drop-deads + overnight events + pending drafts, fetches weather, synthesizes voice_brief + 1-5 action items via `chad_voice("narrator")` in one Sonnet call, prints terminal-pretty or `--json`. The HTTP route `/v1/turtles/home-builder/views/morning/{project_id}` (platform-thread) wraps this same orchestration. Graceful when `home_builder.draft_action` (migration 007) isn't yet applied — judgment_queue stays empty. (~$0.02/run; $0 with `--no-synth`) |
| System status | `hb-status [--json]` | One-shot health check. Aggregates launchd job state + heartbeat freshness + today's spend (Opus / total caps from `core/cost_guard.py`) + morning view cache age + engine queue depths (`pending_drafts`, `open_events`, `unprocessed_user_actions`) + recent stderr errors per launchd job. Pretty terminal output with progress bars on cost caps; `--json` for ops dashboards. Exit codes: `0` healthy, `1` heartbeat stale, `2` critical events open. Use as `hb-status && echo OK`. ($0/run) |
| Project lifecycle | `hb-project list [--include-archived] / show <name> / archive <name> [--reason] [--yes] / create --name [--copy-from <source>] [--target-completion YYYY-MM-DD]` | DB-only project lifecycle CLI. `archive` flips status (active surfaces no longer show it; phase / event / draft history preserved). `create` makes a fresh shell (requires `--target-completion` or `--target-framing-start`). `--copy-from` clones source's phases + milestones with status reset to `not-started` and actuals NULL'd. Resolves projects by UUID / exact name / case-insensitive substring including archived. Hooked into hb-router as `manage-project` and exposed as three top-level tools on hb-chad — `archive_project`, `create_project`, `clone_project` — per ADR 2026-05-09 Q1 (separate tools chosen over a single multi-action tool for routing precision + schema-level validation). Chad says "archive Pelican Point" → `archive_project`; "spin up Full Test Loop V.0 cloned from Whitfield" → `clone_project`; the master agent dispatches. v1 does NOT touch Drive folders or Tracker sheets — DB flip is sufficient because active vs archived filter on `/views/*` fetches handles surface routing. ($0/run via CLI; ~$0.18 via hb-chad on Opus.) |

Active background processes:
- **Dashboard watcher** (`watchers/dashboard.py`) — runs every 60s via launchd. Polls GENERATED TIMELINES for modified Tracker sheets, refreshes their Dashboard tab. State in `.watcher_state.json`. Logs to `watcher.log`. Plist: `~/Library/LaunchAgents/com.chadhomes.dashboard-watcher.plist`.
- **Inbox watcher** (`watchers/inbox.py`) — runs every 5 min via launchd. Polls Gmail for new INBOX messages since the last historyId, classifies via Haiku (using `classifiers/email.classify_thread`), fires a macOS notification on `urgency=high` hits. Also detects invoices via `classifiers/invoice.is_invoice_email` + `extract_invoice_data` and logs them to the Cost Tracker Invoices tab automatically. State in `.inbox_watcher_state.json`. Logs to `inbox_watcher.log`. Plist: `~/Library/LaunchAgents/com.chadhomes.inbox-watcher.plist`.
- **Morning brief** (`agents/morning_brief.py`) — runs at 6:00 AM daily via launchd. Fetches NOAA weather for job site, reads project Tracker + Cost Tracker, checks inbox watcher log for overnight high-urgency emails, composes and sends Chad a daily brief email via Gmail API. Plist: `~/Library/LaunchAgents/com.chadhomes.morning-brief.plist`.
- **Reconcile pass** (`agents/reconcile_agent.py`) — runs every 60s via launchd. Scans `home_builder.user_action` for new rows since the watermark, dispatches each by `action_type` (inspection-result → phase status flip; schedule-override → phase date overrides; checklist-tick / material-delivery-confirm / sub-checkin / vendor-pin currently skip with reason). Watermark in `.reconcile_watermark.json`. Logs to `/tmp/reconcile.{stdout,stderr}.log`. Plist: `~/Library/LaunchAgents/com.chadhomes.reconcile.plist`. Closes the iOS shell → engine action loop.
- **Weekly client update** (`agents/client_update_agent.py`) — runs Mondays at 7 AM via launchd. Invokes `hb-client-update --from-tracker --send`; reads homeowner name + email from the Tracker's "Project Info" tab, generates a polished weekly status email in Chad's voice, sends directly. Plist: `~/Library/LaunchAgents/com.chadhomes.client-update.plist`.
- **Morning view cache** (`agents/morning_view_agent.py`) — runs daily at 6:05 AM via launchd (5 min after `hb-brief` so the email and the in-app surface stay consistent). Invokes `hb-morning Whitfield --cache`; pre-computes the morning view-model JSON payload — including the Sonnet-synthesized voice_brief + 1-5 action_items — and writes atomically to `.morning_cache/<project_id>.json`. The platform's `/v1/turtles/.../views/morning/{project_id}` HTTP route reads from there at cold-launch, returning the pre-synthesized payload without making Chad wait on a synchronous Sonnet call. Plist: `~/Library/LaunchAgents/com.chadhomes.morning-view.plist`. ~$0.02/day.
- **Heartbeat watchdog** (`watchers/watchdog.py`) — runs every 10 min via launchd. Reads `.heartbeats/*.json` (each launchd job beats on success), alerts on staleness via macOS notification + `.heartbeat_alerts.log`. Per-job thresholds: dashboard/reconcile 300s, inbox 1500s, morning-brief 25h, morning-view 25h, client-update 8d. Plist: `~/Library/LaunchAgents/com.chadhomes.watchdog.plist`.

All six home-builder launchd jobs (dashboard-watcher, inbox-watcher, reconcile, morning-brief, client-update, morning-view) plus `hb-bridge` now emit structured JSON to stderr via `home_builder_agent.observability.json_log.configure_json_logging()`. Schema: `ts | level | service | event | correlation_id | message | payload | traceback?`. Live JSON at `/tmp/<job>.stderr.log`; legacy print() text continues to `/tmp/<job>.stdout.log`. The configure helper has a TTY gate so interactive Terminal runs (`hb-reconcile --dry-run`, etc.) stay human-readable on the existing print() statements alone.

## Phase 2 backlog (in priority order)

1. ~~**Morning brief**~~ — SHIPPED: `hb-brief` sends daily 6 AM email with weather, weather-risk phases, project status, invoices, overnight email alerts. Live under launchd.
2. ~~**Change Order agent**~~ — SHIPPED: `hb-change` parses NL → assigns CO# → creates Drive doc → updates Cost Tracker col C → adjusts schedule → drafts client approval email. (~$0.04/run)
3. ~~**Procurement alert system**~~ — SHIPPED: auto-fires inside `hb-update` after every schedule change. 22 material categories, tunable lead times in `config.py`, macOS notification + "Procurement Alerts" tab in Tracker. ($0/run)
4. ~~**Client update emails**~~ — SHIPPED: `hb-client-update --to EMAIL --client-name "Name"` generates homeowner email via Sonnet, creates Gmail draft by default. Add --send to auto-send. (~$0.02/run)
5. ~~**Inspection/permit tracker**~~ — SHIPPED: `hb-inspect` shows permit health + 180-day countdown; `hb-inspect log "..."` logs events via Haiku NL parse; morning brief includes permit expiry warnings. 12-step Baldwin County sequence built-in.
6. ~~**Daily site log**~~ — SHIPPED: `hb-log "..."` appends timestamped entry to per-project Drive doc. `--view` opens in browser, `--tail N` prints last N entries. Append-only, verbatim text — preserves legal record integrity.
7. ~~**Lien waiver tracker**~~ — SHIPPED: `hb-waiver` cross-refs Actuals Log to Lien Waivers tab; flags unwaived payments >$500. `hb-waiver log "..."` records signed waivers via Haiku NL parse. Morning brief includes unwaived count.
8. ~~**Scheduling engine**~~ — SHIPPED v1: backwards-scheduler from phase durations + lead times, 13 phase templates, 4 of 6 view-models live (master/daily/weekly/monthly), Postgres adapter, Drive→Postgres bridge, write-loop reconcile. Real Chad data flowing (Whitfield Residence). 2 V2 view-models stubbed (checklist-gates, notification-feed) pending Checklist + Event store. Spec: [`docs/specs/scheduling-engine.md`](docs/specs/scheduling-engine.md). Sample inputs in [`samples/`](samples/README.md). See "Phase 2 shipped" table above.
9. **Desktop Renderer** — un-parked 2026-05-07. Native macOS SwiftUI app cohabiting in `~/Projects/patton-ai-ios/PattonAIShell.xcodeproj` (new `PattonAIShellMac` target alongside iOS). Renders the same six engine view-models the iOS app does, with desktop-class affordances: full Canvas-based Gantt, monthly Earned-Time view, 24-phase checklist authoring (Precon's 44-item template as the structural model), notification feed, drop-spec onboarding, multi-window, keyboard shortcuts, Cmd+/ → `hb-chad` pop-over. Form factor decided: native Mac SwiftUI, NOT Catalyst / NOT Electron / NOT web. Spec: [`docs/specs/desktop-renderer.md`](docs/specs/desktop-renderer.md). Build path: research (3d) → engine V2 view-models (5d, in this repo) → design (5d) → build (14d) → test (5d) → ship (3d) = ~5 weeks. Two prerequisites in this repo: Checklist entity + Event store wiring to make the V2 `checklist-gates` and `notification-feed` view-models return real data.
10. **Mobile access** — truck-cab variant of item 9: same scheduling engine, smaller surface, field-shaped inputs. Primary mobile payload is the daily view — what's happening where today, drop-dead reminders, and tap-to-update inputs Chad uses from the field (sub on-site Y/N, material delivered Y/N, inspection result). Secondary is the weekly view as a look-ahead. The master build-out schedule, monthly view, and full checklist authoring are desktop-class only — mobile renders them read-only at most. Push notifications are a first-class channel (drop-dead dates approaching, weather alerts, sub no-show, material no-show, inspection results) and must reach Chad's phone even when the desktop app is closed. Open questions: native iOS app vs PWA (Connor is a frontend beginner — PWA roadmap already lives in memory); push-notification provider (APNs direct, Firebase Cloud Messaging, OneSignal); offline behavior when Chad has no signal at a job site (queue inputs, last-known schedule cached locally, sync on reconnect). AppSheet is the fastest interim path (wraps the existing Tracker sheet natively) — viable as a stepping stone before the engine-backed renderer is ready.
11. ~~**Supplier-email watcher**~~ — SHIPPED v1: hooks into the existing inbox-watcher (5-min poll). Heuristic gate (`classifiers/supplier_email.is_supplier_email`) on sender domain + subject signals, then Haiku extraction (`extract_supplier_data`) of vendor / action_type / items / ETA / severity. Emits Events into `home_builder.event` with `source='supplier-email-watcher'` and the appropriate type from `EventType` (eta-change | backorder-detected | stock-change | price-change). Severity ≥ warning fires a macOS notification. ~$0.003 per supplier email. Cross-reference: [`docs/specs/vendor-intelligence-system.md`](docs/specs/vendor-intelligence-system.md). The Vendor + VendorItem upsert path is a follow-on commit when Vendor Intelligence's full schema usage spec lands.
12. **Bid/estimate generator** — feed in new client project scope, pull from Whitfield allowances and cost history, generate preliminary estimate using Chad's actual cost data. Blocked on Chad sharing project cost history.
13. **Code update watcher** — periodically scan each Baldwin County municipality's official website URLs (stored in each compliance guide) for code amendments; notify Chad when something changes. Municipality URLs are already documented in each Drive compliance doc.
14. **Knowledge base validation by Chad** — three categories where research couldn't verify his preferred suppliers
15. **Pricing model** — one-time setup vs subscription vs per-project license
16. **Tracker integration spec** (potential follow-on, not yet scheduled) — Tracker integration is named as a layer in [`docs/specs/canonical-data-model.md` § State ownership boundaries](docs/specs/canonical-data-model.md#state-ownership-boundaries) but has no dedicated spec. Today it's diffused across `integrations/sheets.py` and `watchers/dashboard.py`. If the relational-engine + Sheets-bridge approach in [`canonical-data-model.md` § Schedule persistence strategy](docs/specs/canonical-data-model.md#schedule-persistence-strategy) is adopted, Tracker integration becomes load-bearing — it owns the reconcile pass that ingests Chad's Sheet edits as UserActions and the render pass that writes engine projections back out for human-facing display. Spec dependency: persistence decision (engine state in SQLite vs Postgres vs Sheets-as-store). Schedule once that decision is made.
17. **Chad Agent — channel routing (build step 4)** — wire `hb-chad` into the iOS Ask tab (replaces direct `hb-ask` call) and into the morning-brief composer (replaces hand-rolled prompt). Steps 1–3 (chad_voice extraction, chad_context loaders, agent shell) are SHIPPED. Steps 4–5 are queued; step 5 (long-term memory via `user_signal` / `user_profile`) is blocked on migration 004 cut. Spec: [`docs/specs/chad-agent.md`](docs/specs/chad-agent.md).

> Note: Phase 2 item 11 (Supplier-email watcher) and the Phase 3 Vendor Intelligence System ingest vendor data via different paths — email parse vs catalog scrape — and both feed the same normalized schema. The watcher is the V1 path: it lights up Vendor Intelligence with live data before the scrapers exist, and keeps running alongside them once they ship. See `docs/specs/vendor-intelligence-system.md`.
>
> Note: Phase 2 item 8 (Scheduling engine) is the consumer of vendor lead times produced by the Phase 3 Vendor Intelligence System. Until the vendor system is live, the scheduling engine falls back to the category lead-time defaults captured in `docs/specs/vendor-intelligence-system.md` § Lead-Time + Drop-Dead Date Logic.
>
> Note: Phase 2 items 9 (Chad UX non-Terminal) and 10 (Mobile access) are not two products — they are two surfaces of the same scheduling-engine output. Daily / weekly / monthly views, master schedule, checklist gates, notification feed: every artifact rendered on either surface comes from the same engine. Designing them in isolation duplicates work and creates drift between desktop and phone. Spec the engine's view-model — the JSON shape that powers each view — first, then build desktop and mobile as renderers over it. The split between surfaces is layout and input affordances, not data.
>
> Note: dependency map across the data-and-rendering stack. Build order should respect this graph; surfaces above only get real once the layer below is in place (or its fallback is good enough).
>
> ```
> Vendor Intelligence System  (Phase 3 anchor — normalized SKU/price/lead-time/stock/distance)
>   ├── fed by:        Supplier-email watcher  (Phase 2 item 11, V1 path)
>   │                   └── augmented later by catalog scrapers (Phase 3 V2+)
>   └── consumed by:   Scheduling Engine        (Phase 2 item 8)
>                        ├── desktop renderer:  Chad UX non-Terminal (item 9)
>                        └── mobile renderer:   Mobile Access        (item 10)
> ```
>
> Reading the graph: Vendor Intelligence is the source-of-truth for vendor data. The Supplier-email watcher writes lead-time and availability updates into Vendor Intelligence's normalized schema in V1; until that schema is live, the watcher writes to `KNOWLEDGE BASE/baldwin_county_supplier_research.md` as a fallback. The Scheduling Engine consumes Vendor Intelligence's lead times to compute drop-dead order dates; until Vendor Intelligence ships, it falls back to Chad's category-default lead-time table. Chad UX (desktop) and Mobile Access are two renderers over the same Scheduling Engine view-model — they depend on the engine, not on each other, and the split between them is layout and input affordances, not data.
>
> Note: the shared object contract that prevents drift across every layer in the dependency map above lives in [`docs/specs/canonical-data-model.md`](docs/specs/canonical-data-model.md). It defines the 17 cross-cutting entities, the state-ownership boundaries per layer (who owns truth, who reads, who mutates via the engine), the Event/Notification model, the schedule-persistence strategy, the desktop+mobile view-model contract, and the future-proofing seams (overlap, critical path, multi-tenant, offline mobile sync). Read it before extending any layer; every layer's spec consumes it.

## Phase 3 backlog

1. **Vendor Intelligence System** (anchor) — scrape every vendor's catalog → normalize into a comparable schema → recommend the best buy for any product (price / lead time / stock / distance / Chad's preferences) with plain-English rationale. Productizes across Patton AI customers; positioned as Tier 2/3 anchor feature in pricing. Spec: [`docs/specs/vendor-intelligence-system.md`](docs/specs/vendor-intelligence-system.md).

## Architectural decisions (load-bearing)

- **Active vs on-demand split.** Watch-the-world agents run on schedules (launchd polling). Intent-driven agents stay scripts the user invokes. Don't promote a script to a watcher unless polling actually pays off.
- **Idempotent file naming.** One Timeline doc + one Tracker sheet per project. Re-runs archive to `GENERATED TIMELINES/ARCHIVE/`, never duplicate.
- **Two Google identities.** iCloud account for Drive/Docs/Sheets, Gmail account for inbox-touching agents. Test inbox is `aiwithconnor@gmail.com`; production will be Chad's.
- **Knowledge bases as runtime files.** Three Markdown files in Drive `KNOWLEDGE BASE/`. Edit them, next agent run picks up changes — no code change needed.
- **Caching scoped to where it pays.** Sonnet prompt-caching is currently OFF for the timeline generator (10-min runtime > 5-min TTL means consecutive runs always cache-miss). Will be ON for Gmail watcher when that lands (call frequency fits TTL).
- **Chad voice is one module.** `core/chad_voice.py` is the single source of truth — `chad_voice_system("narrator")` for agent-speaks-TO-Chad (briefs, alerts), `chad_voice_system("author")` for agent-speaks-AS-Chad (homeowner emails, vendor drafts). All future agents that need voice import from there; never re-roll prompts in agent files.
- **Engine state owns truth, specialists are tools.** Per [`docs/specs/chad-agent.md`](docs/specs/chad-agent.md): the 22 specialists are *tools* the master agent (`hb-chad`) composes; they are not the product. The product surface for the iOS shell is `hb-chad` — Chad's AI extension — speaking in his voice with `chad_context` grounding from the engine. Specialist CLIs stay callable directly (Connor / Chad in Terminal) but the canonical surface is Chad-the-master.
- **Heartbeat + dead-man's switch.** Every launchd job decorates `main()` with `core.heartbeat.beat_on_success` and the watchdog (`watchers/watchdog.py`, every 10 min) alerts on staleness. This is the floor under "we can prove this thing is alive in production"; without it, every demo is one silent failure away from collapse.
- **Structured JSON logs from launchd jobs.** Stdlib-only: `observability/json_log.py` installs a `JsonFormatter` on the root logger when stderr is non-interactive. Every entry-point's `main()` calls `configure_json_logging("hb-<slug>")`; `logger.info(..., extra={"event": "...", "correlation_id": ...})` emits one schema-stable JSON line per record to `/tmp/<job>.stderr.log`. Legacy `print()` output keeps going to `.stdout.log` for human eyeballs. Schema in `observability/json_log.py` docstring.

## Conventions for code in this repo

- **All paths/scopes/models live in `config.py`.** Don't hardcode them anywhere else. If a value needs to change, change it in one place.
- **Agents own business logic; integrations own API shape.** A new agent should be ~150 lines, mostly prompt construction + a `main()` that orchestrates calls to `core/` and `integrations/`. If an agent file gets long, look for what should move to integrations.
- **Functions named after what they DO, not which agent they came from.** `find_latest_tracker` lives in `integrations/drive.py` — used by 3 agents. `compute_dashboard_metrics` lives in `integrations/sheets.py` — used by 2 agents + the watcher.
- **Cost reporting at the end of every Claude-touching run.** Use `core/claude_client.sonnet_cost()` / `haiku_cost()` so the per-call USD line is consistent across agents.
- **Best-effort formatting steps.** Doc/Sheet formatting passes (`apply_doc_formatting`, `apply_visual_formatting`) wrap in try/except and continue. Don't lose the upload over a styling failure.
- **Watchers use fire-and-exit, not long-running loops.** launchd handles scheduling. State is JSON in the project root.

## How to add a new agent

1. Drop `agents/my_new_agent.py` with imports from `home_builder_agent.config`, `home_builder_agent.core.*`, `home_builder_agent.integrations.*`. Add a `main()`.
2. Add a `[project.scripts]` entry to `pyproject.toml`: `hb-mything = "home_builder_agent.agents.my_new_agent:main"`
3. `pip install -e .` to register the new shell command.
4. Update this CLAUDE.md table of agents above.
5. If it needs a new integration (e.g. SMS via Twilio), add a new file under `integrations/`.

## How to add a new watcher (Phase 2 pattern)

1. Drop `watchers/my_thing.py`, model it on `watchers/dashboard.py` (state file, log, fire-and-exit, signal-based timeout).
2. Add a `.plist` under the project root mirroring the dashboard plist (different Label, different ProgramArguments target).
3. Install via `launchctl load`. Verify via `launchctl list | grep chadhomes`.

## What to avoid

- **Don't break the running watcher.** It's been refreshing dashboards reliably; treat any changes to its code path as breaking-change material. Stop launchd → change → smoke test → reload launchd.
- **Don't commit `.env`, `credentials.json`, `token.json`.** They're gitignored, but double-check `git status` before committing.
- **Don't put Anthropic prompts in `core/`.** Prompts are agent-specific (Chad voice, classification rules, etc.). Keep them in the agent file that uses them.
- **Don't create a new `find_folder_*` or `get_credentials` function.** Use the ones in `integrations/drive.py` / `core/auth.py`. The whole point of the package was to kill those duplicates.
- **Smoke-test `ensure_*_tab` against a FRESH tracker, not an existing one.** These functions early-return if the tab already exists, which means the create-and-style code path never runs against existing trackers. Three latent foregroundColor-placement bugs in `ensure_inspections_tab` / `ensure_procurement_tab` / `ensure_project_info_tab` lived undetected for sessions because the tabs got created on existing trackers in past sessions. The fix surfaced only when `--from-tracker` triggered tab creation on a tracker that didn't have one yet. Pattern: when adding a new ensure tab, run `hb-timeline` against a throwaway spec → exercise the new tab on that fresh tracker → verify creation succeeds before merging.
- **After merging new console_scripts, re-run `pip install -e . --break-system-packages`** on the system Python (`/Library/Frameworks/Python.framework/Versions/3.14/bin/pip3`). Editable install only materializes new entry points at install time. Without this, launchd plists pointing at the new binaries fail with exit 126 ("command not found") even though the Python module exists. Verify with `ls /Library/Frameworks/Python.framework/Versions/3.14/bin/hb-*` after every merge that adds CLIs.

## Useful commands during development

```bash
# Run any agent end-to-end
hb-timeline pelican_point.md
hb-update "Phase 3 pushed 1 week"
hb-dashboard
hb-inbox --days 14
hb-schedule "Project Name" --target-completion 2026-12-15      # in-memory compute
hb-schedule "Project Name" --from-postgres --view master       # load from Supabase
hb-schedule --ping-db                                          # smoke test the DB connection

# PYTHONPATH quirk on Python 3.14 + editable install:
# If `hb-schedule` (or any other hb-* console_script) reports
# "ModuleNotFoundError: No module named 'home_builder_agent'", the
# editable-install .pth file isn't being discovered by your interpreter.
# Workaround:
#   PYTHONPATH=~/Projects/home-builder-agent hb-schedule ...
# Or invoke via -m which always finds the package:
#   python3 -m home_builder_agent.agents.schedule_agent ...

# Morning brief
hb-brief --dry-run          # preview without sending
hb-brief                    # send now (also runs automatically at 6 AM via launchd)
BRIEF_LAT=30.5 BRIEF_LNG=-87.9 hb-brief --dry-run   # custom job-site coordinates

# Watcher health
launchctl list | grep chadhomes
tail -f ~/Projects/home-builder-agent/watcher.log
tail -f /tmp/dashboard-watcher.stderr.log

# Reload watcher after editing watchers/dashboard.py or its dependencies
launchctl unload ~/Library/LaunchAgents/com.chadhomes.dashboard-watcher.plist
launchctl load ~/Library/LaunchAgents/com.chadhomes.dashboard-watcher.plist

# After editing pyproject.toml [project.scripts]
pip install -e . --break-system-packages

# Git
git status
git log --oneline -10
git diff HEAD
```

## Multi-tenant telemetry (binding — see patton-os ADR 2026-05-09)

This repo's worker agents (`inbox_watcher`, morning brief, watchers) run on
the same Mac Mini as the iOS-facing backend and share the same Supabase
project. Per *Multi-Tenant Telemetry Architecture* in
`~/Projects/patton-os/data/decisions.md`:

- **Single canonical event log:** `platform.event` table.
- **tenant_id** for this worker's events: `chad_homes` (set via
  `PATTON_TENANT_ID` env var in launchd plists or shell).
- **Phase 3 instrumentation TODO:** `inbox_watcher` should emit
  `agent.inbox_reply_drafted` and `agent.inbox_reply_sent`; the morning
  brief job should emit `agent.morning_brief_sent`; the watchdog should
  emit `agent.alert_paged`. Helper pattern mirrors
  `~/Projects/patton-ai-ios/backend/app/services/telemetry.py`.
- **The `home_builder.user_action` table is the historical source.** The
  iOS backend emits `client.submitted_quick_action` events into
  `platform.event` going forward; the existing `user_action` table is
  preserved as the engine's polymorphic action queue (different concern).
- **To check telemetry of this client:**
  `patton-coo client-usage chad_homes --days 7`

## Open questions for future sessions

- Is `compute_dashboard_metrics()` the right home in `integrations/sheets.py`, or should it move to `core/dashboard.py` once the metrics get more involved?
- When the supplier-email watcher lands, where does shared "watcher harness" code go? (`watchers/_base.py`?)
- Should we move to `uv` for dependency management instead of pip+pyproject?
