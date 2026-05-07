"""generate_checklist_templates.py — Sonnet authors the 23 non-Precon phase
checklist templates.

The Precon master template (44 items / 10 categories) was authored by hand to
match Chad's brief. The other 23 phases per scheduling-engine.md § Checklist
Library are generated here from industry-standard practice for luxury custom
home construction in the Southeast US, structured to mirror Precon's shape
(category → items list). Output is a JSON file per phase under
home_builder_agent/scheduling/checklist_templates/.

Chad redlines these as part of the desktop-renderer build (per
docs/specs/desktop-renderer.md § Phase 5 — Test, "Connor uses it for a full
operating day on Whitfield without falling back to Terminal" implies he's
seen the lists and approved them). This generator gives him drafts to redline
rather than blank pages.

Usage:
    python3 scripts/generate_checklist_templates.py
    python3 scripts/generate_checklist_templates.py --phase "Cabinet"
    python3 scripts/generate_checklist_templates.py --dry-run
    python3 scripts/generate_checklist_templates.py --skip-existing

Cost: ~$0.04-0.08 per phase × 23 phases ≈ ~$1-2 total.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from home_builder_agent.config import (
    SONNET_INPUT_COST,
    SONNET_OUTPUT_COST,
    WRITER_MODEL,
)
from home_builder_agent.core.claude_client import make_client
from home_builder_agent.scheduling.checklists import TEMPLATE_DIR, slugify
from home_builder_agent.scheduling.phases import CHECKLIST_PHASE_NAMES


PROMPT_TEMPLATE = """You are authoring a construction phase checklist for Palmetto Custom Homes, a luxury custom home builder in Baldwin County, Alabama. Average build is $600k–$1.5M.

The phase you're authoring for: **{phase_name}**

Below is the master "Precon" template that defines the structural pattern (10 categories × ~4 items per category, real concrete checks not vague aspirations):

{precon_template}

Now author a checklist for the **{phase_name}** phase. Rules:

1. Categories are phase-specific and reflect what an experienced superintendent would track on the ground. Most phases have 3–6 categories. Examples by phase type:
   - Crew/labor phases (Framing, Drywall, Trim): "Crew & Schedule", "Materials On-Site", "Inspections", "Quality Checks"
   - MEP rough phases: "Plans & Permits", "Rough-In Work", "Inspection", "Pre-Cover Sign-Off"
   - Selection/install phases (Cabinet, Tile, Wood Flooring): "Pre-Install Verification", "Installation", "Quality & Punch", "Sign-Off"
   - Set-out / trim-out phases: "Final Connections", "Functional Tests", "Inspection", "Walkthrough"
   - Final / punch-out phases: "Punch List", "Cleaning", "Client Walkthrough", "Closeout"

2. Each category has 3–7 items. Items are concrete, verifiable checks an inspector would tick. Examples:
   - GOOD: "All sole plates set on slab line, anchor bolts engaged, inspected by superintendent"
   - GOOD: "All exterior wall outlets within 12 inches of code-required spacing"
   - BAD: "Framing is good"
   - BAD: "Make sure things look nice"

3. Items reflect the quality bar of a $1M+ custom home, not a tract build. Specific tolerances, named code requirements, and named inspections (Baldwin County) are encouraged when they're real.

4. Do NOT include items already covered in earlier phases. Don't repeat Precon's "contract signed" or "permits submitted" — those close in their own phases.

Output a single JSON object with EXACTLY this shape (no preamble, no markdown fences, no explanation):

{{
  "phase_name": "{phase_name}",
  "template_version": "v1.0-2026-05-07",
  "description": "<one-sentence description of what closes this phase>",
  "categories": [
    {{
      "name": "<category 1 name>",
      "items": [
        "<concrete check 1>",
        "<concrete check 2>",
        "..."
      ]
    }},
    "..."
  ]
}}
"""


def _strip_fences(raw: str) -> str:
    """Trim ```json / ``` fences if Sonnet included them despite instructions."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?\s*```\s*$", "", raw)
    return raw


def generate_template(client, phase_name: str, precon_str: str) -> tuple[dict, object]:
    """One Sonnet call per phase. Returns (template_dict, usage)."""
    prompt = PROMPT_TEMPLATE.format(
        phase_name=phase_name,
        precon_template=precon_str,
    )
    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _strip_fences(response.content[0].text)
    template = json.loads(raw)
    if template.get("phase_name") != phase_name:
        # Tolerate whitespace/case variation; force the canonical spelling
        template["phase_name"] = phase_name
    return template, response.usage


def _cost_usd(usage) -> float:
    inp = (usage.input_tokens / 1_000_000) * SONNET_INPUT_COST
    out = (usage.output_tokens / 1_000_000) * SONNET_OUTPUT_COST
    return inp + out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--phase", default=None,
        help="Generate just one phase by name (case-insensitive substring match).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print summary, don't write files.",
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help="Don't regenerate templates that already exist on disk.",
    )
    args = parser.parse_args()

    precon_path = TEMPLATE_DIR / "precon.json"
    precon_str = precon_path.read_text()

    if args.phase:
        # Substring match against canonical phase names
        targets = [
            n for n in CHECKLIST_PHASE_NAMES
            if args.phase.lower() in n.lower() and n != "Precon"
        ]
        if not targets:
            print(f"No phase matched: {args.phase!r}")
            sys.exit(1)
    else:
        targets = [n for n in CHECKLIST_PHASE_NAMES if n != "Precon"]

    if args.skip_existing:
        before = len(targets)
        targets = [n for n in targets if not (TEMPLATE_DIR / f"{slugify(n)}.json").exists()]
        skipped = before - len(targets)
        if skipped:
            print(f"Skipping {skipped} existing template(s).")

    if not targets:
        print("Nothing to do.")
        return

    client = make_client()
    total_cost = 0.0

    print(f"Generating {len(targets)} template(s) via {WRITER_MODEL}...\n")
    for i, phase in enumerate(targets, 1):
        out_path = TEMPLATE_DIR / f"{slugify(phase)}.json"
        print(f"  [{i}/{len(targets)}] {phase}...", end=" ", flush=True)

        try:
            template, usage = generate_template(client, phase, precon_str)
        except Exception as e:
            print(f"FAILED: {type(e).__name__}: {e}")
            continue

        cost = _cost_usd(usage)
        total_cost += cost

        cat_count = len(template.get("categories", []))
        item_count = sum(len(c.get("items", [])) for c in template.get("categories", []))
        print(f"{cat_count} cats / {item_count} items / ${cost:.4f}")

        if not args.dry_run:
            out_path.write_text(json.dumps(template, indent=2) + "\n")

    print(f"\nTotal cost: ${total_cost:.4f}")
    if args.dry_run:
        print("(dry-run — no files written)")


if __name__ == "__main__":
    main()
