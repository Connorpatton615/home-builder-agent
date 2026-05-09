# Checklist authoring + photo upload

> Chad's flow of approval through a project, made first-class. He
> opens a phase, sees the checklist, ticks items off, edits wording or
> adds new steps in-place, snaps photos against photo-required items
> — and his template edits *persist* across deploys and propagate to
> every future project. This is the difference between software Chad
> uses and software Chad asks Connor to update on his behalf.

**Status:** Approved 2026-05-09 — ADR landed, spec ready for platform-thread implementation.
**Author:** Claude (drafted from CTO scope 2026-05-09 + Chad's feedback to Connor).
**Scope:** Vertical contract + architectural rules. Platform-thread (renderer + HTTP routes) implements against this contract.
**Cross-refs:**
- [`canonical-data-model.md`](canonical-data-model.md) §§ 6 Checklist + 7 ChecklistItem (existing entities)
- [`morning-view-model.md`](morning-view-model.md) (sibling spec — same renderer surface)
- [`~/Projects/patton-os/data/decisions.md`](file:///Users/connorpatton/Projects/patton-os/data/decisions.md) — binding ADR 2026-05-09 ("Checklist authoring architecture")

---

## What this spec is

Chad gave us redlines on the 24 phase checklist packet (commit `defd371`). His feedback had three asks beyond the redlines themselves:

1. **Edit wording on the fly** — he sees an item, decides it's worded wrong, taps to edit it inside the app
2. **Add new steps** — he realizes Foundation needs a step we missed, taps "+", fills it in
3. **Agent remembers** — his edits persist as *template* changes, not just one-off project annotations

Plus the photo upload affordance — he tap-snaps a photo against a photo-required item, the photo lands in his Drive, the system links it to the checklist item.

This spec defines how all four work end-to-end.

---

## Architectural decisions (binding — see ADR 2026-05-09)

These are not negotiable in v1. The platform thread implements against them.

### D1 — Database-as-template-store

The 24 JSON files at `home_builder_agent/scheduling/checklist_templates/` are **first-launch seeds only**. Once seeded into the new template tables (below), the database is the canonical source of truth for what a phase's checklist looks like. Chad's in-app edits write to the database; future-project instantiations read from the database.

The JSON files don't disappear (they remain the bootstrap import for new tenants), but they're never read at runtime after seeding.

### D2 — REST routes for template CRUD, not UserActions

Template edits are explicit, synchronous, user-initiated CRUD. They get standard REST endpoints with immediate response contracts:

- `PATCH /v1/turtles/home-builder/checklist-templates/{template_id}/items/{item_id}` — edit wording or photo_required
- `POST  /v1/turtles/home-builder/checklist-templates/{template_id}/items` — add new item
- `DELETE /v1/turtles/home-builder/checklist-templates/{template_id}/items/{item_id}` — soft-delete (deferred to v1.1)

The renderer gets immediate confirmation (200/204) or conflict (409). The UserAction → reconcile pattern stays reserved for AI-initiated or ambiguous-needs-review operations (drafted emails, reconciled phase status flips).

### D3 — Photo upload as multipart POST returning internal photo_id

```
POST /v1/turtles/home-builder/checklist-items/{item_id}/photos
Content-Type: multipart/form-data; boundary=...

[photo bytes]

→ 201 Created
{
  "photo_id": "uuid",
  "uploaded_at": "2026-05-09T13:30:00Z",
  "drive_url": "https://drive.google.com/file/d/.../view",   // for click-through display
  "checklist_item_id": "uuid"
}
```

Backend uploads to Drive at `Site Logs/<Project>/Checklist Photos/<phase>/<item-slug>/<timestamp>.jpg`, persists a `home_builder.checklist_item_photo` row, returns the **internal photo_id** as the primary identifier. The renderer never sees the Drive URL except as a click-through display target — storage backend is swappable without touching the UI.

### D4 — Propagation policy (the load-bearing rule)

This is the gotcha most teams miss. When Chad edits a template, what happens to project instances that already use that template?

**Wording edits propagate forward only.** Changing "Vapor barrier installed" to "Vapor barrier installed and lapped 12 inches at seams" updates the template, but checklist_item rows for in-flight projects keep their old wording. The next time a Foundation phase is instantiated (new project, or a phase on Whitfield not yet started), the new wording is used.

**Structural edits — add and delete — are template-only.** Adding a new item to the Foundation template does *not* insert it into Whitfield's already-instantiated Foundation checklist. Same for delete. This protects in-flight projects from surprise mutations on partially-completed checklists.

If Chad wants the new item to apply retroactively to Whitfield, he can manually add it via the renderer's per-project edit affordance (out of v1 scope; deferred until requested). The simple rule for v1: templates evolve forward; live instances are stable.

### D5 — `tenant_id` nullable from day one

Every new table includes `tenant_id UUID NULL` matching the established pattern (event, notification, draft_action). Single-tenant in v1, but the schema is multi-tenant-ready. No backfill drama later.

---

## Schema additions — Migration 010

Lands in `patton-ai-ios/backend/migrations/010_checklist_authoring.sql`. Follows the migration_005 + migration_007 conventions (RLS, idempotency, trigger function, indexes).

```sql
-- =============================================================================
-- migration_010_checklist_authoring.sql
-- =============================================================================
-- Per docs/specs/checklist-authoring.md and ADR 2026-05-09.
-- Three new tables: checklist_template, checklist_template_item,
-- checklist_item_photo. Plus a structural change to checklist_item
-- to denormalize the template_item_id (so item edits can resolve back
-- to template provenance for audit).
-- =============================================================================

CREATE TABLE IF NOT EXISTS home_builder.checklist_template (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  phase_slug      TEXT NOT NULL,            -- 'foundation', 'framing', etc.
  phase_template_id INTEGER NOT NULL,       -- 1-24 from CHECKLIST_PHASE_NAMES
  template_version TEXT NOT NULL,           -- 'v1.0-2026-05-07' or 'chad-edit-2026-05-15'
  description     TEXT,
  source          TEXT NOT NULL DEFAULT 'seeded'
                  CHECK (source IN ('seeded', 'chad-authored')),
  seeded_at       TIMESTAMPTZ NULL,         -- non-null on rows written from JSON seed
  tenant_id       UUID NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (phase_slug, tenant_id)            -- one active template per phase per tenant
);

CREATE TABLE IF NOT EXISTS home_builder.checklist_template_item (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  template_id     UUID NOT NULL REFERENCES home_builder.checklist_template(id) ON DELETE CASCADE,
  category        TEXT NOT NULL,
  label           TEXT NOT NULL,
  photo_required  BOOLEAN NOT NULL DEFAULT FALSE,
  sequence_index  INTEGER NOT NULL,         -- 0-based within (template_id, category)
  is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,  -- soft-delete (v1.1)
  version         INTEGER NOT NULL DEFAULT 1,      -- optimistic-update conflict detection
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (template_id, category, label) WHERE is_deleted = FALSE  -- prevent dup edits
);
CREATE INDEX idx_template_item_template_seq
  ON home_builder.checklist_template_item (template_id, category, sequence_index);

CREATE TABLE IF NOT EXISTS home_builder.checklist_item_photo (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  checklist_item_id UUID NOT NULL REFERENCES home_builder.checklist_item(id) ON DELETE CASCADE,
  drive_file_id   TEXT NOT NULL,
  drive_url       TEXT NOT NULL,
  caption         TEXT NULL,
  uploaded_by     UUID NULL REFERENCES auth.users(id) ON DELETE SET NULL,
  uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  tenant_id       UUID NULL
);
CREATE INDEX idx_checklist_item_photo_item
  ON home_builder.checklist_item_photo (checklist_item_id, uploaded_at DESC);

-- Add photo_required + template_item_id to existing checklist_item table
ALTER TABLE home_builder.checklist_item
  ADD COLUMN IF NOT EXISTS photo_required BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS template_item_id UUID NULL
      REFERENCES home_builder.checklist_template_item(id) ON DELETE SET NULL;

-- Triggers (uses home_builder.set_updated_at from migration 007)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'hb_checklist_template_updated_at') THEN
    CREATE TRIGGER hb_checklist_template_updated_at
      BEFORE UPDATE ON home_builder.checklist_template
      FOR EACH ROW EXECUTE FUNCTION home_builder.set_updated_at();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'hb_checklist_template_item_updated_at') THEN
    CREATE TRIGGER hb_checklist_template_item_updated_at
      BEFORE UPDATE ON home_builder.checklist_template_item
      FOR EACH ROW EXECUTE FUNCTION home_builder.set_updated_at();
  END IF;
END $$;

-- RLS: read for any authenticated user with project access (templates are
-- shared); writes by service_role only (REST endpoints run server-side).
ALTER TABLE home_builder.checklist_template ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.checklist_template_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE home_builder.checklist_item_photo ENABLE ROW LEVEL SECURITY;

CREATE POLICY hb_template_user_read ON home_builder.checklist_template
  FOR SELECT TO authenticated USING (true);  -- templates are tenant-scoped via tenant_id

CREATE POLICY hb_template_item_user_read ON home_builder.checklist_template_item
  FOR SELECT TO authenticated USING (true);

CREATE POLICY hb_checklist_item_photo_user_read ON home_builder.checklist_item_photo
  FOR SELECT TO authenticated
  USING (EXISTS (
    SELECT 1 FROM home_builder.checklist_item ci
    JOIN home_builder.checklist cl ON ci.checklist_id = cl.id
    JOIN home_builder.phase p ON cl.phase_id = p.id
    WHERE ci.id = checklist_item_photo.checklist_item_id
      AND home_builder.user_can_access_project(p.project_id)
  ));
```

---

## Seed-from-JSON one-shot

After migration 010 lands, run a seed script once per environment:

```bash
hb-checklist-seed --from-json --tenant-id <chad's tenant uuid or NULL for v1>
```

Behavior:
- Reads each of the 24 JSON files at `scheduling/checklist_templates/<phase>.json`
- For each, INSERTs one `checklist_template` row (`source='seeded'`, `seeded_at=NOW()`)
- Then INSERTs `checklist_template_item` rows for each item, preserving category + sequence_index
- **Idempotent**: skips templates where (phase_slug, tenant_id) already exists with `source='seeded'`. To force re-seed, pass `--force` (drops + re-inserts).

The CLI lives in `home_builder_agent/agents/checklist_seed_agent.py` and is wrapped as `hb-checklist-seed` in `pyproject.toml`.

---

## REST routes — what the renderer calls

### Read paths (already covered by existing `checklist-gates` view-model)

```
GET /v1/turtles/home-builder/views/checklist-gates/{project_id}
→ 200 ChecklistGatesViewPayload (with per-item photo_required + photos array)
```

The existing view-model projection (`view_models.py:checklist_gates_view`) extends to read `template_item_id` so the renderer can resolve back to the canonical template item for editing.

### Template edit

```
PATCH /v1/turtles/home-builder/checklist-templates/{template_id}/items/{item_id}
Content-Type: application/json
{
  "label": "Vapor barrier installed and lapped 12 inches at seams",
  "photo_required": true,
  "version": 3       // optimistic-update guard; current version expected
}
→ 200 { id, label, photo_required, version: 4, updated_at }
→ 409 if version mismatch (renderer re-fetches and retries)
→ 404 if item not found
```

Per **D4 propagation**: this update affects the template; in-flight `checklist_item` rows keep their cached label.

### Template add

```
POST /v1/turtles/home-builder/checklist-templates/{template_id}/items
Content-Type: application/json
{
  "category": "Pre-Pour Verification",
  "label": "Confirm gate access for concrete truck (gate code, lockbox)",
  "photo_required": false,
  "after_item_id": "uuid"  // optional — sequence_index = after.sequence_index + 1
}
→ 201 { id, category, label, photo_required, sequence_index, version: 1, created_at }
```

Per **D4**: new item appears in template; **does NOT insert** into existing project instances. Future Foundation phases pick it up.

### Template delete (deferred to v1.1)

```
DELETE /v1/turtles/home-builder/checklist-templates/{template_id}/items/{item_id}
→ 204
```

Soft-delete (`is_deleted = TRUE`). Out of v1 scope per CTO recommendation — too many propagation edge cases for the v1 ship.

### Photo upload

```
POST /v1/turtles/home-builder/checklist-items/{item_id}/photos
Content-Type: multipart/form-data
{
  "photo": <binary>,
  "caption": "Vapor barrier seamed at south wall, 14-inch lap"  // optional
}
→ 201 {
  "photo_id": "uuid",                    // PRIMARY identifier
  "drive_file_id": "abc123...",          // for backend reference
  "drive_url": "https://drive.google...", // for renderer click-through display
  "uploaded_at": "...",
  "checklist_item_id": "uuid",
  "caption": "..."
}
→ 413 if photo > 10MB (renderer should pre-compress)
```

Backend behavior:
1. Validate JWT + RLS access to checklist_item
2. Upload photo to Drive at `Site Logs/<Project>/Checklist Photos/<phase-slug>/<item-slug>/<timestamp>-<photo_id>.jpg`
3. INSERT row in `home_builder.checklist_item_photo`
4. Return the metadata response

Photo deletion: out of scope for v1 (Chad can manually remove from Drive; the row stays as audit).

### Photo list (renderer reads this when expanding an item card)

```
GET /v1/turtles/home-builder/checklist-items/{item_id}/photos
→ 200 [{ photo_id, drive_url, caption, uploaded_at, uploaded_by }, ...]
```

---

## Renderer contract — what the iOS / Mac surfaces do

### Surface: ChecklistAuthoring

Reachable from a phase row in the master schedule view (tap phase → see checklist). Default landing layout:

```
┌─────────────────────────────────────────────┐
│ Foundation                              ☰    │
│ 30 of 37 complete · 7 photos uploaded       │
├─────────────────────────────────────────────┤
│ ▼ Pre-Pour Verification (5/7)               │
│   ☑ Building permit posted on-site     📷✓  │  ← tap = view photo
│   ☐ Foundation layout staked          📷    │  ← tap photo icon = upload
│   ☐ Excavation depth matches schedule 📷    │
│   …                                          │
│   [+ Add item]                               │
│ ▼ Formwork & Reinforcement (3/7)            │
│   …                                          │
│ [+ Add category]                             │
└─────────────────────────────────────────────┘
```

Renderer affordances:

1. **Tap an item label** → toggle `is_complete` (existing `checklist-tick` UserAction; unchanged behavior)
2. **Tap the 📷 icon next to a photo-required item** →
   - If photo not yet uploaded: native camera/photo-picker opens (mobile: `<input type="file" accept="image/*" capture="environment">`; desktop: file dialog)
   - On selection: progress indicator, upload via the multipart POST route above
   - On 201: show uploaded thumbnail in the item card; cache photo_id locally for future reads
3. **Long-press an item label** → inline edit mode (text field replaces label)
   - On save: PATCH the template item; receive new version; update local state
   - On 409: fetch latest, prompt user to re-apply
4. **Tap "+ Add item"** at the end of any category →
   - Modal or inline form: category (dropdown), label (text), photo_required (toggle)
   - On save: POST the template item; inject into local state at appropriate sequence
5. **Tap photo thumbnail** → fullscreen view with caption + Drive click-through link

### Optimistic UI

For wording edits and adds, the renderer can apply the change locally immediately and reconcile with the server response. The `version` field on each template_item is the conflict-detection key — if the server returns 409, the renderer fetches the latest version and re-applies the edit on top of it (or prompts the user to choose).

---

## Engine-side adapter changes

`home_builder_agent/scheduling/store_postgres.py` adds:

```python
def load_checklist_template(phase_slug: str, *, tenant_id: str | None = None,
                             conn=None) -> dict | None: ...

def list_checklist_template_items(template_id: str, *, conn=None) -> list[dict]: ...

def update_checklist_template_item(item_id: str, *, label=None, photo_required=None,
                                    expected_version: int, conn=None) -> dict | None: ...

def insert_checklist_template_item(template_id: str, *, category: str, label: str,
                                    photo_required: bool, after_item_id: str | None,
                                    conn=None) -> dict: ...

def insert_checklist_item_photo(checklist_item_id: str, *, drive_file_id: str,
                                 drive_url: str, caption: str | None,
                                 uploaded_by: str | None, conn=None) -> str: ...

def list_checklist_item_photos(checklist_item_id: str, *, conn=None) -> list[dict]: ...
```

`scheduling/checklists.py:instantiate_checklist` is updated to read from the DB template (with JSON fallback for cold-start dev environments where seed hasn't run).

`scheduling/view_models.py:_checklist_item_to_payload` reads the per-item `photos` list from `checklist_item_photo` and surfaces it to the renderer.

---

## v1 implementation phases — what ships in what order

Per CTO recommended scope, ship as one coherent deliverable:

**Phase 1 (DB + seed):** Migration 010 + `hb-checklist-seed` CLI + apply against live Supabase. ~3h.

**Phase 2 (engine adapters):** new functions in `store_postgres.py`; update `instantiate_checklist`; update view-model projection. ~4h.

**Phase 3 (REST routes):** patton-ai-ios/backend route handlers for template PATCH/POST + photo POST. ~5h.

**Phase 4 (renderer):** iOS / Mac surface for ChecklistAuthoring with all five affordances. ~6h on iOS, +4h on Mac for desktop-class density. ~10h.

**Phase 5 (testing + Chad walkthrough):** end-to-end smoke against Whitfield, Chad first-touch demo. ~3h.

**Total: ~25h end-to-end** (CTO's 29h mid-estimate, slightly trimmed by deferring camera-affordance polish to file-input-with-capture).

---

## Out of v1 scope (deferred)

Per CTO recommendation:

- **Soft-delete / item removal** — propagation edge cases (what about photos already uploaded against the deleted item?); add when Chad explicitly asks
- **Native camera affordance** — `<input type="file" accept="image/*" capture="environment">` gives camera-first UX on mobile for free; native UIImagePickerController polish is v1.5
- **Per-project template overrides** — Chad might want to edit Foundation differently for Whitfield specifically without affecting future projects; v1 says "edit the template, propagate forward"; per-project drift is v2
- **Reorder via drag-handle** — sequence_index is in the schema; reorder UI is v1.5
- **Photo deletion** — Chad can manually remove from Drive; row stays as audit; explicit delete affordance in v1.5
- **Multi-photo gallery per item** — schema supports many photos per item; renderer surfaces the latest in v1, gallery view in v1.5

---

## Open questions for the platform thread

These need answers before the renderer surface is wired (the spec doesn't lock them; the platform thread decides):

1. **Photo compression on the renderer** — iOS UIImage at full resolution can be 10MB+. Where does compression happen — renderer (preferred, saves bandwidth) or server (fallback)?
2. **Optimistic-update conflict UX** — when the server returns 409 on an edit, does the renderer auto-merge the latest server state, or prompt Chad to choose? Recommendation: prompt for now; auto-merge is v1.5.
3. **"Add category" affordance** — do we let Chad add a new category, or only add items to existing categories? Recommendation: existing only in v1; add-category is v1.5 (it changes the data shape per phase template, more migration risk).
4. **Photo upload offline** — when Chad's phone is on bad job-site wifi, what happens? Recommendation: queue locally on the renderer, retry when connectivity returns. Out of v1 scope but flag for v1.5.

---

## Cross-references this spec touches

- Updates `canonical-data-model.md` § View-model contract — `checklist-gates` view payload now includes `photos` per item + `template_item_id` for edit-back-to-template
- Adds new entities #19 (ChecklistTemplate), #20 (ChecklistTemplateItem), #21 (ChecklistItemPhoto) to canonical-data-model.md
- Updates `scheduling-engine.md` § Checklist library — references the DB-backed template store
- Migration 010 lands in `patton-ai-ios/backend/migrations/`
- ADR 2026-05-09 in `~/Projects/patton-os/data/decisions.md` is the binding architectural anchor
