# The Chad Agent

> One-line summary: a persona layer on top of the existing 21 specialists that *is* Chad Lynch — speaks in his voice, knows his preferences, makes his judgment calls — and represents him both inward (chief-of-staff) and outward (drafts emails, answers for the team).

**Status:** Spec — design locked, build queued.
**Phase:** 2 (post-iOS-TestFlight; co-equal with `vendor-intelligence-system.md`).
**Owner:** CP.
**Last updated:** 2026-05-07.
**Customer:** Chad Lynch — Palmetto Custom Homes, Baldwin County, AL.
**Cross-references:**
- [`canonical-data-model.md`](canonical-data-model.md) — engine state Chad's context loads from
- [`scheduling-engine.md`](scheduling-engine.md) — view-models the agent reasons over
- [`migration_004_review.md`](migration_004_review.md) — `user_signal` / `user_profile` tables (block on this for live memory)

---

## What this is, what it isn't

Today Chad has 21 specialist agents. Each does one thing well. The friction is that **Chad still has to know which agent to invoke for what**. The Chad Agent removes that — Chad just talks. The agent figures out what to do, in Chad's voice.

It is **not** a new specialist. It is a thin **persona layer** that uses the existing specialists as primitives:
- `hb-router` for actions (write path)
- `hb-ask` for retrieval (read path)
- `hb-profile` for personalization
- the scheduling engine for state

It is **not** a chatbot. It is **not** a routing layer. It is *Chad's AI extension* — the layer that holds judgment, voice, and memory and delegates execution to the specialists below.

Inward-facing role: chief-of-staff. Knows what Chad knows, anticipates what he needs, drafts in his voice.
Outward-facing role: Chad's representative. Drafts emails to clients, subs, vendors. Answers questions for the team. Speaks for Chad when Chad isn't there.

---

## Operational backbone — the workflows the agent sits on top of

There are five operational patterns in the home-builder stack today. The Chad Agent's job is to *compose* and *narrate* them in Chad's voice — not to replace any of them.

### W1 — Project birth
```
spec.md  →  hb-timeline  →  Drive Doc + Tracker Sheet
                                 │
                                 ├──→  hb-finance       (Cost Tracker bootstrap)
                                 ├──→  hb-bridge        (Tracker → Postgres)
                                 └──→  next morning's hb-brief includes new project
```

### W2 — Schedule change cascade
```
Chad: "framing pushed a week"
   │
   ├─[manual]→  hb-update         →  Tracker phase rows recompute
   │              │
   │              ├──→ procurement_alerts (inline)  →  notification + Tracker tab
   │              └──→ Dashboard auto-refresh by watcher
   │
   └─[iOS]→  hb-router  →  hb-update  (same path, with engine_activity row written)
```

### W3 — Money in/out
```
Chad photographs receipt          Chad types invoice line       Chad NL ledger entry
       │                                  │                              │
   hb-receipt                       inbox-watcher detects             hb-ledger
   (Vision)                         invoice + extracts              (Sonnet parse)
       │                                  │                              │
       └──────────────  Cost Tracker rows + Actuals Log + Drive receipts
                                 │
                                 └──→  Aging report  →  morning brief
```

### W4 — Field-to-engine write loop *(closes iOS ↔ engine)*
```
iOS Ask tab → POST /v1/turtles/.../actions → home_builder.user_action (Postgres)
                                                       │
                                                  hb-reconcile (60s loop)
                                                       │
                                                       └──→  engine entity writes
                                                                  │
                                                                  └──→  next view fetch
                                                                        reflects it
```

### W5 — Daily ritual
```
06:00  hb-brief (cron)
   ├── pulls weather, project status, invoices due, overnight HIGH emails
   ├── inspections expiring, unwaived payments, weather-risk phases
   └── sends to Chad

ALL DAY:
  inbox-watcher    →  classify, fire HIGH notifications, log invoices
  dashboard-watcher → refresh Tracker dashboards on edits
  reconcile        → drain user_action backlog into engine

10-min: watchdog → alert if any of the above silently dies

Mon 07:00  hb-client-update (cron) → homeowner status email
```

The Chad Agent observes all five and contributes to W2, W5, and any chain of specialists that requires composition in Chad's voice.

---

## Why a persona, not just routing

The 21 specialists already do the work. The Chad Agent's value is *judgment* — the layer between "which tool to use" and "what would Chad actually do here." Two real examples:

1. **A vendor emails: "Cabinets delayed two weeks."** A pure router triages it as HIGH. The Chad Agent thinks: *"Two-week cabinet slip with framing complete next Friday means I push trim/paint and the homeowner expects an update tomorrow."* → triggers `hb-update` (push trim 14d), drafts a homeowner email in Chad's voice explaining the slip, queues it for Chad's approval. Specialists chained, judgment glued.

2. **Chad asks "should I use Wholesale Plumbing again?"** A pure RAG returns invoices. The Chad Agent answers: *"Used them on Pelican Point last quarter — 2 days late on rough-in but pricing was 12% below Hammond. They're fine for a cost-driven phase, not for a critical-path one."* — that's profile + history + judgment, not retrieval.

---

## Anatomy

```
┌────────────────────────────────────────────────────────────────────┐
│ CHAD AGENT — claude-opus, persona: Chad Lynch                     │
│                                                                    │
│  System prompt embeds:                                             │
│   • Chad's voice profile     (tone, idioms, decision style)       │
│   • Active project context   (current state from engine)          │
│   • Recent activity log      (last N engine_activity rows)        │
│   • User profile             (preferences from hb-profile)         │
│   • Knowledge base anchors   (KB Drive docs, jurisdiction rules)  │
│                                                                    │
│  Tool belt (every specialist callable):                            │
│   • hb-router       → action dispatch (write actions)             │
│   • hb-ask          → retrieval (read actions)                    │
│   • engine view-models → schedule projections                     │
│   • profile_get/set → personalization                             │
│                                                                    │
│  Memory:                                                           │
│   • Per-conversation: short-term (current dialog)                 │
│   • Per-Chad:         long-term (user_profile JSONB, signals)     │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
       │                  │                   │
       ▼                  ▼                   ▼
   iOS Ask tab     Morning brief      Email drafts (client/sub/vendor)
   (voice in/out)  (narrative)        (Chad-voice composition)
```

---

## What it is, in code

A new agent — `hb-chad` — thin shell, ~200–400 lines. Most of its weight is the **system prompt** (voice + rules) and the **context loaders** (which tap existing infrastructure).

```python
# Pseudocode
def chad_agent(user_input: str, channel: str) -> str:
    context = {
        "voice":    load_chad_voice_profile(),       # static + learned
        "profile":  load_user_profile(chad.id),      # from hb-profile
        "projects": load_active_projects_summary(),  # from engine
        "activity": load_recent_activity(hours=24),  # from engine_activity
        "channel":  channel,                          # ios | email | terminal
    }

    response, actions = opus.complete(
        system=CHAD_PERSONA_SYSTEM + context_block(context),
        tools=[hb_router_tool, hb_ask_tool, view_model_tools, profile_tools],
        user=user_input,
    )

    for action in actions:
        action.execute()  # engine_activity-logged via hb-router

    return response  # Chad's voice, with citations + suggested follow-ups
```

Cost target: ~$0.05–0.30 per invocation (Opus + tool-calling depth-dependent), aligned with `hb-ask`.

---

## What it requires that doesn't exist yet

| Need | Status today | Gap |
|---|---|---|
| Chad-voice profile | Embedded in 3 agents (`brief`, `client-update`, `change`) — not centralized | Extract into `core/chad_voice.py` shared prompt |
| User profile (preferences) | `hb-profile` builds; migration 004 not yet cut | Land migration 004 → flip `hb-profile` to write live |
| Active project context | Postgres has it; no compact summary endpoint | Add `engine.get_chad_context()` |
| Recent activity rolling window | `engine_activity` table exists | Add `load_recent_activity()` helper |
| Tool-calling glue | `hb-ask` already does Opus + tool-calling | Refactor to share tool defs with `hb-chad` |
| Channel awareness | None — agents always assume Terminal | Add `channel` arg; vary verbosity by surface |
| Long-term memory | `user_profile` JSONB (designed, not live) | Same migration 004 dependency |
| Decision history | `engine_activity` is read-only audit | Layer "did Chad approve / override?" via `user_signal` |

The two real blockers: migration 004 being cut to Supabase, and the centralized Chad-voice module. Everything else is integration.

---

## Build order

1. **Ground.** Extract `core/chad_voice.py` from existing agents. Single source of truth for tone, phrasing rules, decision style. (~1 session)
2. **Context.** Write `engine.get_chad_context()` + `load_recent_activity()`. Compact JSON shape, prompt-embeddable. (~1 session)
3. **Agent shell.** `hb-chad` — Opus + tool belt + persona prompt + context. Terminal-only at first. (~1 session)
4. **Channel routing.** Wire into iOS Ask tab (replaces direct `hb-ask`) + into morning brief composer (replaces hand-rolled prompts). (~1 session)
5. **Memory.** Once migration 004 lands, swap profile loader to live DB read; write to `user_signal` on every Chad approval/override so the agent learns over time. (~1 session)

Five sessions to a real Chad-the-master. Most pieces are already built — this layer mostly *composes* what exists.

---

## Why we should build it

The leverage of the 21 specialists is currently capped because Chad still has to know the menu. The Chad Agent removes the menu. From a product standpoint that turns "a stack of useful tools" into "an AI extension of Chad" — which is what's actually sellable to a second customer.

---

## Cross-tenant horizon (deferred, noted only)

The same `hb-chad` code parameterized by `actor_user_id` becomes "Greg" for Greg's Custom Homes, "Maria" for another builder. Same specialist pool, same engine, different voice/profile/project scope. This is the multi-tenant turtle pattern — **out of scope for this spec** (which is home-builder-agent only) but worth noting that nothing in this design blocks it.

---

## Open questions

- **Voice profile authoring.** Hand-write the initial Chad-voice prompt vs. extract via Sonnet from a corpus of his existing emails? Probably extract: faster, more grounded, gets refined over time via `user_signal`.
- **iOS Ask tab routing.** Does the iOS shell call `hb-chad` directly (bypassing `hb-ask`), or does `hb-chad` become a deeper composer that `hb-ask` upgrades into? Lean: `hb-chad` is the new top, `hb-ask` becomes one of its tools.
- **Approval gate for outward-facing drafts.** Chad-voice emails to clients should not auto-send. Default: draft to Gmail, await Chad's send. The agent suggests a window ("send within 24h") but never sends without confirmation.
- **Cost ceiling per conversation.** Opus + multi-tool loop can spike. Suggested: hard cap at $0.50 / conversation, surface a warning when approaching, fall back to Sonnet for follow-ups within the same conversation.
- **Memory bleed across customers.** Once multi-tenant: confirm `user_profile` is fully scoped by `actor_user_id` and there's no global state (caches, in-memory) that could leak preferences across builders. Test before second customer onboards.
