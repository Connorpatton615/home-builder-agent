"""checklist_seed_agent.py — one-shot seed of the DB-backed checklist
template store from the 24 JSON files in scheduling/checklist_templates/.

Per docs/specs/checklist-authoring.md (D1) and ADR 2026-05-09:
the JSON files are FIRST-LAUNCH SEEDS ONLY. Once this CLI has run
against an environment, runtime never reads the JSON files again —
home_builder.checklist_template + home_builder.checklist_template_item
are the canonical source of truth.

Usage (one-time per environment, after migration 010 applies):

    DATABASE_URL=postgresql://... hb-checklist-seed --from-json
    DATABASE_URL=postgresql://... hb-checklist-seed --from-json --tenant-id <uuid>
    DATABASE_URL=postgresql://... hb-checklist-seed --from-json --force        # drop + reinsert
    DATABASE_URL=postgresql://... hb-checklist-seed --check                    # report only

Idempotency:
    Default behavior skips any (phase_slug, tenant_id) row that already
    exists with source='seeded'. Re-running is a no-op + verification
    print. --force drops the existing template (CASCADE removes items)
    and reinserts from JSON; use only when the JSON has been edited and
    you want the template tables to catch up.

Why a CLI and not a startup hook:
    Schema migrations apply via psql; data seeds are a separate explicit
    step the operator runs once per environment. This keeps the
    dev → staging → prod flow predictable and auditable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

from home_builder_agent.integrations.postgres import connection
from home_builder_agent.scheduling.checklists import slugify
from home_builder_agent.scheduling.phases import CHECKLIST_PHASE_NAMES


TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent / "scheduling" / "checklist_templates"
)


# ---------------------------------------------------------------------------
# JSON → seed-row projection
# ---------------------------------------------------------------------------


def _resolve_template_file(phase_name: str) -> Path | None:
    """Return the JSON path for a phase, or None if the file is missing.

    Phases without templates land as a stub in the source JSON layout
    (precon.json shipped first; the rest were filled in commit defd371
    on home-builder-agent main). If a file is missing here, the seeder
    skips it with a warning rather than erroring — easier ops than
    forcing all 24 to exist before the first run.
    """
    path = TEMPLATE_DIR / f"{slugify(phase_name)}.json"
    return path if path.exists() else None


def _load_template_json(path: Path) -> dict:
    """Read a phase template JSON file. Lets ValueError surface — the
    operator wants to know if a hand-edited template stopped parsing."""
    return json.loads(path.read_text())


def _items_from_template(template: dict) -> list[tuple[str, str, bool, int]]:
    """Flatten the JSON `categories: [{name, items: [...]}]` shape into
    (category, label, photo_required, sequence_index) tuples.

    `sequence_index` is 0-based within (category) and preserves the JSON
    order. Per spec § Schema additions: sequence_index lives on the
    template_item row so the renderer can reorder via drag-handle in
    v1.5 without re-importing the JSON.

    Items may be plain strings (legacy V1 format from the early Precon
    template) or {label, photo_required} objects (current shape since
    commit defd371). Both are tolerated — slugify is idempotent on the
    label so the UNIQUE constraint catches dupes either way.
    """
    rows: list[tuple[str, str, bool, int]] = []
    for cat in template.get("categories", []):
        cat_name = cat.get("name", "Uncategorized")
        for idx, raw in enumerate(cat.get("items", [])):
            if isinstance(raw, str):
                label, photo_required = raw, False
            elif isinstance(raw, dict):
                label = raw.get("label", "")
                photo_required = bool(raw.get("photo_required", False))
            else:
                continue
            if not label:
                continue
            rows.append((cat_name, label, photo_required, idx))
    return rows


# ---------------------------------------------------------------------------
# Seed driver
# ---------------------------------------------------------------------------


def seed_one_phase(
    conn: psycopg.Connection,
    *,
    phase_template_id: int,
    phase_name: str,
    tenant_id: str | None,
    force: bool,
) -> dict:
    """Seed one phase. Returns a {action, items_inserted, template_id}
    summary suitable for the CLI's progress print."""
    slug = slugify(phase_name)
    path = _resolve_template_file(phase_name)
    if path is None:
        return {"action": "skipped-missing-json", "phase": phase_name, "items_inserted": 0}

    template = _load_template_json(path)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text AS id, source
              FROM home_builder.checklist_template
             WHERE phase_slug = %s
               AND tenant_id IS NOT DISTINCT FROM %s::uuid
            """,
            (slug, tenant_id),
        )
        existing = cur.fetchone()

        if existing and not force:
            return {
                "action": "skipped-already-seeded",
                "phase": phase_name,
                "template_id": existing["id"],
                "items_inserted": 0,
            }

        if existing and force:
            # CASCADE on checklist_template_item.template_id removes child rows.
            # Live home_builder.checklist_item.template_item_id rows are SET NULL
            # via the FK on migration 010, so in-flight projects survive.
            cur.execute(
                "DELETE FROM home_builder.checklist_template WHERE id = %s::uuid",
                (existing["id"],),
            )

        cur.execute(
            """
            INSERT INTO home_builder.checklist_template (
                phase_slug, phase_template_id, template_version,
                description, source, seeded_at, tenant_id
            )
            VALUES (
                %s, %s, %s,
                %s, 'seeded', NOW(), %s::uuid
            )
            RETURNING id::text AS id
            """,
            (
                slug,
                phase_template_id,
                template.get("template_version", "v0-stub"),
                template.get("description"),
                tenant_id,
            ),
        )
        template_id = cur.fetchone()["id"]

        item_rows = _items_from_template(template)
        if item_rows:
            cur.executemany(
                """
                INSERT INTO home_builder.checklist_template_item (
                    template_id, category, label, photo_required, sequence_index
                )
                VALUES (%s::uuid, %s, %s, %s, %s)
                """,
                [(template_id, cat, label, photo_required, seq) for (cat, label, photo_required, seq) in item_rows],
            )

    return {
        "action": "reseeded" if (existing and force) else "inserted",
        "phase": phase_name,
        "template_id": template_id,
        "items_inserted": len(item_rows),
    }


def seed_all(
    *,
    tenant_id: str | None,
    force: bool,
) -> list[dict]:
    """Seed all 24 phases in CHECKLIST_PHASE_NAMES order. Single
    transaction — if any phase fails, the whole seed rolls back. The
    UNIQUE constraint on (phase_slug, tenant_id) is the safety net for
    accidental concurrent runs."""
    summaries: list[dict] = []
    with connection(application_name="hb-checklist-seed") as conn:
        for idx, phase_name in enumerate(CHECKLIST_PHASE_NAMES, start=1):
            summary = seed_one_phase(
                conn,
                phase_template_id=idx,
                phase_name=phase_name,
                tenant_id=tenant_id,
                force=force,
            )
            summaries.append(summary)
    return summaries


def report_state(*, tenant_id: str | None) -> list[dict]:
    """Read-only count of templates + items currently in the DB. Use
    after a seed to confirm 24 phases / ~923 items landed."""
    with connection(application_name="hb-checklist-seed") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.phase_slug,
                       t.phase_template_id,
                       t.template_version,
                       t.source,
                       t.seeded_at,
                       COUNT(ti.id) AS item_count,
                       SUM(CASE WHEN ti.photo_required THEN 1 ELSE 0 END) AS photo_required_count
                  FROM home_builder.checklist_template t
                  LEFT JOIN home_builder.checklist_template_item ti
                    ON ti.template_id = t.id AND ti.is_deleted = FALSE
                 WHERE t.tenant_id IS NOT DISTINCT FROM %s::uuid
                 GROUP BY t.phase_slug, t.phase_template_id, t.template_version,
                          t.source, t.seeded_at
                 ORDER BY t.phase_template_id
                """,
                (tenant_id,),
            )
            return list(cur.fetchall())


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="hb-checklist-seed",
        description=(
            "Seed home_builder.checklist_template + checklist_template_item "
            "from scheduling/checklist_templates/*.json. Runs once per "
            "environment after migration 010 lands."
        ),
    )
    parser.add_argument(
        "--from-json",
        action="store_true",
        help="Read the 24 JSON template files and insert (default action).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report only — count templates and items currently in the DB.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Drop existing seeded templates and reinsert. Use sparingly.",
    )
    parser.add_argument(
        "--tenant-id",
        default=None,
        help="Tenant UUID for multi-tenant seeding. v1 leaves this NULL.",
    )
    args = parser.parse_args()

    if args.check:
        rows = report_state(tenant_id=args.tenant_id)
        print(f"\nChecklist templates in DB (tenant_id={args.tenant_id or 'NULL'}):\n")
        total_items = 0
        total_photos = 0
        for r in rows:
            items = int(r["item_count"] or 0)
            photos = int(r["photo_required_count"] or 0)
            total_items += items
            total_photos += photos
            seeded = r["seeded_at"].isoformat() if r["seeded_at"] else "—"
            print(
                f"  [{r['phase_template_id']:>2}] {r['phase_slug']:<24} "
                f"v={r['template_version']:<22} src={r['source']:<14} "
                f"items={items:>3} photo_required={photos:>3} seeded_at={seeded}"
            )
        print(f"\n  {len(rows)} templates · {total_items} items · {total_photos} photo_required")
        return 0

    if not args.from_json:
        parser.print_help()
        return 2

    summaries = seed_all(tenant_id=args.tenant_id, force=args.force)

    inserted = sum(1 for s in summaries if s["action"] in {"inserted", "reseeded"})
    skipped = sum(1 for s in summaries if s["action"] == "skipped-already-seeded")
    missing = sum(1 for s in summaries if s["action"] == "skipped-missing-json")
    item_total = sum(s.get("items_inserted", 0) for s in summaries)

    print()
    for s in summaries:
        action = s["action"]
        if action == "skipped-already-seeded":
            print(f"  [skip] {s['phase']:<24}  already seeded (template_id={s['template_id']})")
        elif action == "skipped-missing-json":
            print(f"  [warn] {s['phase']:<24}  no JSON file on disk")
        else:
            print(
                f"  [{action[:4]}] {s['phase']:<24}  "
                f"items={s['items_inserted']:>3}  template_id={s['template_id']}"
            )

    print(
        f"\n  Done: {inserted} inserted/reseeded · {skipped} already seeded · "
        f"{missing} missing JSON · {item_total} total items written"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
