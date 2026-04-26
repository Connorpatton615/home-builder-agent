"""timeline_generator.py — Construction Timeline + Tracker Sheet generator.

Reads:
  - A project spec (Markdown in WORKSPACE/PROJECT SPECS/)
  - Baldwin County construction reference (knowledge base)
  - Baldwin County supplier research (knowledge base)
  - Chad communication rules (knowledge base)

Asks Claude (acting as Chad's senior PM) to generate a detailed construction
timeline grounded in Baldwin County code, climate, and verified luxury suppliers.

Outputs:
  - Polished Google Doc in GENERATED TIMELINES/, with TOC, hyperlinks,
    native checkbox bullets, 0.75" margins, 115% line spacing
  - 3-tab Google Sheet (Master Schedule | Action Items | Order Schedule)
    with status dropdowns, conditional row colors, date formats

Idempotent: re-running on the same spec archives prior versions to
GENERATED TIMELINES/ARCHIVE/ before creating new ones.

CLI:
  hb-timeline                            # use default spec (pelican_point.md)
  hb-timeline chads_lake_house.md        # specific spec
  hb-timeline --list                     # list available specs and exit
"""

import argparse
import json
import os
import re
import sys

import markdown

from home_builder_agent.config import (
    DEFAULT_SPEC_FILENAME,
    DRIVE_FOLDER_PATH,
    PROJECT_SPECS_DIR,
    TIMELINE_MAX_TOKENS,
    WORKSPACE,
    WRITER_MODEL,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import (
    make_client,
    print_sonnet_cost_block,
    sonnet_cost,
)
from home_builder_agent.core.knowledge_base import load_full_kb
from home_builder_agent.integrations import docs, drive, sheets


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def parse_args():
    """Parse CLI args. Resolution: positional > SPEC_FILE env > default."""
    parser = argparse.ArgumentParser(
        description="Generate a construction timeline from a project spec.",
        epilog=(
            "Examples:\n"
            "  hb-timeline                            (use default spec)\n"
            "  hb-timeline chads_lake_house.md        (specific spec)\n"
            "  hb-timeline --list                     (list available specs)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "spec", nargs="?", default=None,
        help="Spec filename inside PROJECT SPECS/. "
             "If omitted, uses SPEC_FILE env var or default.",
    )
    parser.add_argument(
        "-l", "--list", action="store_true",
        help="List available specs in PROJECT SPECS/ and exit.",
    )
    return parser.parse_args()


def resolve_spec_filename(cli_spec):
    if cli_spec:
        return cli_spec
    return os.environ.get("SPEC_FILE") or DEFAULT_SPEC_FILENAME


def list_available_specs():
    full_dir = os.path.join(WORKSPACE, PROJECT_SPECS_DIR)
    if not os.path.isdir(full_dir):
        print(f"PROJECT SPECS folder not found at {full_dir}")
        return
    files = sorted(f for f in os.listdir(full_dir) if f.endswith(".md"))
    if not files:
        print(f"No .md specs found in {full_dir}")
        return
    print("Available specs in PROJECT SPECS/:")
    for f in files:
        size = os.path.getsize(os.path.join(full_dir, f))
        marker = " (default)" if f == DEFAULT_SPEC_FILENAME else ""
        print(f"  {f}  ({size:,} bytes){marker}")


# ---------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------

def extract_json_block(text):
    """Find ```json ... ``` block in Claude output. Returns dict or None."""
    pattern = r"```json\s*\n(.*?)\n```"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as e:
        print(f"  WARNING: JSON block found but failed to parse: {e}")
        return None


def strip_json_block(text):
    """Remove the ```json ... ``` block from text so it doesn't render in the doc."""
    pattern = r"```json\s*\n.*?\n```\s*"
    return re.sub(pattern, "", text, flags=re.DOTALL).rstrip()


# ---------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------

def build_system_prompt(construction_ref, supplier_ref, comm_rules):
    """Build the system prompt as content blocks (cacheable, though caching
    is currently disabled — see project_active_vs_ondemand memory).

    Returns a list of content blocks compatible with messages.create(system=...).
    """
    role_and_principles = """You are a senior construction project manager with 30 years of
experience building $1M+ luxury custom homes in Baldwin County, Alabama.
You serve Chad's Custom Homes — a builder who works across both coastal
Baldwin (Fairhope, Gulf Shores, Orange Beach, Daphne, Point Clear) and inland
Baldwin (Foley, Loxley, Robertsdale, Spanish Fort).

You generate detailed, realistic construction timelines that are GROUNDED in
the actual Baldwin County reality — code editions in force per jurisdiction,
wind/flood zone requirements, FORTIFIED program mandates, hurricane-season
scheduling realities, regional architectural vocabulary (Coastal/Lowcountry,
Plantation Revival, Acadian / French Country), and verified luxury-tier
suppliers and trade contractors.

You speak the language of working Baldwin builders — direct, no fluff, useful.
You're honest about risks, pragmatic about scheduling, concrete about when
materials must be ordered, and specific about which suppliers serve which tier.

# CRITICAL OPERATING PRINCIPLES

1. **Use the verified Baldwin reference data below.** When you cite codes,
   permit offices, supplier names, lead times, or seasonal constraints,
   cite the ACTUAL Baldwin specifics from the references — not generic
   construction industry averages.

2. **Distinguish coastal vs. inland.** A Fairhope build under FORTIFIED Gold
   mandate with a V-zone pile foundation is a different animal than a Loxley
   inland slab-on-grade build. Read the project spec carefully for site
   address and apply the right regulatory regime.

3. **Name verified suppliers by name** when the research below has a
   [LUXURY] entry in the relevant category. When the research flags a
   category as needing Chad's input, say so — never invent a supplier name.

4. **Account for hurricane season** (June 1 – Nov 30) when scheduling exterior
   trades, concrete pours, paint, and inspections. Coastal projects need to
   weatherize before peak season if at all possible.

5. **Account for permit office reality** — different cities have different
   code editions in force, different FORTIFIED requirements, different
   freeboard rules above FEMA minimum, and different review timelines.

6. **Output format**: Use Markdown with clear headers, bulleted lists, and
   tables where they aid scanability. The output goes to a Google Doc Chad
   reads — make it scannable, not a wall of text.

7. **"Future provision" features must be explicitly disambiguated.** When
   the spec mentions a feature with phrases like "future install," "pre-wire
   only," "rough-in for future," "framed for future," etc. (e.g., elevator
   shaft, generator pad, future pool equipment, future AV system, future
   solar inverter), state in the timeline UNAMBIGUOUSLY: "[Feature] is NOT
   being installed in this build. Only the [framing / rough-in / pre-wire]
   is included now to enable future install without rework." Never let a
   reader wonder whether a feature is in scope."""

    construction_block = (
        "# BALDWIN COUNTY CONSTRUCTION REFERENCE\n\n"
        "The following is your authoritative reference on Baldwin County code, "
        "climate, permitting, and construction realities. Treat it as ground "
        "truth.\n\n"
        "<construction_reference>\n"
        + construction_ref
        + "\n</construction_reference>"
    )

    supplier_block = (
        "# BALDWIN COUNTY LUXURY SUPPLIER KNOWLEDGE BASE\n\n"
        "The following is your reference on suppliers and trade contractors in "
        "the Baldwin County / Mobile / Pensacola luxury market. Tier flags: "
        "[LUXURY] = verified $1M+ tier, [MID-MARKET] = broader market, "
        "[UNCERTAIN] = positioning unclear from public evidence.\n\n"
        "When recommending suppliers, prefer [LUXURY]-tier names. When the "
        "research notes a gap (no luxury candidate verified), explicitly tell "
        "Chad this is a category he needs to weigh in on — do not invent a "
        "supplier.\n\n"
        "<supplier_knowledge_base>\n"
        + supplier_ref
        + "\n</supplier_knowledge_base>"
    )

    comm_block = (
        "# CHAD COMMUNICATION RULES (DISC: D / S)\n\n"
        "Chad is the builder reading every document you produce. He is a "
        "project-focused operator with a Dominant + Steady DISC profile. Apply "
        "the communication rules below to ALL prose you write — phase "
        "descriptions, risk callouts, action items, the Critical Path, status "
        "notes.\n\n"
        "The comprehensive structure (Phase Overview, per-phase sections, "
        "Critical Path, Master Ordering, Regulatory Checklist) stays as-is — "
        "it's the format he expects. The VOICE within each section is what "
        "these rules govern.\n\n"
        "Key principles to internalize:\n"
        "- Lead with status framing (on track / delayed / ready / blocked)\n"
        "- 1-4 sentence prose blocks; no walls of text\n"
        "- Every risk paired with a recommended mitigation — never list a risk alone\n"
        "- Direct, calm, no hype, no decorative emojis (functional status emojis "
        "🟢🟡🔴⚪ ARE encouraged for at-a-glance scannability)\n"
        "- \"If Chad has to read twice or think hard, the message failed.\"\n\n"
        "<chad_communication_rules>\n"
        + comm_rules
        + "\n</chad_communication_rules>"
    )

    return [
        {"type": "text", "text": role_and_principles},
        {"type": "text", "text": construction_block},
        {"type": "text", "text": supplier_block},
        {"type": "text", "text": comm_block},
    ]


def build_user_prompt(spec_content):
    """Build the user-turn prompt with the project spec embedded."""
    return f"""Below is a project specification for one of Chad's custom home
builds. Generate a detailed construction timeline organized by phase.

# REQUIRED DOCUMENT STRUCTURE

Output the document in EXACTLY this order:

1. **Document title** as an H1 heading (e.g., `# Pelican Point Residence — Construction Timeline`)

2. **Project info block** as a brief intro: location, target start, target completion, FORTIFIED tier if applicable, key constraints

3. **`[TOC]` literally on its own line** — this auto-generates a hyperlinked
   table of contents that lets Chad click any heading to jump to it.

4. **Phase Overview Table** — a Markdown table with columns: Phase #, Name,
   Weeks, Calendar, Status. Status column should say "Pending" for now —
   Chad updates as work progresses.

5. **Structured JSON block** — output here, BEFORE the prose body, so it
   never gets truncated. A single ```json ... ``` block (markdown-fenced)
   containing structured data that the tracking spreadsheet will be built
   from. Schema below in section 11. Fill in EVERY phase, EVERY checkbox-task,
   EVERY material order. Status fields default to "Not Started"; Chad updates
   them in the spreadsheet later. Make sure target_date / target_start fields
   are real dates calculated from the project's start date in the spec, not
   relative ("Week 4"). The JSON must be valid and parseable.

6. **Phase-by-phase body** — one `## Phase N: Name` heading per phase. For
   each phase include:
   - Brief description (1-2 sentences)
   - Estimated duration (weeks)
   - **Action Items as checkboxes** — use this exact markdown format for
     each actionable item Chad needs to complete:
     `- [ ] Task description here`
     These will become real interactive checkboxes Chad can click.
   - Key materials to order, when, and from which supplier (name verified
     suppliers from the knowledge base; make supplier names hyperlinks
     `[Supplier Name](https://supplier-url)` where the URL is in the research)
   - Dependencies on previous phases
   - Common risks and mitigation for this Baldwin site

7. **Critical Path Summary** — priority-ranked list of the items most likely
   to delay completion, each with a clear deadline. Make permit-office links
   and code-reference links clickable.

8. **Master Ordering Schedule** — chronological "by week X, order Y from Z"
   table with supplier hyperlinks where available.

9. **Regulatory Checklist** — code edition, wind design speed, flood zone,
   FORTIFIED tier, permit office (with hyperlink), key inspections in order.

10. **(JSON block referenced in section 5 above — schema below in section 11.)**

11. **JSON SCHEMA reference for the JSON block in section 5:**

    ```json
    {{
      "project": {{
        "name": "<string>",
        "location": "<city, state, zip>",
        "target_start": "YYYY-MM-DD",
        "target_completion": "YYYY-MM-DD",
        "fortified_tier": "<None|Roof|Silver|Gold>",
        "wind_speed_mph": <int>,
        "flood_zone": "<e.g. AE-12, V14, X>"
      }},
      "phases": [
        {{
          "number": <int>,
          "name": "<string>",
          "duration_weeks": <int|float>,
          "target_start": "YYYY-MM-DD",
          "target_end": "YYYY-MM-DD",
          "dependencies": ["<phase name>", ...],
          "status": "Not Started"
        }}
      ],
      "tasks": [
        {{
          "phase": <int>,
          "description": "<actionable task>",
          "target_date": "YYYY-MM-DD",
          "owner": "Chad",
          "notes": "<optional, can be empty>"
        }}
      ],
      "orders": [
        {{
          "item": "<material/equipment>",
          "supplier": "<verified supplier name>",
          "supplier_url": "<https://...>",
          "order_by_date": "YYYY-MM-DD",
          "lead_time_weeks": <int>,
          "notes": "<consequence-if-missed or special instruction>"
        }}
      ]
    }}
    ```

# HYPERLINK GUIDANCE

Use Markdown hyperlink syntax `[text](url)` aggressively where you have URLs:
- Permit office contacts (Fairhope Building, Gulf Shores Building, etc.)
- Code references (FORTIFIED, IRC, IBHS)
- Supplier names (Marvin via Dale of Alabama, Acme Brick Loxley, etc.)
- FEMA flood map portal
- Alabama Power, IBHS FORTIFIED
The research files in your context contain DOZENS of URLs — surface them.

# CHECKBOX GUIDANCE

The `- [ ] ...` syntax MUST be used for action items Chad will physically
check off. Examples of good checkbox items:
- [ ] Submit Pella window order to Dale of Alabama
- [ ] Schedule footing inspection with Fairhope Building
- [ ] Pour foundation walls (weather permitting)

Avoid putting non-actionable bullets in checkbox format. Reserve them for
real Chad tasks. Aim for 4-8 checkboxes per phase.

PROJECT SPECIFICATION:
{spec_content}"""


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():
    args = parse_args()
    if args.list:
        list_available_specs()
        return

    spec_filename = resolve_spec_filename(args.spec)
    spec_path = os.path.join(WORKSPACE, PROJECT_SPECS_DIR, spec_filename)
    if not os.path.exists(spec_path):
        print(f"ERROR: Spec file not found: {spec_path}")
        print("Run `hb-timeline --list` to see available specs.")
        sys.exit(1)

    # 1. Anthropic client
    client = make_client()

    # 2. Knowledge base
    print("Loading Baldwin County knowledge base...")
    construction_ref, supplier_ref, comm_rules = load_full_kb()

    # 3. Project spec
    print(f"\nReading spec: {spec_filename}")
    with open(spec_path) as f:
        spec_content = f.read()
    print(f"  ({len(spec_content):,} characters)")

    # 4. Build prompts
    system_prompt = build_system_prompt(construction_ref, supplier_ref, comm_rules)
    user_prompt = build_user_prompt(spec_content)
    total_system_chars = sum(len(b["text"]) for b in system_prompt)
    print(f"\nSystem prompt: {len(system_prompt)} blocks, "
          f"{total_system_chars:,} characters "
          f"(~{total_system_chars // 4:,} tokens)")
    print(f"User prompt:   {len(user_prompt):,} characters")

    # 5. Stream from Claude (long generations exceed 10-min non-streaming SDK timeout)
    print(f"\nAsking Claude ({WRITER_MODEL}) to generate the timeline...")
    print("  (streaming — dots show progress, ~2-4 minutes total)")
    print("  ", end="", flush=True)

    chunk_count = 0
    with client.messages.stream(
        model=WRITER_MODEL,
        max_tokens=TIMELINE_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        for _ in stream.text_stream:
            chunk_count += 1
            if chunk_count % 50 == 0:
                print(".", end="", flush=True)
        response = stream.get_final_message()

    print()
    full_response = response.content[0].text
    print(f"  Generated {response.usage.output_tokens:,} output tokens.")

    # 6a. Extract structured JSON
    print("\nExtracting structured data for tracker sheet...")
    project_data = extract_json_block(full_response)
    if project_data:
        n_phases = len(project_data.get("phases", []))
        n_tasks = len(project_data.get("tasks", []))
        n_orders = len(project_data.get("orders", []))
        print(f"  Parsed: {n_phases} phases, {n_tasks} tasks, {n_orders} orders")
    else:
        print("  No JSON block found — sheet will be skipped.")

    # 6b. Strip JSON from doc-bound markdown
    timeline_md = strip_json_block(full_response)

    # 6c. Markdown → HTML for Drive upload (TOC extension converts [TOC] marker)
    print("\nConverting markdown to HTML...")
    html_body = markdown.markdown(
        timeline_md,
        extensions=["tables", "fenced_code", "nl2br", "toc"],
    )
    full_html = (
        '<!DOCTYPE html><html><head>'
        '<meta charset="utf-8"><title>Construction Timeline</title>'
        '</head><body>' + html_body + '</body></html>'
    )

    # 7. Drive: find folder, archive prior versions, upload
    print("\nAuthenticating with Google...")
    creds = get_credentials()
    drive_svc = drive.drive_service(creds)

    print(f"Finding folder: {' / '.join(DRIVE_FOLDER_PATH)}")
    folder_id = drive.find_folder_by_path(drive_svc, DRIVE_FOLDER_PATH)

    project_name = os.path.splitext(spec_filename)[0].replace("_", " ").title()
    doc_name = f"Timeline – {project_name}"
    sheet_name = f"Tracker – {project_name}"

    archive_folder_id = drive.ensure_archive_folder(drive_svc, folder_id)
    n_archived = (
        drive.archive_existing_artifact(
            drive_svc, doc_name, folder_id, archive_folder_id)
        + drive.archive_existing_artifact(
            drive_svc, sheet_name, folder_id, archive_folder_id)
    )
    if n_archived:
        print(f"Archived {n_archived} prior version(s) → ARCHIVE/")

    print(f"Uploading as Google Doc: {doc_name}")
    file = drive.upload_as_google_doc(drive_svc, full_html, doc_name, folder_id)

    # 7b. Doc formatting (margins, spacing, checkboxes). Best-effort — don't
    # lose the upload over a formatting failure.
    try:
        print("Applying document formatting (margins, spacing, checkboxes)...")
        docs.apply_doc_formatting(creds, file["id"])
    except Exception as e:
        print(f"  WARNING: Could not apply doc formatting: {e}")
        print("  (Doc was still uploaded; manual adjustment available.)")

    # 7c. Tracker sheet from structured JSON (if extraction succeeded)
    sheet_url = None
    if project_data:
        try:
            print(f"\nBuilding tracker sheet: {sheet_name}")
            sheet_file = sheets.build_tracker_sheet(
                creds, project_data, sheet_name, folder_id
            )
            sheet_url = sheet_file["webViewLink"]
            print(f"  Sheet ready at: {sheet_url}")

            # Apply visual formatting (best-effort)
            try:
                sheets_svc = sheets.sheets_service(creds)
                n = sheets.apply_visual_formatting(sheets_svc, sheet_file["id"])
                print(f"  Visual polish applied ({n} formatting requests).")
            except Exception as e:
                print(f"  NOTE: Visual polish skipped: {e}")
        except Exception as e:
            print(f"  WARNING: Could not build tracker sheet: {e}")
            print("  (Doc still uploaded; rerun to retry the sheet.)")

    # 8. Cost reporting
    cost = sonnet_cost(response.usage)

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print(f"Document: {doc_name}")
    print(f"  Doc URL:   {file['webViewLink']}")
    if sheet_url:
        print(f"  Sheet URL: {sheet_url}")
    print()
    print_sonnet_cost_block(cost)
    if cost["cache_read_tokens"]:
        # Estimate what cost would have been without caching
        from home_builder_agent.config import SONNET_INPUT_COST
        without_cache = (
            (cost["fresh_input_tokens"] + cost["cache_read_tokens"])
            * SONNET_INPUT_COST / 1_000_000
            + cost["output"]
        )
        savings = without_cache - cost["total"]
        print(f"  (Saved ~${savings:.4f} via prompt cache hit)")
    print()


if __name__ == "__main__":
    main()
