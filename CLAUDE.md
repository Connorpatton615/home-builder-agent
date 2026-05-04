# CLAUDE.md — Home Builder Agent

This file is what Claude Code reads at session start to understand the project.
Update it as the architecture changes; future sessions are only as smart as
this file plus the code itself.

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
| Client update email | `agents/client_update_agent.py` | `hb-client-update --to EMAIL --client-name "Name" [--send] [--dry-run]` | Weekly homeowner project summary in Chad's voice — reads schedule + COs → Sonnet writes polished email → Gmail draft by default (--send to auto-send). (~$0.02–0.03/run) |
| Inspection tracker | `agents/inspection_tracker.py` | `hb-inspect` / `hb-inspect log "<NL>"` | Baldwin County 12-step inspection sequence tracker + 180-day permit expiry countdown. NL logging via Haiku. macOS notification at 150/165 days. Hooks into morning brief. (~$0.005/log, $0/status) |
| Site log | `agents/site_log_agent.py` | `hb-log "<entry>"` / `--view` / `--tail [N]` | Timestamped append-only site log per project; lives in Drive `Site Logs/<Project> — Site Log`. No Claude rephrasing — Chad's actual words preserved verbatim for legal record integrity. ($0/run) |
| Lien waiver tracker | `agents/lien_waiver_agent.py` | `hb-waiver` / `hb-waiver log "<NL>"` | Cross-references Cost Tracker Actuals Log against signed waivers; flags payments >$500 missing a waiver (lien risk). NL log via Haiku. Hooks into morning brief. (~$0.005/log, $0/status) |

Active background processes:
- **Dashboard watcher** (`watchers/dashboard.py`) — runs every 60s via launchd. Polls GENERATED TIMELINES for modified Tracker sheets, refreshes their Dashboard tab. State in `.watcher_state.json`. Logs to `watcher.log`. Plist: `~/Library/LaunchAgents/com.chadhomes.dashboard-watcher.plist`.
- **Inbox watcher** (`watchers/inbox.py`) — runs every 5 min via launchd. Polls Gmail for new INBOX messages since the last historyId, classifies via Haiku (using `classifiers/email.classify_thread`), fires a macOS notification on `urgency=high` hits. Also detects invoices via `classifiers/invoice.is_invoice_email` + `extract_invoice_data` and logs them to the Cost Tracker Invoices tab automatically. State in `.inbox_watcher_state.json`. Logs to `inbox_watcher.log`. Plist: `~/Library/LaunchAgents/com.chadhomes.inbox-watcher.plist`.
- **Morning brief** (`agents/morning_brief.py`) — runs at 6:00 AM daily via launchd. Fetches NOAA weather for job site, reads project Tracker + Cost Tracker, checks inbox watcher log for overnight high-urgency emails, composes and sends Chad a daily brief email via Gmail API. Plist: `~/Library/LaunchAgents/com.chadhomes.morning-brief.plist`.

## Phase 2 backlog (in priority order)

1. ~~**Morning brief**~~ — SHIPPED: `hb-brief` sends daily 6 AM email with weather, weather-risk phases, project status, invoices, overnight email alerts. Live under launchd.
2. ~~**Change Order agent**~~ — SHIPPED: `hb-change` parses NL → assigns CO# → creates Drive doc → updates Cost Tracker col C → adjusts schedule → drafts client approval email. (~$0.04/run)
3. ~~**Procurement alert system**~~ — SHIPPED: auto-fires inside `hb-update` after every schedule change. 22 material categories, tunable lead times in `config.py`, macOS notification + "Procurement Alerts" tab in Tracker. ($0/run)
4. ~~**Client update emails**~~ — SHIPPED: `hb-client-update --to EMAIL --client-name "Name"` generates homeowner email via Sonnet, creates Gmail draft by default. Add --send to auto-send. (~$0.02/run)
5. ~~**Inspection/permit tracker**~~ — SHIPPED: `hb-inspect` shows permit health + 180-day countdown; `hb-inspect log "..."` logs events via Haiku NL parse; morning brief includes permit expiry warnings. 12-step Baldwin County sequence built-in.
6. ~~**Daily site log**~~ — SHIPPED: `hb-log "..."` appends timestamped entry to per-project Drive doc. `--view` opens in browser, `--tail N` prints last N entries. Append-only, verbatim text — preserves legal record integrity.
7. ~~**Lien waiver tracker**~~ — SHIPPED: `hb-waiver` cross-refs Actuals Log to Lien Waivers tab; flags unwaived payments >$500. `hb-waiver log "..."` records signed waivers via Haiku NL parse. Morning brief includes unwaived count.
8. **Scheduling engine** — backwards-scheduler from phase durations + vendor lead times → master build-out schedule with drop-dead order dates + 24-phase checklist gates + daily/weekly/monthly views. Spec: [`docs/specs/scheduling-engine.md`](docs/specs/scheduling-engine.md). Sample inputs in [`samples/`](samples/README.md).
9. **Chad UX (non-Terminal)** — primary rendering surface for the scheduling engine's outputs. Renders: the master build-out schedule (Gantt or equivalent timeline view); the daily view (today's activity per active project — tasks, deliveries, installs, inspections, drop-dead dates hitting today); the weekly view (drop-dead dates this week, milestone meetings, scheduled tasks per job); the monthly view (delivery dates, install dates, inspections, drop-dead dates for open selections, % completion vs plan); the per-phase checklist gates (24 checklists with Precon's 44-item / 10-category template as the model); and the live notification feed (selection deadlines approaching, weather impact, sub no-show, material no-show, inspection failure / re-inspect). Also covers the input side: drop-spec-into-folder onboarding plus email/text notification when output is ready. UI tech is an open question (web app vs Electron desktop vs spreadsheet-bridge add-on) and is decided jointly with item 10 since the mobile surface constrains the choice. Data model: [`docs/specs/scheduling-engine.md`](docs/specs/scheduling-engine.md).
10. **Mobile access** — truck-cab variant of item 9: same scheduling engine, smaller surface, field-shaped inputs. Primary mobile payload is the daily view — what's happening where today, drop-dead reminders, and tap-to-update inputs Chad uses from the field (sub on-site Y/N, material delivered Y/N, inspection result). Secondary is the weekly view as a look-ahead. The master build-out schedule, monthly view, and full checklist authoring are desktop-class only — mobile renders them read-only at most. Push notifications are a first-class channel (drop-dead dates approaching, weather alerts, sub no-show, material no-show, inspection results) and must reach Chad's phone even when the desktop app is closed. Open questions: native iOS app vs PWA (Connor is a frontend beginner — PWA roadmap already lives in memory); push-notification provider (APNs direct, Firebase Cloud Messaging, OneSignal); offline behavior when Chad has no signal at a job site (queue inputs, last-known schedule cached locally, sync on reconnect). AppSheet is the fastest interim path (wraps the existing Tracker sheet natively) — viable as a stepping stone before the engine-backed renderer is ready.
11. **Supplier-email watcher** — V1 feeder for the Vendor Intelligence System. Parses inbound supplier emails (order acknowledgements, ship notifications, ETA updates, backorder notices) and writes structured lead-time + availability updates into Vendor Intelligence's normalized product schema. Designed to work *before* the catalog scrapers are built (so Vendor Intelligence has live signal on day one) and continue to run alongside them after — supplier emails often carry fresher per-order ETAs than vendor catalog pages. Fallback path: until the Vendor Intelligence schema is live, the watcher's keyword-driven writes land in `KNOWLEDGE BASE/baldwin_county_supplier_research.md` as today. Cross-reference: [`docs/specs/vendor-intelligence-system.md`](docs/specs/vendor-intelligence-system.md).
12. **Bid/estimate generator** — feed in new client project scope, pull from Whitfield allowances and cost history, generate preliminary estimate using Chad's actual cost data. Blocked on Chad sharing project cost history.
13. **Code update watcher** — periodically scan each Baldwin County municipality's official website URLs (stored in each compliance guide) for code amendments; notify Chad when something changes. Municipality URLs are already documented in each Drive compliance doc.
14. **Knowledge base validation by Chad** — three categories where research couldn't verify his preferred suppliers
15. **Pricing model** — one-time setup vs subscription vs per-project license
16. **Tracker integration spec** (potential follow-on, not yet scheduled) — Tracker integration is named as a layer in [`docs/specs/canonical-data-model.md` § State ownership boundaries](docs/specs/canonical-data-model.md#state-ownership-boundaries) but has no dedicated spec. Today it's diffused across `integrations/sheets.py` and `watchers/dashboard.py`. If the relational-engine + Sheets-bridge approach in [`canonical-data-model.md` § Schedule persistence strategy](docs/specs/canonical-data-model.md#schedule-persistence-strategy) is adopted, Tracker integration becomes load-bearing — it owns the reconcile pass that ingests Chad's Sheet edits as UserActions and the render pass that writes engine projections back out for human-facing display. Spec dependency: persistence decision (engine state in SQLite vs Postgres vs Sheets-as-store). Schedule once that decision is made.

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

## Useful commands during development

```bash
# Run any agent end-to-end
hb-timeline pelican_point.md
hb-update "Phase 3 pushed 1 week"
hb-dashboard
hb-inbox --days 14

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

## Open questions for future sessions

- Is `compute_dashboard_metrics()` the right home in `integrations/sheets.py`, or should it move to `core/dashboard.py` once the metrics get more involved?
- When the supplier-email watcher lands, where does shared "watcher harness" code go? (`watchers/_base.py`?)
- Should we move to `uv` for dependency management instead of pip+pyproject?
