# Construction-Turtle Roadmap — Phase 2 (Weeks 9–16)

> One-line summary: aggressive 8-week sequence that ships 15 of the 30 Phase-2 candidate features by Week 16 and leaves 4 deferred-but-ready (super-side, ships when client #2 has supers). Bundles by shared infrastructure so each week ships 2–3 features riding the same pipe.

**Status:** Active build plan.
**Created:** 2026-05-11.
**Builds on:** [ROADMAP-2026-05-11.md](ROADMAP-2026-05-11.md) (Phase 1, weeks 1–8).
**Anchors:**
- ADR-POS-001 — construction-turtle positioning
- 2026-05-11 iOS UX design language v2 (five signature moves)
- 2026-05-11 Phase-2 backlog locked (top 5 priorities + 25 supporting concepts)
- 2026-05-11 ADR-UI-001 — Field Card is the universal UI primitive

## TL;DR

Phase 1 (weeks 1–8) builds the foundation: SMS hub, Field Card synthesizer, voice walkthrough, photo pipeline. Phase 2 leverages that foundation to ship 15 features in 8 weeks by bundling: SMS-based features ride together, Field Card variants ship as config rows, office workflows bundle, photo features bundle.

**By Week 16: 15 features shipped + 4 deferred-but-ready (auto-ship when client #2 has supers).**

## The 5 sequencing principles

1. **Infrastructure-first weeks unlock bundles.** Once SMS hub (W2) exists, every SMS-based feature is a config row.
2. **Field Card variants are config rows.** Per ADR-UI-001, 10 of the 15 new ideas are Field Card surfaces — once the synthesizer abstraction lands, each new audience/slot is hours, not weeks.
3. **Defer non-Chad workflows.** Multi-super, multi-PM coordination features don't fit Palmetto. They sit ready but ship when client #2 has supers (Newton RFID, future Nashville builders, etc.).
4. **Bundle by audience.** Owner-batch ships together. Office-batch ships together. Reduces context-switch cost.
5. **Dual-agent execution.** Per the 2026-05-11 dual-agent workflow ADR: Claude Code designs + integrates + reviews; Codex parallel-executes well-specified mechanical work + audits Claude's PRs with REVIEW_BRIEFs. Each week below carries an explicit tool-split — without it, the 8-week timeline slips.

## Tool split by week (per dual-agent workflow ADR 2026-05-11)

| Week | Claude Code | Codex | Codex parallelism |
|---|---|---|---|
| 9 SMS Hub Ext. | Field Card synthesizer abstraction + webhook + router shape | per-command handlers ("is OK?", "what's my budget?") + tests | 2 parallel workers |
| 10 Owner Pack | narrative timeline screen design (iOS) + synthesizer audience configs | 4 email templates: decision queue · milestone celebration · Friday recap · narrative timeline | 4 parallel workers |
| 11 Permit + Insp. | per-county adapter protocol + CountyAdapter test harness | per-county scrapers (Baldwin, Davidson, Williamson, ...) + inspection coordination flow | N parallel workers (one per county) |
| 12 Photo+Voice | 30-second-walk orchestrator design + iOS Camera-tab integration | the four output composers (site log writer · homeowner email · punch list · Drive PDF) | 4 parallel workers |
| 13 CarPlay | mostly Claude — Apple entitlement risk + CarPlay template specifics | post-build REVIEW_BRIEF audit + test scaffolding | minimal |
| 14 Office Pack 1 | invoice parser design + lien-waiver state model | parser implementation + lien-waiver SMS flow | 2 parallel workers |
| 15 Office Pack 2 | review + integration only — Codex-heavy week | 3 Field Card variants (draw schedule, doc routing, "what's open") | 3 parallel workers |
| 16 Polish | cross-review pass + Phase-3 ADR | Codex audits everything Claude shipped Phase 2 → punch list | 1 deep-audit worker |

**Codex parallel work peaks at W11 + W12** (4+ workers each) — those are the parallelism wins that compress the schedule. **W13 (CarPlay) is the sequential bottleneck** — accept it, mitigate by overlapping with W14 prep where possible.

Handoff briefs land in:
- `~/Projects/patton-ai-ios/docs/CODEX_HANDOFFS/VERTICAL_HANDOFF__<topic>.md` (execution briefs)
- `~/Projects/patton-ai-ios/docs/CODEX_HANDOFFS/CODEX_REVIEW_BRIEF__<topic>.md` (audit briefs)
- `~/Projects/patton-os/data/codex_review_package_<DATE>.md` (recurring cross-repo Codex review cycle)

Template for new briefs: `~/Projects/patton-ai-ios/docs/CODEX_HANDOFFS/TEMPLATE__VERTICAL_HANDOFF.md`.

## Week-by-week

### Week 9 — SMS Hub Extension Pack

**Ships:**
- **Owner #2:** "is everything OK?" SMS auto-reply — owner texts Twilio number, AI synthesizes a 2-sentence status + one fresh photo, sends back.
- **Owner #5:** "what's my budget?" SMS — owner texts the question, AI replies with spend vs contract + variance lines.

**Reuses:** Week-2 Twilio infra · existing cost tracker · Field Card synthesizer (per ADR-UI-001).

**Effort:** 3 days. The Twilio inbound router gets two new commands; both call into the Field Card synthesizer with different audience/slot/intent configs.

**Demo:** Owner texts at 11 PM. Two seconds later, gets a Field Card reply that dissolves the anxiety. Builder slept through it.

---

### Week 10 — Owner Pack

**Ships:**
- **Phase-2 #4:** Owner narrative timeline — per-project AI-curated milestone timeline (photos + Chad-voice captions). Shareable read-only link, no portal.
- **Owner #1:** Decision queue email — Tuesday + Thursday, AI synthesizes the week's open decisions (selections, COs, schedule confirms) for the owner with one-tap reply options.
- **Owner #3:** Milestone celebration emails — phase completes (foundation, framing, dry-in) → AI auto-generates an email with the photo + the meaning. Owner forwards to family.
- **Owner #4:** Friday recap email — this is already Week-5's deliverable from Phase 1; calling it out as the owner-Friday-recap completes the owner-side picture.

**Reuses:** branded_pdf template · photo captions (W8) · Field Card synthesizer.

**Effort:** 5 days. The narrative timeline is the lift (~3 days); the others are Field Card variants on top of existing data.

**Demo:** Owner gets the narrative-timeline link in their next milestone email. They forward to their dad in Florida. Word-of-mouth marketing built in.

---

### Week 11 — Permit + Inspection

**Ships:**
- **Phase-2 #5:** Permit office watcher — per-builder background agent polls county portals on schedule, push notifications on status change, telemetry to `platform.event`. Baldwin County adapter ships first; framework reusable for future builders. Spec already exists at `docs/specs/permit-office-watcher.md`.
- **Super #9:** Inspection coordination — extends the watcher: builder targets a phase → AI schedules inspection request, monitors portal, texts the relevant party (Chad today; super when applicable) the day before, logs the result.

**Reuses:** Watcher framework (new) · per-county adapter pattern (new) · `platform.event` for telemetry.

**Effort:** 5 days. Most of the cost is the Baldwin County adapter (~2 days because portal-specific HTML scraping). Inspection coordination is a small extension on top.

**Demo:** Building permit status moves overnight. Chad's phone buzzes at 7 AM with the update before he opens the county website.

---

### Week 12 — Photo + Voice Power

**Ships:**
- **Phase-2 #1:** 30-second site walk → 4 outputs — camera + voice produces simultaneously: site log entry, homeowner update email draft, punch-list for trades, Drive-folder PDF. One input, four outputs. **The single most demoable feature in the entire product.**
- **Phase-2 #2:** Photo-as-universal-verb — AI classifies every uploaded photo (receipt / permit / site log / progress / other) and auto-routes. Camera button is the only menu.

**Reuses:** W4 voice walkthrough · W8 photo captions · Drive folder management · cost tracker · branded_pdf.

**Effort:** 5 days. Both features sit on top of W4 + W8 plumbing — they're orchestration, not net-new AI.

**Demo:** Chad walks Whitfield for 30 seconds narrating into the iPhone. By the time he's back in the truck, his homeowner has an email + the daily log is updated + the punch list went to the trim sub.

---

### Week 13 — CarPlay

**Ships:**
- **Phase-2 #3:** CarPlay companion — Today brief reads aloud as Chad pulls out of the driveway. Voice commands while driving. Zero construction-PM competitors have this.

**Reuses:** Today's brief data · Field Card synthesizer (the spoken version is just the same prose with a different render path).

**Effort:** 5 days, but with **Apple-entitlement risk**. CarPlay templates have strict approval rules; if our intended UX uses something Apple doesn't support, we hit a wall. Budget +2 days for rework.

**Demo:** Chad pulls out of his driveway at 6:15 AM. CarPlay greets him: "30 minutes to Pelican Point. Framing crew confirmed Tuesday. Lowe's has your fixtures. Mrs. Whitfield emailed about lighting." He arrives briefed.

---

### Week 14 — Office Pack 1

**Ships:**
- **Office #11:** AI invoice triage — subs email invoices → AI extracts amount + lines + project → matches contract/budget → flags discrepancies → drafts reply or auto-pays under threshold. Office staff approves the AI's recommendation; doesn't enter data.
- **Office #12:** Lien waiver chase — AI tracks outstanding waivers per draw → auto-texts subs missing waivers with magic-link to sign → escalates after 3 days. Replaces the chase-by-PDF workflow.

**Reuses:** Week-2 SMS magic-link · new `invoices` table · new `lien_waivers` table · existing cost tracker.

**Effort:** 5 days. The lift is the new data models + the invoice email-parsing classifier. Lien waiver flow is 80% on top of the SMS infra.

**Demo:** Sub emails invoice. AI categorizes within 60 seconds. Office staff sees a Field Card: "Acme Plumbing invoice for $4,200 — matches PO, line items reconcile, recommend pay." One-tap approve.

---

### Week 15 — Office Pack 2

**Ships:**
- **Office #13:** Draw schedule auto-generator — phase milestones + costs → AI generates draw schedule + lien waiver requirements + bank-friendly PDF.
- **Office #14:** AI document routing — inbound PDF (contract, CO, insurance certificate, plan revision) → AI classifies + files in Drive + logs audit trail + notifies parties.
- **Office #15:** "What's open" status board — Field Card variant showing per-project open items: unsigned COs, outstanding invoices, expired COIs, permits to renew. Email digest + future iOS surface.

**Reuses:** branded_pdf template · Drive sync · Field Card synthesizer · permit watcher (W11) data.

**Effort:** 5 days. All three reuse existing primitives heavily; the routing classifier is the only meaningful new code.

**Demo:** Office staff opens their morning Friday digest. One Field Card: "3 invoices pending approval, 1 COI expired Tuesday, 2 permits up for renewal next month. Tap to triage." Done in 4 minutes.

---

### Week 16 — Polish + Telemetry Analysis + Cleanup

**Ships:**
- Telemetry analysis: which Phase-2 features actually got used in production? Pull `platform.event` per feature, score adoption.
- Polish: any Phase-2 feature that landed but isn't being used gets a UX review — either fix or remove.
- Sub-bug fixes accumulated through the phase: e.g., the `patton-cto adr` worktree bug from 2026-05-11 (writes to worktree's `decisions.md` instead of canonical).
- Phase-3 planning session: review remaining 11 candidate features, lock the next ADR.

**Effort:** 5 days, mostly inspection + cleanup, not new build.

---

## Deferred to client-#2 onboarding

These four super-side features don't fit Chad's solo-operator workflow (he IS the super). Code is specced + ready; ships when the first builder with multiple supers signs:

- **Super #6:** Morning packet — 5 AM SMS to each super with today's plan.
- **Super #7:** End-of-day rollup — super dictates 30s, AI synthesizes into daily log + builder brief.
- **Super #8:** Decision routing — super texts AI, AI either answers / routes / suggests.
- **Super #10:** Cross-site anomaly view — builder sees which site/super is behind.

**Adds ~3 days to the second multi-super builder's onboarding** (mostly Field Card synthesis config + Twilio number provisioning).

## What ships when — cumulative leverage view

| Week | Features shipped | Cumulative | Notes |
|---|---|---|---|
| 9 | 2 | 2 | SMS owner extensions |
| 10 | 3 (plus W5's already counts) | 5 | Owner-side complete |
| 11 | 2 | 7 | Permit + inspection |
| 12 | 2 | 9 | The big demo (site walk) |
| 13 | 1 | 10 | CarPlay (high-risk) |
| 14 | 2 | 12 | Office pack 1 |
| 15 | 3 | 15 | Office-side complete |
| 16 | — | 15 | Polish + retrospective |

Plus 4 deferred-but-ready = 19 of 30 candidate features have shipped or are ready to drop in.

The remaining 11 (vendor reliability score, weather-as-actor, predictive CO date, Apple Watch glance, voice schedule moves, voice change orders, cost guardrails, "I'm with the owner now" briefing, iPad portfolio mode, and 2 others) slot into Phase 3 (Weeks 17–24).

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| CarPlay (W13) hits Apple entitlement wall | Budget +2 days; if blocked, swap with Phase 3 voice-change-orders feature so the week isn't wasted |
| Office pack (W14, W15) needs new data models | Migration work front-loaded in W14 so W15 just composes |
| Per-county permit adapter (W11) fragile to portal redesigns | Saved HTML fixtures + adapter tests; redesigns surface as `permit.poll_failed` events, not silent breaks |
| Field Card synthesizer prose quality degrades with audience proliferation | Per-audience voice-rubric tests (mirror of `cxo_engine` rubric pattern); a Field Card that fails rubric blocks the merge |
| Connor solo-developer capacity slip | Phase 2 timeline is "8 weeks of focused work" — calendar weeks may stretch to 10–12 if interrupted by client crises |

## When this gets stale

- Client #2 signs and has supers → de-defer Super #6/7/8/10, slot into the next available week.
- New Phase-2 idea emerges that beats one of the locked 15 → run a small ADR amendment, swap in.
- Buildertrend or BuildBook ships a competing feature that closes one of our wedges → re-prioritize accordingly.

Re-run `patton-cto adr "..."` to record any pivot, then update this file with a note pointing at the new ADR.

## Cross-references

- Phase 1 roadmap: [ROADMAP-2026-05-11.md](ROADMAP-2026-05-11.md)
- Buildertrend competitor research: [competitor-research-2026-05-11.md](competitor-research-2026-05-11.md)
- CompanyCam + photo workflows: [competitor-research-photos-2026-05-11.md](competitor-research-photos-2026-05-11.md)
- UX design overhaul: [ux-design-overhaul-2026-05-11.md](ux-design-overhaul-2026-05-11.md)
- Permit watcher spec: [specs/permit-office-watcher.md](specs/permit-office-watcher.md)
- ADRs: `~/Projects/patton-os/data/decisions.md` — search for ADR-POS-001, ADR-UI-001, "iOS UX design language", "Phase-2"
