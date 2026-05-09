# Chad cutover runbook

> The end-to-end playbook for moving the home-builder agent from
> Connor's dev environment (aiwithconnor@gmail.com + Connor's Drive +
> Connor's Mac mini) to Chad's live operating environment (Chad's
> Gmail + Chad's Drive + Chad's hardware-or-tunnel of choice). Walk
> this top-to-bottom on the day we go live; every step has a
> verification check. Rollback notes at the bottom.
>
> **Status:** Draft, ready to execute. Author: Claude (2026-05-09).
> **Audience:** Connor (with any Claude session). Not customer-facing.
> **Estimated execution time end-to-end:** 4-6 hours, single sitting.

---

## What "cutover" actually means

The system today reads from + writes to:

- `aiwithconnor@gmail.com` — for inbox classification, draft creation,
  and outgoing email (morning brief, weekly client update, change-order
  approvals, vendor replies)
- A Drive folder hierarchy under Connor's account — Tracker, Cost
  Tracker, Site Logs, Knowledge Bases, Generated Timelines, Change
  Orders, Receipts
- A Supabase Postgres database where Whitfield is `home_builder.project`
  row UUID `976d146d-dd1e-4a88-9022-158f9e348010`
- `~/Projects/home-builder-agent/.env` with Connor's API keys

After cutover, the system reads from + writes to:

- Chad's Gmail account
- Chad's Drive folder hierarchy (recreated under his account, with
  the same shape)
- The same Supabase project (Whitfield's UUID stays; only its
  `drive_folder_id` field updates)
- The same `.env` (Anthropic key + DATABASE_URL stay; Google
  credentials regenerate)

The Whitfield project's UUID is stable across the cutover. Anything
already persisted in Postgres (phases, drop-deads, events, draft
actions, etc.) carries forward unchanged. Only the bridge and the
inbox watcher need to point at new places.

---

## Pre-cutover — gather these from Chad before you start

**You cannot do steps below without these.** If they're not in hand,
schedule the cutover for the day after Chad replies.

- [ ] Chad's Gmail account address (for the OAuth flow)
- [ ] Chad's preferred sender display name + email signature block —
      used by every outgoing message in `chad_voice("author")` mode
- [ ] Chad's actual cell phone (the one that should appear on
      homeowner emails)
- [ ] Chad's Whitfield homeowner email (currently a placeholder —
      `CO_CLIENT_EMAIL` in config.py)
- [ ] Chad's permission to enable the Gmail/Drive scopes the agent
      needs (he'll see them at OAuth consent time)
- [ ] Confirmation Chad has reviewed + redlined the three knowledge
      bases (subs.md, suppliers.md, code_compliance.md) and the
      chad_voice rules (NARRATOR_RULES + AUTHOR_RULES). See § Knowledge
      base redline cycle below — this can run in parallel with
      provisioning but must be done before launchd jobs go live.

---

## Step 1 — Provision Chad's Drive folder structure (45 min)

Chad's Drive needs to mirror the structure Connor's account has today.
Either Chad creates the folders himself following a template Connor
provides, or Connor (with Chad's account access) creates them
programmatically.

**Recommended path:** programmatic creation using a one-time
`hb-provision` CLI we ship for this. Until that exists, manual
creation per the template below.

### Folder template (under "My Drive" or a designated parent)

```
PALMETTO CUSTOM HOMES/
├── KNOWLEDGE BASE/
│   ├── subs.md            ← Chad-redlined version
│   ├── suppliers.md       ← Chad-redlined version
│   └── code_compliance.md ← Chad-redlined version
├── GENERATED TIMELINES/
│   ├── ARCHIVE/           ← Auto-rotated old trackers go here
│   └── (Tracker — <Project Name> — YYYY-MM-DD)  ← Active project
├── Site Logs/
│   ├── <Project Name>/
│   │   ├── <Project Name> — Site Log (Doc, append-only)
│   │   └── Receipts/      ← Receipt photos auto-saved here
└── Chad's Finance Office/
    └── Cost Tracker — <Project Name> (Sheet, 21 sections)
        Tabs: Sections, Allowances, Actuals Log, Invoices, Lien Waivers,
              Inspections, Procurement Alerts, Project Info, Change Orders,
              Finance Summary
```

### Migration of existing Whitfield artifacts

The Whitfield Tracker, Cost Tracker, prior Change Orders, and existing
site log content live on Connor's Drive today. They need to either:

- **Be copied to Chad's Drive** preserving content (preferred — Chad
  sees his actual project state from Day 1)
- **Or be regenerated** via `hb-timeline pelican_point.md` against
  Chad's Drive (loses prior CO history; not preferred)

Recommended: download/upload via Drive's web UI. Each artifact is a
single Sheet or Doc; takes <5 min total. The drive_folder_id changes,
but the file *content* is preserved.

### Verification

```bash
# After folder structure exists in Chad's Drive:
hb-bridge "Whitfield" --dry-run
# Expected: shows the new drive_folder_id will replace the old one,
# does NOT actually write yet (--dry-run).
```

---

## Step 2 — Re-auth: regenerate credentials.json + token.json (30 min)

Today's `~/Projects/home-builder-agent/credentials.json` is a Google
OAuth client tied to Connor's GCP project. The `token.json` is the
specific consent for `aiwithconnor@gmail.com`.

For Chad, two paths — pick based on whether you want him on Connor's
GCP project or his own:

### Path A — Chad uses Connor's existing GCP OAuth client (faster)

- Connor's GCP project already has the right OAuth scopes configured
  (Drive, Sheets, Docs, Gmail). Chad just authorizes against it as a
  test user.
- In GCP Console → OAuth consent screen → add Chad's Gmail as a test
  user (assuming the app is in "Testing" mode — confirm this).
- Delete `token.json` from the home-builder-agent repo.
- Run `hb-help "ping"` (or any agent) — triggers a fresh browser
  OAuth flow. Sign in as Chad, grant scopes, token saves.
- Verify `gmail.users().getProfile(userId="me")` returns Chad's email.

### Path B — Chad gets his own GCP project (slower, cleaner long-term)

- Create a new GCP project under Chad's Google Cloud account.
- Enable Drive, Sheets, Docs, Gmail APIs.
- Create OAuth 2.0 client (Desktop app type) → download
  credentials.json.
- Replace `~/Projects/home-builder-agent/credentials.json` with Chad's.
- Delete `token.json`.
- Run an agent → OAuth flow → save token.

**Recommendation:** Path A for the first 30-90 days while we validate
the cutover, then migrate to Path B once Chad is paying. Path A means
Connor stays the OAuth-app owner; if anything goes sideways with
billing/auth, Connor can fix it without locking out Chad.

### Verification

```bash
hb-inbox --days 1 --max-threads 3
# Expected: lists Chad's inbox threads (not Connor's), no auth errors
```

---

## Step 3 — Update `config.py` for Chad's specifics (15 min)

File: `~/Projects/home-builder-agent/home_builder_agent/config.py`

Search-and-replace these. Each is a verifiable line of code with a
clear comment near it:

| Variable | Current value | New value |
|---|---|---|
| `BRIEF_RECIPIENT_EMAIL` | `aiwithconnor@gmail.com` | Chad's actual email |
| `BRIEF_SENDER_NAME` | `Patton AI / Connor Patton` | `Chad Lynch / Palmetto Custom Homes` (verify exact preference) |
| `BRIEF_SITE_LAT` | placeholder | actual Whitfield lat (from Chad) |
| `BRIEF_SITE_LNG` | placeholder | actual Whitfield lng |
| `BRIEF_SITE_ADDRESS` | placeholder | actual Whitfield street address |
| `CO_CLIENT_EMAIL` | placeholder | actual Whitfield homeowner email |
| `FINANCE_PROJECT_NAME` | "Whitfield Residence" | leave (already correct) |
| `DRIVE_FOLDER_PATH` | `"GENERATED TIMELINES"` | leave (path is relative — works under Chad's `PALMETTO CUSTOM HOMES/` parent) |
| `FINANCE_FOLDER_PATH` | `"Chad's Finance Office"` | leave |

### Also update `core/chad_voice.py`

- `CHAD_SIGNATURE_BLOCK` — phone number is currently
  `(251) 555-0100`. Replace with Chad's actual cell.
- `CHAD_SHORT_SIGNATURE_BLOCK` — sanity-check.
- `NARRATOR_RULES` and `AUTHOR_RULES` — already inferred from Chad's
  style. **Chad should review these before going live** (see Knowledge
  base redline cycle below).

### Verification

```bash
hb-brief --dry-run
# Expected: brief composes with Chad's email as recipient, real lat/lng
# in the weather call, real site address, signed in Chad's name.
# Inspect the rendered HTML in the dry-run output.
```

---

## Step 4 — Update the Whitfield project row in Postgres (15 min)

Whitfield's UUID `976d146d-dd1e-4a88-9022-158f9e348010` stays. Only
its Drive references change.

```sql
-- Connect via DATABASE_URL
UPDATE home_builder.project
SET drive_folder_id   = '<Chad''s GENERATED TIMELINES folder id>',
    drive_folder_path = 'PALMETTO CUSTOM HOMES/GENERATED TIMELINES'
WHERE id = '976d146d-dd1e-4a88-9022-158f9e348010';
```

Or just re-run `hb-bridge "Whitfield"` — it's idempotent on
drive_folder_id.

### Wipe transient state from Connor's testing

```bash
cd ~/Projects/home-builder-agent
rm -f .reconcile_watermark.json
rm -f .inbox_watcher_state.json
rm -f .watcher_state.json
rm -f .weather_cache.json
rm -rf .heartbeats/
rm -rf .morning_cache/
```

These all regenerate on next agent run. Wipe is so Chad's first
runs don't inherit Connor's test artifacts (Connor's last-seen
historyId from Gmail, Connor's lat/lng cached forecast, etc.).

### Optional: wipe Connor's smoke-test draft_actions

```sql
-- Today's session inserted + discarded one smoke-test draft. Already
-- cleaned. But if any other test artifacts crept in:
DELETE FROM home_builder.draft_action
WHERE originating_agent = 'hb-smoke-test'
   OR project_id = '976d146d-dd1e-4a88-9022-158f9e348010'
      AND status = 'discarded'
      AND originating_agent NOT IN ('hb-change', 'hb-client-update', 'hb-inbox',
                                     'hb-waiver', 'hb-supplier-email');
```

### Verification

```bash
hb-schedule --ping-db
# Expected: ✅ Postgres reachable, home_builder schema present, 18 tables.

hb-schedule "Whitfield" --from-postgres --view master
# Expected: prints the master view with phases + drop-dead overlay.
```

---

## Step 5 — Knowledge base redline cycle (Chad-side, async — schedule before go-live)

Run this in parallel with Steps 1-4. Chad must complete it before
the launchd jobs go live (Step 7), or the system speaks to him in
inferred-Chad's voice instead of his actual voice.

### What Chad reviews

**File 1: `KNOWLEDGE BASE/subs.md`**
- 30-50 subcontractor profiles drafted from research
- Chad redlines: who he actually uses, who he doesn't, contact info,
  rates, reliability notes
- Estimated time for him: 30-60 min

**File 2: `KNOWLEDGE BASE/suppliers.md`**
- 30-50 supplier profiles (lumber yards, electrical, plumbing,
  cabinet shops, etc.)
- Chad confirms preferred suppliers per category, lead times, terms
- Estimated time: 30-60 min

**File 3: `KNOWLEDGE BASE/code_compliance.md`**
- Baldwin County code requirements per phase
- Chad confirms accuracy from his actual experience
- Estimated time: 30 min

**File 4: `core/chad_voice.py` (NARRATOR_RULES + AUTHOR_RULES)**
- The voice rules driving every Chad-voiced message
- Chad reads + edits the bullet points until they sound like him
- Estimated time: 15 min

### Process

- Connor sends Chad a packet (ideally email with redline-able copies
  attached, or a Doc with comments enabled)
- Chad redlines on his own time (allow 1 week)
- Connor merges redlines into the live files
- Re-deploys (these are runtime files — no code change, just file
  edit and the next agent run picks it up)

### Verification

```bash
hb-brief --dry-run
# Expected: the morning brief reads as Chad-voiced (not generic).
# If it sounds AI-generic, the chad_voice rules need more redline.

hb-client-update --to chad@palmettocustomhomes.com --client-name "Test" --dry-run
# Expected: the homeowner update reads in Chad's warm-not-corporate voice.
```

---

## Step 6 — Reload all launchd jobs against Chad's environment (20 min)

The launchd jobs are user-scoped (`~/Library/LaunchAgents/`). They
inherit the OAuth tokens + .env automatically.

```bash
# Unload all 8 home-builder jobs:
for job in dashboard-watcher inbox-watcher reconcile morning-brief \
           client-update morning-view notification-triggers watchdog; do
  launchctl unload ~/Library/LaunchAgents/com.chadhomes.$job.plist 2>/dev/null
done

# Verify all are unloaded:
launchctl list | grep chadhomes
# Expected: no rows

# Reload all 8:
for job in dashboard-watcher inbox-watcher reconcile morning-brief \
           client-update morning-view notification-triggers watchdog; do
  launchctl load ~/Library/LaunchAgents/com.chadhomes.$job.plist
done

# Verify all are loaded:
launchctl list | grep chadhomes
# Expected: 8 rows, LastExitStatus column should be "-" (never run yet)
# or "0" (ran successfully)
```

### Smoke fire each long-cadence job once (verify cutover)

```bash
launchctl start com.chadhomes.morning-brief         # email lands in Chad's inbox
launchctl start com.chadhomes.morning-view          # cache file appears
launchctl start com.chadhomes.notification-triggers # drop-dead scan runs

# Wait ~30 seconds, then check:
tail /tmp/morning-brief.stdout.log
tail /tmp/morning-view.stdout.log
ls ~/Projects/home-builder-agent/.morning_cache/
```

The 60-second jobs (reconcile, dashboard-watcher) and 5-min job
(inbox-watcher) will fire on their own.

---

## Step 7 — Patton AI shell account + first launch (45 min, Chad-side)

The Patton AI iOS / Mac app is what Chad opens. Per the morning view
handoff to the platform thread, the renderer is being wired now —
when it ships, Chad's first-launch flow:

1. Chad downloads the iOS app from TestFlight (or Mac DMG when
   distribution lands)
2. Sign in with Apple (or magic-link via Supabase if he prefers email)
3. Supabase user account is provisioned automatically
4. App routes to the Morning surface as the default landing
5. The HTTP route fetches `.morning_cache/<project_id>.json` from the
   server-side cache and renders immediately

### Until the renderer is live (interim)

Chad can use the Terminal-side surface:

```bash
hb-morning Whitfield                 # full payload, terminal-pretty
hb-chad "what should I do today?"    # voice-of-Chad master agent
hb-update "<NL site update>"         # NL schedule update
hb-receipt photo.jpg                 # photo → Cost Tracker
```

These work today. Chad's "morning workstation" experience just lives
in Terminal until the in-app surface ships.

### Verification

```bash
# After Chad signs in to the iOS app:
# 1. Open the Morning tab → renders without spinner
# 2. Tap Approve on a queue item → the row disappears + Gmail draft sends
# 3. Pull to refresh → cache_warming banner appears for ~10s, then
#    fresh payload loads
```

---

## Step 8 — End-to-end smoke test (30 min)

After all 7 steps above, run this checklist. Each item should pass
without Connor intervening.

| # | What | Expected |
|---|---|---|
| 1 | `hb-morning Whitfield` | renders Chad-voiced brief, today's drop-deads, weather |
| 2 | Send a test email TO Chad's inbox tagged with high urgency | within 5 min, hb-inbox classifies it, drafts a reply, draft_action row appears in Postgres |
| 3 | Run `hb-change "test CO: $500 to vendor X for door upgrade"` | CO doc lands in Chad's Drive, Cost Tracker col C updates, Gmail draft to homeowner is created, draft_action row appears |
| 4 | Photo a real receipt → `hb-receipt path/to/photo.jpg` | vendor + amount + category extracted, Cost Tracker Actuals Log updates, photo saved to Drive Receipts |
| 5 | Tomorrow morning at 6:05 AM | `.morning_cache/<id>.json` updated automatically, voice_brief reflects overnight events |
| 6 | Heartbeat watchdog | no staleness alerts after 24h |
| 7 | Verify against the production Supabase | all engine_activity rows have `actor_user_id` = Chad's Supabase UUID |

---

## Knowledge base of subs/vendors — Chad-specific data

Three categories where research couldn't verify Chad's actual
preferences (per CLAUDE.md Phase 2 backlog item 14):

- Foundation contractors / concrete suppliers
- Window vendors (Anderson? Marvin? Pella?)
- Cabinet shops Chad has actually worked with

These need Chad-specific input before the AI can confidently
recommend in vendor-related contexts. Until then, the
category-default lead-time table in `config.py:PROCUREMENT_LEAD_TIMES`
is the V1 floor.

---

## Rollback plan — if cutover goes sideways

If anything in steps 1-7 breaks badly enough that Chad's morning is
broken:

1. **Stop the launchd jobs:**
   ```bash
   for job in dashboard-watcher inbox-watcher reconcile morning-brief \
              client-update morning-view notification-triggers watchdog; do
     launchctl unload ~/Library/LaunchAgents/com.chadhomes.$job.plist
   done
   ```

2. **Restore Connor's `token.json`** from a backup made BEFORE step 2
   (always make this backup first; `cp token.json token.json.pre-cutover`).

3. **Restore Connor's `config.py`** from git:
   ```bash
   cd ~/Projects/home-builder-agent
   git checkout home_builder_agent/config.py
   git checkout home_builder_agent/core/chad_voice.py
   ```

4. **Restore the Postgres project row's drive_folder_id** to its
   pre-cutover value:
   ```sql
   UPDATE home_builder.project
   SET drive_folder_id   = '<Connor''s pre-cutover folder id>',
       drive_folder_path = 'GENERATED TIMELINES'
   WHERE id = '976d146d-dd1e-4a88-9022-158f9e348010';
   ```

5. **Reload launchd** — system back to Connor's-Drive mode.

The data Chad sees in his Drive after rollback is preserved (Drive
files are not deleted on rollback; the agents just stop pointing at
them). Chad won't notice anything until you re-cut over.

---

## What this runbook does NOT cover

- **Patton AI shell auth flow** — the iOS / Mac app's Sign-in-with-Apple
  + Supabase exchange is platform-thread work. When that's wired,
  Chad's first-launch app experience is covered there, not here.
- **Custom URL scheme / DMG distribution** — also platform-thread.
- **Multi-tenant** — single-tenant per ADR 2026-05-07. When the
  second customer signs, that's a different runbook.
- **Push notifications (APNs)** — platform-thread migration 008,
  in flight on a sibling branch. Chad's morning surface works without
  push; push is additive.

---

## Estimated total time, end-to-end

| Step | Owner | Time |
|---|---|---|
| Pre-cutover info gathering | Chad emails Connor | async (1 day round-trip) |
| 1. Drive provisioning | Connor + Chad's account access | 45 min |
| 2. Re-auth | Connor | 30 min |
| 3. Config updates | Connor | 15 min |
| 4. Postgres project row | Connor | 15 min |
| 5. Knowledge base redlines | Chad | async (allow 1 week) |
| 6. Launchd reload + smoke fire | Connor | 20 min |
| 7. iOS shell first launch | Chad + Connor walkthrough | 45 min |
| 8. End-to-end smoke test | Connor | 30 min |
| **Active execution time** | | **~3.5 hours** |
| **Calendar time including Chad's redline cycle** | | **~7-10 days** |

---

## When to execute this runbook

When all of the following are true:

- [ ] Chad has redlined the three knowledge bases + chad_voice rules
- [ ] Chad has provided his Gmail, signature block, cell phone, and
      Whitfield homeowner email
- [ ] The platform thread has shipped the morning view HTTP route
      (otherwise the in-app surface is empty until they ship)
- [ ] Push notifications are at least scaffolded (APNs migration 008
      applied, even if APNs delivery isn't wired) — so the system
      isn't degraded silently
- [ ] You have a 4-hour block on the calendar to walk through Steps
      1-7 in one sitting (Step 8 can be next-day)

If any of those are false, set the cutover date for after they're
true.

---

## After cutover — what changes for the home-builder-agent thread

Future Claude Code sessions reading CLAUDE.md will see the same
environment they see today, except:

- `aiwithconnor@gmail.com` references in code comments are stale —
  refactor to `chad@palmettocustomhomes.com` in the next pass
- `BRIEF_SITE_LAT/LNG/ADDRESS` placeholders are now real values
- The Postgres project row's drive_folder_id is Chad's
- The launchd jobs are still owned by Connor's user — when Chad gets
  his own Mac mini or a hosted runner, lift the jobs there (out of
  scope for V1 cutover)

This runbook lives in the repo as the canonical playbook. Update it
when the second customer cutover happens (it'll need a multi-tenant
overlay).
