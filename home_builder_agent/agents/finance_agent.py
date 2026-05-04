"""finance_agent.py — Chad's Finance Office agent.

Finds or creates the "Chad's Finance Office" folder in Google Drive,
then finds or creates the Cost Tracker sheet for the active project.

On first run:
  - Creates "Chad's Finance Office" folder under Home Builder Agent V.1
  - Creates "Whitfield Residence — Cost Tracker" sheet with all 15+ sections,
    pre-populated with the known allowance budgets from the project spec
  - Writes a Finance Summary tab with KPI snapshot

On subsequent runs (sheet already exists):
  - Reads the current Budget / Actual / Billed values
  - Re-writes the Finance Summary tab so it stays current
  - Prints a finance summary to the terminal

CLI: hb-finance
"""

from googleapiclient.discovery import build

from home_builder_agent.config import FINANCE_FOLDER_PATH, FINANCE_PROJECT_NAME
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.integrations import drive
from home_builder_agent.integrations.finance import (
    COST_SECTIONS,
    create_cost_tracker_sheet,
    find_cost_tracker,
    read_finance_summary,
    write_finance_summary,
)


def _find_or_create_finance_folder(drive_svc):
    """Walk FINANCE_FOLDER_PATH, creating the last segment if it doesn't exist."""
    # Walk all but the final segment
    parent_id = drive.find_folder_by_path(drive_svc, FINANCE_FOLDER_PATH[:-1])
    folder_name = FINANCE_FOLDER_PATH[-1]

    # Check if it already exists
    # Apostrophes in folder names must be escaped as \' in Drive query strings
    escaped_name = folder_name.replace("'", r"\'")
    query = (
        f"name='{escaped_name}' "
        "and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents "
        "and trashed=false"
    )
    existing = drive_svc.files().list(
        q=query, fields="files(id,name)"
    ).execute().get("files", [])

    if existing:
        return existing[0]["id"]

    # Create it
    folder = drive_svc.files().create(
        body={
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        },
        fields="id",
    ).execute()
    print(f"  Created folder: {' / '.join(FINANCE_FOLDER_PATH)}")
    return folder["id"]


def main():
    print("Authenticating with Google...")
    creds = get_credentials()

    drive_svc  = build("drive",  "v3", credentials=creds)
    sheets_svc = build("sheets", "v4", credentials=creds)

    # ── 1. Finance Office folder ───────────────────────────────────────
    print(f"\nLocating Chad's Finance Office folder...")
    folder_id = _find_or_create_finance_folder(drive_svc)
    print(f"  Folder ready.")

    # ── 2. Cost Tracker sheet ──────────────────────────────────────────
    project_name = FINANCE_PROJECT_NAME
    print(f"\nLooking for Cost Tracker: {project_name}...")

    existing = find_cost_tracker(drive_svc, folder_id, project_name)

    if existing:
        sheet_id  = existing["id"]
        sheet_url = existing["webViewLink"]
        print(f"  Found existing sheet: {existing['name']}")
        created = False
    else:
        print(f"  Not found — creating Cost Tracker...")
        result    = create_cost_tracker_sheet(creds, project_name, folder_id)
        sheet_id  = result["id"]
        sheet_url = result["webViewLink"]
        created = True
        print(f"  Created: {project_name} — Cost Tracker")

    # ── 3. Read finance summary ────────────────────────────────────────
    print("\nReading financial data...")
    summary = read_finance_summary(sheets_svc, sheet_id)

    # ── 4. Write / refresh Finance Summary tab ─────────────────────────
    print("Updating Finance Summary tab...")
    write_finance_summary(sheets_svc, sheet_id, summary, project_name)

    # ── 5. Terminal report ─────────────────────────────────────────────
    def _fmt(v):
        try:
            return f"${float(v):,.0f}"
        except (ValueError, TypeError):
            return "—"

    print("\n" + "=" * 60)
    print("CHAD'S FINANCE OFFICE — " + project_name.upper())
    print("=" * 60)
    print(f"  Contract Price (Budget):  {_fmt(summary['contract_budget'])}")
    print(f"  Actual Spent (to Date):   {_fmt(summary['contract_actual'])}")
    print(f"  Budget Remaining:         {_fmt(summary['budget_remaining'])}")
    pct = summary.get("pct_spent", 0)
    print(f"  % of Budget Used:         {pct:.1f}%")
    print(f"  Total Billed to Client:   {_fmt(summary['contract_billed'])}")

    if summary["contract_actual"] > summary["contract_budget"]:
        print("\n  ⚠️  OVER BUDGET — actuals exceed contract price!")
    elif summary["contract_actual"] == 0:
        print("\n  ℹ️  No actuals entered yet.")
        print("     Open the Cost Tracker tab and fill in Actual ($) as costs come in.")

    if created:
        print(f"\n  ✅  New sheet created with {len(COST_SECTIONS)} sections")
        print(f"     and all known allowances pre-populated.")

    print(f"\nSheet:  {sheet_url}")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
