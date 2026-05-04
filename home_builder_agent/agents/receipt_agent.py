"""receipt_agent.py — Receipt photo → Cost Tracker update.

Chad photographs a receipt on his phone, transfers the image to the Mac,
and runs `hb-receipt /path/to/receipt.jpg`. Claude Vision extracts the
vendor, date, total, and a construction-section guess. Chad confirms the
job and section, then the agent:

  1. Bumps the Actual ($) subtotal in the matching Cost Tracker sheet.
  2. Uploads the receipt image to Drive under
     Finance Office / Receipts / {project_name}/ with a clean filename.

Supported formats: JPEG (.jpg, .jpeg) and PNG (.png).
HEIC images (iPhone default) must be exported as JPEG first.

CLI: hb-receipt /path/to/receipt.jpg
Cost: ~$0.003 per run (one Sonnet Vision call).
"""

import argparse
import base64
import io
import os
import re

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from home_builder_agent.config import (
    FINANCE_FOLDER_PATH,
    FINANCE_PROJECT_NAME,
    FINANCE_RECEIPTS_DIR,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import make_client, sonnet_cost
from home_builder_agent.classifiers.invoice import extract_receipt_data
from home_builder_agent.integrations import drive as drive_int
from home_builder_agent.integrations.finance import (
    update_cost_tracker_actual,
    add_actuals_log_row,
)

# ─────────────────────────────────────────────
# Construction cost sections — must match section headers in the sheet exactly
# ─────────────────────────────────────────────

COST_SECTIONS = [
    "Permits & Fees",
    "Site Work",
    "Footings & Foundation",
    "Concrete Slabs",
    "Framing",
    "Structural Steel",
    "Windows & Exterior Doors",
    "Roofing & Gutters",
    "Mechanical Systems",
    "Exterior Veneer",
    "Insulation & Drywall",
    "Cabinets & Countertops",
    "Fireplace / Hearth / Mantle",
    "Interior Trim & Stairs",
    "Flooring",
    "Wall Coverings & Paint",
    "Appliance Package",
    "Landscaping & Site Improvements",
    "Clean-Up",
    "General Conditions",
    "Contingency",
]


# ─────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────

def _list_active_projects(drive_svc, creds):
    """Walk FINANCE_FOLDER_PATH and return all Cost Tracker spreadsheets.

    Returns a list of dicts: [{"name": project_name, "id": sheet_id}, ...]
    where `name` is the project name with " — Cost Tracker" stripped.
    Falls back to [{"name": FINANCE_PROJECT_NAME, "id": None}] if the folder
    doesn't exist or contains no trackers.
    """
    try:
        finance_folder_id = drive_int.find_folder_by_path(drive_svc, FINANCE_FOLDER_PATH)
    except FileNotFoundError:
        return [{"name": FINANCE_PROJECT_NAME, "id": None}]

    files = drive_int.find_files_by_name_pattern(
        drive_svc,
        "Cost Tracker",
        finance_folder_id,
        mime_type="application/vnd.google-apps.spreadsheet",
    )

    projects = []
    for f in files:
        raw_name = f["name"]
        # Strip the trailing " — Cost Tracker" (em-dash variant)
        project_name = re.sub(r"\s+[—–-]+\s+Cost Tracker$", "", raw_name).strip()
        projects.append({"name": project_name, "id": f["id"]})

    if not projects:
        return [{"name": FINANCE_PROJECT_NAME, "id": None}]

    return projects


def _find_or_create_subfolder(drive_svc, parent_id, name):
    """Return ID of a subfolder named `name` under `parent_id`, creating it if needed."""
    query = (
        f"name='{name}' "
        "and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents "
        "and trashed=false"
    )
    results = drive_svc.files().list(q=query, fields="files(id)").execute()
    folders = results.get("files", [])
    if folders:
        return folders[0]["id"]

    folder = drive_svc.files().create(
        body={
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        },
        fields="id",
    ).execute()
    return folder["id"]


def _save_receipt_to_drive(drive_svc, photo_path, project_name, vendor, date_str):
    """Upload the receipt image into Finance Office / Receipts / {project_name}/.

    Filename format: {date}-{vendor_clean}.{ext}
    where vendor_clean strips non-alphanumeric characters and collapses runs
    of hyphens/spaces into single hyphens.

    Returns the webViewLink of the uploaded file.
    """
    # Walk to the Finance Office folder
    finance_folder_id = drive_int.find_folder_by_path(drive_svc, FINANCE_FOLDER_PATH)

    # Find or create "Receipts" subfolder
    receipts_folder_id = _find_or_create_subfolder(drive_svc, finance_folder_id, FINANCE_RECEIPTS_DIR)

    # Find or create project subfolder under Receipts
    project_folder_id = _find_or_create_subfolder(drive_svc, receipts_folder_id, project_name)

    # Build a clean filename
    vendor_clean = re.sub(r"[^A-Za-z0-9]+", "-", vendor).strip("-")
    ext = os.path.splitext(photo_path)[1].lstrip(".").lower()
    if ext in ("jpg", "jpeg"):
        ext = "jpg"
    filename = f"{date_str}-{vendor_clean}.{ext}"

    # Determine MIME type
    mime_type = "image/jpeg" if ext == "jpg" else "image/png"

    with open(photo_path, "rb") as f:
        media = MediaIoBaseUpload(io.BytesIO(f.read()), mimetype=mime_type)

    result = drive_svc.files().create(
        body={
            "name": filename,
            "parents": [project_folder_id],
        },
        media_body=media,
        fields="id, webViewLink",
    ).execute()

    return result.get("webViewLink", "")


def _prompt_section_choice(category_guess):
    """Print the 20 cost sections and return the chosen section name.

    Marks the guessed section with [suggested].  Loops until valid input.
    """
    print("\nWhich cost section?")
    guess_lower = (category_guess or "").strip().lower()
    col_width = 26
    for i, section in enumerate(COST_SECTIONS, start=1):
        tag = "  [suggested]" if section.lower() == guess_lower else ""
        label = f"{i:>2}. {section}{tag}"
        print(f"  {label}")

    while True:
        raw = input("\n  Enter section number: ").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(COST_SECTIONS):
                return COST_SECTIONS[idx]
        print(f"  Please enter a number between 1 and {len(COST_SECTIONS)}.")


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main():
    """CLI entry point for hb-receipt."""

    # ── 1. Parse CLI arg ──────────────────────────────────────────────────
    parser = argparse.ArgumentParser(
        description="Read a receipt photo and update the Cost Tracker sheet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Supported formats: .jpg, .jpeg, .png  (convert HEIC to JPEG first)",
    )
    parser.add_argument(
        "photo",
        metavar="PHOTO",
        help="Path to the receipt image (.jpg, .jpeg, or .png)",
    )
    args = parser.parse_args()
    photo_path = args.photo

    # Validate file exists
    if not os.path.isfile(photo_path):
        print(f"Error: file not found — {photo_path}")
        raise SystemExit(1)

    # Validate extension
    ext_lower = os.path.splitext(photo_path)[1].lower()
    if ext_lower == ".heic":
        print(
            "HEIC format detected — please convert to JPEG first "
            "(use Photos app > Export, choose JPEG)."
        )
        raise SystemExit(1)
    if ext_lower not in (".jpg", ".jpeg", ".png"):
        print(
            f"Unsupported format '{ext_lower}'. "
            "Please supply a .jpg, .jpeg, or .png file."
        )
        raise SystemExit(1)

    # ── 2. Read and base64-encode the image ───────────────────────────────
    with open(photo_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    media_type = (
        "image/jpeg" if ext_lower in (".jpg", ".jpeg") else "image/png"
    )

    # ── 3. Extract receipt data via Claude Vision ─────────────────────────
    print("Reading receipt with Claude Vision...")
    client = make_client()

    try:
        extracted, usage = extract_receipt_data(client, image_b64, media_type)
    except Exception as exc:
        print(f"\nError reading receipt: {exc}")
        raise SystemExit(1)

    vendor = extracted.get("vendor", "Unknown Vendor")
    date_str = extracted.get("date", "")
    total = extracted.get("total", 0.0)
    line_items = extracted.get("line_items", [])
    category_guess = extracted.get("category_guess", "")

    item_count = len(line_items) if isinstance(line_items, list) else 0
    item_label = f"{item_count} line item{'s' if item_count != 1 else ''}"

    print()
    print("┌─────────────────────────────────────┐")
    print("│  RECEIPT EXTRACTED                  │")
    print(f"│  Vendor:  {vendor:<26}│")
    print(f"│  Date:    {date_str:<26}│")
    print(f"│  Total:   ${total:<25.2f}│")
    print(f"│  Items:   {item_label:<26}│")
    print(f"│  Guess:   {category_guess:<26}│")
    print("└─────────────────────────────────────┘")

    # ── 4. Authenticate with Google ───────────────────────────────────────
    print("\nAuthenticating with Google...")
    try:
        creds = get_credentials()
        drive_svc = build("drive", "v3", credentials=creds)
        sheets_svc = build("sheets", "v4", credentials=creds)
    except Exception as exc:
        print(f"\nError authenticating with Google: {exc}")
        raise SystemExit(1)

    # ── 5. Find active projects ───────────────────────────────────────────
    try:
        projects = _list_active_projects(drive_svc, creds)
    except Exception as exc:
        print(f"\nWarning: could not list Cost Tracker sheets — {exc}")
        projects = [{"name": FINANCE_PROJECT_NAME, "id": None}]

    # ── 6. Ask which job ──────────────────────────────────────────────────
    if len(projects) == 1:
        selected_project = projects[0]
        print(f"\nSelect job:")
        print(f"  1. {selected_project['name']}  ← auto-selected (only project)")
    else:
        print("\nSelect job:")
        for i, p in enumerate(projects, start=1):
            print(f"  {i}. {p['name']}")
        while True:
            raw = input("\n  Enter project number: ").strip()
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(projects):
                    selected_project = projects[idx]
                    break
            print(f"  Please enter a number between 1 and {len(projects)}.")

    project_name = selected_project["name"]
    sheet_id = selected_project["id"]

    # ── 7. Ask which cost section ─────────────────────────────────────────
    section_name = _prompt_section_choice(category_guess)

    # ── 8. Confirm total and update mode ─────────────────────────────────
    print(f'\nUpdate Actual ($) for section "{section_name}"?')
    print(f"  Receipt total:  ${total:.2f}")
    add_raw = input("  Add to existing actual? [y/n]: ").strip().lower()
    add_to_existing = add_raw != "n"

    # ── 9. Update Cost Tracker + log to Actuals Log ───────────────────────
    if sheet_id is None:
        print(
            "\nWarning: no Cost Tracker sheet ID found — skipping sheet update. "
            "Run hb-finance first to create the Cost Tracker for this project."
        )
        new_total = total
        sheet_row = None
    else:
        try:
            amount_to_add = total if add_to_existing else total
            sheet_row, new_total = update_cost_tracker_actual(
                sheets_svc, sheet_id, section_name, amount_to_add,
            )
        except Exception as exc:
            print(f"\nError updating Cost Tracker: {exc}")
            raise SystemExit(1)

    # ── 10. Save receipt to Drive ─────────────────────────────────────────
    try:
        web_link = _save_receipt_to_drive(
            drive_svc, photo_path, project_name, vendor, date_str
        )
    except Exception as exc:
        print(f"\nWarning: receipt upload failed — {exc}")
        web_link = None

    # ── 11. Log to Actuals Log tab ────────────────────────────────────────
    if sheet_id:
        try:
            add_actuals_log_row(sheets_svc, sheet_id, {
                "date": date_str or "unknown",
                "vendor": vendor,
                "amount": total,
                "section": section_name,
                "receipt_link": web_link or "",
                "notes": f"Logged via hb-receipt",
            })
        except Exception:
            pass  # log failure should never block the success path

    # ── 12. Print success summary ─────────────────────────────────────────
    cost = sonnet_cost(usage)
    print()
    print(f"✅  Cost Tracker updated — {section_name} actual: ${new_total:.2f}")
    if web_link:
        # Build a human-readable path label for display
        receipts_dir = FINANCE_RECEIPTS_DIR
        vendor_clean = re.sub(r"[^A-Za-z0-9]+", "-", vendor).strip("-")
        ext_display = "jpg" if ext_lower in (".jpg", ".jpeg") else "png"
        filename_display = f"{date_str}-{vendor_clean}.{ext_display}"
        print(
            f"✅  Receipt saved: Finance Office / {receipts_dir} / "
            f"{project_name} / {filename_display}"
        )
    print()
    print(f"Cost: ${cost['total']:.4f}  (Sonnet vision call)")


if __name__ == "__main__":
    main()
