# Learning Loop — How the Agent Learns Chad

> One-line summary: the active-learning + clarification protocol that sits on top of migration 004's `user_signal` + `user_profile` foundation. Defines when the agent asks Chad, how it captures the answer, how it remembers decision rationale, how it surfaces what it thinks it knows for Chad to confirm or correct, and how confidence + decay + source attribution evolve over time. Migration 004 gave us the *bones* — passive observation. This is the *muscle* — active partnership.

**Status:** Spec — additive to migration 004; no schema changes (open-enum signal_type lets new vocabulary land in code).
**Phase:** Active — depends on 004 cut + `hb-profile` Phase A.2; unblocks chad-agent.md step 5 (long-term memory wiring).
**Owner:** CP.
**Last updated:** 2026-05-08.
**Cross-references:**
- [`migration_004_review.md`](migration_004_review.md) — the persistence foundation this builds on
- [`chad-agent.md`](chad-agent.md) — the agent that runs this loop
- [`mac-cmd-slash-popover.md`](mac-cmd-slash-popover.md) — desk-side surface for clarifications + reviews
- [`mobile-design-language.md`](mobile-design-language.md) — field-side surface for low-friction signal capture
- [`canonical-data-model.md`](canonical-data-model.md) § State ownership — signals are user-owned, profile is engine-owned

---

## What this is, what it isn't

**Is:** The protocol that turns Chad's day-to-day work into structured learning. Four loops:

1. **Capture** — every meaningful decision Chad makes (with or without the agent) becomes a typed signal. Decisions get *reasons attached*, not just outcomes.
2. **Ask** — when the agent doesn't have enough info to act well, it asks Chad — at the right moment, on the right surface, in the right register. The answer becomes a signal.
3. **Reflect** — the agent periodically surfaces what it thinks it knows ("I noticed you always pick Coastal Concrete for slabs") and Chad confirms, corrects, or scopes it.
4. **Suggest to Patton AI** — the agent watches Chad's friction points (manual repetition, repeated questions, consistent overrides, copy-paste workflows) and surfaces them to *Connor*, not Chad, as candidate new agents or features. Chad never has to ask for what he needs — the system tells Connor what Chad needs before Chad has to.

**Isn't:**
- A new database. Reuses `user_signal` + `user_profile` + `engine_activity`.
- A new agent. Extends `hb-chad` + `hb-profile` with new behaviors and new signal types.
- A surveillance system. Every learned fact is visible to Chad in the profile-review surface; he can correct, scope, or delete any of it.
- An autonomy expander. The agent still proposes; Chad still decides. Learning makes proposals *better*, not unilateral.

The shorthand: **the agent goes from a tool that watches Chad to a partner that talks with him.**

---

## The current state (after 004 + hb-profile)

What we already have:

- `user_signal` — append-only log of in-app behavior (screen views, ask queries, notification taps, voice usage). High write rate. iOS-emitted.
- `user_profile` — JSONB row per user: vocabulary, working hours, attention weights, decision patterns, ignored alert types, answer-style preferences. Built nightly by `hb-profile` from signals + engine_activity + Drive activity + Gmail history.
- `engine_activity` — autonomous Claude actions. Chad's pattern of *what he uses* is observable from this.

What's missing — the gaps this spec closes:

| Gap | Today | This spec adds |
|---|---|---|
| **Why behind a decision** | Outcome captured, rationale lost | `decision_rationale` signal — captured opportunistically |
| **Corrections** | Chad's overrides are observable but not flagged as corrections | `correction_received` signal + special weight in profile builder |
| **Explicit preferences** | Inferred only from frequency | `preference_stated` signal — Chad can declare "I always X" |
| **Agent uncertainty** | Agent guesses or fails silently | `clarification_asked` / `clarification_answered` protocol |
| **Pattern confirmation** | Profile updates silently overnight | Weekly review ritual — Chad sees + confirms learned patterns |
| **Confidence per fact** | Profile is flat — every field equally weighted | Confidence scores + source attribution per learned fact |
| **Scoping** | Profile is global — no notion of "this is project-specific" | `scope` field on each learned fact (durable / project / one-off) |
| **Decay** | Profile never forgets | Decay-eligible facts age out without reinforcement |

---

## Part A — Active capture mechanisms

Five new signal types extend the v1 vocabulary in `migration_004_review.md` § Signal vocabulary. All land via Pydantic / Codable additions; no migration needed (open-enum).

### A1 — `decision_rationale`

When Chad makes a non-obvious decision (especially one that conflicts with what the agent would predict), the agent has 24 hours to opportunistically ask "why" in a low-friction way. The answer gets stored as the rationale.

**Trigger conditions** (any of):
- Chad picks a vendor that isn't his most-frequent for that category
- Chad overrides an agent suggestion (the agent proposed X, Chad chose Y)
- Chad makes a schedule change >7 days in either direction
- Chad approves a Change Order that Chad-historically would have negotiated down

**Capture surface:** The next time Chad opens `hb-chad` (Cmd+/ on Mac, Ask tab on iOS), the agent prepends a single low-friction question:

> *Quick one — yesterday you went with Hammond on the Whitfield plumbing rough-in instead of Wholesale (your usual). Anything I should know about why? (Skip if it was a coin flip.)*

Chad answers, or types "skip," or just types his actual question. Either way, the signal lands:

```json
{
  "signal_type": "decision_rationale",
  "payload": {
    "decision_ref": "engine_activity:<uuid>",          // links to the action
    "category": "vendor_choice",
    "skipped": false,
    "rationale_text": "Hammond's PM is sharper. Wholesale ghosted me on Pelican.",
    "captured_at_session_id": "<uuid>"
  }
}
```

`rationale_text` is verbatim Chad words — never paraphrased — so the profile builder can mine it later without lossy summarization.

**Frequency cap:** max one rationale-ask per pop-over open. Skipping is silent (no "are you sure?"). Three skips in a row on the same category → stop asking about that category for 30 days. The agent learns *what Chad cares to explain* alongside *what Chad does*.

### A2 — `correction_received`

When Chad explicitly corrects the agent — in any channel — that's the highest-value signal we can get. Special-cased for weight.

**Trigger surfaces:**
- `hb-chad` reply: Chad's next message starts with "no," "actually," "wrong," "that's not right," "use X instead"
- iOS Ask tab: thumbs-down button (new affordance)
- Notification action: a "this wasn't useful" tap on a past notification
- Email reply: "this isn't right" pattern in a reply to an agent-drafted message
- Engine override: Chad opens the Tracker and reverts a value the agent just wrote

**Signal payload:**

```json
{
  "signal_type": "correction_received",
  "payload": {
    "what_was_wrong": "predicted Wholesale would be cheaper; was off by 8%",
    "agent_action_ref": "engine_activity:<uuid>",
    "chad_correction_text": "Wholesale's been padding their bids since March",
    "channel": "mac-popover" | "ios-ask" | "notification" | "email" | "tracker-override"
  }
}
```

Profile builder weights `correction_received` signals at **5×** the weight of normal observation signals — a single correction outpredicts five passive frequency observations. Corrections also flag the *predicted* value as **stale** — the profile can't keep relying on it.

### A3 — `preference_stated`

The simplest one: Chad explicitly tells the agent a preference. Chad never has to phrase it formally; `hb-chad` recognizes preference statements in any conversation and emits the signal.

**Recognition patterns** (handled in the agent prompt, not regex):
- "I always do X for Y"
- "From now on, default to X"
- "Never schedule X on Z"
- "Stop asking me about X"
- "I prefer X over Y"

**Signal payload:**

```json
{
  "signal_type": "preference_stated",
  "payload": {
    "raw_statement": "I always frame on Tuesdays — homeowner does walkthroughs Mondays",
    "domain": "scheduling" | "vendor" | "voice" | "notification" | "format" | "other",
    "scope": "durable" | "project" | "one-off",
    "agent_understanding": "Schedule framing-start on Tuesdays for projects with weekly Monday client walkthroughs"
  }
}
```

`agent_understanding` is the agent's paraphrase of what it thinks Chad meant. The next message Chad sees confirms it: *"Got it — I'll default framing starts to Tuesdays for projects on a Monday-walkthrough cadence. Tell me if that misreads."* If Chad replies with a correction, that's a `correction_received` signal too.

### A4 — `pattern_confirmed` / `pattern_rejected`

Generated by the **weekly review ritual** (see Part C). The agent surfaces a learned pattern; Chad's confirm or reject is the signal.

```json
{
  "signal_type": "pattern_confirmed" | "pattern_rejected",
  "payload": {
    "pattern_id": "<profile_pattern_uuid>",
    "pattern_description": "You pick Coastal Concrete for slab >2500sqft",
    "evidence_count": 7,
    "scope": "durable",
    "rejection_reason": "Coastal's slipped twice this year — switching to Pamlico"   // only on reject
  }
}
```

A confirmed pattern jumps to **high confidence** in the profile. A rejected pattern is removed *and* future occurrences are suppressed from the review for 90 days (don't keep asking about something Chad explicitly killed).

### A5 — `clarification_asked` / `clarification_answered`

The signal pair that makes Part B work. See § Part B for when the agent asks. When it does:

```json
// Agent emits this when it asks
{
  "signal_type": "clarification_asked",
  "payload": {
    "question_id": "<uuid>",
    "topic": "vendor-for-category" | "schedule-target" | "homeowner-preference" | "...",
    "context_ref": "engine_activity:<uuid>" | "event:<uuid>",
    "channel": "mac-popover" | "ios-ask" | "...",
    "question_text": "I'm about to draft a homeowner email about the cabinet slip — do you want me to suggest a 1-week delay or just describe the slip?"
  }
}

// Chad's reply is captured as the answer
{
  "signal_type": "clarification_answered",
  "payload": {
    "question_id": "<uuid>",
    "answer_text": "describe the slip, no specific delay yet — I want to talk to him first",
    "answered_at_ms_after_asked": 18400,
    "scope": "one-off" | "project" | "durable"     // agent infers from phrasing
  }
}
```

The pair lets us measure clarification quality: which questions did Chad answer? Which did he skip? Which did he answer once and we should never have asked again? Profile builder uses this to tune the asking thresholds (Part B).

---

## Part B — Clarification protocol (when + how the agent asks)

The most important rule: **the agent's job is to act well, not to interrogate.** Asking should be the exception, not the default. Every avoidable question erodes the partnership feel and trains Chad to ignore prompts.

### B1 — When to ask (the four legitimate cases)

The agent asks when *and only when* one of these holds:

1. **Required input is missing.** E.g., drafting a homeowner email but the homeowner's tone preference is unknown.
2. **Confidence on the predicted answer is below threshold.** Profile builder maintains a confidence score per fact. Below ~0.4 confidence → ask rather than guess.
3. **The action is high-stakes and the agent's prediction conflicts with a recent signal.** E.g., the profile says "Chad prefers Hammond" but a `correction_received` two weeks ago downweighted Hammond. Better to ask than guess wrong twice.
4. **Chad explicitly invited it.** "What do you want to know?" / "What's missing?" — agent enumerates what it doesn't have.

### B2 — When NOT to ask

Even when one of B1 holds, the agent **doesn't** ask if any of these block:

- The action isn't blocked by the missing info (proceed with a flagged best-guess instead, capture as `decision_rationale` candidate).
- The same question was asked in the last 72 hours and skipped or answered (the answer applies; don't re-ask).
- Chad is on a high-urgency surface (a lock-screen `critical-blocking` notification is not the time to ask "what voice do you want for this?").
- It's outside Chad's working hours per the profile (don't push notifications asking for input at 11pm).
- The agent has asked >2 clarifications today — daily cap. The third defaults to best-guess + flag.

### B3 — How to ask, by channel

The same question is asked differently per surface. The agent's prompt branches on `channel`.

| Channel | Posture | Format |
|---|---|---|
| `mac-popover` | Desk, full attention | One-line context + question + 2-3 quick-tap chip options OR free-text |
| `ios-ask` | Field, partial attention | One-line question + 2 chip options (Yes/No or A/B); free-text fallback |
| `ios-notification` | Lock screen, glance | Don't ask; defer to next app open |
| `email` | Async, full prose available | Asked inline in the agent-drafted message Chad's about to send: *"(I assumed X — flag if not.)"* — Chad fixes in the draft and the fix becomes the signal |
| `terminal` | Power user, structured | Prompt with options enumerated — same as Mac but no chip UI |

**Mac pop-over example:**

> Heads-up before I draft this — homeowner update for the cabinet slip:
> Want me to suggest a specific reschedule date, or describe the slip and let you talk to him first?
> [ Suggest date ]   [ Describe only ]   [ Skip — I'll write it ]

Three quick chips + always a skip. Skip is a `clarification_asked` with no answer attached → suppressed for 7 days for the same context.

### B4 — Capturing the answer back into the profile

The clarification protocol is closed-loop:

1. Agent asks → `clarification_asked` signal lands
2. Chad answers (or skips) → `clarification_answered` signal lands
3. `hb-profile` next nightly run reads both, decides:
   - **One-off** scope → answer applies once, doesn't update profile
   - **Project** scope → answer added to the profile's `project_overrides[<project_id>]`
   - **Durable** scope → answer updates the global profile field, with `source: "stated_by_chad"` and `confidence: 1.0`

Scope inference is from Chad's phrasing. "for this house" → project. "always" / "from now on" → durable. Otherwise → one-off, with the agent free to ask again if the same question recurs.

### B5 — Anti-pattern: never ask twice for the same thing

Every `clarification_asked` includes a `topic` key. The agent maintains a memory (in-conversation + persistent via signal) of topics asked in the last 30 days. Same topic → use the prior answer or its inferred default; do not re-ask. If the prior answer is now stale (e.g., signal that Chad changed his mind), the agent updates internally without re-prompting.

---

## Part C — Reflection ritual (weekly profile review)

A new surface — the **weekly digest** — lands every Monday morning. Two delivery channels:

1. **In the morning brief** (existing `hb-brief`, expanded): a new "What I think I'm learning" section, 3–5 bullets, each a one-line pattern with a confirm/reject affordance.
2. **In `hb-chad` Cmd+/**: the first time the pop-over opens on a Monday, a single reflection card appears above the composer.

### Format

Per-pattern card:

```
┌──────────────────────────────────────────────┐
│  I noticed: framing always starts on a       │  pattern statement (Chad-voiced)
│  Tuesday for projects with Monday walkthroughs.│  
│                                              │
│  Evidence: 7 of last 8 framing starts.       │  evidence count
│                                              │
│  [ Yes — default this ]                      │  pattern_confirmed
│  [ No — context-specific ]                   │  pattern_rejected
│  [ Stop noticing this ]                      │  suppression
└──────────────────────────────────────────────┘
```

Three buttons, three signals. "Stop noticing this" silences the pattern *and* tells the profile builder this category is over-fitted — useful negative signal.

### Cap

Max **3 patterns per week**. The profile builder ranks candidate patterns by evidence-strength × novelty (haven't surfaced this exact pattern in last 90 days) and surfaces the top 3. Below 3 evidence-count → not surfaced.

### What this prevents

Without the reflection ritual, the profile drifts silently. With it:
- Chad knows what the system thinks it knows
- Wrong learnings get killed before they compound
- Right learnings jump from "inferred" to "stated" — high confidence, immune to decay
- The agent feels like it's asking permission rather than acting on assumptions

---

## Part D — Profile evolution (confidence, source, decay)

Migration 004's `Profile JSONB v1` is flat: every field is just a value. Part D layers structure on top — without migrating the table — by changing what the JSONB *contains*. (The migration explicitly allows this: "Profile JSONB shape NOT enforced at the DB layer" — Pydantic owns the shape.)

### D1 — Per-fact metadata

Every learned fact in the profile gets wrapped:

```json
{
  "decision_patterns": {
    "common_vendors": {
      "windows": {
        "value": "Andersen",
        "confidence": 0.92,
        "source": "observed",
        "evidence_count": 14,
        "scope": "durable",
        "last_reinforced_at": "2026-04-30T12:00:00Z",
        "decay_eligible": false
      },
      "concrete": {
        "value": "Coastal Concrete",
        "confidence": 1.00,
        "source": "stated_by_chad",
        "evidence_count": 23,
        "scope": "durable",
        "last_reinforced_at": "2026-05-05T08:30:00Z",
        "decay_eligible": false,
        "stated_at": "2026-04-12T14:22:00Z"
      },
      "plumbing": {
        "value": "Hammond",
        "confidence": 0.41,
        "source": "observed",
        "evidence_count": 4,
        "scope": "durable",
        "last_reinforced_at": "2026-03-15T09:00:00Z",
        "decay_eligible": true,
        "last_correction_at": "2026-04-22T16:00:00Z",
        "correction_text": "Hammond ghosted me on Pelican"
      }
    }
  }
}
```

### D2 — Source attribution (rank order)

| Source | Trust weight | Decay-eligible? |
|---|---|---|
| `stated_by_chad` (preference_stated) | 1.00 | No |
| `confirmed_by_chad` (pattern_confirmed) | 0.95 | No |
| `corrected_by_chad` (correction_received → updated value) | 0.90 | After 180d without reinforcement |
| `observed` (frequency-based inference) | scales with evidence_count, capped at 0.85 | Yes |
| `inferred` (cross-signal correlation, e.g., "Chad uses voice 80% on Mondays") | capped at 0.60 | Yes |

A `correction_received` signal **drops** the corrected fact's confidence to ≤ 0.30 immediately, and removes any prior `observed` evidence. A correction is a hard reset.

### D3 — Decay rules

- Decay-eligible facts lose 10% confidence per 90 days without reinforcement.
- Confidence < 0.30 → fact is dropped from the profile entirely (rather than carrying low-quality state).
- Stated / confirmed facts are immune unless explicitly corrected.
- A reinforcement is any signal that ratifies the fact: another observation, a non-correcting use, an unrelated signal that aligns with the fact.

### D4 — Scope hierarchy

Three scopes; lookups walk the hierarchy:

1. **`one-off`** — applied to a single decision; not stored in profile, only in signals.
2. **`project`** — stored in `profile.project_overrides[<project_id>]`. Overrides durable for that project only.
3. **`durable`** — global default. Applies to any project without an override.

When the agent needs a value, it asks: "is there a `project_overrides[<active_project>]` entry? Yes → use it. No → use durable. No durable → ask (Part B), or proceed with flagged best-guess."

---

## Part E — Product instinct: surfacing build candidates to Patton AI

The most valuable thing the agent can learn from Chad isn't *how to act like Chad* — it's *what's missing from the system that would make Chad's life easier*. Chad shouldn't have to file feature requests; he won't, his schedule's too busy. The agent watches him work, notices friction, and tells **Connor (Patton AI)** what to build next.

This loop is **separate from Chad's experience.** Chad never sees the build-candidate output. He doesn't get asked "should we build this?" He just keeps working. Connor gets a weekly product-instinct report that says "here's what Chad's been hitting friction on; here's what we should consider building."

### E1 — What counts as a friction signal

The agent watches for five patterns. Each derives from existing signals + `engine_activity` — no new emission code needed.

| Pattern | What it looks like | What it suggests |
|---|---|---|
| **Manual repetition** | Chad does the same multi-step sequence ≥5 times in 14 days (e.g., open Tracker → switch to Cost Tracker tab → look up vendor → copy phone number → text vendor) | Candidate: a new tool/agent that collapses the sequence to one tap |
| **Asks the agent the same question** | Chad's `ask_query` signals contain ≥3 semantically-similar questions in 14 days ("when's the next inspection?", "is plumbing inspection booked?", etc.) | Candidate: a proactive surface (notification, dashboard widget, daily digest section) that answers the question before Chad asks |
| **Consistent override** | Chad overrides the same agent default ≥3 times in 30 days (e.g., the agent always proposes a 7-day delay, Chad always changes it to 5) | Candidate: the default itself is wrong; either tune it or expose a per-Chad preference |
| **Copy-paste / context-switch workflow** | Chad selects text from one Patton AI surface and pastes it into a non-Patton surface (Gmail, iMessage, Notes) — observable from `share_received` + `screen_view` patterns | Candidate: a native composer/share-target that handles the destination directly |
| **Long unstructured asks** | Chad's `ask_query` text length is consistently >200 chars and the agent's response triggers a `correction_received` >30% of the time | Candidate: a structured input surface for that intent (e.g., a Change Order form rather than NL parsing) |

### E2 — The build-candidate record

When the agent detects one of E1's patterns crossing threshold, it emits a `build_candidate` row to a new lightweight store — **not** in `home_builder.user_signal` (that's Chad-personal data). Lives in:

```
~/Projects/home-builder-agent/.product_instinct/candidates.jsonl
```

Append-only JSONL. Per row:

```json
{
  "id": "<uuid>",
  "detected_at": "2026-05-08T09:00:00Z",
  "pattern_type": "manual_repetition" | "repeated_question" | "consistent_override" | "context_switch" | "long_unstructured_ask",
  "evidence_count": 7,
  "evidence_window_days": 14,
  "evidence_refs": ["engine_activity:<uuid>", "user_signal:<uuid>", ...],
  "chad_behavior_summary": "Chad has texted vendors 11 times in 14 days, every time after looking up the vendor's phone in the Cost Tracker.",
  "suggested_build": {
    "kind": "new_agent" | "new_surface" | "new_default" | "tool_extension",
    "one_line": "A vendor-text agent that drafts SMS to a vendor with project context attached, fired from the Cost Tracker row.",
    "rationale": "Chad's repeating a 5-step manual sequence; collapsing it saves ~30s per occurrence and removes the context switch out of Patton AI to Messages.",
    "estimated_effort_days": 1.5,
    "rough_priority": "medium" | "high" | "low",
    "blockers_or_dependencies": ["Twilio integration not yet in place"]
  },
  "chad_visible": false                        // explicit: this is for Connor, not Chad
}
```

The `suggested_build` block is *the agent's product instinct.* It's a hypothesis, not a directive. Connor reviews; Connor decides; Chad never sees it.

### E3 — Surfacing to Connor

A new agent — **`hb-instinct`** — runs weekly (Sunday 8pm via launchd, doesn't compete with the morning brief). It:

1. Reads `engine_activity` + `user_signal` for the past 14 days
2. Runs the five E1 detectors
3. Appends new `build_candidate` rows to the JSONL
4. Composes a single email to Connor: "Patton AI Product Instinct — Week of 2026-05-04"

The email format:

```
PATTON AI — PRODUCT INSTINCT
Week of May 4 — based on Chad's actual usage

3 candidates this week (1 high, 2 medium):

────────────────────────────────────────────────
[HIGH]  Vendor-text agent
────────────────────────────────────────────────
Chad's manually texting vendors 11 times in 14 days, every
time after looking up the phone number in the Cost Tracker.

What to build: an SMS draft tool fired from a Cost Tracker
row — auto-attaches project + phase context, sends to
vendor's number on file.

Rough effort: 1.5 days. Blocker: Twilio not yet wired.
Evidence: engine_activity #abc123, #def456, #ghi789 (+8 more)

[ Build it ]   [ Park it ]   [ Not now — re-surface in 30d ]

────────────────────────────────────────────────
[MEDIUM]  ...
```

Each candidate has three actions, link-targeted at a tiny FastAPI endpoint that updates the candidate's status in the JSONL:

| Action | Effect |
|---|---|
| **Build it** | Status flips to `accepted`. Optional: auto-creates a TODO entry under the home-builder-agent repo backlog (a future integration). |
| **Park it** | Status flips to `parked`. Won't surface again unless evidence_count doubles. |
| **Not now — re-surface in 30d** | Status flips to `snoozed`, with `re_surface_at`. Reappears at the snooze date. |

### E4 — Anti-double-counting

A candidate that's been **accepted** or **parked** suppresses re-detection of the same pattern for 90 days. Otherwise the agent would re-surface "Chad keeps texting vendors" weekly, which becomes noise. Snoozed candidates re-surface only at their re-surface date.

### E5 — Quality control: avoid hallucinated candidates

Hard rules to keep `hb-instinct` from making up problems:

- **Minimum evidence_count = 5.** Below 5 occurrences in 14 days, the pattern doesn't exist yet.
- **Maximum 5 candidates per weekly email.** If the detector fires more, rank by `evidence_count × est_value / est_effort` and surface the top 5. The rest go in the JSONL but not the email.
- **No candidate without specific evidence_refs.** Every candidate cites the actual signals / activity rows that support it. If the agent can't cite, it can't claim the pattern exists.
- **`suggested_build` may be wrong; that's fine.** The detection of friction is the value; the agent's solution proposal is just a starting point. Connor's decision overrides any suggestion.
- **Cost cap: $0.10 per `hb-instinct` weekly run.** If Sonnet gets verbose, cap the prompt + response token budget.

### E6 — What this enables

- **Chad never has to file a feature request.** The system files them on his behalf, derived from his actual behavior.
- **Connor's roadmap is grounded in real usage, not guesses.** Every candidate has citation-quality evidence (specific signal IDs).
- **Friction has a half-life.** Pain points get surfaced within 14 days of becoming patterns, not months later when Chad finally complains.
- **Multi-builder generalization.** When Greg's Custom Homes onboards, the same `hb-instinct` runs against Greg's signals; comparing Chad's candidates to Greg's surfaces *which patterns are universal* (build for everyone) vs *which are individual* (per-builder).
- **Sales surface.** "Patton AI watches your work and tells me what to build next for you" is a stronger pitch than "tell me what features you need." This is the difference between a vendor and a partner.

### E7 — Privacy + boundary

- **Connor sees patterns + counts + specific signal IDs**, *not* full signal payloads. The email shows "11 vendor texts in 14 days" — not the actual text content of those texts.
- **Chad can opt out of `hb-instinct`** in the privacy-review screen ("Don't suggest features based on my usage"). Default: opted in (since the output goes to Connor, not Chad, and improves the product Chad uses). Opt-out is a per-builder setting; updated nightly from `user_profile.preferences.allow_product_instinct`.
- **`hb-instinct` only runs against builders who've opted in.** Multi-tenant Phase B forward-compat baked in.
- **No raw rationale text in the email.** `correction_text`, `rationale_text`, etc. stay in the signal store. The candidate body uses behavior summaries, not Chad's words.

### E8 — Why this matters for Patton AI as a business, not just Chad's experience

The build-candidate stream is the **most valuable artifact this whole system produces** for the company, separate from its value for Chad. Reasons:

1. **Roadmap correctness.** Most product roadmaps are 80% wrong because they're built on assumptions. This roadmap is built on observed friction.
2. **Sales evidence.** "Here's what we built for Chad in his first 90 days, derived from watching him work" is a demo that closes builders.
3. **Defensibility.** Anyone can copy the agent surface. Nobody else has the build-candidate stream — it's a function of *being deployed*, not of being clever. Multi-builder data compounds it further.
4. **Connor's leverage.** Without `hb-instinct`, Connor has to imagine what Chad needs. With it, Connor responds to a curated weekly digest of evidence-backed candidates. Time spent on roadmap discovery → near zero. Time spent shipping the right things → maximized.

This is the part of the system that makes Patton AI a *learning company*, not just a learning agent.

---

## Part F — Implementation plan

Sequenced. Each step is testable in isolation. Parts A–D land first (the Chad-facing learning loop); Part E (`hb-instinct`) layers on once the signal store has 30+ days of history to detect from.

### Phase 1 — Chad-facing loop (Parts A–D)

| # | Step | Where | Effort | Depends on |
|---|---|---|---|---|
| 1 | Add 5 new signal types to `home_builder_agent/personalization/signal_vocab.py` (Pydantic enum + payload models) | this repo | half day | 004 cut |
| 2 | Mirror new types in iOS `Codable` `SignalType.swift` | patton-ai-ios | half day | step 1 |
| 3 | `hb-chad` system prompt: add the recognition rules for `correction_received`, `preference_stated`, `decision_rationale` opportunistic ask | this repo | day | step 1 |
| 4 | `hb-chad` clarification protocol: add when-to-ask logic (B1+B2), question-id memory (B5) | this repo | day | step 3 |
| 5 | Per-channel prompt branches in `hb-chad`: `mac-popover`, `ios-ask`, `email` get different format rules (B3) | this repo | half day | step 4 |
| 6 | `hb-profile` builder: extend with per-fact metadata wrapping (D1), source attribution (D2), decay logic (D3), scope hierarchy (D4) | this repo | 2 days | step 1 |
| 7 | Weekly digest: `hb-profile` emits top-3 candidate patterns; `hb-brief` consumes them in a new section; pop-over consumes them on first Monday open | this repo | day | step 6 |
| 8 | Pattern confirm / reject UI: chip buttons in morning email + pop-over reflection card | patton-ai-ios + this repo | day | step 7 |
| 9 | Profile-review surface: a new screen ("What I think I know about you") that shows the full profile with edit / delete affordances | patton-ai-ios | 2 days | step 6 |
| 10 | Telemetry: add a `learning_loop_health` log per nightly `hb-profile` run — clarifications asked, answered, skip rate, pattern confirm rate, correction rate | this repo | half day | step 7 |

**Phase 1 totals:** ~6 days this repo, ~3 days iOS.

### Phase 2 — Patton AI-facing loop (Part E)

Lands after Phase 1 has 30+ days of signals to detect against — earlier and `hb-instinct` will see noise.

| # | Step | Where | Effort | Depends on |
|---|---|---|---|---|
| 11 | Build `hb-instinct` agent: five E1 detectors, JSONL writer, email composer | this repo | 2 days | Phase 1 step 6 + 30 days of signals |
| 12 | FastAPI endpoint for the three candidate-status actions (`/v1/instinct/<id>/{build,park,snooze}`) | shell-backend | half day | step 11 |
| 13 | launchd plist `com.chadhomes.product-instinct.plist` running Sundays 8pm | this repo | quarter day | step 11 |
| 14 | Privacy-review screen toggle for `allow_product_instinct` | iOS | half day | step 6 |
| 15 | Opt-out check in `hb-instinct` (skip the run if the user is opted out) | this repo | quarter day | step 14 |

**Phase 2 totals:** ~3.5 additional days, almost all in this repo.

Steps 1–4 deliver the field-side capture loop. Steps 6–8 deliver the reflection ritual. Step 9 delivers the transparency surface. Step 10 makes the loop measurable. Steps 11–15 turn the captured behavior into Patton AI's roadmap.

---

## Anti-patterns

- **Asking too often.** Daily cap = 2 clarifications. If the agent feels like it's interrogating, the partnership breaks.
- **Asking the same thing twice.** Topic memory is mandatory. Re-asking is the fastest way to lose Chad's trust.
- **Silent learning that surprises Chad.** Every learned pattern is surfaceable in the profile-review screen. Never let the agent's behavior change in a way Chad can't trace.
- **Paraphrasing Chad's words in signals.** `rationale_text`, `correction_text`, `raw_statement` are stored verbatim. The profile builder may interpret; the signal store keeps the source.
- **Treating corrections as edge cases.** Corrections are 5× weight. They're the most valuable signal we capture. If the system feels neutral about a correction, the math is wrong.
- **Letting the profile grow unbounded.** Decay + drop-below-0.30 are real. A profile of 500 facts with 200 stale ones is worse than a profile of 80 high-confidence facts.
- **Surveillance framing.** Never write "I tracked that you…" — write "I noticed you…" or "I saw that…" The voice is partner, not observer.
- **Asking about things Chad never decides on.** Don't ask "what's your preferred font for the morning brief?" Asking gates exist for *operational* judgment calls, not stylistic minutiae.
- **Failing closed.** When the agent doesn't have the info and shouldn't ask (B2), it must proceed with a flagged best-guess and capture the eventual outcome as a `decision_rationale` candidate. Silent failure or silent wrong-answer is worse than a flag.

---

## Open questions

- **Profile-review screen on Mac vs iOS first?** iOS has the active engagement; Mac has the surface for serious review. Lean: **both**, but iOS first since the existing Activity tab is the natural neighbor.
- **Cost of nightly `hb-profile` runs at scale.** Today: 1 user, ~$0.05/run. At 100 builders: ~$5/night. Linear, fine. Worth a token cap per run? Lean: cap at $0.50 per user per run as a safety rail.
- **Decay rate empirical-tuning.** 10% per 90 days is a guess. After 6 months of real data, revisit — some categories (vendor preference) may be more stable; others (homeowner preference per project) may need faster decay.
- **Should `correction_received` trigger a one-time "any other corrections?" follow-up?** Lean: **no for v1**. One ask per moment. Multi-correction batches feel inquisitorial.
- **Privacy surface for stated preferences.** A stated preference is more sensitive than an observed pattern (Chad's spoken word, not just frequency). Worth a separate visibility tier in the profile-review screen (statements at top, observations below)? Lean: **yes**.
- **Cross-builder pattern transfer (Phase B / multi-tenant).** When Greg's Custom Homes signs on, can Greg's profile bootstrap from anonymized observations across all builders? Lean: **no by default**, opt-in via a future privacy setting; out of scope for v1.
- **Voice rationale capture in the field.** A long-press on the iOS Ask tab → "tell me why you picked X" voice-to-text → `decision_rationale` signal. Low friction, voice-native. Lean: **build in v1.1** after the basic loop is stable.
- **Correction surfaced as a follow-up to the agent's original answer?** When Chad says "no, that's wrong," should the agent proactively try again with the correction baked in? Lean: **yes** — silence after a correction wastes the signal. Phrasing: "Got it — let me try that again. With Hammond ghosting on Pelican factored in, here's what I'd say…"
