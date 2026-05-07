# Migration 004 — Review Doc

> Inline-SQL review surface for `home_builder.user_signal` +
> `home_builder.user_profile` — the persistence layer for the
> always-learning-Chad ecosystem. Both repos edit this same markdown
> freely until aligned; once green-lit, the SQL gets cut into
> `~/Projects/patton-ai-ios/backend/migrations/004_user_signal_and_profile.sql`
> and applied to the existing Supabase project (`neovanarazgxwpihuhep`).

**Status:** Draft for CTO review.
**Phase:** A v0 product surface — *personalization layer*.
**Owner (cut):** patton-ai-ios CTO.
**Owner (signal-emission spec + profile-builder agent):** home-builder track.
**Last updated:** 2026-05-06.
**Cross-references:**
- `~/Projects/home-builder-agent/docs/specs/canonical-data-model.md`
- `~/Projects/home-builder-agent/docs/specs/migration_002_review.md`
- `~/Projects/home-builder-agent/docs/specs/migration_003_review.md`
- `~/Projects/patton-ai-ios/docs/03_build/turtle_contract_v1.md`
- Privacy policy: https://gist.github.com/Connorpatton615/8bc1fddd23e8dcda17e85ad6790dc494
  (already updated 2026-05-06 to cover personalization profile + in-app
  behavior signals — *land 004 only after the policy delta is published*)

---

## What this migration does

Lands the persistence layer that lets the ecosystem **learn Chad** over
time without crossing iOS sandbox boundaries we can't (and shouldn't)
cross.

Two goals:

1. **`home_builder.user_signal` table.** Append-only log of in-app
   events Chad triggers — screen views, Ask queries, notification
   dismissals, voice vs text choices, project switches, tool clicks.
   The iOS shell batch-POSTs these via `/v1/signals`. The
   profile-builder agent reads them.

2. **`home_builder.user_profile` table.** One JSONB row per user
   capturing the current preference profile derived from signals +
   engine_activity history. Read by every Claude-touching surface
   (`hb-ask`, `hb-router` classification, push notification templates)
   via system-prompt injection.

Out of scope for `004`:
- The actual signal-emission code in iOS (CTO instruments Phase A.1).
- The profile-builder agent itself (`hb-profile`, drafted separately
  in the home-builder track, lands as Phase A.2).
- Retention / archival jobs (deferred — see open question Q-C below).

---

## Design decisions

Numbered so we can reference them in review comments.

1. **Two tables, two distinct roles.**
   - `user_signal` (NEW): high-write, append-only, granular — *what
     Chad did inside the app, second-by-second*.
   - `user_profile` (NEW): low-write, mutable, aggregated — *what we
     learned about Chad from those signals*.

   Compare/contrast to migration 003's `engine_activity`: that table
   captures **Claude-autonomous actions** (audit trail). `user_signal`
   captures **Chad-explicit micro-interactions** (preference signal).
   Both can land for the same user-facing event — when Chad asks Ask
   a question, that's one `user_signal` row (`signal_type='ask_query'`)
   AND one `engine_activity` row (the autonomous Claude action that
   answered it). They observe different concerns and should not be
   merged.

2. **CASCADE on user FKs (different from engine_activity).** When
   Chad deletes his account, his signals + profile MUST be hard-deleted
   per the privacy-policy commitment ("permanently deleted when you
   delete your account"). This is the opposite of engine_activity's
   `ON DELETE RESTRICT`, which is for audit immutability against
   project deletion. Two different concerns, two different policies:

   | Table | actor_user_id | project_id | Why |
   |---|---|---|---|
   | engine_activity (003) | SET NULL | RESTRICT | Audit log; outlives users + projects |
   | user_signal (004) | CASCADE | SET NULL | Personal preference data; deleted with user |
   | user_profile (004) | CASCADE | n/a | Personal preference data; deleted with user |

3. **`signal_type` is open-enum TEXT, not CHECK.** Same rationale as
   `event.type` in 002 (decision 4): the signal vocabulary will grow
   as we instrument more iOS surfaces and add new tools. CHECK
   constraints would force a migration per new signal type. Application
   layer (Pydantic on the engine side, Codable on iOS) enforces the
   shape. Documented v1 vocabulary in §Signal vocabulary below.

4. **`payload JSONB`, not column-per-signal-shape.** Signal payloads
   vary widely (screen_view = `{screen: "ask"}`, ask_query =
   `{question_length: 47, voice: false, project_id: "..."}`,
   notification_dismissed = `{event_id: "...", dismissed_after_ms:
   1240}`). Polymorphic JSONB beats 30 nullable columns. Same pattern
   as `event.payload` in 002 and `engine_activity.parameters` in 003.

5. **One `user_profile` row per user (UNIQUE on actor_user_id) +
   UPSERT semantics.** No versioned history. Rationale:
   - Profile-builder agent rewrites the JSONB nightly. Versioning
     would 365× the row count per user per year for marginal value.
   - If we ever need rollback, Supabase backups cover it.
   - The profile is *current state*, not an event stream — different
     primitive than user_signal.
   - `last_built_at` + `last_built_signal_count` give debug visibility
     without needing history rows.

6. **Profile JSONB shape NOT enforced at the DB layer.** Pydantic on
   the engine side owns the shape (with a `version` field on the
   inside for forward-compat). iOS never reads `user_profile` directly
   — only Claude reads it, via system-prompt injection by the engine.
   This means schema iteration on the profile shape is a Pydantic-only
   change, no migration. Documented v1 shape in §Profile JSONB v1
   below.

7. **`session_id` for grouping signals from the same app session.**
   Lets the profile-builder reason about sequences ("in this 5-minute
   session Chad opened Whitfield → asked about budget → opened Cost
   Tracker URL → dismissed the procurement notification"). iOS shell
   generates a new UUID on each foreground entry; nullable for
   background-emitted signals.

8. **Indexes scoped to the profile-builder's read patterns.** Builder
   queries are always `actor_user_id` + recency or `actor_user_id` +
   `signal_type`. Two indexes cover both. No composite-with-project
   yet — revisit if the per-project profile feature lands.

9. **Service-role-only writes (mirrors engine_activity).** iOS POSTs
   to `/v1/signals` go through the backend which writes via service
   role. The profile-builder agent runs server-side with the same
   credentials. This keeps RLS policy simple (the actual access
   control is at the API route, not the table).

10. **No `tenant_id` indexes yet.** Phase A is single-tenant per 002
    decision 8; Phase B will add `tenant_id` indexes when multi-tenant
    routing matters.

---

## Table inventory

| # | Table | Project-scoped? | Write rate | Notes |
|---|---|---|---|---|
| 1 | `home_builder.user_signal` (NEW) | optional (nullable FK) | high (10s-100s/day/user) | Append-only event log of in-app behavior |
| 2 | `home_builder.user_profile` (NEW) | no — per-user | low (~1/day/user) | Current preference state JSONB |

---

## SQL

```sql
-- =============================================================================
-- migration_004_user_signal_and_profile.sql
-- =============================================================================
-- Adds the two-table personalization layer:
--   user_signal  — high-write append-only event log of in-app behavior
--   user_profile — low-write per-user preference state JSONB
--
-- See ~/Projects/home-builder-agent/docs/specs/migration_004_review.md
-- for design rationale, signal vocabulary, and the v1 profile JSONB shape.
--
-- Privacy: both tables CASCADE on user delete per the personalization
-- language in the published privacy policy (gist updated 2026-05-06).
-- =============================================================================

-- PREREQUISITES (provided by 002 + 003):
--   • pgcrypto extension
--   • home_builder schema
--   • home_builder.project, home_builder.event, home_builder.engine_activity
--   • is_master() function
--   • home_builder.user_can_access_project() helper
-- =============================================================================

-- 1. user_signal --------------------------------------------------------------
-- Append-only log of in-app behavior signals. iOS shell batch-POSTs to
-- /v1/signals; profile-builder reads to derive user_profile.
-- LOAD-BEARING: this is the *only* preference-signal store. New iOS
-- surfaces emit here; the profile-builder reads here. No drift surface.
CREATE TABLE IF NOT EXISTS home_builder.user_signal (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    actor_user_id               UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
                                -- NOT NULL: every signal must attribute to a user.
                                -- CASCADE (vs engine_activity's SET NULL): signals are
                                -- personal preference data, deleted with the user per
                                -- the privacy-policy commitment.

    signal_type                 TEXT NOT NULL,
                                -- Open-enum (decision 3). v1 vocabulary documented in
                                -- the review doc § Signal vocabulary. Examples:
                                --   'screen_view'          — Chad opened a screen
                                --   'ask_query'            — Chad asked an Ask question
                                --   'ask_followup'         — Chad re-asked within 60s
                                --   'tool_invoked'         — Chad tapped a tool result
                                --   'notification_acted'   — Chad tapped a push notif
                                --   'notification_dismissed' — Chad swiped notif away
                                --   'voice_input_used'     — Chad used voice
                                --   'project_switched'     — Chad picked a different project
                                --   'session_start'        — App entered foreground
                                --   'session_end'          — App backgrounded

    surface                     TEXT NOT NULL DEFAULT 'chat'
                                CHECK (surface IN ('chat', 'voice', 'cli', 'background')),
                                -- Mirrors engine_activity.surface for symmetry. Most rows
                                -- = 'chat' (iOS interaction). 'background' for signals
                                -- emitted while the app is suspended (rare).

    payload                     JSONB NOT NULL DEFAULT '{}'::jsonb,
                                -- Signal-specific structured data. Polymorphic per
                                -- decision 4. Pydantic owns the shape contract.
                                -- Examples in § Signal vocabulary.

    project_id                  UUID REFERENCES home_builder.project(id) ON DELETE SET NULL,
                                -- Optional. Some signals are project-scoped
                                -- (ask_query about Whitfield); others aren't (general
                                -- screen_view of the projects list). SET NULL on
                                -- project delete: the signal still has value to the
                                -- profile-builder even if the project is gone.

    session_id                  UUID,
                                -- Optional. Groups signals from one foreground app
                                -- session (decision 7). iOS generates per session_start;
                                -- omitted for background-emitted signals.

    client_timestamp            TIMESTAMPTZ,
                                -- When the iOS device recorded the event. Different
                                -- from created_at (server-received) for batched
                                -- POSTs and offline replay. Nullable so server-side
                                -- emitters don't have to spoof one.

    tenant_id                   UUID NULL,
                                -- Phase B forward-compat per 002 decision 8.

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. user_profile -------------------------------------------------------------
-- Current preference state per user. Updated by the profile-builder agent.
-- Read by every Claude-touching surface via system-prompt injection.
CREATE TABLE IF NOT EXISTS home_builder.user_profile (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    actor_user_id               UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
                                -- UNIQUE: one row per user (decision 5).
                                -- CASCADE: deleted with the user.

    profile                     JSONB NOT NULL DEFAULT '{}'::jsonb,
                                -- The actual preference document. Pydantic-typed on
                                -- the engine side per decision 6; v1 shape documented
                                -- in § Profile JSONB v1.

    version                     INTEGER NOT NULL DEFAULT 1,
                                -- Schema version of the profile JSONB. Lets the
                                -- profile-builder evolve the shape without DB
                                -- migrations. Engine code reads `version` first,
                                -- then dispatches to the matching Pydantic model.

    last_built_at               TIMESTAMPTZ,
                                -- When the profile-builder last rewrote this row.
                                -- Lets the dispatcher skip Claude-side injection
                                -- for users whose profile is stale (>24h old =
                                -- run with no profile or a default profile).

    last_built_signal_count     INTEGER,
                                -- How many user_signal rows informed this build.
                                -- Debug-only; helps explain "why is the profile
                                -- thin / rich".

    tenant_id                   UUID NULL,
                                -- Phase B forward-compat.

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================================
-- updated_at trigger for user_profile
-- =============================================================================

CREATE OR REPLACE FUNCTION home_builder.touch_user_profile_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS user_profile_updated_at ON home_builder.user_profile;
CREATE TRIGGER user_profile_updated_at
    BEFORE UPDATE ON home_builder.user_profile
    FOR EACH ROW
    EXECUTE FUNCTION home_builder.touch_user_profile_updated_at();

-- =============================================================================
-- Indexes
-- =============================================================================

-- user_signal: profile-builder's two read patterns (decision 8)
CREATE INDEX IF NOT EXISTS idx_hb_signal_actor_recent
    ON home_builder.user_signal(actor_user_id, created_at DESC);
    -- "all of Chad's recent signals" — primary builder query

CREATE INDEX IF NOT EXISTS idx_hb_signal_actor_type_recent
    ON home_builder.user_signal(actor_user_id, signal_type, created_at DESC);
    -- "all of Chad's notification dismissals" / "all his ask queries"

CREATE INDEX IF NOT EXISTS idx_hb_signal_session
    ON home_builder.user_signal(session_id)
    WHERE session_id IS NOT NULL;
    -- Group-by-session for sequence analysis. Partial index since
    -- background-emitted signals lack session_id.

-- user_profile: just find-by-user + find-stale
CREATE INDEX IF NOT EXISTS idx_hb_profile_stale
    ON home_builder.user_profile(last_built_at NULLS FIRST);
    -- Profile-builder cron query: "which profiles need rebuilding?"
    -- (NULLS FIRST so never-built profiles get prioritized.)

-- =============================================================================
-- RLS
-- =============================================================================

-- user_signal --------------------------------------------------------------

ALTER TABLE home_builder.user_signal ENABLE ROW LEVEL SECURITY;

-- SELECT: actor reads their own; master reads all. No project-scoped read —
-- signals are personal preference data, not project artifacts (decision 9).
DROP POLICY IF EXISTS hb_signal_select ON home_builder.user_signal;
CREATE POLICY hb_signal_select ON home_builder.user_signal
    FOR SELECT USING (
        is_master() OR actor_user_id = auth.uid()
    );

-- WRITE: service_role only. Backend writes; profile-builder reads.
-- iOS never writes directly; it goes through /v1/signals which holds
-- service_role.
DROP POLICY IF EXISTS hb_signal_write_service_only ON home_builder.user_signal;
CREATE POLICY hb_signal_write_service_only ON home_builder.user_signal
    FOR ALL USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

-- user_profile -------------------------------------------------------------

ALTER TABLE home_builder.user_profile ENABLE ROW LEVEL SECURITY;

-- SELECT: actor reads own (transparency — Chad can see his own profile);
-- master reads all.
DROP POLICY IF EXISTS hb_profile_select ON home_builder.user_profile;
CREATE POLICY hb_profile_select ON home_builder.user_profile
    FOR SELECT USING (
        is_master() OR actor_user_id = auth.uid()
    );

-- WRITE: service_role only. Profile-builder agent writes.
DROP POLICY IF EXISTS hb_profile_write_service_only ON home_builder.user_profile;
CREATE POLICY hb_profile_write_service_only ON home_builder.user_profile
    FOR ALL USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
```

---

## Signal vocabulary (v1)

Documented for review; lives as a Pydantic Enum + payload model on the
engine side. iOS Codable mirrors it. Open-enum at the DB layer means
new types land in code without migration.

| `signal_type` | Emitted when | Payload shape (v1) |
|---|---|---|
| `session_start` | App enters foreground | `{}` |
| `session_end` | App backgrounded | `{duration_ms: int}` |
| `screen_view` | New screen presented | `{screen: str, source: "tap" \| "deep_link" \| "back"}` |
| `project_switched` | Project picker selection | `{from_project_id?: uuid, to_project_id: uuid}` |
| `ask_query` | Chad submits a question | `{question_length: int, voice: bool, model_used?: str}` |
| `ask_followup` | Chad re-asks within 60s of an answer | `{prior_query_id?: uuid, time_since_prior_ms: int}` |
| `tool_invoked` | Chad tapped a tool result link | `{tool_name: str, target_url?: str}` |
| `notification_acted` | Chad tapped a push notification | `{event_id: uuid, latency_ms: int}` |
| `notification_dismissed` | Chad swiped a push notification away | `{event_id: uuid, dismissed_after_ms?: int}` |
| `voice_input_used` | Voice input fired (vs typed) | `{transcript_length: int, mode: "siri" \| "in_app"}` |
| `voice_input_canceled` | Voice input started, then canceled | `{}` |
| `share_received` | Chad shared content INTO the app | `{source_app?: str, content_type: str}` |

Future additions land via Pydantic + iOS Codable — no migration.

---

## Profile JSONB v1

Documented for review; lives as `HBUserProfileV1` in
`home_builder_agent/scheduling/schemas.py` (to be added with the
profile-builder agent in the home-builder track follow-up).

```json
{
  "version": 1,
  "vocabulary": {
    "preferred_terms": ["knock it out", "shoot me a copy"],
    "avoid": ["asynchronous", "pursuant to"]
  },
  "working_hours": {
    "weekday_start_hour": 6,
    "weekday_end_hour": 19,
    "weekend_active": false,
    "timezone": "America/Chicago"
  },
  "attention_weights": {
    "<project_uuid>": 0.8,
    "<project_uuid>": 0.2
  },
  "decision_patterns": {
    "common_vendors": {
      "windows": "Anderson",
      "concrete": "Coastal Concrete"
    },
    "common_amounts": {
      "permit_fee_typical": 850.00
    }
  },
  "ignored_alert_types": ["upcoming-procurement"],
  "answer_style": {
    "length_preference": "short",
    "format": "bullets-then-implication",
    "include_dollar_amounts": true,
    "include_dates": true
  },
  "voice_input_pct": 0.42,
  "session_count_30d": 47,
  "ask_query_count_30d": 19
}
```

The profile-builder agent (`hb-profile`, lands as a follow-up in the
home-builder track) reads `user_signal` + `engine_activity` + Drive
file-modification patterns + Gmail thread-classification history,
emits this JSONB nightly via `INSERT ... ON CONFLICT (actor_user_id)
DO UPDATE`.

---

## What this enables

Once 004 lands + the profile-builder ships:

1. **`hb-ask`** injects `user_profile.profile` into its system prompt.
   Claude answers Chad in his vocabulary, with his preferred density.
2. **`hb-router`** can use `attention_weights` to disambiguate
   "the project" when Chad doesn't say which one.
3. **Push notification dispatcher** consults `working_hours` before
   sending — no 11pm "windows are overdue" pings.
4. **Notification template engine** consults `ignored_alert_types`
   before queuing — types Chad always dismisses get suppressed.
5. **Future: proactive Claude** ("you usually order windows around this
   phase") reads `decision_patterns` to make grounded suggestions.

All of these are post-004 follow-ups owned by the home-builder track.
004 just lands the persistence.

---

## What lands next on the home-builder side (post-004 cut)

1. **`hb-profile` agent** — reads `user_signal` + `engine_activity` +
   existing surfaces, emits `user_profile.profile` JSONB. Runs nightly
   via launchd. Lands in `home_builder_agent/agents/profile_agent.py`.
2. **Pydantic models** — `HBUserSignalPayload` (per signal_type) +
   `HBUserProfileV1` in `home_builder_agent/scheduling/schemas.py`.
   Regenerates `view_models_schema.json` for iOS Codable.
3. **`HBUserProfile` injection** — `make_system_prompt(...)` helper in
   `home_builder_agent/core/profile_inject.py`. Every Claude-touching
   call site gets a 2-line refactor.
4. **Feedback loops in hb-ask** — when iOS POSTs an `ask_followup`
   signal within 60s of an `ask_query`, that's a "your prior answer
   missed" signal. The profile-builder downweights that answer style.

These all flow naturally from the 004 schema. None of them require
schema changes.

---

## Open questions for CTO review

**Q-A — Signal ingestion path:** single batch POST `/v1/signals`
(iOS sends array every 30s when foreground + on background entry),
or per-event POST? My take: **batch**, for battery + backend
write-amplification reasons. Per-event is fine for testing but
shouldn't ship.

**Q-B — `user_signal.project_id` nullable: keep?** Some signals are
project-scoped (`ask_query` about Whitfield, `project_switched` to a
project), some aren't (`screen_view` of project picker). Optional FK
seems right; want to confirm vs splitting into two tables (which I
think is overkill). My take: **keep nullable optional FK**.

**Q-C — Retention policy:** signals grow unbounded (10s-100s/day/user).
Three options:
1. Keep forever — text rows are cheap, postgres can handle 10M rows
   per user before we'd notice. Simplest.
2. 1-year hard retention via nightly DELETE.
3. 90-day raw + monthly rollup table.

My take: **option 1 (keep forever)** for v0; revisit at 100k rows/user.
Profile-builder pre-aggregates so raw rows only matter for debugging.

**Q-D — Profile JSONB schema enforcement:** Pydantic on the engine
side, no DB CHECK constraint. iOS never reads `user_profile` directly —
only Claude does. Engine writes through validated Pydantic. Agree?

**Q-E — Profile UPSERT or versioned history:** UPSERT (single row +
last_built_at) per decision 5. No history table. Agree?

**Q-F — Privacy / GDPR data export:** the published privacy policy
(§7) gives users right-to-portability. The actor-reads-own RLS policy
on both tables means an authenticated user can already SELECT their
rows via PostgREST. Is that the export path, or do we need an explicit
`/v1/me/data-export` route? My take: **explicit route in Phase B**;
v0 ships without it (the right exists, the convenience UI doesn't).

**Q-G — Interaction with `engine_activity`:** when Chad asks an Ask
question, that's ONE row in `user_signal` (the `ask_query` signal)
AND ONE row in `engine_activity` (the autonomous Claude action that
answered it). They observe different concerns and shouldn't be
merged — confirming you agree before we commit to this in code.

---

## Sequencing per the workflow we agreed

1. **You** review this doc, comment inline, answer Q-A through Q-G.
2. **You** decide whether the privacy-policy delta (already published
   2026-05-06 to the gist) needs a sub-processor amendment for
   personalization.
3. **I** revise this doc per your comments, you cut
   `~/Projects/patton-ai-ios/backend/migrations/004_user_signal_and_profile.sql`
   verbatim from the SQL block above (with revisions baked in), apply
   to Supabase staging.
4. **I** ship `HBUserSignalPayload` + `HBUserProfileV1` Pydantic
   models + regenerate JSON Schema for iOS Codable.
5. **I** ship the `hb-profile` agent stub that reads existing
   `engine_activity` + Gmail/Drive history (signals we already have)
   to emit a v0 profile, even before iOS instrumentation lands.
6. **You** stub `/v1/signals` batch ingestion route + the
   `/v1/me/profile` read route.
7. **You** instrument iOS shell to emit signals.
8. **I** wire profile injection into `hb-ask`, `hb-router`, and the
   notification dispatcher.

Steps 4–5 can run in parallel with steps 6–7.

---

## Appendix — entity ↔ canonical-model § cross-reference

| Entity | Canonical model § |
|---|---|
| `user_signal` | (NEW — to be added to canonical-data-model.md as entity 19) |
| `user_profile` | (NEW — to be added to canonical-data-model.md as entity 20) |

---

## Cut log

- **2026-05-06** — Draft authored by home-builder track. Privacy policy
  gist updated same day with personalization profile language. Awaiting
  CTO review on Q-A through Q-G before cut.
