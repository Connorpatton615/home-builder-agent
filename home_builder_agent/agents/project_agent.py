"""project_agent.py — hb-project: project lifecycle CLI.

Closes the gap surfaced by Chad's iOS Ask tab on 2026-05-09, where
hb-chad correctly identified that archive_project / create_project /
clone_project tools didn't exist. This is the engine-side primitive;
hb-router exposes it as the `manage-project` command type;
hb-chad's `manage_project` tool routes plain-English asks through it.

CLI:
  hb-project list [--include-archived]
  hb-project archive <name|uuid> [--reason "<text>"]
  hb-project create --name "<name>" [options]
  hb-project clone <source-name|uuid> --name "<new-name>" [options]
  hb-project show <name|uuid>

Cost: $0/run (no Claude calls — pure DB writes).

Drive-side changes (folder rename on archive, Tracker sheet clone on
create) are NOT in v1 of this CLI. They land as a follow-on once the
Drive folder structure is finalized at cutover. For Connor's pre-
cutover test loops, DB-only is sufficient — the active vs. archived
filter on /views/* fetches handles surface routing.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid as _uuid
from datetime import date, datetime

from home_builder_agent.observability.json_log import configure_json_logging


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"❌ Invalid date {s!r}; expected YYYY-MM-DD")


def _resolve_project(name_or_id: str) -> dict | None:
    """Mirror of triggers_agent._resolve_project — UUID → exact name →
    case-insensitive substring → also matches archived projects so the
    archive command works against already-archived ids cleanly.
    """
    from home_builder_agent.scheduling.store_postgres import (
        load_active_projects,
        load_project_by_id,
        load_project_by_name,
        _query_one,
    )

    # 1. UUID-shaped exact lookup
    try:
        _uuid.UUID(name_or_id)
        row = load_project_by_id(name_or_id)
        if row:
            return row
    except (ValueError, KeyError):
        pass

    # 2. Exact name match (active only — load_project_by_name's filter)
    row = load_project_by_name(name_or_id)
    if row:
        return row

    # 3. Substring match across ALL projects including archived
    needle = name_or_id.lower()
    try:
        all_rows = _query_one(
            """
            SELECT json_agg(p) AS projects FROM (
                SELECT
                    id::text AS id, name, customer_name, address,
                    target_completion_date, target_framing_start_date,
                    status, drive_folder_id, drive_folder_path, tenant_id,
                    created_at, updated_at
                FROM home_builder.project
                ORDER BY created_at DESC
            ) p
            """
        )
        for p in (all_rows.get("projects") if all_rows else None) or []:
            if needle in (p.get("name") or "").lower():
                return p
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_list(args) -> int:
    """List projects."""
    from home_builder_agent.scheduling.store_postgres import _query_many

    where = "" if args.include_archived else "WHERE status != 'archived'"
    rows = _query_many(
        f"""
        SELECT
            id::text AS id, name, customer_name, status,
            target_completion_date, target_framing_start_date,
            created_at
        FROM home_builder.project
        {where}
        ORDER BY status NULLS LAST, name
        """,
    )
    if not rows:
        print("(no projects)")
        return 0
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    print()
    print(f"  {len(rows)} project{'s' if len(rows) != 1 else ''}"
          + (" (archived included)" if args.include_archived else ""))
    print()
    for r in rows:
        status_chip = {
            "active": "🟢 active   ",
            "planning": "🟡 planning ",
            "archived": "⚪ archived ",
        }.get(r["status"] or "", f"   {r.get('status') or '?':<10}")
        completion = r.get("target_completion_date")
        completion_str = completion.isoformat() if completion else "(no target)"
        print(f"  {status_chip}  {r['name']:<40}  → {completion_str}  "
              f"(id {r['id'][:8]}…)")
    print()
    return 0


def _cmd_show(args) -> int:
    proj = _resolve_project(args.project)
    if not proj:
        print(f"❌ No project matched {args.project!r}")
        return 1
    if args.json:
        print(json.dumps(proj, indent=2, default=str))
        return 0
    print()
    print(f"  {proj['name']}")
    print(f"  ──────────────────────────────────────────")
    print(f"  id:                  {proj['id']}")
    print(f"  status:              {proj['status']}")
    print(f"  customer:            {proj.get('customer_name') or '(none)'}")
    print(f"  address:             {proj.get('address') or '(none)'}")
    print(f"  target completion:   {proj.get('target_completion_date') or '(none)'}")
    print(f"  target framing:      {proj.get('target_framing_start_date') or '(none)'}")
    print(f"  drive folder id:     {(proj.get('drive_folder_id') or '(none)')[:30]}")
    print(f"  tenant_id:           {proj.get('tenant_id') or '(NULL — single-tenant)'}")
    print(f"  created_at:          {proj.get('created_at')}")
    print()
    return 0


def _cmd_archive(args) -> int:
    from home_builder_agent.scheduling.store_postgres import archive_project_in_db

    proj = _resolve_project(args.project)
    if not proj:
        print(f"❌ No project matched {args.project!r}")
        return 1

    if proj["status"] == "archived":
        print(f"  {proj['name']} is already archived. No-op.")
        return 0

    if not args.yes:
        # Confirm — destructive-ish (reversible via UPDATE but still)
        print(f"\nArchive: {proj['name']} ({proj['id'][:8]}…)?")
        print(f"  status: {proj['status']} → archived")
        if args.reason:
            print(f"  reason: {args.reason}")
        confirm = input("  Type 'yes' to confirm: ").strip().lower()
        if confirm not in ("yes", "y"):
            print("  Aborted.")
            return 1

    ok = archive_project_in_db(proj["id"], reason=args.reason)
    if ok:
        print(f"✅ Archived: {proj['name']} (id {proj['id'][:8]}…)")
        if args.reason:
            print(f"   reason: {args.reason}")
        print(
            "   Phase / event / draft history preserved. "
            "Active-project surfaces no longer show this project."
        )
        return 0
    else:
        print(f"⚠️  Update returned False (race? already archived?)")
        return 1


def _cmd_create(args) -> int:
    from home_builder_agent.scheduling.store_postgres import (
        create_project_in_db,
        clone_project_in_db,
    )

    if args.copy_from:
        # CLONE path
        source = _resolve_project(args.copy_from)
        if not source:
            print(f"❌ Source project {args.copy_from!r} not found")
            return 1
        new_id = clone_project_in_db(
            source["id"],
            new_name=args.name,
            customer_name=args.customer_name,
            address=args.address,
            target_completion_date=_parse_date(args.target_completion),
            target_framing_start_date=_parse_date(args.target_framing_start),
        )
        print(f"\n✅ Created (cloned from {source['name']}):")
        print(f"   {args.name}")
        print(f"   id: {new_id}")
        print(f"   phases + milestones copied from source (status=not-started)")
        print(
            "\n   Next: run `hb-update` against this project to update planned dates "
            "if you passed --target-completion or --target-framing-start.\n"
        )
        return 0

    # FRESH path
    if not (args.target_completion or args.target_framing_start):
        print(
            "❌ create requires either --target-completion or "
            "--target-framing-start (or pass --copy-from <project> to "
            "clone an existing one)"
        )
        return 1

    new_id = create_project_in_db(
        name=args.name,
        customer_name=args.customer_name or "TBD",
        address=args.address,
        target_completion_date=_parse_date(args.target_completion),
        target_framing_start_date=_parse_date(args.target_framing_start),
    )
    print(f"\n✅ Created (no phases yet):")
    print(f"   {args.name}")
    print(f"   id: {new_id}")
    print(
        "\n   Next: run `hb-schedule \"" + args.name + "\" --target-completion "
        f"{args.target_completion or args.target_framing_start} --seed-postgres` "
        "to instantiate phases.\n"
    )
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    configure_json_logging("hb-project")
    parser = argparse.ArgumentParser(
        description="Project lifecycle CLI — list, archive, create, clone.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # list
    p_list = sub.add_parser("list", help="List active (and optionally archived) projects.")
    p_list.add_argument("--include-archived", action="store_true",
                        help="Include archived projects in the listing.")
    p_list.add_argument("--json", action="store_true", help="JSON output.")

    # show
    p_show = sub.add_parser("show", help="Show one project's full record.")
    p_show.add_argument("project", help="Name (substring OK) or UUID.")
    p_show.add_argument("--json", action="store_true")

    # archive
    p_archive = sub.add_parser("archive", help="Mark a project as archived (status flip).")
    p_archive.add_argument("project", help="Name (substring OK) or UUID.")
    p_archive.add_argument("--reason", help="Optional reason; logged but not persisted.")
    p_archive.add_argument("--yes", "-y", action="store_true",
                           help="Skip confirmation prompt.")

    # create / clone
    p_create = sub.add_parser(
        "create",
        help="Create a new project. Pass --copy-from to clone shape; "
             "otherwise pass --target-completion or --target-framing-start.",
    )
    p_create.add_argument("--name", required=True, help="New project name (required).")
    p_create.add_argument(
        "--copy-from",
        help="Source project (name substring or UUID). Clones phases + "
             "milestones with fresh status. Without this, creates an "
             "empty shell with no phases.",
    )
    p_create.add_argument("--customer-name", help="Customer / homeowner name.")
    p_create.add_argument("--address", help="Project address.")
    p_create.add_argument("--target-completion", help="YYYY-MM-DD")
    p_create.add_argument("--target-framing-start", help="YYYY-MM-DD")

    args = parser.parse_args()

    if args.cmd == "list":
        sys.exit(_cmd_list(args))
    if args.cmd == "show":
        sys.exit(_cmd_show(args))
    if args.cmd == "archive":
        sys.exit(_cmd_archive(args))
    if args.cmd == "create":
        sys.exit(_cmd_create(args))


if __name__ == "__main__":
    main()
