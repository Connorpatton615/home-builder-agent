# Migration 003 — Review Doc

> Inline-SQL review surface for `home_builder.engine_activity` + new event
> type registrations + the `hb-router` chokepoint rule. Both repos edit
> this same markdown freely until aligned; once green-lit, the SQL gets
> cut into
> `~/Projects/patton-ai-ios/backend/migrations/003_engine_activity_and_event_types.sql`
> and applied to the existing Supabase project (`neovanarazgxwpihuhep`).

**Status:** Draft for CTO review.
**Phase:** A v0 product surface (per `turtle_contract_v1.md` § 3 + Connor's locked v0 vision).
**Owner (cut):** patton-ai-ios CTO.
**Owner (column-type alignment + chokepoint enforcement):** home-builder track.
**Last updated:** 2026-05-06.
**Cross-references:**
- `~/Projects/home-builder-agent/docs/specs/canonical-data-model.md`
- `~/Projects/home-builder-agent/docs/specs/migration_002_review.md`
- `~/Projects/patton-ai-ios/docs/03_build/turtle_contract_v1.md`
- `~/Projects/patton-ai-ios/backend/migrations/002_home_builder_schema.sql`

---

## What this migration does

Lands the persistence + API surface that makes the **Activity** tab and
the **Ask** tab real on the iOS side.

Three goals, in priority order:

1. **`home_builder.engine_activity` table.** Every autonomous action
   Claude takes on Chad's behalf gets a row here. Surfaces in the iOS
   Activity tab as an audit log so Chad can see what his AI assistant
   did and when.
2. **Two new event types — `email-urgent` and naming alignment.**
   Push notification surface needs these registered as canonical
   `home_builder.event.type` values so the dispatcher can route them.
3. **SSE stream contract for `/v1/turtles/home-builder/ask`.** Documents
   the wire format for streaming Claude's answer + tool-use to iOS chat.

Out of scope for `003`: phase-photo attachment table (`home_builder.attachment`)
— flagged in §6 below as the natural next migration (004) when the v1
photo feature lands.

---

## Design decisions

Numbered so we can reference them in review comments.

1. **Engine_activity is a *new* canonical entity, not a UserAction subset.**
   Two distinct concerns:
   - `home_builder.user_action` (already in 002): Chad's *explicit*
     inputs (taps a button, types a command). Captured per
     canonical-data-model § entity 16.
   - `home_builder.engine_activity` (new): Claude's *autonomous*
     decisions on Chad's behalf. The Q1c true-assistant model means
     Claude can take actions without per-step confirmation; activity
     log is the audit trail.

   Three categories of action across the system:
   | Category | Logged where |
   |---|---|
   | Claude-autonomous via chat/voice | `engine_activity` (hb-router writes) |
   | Chad-explicit-tap via iOS button | `user_action` (already exists) |
   | Direct CLI from terminal (operator use) | nothing — not "agent acting on Chad's behalf" |

2. **Single chokepoint = `hb-router` (LOAD-BEARING).** Per the same
   load-bearing rule as `Event` in canonical-data-model § entity 17
   ("many emitters, one owner, one store"), `hb-router` is the *only*
   path that creates `engine_activity` rows. The 14 existing agents
   stay unaware of the table and don't write to it — direct CLI calls
   to `hb-receipt`, `hb-update`, etc. don't generate activity rows.

   Why this matters: every future agent inherits activity-logging for
   free if it's invoked via `hb-router`. New agents don't need to know
   about the table. Drift surface = zero.

   Rule formal statement (mirroring entity 17 Rule 2 wording):
   > **Rule 3 — One activity store, one writer.** `home_builder.engine_activity`
   > is written *only* by `hb-router`. Direct CLI invocations of agents
   > do NOT create activity rows. UserAction-driven flows (iOS POST
   > /actions → reconcile dispatch) do NOT create activity rows. Only
   > Claude-autonomous flows through `hb-router` create rows. If a
   > future feature seems to need a different writer, the design is
   > wrong — make it route through `hb-router` instead.

3. **`outcome` enum captures the four real states.** `success` /
   `partial` / `error` / `rejected`. The `rejected` case covers the
   subset of Q1c actions that still require Chad confirmation (sending
   email to clients, financial transactions) where Claude proposes,
   Chad declines.

4. **`affected_entity_type` + `affected_entity_id` for jump-to-detail.**
   When Chad taps an activity row in the iOS Activity tab, the app
   needs to deep-link to the canonical entity Claude mutated (the
   receipt that got logged, the change order that got drafted, etc.).
   Polymorphic FK pattern same as `user_action` — discriminator + UUID,
   no DB-level FK constraint.

5. **`parameters` as JSONB.** Claude's classification of the user's
   intent + the structured params it extracted. Lets Chad see "Claude
   parsed your request as: log_receipt(amount=400, vendor='Wholesale
   Plumbing', category='Plumbing')." Useful for transparency + debugging.

6. **`tenant_id UUID NULL` on the new table.** Same Phase B forward-compat
   as 002. NULL-allowed in Phase A.

7. **RLS scoped via the standard helper.** Activity rows are
   project-scoped when `project_id` is non-null; otherwise fall back to
   actor-only read (same as `user_action` policy in 002).

8. **Two new event types added to `home_builder.event.type`:**
   - `email-urgent` — fired by inbox-watcher when a Gmail thread
     classified urgency=high. Drives the iOS push notification for
     "high-urgency client/sub email arrives" (one of the 5 critical
     triggers).
   - **Weather event type — naming alignment confirmation needed.**
     Canonical model uses `weather-delay` as the Event type emitted from
     a WeatherImpact (entity 15) when severity ≥ warning. CTO's dispatch
     spec mentioned `weather-impact`. My read: keep `weather-delay`
     (already in Pydantic schema enum), don't add `weather-impact`.
     Confirm in review.

9. **No CHECK constraint on `event.type` enum.** Per 002 design
   decision 4 (TEXT + open enum so new types don't require schema
   migrations). Just register the new types in the canonical model spec
   + Pydantic schemas.

---

## Table inventory

| # | Table | Project-scoped? | Notes |
|---|---|---|---|
| 1 | `home_builder.engine_activity` (NEW) | yes (denormalized, nullable) | Audit log for Claude-autonomous actions |

Plus enum updates (no schema change, doc-only registration):
- `home_builder.event.type`: add `email-urgent`. Confirm `weather-delay` (existing).

---

## SQL

```sql
-- =============================================================================
-- migration_003_engine_activity_and_event_types.sql
-- =============================================================================
-- Adds the engine_activity audit table (Q1c true-assistant audit surface)
-- and registers two new event types in canonical-data-model.md (no schema
-- change; event.type is open-enum TEXT per 002 design decision 4).
--
-- See ~/Projects/home-builder-agent/docs/specs/migration_003_review.md
-- for the chokepoint rule (Rule 3) and field-level rationale.
-- =============================================================================

-- PREREQUISITES (provided by 002_home_builder_schema.sql):
--   • pgcrypto extension
--   • home_builder schema
--   • home_builder.user_can_access_project() helper
--   • home_builder.project, home_builder.event tables
--   • public.user_turtle_projects bridge
--   • is_master() function
-- =============================================================================

-- 1. engine_activity ----------------------------------------------------------
-- Audit log for Claude-autonomous actions taken via hb-router.
-- LOAD-BEARING: hb-router is the only writer (Rule 3, see review doc).
-- Direct CLI invocations and UserAction-driven flows do NOT write here.
CREATE TABLE IF NOT EXISTS home_builder.engine_activity (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id               UUID REFERENCES auth.users(id) ON DELETE SET NULL,
                                -- Whose intent triggered this action (Chad, in iOS).
                                -- Nullable so background scheduled actions (no actor)
                                -- can land here too — e.g., a future "Claude proactively
                                -- did X overnight" feature.

    project_id                  UUID REFERENCES home_builder.project(id) ON DELETE RESTRICT,
                                -- Denormalized for RLS perf (decision 6 in 002).
                                -- Nullable for cross-project actions
                                -- (e.g., "show me all overdue invoices").
                                -- ON DELETE RESTRICT (not CASCADE): Activity is an audit
                                -- surface and outlives deleted projects (mirrors Event in
                                -- canonical-data-model § entity 17). Soft-delete via
                                -- project.status = 'archived' is the actual workflow.
                                -- Hard-delete with audit history attached is the wrong
                                -- behavior for a compliance/debugging table.

    surface                     TEXT NOT NULL DEFAULT 'chat'
                                CHECK (surface IN ('chat', 'voice', 'cli', 'background')),
                                -- Where the action originated. Most rows = 'chat' or
                                -- 'voice' (the iOS Ask tab). 'cli' would only appear
                                -- if hb-router is invoked directly from terminal
                                -- (rare — operator path bypasses hb-router today).
                                -- 'background' for future scheduled Claude actions.

    invoked_agent               TEXT NOT NULL,
                                -- Which underlying agent ran (e.g., 'hb-receipt',
                                -- 'hb-update', 'hb-change'). Useful for filtering
                                -- the Activity tab by what kind of work was done.

    user_intent                 TEXT NOT NULL,
                                -- Chad's original NL input as captured.
                                -- ("Log a $400 receipt from Wholesale Plumbing")
                                -- This is what shows in the Activity tab as the
                                -- "what you said" line.

    classified_command_type     TEXT,
                                -- Router's classification slug (e.g., 'log-receipt',
                                -- 'phase-update', 'change-order'). Lets the Activity
                                -- tab group/filter actions semantically.

    parameters                  JSONB NOT NULL DEFAULT '{}'::jsonb,
                                -- Structured params the router extracted from
                                -- user_intent and passed to invoked_agent.
                                -- Transparency: Chad can see how Claude parsed his
                                -- words. Debugging: when an action fails, params
                                -- explain what the agent received.

    outcome                     TEXT NOT NULL
                                CHECK (outcome IN ('success', 'partial', 'error', 'rejected')),
                                -- 'success': agent ran cleanly, work landed
                                -- 'partial': agent ran, some work landed, some didn't
                                -- 'error': agent failed; nothing committed
                                -- 'rejected': Claude proposed action requiring
                                --             confirmation (send-email, financial),
                                --             Chad declined

    result_summary              TEXT NOT NULL DEFAULT '',
                                -- Human-readable summary for the Activity tab.
                                -- ("Logged $400 receipt to Plumbing line. Budget at 87%.")
                                -- Generated by the agent itself; router passes through.
                                -- NOT NULL DEFAULT '' so iOS doesn't have to handle
                                -- null/optional rendering for the row title; agents
                                -- generating no-op summaries set explicit empty string.

    affected_entity_type        TEXT,
                                -- 'phase' / 'invoice' / 'change-order' / 'receipt' / etc.
                                -- The canonical entity Claude mutated. Polymorphic
                                -- per same pattern as user_action (decision 4).
                                -- Nullable — some actions are queries, not mutations.

    affected_entity_id          UUID,
                                -- The specific entity UUID. Lets iOS deep-link
                                -- from an Activity row → entity detail.

    cost_usd                    NUMERIC(8, 4),
                                -- Anthropic API cost for this single activity.
                                -- Sum across rows = Chad's daily/weekly Claude bill.

    duration_ms                 INTEGER,
                                -- Wall-clock duration: NL parse + tool calls +
                                -- agent execution. Helps spot slow paths.

    error_message               TEXT,
                                -- If outcome='error' or 'partial', what failed.
                                -- Plain text. Not stack traces — those go to logs.

    tenant_id                   UUID NULL,
                                -- Phase B forward-compat per 002 decision 8.

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================================
-- Indexes
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_hb_activity_actor_recent
    ON home_builder.engine_activity(actor_user_id, created_at DESC);
    -- Activity tab default query: "what has Claude done for me, newest first"

CREATE INDEX IF NOT EXISTS idx_hb_activity_project_recent
    ON home_builder.engine_activity(project_id, created_at DESC);
    -- Per-project Activity filter: "what has Claude done on Whitfield"

CREATE INDEX IF NOT EXISTS idx_hb_activity_outcome_recent
    ON home_builder.engine_activity(outcome, created_at DESC)
    WHERE outcome IN ('error', 'partial');
    -- Surface failed/partial actions for debugging + Chad's "what went wrong"

CREATE INDEX IF NOT EXISTS idx_hb_activity_command_type
    ON home_builder.engine_activity(classified_command_type, created_at DESC);
    -- "Show me all receipt-logging actions this week"

-- =============================================================================
-- RLS
-- =============================================================================

ALTER TABLE home_builder.engine_activity ENABLE ROW LEVEL SECURITY;

-- SELECT: actor reads their own; project members read project rows;
-- master reads all
DROP POLICY IF EXISTS hb_activity_select ON home_builder.engine_activity;
CREATE POLICY hb_activity_select ON home_builder.engine_activity
    FOR SELECT USING (
        is_master()
        OR actor_user_id = auth.uid()
        OR (project_id IS NOT NULL AND home_builder.user_can_access_project(project_id))
    );

-- WRITE: service_role only. The hb-router process holds service_role and
-- enforces Rule 3 in code (this is the chokepoint).
DROP POLICY IF EXISTS hb_activity_write_service_only ON home_builder.engine_activity;
CREATE POLICY hb_activity_write_service_only ON home_builder.engine_activity
    FOR ALL USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
```

---

## Event type registrations (no schema change)

Per 002 design decision 4, `home_builder.event.type` is open-enum TEXT.
Adding new types requires no migration — just registration in the spec
+ Pydantic schemas.

### `email-urgent` (NEW)

| Field | Value |
|---|---|
| Emitter | `inbox-watcher` (`watchers/inbox.py`) |
| Trigger | Gmail thread classifier returns `urgency=high` |
| Required entity refs | None (cross-project) |
| Severity | `warning` (when client email) or `critical` (when sub no-show context) |
| Payload fields | `gmail_thread_id`, `subject`, `from_addr`, `urgency_reason`, `summary` |

Wired into the iOS push notification surface as critical-attention-only.

### `weather-delay` (CONFIRM EXISTING)

Already in canonical-data-model.md § Event types and Pydantic schema
`NotificationItemPayload.type` enum. CTO's "weather-impact" naming
was the entity (WeatherImpact, entity 15), not the Event type. Keeping
`weather-delay` as the canonical Event type. No change.

---

## SSE stream contract for `/v1/turtles/home-builder/ask`

Format: standard Server-Sent Events. Each event has three lines:
`id: <monotonic int>\n event: <type>\n data: <json>\n\n`.

### Event types

| Event | When fired | Payload |
|---|---|---|
| `text_delta` | Each token batch from Claude | `{"delta": "..."}` |
| `tool_use` | Claude invokes a tool | `{"id": "...", "name": "search_drive", "input": {"query": "..."}}` |
| `tool_result` | Tool returns result | `{"id": "...", "name": "search_drive", "duration_ms": 234, "summary": "Found 5 files"}` |
| `citation_added` | A file is opened with read_drive_file | `{"file_id": "...", "name": "...", "webViewLink": "..."}` |
| `message_complete` | Final message done | `{"answer": "<full text>", "citations": [...], "tools_called": [...], "cost_usd": 0.15, "duration_ms": 11000, "input_tokens": 8234, "output_tokens": 350}` |
| `error` | Anything errors | `{"type": "...", "message": "..."}` |

### Notes

- iOS chat UI renders `text_delta` events progressively into the active
  message bubble.
- `tool_use` events let iOS show a "thinking…" / "looking up Whitfield…"
  status indicator with the specific tool name.
- `citation_added` lets iOS render citation chips as files come in
  rather than waiting for the final message.
- `message_complete` is the terminal event — iOS can flush UI state and
  enable input field for next question.
- `error` is terminal too — surface in iOS as red banner under the
  message bubble.

### Reconnection — `Last-Event-ID` pattern (mirrors `/v1/runs` per task_011)

Chad uses this on flaky cellular in the truck. Connection drops mid-stream
are expected, not exceptional. The protocol:

- Server emits `id: <integer>` on every event. IDs are monotonic per
  stream (start at 1, increment per emission). Final `message_complete`
  carries the highest ID for the stream.
- Server buffers all events for the active stream in Redis with a 5-minute
  TTL (same buffer pattern + TTL as `/v1/runs/{run_id}/stream` per
  `task_011`).
- If the connection drops, iOS reconnects to the same stream URL with the
  `Last-Event-ID: <int>` HTTP header carrying the last ID it successfully
  processed.
- Server reads the buffer at that ID, replays events from `id+1` forward,
  then continues live emission.
- If `Last-Event-ID` is older than the 5-min buffer window, server emits
  an `error` event with `type: "stream_expired"` and iOS re-asks the
  question fresh.

iOS implementation note: SwiftUI's URLSession EventSource (or Foundation's
URLSessionDataTask + delegate parser) handles `id:` automatically. iOS
side wires the Last-Event-ID reconnection logic in week 2; v0 can ship
without reconnect logic and treat drops as fail-loud.

### CLI vs HTTP

The `hb-ask` CLI keeps current non-streaming behavior (returns full
JSON dict at end). Only the FastAPI route handler at
`/v1/turtles/home-builder/ask` consumes the streaming generator.

The streaming generator on the engine side emits `(event_id, event_type,
payload)` tuples. The route handler is responsible for serializing them
into the `id: ...\n event: ...\n data: ...\n\n` SSE format AND for
maintaining the Redis buffer for replay. Engine doesn't know about Redis
or SSE wire format — clean boundary.

---

## What lands next on the home-builder side

1. **hb-router** — NL command dispatch + the chokepoint that enforces
   Rule 3. Wraps the 14 existing CLIs behind one entry point. Will
   ship in parallel with this doc review.

2. **Streaming hb-ask refactor** — adds `ask_question_stream(question)`
   generator that yields the events above. Existing `ask_question()`
   stays as-is for CLI consumers.

3. **Pre-populated suggestion chips endpoint** — when Connor gets Chad's
   5 questions / 5 commands, I'll wire `/v1/turtles/home-builder/ask/suggestions`
   to return them as iOS chat-input chip suggestions.

---

## Future migration 004 — flagged here for context

Per Connor's 2026-05-06 vision update:

**Phase photos with vision-based spec checking** lands in v1, after v0
ships and Chad's daily usage validates priority. New migration 004 will
add:

```
home_builder.attachment (
    id, project_id, phase_id, file_url, mime_type,
    captured_at, captured_by_user_id, vision_analysis_text,
    spec_compliance_status, ...
)
```

Plus a Drive folder structure: `<project>/Phase Photos/<phase_name>/`
populated by the iOS upload path. Vision analysis via Claude with
images — composes on existing `hb-receipt` photo→Vision pattern.

Not in 003. Just flagging so the CTO knows where the photo feature
lands when it's queued.

---

## Open questions for CTO review

Lettered for easy reference in PR-style comments.

### Q-A — Confirm `weather-delay` over `weather-impact` for Event type naming

My read: `weather-delay` is the Event type (already in Pydantic schema +
canonical-data-model § Event types). `weather-impact` is the entity name
(WeatherImpact, entity 15). Your dispatch spec used `weather-impact`,
which I think was the entity reference, not a new Event type request.

Confirm: pushing on `weather-delay` Event emissions. No new event type
needed for weather. Just `email-urgent` is new.

### Q-B — `surface` enum: include `'background'` or defer?

I included `'background'` as a possible surface value (for future
"Claude proactively did X overnight" actions — e.g., morning-brief
generation could land in Activity log too). It's not used in v0.
Including it now means we don't need a CHECK constraint update later.

If you'd rather keep the v0 enum minimal (just `chat`, `voice`, `cli`),
I'll drop `background` and we add it when needed.

### Q-C — Should `result_summary` be NOT NULL?

Currently nullable. Argument for NOT NULL: the Activity tab always shows
a summary; a row without one renders awkward. Argument for nullable:
some actions might be too quick to summarize (a no-op).

Lean: NOT NULL with empty-string allowed. CTO's call.

### Q-D — Index on `(actor_user_id, project_id, created_at DESC)` composite?

The default Activity tab query is "my recent activity, optionally
filtered by project." A 3-column composite would serve that exactly.

Currently I have two separate indexes (actor_recent + project_recent).
The composite would be more selective for the filtered case, slightly
larger on disk. Marginal call.

Lean: keep two separate indexes for v0; add composite if profiling
shows the filter case is slow.

### Q-E — Does the SSE event format match your `/v1/runs/{run_id}/stream`?

You said you'll fork the wire format. I described the events I think
are useful (text_delta, tool_use, tool_result, citation_added,
message_complete, error). If your run-stream format uses different
event names or a different envelope, point me at the file and I'll
align.

### Q-F — Should `hb-router` write activity rows BEFORE invoking the agent (intent + pending), or AFTER (with outcome)?

Two options:
- **Before:** Insert with outcome='pending', UPDATE to final outcome
  after the agent returns. Pro: every activity is visible mid-flight
  in the Activity tab. Con: extra UPDATE per activity.
- **After:** Insert once, after the agent returns. Pro: simpler. Con:
  in-flight activities don't show in Activity tab until they complete
  (could be 30s for a complex action).

Lean: **After** for v0. Activity tab is for audit, not progress
tracking. Streaming progress already happens in the Ask tab via SSE.

---

## Sequencing per the workflow we agreed

1. **You** review this doc, comment inline, answer Q-A through Q-F.
2. **You** ship (a) Pydantic + JSON Schema for the new event types + the
   activity payload (mirrors the master/daily/weekly/monthly view-model
   pattern from 002).
3. **I** revise this doc per your comments, cut
   `~/Projects/patton-ai-ios/backend/migrations/003_engine_activity_and_event_types.sql`
   verbatim from the SQL block above (with revisions baked in), apply
   to Supabase staging.
4. **I** stub `/v1/turtles/home-builder/activity` and
   `/v1/turtles/home-builder/ask/stream` route handlers.
5. **You** wire hb-router to write activity rows; refactor hb-ask for
   streaming.

---

## Appendix — entity ↔ canonical-model § cross-reference

| Entity | Canonical model § |
|---|---|
| `engine_activity` | (NEW — to be added to canonical-data-model.md as entity 18) |
| `event.type=email-urgent` | (NEW — added to § Per-type payload contract) |
| `event.type=weather-delay` | (existing — § Per-type payload contract) |

---

## Round 2 — CTO Review Answers Applied (home-builder track)

All 6 Q-A through Q-F answers + the structural correction baked into
the SQL block above. Verifications:

| # | Item | Where it landed |
|---|---|---|
| Q-A | `weather-delay` confirmed as Event type, `weather-impact` is the entity name (not a new event type). Just `email-urgent` is new. | § Event type registrations — confirmed. No SQL change. |
| Q-B | Keep `'background'` in `surface` enum for forward-compat (proactive Claude work via launchd jobs later). | § SQL — `surface` CHECK already includes `'background'`; no change. |
| Q-C | `result_summary NOT NULL DEFAULT ''` — empty string allowed for genuine no-op summaries; iOS doesn't have to handle nullable rendering. | § SQL `engine_activity` table — `result_summary TEXT NOT NULL DEFAULT ''` plus an explanatory comment. |
| Q-D | Keep two separate indexes (actor_recent + project_recent + outcome_recent + command_type). No composite for v0; revisit if `EXPLAIN ANALYZE` shows the both-filters-set query is slow at volume. | § Indexes — no change. |
| Q-E | Don't mirror `/v1/runs/{run_id}/stream` envelope; typed events with named `event:` field are the right idiom for `/ask`. **Add `Last-Event-ID` reconnection pattern with `id:` lines + Redis 5-min TTL replay buffer.** | § SSE stream contract — added "Reconnection" subsection documenting the pattern + the engine-vs-route-handler boundary (engine yields tuples; route serializes + buffers). |
| Q-F | Insert AFTER agent dispatch, not before. Activity is observability, not progress tracking. Single insert per action, append-only-ish, halves writes. Audit-loss on router crash is acceptable since the agent's own transaction is atomic. | § Design decisions — already aligned (decision #1 + the chokepoint description). `hb-router` code at `home_builder_agent/agents/router_agent.py` already inserts AFTER `_invoke_agent` returns. |

**Structural correction (CTO's note beyond the Q's):**

| Item | Where it landed |
|---|---|
| `project_id ON DELETE RESTRICT`, not `CASCADE`. Activity rows outlive deleted projects (mirrors Event in canonical model § entity 17). Soft-delete via `project.status='archived'` is the actual workflow. | § SQL `engine_activity` table — `ON DELETE RESTRICT` + comment explaining why (audit immutability). |

**Status:** Greenlit per the "round-2 revisions in, ready to cut" gate.
CTO can cut `003_engine_activity_and_event_types.sql` from the SQL
block above verbatim.

---

## Cut log

- **2026-05-06** — Round 2 revisions baked by home-builder track per
  CTO's Q-A through Q-F answers + the `ON DELETE RESTRICT` structural
  correction + the `Last-Event-ID` SSE reconnection pattern. Migration
  not yet cut to
  `~/Projects/patton-ai-ios/backend/migrations/003_engine_activity_and_event_types.sql`
  — CTO's next pass.
