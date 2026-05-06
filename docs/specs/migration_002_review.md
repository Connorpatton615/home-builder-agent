# Migration 002 — Review Doc

> Inline-SQL review surface for the `home_builder.*` schema and shell-side
> auth/dispatch tables. Both repos edit this same markdown freely until
> aligned; once green-lit, the SQL gets cut into
> `~/Projects/patton-ai-ios/backend/migrations/002_home_builder_schema.sql`
> and applied to the existing Supabase project (`neovanarazgxwpihuhep`).

**Status:** Draft for home-builder review.
**Phase:** A (per `turtle_contract_v1.md` § 3).
**Owner (cut):** patton-ai-ios CTO.
**Owner (column-type alignment):** home-builder track.
**Last updated:** 2026-05-04.
**Cross-references:**
- `~/Projects/home-builder-agent/docs/specs/canonical-data-model.md`
- `~/Projects/patton-ai-ios/docs/03_build/turtle_contract_v1.md`
- `~/Projects/patton-ai-ios/backend/migrations/001_initial_schema.sql` (precedent for type/RLS conventions)

---

## What this migration does

Lands the `home_builder.*` schema and two new `public.*` tables that connect
the iOS shell's auth chain to home-builder's per-project data.

Three goals, in priority order:

1. **Phase A persistence target.** Every entity in
   `canonical-data-model.md` gets a Postgres table so home-builder's engine
   can read/write canonical state instead of recomputing from Sheets.
2. **iOS-shell scoping bridge.** `public.user_turtle_projects` lets the
   shell's FastAPI (using a Supabase JWT) resolve a user → project set,
   and RLS scopes every home-builder read off that mapping.
3. **Forward compat for Phase B (multi-tenant).** Every table carries a
   `tenant_id UUID NULL` column now, NOT NULL'd via a later migration once
   customer #2 is live.

Out of scope for `002`: ScheduleView cache layer (compute on demand for
v0), populated reference data (phase templates, checklist library, lead-time
defaults — those land in `003`).

---

## Design decisions

Numbered so we can reference them in review comments.

1. **Schema namespace = `home_builder`.** All engine-owned tables live
   under it. `public.user_turtle_projects` and `public.device_tokens` are
   shell-owned and stay in `public`.
2. **UUID PKs everywhere, server-generated via `gen_random_uuid()`.** Same
   convention as `001`. Lets iOS clients generate UUIDs for offline-queued
   UserActions before sync.
3. **`TIMESTAMPTZ` for every `*_at` field.** UTC at rest. No naive
   timestamps anywhere.
4. **`TEXT + CHECK` for enums, not Postgres `ENUM` types.** Adding a value
   is `ALTER TABLE ... DROP CONSTRAINT ... ADD CONSTRAINT ...` which is
   safer than `ALTER TYPE ... ADD VALUE` (which can't be in a transaction
   in older Postgres versions). Canonical model is going to grow event
   types over time — TEXT + CHECK is more agile.
5. **`JSONB` for type-specific payloads.** `event.payload`,
   `user_action.payload`, `weather_impact.forecast_snapshot`,
   `vendor_item.variants` all `JSONB NOT NULL DEFAULT '{}'::jsonb`.
6. **Denormalize `project_id` onto child tables for RLS performance.**
   Canonical model specifies `phase_id` on Task/Checklist/ChecklistItem
   without `project_id`. RLS policies that traverse two joins to reach a
   `project_id` ACL will get expensive at scale. So we denormalize:
   `task`, `checklist`, `checklist_item`, `dependency`, `notification`,
   `user_action` all carry a `project_id` column that the engine maintains
   on insert/update. **Cross-project moves are effectively never** for
   these entities — the consistency cost is low, the perf win is large.
7. **Polymorphic FKs for `dependency` and `user_action`.** Both can point
   at multiple entity types (Phase or Task; Phase, Task, Delivery,
   Inspection, ChecklistItem, Vendor). Modeled as
   `(target_entity_type, target_entity_id)` with NO database-level FK,
   discriminated by a CHECK constraint. **See open question Q-A below** —
   this is the call I most want home-builder input on.
8. **`tenant_id UUID NULL` on every table** including
   `public.user_turtle_projects`. Phase A leaves all rows NULL. Phase B
   migration backfills + flips to NOT NULL.
9. **RLS scoped via `public.user_turtle_projects` join, not duplicated
   per-table-fragment.** A single `home_builder.user_can_access_project()`
   helper function is the policy template; every table's policy calls
   into it. Adding a future scoping rule (e.g., role-based read-only) is
   a single function change, not 16 policy rewrites.
10. **Vendor / VendorItem / LeadTime are NOT project-scoped.** Per
    canonical model, vendor data is shared across projects. RLS for
    these tables = "any authenticated user with at least one
    `home-builder` row in `user_turtle_projects` can read." Writes
    restricted to `service_role` (Vendor Intelligence System writes
    server-side via Supabase service key).
11. **`ON DELETE` policy = `RESTRICT` by default, `CASCADE` only where the
    child is genuinely owned.** Phases of a Project: CASCADE. Events
    referencing a Project: RESTRICT (Events outlive deleted projects for
    audit). Most projects soft-delete via `status = 'archived'` anyway.

---

## Table inventory

| # | Table | Project-scoped? | Notes |
|---|---|---|---|
| 1 | `home_builder.project` | self (PK is the project_id) | Status-based soft-delete. |
| 2 | `home_builder.phase` | yes (direct) | `sequence_index` for ordering. |
| 3 | `home_builder.task` | yes (denormalized) | Optional in V1. |
| 4 | `home_builder.milestone` | yes (direct) | Date-anchored event. |
| 5 | `home_builder.dependency` | yes (denormalized) | Polymorphic predecessor/successor. |
| 6 | `home_builder.checklist` | yes (denormalized) | 1:1 with Phase, gates phase completion. |
| 7 | `home_builder.checklist_item` | yes (denormalized) | Tickable items. |
| 8 | `home_builder.vendor` | no (global) | Cross-project shared. |
| 9 | `home_builder.vendor_item` | no (global) | Cross-project shared. |
| 10 | `home_builder.lead_time` | no (global) | Cross-project shared. |
| 11 | `home_builder.delivery` | yes (direct) | Material arrivals. |
| 12 | `home_builder.inspection` | yes (direct) | Permit / sign-off events. |
| 13 | `home_builder.notification` | yes (denormalized, nullable) | Engine-owned record; shell dispatches. |
| 14 | `home_builder.weather_impact` | yes (direct, nullable) | Forecast-driven. |
| 15 | `home_builder.user_action` | yes (denormalized, nullable) | Audit trail of inputs. |
| 16 | `home_builder.event` | yes (direct, nullable per type) | Canonical event store. |
| — | `public.user_turtle_projects` | bridge | Auth → project set. |
| — | `public.device_tokens` | shell-side | APNs token registry. |

ScheduleView (canonical entity 14) is intentionally **not** a table. It's
a derived projection computed at request time per the contract.

---

## SQL

```sql
-- =============================================================================
-- migration_002_home_builder_schema.sql
-- =============================================================================
-- Home-builder canonical schema + iOS shell auth bridge.
-- See ~/Projects/home-builder-agent/docs/specs/canonical-data-model.md
-- for entity definitions and ownership rules.
--
-- PREREQUISITES (provided by 001_initial_schema.sql, confirmed):
--   • pgcrypto extension
--   • public.is_master() function — defined at 001 lines 114-126,
--     SECURITY DEFINER with `SET search_path = public`. Visible to
--     home_builder.* helpers below because their search_path includes public.
--   • auth.users table (Supabase-provided)
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE SCHEMA IF NOT EXISTS home_builder;

-- -----------------------------------------------------------------------------
-- public.user_turtle_projects — auth → project bridge
-- -----------------------------------------------------------------------------
-- Maps Supabase auth user_id to the home-builder projects they can access.
-- Populated by Patton AI ops at customer-provisioning time. RLS-readable
-- by the user themselves; writes restricted to service_role.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_turtle_projects (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    turtle_id    TEXT NOT NULL,                       -- 'home-builder', future 'cfo', 'sales'
    project_id   UUID NOT NULL,                       -- FK enforced per-turtle, not here
    role         TEXT NOT NULL DEFAULT 'owner'
                 CHECK (role IN ('owner', 'admin', 'viewer')),
    tenant_id    UUID NULL,                           -- Phase B forward-compat
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, turtle_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_user_turtle_projects_user_turtle
    ON public.user_turtle_projects (user_id, turtle_id);
CREATE INDEX IF NOT EXISTS idx_user_turtle_projects_project
    ON public.user_turtle_projects (project_id);

-- -----------------------------------------------------------------------------
-- public.device_tokens — APNs registry (shell-side, parked until push lands)
-- -----------------------------------------------------------------------------
-- Created now so push provider integration is a wiring task, not a
-- schema-migration task. No data flows through this in Phase A.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.device_tokens (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    apns_token   TEXT NOT NULL,
    bundle_id    TEXT NOT NULL,
    environment  TEXT NOT NULL CHECK (environment IN ('sandbox', 'production')),
    app_version  TEXT,
    tenant_id    UUID NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, apns_token, environment)
);

CREATE INDEX IF NOT EXISTS idx_device_tokens_user
    ON public.device_tokens (user_id);
CREATE INDEX IF NOT EXISTS idx_device_tokens_apns_token
    ON public.device_tokens (apns_token);  -- Nit-7: invalid-token cleanup path

-- -----------------------------------------------------------------------------
-- home_builder.user_can_access_project — RLS helper
-- -----------------------------------------------------------------------------
-- Single function every home_builder.* policy calls into. Future scoping
-- rules (org-wide read, role-restricted write) become function changes,
-- not 16 separate policy rewrites.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION home_builder.user_can_access_project(p_project_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, home_builder
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.user_turtle_projects
        WHERE user_id = auth.uid()
          AND turtle_id = 'home-builder'
          AND project_id = p_project_id
    ) OR is_master();
$$;

REVOKE ALL ON FUNCTION home_builder.user_can_access_project(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION home_builder.user_can_access_project(UUID) TO authenticated;

-- -----------------------------------------------------------------------------
-- home_builder.user_has_any_home_builder_project — vendor-table RLS helper
-- -----------------------------------------------------------------------------
-- Vendor / VendorItem / LeadTime are not project-scoped. Anyone with at
-- least one home-builder project can read them. Writes are service_role.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION home_builder.user_has_any_home_builder_project()
RETURNS BOOLEAN
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, home_builder
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.user_turtle_projects
        WHERE user_id = auth.uid()
          AND turtle_id = 'home-builder'
    ) OR is_master();
$$;

REVOKE ALL ON FUNCTION home_builder.user_has_any_home_builder_project() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION home_builder.user_has_any_home_builder_project() TO authenticated;

-- =============================================================================
-- Entity tables
-- =============================================================================

-- 1. project ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS home_builder.project (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                        TEXT NOT NULL,
    customer_name               TEXT NOT NULL,
    address                     TEXT,
    target_completion_date      DATE,
    target_framing_start_date   DATE,
    status                      TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'on-hold', 'closed', 'archived')),
    contract_signed_at          TIMESTAMPTZ,
    drive_folder_id             TEXT,                  -- ex-identity, now metadata
    drive_folder_path           TEXT,                  -- ex-identity, now metadata
    tenant_id                   UUID NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        target_completion_date IS NOT NULL
        OR target_framing_start_date IS NOT NULL
    )
);

-- 2. phase --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS home_builder.phase (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id                      UUID NOT NULL
                                    REFERENCES home_builder.project(id) ON DELETE CASCADE,
    phase_template_id               TEXT NOT NULL,    -- references checklist library; populated in 003
    name                            TEXT NOT NULL,
    sequence_index                  INTEGER NOT NULL CHECK (sequence_index BETWEEN 1 AND 24),
    status                          TEXT NOT NULL DEFAULT 'not-started'
                                    CHECK (status IN ('not-started', 'in-progress', 'blocked-on-checklist', 'complete')),
    planned_start_date              DATE,
    planned_end_date                DATE,
    actual_start_date               DATE,
    actual_end_date                 DATE,
    default_duration_days           INTEGER,
    project_override_duration_days  INTEGER,
    tenant_id                       UUID NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, sequence_index)
);

-- 3. task ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS home_builder.task (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phase_id                    UUID NOT NULL
                                REFERENCES home_builder.phase(id) ON DELETE CASCADE,
    project_id                  UUID NOT NULL,        -- denormalized for RLS perf (see decision 6)
    name                        TEXT NOT NULL,
    planned_start_date          DATE,
    planned_end_date            DATE,
    planned_duration_hours      NUMERIC(8, 2),
    actual_completion_at        TIMESTAMPTZ,
    assigned_resource_ref       TEXT,                  -- V2 Resource entity stub
    status                      TEXT NOT NULL DEFAULT 'scheduled'
                                CHECK (status IN ('scheduled', 'in-progress', 'complete', 'blocked')),
    notes                       TEXT,
    float_time_days             INTEGER,               -- V2 critical path; nullable in V1
    tenant_id                   UUID NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id) REFERENCES home_builder.project(id) ON DELETE CASCADE
);

-- 4. milestone ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS home_builder.milestone (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL
                    REFERENCES home_builder.project(id) ON DELETE CASCADE,
    phase_id        UUID REFERENCES home_builder.phase(id) ON DELETE SET NULL,
    name            TEXT NOT NULL,
    planned_date    DATE,
    actual_date     DATE,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'hit', 'missed', 'rescheduled')),
    tenant_id       UUID NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. dependency ---------------------------------------------------------------
-- Typed FKs per Q-A round-1 review. Predecessor and successor each point
-- at exactly ONE of phase or task; CHECK constraints enforce the
-- exactly-one rule and prevent self-references. Real FK integrity, no
-- engine-side validation needed for referential checks.
CREATE TABLE IF NOT EXISTS home_builder.dependency (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id                  UUID NOT NULL                 -- denormalized for RLS
                                REFERENCES home_builder.project(id) ON DELETE CASCADE,
    predecessor_phase_id        UUID REFERENCES home_builder.phase(id) ON DELETE CASCADE,
    predecessor_task_id         UUID REFERENCES home_builder.task(id)  ON DELETE CASCADE,
    successor_phase_id          UUID REFERENCES home_builder.phase(id) ON DELETE CASCADE,
    successor_task_id           UUID REFERENCES home_builder.task(id)  ON DELETE CASCADE,
    dependency_kind             TEXT NOT NULL DEFAULT 'finish-to-start'
                                CHECK (dependency_kind IN (
                                    'finish-to-start', 'start-to-start',
                                    'finish-to-finish', 'start-to-finish'
                                )),
    offset_days                 INTEGER NOT NULL DEFAULT 0,
    tenant_id                   UUID NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Exactly one predecessor (phase OR task), exactly one successor (phase OR task)
    CHECK (
        (predecessor_phase_id IS NOT NULL)::int +
        (predecessor_task_id  IS NOT NULL)::int = 1
    ),
    CHECK (
        (successor_phase_id IS NOT NULL)::int +
        (successor_task_id  IS NOT NULL)::int = 1
    ),
    -- No self-references (phase pointing at itself, task pointing at itself)
    CHECK (
        NOT (predecessor_phase_id IS NOT NULL AND predecessor_phase_id = successor_phase_id)
        AND
        NOT (predecessor_task_id  IS NOT NULL AND predecessor_task_id  = successor_task_id)
    )
);

-- 6. checklist ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS home_builder.checklist (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phase_id            UUID NOT NULL UNIQUE          -- 1:1 with phase
                        REFERENCES home_builder.phase(id) ON DELETE CASCADE,
    project_id          UUID NOT NULL                 -- denormalized
                        REFERENCES home_builder.project(id) ON DELETE CASCADE,
    template_version    TEXT NOT NULL,                -- which version of the 24-checklist library
    status              TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'closed')),
    completed_count     INTEGER NOT NULL DEFAULT 0,
    total_count         INTEGER NOT NULL DEFAULT 0,
    tenant_id           UUID NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 7. checklist_item -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS home_builder.checklist_item (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    checklist_id    UUID NOT NULL
                    REFERENCES home_builder.checklist(id) ON DELETE CASCADE,
    project_id      UUID NOT NULL                     -- denormalized
                    REFERENCES home_builder.project(id) ON DELETE CASCADE,
    category        TEXT NOT NULL,                    -- 'client-and-contract', 'plans-and-engineering', etc.
    label           TEXT NOT NULL,
    is_complete     BOOLEAN NOT NULL DEFAULT FALSE,
    completed_by    UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    completed_at    TIMESTAMPTZ,
    notes           TEXT,
    sort_index      INTEGER,                          -- for stable ordering within category
    tenant_id       UUID NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((is_complete = FALSE) OR (completed_at IS NOT NULL))
);

-- 8. vendor (NOT project-scoped) ----------------------------------------------
CREATE TABLE IF NOT EXISTS home_builder.vendor (
    id                                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                                    TEXT NOT NULL,
    type                                    TEXT NOT NULL CHECK (type IN (
                                                'paint', 'lumber', 'plumbing', 'electrical',
                                                'tile', 'cabinet', 'appliance', 'hardware', 'other'
                                            )),
    address                                 TEXT,
    distance_from_default_jobsite_miles     NUMERIC(7, 2),
    preferred_vendor_weight                 NUMERIC(4, 3),                       -- 0.000–1.000
    tos_status                              TEXT NOT NULL DEFAULT 'compliant'
                                            CHECK (tos_status IN ('compliant', 'restricted', 'blocked')),
    last_scraped_at                         TIMESTAMPTZ,
    last_email_seen_at                      TIMESTAMPTZ,
    tenant_id                               UUID NULL,
    created_at                              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 9. vendor_item (NOT project-scoped) -----------------------------------------
CREATE TABLE IF NOT EXISTS home_builder.vendor_item (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_id               UUID NOT NULL
                            REFERENCES home_builder.vendor(id) ON DELETE CASCADE,
    vendor_sku              TEXT,                              -- vendor's own SKU code
    normalized_product_id   UUID,                              -- FK to normalized-product registry (added in 003)
    name                    TEXT NOT NULL,
    category                TEXT,
    variants                JSONB NOT NULL DEFAULT '{}'::jsonb, -- size/color/finish/dimensions
    unit                    TEXT,
    price                   NUMERIC(12, 2),
    in_stock                TEXT CHECK (in_stock IN ('yes', 'no', 'low', 'unknown')),
    lead_time_id            UUID,                              -- FK added below after lead_time exists
    vendor_url              TEXT,
    scraped_at              TIMESTAMPTZ,
    match_confidence        NUMERIC(4, 3),                     -- 0.000–1.000
    tenant_id               UUID NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 10. lead_time (NOT project-scoped) ------------------------------------------
CREATE TABLE IF NOT EXISTS home_builder.lead_time (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope           TEXT NOT NULL CHECK (scope IN ('sku', 'vendor', 'category')),
    scope_id        TEXT NOT NULL,                     -- TEXT because category-name; UUIDs for sku/vendor stored as text
    days            INTEGER NOT NULL,
    source          TEXT NOT NULL CHECK (source IN (
                        'vendor-sku-published', 'vendor-default',
                        'category-default', 'manual-override'
                    )),
    confidence      NUMERIC(4, 3),                     -- 0.000–1.000
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id       UUID NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Backfill the FK on vendor_item now that lead_time exists
ALTER TABLE home_builder.vendor_item
    ADD CONSTRAINT vendor_item_lead_time_fk
    FOREIGN KEY (lead_time_id) REFERENCES home_builder.lead_time(id) ON DELETE SET NULL;

-- 11. delivery ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS home_builder.delivery (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL
                    REFERENCES home_builder.project(id) ON DELETE CASCADE,
    phase_id        UUID
                    REFERENCES home_builder.phase(id) ON DELETE SET NULL,
    vendor_item_id  UUID
                    REFERENCES home_builder.vendor_item(id) ON DELETE SET NULL,
    scheduled_date  DATE,
    actual_date     DATE,
    status          TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled', 'en-route', 'delivered', 'missed')),
    tracking_ref    TEXT,
    source          TEXT NOT NULL DEFAULT 'manual-mark'
                    CHECK (source IN ('vendor-confirmation', 'email-parse', 'manual-mark')),
    tenant_id       UUID NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 12. inspection --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS home_builder.inspection (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id              UUID NOT NULL
                            REFERENCES home_builder.project(id) ON DELETE CASCADE,
    phase_id                UUID
                            REFERENCES home_builder.phase(id) ON DELETE SET NULL,
    inspection_type         TEXT NOT NULL,             -- 'foundation', 'rough-in', etc. — open enum
    inspector_authority     TEXT NOT NULL DEFAULT 'baldwin-county'
                            CHECK (inspector_authority IN ('baldwin-county', 'private', 'other')),
    scheduled_date          DATE,
    actual_date             DATE,
    status                  TEXT NOT NULL DEFAULT 'scheduled'
                            CHECK (status IN ('scheduled', 'passed', 'failed', 'reinspect-needed')),
    failure_notes           TEXT,
    reinspect_date          DATE,
    source                  TEXT NOT NULL DEFAULT 'manual-mark'
                            CHECK (source IN ('permit-portal', 'manual-mark')),
    tenant_id               UUID NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 13. notification ------------------------------------------------------------
-- Engine-owned record. Shell dispatcher writes delivered_at/viewed_at/push_id
-- as the authorized-dispatcher exception (canonical-data-model.md commit
-- a62a984 codifies this). See Q-B in round-1 review.
--
-- ORDER NOTE (cyclic-FK pattern, Nit-5): this table is created BEFORE event
-- because notification has no fields event references back, but event has
-- fields notification needs to FK to. The FK from notification.event_id →
-- event.id is added at the bottom of this migration via ALTER TABLE once
-- event exists. Cyclic in spirit, ordered in execution.
CREATE TABLE IF NOT EXISTS home_builder.notification (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id        UUID NOT NULL,                     -- FK added below after event exists
    project_id      UUID,                              -- denormalized; nullable since some events aren't project-scoped
    channel         TEXT NOT NULL CHECK (channel IN ('in-app', 'push', 'email', 'sms')),
    surface_target  TEXT NOT NULL CHECK (surface_target IN (
                        'daily-view', 'weekly-view', 'master-schedule',
                        'notification-feed', 'banner'
                    )),
    push_id         TEXT,                              -- APNs/FCM provider reference
    delivered_at    TIMESTAMPTZ,
    viewed_at       TIMESTAMPTZ,
    dismissed_at    TIMESTAMPTZ,
    click_action    JSONB,                             -- deep-link target {entity_type, entity_id}
    tenant_id       UUID NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 14. weather_impact ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS home_builder.weather_impact (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id              UUID NOT NULL
                            REFERENCES home_builder.project(id) ON DELETE CASCADE,
    phase_id                UUID REFERENCES home_builder.phase(id) ON DELETE SET NULL,
    task_id                 UUID REFERENCES home_builder.task(id) ON DELETE SET NULL,
    forecast_window_start   TIMESTAMPTZ NOT NULL,
    forecast_window_end     TIMESTAMPTZ NOT NULL,
    severity                TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    affected_activity       TEXT NOT NULL,             -- 'concrete-pour', 'framing', etc. — open enum
    forecast_snapshot       JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_provider         TEXT,                      -- 'noaa', 'tomorrow-io', 'openweather'
    observed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id               UUID NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (forecast_window_end >= forecast_window_start)
);

-- 15. user_action -------------------------------------------------------------
-- Polymorphic target — see open question Q-A.
CREATE TABLE IF NOT EXISTS home_builder.user_action (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id           UUID NOT NULL REFERENCES auth.users(id) ON DELETE SET NULL,
    project_id              UUID,                      -- denormalized; nullable for vendor-pin actions
    surface                 TEXT NOT NULL CHECK (surface IN ('desktop', 'mobile', 'tracker-edit')),
    action_type             TEXT NOT NULL,             -- open enum; new types added without schema change
    target_entity_type      TEXT NOT NULL,
    target_entity_id        UUID NOT NULL,
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at             TIMESTAMPTZ NOT NULL,      -- when actor performed it (offline-safe)
    synced_at               TIMESTAMPTZ NOT NULL DEFAULT now(),  -- when engine received it
    conflict_resolution     TEXT,                      -- 'server-wins', 'client-wins', 'last-write-wins', NULL if none
    idempotency_key         UUID NOT NULL DEFAULT gen_random_uuid(),  -- Nit-3: client supplies for retry safety; default kills multi-NULL UNIQUE trap
    tenant_id               UUID NULL,
    UNIQUE (actor_user_id, idempotency_key)            -- per-actor dedup; idempotency_key is now NOT NULL so multi-NULL trap is gone
);

-- 16. event -------------------------------------------------------------------
-- Engine-owned canonical store. Many emitters, one owner per Rule 2 of the
-- canonical model.
CREATE TABLE IF NOT EXISTS home_builder.event (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type                    TEXT NOT NULL,             -- open enum; canonical model adds types over time
    severity                TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical', 'blocking')),
    status                  TEXT NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open', 'acknowledged', 'resolved')),
    project_id              UUID REFERENCES home_builder.project(id) ON DELETE RESTRICT,
    phase_id                UUID REFERENCES home_builder.phase(id) ON DELETE SET NULL,
    task_id                 UUID REFERENCES home_builder.task(id) ON DELETE SET NULL,
    vendor_id               UUID REFERENCES home_builder.vendor(id) ON DELETE SET NULL,
    sku_id                  UUID REFERENCES home_builder.vendor_item(id) ON DELETE SET NULL,
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    source                  TEXT NOT NULL CHECK (source IN (
                                'scheduling-engine', 'vendor-intelligence',
                                'supplier-email-watcher', 'weather-monitor', 'permit-portal'
                            )),
    acknowledgement_actor   UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at         TIMESTAMPTZ,
    resolved_at             TIMESTAMPTZ,
    tenant_id               UUID NULL
);

-- Backfill the FK on notification now that event exists
ALTER TABLE home_builder.notification
    ADD CONSTRAINT notification_event_fk
    FOREIGN KEY (event_id) REFERENCES home_builder.event(id) ON DELETE CASCADE;

-- =============================================================================
-- Indexes
-- =============================================================================

-- project
CREATE INDEX IF NOT EXISTS idx_hb_project_status      ON home_builder.project(status);
CREATE INDEX IF NOT EXISTS idx_hb_project_tenant      ON home_builder.project(tenant_id);
CREATE INDEX IF NOT EXISTS idx_hb_project_name        ON home_builder.project(name);  -- Nit-2: hb-schedule "Pelican Point" lookups

-- phase
CREATE INDEX IF NOT EXISTS idx_hb_phase_project_seq   ON home_builder.phase(project_id, sequence_index);
CREATE INDEX IF NOT EXISTS idx_hb_phase_status_open   ON home_builder.phase(status)
    WHERE status IN ('in-progress', 'blocked-on-checklist');

-- task
CREATE INDEX IF NOT EXISTS idx_hb_task_project        ON home_builder.task(project_id);
CREATE INDEX IF NOT EXISTS idx_hb_task_phase          ON home_builder.task(phase_id);
CREATE INDEX IF NOT EXISTS idx_hb_task_planned_start  ON home_builder.task(planned_start_date);

-- milestone
CREATE INDEX IF NOT EXISTS idx_hb_milestone_project   ON home_builder.milestone(project_id, planned_date);

-- dependency (typed FK indexes per Q-A; partial WHERE so we don't index NULL halves)
CREATE INDEX IF NOT EXISTS idx_hb_dep_project              ON home_builder.dependency(project_id);
CREATE INDEX IF NOT EXISTS idx_hb_dep_predecessor_phase    ON home_builder.dependency(predecessor_phase_id) WHERE predecessor_phase_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_hb_dep_predecessor_task     ON home_builder.dependency(predecessor_task_id)  WHERE predecessor_task_id  IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_hb_dep_successor_phase      ON home_builder.dependency(successor_phase_id)   WHERE successor_phase_id   IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_hb_dep_successor_task       ON home_builder.dependency(successor_task_id)    WHERE successor_task_id    IS NOT NULL;

-- checklist + items
CREATE INDEX IF NOT EXISTS idx_hb_checklist_project   ON home_builder.checklist(project_id);
CREATE INDEX IF NOT EXISTS idx_hb_ci_checklist_cat    ON home_builder.checklist_item(checklist_id, category, sort_index);
CREATE INDEX IF NOT EXISTS idx_hb_ci_open_per_proj    ON home_builder.checklist_item(project_id)
    WHERE is_complete = FALSE;

-- vendor + item + lead_time
CREATE INDEX IF NOT EXISTS idx_hb_vendor_type         ON home_builder.vendor(type);
CREATE INDEX IF NOT EXISTS idx_hb_vendor_item_vendor  ON home_builder.vendor_item(vendor_id);
CREATE INDEX IF NOT EXISTS idx_hb_vendor_item_normalized ON home_builder.vendor_item(normalized_product_id);
CREATE INDEX IF NOT EXISTS idx_hb_lead_time_scope     ON home_builder.lead_time(scope, scope_id, observed_at DESC);

-- delivery
CREATE INDEX IF NOT EXISTS idx_hb_delivery_proj_date  ON home_builder.delivery(project_id, scheduled_date);
CREATE INDEX IF NOT EXISTS idx_hb_delivery_status_late ON home_builder.delivery(scheduled_date)
    WHERE status IN ('scheduled', 'en-route');

-- inspection
CREATE INDEX IF NOT EXISTS idx_hb_inspection_proj     ON home_builder.inspection(project_id, scheduled_date);
CREATE INDEX IF NOT EXISTS idx_hb_inspection_open     ON home_builder.inspection(project_id, scheduled_date)
    WHERE status IN ('scheduled', 'reinspect-needed');  -- Nit-6: morning-brief permit-expiry scan

-- notification
CREATE INDEX IF NOT EXISTS idx_hb_notification_event  ON home_builder.notification(event_id);
CREATE INDEX IF NOT EXISTS idx_hb_notification_proj_undelivered
    ON home_builder.notification(project_id, created_at DESC)
    WHERE delivered_at IS NULL;

-- weather_impact
CREATE INDEX IF NOT EXISTS idx_hb_weather_proj_window ON home_builder.weather_impact(project_id, forecast_window_start);

-- user_action
CREATE INDEX IF NOT EXISTS idx_hb_user_action_actor   ON home_builder.user_action(actor_user_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_hb_user_action_proj    ON home_builder.user_action(project_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_hb_user_action_target  ON home_builder.user_action(target_entity_type, target_entity_id);
CREATE INDEX IF NOT EXISTS idx_hb_user_action_synced  ON home_builder.user_action(synced_at DESC);  -- Q-G(b): engine reconcile-pass scan

-- event
CREATE INDEX IF NOT EXISTS idx_hb_event_proj_status   ON home_builder.event(project_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hb_event_open_severity ON home_builder.event(severity, created_at DESC)
    WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_hb_event_type          ON home_builder.event(type, created_at DESC);

-- =============================================================================
-- RLS — every table on, policies via the helper functions
-- =============================================================================

-- public bridge tables -------------------------------------------------------
ALTER TABLE public.user_turtle_projects  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.device_tokens         ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS utp_select_own ON public.user_turtle_projects;
CREATE POLICY utp_select_own ON public.user_turtle_projects
    FOR SELECT USING (is_master() OR user_id = auth.uid());

DROP POLICY IF EXISTS utp_write_service_only ON public.user_turtle_projects;
CREATE POLICY utp_write_service_only ON public.user_turtle_projects
    FOR ALL USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

DROP POLICY IF EXISTS device_tokens_select_own ON public.device_tokens;
CREATE POLICY device_tokens_select_own ON public.device_tokens
    FOR SELECT USING (is_master() OR user_id = auth.uid());

DROP POLICY IF EXISTS device_tokens_write_own ON public.device_tokens;
CREATE POLICY device_tokens_write_own ON public.device_tokens
    FOR ALL USING (user_id = auth.uid() OR auth.role() = 'service_role')
    WITH CHECK (user_id = auth.uid() OR auth.role() = 'service_role');

-- home_builder.* — project-scoped tables -------------------------------------
-- Single template repeated. Engine writes via service_role; reads scoped via
-- user_can_access_project(). Notification has the dispatcher exception:
-- service_role can write delivered_at/viewed_at/push_id even when running
-- shell-side as the dispatcher process.

ALTER TABLE home_builder.project          ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.phase            ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.task             ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.milestone        ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.dependency       ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.checklist        ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.checklist_item   ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.delivery         ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.inspection       ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.notification     ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.weather_impact   ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.user_action      ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.event            ENABLE ROW LEVEL SECURITY;

-- home_builder.project — explicit policy (uses id, not project_id) per Nit-1.
-- Pulled out of the loop so the hot-path policy expression is a clean
-- column reference instead of a CASE that the planner would have to
-- reason through on every query.
DROP POLICY IF EXISTS hb_project_select ON home_builder.project;
CREATE POLICY hb_project_select ON home_builder.project
    FOR SELECT USING (home_builder.user_can_access_project(id));

DROP POLICY IF EXISTS hb_project_write_service_only ON home_builder.project;
CREATE POLICY hb_project_write_service_only ON home_builder.project
    FOR ALL USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

-- All other project-scoped tables use a uniform template (project_id direct).
-- No CASE in policy expression — each table reads project_id by name.
DO $$
DECLARE t TEXT;
BEGIN
    FOR t IN SELECT unnest(ARRAY[
        'phase', 'task', 'milestone', 'dependency',
        'checklist', 'checklist_item', 'delivery', 'inspection',
        'weather_impact'
    ]) LOOP
        EXECUTE format($f$
            DROP POLICY IF EXISTS hb_%1$s_select ON home_builder.%1$s;
            CREATE POLICY hb_%1$s_select ON home_builder.%1$s
                FOR SELECT USING (home_builder.user_can_access_project(project_id));
            DROP POLICY IF EXISTS hb_%1$s_write_service_only ON home_builder.%1$s;
            CREATE POLICY hb_%1$s_write_service_only ON home_builder.%1$s
                FOR ALL USING (auth.role() = 'service_role')
                WITH CHECK (auth.role() = 'service_role');
        $f$, t);
    END LOOP;
END $$;

-- event — same shape but project_id is nullable; allow read of project-null events
-- (e.g., vendor-intelligence emissions) to any user with at least one home-builder project
DROP POLICY IF EXISTS hb_event_select ON home_builder.event;
CREATE POLICY hb_event_select ON home_builder.event
    FOR SELECT USING (
        (project_id IS NOT NULL AND home_builder.user_can_access_project(project_id))
        OR (project_id IS NULL AND home_builder.user_has_any_home_builder_project())
    );

DROP POLICY IF EXISTS hb_event_write_service_only ON home_builder.event;
CREATE POLICY hb_event_write_service_only ON home_builder.event
    FOR ALL USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

-- notification — same as event; project_id is denormalized but nullable
DROP POLICY IF EXISTS hb_notification_select ON home_builder.notification;
CREATE POLICY hb_notification_select ON home_builder.notification
    FOR SELECT USING (
        (project_id IS NOT NULL AND home_builder.user_can_access_project(project_id))
        OR (project_id IS NULL AND home_builder.user_has_any_home_builder_project())
    );

DROP POLICY IF EXISTS hb_notification_write_service_only ON home_builder.notification;
CREATE POLICY hb_notification_write_service_only ON home_builder.notification
    FOR ALL USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

-- user_action — actor can read their own; service_role writes
DROP POLICY IF EXISTS hb_user_action_select ON home_builder.user_action;
CREATE POLICY hb_user_action_select ON home_builder.user_action
    FOR SELECT USING (
        is_master()
        OR actor_user_id = auth.uid()
        OR (project_id IS NOT NULL AND home_builder.user_can_access_project(project_id))
    );

DROP POLICY IF EXISTS hb_user_action_write_service_only ON home_builder.user_action;
CREATE POLICY hb_user_action_write_service_only ON home_builder.user_action
    FOR ALL USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

-- vendor / vendor_item / lead_time — global read, service_role write
ALTER TABLE home_builder.vendor       ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.vendor_item  ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.lead_time    ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE t TEXT;
BEGIN
    FOR t IN SELECT unnest(ARRAY['vendor', 'vendor_item', 'lead_time']) LOOP
        EXECUTE format($f$
            DROP POLICY IF EXISTS hb_%1$s_select ON home_builder.%1$s;
            CREATE POLICY hb_%1$s_select ON home_builder.%1$s
                FOR SELECT USING (home_builder.user_has_any_home_builder_project());
            DROP POLICY IF EXISTS hb_%1$s_write_service_only ON home_builder.%1$s;
            CREATE POLICY hb_%1$s_write_service_only ON home_builder.%1$s
                FOR ALL USING (auth.role() = 'service_role')
                WITH CHECK (auth.role() = 'service_role');
        $f$, t);
    END LOOP;
END $$;

-- =============================================================================
-- SECURITY DEFINER search_path audit (matches 001's gate)
-- =============================================================================
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT n.nspname || '.' || p.proname AS fn
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE p.prosecdef = TRUE
          AND n.nspname IN ('public', 'home_builder')
          AND NOT EXISTS (
              SELECT 1 FROM unnest(p.proconfig) c
              WHERE c LIKE 'search_path=%'
          )
    LOOP
        RAISE EXCEPTION 'SECURITY DEFINER without search_path: %', r.fn;
    END LOOP;
END $$;
```

---

## Open questions for home-builder review

Lettered for easy reference in PR-style comments.

### Q-A — Polymorphic FKs on `dependency` and `user_action`

I went with `(target_entity_type, target_entity_id)` and **no database-level
FK constraint**. The discriminator is enforced by a `CHECK`. Pros: one
column pair, schema-clean. Cons: no FK integrity — you can insert a UUID
that doesn't exist. The engine has to validate at write time.

**Alternative:** split into typed FK columns
(`predecessor_phase_id`, `predecessor_task_id`) with a `CHECK` that
exactly one is non-null. Pros: real FK integrity. Cons: schema gets ugly
fast for `user_action` which can target six entity types — six FK
columns plus the CHECK.

**My lean:** keep polymorphic for `user_action` (six target types is too
many for typed FKs), switch `dependency` to typed FKs since it's only two
target types. Open to your call.

### Q-B — Notification dispatcher writes vs Rule 1

Per your incoming canonical-model clarification, the dispatcher process
gets to write `delivered_at`, `viewed_at`, `push_id` as an authorized
exception. In SQL terms, the dispatcher runs as `service_role` (the iOS
shell backend connects to Supabase as service_role for engine ops). The
RLS policy as written allows **any** service_role write to notification —
not specifically the dispatcher.

**Question:** is policy-level enforcement of the dispatcher boundary out
of scope for `002`? My read is yes — the boundary is enforced at the
process layer (only the dispatcher process holds these write paths), and
RLS just gates user-vs-service. If you want a tighter SQL constraint
(e.g., a separate `dispatcher_role` distinct from generic `service_role`),
flag it.

### Q-C — `lead_time.scope_id` as TEXT vs polymorphic UUID

Canonical model says `scope_id` is "FK varies by scope: VendorItem.id,
Vendor.id, or category-name". I made it TEXT to absorb the
category-name case (which isn't a UUID). Vendor/SKU UUIDs get stringified
on insert.

**Alternative:** three nullable columns (`vendor_item_id`, `vendor_id`,
`category_name`) with a CHECK that exactly one matches the `scope`
discriminator. More verbose, gives you real FK integrity for the UUID
cases.

### Q-D — Reference-data tables in `002` or `003`?

Currently the migration creates empty tables. Reference data the engine
needs:
- 24 phase templates
- Phase duration defaults (your table in `scheduling-engine.md`)
- Lead-time category defaults (your table in `vendor-intelligence-system.md`)
- Precon's 44-item × 10-category checklist

These could go in `002` as `INSERT` statements at the bottom, or in a
separate `003_reference_data.sql` migration. **My lean:** `003`. Lets
you iterate on the values without re-running schema DDL. Confirm.

### Q-E — `phase_template_id` type

I made it `TEXT` (slug-like, e.g., `'precon'`, `'foundation'`). Could be
a UUID FK to a `phase_template` table that lands in `003`. TEXT is more
human-readable in raw SQL inspection; UUID is more consistent with
everything else.

### Q-F — `checklist_item.category` as open TEXT vs CHECK enum

Precon has 10 categories; the other 23 phases have smaller sets. I left
`category` as open TEXT. Could lock it to a CHECK enum once the full
24-checklist library is defined. **Lean:** keep open until the library
ships in `003`, then add the CHECK in `004`.

### Q-G — Indexes I haven't added but you might want

- `home_builder.event` partial index for a notification-feed query that
  filters on `severity` and `status`. I added one for `(severity,
  created_at DESC) WHERE status = 'open'` — confirm that matches the
  feed query you envision.
- `home_builder.user_action` for the engine's reconcile pass: do you
  scan by `synced_at` or `recorded_at`? I indexed `recorded_at`; let me
  know if `synced_at` is the access pattern.

### Q-H — Connection model for engine writes

The engine runs on Mac Mini in Phase A. It needs to connect to Supabase
as `service_role` (to bypass RLS for engine ops). That means
`SUPABASE_SERVICE_ROLE_KEY` lives on Mac Mini in Phase A. I'm fine with
that (it's already where Drive/Gmail OAuth lives), but flagging it
explicitly so we both agree on the credential boundary.

---

## What lands next

Per the sequencing we agreed:

1. **You** review this doc, comment inline, answer the open questions.
2. **You** ship (a) Pydantic + JSON Schema in parallel.
3. **I** revise this doc per your comments, cut
   `~/Projects/patton-ai-ios/backend/migrations/002_home_builder_schema.sql`
   verbatim from the SQL block above (with revisions baked in), apply to
   Supabase staging.
4. **I** stub `/v1/turtles/home-builder/views/{view_type}` and `/actions`
   route handlers against the shapes from your `view_models_schema.json`.
5. **You** start (c) engine refactor against the live schema.

Push provider decision still parked.

---

## Appendix — entity ↔ canonical-model § cross-reference

Quick lookup if you're reviewing against the spec.

| Entity | Canonical model § |
|---|---|
| `project` | § entity 1 |
| `phase` | § entity 2 |
| `task` | § entity 3 |
| `milestone` | § entity 4 |
| `dependency` | § entity 5 |
| `checklist` | § entity 6 |
| `checklist_item` | § entity 7 |
| `vendor` | § entity 8 |
| `vendor_item` | § entity 9 |
| `lead_time` | § entity 10 |
| `delivery` | § entity 11 |
| `inspection` | § entity 12 |
| `notification` | § entity 13 |
| `weather_impact` | § entity 15 |
| `user_action` | § entity 16 |
| `event` | § entity 17 |
| `user_turtle_projects` | (new — bridge to canonical model) |
| `device_tokens` | (new — shell-side, not in canonical model) |

ScheduleView (canonical § entity 14) is intentionally not a table — see
"What this migration does" § out-of-scope.

---

## Home-Builder Review (round 1)

**Top-line:** Strong draft. Honors canonical-data-model throughout.
Sensible defaults on column types, indexes, RLS. No fundamental rewrites
needed — only polish + a few minor adds. **Greenlit to cut after the
revisions in this section land.**

### Answers to open questions

#### Q-A — Polymorphic FKs

**Confirm your lean. Typed FKs on `dependency`, polymorphic on `user_action`.**

`dependency` is only two target types (Phase or Task); 4 typed columns
with exactly-one-non-null CHECK is the same column count as the polymorphic
version (`predecessor_type`, `predecessor_id`, `successor_type`, `successor_id`)
but gives real FK integrity. Worth it.

`user_action` is six target types (phase, task, delivery, inspection,
checklist-item, vendor) — typed FKs would be 12 columns + a 6-way CHECK,
which is uglier than just enforcing the discriminator at the engine
write path. Keep polymorphic.

**Proposed `dependency` shape:**

```sql
CREATE TABLE IF NOT EXISTS home_builder.dependency (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id                  UUID NOT NULL
                                REFERENCES home_builder.project(id) ON DELETE CASCADE,
    predecessor_phase_id        UUID REFERENCES home_builder.phase(id) ON DELETE CASCADE,
    predecessor_task_id         UUID REFERENCES home_builder.task(id)  ON DELETE CASCADE,
    successor_phase_id          UUID REFERENCES home_builder.phase(id) ON DELETE CASCADE,
    successor_task_id           UUID REFERENCES home_builder.task(id)  ON DELETE CASCADE,
    dependency_kind             TEXT NOT NULL DEFAULT 'finish-to-start'
                                CHECK (dependency_kind IN (
                                    'finish-to-start', 'start-to-start',
                                    'finish-to-finish', 'start-to-finish'
                                )),
    offset_days                 INTEGER NOT NULL DEFAULT 0,
    tenant_id                   UUID NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Exactly one predecessor, exactly one successor, never self-referential
    CHECK (
        (predecessor_phase_id IS NOT NULL)::int +
        (predecessor_task_id  IS NOT NULL)::int = 1
    ),
    CHECK (
        (successor_phase_id IS NOT NULL)::int +
        (successor_task_id  IS NOT NULL)::int = 1
    ),
    CHECK (
        NOT (predecessor_phase_id IS NOT NULL AND predecessor_phase_id = successor_phase_id)
        AND
        NOT (predecessor_task_id  IS NOT NULL AND predecessor_task_id  = successor_task_id)
    )
);
```

Indexes adjust accordingly (drop `idx_hb_dep_predecessor` and
`idx_hb_dep_successor`; add `idx_hb_dep_predecessor_phase`,
`_predecessor_task`, `_successor_phase`, `_successor_task`).

#### Q-B — Dispatcher boundary in SQL vs process layer

**Confirm: process-level for 002.** No `dispatcher_role`. The dispatcher
process holds these write paths; that's enough boundary for v0. If we
ever find generic `service_role` writes leaking into `notification` from
non-dispatcher code, we revisit. The canonical-model clarification I
landed in `a62a984` already documents the rule semantically.

#### Q-C — `lead_time.scope_id` as TEXT

**Confirm TEXT.** Engine code path validates UUID parse + row-existence
on write when `scope ∈ {sku, vendor}`. Three-nullable-column alternative
adds SQL surface for marginal integrity benefit. Keep simple.

One nit: rename to `scope_ref` so future readers don't conflate it with
the row's own UUID id. `scope_id` reads like "id of the scope record."
Open to keeping `scope_id` if you prefer consistency with column-naming
elsewhere.

#### Q-D — Reference data in 002 vs 003

**Confirm 003.** 002 stays structural; 003 carries the seeds:
- 24 phase templates (`phase_template` table — see Q-E)
- Phase duration defaults
- Lead-time category defaults (already in `home_builder_agent/config.py`,
  but landing them in DB lets the engine resolve from one source on
  query rather than dual-source)
- Precon's 44-item × 10-category checklist content
- 23 non-precon checklist drafts (these are agent-generated; Chad redlines)

Keeping them out of 002 lets us iterate on values without DDL replay.

#### Q-E — `phase_template_id` as TEXT slug

**Confirm TEXT slug** (`'precon'`, `'foundation'`, etc.). Reasons:
- Human-readable in raw SQL inspection
- Stable identifier — slug doesn't change if we recreate the templates table
- Matches the slug pattern we already use (`turtle_id = 'home-builder'`)

In 003, the `phase_template` reference table will key on the same slug
as PK. So the FK we defer in 002 becomes:

```sql
ALTER TABLE home_builder.phase
    ADD CONSTRAINT phase_template_fk
    FOREIGN KEY (phase_template_id) REFERENCES home_builder.phase_template(slug);
```

#### Q-F — `checklist_item.category` as open TEXT

**Confirm open TEXT until library ships in 003**, then add CHECK in 004
once the full set is known. Same reasoning as Q-D — categories will
evolve as we get the full 24-checklist library populated.

Suggest using lowercase-hyphenated slugs (`client-and-contract`,
`plans-and-engineering`, etc.) consistent with our hyphenated-status
convention.

#### Q-G — Indexes

**(a) Event partial index** — confirm `(severity, created_at DESC) WHERE
status = 'open'` matches the notification-feed query. Engine ranks
`blocking → critical → warning → info` then by recency. The index covers it.

**(b) User-action — need BOTH `recorded_at` AND `synced_at`.** The
reconcile pass scans `synced_at DESC` to find UserActions the engine
hasn't fully projected yet (e.g., when the engine restarts and needs to
catch up). The actor-history view scans `recorded_at DESC`. They're
distinct access patterns. Add:

```sql
CREATE INDEX IF NOT EXISTS idx_hb_user_action_synced
    ON home_builder.user_action(synced_at DESC);
```

#### Q-H — `SUPABASE_SERVICE_ROLE_KEY` on Mac Mini in Phase A

**Confirm.** Mac Mini already holds Drive/Gmail OAuth tokens. Adding the
Supabase service-role key is consistent with the existing credential
boundary. We're not making it worse. Document this in the morning-brief
plist or a `.env.example` note so the next maintainer sees the expected
secret set.

In Phase B, when the engine moves off Mac Mini, the service-role key
moves with it (Modal secret / Railway env var). Mac Mini stops having it.

### Structural feedback (numbered nits)

#### Nit-1 — RLS template CASE expression

The DO-block template uses `CASE '%1$s' WHEN 'project' THEN id ELSE
project_id END` to handle the project table reading its own `id`. Functionally
correct, but the case expression evaluates per-row at policy time. Postgres
*should* short-circuit on the constant discriminator, but this is in the
hot read path for every project-scoped query.

**Suggest:** define `project` table policy explicitly (uses `id`), apply
the `project_id`-using template to the other ten tables. Cleaner SQL,
no case expression in the policy:

```sql
-- Project: USING (home_builder.user_can_access_project(id))
DROP POLICY IF EXISTS hb_project_select ON home_builder.project;
CREATE POLICY hb_project_select ON home_builder.project
    FOR SELECT USING (home_builder.user_can_access_project(id));
DROP POLICY IF EXISTS hb_project_write_service_only ON home_builder.project;
CREATE POLICY hb_project_write_service_only ON home_builder.project
    FOR ALL USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

-- Then the DO-block template only needs to handle the rest:
DO $$
DECLARE t TEXT;
BEGIN
    FOR t IN SELECT unnest(ARRAY[
        'phase', 'task', 'milestone', 'dependency',
        'checklist', 'checklist_item', 'delivery', 'inspection',
        'weather_impact'
    ]) LOOP
        -- ... policy uses project_id directly, no CASE
    END LOOP;
END $$;
```

#### Nit-2 — Add `idx_hb_project_name`

V1 development will sometimes look up projects by name (e.g.,
`hb-schedule "Pelican Point"`). Currently that's a sequential scan.

```sql
CREATE INDEX IF NOT EXISTS idx_hb_project_name ON home_builder.project(name);
```

Minor. Not blocking.

#### Nit-3 — `idempotency_key` allowing multiple NULLs

The `UNIQUE (actor_user_id, idempotency_key)` constraint allows multiple
rows with `idempotency_key = NULL` (Postgres default for UNIQUE on
NULL columns). Engine should set it to non-NULL on every insert to
avoid relying on that behavior. Suggest:

- Make `idempotency_key UUID NOT NULL DEFAULT gen_random_uuid()` so
  server-side inserts get a fresh UUID; iOS-side inserts override
  with the client-generated one.

Tiny change, removes an entire class of "did the dedup actually work?" bugs.

#### Nit-4 — Confirm `is_master()` is defined in 001

The two helper functions reference `is_master()` from somewhere outside
this migration. Just confirm it's defined in `001_initial_schema.sql` and
visible to the `home_builder` schema. If not, we need to add it (likely
as `public.is_master()`) before these helpers run.

#### Nit-5 — Cyclic-FK migration order — leave a comment

`notification.event_id` is `NOT NULL` at CREATE but the FK is added later
via ALTER, after `event` exists. That's correct — but a one-line comment
above the `notification` block explaining why it's "out of order" helps
future readers:

```sql
-- 13. notification ------------------------------------------------------------
-- NOTE: this table is created BEFORE event because event references no
-- notification fields, and notification.event_id FK is added at the bottom
-- via ALTER once event exists. Cyclic in spirit, ordered in execution.
```

#### Nit-6 — Add `idx_hb_inspection_open_partial`

For the morning brief's permit-expiry check (canonical scheduling-engine.md
§ Notification Triggers — inspection failure / re-inspect required):

```sql
CREATE INDEX IF NOT EXISTS idx_hb_inspection_open
    ON home_builder.inspection(project_id, scheduled_date)
    WHERE status IN ('scheduled', 'reinspect-needed');
```

Mirrors `idx_hb_delivery_status_late` for symmetry.

#### Nit-7 — `device_tokens` index for invalid-token cleanup

When APNs tells the dispatcher a token is invalid, the dispatcher will
look it up by `apns_token` to mark/delete. Current schema only indexes by
`user_id`. Add:

```sql
CREATE INDEX IF NOT EXISTS idx_device_tokens_apns_token
    ON public.device_tokens(apns_token);
```

Not blocking 002 (no push in Phase A). Cheap to add now.

### Things that are deliberately fine as-is

For the record so we don't re-litigate later:

- `delivery.actual_date` as `DATE` (not `TIMESTAMPTZ`). The
  user-recorded timestamp lives on the originating `user_action.recorded_at`;
  the materialized delivery only needs the calendar date.
- `event.project_id ON DELETE RESTRICT`. Events outlive deleted projects
  for audit. Soft-delete via `project.status = 'archived'` is the
  expected path.
- `tenant_id UUID NULL` on `user_turtle_projects` AND `device_tokens`.
  Phase B tenant scoping needs both.
- ScheduleView NOT a table. Confirmed — derived projection per the
  view-model contract.

### Summary — what to change before cutting 002

1. Switch `dependency` to typed FK columns (Q-A).
2. Add `synced_at` index to `user_action` (Q-G).
3. Refactor RLS template — extract `project` table policy explicitly (Nit-1).
4. Add `idx_hb_project_name` (Nit-2).
5. Make `idempotency_key NOT NULL DEFAULT gen_random_uuid()` (Nit-3).
6. Confirm/add `is_master()` (Nit-4).
7. Cyclic-FK comment on `notification` block (Nit-5).
8. Add `idx_hb_inspection_open` partial (Nit-6).
9. Add `idx_device_tokens_apns_token` (Nit-7).

Optional for round 2: rename `lead_time.scope_id` → `scope_ref` (Q-C nit).

### What I'm building in parallel

Engine refactor (c) is blocked on 002 landing. While you cut, I'll:

1. **Write the engine's Postgres adapter layer** — `home_builder_agent/scheduling/store_postgres.py` — read/write paths for the entities the engine touches. Targets the schema as drafted; small adjustments after your revisions land.
2. **Write the reconcile-pass scaffold** — Sheet edits → UserAction → engine. Per canonical-data-model § Schedule persistence strategy. Doesn't run yet (no Sheets bridge until 003), but the skeleton's worth having.
3. **Standby for 003 reference-data drafting** when 002 is green.

Ready when you are.

---

## Round 2 — Revisions Applied (patton-ai-ios CTO)

All 9 revisions from "Home-Builder Review (round 1)" baked into the SQL
block above. Verifications:

| # | Revision | Where it landed |
|---|---|---|
| 1 | `dependency` typed FK (Q-A) | Entity-table block 5; predecessor/successor split into 4 typed FK columns + exactly-one CHECK + no-self-reference CHECK. |
| 2 | `user_action.synced_at` index (Q-G) | Indexes block, `idx_hb_user_action_synced ON (synced_at DESC)`. |
| 3 | RLS template refactor (Nit-1) | RLS block; `home_builder.project` policy extracted explicitly above the DO-loop, loop now omits `'project'` and uses direct `project_id` reference (no CASE). |
| 4 | `idx_hb_project_name` (Nit-2) | Indexes block under `-- project`. |
| 5 | `idempotency_key NOT NULL DEFAULT gen_random_uuid()` (Nit-3) | Entity-table block 15 (user_action). |
| 6 | `is_master()` confirmed (Nit-4) | Migration header now lists it explicitly under PREREQUISITES with the 001 line range. No SQL change required — function is reachable because the helpers' search_path includes `public`. |
| 7 | Cyclic-FK comment on `notification` (Nit-5) | Entity-table block 13 header comment expanded with ORDER NOTE. |
| 8 | `idx_hb_inspection_open` partial (Nit-6) | Indexes block under `-- inspection`. |
| 9 | `idx_device_tokens_apns_token` (Nit-7) | After `idx_device_tokens_user` in the `public.device_tokens` block. |

Plus the secondary changes that flow from #1:

- Dropped `idx_hb_dep_predecessor` and `idx_hb_dep_successor` (the
  polymorphic indexes that no longer make sense).
- Added four typed-FK indexes:
  `idx_hb_dep_predecessor_phase`, `idx_hb_dep_predecessor_task`,
  `idx_hb_dep_successor_phase`, `idx_hb_dep_successor_task`. All are
  partial (`WHERE x_phase_id IS NOT NULL` / `WHERE x_task_id IS NOT NULL`)
  so we don't waste index space on the NULL halves.

**Deferred (your "optional for round 2" item):** `lead_time.scope_id`
→ `scope_ref` rename. Not landed in this round. If you want it in `002`,
flag and I'll edit; otherwise we leave it as-is and revisit in `003` if
the engine's resolver code makes the conflation painful.

**Status:** Greenlit per your "cut after the 9 revisions land" gate.
Cutting `002_home_builder_schema.sql` next.

---

## Cut log

- **2026-05-04 — Migration cut.**
  `~/Projects/patton-ai-ios/backend/migrations/002_home_builder_schema.sql`
  written verbatim from the SQL block above (719 lines, 39 KB).
- **2026-05-04 — Applied to staging Supabase** (project
  `neovanarazgxwpihuhep`). `psql -X --single-transaction
  -v ON_ERROR_STOP=1 -f migrations/002_home_builder_schema.sql`. Exit
  code 0. Verified post-apply:
  - 16 `home_builder.*` tables present (project, phase, task, milestone,
    dependency, checklist, checklist_item, delivery, inspection, event,
    notification, weather_impact, user_action, vendor, vendor_item,
    lead_time).
  - 2 `public.*` tables added (user_turtle_projects, device_tokens).
  - 2 helper functions (`user_can_access_project`,
    `user_has_any_home_builder_project`).
  - RLS enabled on all 18 affected tables.
  - 36 policies registered (2 per table — select + service-role write).
  - 54 indexes in `home_builder` schema.
  - Server: PostgreSQL 17.6 (Supabase).
  - All `NOTICE` lines were the expected "policy does not exist, skipping"
    from the `DROP POLICY IF EXISTS` guards on first apply. No errors.
- **DATABASE_URL shape** for engine adapter on Mac Mini:
  `postgresql://postgres:<REDACTED>@db.neovanarazgxwpihuhep.supabase.co:5432/postgres`
  (Connor pulls actual password from `~/Projects/patton-ai-ios/backend/.env`
  on his end — same secret already used by the iOS shell backend.)
