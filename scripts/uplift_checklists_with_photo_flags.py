"""uplift_checklists_with_photo_flags.py — one-time content pass.

Reads each of the 24 phase checklist JSON files under
home_builder_agent/scheduling/checklist_templates/, sends the items
to Sonnet to flag photo_required per item, and writes the file back
with the new {label, photo_required} shape.

Idempotent: items already in dict shape are passed through unchanged
unless --force is set. Items in plain-string shape are upgraded.

Usage:
    PYTHONPATH=. python3 scripts/uplift_checklists_with_photo_flags.py [--dry-run] [--force] [--phase NAME]

Cost: ~$0.005 per phase × 24 phases = ~$0.12 total.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from home_builder_agent.config import WRITER_MODEL
from home_builder_agent.core.claude_client import make_client, sonnet_cost


TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent
    / "home_builder_agent"
    / "scheduling"
    / "checklist_templates"
)

SYSTEM_PROMPT = """You are reviewing a home builder's phase checklist for a custom-home build in Baldwin County, Alabama. For each checklist item the builder lists, decide whether the builder should require photo evidence to close that item.

photo_required = true   physical site work that should be visually documented for quality / inspection / legal record
photo_required = false  purely administrative paperwork, coordination, or filing-cabinet items

Examples that are clearly true (photo evidence expected):
- Vapor barrier installed and lapped
- Rebar size, spacing, lap lengths verified in formwork
- Concrete placed, consolidated, finished
- Hurricane straps installed at every roof framing connection
- Anchor bolts set per structural drawings
- Drywall hung and screwed
- Termite pre-treatment applied

Examples that are clearly false (administrative):
- Building permit submitted to Baldwin County
- Schedule confirmed with subcontractor
- Insurance certificates exchanged
- Concrete delivery tickets reviewed on arrival
- Engineer's stamped plans on file
- Mill certs on file
- Subcontractor confirmed on-site with current COI

Edge cases — apply this rule:
- "Permit card posted on-site" → true (photo of posted card is reasonable evidence; courts have treated photographed card as acceptable)
- "Inspector signed and dated card" → true (photo of signed card)
- "Site survey complete and on file" → false (paperwork item)
- "Approved plans match permit set" → false (paperwork verification)

When in doubt, prefer true — false-positives are a 2-second discard for the builder; missing flags are a missed legal/quality protection.

Output JSON ONLY. No markdown fence, no commentary. Output is an array of objects, same length and order as the input. Each object: {"label": "<verbatim>", "photo_required": <bool>}."""


def _strip_fence(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*\n?", "", s)
    s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


def _flatten_items(template: dict) -> list[tuple[int, int, dict]]:
    """Return [(cat_idx, item_idx, item_obj_or_str), ...] in document order."""
    out = []
    for ci, cat in enumerate(template.get("categories", [])):
        for ii, it in enumerate(cat.get("items", [])):
            out.append((ci, ii, it))
    return out


def _normalize_to_dict(item) -> dict:
    """{label, photo_required} from either a plain string or an existing dict."""
    if isinstance(item, str):
        return {"label": item, "photo_required": False}
    if isinstance(item, dict):
        return {
            "label": item.get("label", ""),
            "photo_required": bool(item.get("photo_required", False)),
        }
    return {"label": str(item), "photo_required": False}


def _phase_needs_uplift(template: dict, *, force: bool) -> bool:
    """True if any item is still a plain string OR if --force is set."""
    if force:
        return True
    for cat in template.get("categories", []):
        for it in cat.get("items", []):
            if isinstance(it, str):
                return True
            if isinstance(it, dict) and "photo_required" not in it:
                return True
    return False


def _ask_sonnet_for_flags(client, phase_name: str, items: list[dict]) -> tuple[list[dict], object]:
    """One Sonnet call per phase; returns the flagged items + the usage object."""
    user_prompt = f"""Phase: {phase_name}

Items:
{json.dumps([{'label': i['label']} for i in items], indent=2)}

Output the flagged array (same length, same order) as JSON only."""
    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = response.content[0].text
    cleaned = _strip_fence(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Sonnet response failed to parse for {phase_name}: {e}\nRaw: {cleaned[:400]}"
        )
    if not isinstance(parsed, list) or len(parsed) != len(items):
        raise ValueError(
            f"Sonnet returned {type(parsed).__name__} of length "
            f"{len(parsed) if isinstance(parsed, list) else '?'}; "
            f"expected list of {len(items)}"
        )
    out = []
    for src, flagged in zip(items, parsed, strict=True):
        if not isinstance(flagged, dict):
            raise ValueError(f"Sonnet item not a dict: {flagged!r}")
        out.append({
            "label": src["label"],   # preserve verbatim from input
            "photo_required": bool(flagged.get("photo_required", False)),
        })
    return out, response.usage


def _process_template(client, path: Path, *, dry_run: bool, force: bool):
    template = json.loads(path.read_text())
    phase_name = template.get("phase_name", path.stem)

    if not _phase_needs_uplift(template, force=force):
        return {
            "phase": phase_name,
            "skipped": True,
            "items": 0,
            "photo_required_count": 0,
            "cost": 0.0,
        }

    # Flatten + normalize current items
    flat = _flatten_items(template)
    normalized = [_normalize_to_dict(it) for (_, _, it) in flat]

    # One Sonnet call for the whole phase
    flagged, usage = _ask_sonnet_for_flags(client, phase_name, normalized)
    cost_breakdown = sonnet_cost(usage)
    cost_usd = cost_breakdown["total"]

    # Write flags back into the template structure
    cursor = 0
    for cat in template.get("categories", []):
        new_items = []
        for _ in cat.get("items", []):
            new_items.append({
                "label": flagged[cursor]["label"],
                "photo_required": flagged[cursor]["photo_required"],
            })
            cursor += 1
        cat["items"] = new_items

    if not dry_run:
        path.write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n")

    photo_required_count = sum(1 for f in flagged if f["photo_required"])
    return {
        "phase": phase_name,
        "skipped": False,
        "items": len(flagged),
        "photo_required_count": photo_required_count,
        "cost": cost_usd,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Add photo_required flags to phase checklist templates via Sonnet."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change; don't write files.")
    parser.add_argument("--force", action="store_true",
                        help="Re-flag items even if already in {label, photo_required} shape.")
    parser.add_argument("--phase", default=None,
                        help="Restrict to a single phase (use the slug, e.g. 'foundation').")
    args = parser.parse_args()

    if not TEMPLATE_DIR.exists():
        print(f"Template dir missing: {TEMPLATE_DIR}", file=sys.stderr)
        sys.exit(1)

    files = sorted(TEMPLATE_DIR.glob("*.json"))
    if args.phase:
        slug = args.phase.lower().replace(" ", "-")
        files = [p for p in files if p.stem == slug]
        if not files:
            print(f"No template matched --phase {args.phase!r}", file=sys.stderr)
            sys.exit(1)

    print(f"{'(dry-run) ' if args.dry_run else ''}"
          f"Uplift {len(files)} phase template(s){' (force=on)' if args.force else ''}\n")

    client = make_client()
    total_cost = 0.0
    total_items = 0
    total_photo = 0
    summary = []
    for path in files:
        try:
            r = _process_template(client, path, dry_run=args.dry_run, force=args.force)
        except Exception as e:
            print(f"  ⚠️  {path.stem}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        summary.append(r)
        if r["skipped"]:
            print(f"  ⏭️   {r['phase']:24} (already flagged; --force to re-run)")
            continue
        print(
            f"  ✅  {r['phase']:24} "
            f"{r['items']:3} items, "
            f"📷 {r['photo_required_count']:3} require photos  "
            f"(${r['cost']:.4f})"
        )
        total_cost += r["cost"]
        total_items += r["items"]
        total_photo += r["photo_required_count"]

    print()
    print(f"Total: {total_items} items across {len([r for r in summary if not r['skipped']])} phase(s) "
          f"flagged; {total_photo} require photos. Cost: ${total_cost:.4f}")
    if args.dry_run:
        print("(dry-run — no files written)")


if __name__ == "__main__":
    main()
