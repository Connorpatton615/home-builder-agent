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
4. ~~**Inspection/permit tracker**~~ — SHIPPED: `hb-inspect` shows permit health + 180-day countdown; `hb-inspect log "..."` logs events via Haiku NL parse; morning brief includes permit expiry warnings. 12-step Baldwin County sequence built-in.
5. ~~**Daily site log**~~ — SHIPPED: `hb-log "..."` appends timestamped entry to per-project Drive doc. `--view` opens in browser, `--tail N` prints last N entries. Append-only, verbatim text — preserves legal record integrity.
6. ~~**Lien waiver tracker**~~ — SHIPPED: `hb-waiver` cross-refs Actuals Log to Lien Waivers tab; flags unwaived payments >$500. `hb-waiver log "..."` records signed waivers via Haiku NL parse. Morning brief includes unwaived count.
7. **Bid/estimate generator** — feed in new client project scope, pull from Whitfield allowances and cost history, generate preliminary estimate using Chad's actual cost data.
8. **Code update watcher** — periodically scan each Baldwin County municipality's official website URLs (stored in each compliance guide) for code amendments; notify Chad when something changes. Municipality URLs are already documented in each Drive compliance doc.
9. **Supplier-email watcher** — scan supplier emails → auto-update `KNOWLEDGE BASE/baldwin_county_supplier_research.md`
10. **Mobile access** — Chad interacts from phone on a job site (AppSheet reads Tracker sheet natively — fastest path)
11. **Knowledge base validation by Chad** — three categories where research couldn't verify his preferred suppliers
12. **Pricing model** — one-time setup vs subscription vs per-project license

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
