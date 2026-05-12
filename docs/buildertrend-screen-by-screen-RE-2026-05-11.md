# Buildertrend iOS — Screen-by-Screen Reverse Engineering

**Date:** 2026-05-11
**Author:** Patton AI competitive intelligence / surgical RE pass
**Audience:** Connor — sequencing decisions for the 8-week build plan
**Anchors:**
- [competitor-research-2026-05-11.md](competitor-research-2026-05-11.md) — Buildertrend deep dive
- [ux-design-overhaul-2026-05-11.md](ux-design-overhaul-2026-05-11.md) — design language v2
- [ROADMAP-2026-05-11.md](ROADMAP-2026-05-11.md) — 8-week sequence
- [ADR-POS-001](../../patton-os/data/decisions.md) — Construction-Turtle positioning (binding do-not-build list)

---

## 1. TL;DR

I mapped every distinct screen the Buildertrend iOS app exposes — 65 of them counting sub-portal and homeowner-portal screens, ~54 builder-facing — surfaced via the [BT help center](https://buildertrend.com/help-article/introduction-to-mobile-navigation/), [App Store listing](https://apps.apple.com/us/app/buildertrend/id504370616), and [Capterra](https://www.capterra.com/p/70092/Buildertrend/reviews/) / [G2](https://www.g2.com/products/buildertrend/reviews) reviews from 2024–2026. Three patterns dominate the surface and form the wedge against the incumbent:

1. **Every Buildertrend screen is a form.** Daily Logs has 9 fields and 4 visibility toggles. A Schedule Item has 13. A Change Order has ~12 plus a Release dialog and a signature pad. Selections is a form-within-a-form (Selection + Choices + Allowances). Our 30 candidate features replace forms with voice + photo + auto-inference — Wave-1 #4 (30-second site walk), #6 (voice schedule moves), #5 (voice change orders) each collapse a 7-screen BT flow into a single voice gesture.
2. **Every screen requires a project picker first.** "Select a Job from the Jobs List, then tap More, then choose X" is the universal navigation prelude. Our **GPS auto-bind** (Week 1) + **photo-as-universal-verb** (Wave-1 #1) eliminate the picker entirely.
3. **Every screen requires accounts and portals.** Subs log in to a Sub Portal; homeowners log in to a Client Portal. The whole architecture assumes a closed multi-user platform — which is precisely why ADR-POS-001 forbids us from building it. SMS subs + Wave-1 #8 (Owner narrative timeline) email replace the entire portal layer.

Of the 65 distinct BT screens mapped below, our 30-feature set covers 50 builder-facing screens with REBUILD/SKIP/KEEP. **4 builder-facing screens have no candidate replacement** — flagged in §5. **10 KEEP-worthy patterns** are noted where BT got the verb right and we should mirror the affordance.

---

## 2. Per-screen RE table

Sorted by Buildertrend's own marketing prominence — the things they put first on [the app marketing page](https://buildertrend.com/app/) and the [navigation help article](https://buildertrend.com/help-article/introduction-to-mobile-navigation/) appear first.

### 2.1 Top-level navigation surface (the universal shell)

| # | Screen | Verb the user actually performs | Pain cited in reviews | Decision | Replacement / rationale |
|---|---|---|---|---|---|
| 1 | **Login** | Type username + password (or SSO) | "every time you get used to a new change some IT person creates an extra step" ([Capterra, 2025](https://www.capterra.com/p/70092/Buildertrend/reviews/)) | KEEP (necessary) | Once-only Sign in with Apple. No subs, no homeowners log in. |
| 2 | **Jobs List (project picker dropdown)** | "Pick which project I'm on right now" | Universal complaint — forced picker on every action. "10x more clicking" ([Capterra](https://www.capterra.com/p/70092/Buildertrend/reviews/)) | REBUILD | Wave-1 #1 + Week 1 GPS geofence auto-bind. App knows where you are. Picker stops being the first tap of the day. |
| 3 | **Summary (Job Summary landing)** | "Glance at project: to-dos, time clock, recent logs, schedule" | Reviewers: KPI grid reads as "a tax form" | REBUILD | Wave-1 #8 + the Field Card ([ux-design-overhaul §4.2](ux-design-overhaul-2026-05-11.md)). One sentence of prose, not a 6-tile grid. |
| 4 | **Notifications (bell icon)** | "What got my attention while away" | "Constant change... moving a box to the other side of the screen" ([Capterra p3](https://www.capterra.com/p/70092/Buildertrend/reviews/?page=3)) | REBUILD | Wave-1 #10 (AI auto-reply triage). Only things that need Chad's eyes surface. |
| 5 | **Activity Feed (real-time stream)** | "What happened across all jobs in 24h" | Information overload — universal pattern | SKIP (tab) / REBUILD (function) | Wave-2 super-side morning packet + end-of-day rollup. Two AI-summarized digests, not a raw stream. |
| 6 | **Quick Add Menu (+ icon overlay)** | "Take a photo OR create a daily log OR create a to-do — fast" | One of the few things BT got right — camera entry at any tap depth. | KEEP (pattern, not chrome) | Mirror as center-tab Capture FAB ([ux-design-overhaul §3.2](ux-design-overhaul-2026-05-11.md)). Right idea, wrong app shell — we make it the literal center of gravity. |
| 7 | **Global Search (magnifying glass)** | "Find a task, comment, document by keyword" | Reviewers don't complain about search — they complain they can't find anything *without* it, which is the indictment. | REBUILD | Ask tab is the search surface. "Show me the framing photos with rebar" (Week 8 in [ROADMAP](ROADMAP-2026-05-11.md)) — conversational, not keyword. |
| 8 | **More tab (hamburger drawer in disguise)** | "Find features not in the bottom 4 tabs — Change Orders, Selections, Warranty, Bills, RFIs, Files, Messages..." | "Hamburger menu in 2026" ([ux-design-overhaul §2.2](ux-design-overhaul-2026-05-11.md)). Settings is 3 taps deep via "More → Settings → About" ([BT docs](https://buildertrend.com/help-article/mobile-app-version/)). | REBUILD | Our "More" = Activity log + Settings only. Selections, Change Orders surface inside the relevant project, not as global modules. |

### 2.2 Project Management features (the daily verbs)

| # | Screen | Verb | Pain | Decision | Replacement / rationale |
|---|---|---|---|---|---|
| 9 | **Daily Logs Dashboard** | "List today's and recent daily logs" | "Trades hate it because it is hard to use on their phones" ([G2, 2025](https://www.g2.com/products/buildertrend/reviews)) | REBUILD | Wave-1 #4 (30-second site walk). List view disappears — walks materialize as Wave-2 super-side morning packet + photo grid. |
| 10 | **Daily Log Create/Edit form** | 9 fields + 4 visibility toggles + draft state | "Inability to autosave the daily log" ([Capterra](https://www.capterra.com/p/70092/Buildertrend/reviews/)). 3–4 taps minimum per [BT docs](https://buildertrend.com/help-article/daily-logs-on-mobile/). | REBUILD | Wave-1 #4. Walk → talk → photos → AI generates log + homeowner update + schedule deltas + punch items in one pass. Form goes to zero. |
| 11 | **Schedule (Month view)** | "See this month as a calendar grid" | "Confusing on a phone" ([Capterra](https://www.capterra.com/p/70092/Buildertrend/reviews/)) — 30 days at 9pt | SKIP | ADR-POS-001 — "no Gantt / scheduling grid." Replaced by Wave-1 #6 (voice moves) + Wave-2 morning packet. |
| 12 | **Schedule (Agenda, default)** | "This week's items in order" | Less hated, closest to useful | KEEP (affordance) | "See what's next" mirrored in Field Card + morning packet. No standalone Agenda screen. |
| 13 | **Schedule (List view)** | "Spreadsheet-style list" | Spreadsheet-on-phone is a usability crime ([ux-design-overhaul §2.2](ux-design-overhaul-2026-05-11.md)) | SKIP | ADR-POS-001 — same as #11. |
| 14 | **Schedule (Gantt on phone)** | "View Gantt-bar dependencies" | The worst phone screen in construction software | SKIP | ADR-POS-001 — explicit "no Gantt." |
| 15 | **Schedule Item Create/Edit form** | 13 fields per [BT docs](https://buildertrend.com/help-article/schedule-on-mobile/): title, assignee, dates, predecessor, phase, color, attachments, visibility | "Constant change... moving a box to the other side of the screen" ([Capterra p3](https://www.capterra.com/p/70092/Buildertrend/reviews/?page=3)) | REBUILD | Wave-1 #6 (voice schedule moves). "Pour Wednesday instead of Tuesday — text Manny." Agent edits schedule + drafts SMS + emits platform.event. |
| 16 | **To-Do's / Punch List** | "Track open punch items, check them off" | "Now you have to click on another tab to get to the created checklist instead of scrolling" ([App Store, 2023, still cited 2025](https://apps.apple.com/us/app/buildertrend/id504370616)) | KEEP (the verb) | Verb is correct. We KEEP it as Phase Checklist pattern (already shipped per [competitor-research §5](competitor-research-2026-05-11.md)) — punch items live inside the project, not a separate dashboard. Backlog: AI-vision punch list from walkthroughs. |
| 17 | **Job Details** | "View address, PMs, job notes, directions" | Inert read-only metadata | KEEP | Project Header card in Project Detail ([ux-design-overhaul §4.3](ux-design-overhaul-2026-05-11.md)). One of the few BT screens that's the right shape. |
| 18 | **Selections Dashboard** | "List all selections for this project" | "Not very user friendly" ([Capterra, 2025](https://www.capterra.com/p/70092/Buildertrend/reviews/)) | REBUILD | [ROADMAP Week 3](ROADMAP-2026-05-11.md) — Selections module (lean), drop-dead-sorted not room-sorted ([ux-design-overhaul §2.2](ux-design-overhaul-2026-05-11.md)). |
| 19 | **Selection Create/Edit form** | Form-within-a-form: title, category, location, deadline, allowance, then nested Choices ([BT docs](https://buildertrend.com/help-article/selections-on-mobile/)) | BT's weakest module for custom builders | REBUILD | Wave-1 #4 voice flow. "Master bath tile, Lowe's, $1200, due Aug 15" → agent fills all 6 fields ([Week 3 criteria](ROADMAP-2026-05-11.md)). |
| 20 | **Selections Choices screen** | "Add choices — flat fee / line items / request-from-vendor" | Three-deep modal stacking; process-by-form, not conversation | REBUILD | Folded into selections voice add (#19). One flow, not three nested forms. |
| 21 | **Allowances screen** | "Manage budgeted amounts linked to selections" | Hidden tab — discoverability problem | REBUILD | Wave-1 #7 (cost guardrails before commit). "$400 over allowance — say 'yes' to flag as upgrade." Becomes a guardrail event, not a screen. |
| 22 | **Change Orders Dashboard** | "List of change orders" | Just a list of pending items | REBUILD | Wave-1 #5 (voice change orders). List becomes Wave-1 #8 homeowner timeline. |
| 23 | **Change Order Create/Edit form** | ~12 fields per [BT docs](https://buildertrend.com/help-article/change-orders-on-mobile/): ID, title, line items, builder cost, client price, 4 description tiers (internal/sub/client), deadline, attachments | "10x more clicking" ([Capterra](https://www.capterra.com/p/70092/Buildertrend/reviews/)). 5–7 taps minimum just to create a basic CO. | REBUILD | Wave-1 #5. "Homeowner wants to upgrade the island to quartzite, add $4,200." Agent drafts full CO with provenance via `.aiAuthored()`. |
| 24 | **Change Order Detail view** | "Review the CO — release / approve / decline / edit" | Cluttered with 7 buttons | KEEP (the verb) | "Review + approve" rendered inside Wave-1 #8 homeowner timeline + Chad's morning packet. No separate detail screen. |
| 25 | **CO Release + Signature pad** | "Send to client + capture finger signature" | Awkward but legally required | KEEP (necessary) | Unavoidable for e-signed legal artifacts. Reached via magic-link email on homeowner side; Chad's side uses voice approval. |
| 26 | **Warranty** | "View warranty info, appointments, feedback" | Mostly view-only on mobile ([BT docs](https://buildertrend.com/help-article/navigating-project-management/)) | SKIP (v1) | Out of scope per [ROADMAP](ROADMAP-2026-05-11.md). Backlog: Wave-2 super-side inspection coordination. |
| 27 | **RFIs Dashboard** | "List formal Requests for Info" | Custom builders don't do formal RFIs ([competitor-research §6](competitor-research-2026-05-11.md)) | SKIP | ADR-POS-001 — "no formal RFI module." Replaced informally by Wave-2 "what's open" status board + Ask tab. |
| 28 | **RFI Create form** | "Submit RFI: assignee, deadline" | Same | SKIP | Same — ADR-POS-001. |

### 2.3 Financial features

| # | Screen | Verb | Pain | Decision | Replacement / rationale |
|---|---|---|---|---|---|
| 29 | **Bids Dashboard** | "View bids assigned to me as a sub" | Portal-login pain. "GCs no longer need PR skills because they never call subs" ([ContractorTalk thread](https://www.contractortalk.com/threads/buildertrend-warning.450111/)) | SKIP | ADR-POS-001 — no sub portal. Replaced by Wave-1 #12 (vendor reliability score) Chad-side + SMS coordination sub-side. No sub opens our app. |
| 30 | **Purchase Orders Dashboard** | "View POs I've been assigned, approve/decline" | Same portal pain | SKIP / REBUILD split | Chad-facing PO REBUILT as Wave-1 #5 variant. Sub-facing PO acceptance SKIPped per ADR-POS-001. |
| 31 | **PO Create form + Approval + Signature pad** | Title, assignee, scope, deadline, line items, attachments + finger signature ([BT docs](https://buildertrend.com/help-article/bills-purchase-orders-on-mobile/)) | Form-fatigue pattern | REBUILD | Wave-1 #5 extends to POs. Voice draft + AI line items + provenance dot. |
| 32 | **Bills Dashboard** | "View bills, approve lien waivers" | Same portal pain | REBUILD | Wave-2 office-staff invoice triage + lien waiver chase. Triaged list, not raw dashboard. |
| 33 | **Add Bill (scratch / from PO / receipt scan)** | "Log a bill: number, vendor, amount, attach receipt" | One of BT's better mobile flows — receipt scan works | KEEP (the scan verb) | Chad already has this ([competitor-research §5](competitor-research-2026-05-11.md)). Dashboard view REBUILT (#32); capture verb preserved. |
| 34 | **Invoices (builder side)** | "Create + release invoices to client" | Forms-heavy desktop work in practice | REBUILD | Wave-2 office-staff draw schedule generator + invoice triage. Chad doesn't see this. |
| 35 | **Invoices (homeowner side)** | "View + pay invoice" | BT itself admits "desktop more user-friendly" ([BT blog](https://buildertrend.com/blog/mobile-app-for-homeowners/)) | SKIP | ADR-POS-001 — no homeowner portal. Stripe payment link inside Wave-1 #8 timeline email. No app. |
| 36 | **Estimates / Bids (builder side)** | "Build detailed estimate with line items" | "The phone app has some big limitations" ([G2, 2025](https://www.g2.com/products/buildertrend/reviews)) — desktop-only in practice | SKIP | Build-phase, not bid-phase ([competitor-research §6](competitor-research-2026-05-11.md)). Out of scope. |
| 37 | **Proposals / Takeoff** | "Upload plans, measure, generate proposal" | Desktop-only in practice | SKIP | Same — out of scope. |

### 2.4 Communication, Files, Time Clock, Leads, Settings

| # | Screen | Verb | Pain | Decision | Replacement / rationale |
|---|---|---|---|---|---|
| 38 | **Comments Dashboard + Composer** | "Comment on a To-Do / Schedule Item / etc." | Feature-tied comments fragment communication across modules | REBUILD | Wave-2 super-side decision routing — questions go to Ask, not per-feature threads. |
| 39 | **Messages Dashboard + Composer** | "Send emails inside the platform" | "Subs hate 2–3 daily auto-emails they can't disable" ([ContractorTalk](https://www.contractortalk.com/threads/buildertrend-warning.450111/)) | REBUILD | Wave-1 #10 (AI auto-reply triage). Email is the universal channel; inbox watcher handles it on Chad's gmail. |
| 40 | **Direct Chat Dashboard + Composer** | "1:1 / group chat in-app" | Yet another inbox | SKIP | No in-app chat. Subs use SMS; team chats go through Ask. |
| 41 | **Files (Documents) Dashboard** | "Browse documents, annotate, request signatures" | Folders don't sync across photo/video/document silos ([BT docs](https://buildertrend.com/help-article/files-on-mobile/)) | REBUILD | Drive-backed unified file browser ([competitor-research §6 #6](competitor-research-2026-05-11.md)). |
| 42 | **Photos Gallery** | "View project photo grid, tap to view, annotate" | "Sometimes drawings wouldn't load, and other times the app would crash" ([Capterra, 2025](https://www.capterra.com/p/70092/Buildertrend/reviews/)) | REBUILD | Wave-1 #1 + #4. Photos are the substrate, not a tab. Browse stays as CompanyCam-style cover-photo tiles ([ux-design-overhaul §3.8](ux-design-overhaul-2026-05-11.md)). |
| 43 | **Photo Annotation (markup tools)** | "Draw arrows, circles, text on photo or plan PDF" | Underused — nobody finds them | KEEP (the verb) | Photo annotation as first-class iPad verb ([ROADMAP Week 7](ROADMAP-2026-05-11.md)) — PencilKit, pressure-sensitive. |
| 44 | **Photo Share Extension (iOS)** | "Share photos from camera roll into a BT job folder without opening app" | One of BT's smarter integrations | KEEP (the pattern) | iOS share-sheet → photo lands in GPS-bound active project automatically. |
| 45 | **Time Clock (Clock In / Out)** | "Clock in to a job, cost code, GPS-stamp" | "Needlessly complicated features (such as the time clock on iPhone)" ([Capterra, 2025](https://www.capterra.com/p/70092/Buildertrend/reviews/)) | SKIP | ADR-POS-001 — "no time clock / payroll." Subs are 1099. |
| 46 | **Shifts list (Time Clock history)** | "Review past shifts, edit" | Same as #45 | SKIP | Same — ADR-POS-001. |
| 47 | **Time Clock widget (home screen)** | "See current clock-in status" | Irrelevant to our ICP | SKIP | Same — ADR-POS-001. Our widget surfaces morning packet, not time clock. |
| 48 | **Lead Opportunities Dashboard** | "View leads in pipeline" | Wrong ICP — production builders, not custom | SKIP | ADR-POS-001 — "no lead pipeline / CRM." ([ROADMAP "What's NOT in this plan"](ROADMAP-2026-05-11.md)) |
| 49 | **Lead Activity Calendar** | "Scheduled lead follow-ups" | Wrong ICP | SKIP | Same — ADR-POS-001. |
| 50 | **Lead Map** | "Map of lead locations" | Wrong ICP | SKIP | Same — ADR-POS-001. |
| 51 | **Lead Proposals (legacy)** | "Cost estimates for prospects" | Bid-phase | SKIP | Out of scope. |
| 52 | **Settings → My Account** | "Edit profile, password, sign out" | 3-tap nav to reach Settings ([BT docs](https://buildertrend.com/help-article/mobile-app-version/)) | KEEP (simplified) | One-screen settings ([ux-design-overhaul §3.11](ux-design-overhaul-2026-05-11.md)). No nested menus. |
| 53 | **Settings → Notifications preferences** | "Toggle email / SMS / push per feature" | Need exists because default firehose is too noisy | REBUILD | Wave-1 #10 reduces granularity need via upstream filtering. Simple toggles + AI triage upstream. |
| 54 | **Settings → About / Version** | "What version I'm on" | Standard utility | KEEP | Apple HIG minimum. |

### 2.5 Subcontractor-side screens (the entire side of BT we never build)

Listed for completeness — the entire portal layer is SKIPped by ADR-POS-001.

| # | Screen | Verb | Pain | Decision | Replacement / rationale |
|---|---|---|---|---|---|
| 55 | **Sub Portal Login + onboarding** | "Sub creates account, accepts BT subscription" | Universal sub hatred. "Forced subscription model, 2–3 daily auto-emails subs can't disable" ([ContractorTalk](https://www.contractortalk.com/threads/buildertrend-warning.450111/)) | SKIP | ADR-POS-001 — no portal. Subs via SMS ([ROADMAP Week 2](ROADMAP-2026-05-11.md)). |
| 56 | **Sub PO acceptance** | "Sub approves a PO with e-signature" | Same | SKIP | SMS + magic-link form. |
| 57 | **Sub Bid response** | "Sub responds with pricing" | Same | SKIP | Wave-1 #12 tracks subs Chad-side; subs don't bid through our app. |
| 58 | **Sub schedule view** | "Sub sees assigned tasks" | Subs don't open portal in practice — premise fails | SKIP | SMS reminder + magic-link if they want detail. |

### 2.6 Homeowner-side screens (also entirely SKIPped)

| # | Screen | Verb | Pain | Decision | Replacement / rationale |
|---|---|---|---|---|---|
| 59 | **Client Portal Login** | "Homeowner logs in to see project" | Even BT undersells the portal: "desktop more user-friendly" ([BT blog](https://buildertrend.com/blog/mobile-app-for-homeowners/)) | SKIP | ADR-POS-001 — no homeowner portal. Replaced by Wave-1 #8 + Wave-2 owner anxiety inbox + Friday recap + milestone emails + decision queue. |
| 60 | **Client schedule view** | "Homeowner sees schedule" | Homeowners don't read Gantt charts | SKIP | Wave-1 #8 — same data as prose. |
| 61 | **Client photos view** | "Homeowner views progress photos" | Decent but portal-gated | SKIP | Inline thumbnails in Wave-1 #8 + Wave-2 Friday recap. |
| 62 | **Client selections approval** | "Homeowner approves a selection" | "Not very user friendly" ([Capterra, 2025](https://www.capterra.com/p/70092/Buildertrend/reviews/)) | SKIP | Email-based approval inside Wave-1 #8 timeline. Magic-link auth, not portal account. |
| 63 | **Client change order approval + signature** | "Homeowner approves a CO with e-signature" | Awkward but legally required | KEEP (the signature flow) | Finger-signature step accessed via magic-link email, not portal app. Same legal acceptance, no login. |
| 64 | **Client messaging** | "Homeowner messages builder" | Yet another inbox | SKIP | Regular email. Wave-1 #10 inbox watcher drafts Chad's reply. |
| 65 | **Client invoices + pay online** | "Homeowner views and pays" | OK pattern, portal-gated | SKIP | Stripe payment link inside Wave-1 #8 email. |

---

## 3. Cross-screen patterns Buildertrend does *everywhere* — and we never do

These are not screens but pathologies. Each must be a binding "we don't do this" for our app.

| Anti-pattern | How it manifests in BT | Why we never ship this | Source |
|---|---|---|---|
| **Mandatory project picker as first action** | "Select a Job from the Jobs List, then tap More, then..." appears in every help article. The picker is the universal precondition. | GPS auto-bind + photo-as-universal-verb make the picker obsolete. The first tap of the day is never "pick the project." | [BT Daily Logs doc](https://buildertrend.com/help-article/daily-logs-on-mobile/), [Selections doc](https://buildertrend.com/help-article/selections-on-mobile/), [Schedule doc](https://buildertrend.com/help-article/schedule-on-mobile/) |
| **Hamburger drawer for primary navigation** | "More" tab hides 60+ items behind a single tap. Selections, Change Orders, Warranty, Bills, RFIs all live behind one indistinct icon. | ADR-POS-001 + [ux-design-overhaul §3.2](ux-design-overhaul-2026-05-11.md) lock a 5-tab bar with center Capture FAB. Everything else lives inside a project, not as a global module. | [BT mobile version doc](https://buildertrend.com/help-article/mobile-app-version/) |
| **Login wall for every non-builder** | Subs log in. Homeowners log in. Suppliers log in. Every external party becomes a managed account. | ADR-POS-001 — no portals. SMS for subs, email for homeowners. Zero auth surface outside the builder. | [ContractorTalk thread](https://www.contractortalk.com/threads/buildertrend-warning.450111/), [BT Client Portal FAQs](https://buildertrend.com/help-article/client-portal-faqs/) |
| **Dense forms as the only authoring path** | Every CRUD operation is a 7-to-13-field form. The user types or picks; the system never infers. | Voice + photo + AI inference is our authoring grammar. Forms exist only as the *fallback* edit surface after the agent has filled it. | [BT Schedule on Mobile](https://buildertrend.com/help-article/schedule-on-mobile/), [BT Change Orders on Mobile](https://buildertrend.com/help-article/change-orders-on-mobile/) |
| **One-feature-per-tap navigation** | Visibility settings are a nested form (Internal Notes / Sub Notes / Client Notes — 4 visibility tiers per Schedule Item). Each toggle is its own tap. | Visibility is inferred from project context + role of recipient. Chad never thinks about it. | [BT Schedule doc](https://buildertrend.com/help-article/schedule-on-mobile/), [BT Selections doc](https://buildertrend.com/help-article/selections-on-mobile/) |
| **Feature-tied comments fragmenting communication** | Comments live inside To-Dos, Schedule Items, Change Orders, POs separately. Cross-feature conversation is impossible. | Ask is the unified conversation surface. Anything you'd comment-on becomes a question in Ask. | [BT Messaging on Mobile](https://buildertrend.com/help-article/messaging-on-mobile/) |
| **Constant UI churn** | "It's a constant change, with every time you get used to a new change some IT person creating an extra step or moving a box to the other side of the screen" ([Capterra page 3](https://www.capterra.com/p/70092/Buildertrend/reviews/?page=3)). | Single-builder fit per ADR-POS-001. Chad's app changes only when Chad's process changes. No A/B tests on production tenants. | [Capterra reviews](https://www.capterra.com/p/70092/Buildertrend/reviews/) |
| **Raw activity stream as the default surface** | The Activity Feed dumps every event in chronological order. The user has to filter. | AI-summarized morning packet + end-of-day rollup replace raw streams. We do the filtering upstream. | [BT Mobile Nav intro](https://buildertrend.com/help-article/introduction-to-mobile-navigation/) |
| **Spreadsheet-style data views on a phone** | List view of schedule items, list view of bills, list view of POs — all dense tables on a 6.1" screen. | Field Card prose + photo-first hero per [ux-design-overhaul §3.6 / §4.2](ux-design-overhaul-2026-05-11.md). Tables are a fallback for power-user drill-in, not a primary surface. | [BT Schedule on Mobile](https://buildertrend.com/help-article/schedule-on-mobile/) |
| **Internal-versus-external visibility toggles on every artifact** | Internal Notes / Sub Notes / Client Notes — separate text fields per artifact. Author chooses visibility. | Visibility derived from artifact type (a CO is for the homeowner; a punch item is internal). User authors content once; agent drafts the visible-to-external version. | [BT Schedule doc](https://buildertrend.com/help-article/schedule-on-mobile/) |

---

## 4. The 30-feature coverage chart

Mapping the 30 candidate features → which Buildertrend screens each one replaces or KEEPs.

**Wave 1 (15 builder-side):**

| # | Feature | BT screens covered |
|---|---|---|
| 1 | Photo-as-universal-verb | #2 (picker), #6 (Quick Add), #9-10 (Daily Logs), #42 (Photos) |
| 2 | CarPlay companion | Additive — BT has nothing |
| 3 | Apple Watch glance | Additive — BT has nothing |
| 4 | 30-second site walk | #9-10 (Daily Logs), partial #15 (Schedule), #16 (Punch), #38 (Comments) |
| 5 | Voice change orders | #22-25 (CO flow), partial #31 (PO Create) |
| 6 | Voice schedule moves | #15 (Schedule Item form) |
| 7 | Cost guardrails before commit | #21 (Allowances), implicit in CO/PO authoring |
| 8 | Owner narrative timeline | #59-65 (entire client portal layer), #5 (Activity Feed) |
| 9 | "I'm with the owner now" briefing | Additive — BT has nothing |
| 10 | AI auto-reply triage | #4 (Notifications), #38-39 (Comments/Messages), #53 |
| 11 | Permit office watcher | Additive — BT has nothing |
| 12 | Vendor reliability score | #29 (Bids dashboard — Chad's view) |
| 13 | Weather as first-class actor | Upgrades the weather field in #10 from display to action |
| 14 | Predictive CO date | Additive — BT has nothing |
| 15 | iPad portfolio mode | iPad-specific — BT has no iPad-native experience |

**Wave 2 (15 owner/super/office):**

- **Owner:** Decision queue → #62, #63; anxiety inbox → #64; milestone emails → #5, #61; Friday recap → #59-61; "what's my budget?" → #34-35.
- **Super:** Morning packet → #3, #5; end-of-day rollup → #5, #9; decision routing → #38-40; inspection coordination → partial #26; anomaly view → partial #3, #5.
- **Office staff:** Invoice triage → #32; lien waiver chase → #32; draw schedule → #34; document routing → #41; "what's open" → #5, #27-28.

**Coverage summary:**

| Decision | Screens | % of total |
|---|---|---|
| REBUILD | 18 | 28% |
| SKIP | 33 | 51% |
| KEEP | 10 | 15% |
| Gaps (no candidate) | 4 | 6% |
| **Total distinct screens** | **65** | **100%** |

Note: the count includes the homeowner-side (7) and sub-side (4) portal screens, all SKIPped under ADR-POS-001. Excluding those, the builder-facing surface is **54 screens**, of which our 30-list covers **50** with REBUILD/SKIP/KEEP. The 4 builder-facing gaps are below.

---

## 5. Builder-facing gaps the 30 ideas don't address

Four Buildertrend screens have no candidate replacement in the 30-list. Each is flagged for future ideation.

| Gap | BT screen | Why it's a gap | Suggested future feature |
|---|---|---|---|
| 1 | **Warranty + post-handoff service** (#26) | Custom builders handle warranty 12+ months after handoff. The verb (homeowner reports issue → schedule appointment → capture feedback) is real and our 30-list has nothing for it. | Wave-3: "Warranty year as managed timeline" — post-handoff inbox watcher with warranty-aware agent prompt. Inspector contact directory ties in. |
| 2 | **PDF signature send** (#41) | "Request a signature on a PDF" (plans, contracts, addenda) is a real builder task. Wave-1 #8 covers homeowner CO sigs but not arbitrary PDFs. | Wave-3: "Universal e-sign send" — Capture mode scans a PDF, picks a recipient, sends magic-link signature request. Same plumbing as #25/#63. |
| 3 | **Estimates / Takeoff** (#36-37) | Explicitly out of scope per [competitor-research §6](competitor-research-2026-05-11.md). | Will not fill — deliberate exclusion of bid-phase tools. |
| 4 | **Annotated photo → sub task** (#43 partial) | Chad annotates on iPad (Week 7), wants to send "do this here" to a sub. SMS coordination exists but not the annotated-photo bridge. | Wave-3: "Annotated photo → SMS task" — Pencil annotate → tap "send to sub" → SMS with marked-up photo + voice-transcribed instruction. Bridges Week 2 + Week 7. |

The Warranty gap (#1) is the most consequential — BT charges for it as a Complete-tier feature. Worth a Wave-3 candidate before broader client onboarding.

---

## 6. Sources

### Buildertrend official documentation (mobile-specific)
- [App Store listing](https://apps.apple.com/us/app/buildertrend/id504370616), [Mobile App marketing page](https://buildertrend.com/app/), [Introduction to Mobile Navigation](https://buildertrend.com/help-article/introduction-to-mobile-navigation/) — top-level surface
- [Navigating Project Management](https://buildertrend.com/help-article/navigating-project-management/), [Navigating Financial Management](https://buildertrend.com/help-article/navigating-financial-management/) — feature maps
- [Daily Logs on Mobile](https://buildertrend.com/help-article/daily-logs-on-mobile/), [Schedule on Mobile](https://buildertrend.com/help-article/schedule-on-mobile/), [Selections on Mobile](https://buildertrend.com/help-article/selections-on-mobile/), [Change Orders on Mobile](https://buildertrend.com/help-article/change-orders-on-mobile/), [Bills & POs on Mobile](https://buildertrend.com/help-article/bills-purchase-orders-on-mobile/), [Files on Mobile](https://buildertrend.com/help-article/files-on-mobile/), [Invoices on Mobile](https://buildertrend.com/help-article/invoices-on-mobile/), [Messaging on Mobile](https://buildertrend.com/help-article/messaging-on-mobile/), [Time Clock on Mobile](https://buildertrend.com/help-article/time-clock-on-mobile/) — per-screen flows
- [Mobile App FAQs](https://buildertrend.com/help-article/mobile-app-faqs/), [Mobile App Version](https://buildertrend.com/help-article/mobile-app-version/), [Mobile App Setup](https://buildertrend.com/help-article/buildertrend-mobile-app-setup/), [Buildertrend Glossary](https://buildertrend.com/help-article/buildertrend-glossary/), [Client Portal FAQs](https://buildertrend.com/help-article/client-portal-faqs/), [Lead Opportunities Overview](https://buildertrend.com/help-article/lead-opportunities-overview/), [Mobile App for Homeowners](https://buildertrend.com/blog/mobile-app-for-homeowners/)

### Recent reviewer quotes (2024–2026)
- [Capterra reviews](https://www.capterra.com/p/70092/Buildertrend/reviews/) — "10x more clicking"; "trades hate it because it is hard to use on their phones"
- [Capterra page 3](https://www.capterra.com/p/70092/Buildertrend/reviews/?page=3) — "constant change... moving a box to the other side of the screen"
- [App Store reviews](https://apps.apple.com/us/app/buildertrend/id504370616) — "you have to click on another tab to get to the created checklist instead of just scrolling down"
- [G2 reviews](https://www.g2.com/products/buildertrend/reviews) — "the phone app has some big limitations compared to what you can do from a computer"
- [Jibble Buildertrend Review 2025](https://www.jibble.io/construction-software-reviews/buildertrend-review), [ContractorTalk Buildertrend Warning thread](https://www.contractortalk.com/threads/buildertrend-warning.450111/), [StackVett Buildertrend Review 2026](https://stackvett.com/buildertrend-review/), [Workyard Buildertrend Review](https://www.workyard.com/compare/buildertrend-review)

### Internal references
- [competitor-research-2026-05-11.md](competitor-research-2026-05-11.md), [ux-design-overhaul-2026-05-11.md](ux-design-overhaul-2026-05-11.md), [ROADMAP-2026-05-11.md](ROADMAP-2026-05-11.md), [ADR-POS-001](../../patton-os/data/decisions.md), [ADR-UX-002](../../patton-os/data/decisions.md)
