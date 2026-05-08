# Profile Review Screen — "What I Think I Know About You"

> One-line summary: the iOS surface that makes the learning loop visible. Chad opens it any time and sees the entire `user_profile` rendered as plain-language facts grouped by domain, each labeled with how the agent learned it (stated / confirmed / corrected / observed / inferred), how confident the agent is in it, and a one-tap path to confirm, correct, scope, or delete it. The transparency anchor that keeps the personalization layer feeling like partnership instead of surveillance.

**Status:** Spec — companion to [`learning-loop.md`](learning-loop.md) Phase 1 step 9.
**Phase:** Active — depends on migration 004 cut + `hb-profile` Phase A.2 + the per-fact metadata wrapping from `learning-loop.md` Part D.
**Owner:** CP (spec); patton-ai-ios CTO (build).
**Last updated:** 2026-05-08.
**Lives in:** `~/Projects/patton-ai-ios/PattonAIShell/Views/ProfileReview/` (iOS) and `~/Projects/patton-ai-ios/Mac/Views/ProfileReview/` (Mac, v1.5+).
**Cross-references:**
- [`learning-loop.md`](learning-loop.md) — the four-loop system this surface makes visible (especially Part D — Profile evolution)
- [`migration_004_review.md`](migration_004_review.md) — the JSONB shape this surface renders
- [`mobile-design-language.md`](mobile-design-language.md) — visual + interaction vocabulary
- [`chad-agent.md`](chad-agent.md) — the agent that authored most of these facts

---

## Why this surface exists

The learning loop in `learning-loop.md` does three things that don't survive without a transparency surface:

1. **Captures decision rationale.** Without the screen, Chad never knows the system stored "you went with Hammond because their PM is sharper."
2. **Builds confidence-weighted facts.** Without the screen, Chad has no way to see "the agent is 41% sure I prefer Hammond" — he just experiences the agent acting on it.
3. **Decays + corrects facts over time.** Without the screen, Chad can't kill a wrong learning. The system silently updates; he silently mistrusts.

The Profile Review Screen is the single answer to all three. **It is the difference between the agent feeling like a partner and feeling like a tracker.**

It also serves three product purposes:

- **Trust.** Builders are private people. Showing them the data we've inferred — and giving them control — is the only honest way to do this.
- **Privacy compliance.** The privacy policy promises "you can review and edit what we know about you." This screen is how that promise is kept.
- **Sales demo.** "Look — Chad can see exactly what the agent has learned, and correct it" is a closer than "trust us, the AI is learning."

---

## Where it lives + how Chad gets there

### Entry points (iOS)

1. **Settings tab → "What I think I know about you"** — primary entry, always available.
2. **Long-press any tab bar icon → "Why this?"** — context menu reveals the profile facts driving that tab's behavior. (E.g., long-press the daily tab → "Notification routing uses your working hours: 6am–7pm Mon–Fri. View profile.")
3. **From the weekly digest** in the morning brief → tapping any pattern card → opens the screen scoped to that pattern's category.
4. **From `hb-chad`** — Chad asks "what do you think I prefer for cabinets?" → response includes a "View profile" affordance that deep-links to the cabinets fact.

### Mac variant (v1.5+)

Same data, fuller layout — three-pane (categories left, facts center, fact detail right). Spec'd in v1.5; iOS is the v1 surface.

---

## Visual anatomy (iOS)

The screen lives at `sheet.large` detent or as a full-tab takeover (depending on entry — long-press = sheet, settings = full screen). Layout:

```
┌─────────────────────────────────────┐
│ ◀  What I think I know about you   │  nav bar (44pt)
├─────────────────────────────────────┤
│                                     │
│  Last updated 4 hours ago           │  freshness — type.caption
│  127 facts · 89% high confidence    │  summary stat
│                                     │
│  [ ▼ All ] [ Stated ] [ Inferred ]  │  filter chips (44pt)
│                                     │
├─────────────────────────────────────┤
│  ▾ VENDORS                          │  category accordion header
│                                     │
│  ┃ Concrete: Coastal Concrete       │  fact row — type.body-emph
│  ┃ ✓ You told me · 23 examples      │  source + evidence — caption
│  ┃                          [ → ]   │  72pt row, tap → fact detail
│                                     │
│  ┃ Plumbing: Hammond                │
│  ┃ ⚠ corrected last month · 41%    │  warning — fact is uncertain
│  ┃                          [ → ]   │
│                                     │
│  ┃ Windows: Andersen                │
│  ┃ ◯ observed · 14 examples · 92%  │
│  ┃                          [ → ]   │
│                                     │
│  ▾ SCHEDULING                       │
│  ┃ Working hours: 6am-7pm Mon-Fri  │
│  ┃ ✓ You told me · 1.0 confidence  │
│  ...                                │
│                                     │
├─────────────────────────────────────┤
│  ◉ Today  ◯ Schedule  ◯ Ask  ◯ ⚙  │  tab bar
└─────────────────────────────────────┘
```

### Key elements

**Header strip** (above the categories):
- "Last updated" timestamp — when `hb-profile` last rebuilt.
- Summary stat — total fact count + % at high confidence (≥0.85).
- Filter chips — All / Stated / Confirmed / Corrected / Observed / Inferred. Filter by source.

**Category accordion** — facts grouped by the top-level keys in the profile JSONB:
- Vendors
- Scheduling
- Vocabulary (preferred terms / avoid list)
- Format preferences (answer length, bullet style, dollar amounts)
- Working hours
- Notification preferences
- Decision patterns (common amounts, common project types)
- Ignored alert types

Categories collapse / expand by tap. Default state: top 3 by recent change activity expanded; rest collapsed.

**Fact row** (72pt, mobile-design-language `tap.standard`):
- Line 1: domain-readable label and value, `type.body-emph`
- Line 2: source glyph + source label + evidence count + confidence percent (when relevant), `type.caption`
- Right chevron → tap to open fact detail sheet

**Source glyphs** (consistent across screen):

| Glyph | Source | What Chad sees |
|---|---|---|
| ✓ | `stated_by_chad` | "You told me" |
| 👍 | `confirmed_by_chad` | "You confirmed" |
| 🔧 | `corrected_by_chad` | "Corrected from prior" |
| ◯ | `observed` | "Observed N times" |
| ✻ | `inferred` | "Inferred from your patterns" |

(Per the design language: no decorative emoji *anywhere except* these source glyphs, where they carry semantic weight. SF Symbols equivalents replace emoji in the actual UI.)

**Confidence badge:**
- ≥ 0.85 — no badge, considered solid
- 0.50 – 0.85 — small grey badge with the percentage
- < 0.50 — amber `⚠` badge with the percentage and an inline "uncertain" hint

**Color discipline:** zero hue except `status.warning` for low-confidence flags and `status.healthy` for the "high-confidence" header stat. Per `mobile-design-language.md`.

---

## Fact detail sheet

Tapping any fact row opens `sheet.medium` with the full provenance and edit affordances:

```
┌─────────────────────────────────────┐
│           ─────                     │  drag handle
│                                     │
│  Plumbing vendor                    │  type.title
│  Hammond                            │  type.display
│                                     │
│  ──────────────────────────         │
│  HOW I LEARNED THIS                 │  type.caption (header)
│                                     │
│  Observed 4 times: Hammond chosen  │
│  for plumbing rough-in on 4 of 7   │
│  recent projects.                   │
│                                     │
│  Corrected last month: you told    │
│  me Hammond's been padding bids    │
│  since March. Confidence dropped   │
│  from 78% to 41%.                  │
│                                     │
│  ──────────────────────────         │
│  USED FOR                           │
│  • Default vendor when drafting     │
│    bid emails for plumbing          │
│  • Fallback in 3 view-models when   │
│    a vendor isn't specified         │
│                                     │
│  ──────────────────────────         │
│                                     │
│  [ Confirm — keep using Hammond ]   │  tap.primary
│  [ Change to a different vendor  ]  │  tap.standard
│  [ Stop using this fact          ]  │  tap.standard, destructive
│  [ Make project-specific only    ]  │  tap.standard
│                                     │
└─────────────────────────────────────┘
```

### What each action does

| Action | Signal emitted | Profile effect |
|---|---|---|
| **Confirm — keep using X** | `pattern_confirmed` | Confidence jumps to ≥ 0.95, source becomes `confirmed_by_chad`, decay-ineligible |
| **Change to a different vendor** | `correction_received` + `preference_stated` (with new value) | Old value drops to ≤ 0.30 (effectively removed), new value lands at confidence 1.0 from stated source |
| **Stop using this fact** | `pattern_rejected` | Fact removed from profile entirely; suppressed from re-detection for 90 days |
| **Make project-specific only** | `preference_stated` with scope = "project" | Fact moves from `decision_patterns.<category>` → `project_overrides.<active_project>.<category>`; durable global value re-evaluated from remaining evidence |

All four actions are one-tap; no nested confirms (the destructive one — Stop using — gets a small confirmation tooltip but no full modal).

### Provenance section ("How I learned this")

Free-text narrative composed by `hb-chad` at fact-detail-open time, citing actual signal evidence in plain language. Chad sees *why* the agent thinks what it thinks, not raw signal IDs. Behind the scenes, the narrative is generated from the fact's `evidence_refs` list — same data the agent uses, rendered for humans.

For high-evidence facts (≥10 observations), the narrative caps at "Observed N times across [list of contexts]" rather than enumerating every instance. For corrected facts, the correction event always appears prominently — Chad's own words from the original `correction_text` are quoted verbatim.

### "Used for" section

A short list of where this fact is currently driving agent behavior: which agents read it, which view-models use it, which notification rules consult it. Generated from a static map of `(profile_field → consuming_systems)` maintained in `home_builder_agent/personalization/usage_map.py`. Helps Chad understand *what changes* if he edits the fact.

---

## Edit flows

### Flow A — Chad adds a preference directly

Not all facts come from observation. Chad can add a preference outright:

1. Tap "+ Add a preference" button at the bottom of the screen
2. Sheet opens with a free-text composer: "I always..." / "Never..." / "From now on..." prompts
3. Chad types: "I always frame on Tuesdays for projects with Monday client walkthroughs"
4. `hb-chad` parses (Sonnet) → confirms understanding inline: *"Got it — I'll default framing starts to Tuesdays for projects on a Monday-walkthrough cadence. Is that right?"*
5. Chad confirms → `preference_stated` signal lands → fact added to profile at confidence 1.0

This is the same path as `learning-loop.md` Part A3 (`preference_stated`), exposed as an explicit UI affordance.

### Flow B — Chad corrects a fact

From any fact detail sheet, "Change to a different vendor" → composer asks for the new value → fires `correction_received` + `preference_stated` → profile updates immediately. The next time any agent reads that fact, it gets the corrected value.

### Flow C — Chad scopes a fact

"Make project-specific only" → asks Chad which project → moves the fact under `project_overrides`. The next time an agent reads it for a *different* project, it falls back to the global default (or asks if there isn't one).

### Flow D — Chad nukes a category

Less common but supported. From the category header, a small "..." menu offers "Reset all of this category." Confirmation modal explains: "This removes 8 vendor preferences. The agent will re-learn them as you work — this can't be undone." On confirm: all facts in the category drop to confidence 0.0 (effectively removed); related signals stay in the audit log but stop being weighted toward profile reconstruction for 90 days.

This is destructive and intentionally hard to find. Most users will never use it. It exists for "the agent learned a bunch of wrong things during a confusing project, start over."

---

## "Why this?" entry point

Long-pressing a tab bar icon (or any agent suggestion in the app) reveals a context menu with a "Why this?" item. Tapping opens a mini-sheet:

```
This tab is filtered to Whitfield because:
• You've opened Whitfield 7× more than other projects this week
• Your morning brief covered Whitfield first

Want to change this default?
[ View profile ] [ Change behavior ]
```

Two-line explanation + two CTAs. The first deep-links to the relevant profile fact. The second opens a quick override (e.g., "always show all projects regardless of attention weights").

This pattern applies anywhere the agent makes a non-trivial decision Chad might want to interrogate: which project gets surfaced first, which vendor was suggested, why a notification fired (or didn't), why the morning brief leads with one thing vs another. **Every agent decision is interrogable.**

---

## What the screen does NOT do

- **No raw signal browsing.** Chad doesn't see `user_signal` rows. The screen renders the *profile* — the aggregated state — not the event log.
- **No model retraining UI.** The agent is Claude; there's no fine-tune to nudge. Edits change the *profile data Claude reads*, not the model.
- **No "explain like I'm 5" of the LLM.** The narrative is grounded in concrete signals + evidence counts, not "the AI thought."
- **No social / share affordances.** Profiles are private. There's no "share my profile" feature. Multi-builder comparison happens server-side at the `hb-instinct` level (Part E), with anonymization.
- **No Chad-vs-Connor split view.** Chad's profile is Chad's. He doesn't see "things Connor configured" as a separate category — Connor doesn't configure his profile.
- **No bulk export in v1.** v1.5 may add a JSON export for portability; v1 is read+edit on-device.

---

## Privacy + boundary

Every promise the privacy policy makes about personalization is enforceable from this screen:

- **"You can see what we've learned"** → the screen itself.
- **"You can correct mistakes"** → fact detail sheet "Change" action.
- **"You can delete it"** → fact detail sheet "Stop using this fact" + category-level "Reset all."
- **"It deletes when you delete your account"** → migration 004's `CASCADE on user delete`. The screen surfaces account deletion as an option in the very-last category (Settings → Account), with the same explicit copy.

A small "Privacy" link in the screen footer opens the published privacy policy. The screen is the operational counterpart to the policy.

---

## Performance + freshness

- **Render is local.** The profile JSONB is fetched on screen open via `GET /v1/profile` (existing endpoint behind shell-backend). Cached locally for 5 minutes.
- **Edit propagation is instant locally, eventual server-side.** A correction or confirm updates the on-device cached profile immediately (optimistic), queues the signal write, reconciles. The edit's effect on agent behavior is delayed by one `hb-profile` rebuild cycle (nightly) — but the profile cache shows the new value immediately.
- **Stale freshness indicator.** "Last updated 4 hours ago" — if `hb-profile`'s last build is older than 36h, the indicator shows in `status.warning` with a "rebuild now" affordance (calls a server endpoint to trigger an out-of-band rebuild).
- **No streaming or live updates.** This is a snapshot view. Pull-to-refresh re-fetches.

---

## Implementation plan

Slots into `learning-loop.md` Phase 1, expands step 9 ("Profile-review surface") with concrete sub-steps:

| # | Step | Where | Effort | Depends on |
|---|---|---|---|---|
| 9a | `GET /v1/profile` returns the per-fact-metadata-wrapped JSONB (per Part D) | shell-backend | half day | learning-loop.md step 6 |
| 9b | iOS: `ProfileReviewView` scaffold — nav bar, category accordions, fact rows | iOS | day | step 9a |
| 9c | iOS: `FactDetailSheet` — provenance narrative, four edit actions, signal emission on each | iOS | day | step 9b |
| 9d | `hb-chad` provenance generator — given a `fact_id`, return the plain-language "How I learned this" narrative | this repo | half day | step 9a |
| 9e | iOS: "+ Add a preference" composer flow with `hb-chad` round-trip confirm | iOS | half day | step 9c |
| 9f | iOS: "Why this?" long-press context menu on tab bar + agent suggestions | iOS | half day | step 9b |
| 9g | `usage_map.py` — static `(profile_field → consuming_systems)` map for the "Used for" section | this repo | quarter day | none |
| 9h | Category-level "Reset all" + Account deletion entry-point | iOS | half day | step 9c |
| 9i | Telemetry: log `profile_review_opened` and per-action signals (correction / confirm / nuke) for the loop's loop — measuring whether the screen is being used | this repo + iOS | quarter day | step 9c |

**Total:** ~5 days iOS, ~1.5 days this repo. Lands as one PR per major step (a–c, then d–g, then h–i).

The screen ships when 9a–9c land. The rest are progressive enhancements that compound trust over weeks, not gates on the v1 release.

---

## Anti-patterns

- **Don't paraphrase Chad's words in the provenance narrative.** When citing a `correction_text` or `rationale_text`, quote verbatim.
- **Don't render confidence as a science.** "92%" as a number is fine; "based on a Bayesian posterior over your preference distribution" is wrong tone. Plain-language cues only.
- **Don't make editing scary.** Every edit is reversible (re-correct), no big warning banners, no "are you sure" modals on the common case. Only the destructive nuke gets a confirm.
- **Don't surface internal taxonomies.** Chad sees "Concrete: Coastal Concrete," not `decision_patterns.common_vendors.concrete`. Field names in the profile JSONB are translated through a label map.
- **Don't show empty categories.** If a category has zero facts, hide it. Empty states are for "you haven't built up history here yet — keep working" only when the screen as a whole is empty.
- **Don't expose `hb-instinct` candidates here.** The product instinct loop (Part E of `learning-loop.md`) outputs to Connor, not Chad. This screen is Chad's view of *his own profile*; Patton AI's roadmap is not on it.
- **Don't make corrections feel like Chad failed.** The framing is "you taught the agent something new," not "you fixed a bug." Source labels reinforce this — "Corrected from prior" not "Fixed an error."
- **Don't conflate scopes silently.** When Chad picks "Make project-specific only," explicitly confirm which project the scope applies to. Don't infer from the active surface — too easy to scope wrongly mid-thought.

---

## What this unlocks

- **Trust as a product.** Chad can see his own data and edit it any time. The fastest objection-killer in any sales conversation.
- **Higher signal quality.** Direct edits via this screen are the highest-confidence signal we ever capture (`stated_by_chad` source). They compound the loop's accuracy.
- **Unblocks builder-onboarding sales pitch.** "Show me what the AI knows about Chad" is a demo-able artifact that doesn't require setting up a project, generating activity, etc. We can demo the screen with Chad's real (anonymized) profile.
- **Makes the privacy policy enforceable in UI, not just text.** Every promise has a corresponding screen action.
- **Builds the foundation for a future pro-builder feature.** A "Profile audit" that compares Chad's profile to industry-typical patterns — useful for "are my preferences out of step with the market?" Lean: defer to Phase 3.

---

## Open questions

- **Mac variant priority.** v1.5? v2? Mac is the desk-side surface; deeper inspection of the profile fits the desk posture. Lean: build at v1.5 once iOS validates the pattern, with a three-pane layout (categories left, facts center, fact detail right).
- **Profile-review onboarding.** First time Chad opens the screen, does the agent walk him through it? Lean: **no walk-through**, but show one inline "what is this?" tip on the first row that dismisses on tap. Walk-throughs are a smell.
- **Dual-attestation for corrections.** Should `hb-chad` ask Chad to confirm a correction once before it sticks? E.g., "You're switching from Hammond to Wholesale for plumbing — that'll change my default for new projects. Sound right?" Lean: **no in v1** — corrections are already explicit; double-confirming adds friction. Add only if mis-corrections become a measured problem.
- **Notification "why this?" for non-fired notifications.** Hardest case: explaining why a notification *didn't* fire. ("Why didn't I get a heads-up about cabinet delivery?") Lean: defer — the answer requires reasoning over silence, which is a harder UX problem than reasoning over an action.
- **Profile sharing for trusted builders / partners.** Could Chad share read-only access to his profile with his lead PM? Useful for "this PM acts on Chad's behalf" workflows. Lean: defer to multi-tenant Phase B.
- **Empty-state-of-screen first-week experience.** Week one, the profile is mostly empty (signals haven't aggregated). Default screen should communicate "the more you work, the more I learn — check back next week" rather than feeling broken. Lean: a single header card on the empty screen explaining the loop, dismissable but persistent at low opacity until ≥30 facts exist.
- **Per-fact "stop noticing this" for `hb-instinct`.** Should low-value patterns observed by `hb-instinct` (Part E) be suppressible from the profile-review screen even though they only surface to Connor? E.g., "Don't watch how often I copy-paste vendor phone numbers." Lean: yes — privacy interest, even if Chad never sees the candidates Connor receives.
