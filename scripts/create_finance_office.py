"""One-shot script to create:
  1. "Chad's Finance Office" folder in Home Building Agent V.1
  2. "Whitfield Residence — Cost Tracker" Google Sheet (3 tabs) inside it

Run once: python scripts/create_finance_office.py
Prints the folder ID and sheet ID at the end — paste them into config.py.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from googleapiclient.discovery import build
from home_builder_agent.core.auth import get_credentials

# ── Folder IDs ────────────────────────────────────────────────────────────────
HOME_BUILDER_V1_FOLDER_ID = "1NvXpPiTimkBOBDmkrXBBUJRBiOEClBx6"
FINANCE_OFFICE_NAME = "Chad's Finance Office"
COST_TRACKER_NAME = "Whitfield Residence — Cost Tracker"   # em-dash

# ── Contract total (for draw schedule %) ─────────────────────────────────────
CONTRACT_TOTAL = 524_000

# ── Section definitions (order matters — exactly as Chad's XLS) ──────────────
SECTIONS = [
    ("Permits & Fees", [
        "Building Permits", "Impact Fees", "Miscellaneous Permits",
        "Engineering", "City License Fee", "Surveying",
    ]),
    ("Site Work", [
        "Gravel for Stabilized Entry", "Silt Fence", "Land Clearing",
        "Demolition", "Excavation", "Fill Dirt",
    ]),
    ("Footings & Foundation", [
        "Footing Labor", "Footing Concrete", "House Slab/Block Fill Labor",
        "Fill Dirt for Slab", "Compaction Testing", "Termite Treatment",
        "Drain Labor", "Drain Material", "Block Labor", "Block Material",
        "Block Fill Concrete", "Block Mortar & Sand", "Concrete Pump",
        "Slab Concrete",
    ]),
    ("Concrete Slabs", [
        "Fill for Slab", "Slab Labor", "Slab Concrete", "Wire Mesh",
        "Poly Labor", "Poly Material", "Misc. Steel/Rebar",
    ]),
    ("Framing", [
        "Framing Package (incl. cornice)", "Framing Labor", "Deck Labor",
        "Deck Material", "Nail & Strapping",
    ]),
    ("Windows & Exterior Doors", [
        "Window & Ext Door Package", "Front Door Allowance", "Lock Sets",
        "Garage Doors", "Storm Shutters",
    ]),
    ("Roofing & Gutters", [
        "Roofing Labor", "Roofing Shingle Package", "Metal Roofing",
        "Flashing Labor", "Drip Edge", "Ridge Vent", "Roofing Vents", "Gutters",
    ]),
    ("Mechanical Systems", [
        "Plumbing Labor", "Plumbing Fixtures", "Gas Service", "Sewer Lines",
        "Water Lines", "Water Heater", "Water Tap & Meter",
        "HVAC Package L&M", "HVAC Vent Hoods & Fans L&M",
        "Electrical Labor", "Electrical Fixtures",
    ]),
    ("Exterior Veneer", [
        "Brick Labor", "Brick Material", "Brick Mortar", "Brick Sand",
        "Stone Labor", "Stone Material", "Stucco L&M",
        "Siding Labor", "Siding Material",
    ]),
    ("Insulation & Drywall", [
        "Insulation Batt (Walls)", "Insulation Blown (Ceilings)",
        "Spray Foam (Walls)", "Spray Foam (Ceilings)",
        "Drywall Labor", "Drywall Material",
    ]),
    ("Cabinets & Countertops", [
        "Cabinet Package (Kitchen)", "Cabinet Package (Vanities)",
        "Misc. Cabinets", "Countertops L&M", "Shelving",
    ]),
    ("Fireplace, Hearth & Mantle", [
        "Chimney Cap", "Paneling Labor", "Firebox & Gas Insert", "Mantle L&M",
    ]),
    ("Interior Trim & Stairs", [
        "Doors & Trim Labor", "Interior Door Package", "Door Hardware",
        "Interior Trim Material", "Stair System Labor", "Stair System Parts",
        "Beams & Columns", "Paneling Material", "Bathroom Hardware",
        "Frameless Shower Doors",
    ]),
    ("Flooring", [
        "Hardwood Material", "Hardwood Labor", "Tile Floor Material",
        "Tile Floor Labor", "Tile Backsplash Material", "Tile Backsplash Labor",
        "Primary Shower Tile Material", "Primary Shower Tile Labor", "Carpet L&M",
    ]),
    ("Wall Coverings & Paint", [
        "Interior Paint", "Exterior Paint", "Trim Paint", "Porches",
    ]),
    ("Appliance Package", [
        "Kitchen Appliances", "Outdoor Appliances", "Vent Hood Motor & Duct",
    ]),
    ("Landscaping & Site Improvements", [
        "Tractor Work/Final Grading", "Landscaping Package",
        "Piping for Downspouts L&M", "Concrete Driveway & Walks",
        "Mail Box", "Screening for Covered Decks L&M",
    ]),
    ("Clean-Up", [
        "Dumpster Fees", "Final Clean-Up Fee", "Pressure Washing",
    ]),
    ("General Conditions", [
        "Superintendent Fee", "Job Overhead", "Builders Risk Insurance",
        "Equipment Rental", "Misc. Expenses", "Portolet",
        "Common Labor", "Utilities", "Contingency",
    ]),
]

# ── Pre-populated budget values {(section_name, line_item): amount} ───────────
BUDGET_VALUES = {
    ("Windows & Exterior Doors", "Front Door Allowance"): 2200,
    ("Windows & Exterior Doors", "Lock Sets"): 500,
    ("Windows & Exterior Doors", "Garage Doors"): 3200,
    ("Mechanical Systems", "Plumbing Fixtures"): 4500,
    ("Mechanical Systems", "Water Heater"): 1200,
    ("Mechanical Systems", "Electrical Fixtures"): 6500,
    ("Cabinets & Countertops", "Countertops L&M"): 14000,
    ("Cabinets & Countertops", "Shelving"): 2400,
    ("Interior Trim & Stairs", "Bathroom Hardware"): 800,
    ("Interior Trim & Stairs", "Frameless Shower Doors"): 2000,
    ("Flooring", "Hardwood Material"): 13500,   # combined; put all on Material row
    ("Flooring", "Primary Shower Tile Material"): 2800,  # combined on Material row
    ("Flooring", "Carpet L&M"): 1440,
    ("Wall Coverings & Paint", "Interior Paint"): 8000,
    ("Appliance Package", "Kitchen Appliances"): 12000,
    ("Landscaping & Site Improvements", "Landscaping Package"): 4500,
}


def build_cost_sheet_rows():
    """Return (rows, section_total_row_indices, line_item_map).

    rows: list-of-lists ready to batchUpdate.
    section_total_row_indices: list of 1-based row numbers that are TOTAL rows.
    line_item_map: dict (section, item) -> 1-based row number for budget cell.
    section_header_rows: list of 1-based row numbers that are section headers.
    grand_total_row: 1-based row number of grand total.
    """
    rows = []
    # Row 1 = column headers
    rows.append(["Line Item", "Budget", "Actual", "Diff", "Billed", "Notes"])

    section_total_row_indices = []
    section_header_rows = []
    line_item_map = {}   # (section, item) -> 1-based row number
    section_total_cells = []   # B col cell refs for grand total SUM

    for section_name, items in SECTIONS:
        # Section header row
        section_header_rows.append(len(rows) + 1)   # 1-based
        rows.append([section_name, "", "", "", "", ""])

        item_row_start = len(rows) + 1  # first data row of this section
        for item in items:
            row_num = len(rows) + 1
            budget_val = BUDGET_VALUES.get((section_name, item), 0)
            # D column formula: =B{n}-C{n}
            diff_formula = f"=B{row_num}-C{row_num}"
            rows.append([item, budget_val, 0, diff_formula, 0, ""])
            line_item_map[(section_name, item)] = row_num

        item_row_end = len(rows)   # last data row of this section
        # Blank spacer
        rows.append(["", "", "", "", "", ""])
        # Total row
        total_row_num = len(rows) + 1
        section_total_row_indices.append(total_row_num)

        b_sum = f"=SUM(B{item_row_start}:B{item_row_end})"
        c_sum = f"=SUM(C{item_row_start}:C{item_row_end})"
        d_sum = f"=SUM(D{item_row_start}:D{item_row_end})"
        e_sum = f"=SUM(E{item_row_start}:E{item_row_end})"
        rows.append([f"{section_name} Total", b_sum, c_sum, d_sum, e_sum, ""])
        section_total_cells.append(total_row_num)

        # Blank spacer after total
        rows.append(["", "", "", "", "", ""])

    # Grand Total row
    grand_total_row = len(rows) + 1
    b_refs = "+".join([f"B{r}" for r in section_total_cells])
    c_refs = "+".join([f"C{r}" for r in section_total_cells])
    d_refs = "+".join([f"D{r}" for r in section_total_cells])
    e_refs = "+".join([f"E{r}" for r in section_total_cells])
    rows.append(["GRAND TOTAL", f"={b_refs}", f"={c_refs}", f"={d_refs}", f"={e_refs}", ""])

    return rows, section_total_row_indices, line_item_map, section_header_rows, grand_total_row


def build_draw_schedule_rows():
    """6-draw schedule pre-populated."""
    headers = ["Draw #", "Description", "Amount", "% of Contract",
               "Phase", "Target Date", "Requested", "Approved", "Funded", "Notes"]
    draws = [
        (1, "Foundation Complete",        0.15),
        (2, "Framing Complete",           0.15),
        (3, "Rough MEP Complete",         0.15),
        (4, "Drywall Complete",           0.20),
        (5, "Trim & Finish Complete",     0.20),
        (6, "Final Walkthrough/CO",       0.15),
    ]
    rows = [headers]
    for num, desc, pct in draws:
        amount = round(CONTRACT_TOTAL * pct)
        rows.append([num, desc, amount, f"{int(pct*100)}%",
                     "", "", False, False, False, ""])
    return rows


def build_change_order_rows():
    """20 blank rows + header."""
    headers = ["CO #", "Date", "Description", "Requested By",
               "Amount", "Owner Approved", "Impact on Schedule (days)", "Notes"]
    rows = [headers]
    for i in range(20):
        rows.append(["", "", "", "", "", False, "", ""])
    return rows


def create_folder(drive_svc, name, parent_id):
    """Create a folder inside parent and return its ID."""
    result = drive_svc.files().create(
        body={
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        },
        fields="id",
    ).execute()
    return result["id"]


def create_spreadsheet(sheets_svc, drive_svc, title, parent_folder_id):
    """Create a spreadsheet with 3 tabs and return its ID."""
    body = {
        "properties": {"title": title},
        "sheets": [
            {"properties": {"sheetId": 0, "title": "Cost Sheet",
                            "gridProperties": {"frozenRowCount": 1}}},
            {"properties": {"sheetId": 1, "title": "Draw Schedule",
                            "gridProperties": {"frozenRowCount": 1}}},
            {"properties": {"sheetId": 2, "title": "Change Orders",
                            "gridProperties": {"frozenRowCount": 1}}},
        ],
    }
    sheet = sheets_svc.spreadsheets().create(
        body=body, fields="spreadsheetId,spreadsheetUrl"
    ).execute()
    sheet_id = sheet["spreadsheetId"]

    # Move from root to Finance Office folder
    drive_svc.files().update(
        fileId=sheet_id,
        addParents=parent_folder_id,
        removeParents="root",
        fields="id, parents",
    ).execute()

    return sheet_id, sheet["spreadsheetUrl"]


def apply_cost_sheet_formatting(sheets_svc, sheet_id, section_header_rows,
                                 section_total_row_indices, grand_total_row,
                                 total_data_rows):
    """Apply all formatting to the Cost Sheet (sheet 0)."""

    NAVY       = {"red": 0.13, "green": 0.23, "blue": 0.40}
    NAVY_LIGHT = {"red": 0.87, "green": 0.91, "blue": 0.96}
    WHITE      = {"red": 1.00, "green": 1.00, "blue": 1.00}
    LIGHT_GRAY = {"red": 0.95, "green": 0.95, "blue": 0.95}

    reqs = []

    # ── Column widths ─────────────────────────────────────────────────────────
    widths = [280, 110, 110, 110, 110, 200]
    for i, px in enumerate(widths):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": 0, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": px}, "fields": "pixelSize",
        }})

    # ── Freeze row 1 ──────────────────────────────────────────────────────────
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount",
    }})

    # ── Column header row (row 1 = index 0) ───────────────────────────────────
    reqs.append({"repeatCell": {
        "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 6},
        "cell": {"userEnteredFormat": {
            "backgroundColor": NAVY,
            "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 10},
            "verticalAlignment": "MIDDLE",
            "horizontalAlignment": "CENTER",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,horizontalAlignment)",
    }})

    # ── Section header rows (navy bg, white bold) ─────────────────────────────
    for row_1based in section_header_rows:
        ri = row_1based - 1  # 0-indexed
        reqs.append({"repeatCell": {
            "range": {"sheetId": 0, "startRowIndex": ri, "endRowIndex": ri + 1,
                      "startColumnIndex": 0, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 10},
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
        }})

    # ── TOTAL rows (light navy, bold) ─────────────────────────────────────────
    for row_1based in section_total_row_indices:
        ri = row_1based - 1
        reqs.append({"repeatCell": {
            "range": {"sheetId": 0, "startRowIndex": ri, "endRowIndex": ri + 1,
                      "startColumnIndex": 0, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY_LIGHT,
                "textFormat": {"bold": True, "fontSize": 10},
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
        }})

    # ── Grand Total row (deep navy, white bold, font 12) ─────────────────────
    gri = grand_total_row - 1
    reqs.append({"repeatCell": {
        "range": {"sheetId": 0, "startRowIndex": gri, "endRowIndex": gri + 1,
                  "startColumnIndex": 0, "endColumnIndex": 6},
        "cell": {"userEnteredFormat": {
            "backgroundColor": NAVY,
            "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 12},
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
    }})

    # ── Currency format for B, C, D, E columns (data rows) ───────────────────
    reqs.append({"repeatCell": {
        "range": {"sheetId": 0, "startRowIndex": 1,
                  "endRowIndex": grand_total_row,
                  "startColumnIndex": 1, "endColumnIndex": 5},
        "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"},
        }},
        "fields": "userEnteredFormat.numberFormat",
    }})

    # ── Tab colors ────────────────────────────────────────────────────────────
    tab_colors = {
        0: {"red": 0.13, "green": 0.23, "blue": 0.40},   # navy — Cost Sheet
        1: {"red": 0.18, "green": 0.49, "blue": 0.34},   # green — Draw Schedule
        2: {"red": 0.85, "green": 0.60, "blue": 0.10},   # amber — Change Orders
    }
    for sid, color in tab_colors.items():
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": sid, "tabColorStyle": {"rgbColor": color}},
            "fields": "tabColorStyle",
        }})

    # ── D column (Diff) conditional formatting: green ≥0, red <0 ─────────────
    # Green rule (value >= 0)
    reqs.append({"addConditionalFormatRule": {
        "rule": {
            "ranges": [{"sheetId": 0, "startRowIndex": 1,
                        "endRowIndex": grand_total_row,
                        "startColumnIndex": 3, "endColumnIndex": 4}],
            "booleanRule": {
                "condition": {"type": "NUMBER_GREATER_THAN_EQ",
                              "values": [{"userEnteredValue": "0"}]},
                "format": {"textFormat": {"foregroundColor":
                           {"red": 0.13, "green": 0.50, "blue": 0.13}}},
            },
        },
        "index": 0,
    }})
    # Red rule (value < 0)
    reqs.append({"addConditionalFormatRule": {
        "rule": {
            "ranges": [{"sheetId": 0, "startRowIndex": 1,
                        "endRowIndex": grand_total_row,
                        "startColumnIndex": 3, "endColumnIndex": 4}],
            "booleanRule": {
                "condition": {"type": "NUMBER_LESS",
                              "values": [{"userEnteredValue": "0"}]},
                "format": {"textFormat": {"foregroundColor":
                           {"red": 0.80, "green": 0.10, "blue": 0.10}}},
            },
        },
        "index": 1,
    }})

    # ── Banded rows for data rows (white / light gray alternating) ─────────────
    # We'll do a simple alternating via addBanding on the full data range
    reqs.append({"addBanding": {"bandedRange": {
        "range": {"sheetId": 0, "startRowIndex": 1,
                  "endRowIndex": grand_total_row,
                  "startColumnIndex": 0, "endColumnIndex": 6},
        "rowProperties": {
            "firstBandColor": WHITE,
            "secondBandColor": LIGHT_GRAY,
        },
    }}})

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": reqs}
    ).execute()


def apply_draw_schedule_formatting(sheets_svc, sheet_id):
    """Freeze row 1, format Amount column as currency, add checkbox validation."""
    reqs = []

    # Freeze row 1
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": 1, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount",
    }})

    # Amount col (C = index 2) as currency
    reqs.append({"repeatCell": {
        "range": {"sheetId": 1, "startRowIndex": 1, "endRowIndex": 10,
                  "startColumnIndex": 2, "endColumnIndex": 3},
        "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"},
        }},
        "fields": "userEnteredFormat.numberFormat",
    }})

    # Checkbox validation for Requested (col G=6), Approved (col H=7), Funded (col I=8)
    for col_idx in (6, 7, 8):
        reqs.append({"setDataValidation": {
            "range": {"sheetId": 1, "startRowIndex": 1, "endRowIndex": 10,
                      "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
        }})

    # Column widths
    draw_widths = {0: 70, 1: 200, 2: 110, 3: 110, 4: 150, 5: 110, 6: 100, 7: 100, 8: 80, 9: 180}
    for ci, px in draw_widths.items():
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": 1, "dimension": "COLUMNS",
                      "startIndex": ci, "endIndex": ci + 1},
            "properties": {"pixelSize": px}, "fields": "pixelSize",
        }})

    # Header row formatting
    NAVY  = {"red": 0.13, "green": 0.23, "blue": 0.40}
    WHITE = {"red": 1.00, "green": 1.00, "blue": 1.00}
    reqs.append({"repeatCell": {
        "range": {"sheetId": 1, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 10},
        "cell": {"userEnteredFormat": {
            "backgroundColor": NAVY,
            "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 10},
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
    }})

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": reqs}
    ).execute()


def apply_change_order_formatting(sheets_svc, sheet_id):
    """Freeze row 1, add checkbox validation for Owner Approved column."""
    reqs = []

    # Freeze row 1
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": 2, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount",
    }})

    # Checkbox for Owner Approved (col F = index 5)
    reqs.append({"setDataValidation": {
        "range": {"sheetId": 2, "startRowIndex": 1, "endRowIndex": 21,
                  "startColumnIndex": 5, "endColumnIndex": 6},
        "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
    }})

    # Column widths
    co_widths = {0: 60, 1: 100, 2: 280, 3: 130, 4: 110, 5: 110, 6: 160, 7: 220}
    for ci, px in co_widths.items():
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": 2, "dimension": "COLUMNS",
                      "startIndex": ci, "endIndex": ci + 1},
            "properties": {"pixelSize": px}, "fields": "pixelSize",
        }})

    # Amount (col E = index 4) as currency
    reqs.append({"repeatCell": {
        "range": {"sheetId": 2, "startRowIndex": 1, "endRowIndex": 21,
                  "startColumnIndex": 4, "endColumnIndex": 5},
        "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"},
        }},
        "fields": "userEnteredFormat.numberFormat",
    }})

    # Header row formatting
    NAVY  = {"red": 0.13, "green": 0.23, "blue": 0.40}
    WHITE = {"red": 1.00, "green": 1.00, "blue": 1.00}
    reqs.append({"repeatCell": {
        "range": {"sheetId": 2, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 8},
        "cell": {"userEnteredFormat": {
            "backgroundColor": NAVY,
            "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 10},
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
    }})

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": reqs}
    ).execute()


def main():
    print("Authenticating with Google...")
    creds = get_credentials()
    drive_svc  = build("drive",  "v3", credentials=creds)
    sheets_svc = build("sheets", "v4", credentials=creds)

    # ── Step 1: Create Finance Office folder ──────────────────────────────────
    print(f"\nCreating folder '{FINANCE_OFFICE_NAME}' in Home Building Agent V.1...")
    folder_id = create_folder(drive_svc, FINANCE_OFFICE_NAME, HOME_BUILDER_V1_FOLDER_ID)
    print(f"  Folder ID: {folder_id}")

    # ── Step 2: Create the spreadsheet ───────────────────────────────────────
    print(f"\nCreating spreadsheet '{COST_TRACKER_NAME}'...")
    sheet_id, sheet_url = create_spreadsheet(sheets_svc, drive_svc,
                                              COST_TRACKER_NAME, folder_id)
    print(f"  Sheet ID: {sheet_id}")
    print(f"  URL: {sheet_url}")

    # ── Build Cost Sheet rows ─────────────────────────────────────────────────
    print("\nBuilding Cost Sheet data...")
    (cost_rows, section_total_row_indices,
     line_item_map, section_header_rows, grand_total_row) = build_cost_sheet_rows()
    print(f"  {len(cost_rows)} rows total, grand total at row {grand_total_row}")

    draw_rows = build_draw_schedule_rows()
    co_rows = build_change_order_rows()

    # ── Write values to all 3 tabs ────────────────────────────────────────────
    print("Writing values to Cost Sheet, Draw Schedule, Change Orders...")
    sheets_svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": [
                {"range": "Cost Sheet!A1",     "values": cost_rows},
                {"range": "Draw Schedule!A1",  "values": draw_rows},
                {"range": "Change Orders!A1",  "values": co_rows},
            ],
        },
    ).execute()
    print("  Values written.")

    # ── Step 3: Apply formatting ──────────────────────────────────────────────
    print("\nApplying Cost Sheet formatting...")
    try:
        apply_cost_sheet_formatting(
            sheets_svc, sheet_id,
            section_header_rows, section_total_row_indices, grand_total_row,
            len(cost_rows)
        )
        print("  Cost Sheet formatting applied.")
    except Exception as e:
        print(f"  WARNING: Cost Sheet formatting failed (non-fatal): {e}")

    print("Applying Draw Schedule formatting...")
    try:
        apply_draw_schedule_formatting(sheets_svc, sheet_id)
        print("  Draw Schedule formatting applied.")
    except Exception as e:
        print(f"  WARNING: Draw Schedule formatting failed (non-fatal): {e}")

    print("Applying Change Orders formatting...")
    try:
        apply_change_order_formatting(sheets_svc, sheet_id)
        print("  Change Orders formatting applied.")
    except Exception as e:
        print(f"  WARNING: Change Orders formatting failed (non-fatal): {e}")

    # ── Print IDs for config.py ───────────────────────────────────────────────
    print("\n" + "="*60)
    print("DONE. Add these to home_builder_agent/config.py:")
    print(f"  FINANCE_FOLDER_ID   = \"{folder_id}\"")
    print(f"  COST_TRACKER_ID     = \"{sheet_id}\"")
    print(f"\nCost Tracker URL: {sheet_url}")
    print("="*60)

    return folder_id, sheet_id, sheet_url


if __name__ == "__main__":
    main()
