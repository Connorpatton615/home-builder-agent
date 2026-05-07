"""finance.py — Chad's Finance Office integration.

Creates and manages Cost Tracker Google Sheets for each project.
Mirrors the structure of Chad's XLS Cost Sheet: 15+ sections with
Budget / Change Orders / Revised Budget / Actual / Difference / Billed columns.

Tab structure:
  Cost Tracker       — line-item detail by section, pre-populated with
                       known allowances from the project spec (11 columns)
  Finance Summary    — auto-computed KPIs written by hb-finance
  Allowance Recon    — allowance tracking vs. client selections
  Actuals Log        — append-only log of actual cost entries
  Invoices           — invoice register with aging buckets
"""

from datetime import datetime

from googleapiclient.discovery import build


# ---------------------------------------------------------------------
# Cost section data — Whitfield Residence
# Each item is (description, budget_or_None)
# None = Chad fills in the actual budget number
# ---------------------------------------------------------------------

COST_SECTIONS = [
    {
        "name": "Permits & Fees",
        "items": [
            ("Building Permits", None),
            ("Impact Fees", None),
            ("Survey & Staking", None),
            ("Architectural Plans / Engineering", None),
        ],
    },
    {
        "name": "Site Work",
        "items": [
            ("Land Clearing & Grading", None),
            ("Erosion Control", None),
            ("Temporary Power & Utilities", None),
        ],
    },
    {
        "name": "Footings & Foundation",
        "items": [
            ("Footings", None),
            ("Foundation Walls", None),
            ("Waterproofing & Drainage", None),
        ],
    },
    {
        "name": "Concrete Slabs",
        "items": [
            ("Interior Slab on Grade", None),
            ("Garage Slab", None),
            ("Porch / Patio Concrete", None),
        ],
    },
    {
        "name": "Framing",
        "items": [
            ("Framing Labor & Materials", None),
            ("Engineered Lumber / LVL", None),
            ("Structural Sheathing & Housewrap", None),
        ],
    },
    {
        "name": "Structural Steel",
        "items": [
            ("Steel Beams & Columns", None),
        ],
    },
    {
        "name": "Windows & Exterior Doors",
        "items": [
            ("Windows", None),
            ("Front Door Allowance", 2200),
            ("Exterior Doors (other)", None),
            ("Garage Doors", None),
        ],
    },
    {
        "name": "Roofing & Gutters",
        "items": [
            ("Roofing Materials & Labor", None),
            ("Gutters & Downspouts", None),
        ],
    },
    {
        "name": "Mechanical Systems",
        "items": [
            ("Plumbing Rough-In", None),
            ("Plumbing Fixtures Allowance", 4500),
            ("Water Heater (Tankless) Allowance", 1200),
            ("HVAC Equipment & Labor", None),
            ("Electrical Rough-In", None),
            ("Lighting Fixtures Allowance", 6500),
            ("Kitchen Vent Hood Allowance", 1800),
        ],
    },
    {
        "name": "Exterior Veneer",
        "items": [
            ("Siding / Brick / Stone", None),
            ("Exterior Trim & Millwork", None),
        ],
    },
    {
        "name": "Insulation & Drywall",
        "items": [
            ("Insulation", None),
            ("Drywall & Finish", None),
        ],
    },
    {
        "name": "Cabinets & Countertops",
        "items": [
            ("Kitchen Cabinets", None),
            ("Countertops — All Baths & Kitchen", 14000),
            ("Primary Closet System", 2400),
        ],
    },
    {
        "name": "Fireplace / Hearth / Mantle",
        "items": [
            ("Fireplace Unit", None),
            ("Hearth & Mantle", None),
        ],
    },
    {
        "name": "Interior Trim & Stairs",
        "items": [
            ("Interior Doors & Hardware", None),
            ("Trim Millwork & Wainscoting", None),
            ("Stairs", None),
            ("Bath Hardware", 800),
            ("Mirrors", 600),
        ],
    },
    {
        "name": "Flooring",
        "items": [
            ("Wood Flooring Installed (1,800 sqft @ $7.50)", 13500),
            ("Tile — Primary Shower Allowance", 2800),
            ("Tile — Guest Bath Shower Allowance (×2)", 2400),
        ],
    },
    {
        "name": "Wall Coverings & Paint",
        "items": [
            ("Interior Paint", None),
            ("Exterior Paint / Stain", None),
        ],
    },
    {
        "name": "Appliance Package",
        "items": [
            ("Appliance Package Allowance", 12000),
        ],
    },
    {
        "name": "Landscaping & Site Improvements",
        "items": [
            ("Landscaping Allowance", 4500),
            ("Irrigation System", None),
            ("Driveway & Flatwork", None),
        ],
    },
    {
        "name": "Clean-Up",
        "items": [
            ("Construction Clean-Up", None),
            ("Final Deep Clean", None),
        ],
    },
    {
        "name": "General Conditions",
        "items": [
            ("Superintendent / Project Management", None),
            ("Temporary Facilities & Utilities", None),
            ("Waste Management / Dumpsters", None),
            ("Builder's Risk Insurance", None),
            ("Miscellaneous", None),
        ],
    },
    {
        "name": "Contingency",
        "items": [
            ("Contingency Reserve (5% of contract)", None),
            ("Contingency Draws Used", None),
        ],
    },
]


# ---------------------------------------------------------------------
# Row builder — 11-column layout
#
# Columns (0-based indices):
#   A(0)  Line Item
#   B(1)  Budget ($)
#   C(2)  Change Orders ($)
#   D(3)  Revised Budget       =IF(C{r}="",B{r},B{r}+C{r})
#   E(4)  Actual ($)
#   F(5)  Difference ($)       =IF(E{r}="","",D{r}-E{r})
#   G(6)  Billed ($)
#   H(7)  Sub / Vendor
#   I(8)  Lien Waiver?         FALSE (checkbox)
#   J(9)  Draw #
#   K(10) Notes
# ---------------------------------------------------------------------

def _build_tracker_rows(project_name):
    """Build value rows for the Cost Tracker tab.

    Returns (rows, metadata) where:
      rows      = list of 11-element value arrays, one per sheet row
      metadata  = dict with row indices (0-based) for key rows
    """
    rows = []
    meta = {
        "title_row": 0,
        "header_row": 1,
        "section_header_rows": [],
        "subtotal_rows": [],
        "labor_total_row": None,
        "overhead_row": None,
        "profit_row": None,
        "contract_price_row": None,
    }

    # Row 0 — title (display: project name)
    rows.append([f"{project_name.upper()} — COST TRACKER",
                 "", "", "", "", "", "", "", "", "", ""])

    # Row 1 — column headers (11 columns)
    rows.append([
        "Line Item", "Budget ($)", "Change Orders ($)", "Revised Budget",
        "Actual ($)", "Difference ($)", "Billed ($)", "Sub / Vendor",
        "Lien Waiver?", "Draw #", "Notes",
    ])

    for section in COST_SECTIONS:
        # Section header row
        meta["section_header_rows"].append(len(rows))
        rows.append([section["name"].upper(),
                     "", "", "", "", "", "", "", "", "", ""])

        first_item_idx = len(rows)           # 0-based, first item of this section
        for item_name, budget in section["items"]:
            r = len(rows) + 1               # 1-based sheet row number
            revised  = f'=IF(C{r}="",B{r},B{r}+C{r})'
            diff     = f'=IF(E{r}="","",D{r}-E{r})'
            rows.append([
                item_name,
                budget if budget is not None else "",  # B Budget
                "",       # C Change Orders
                revised,  # D Revised Budget
                "",       # E Actual
                diff,     # F Difference
                "",       # G Billed
                "",       # H Sub/Vendor
                "FALSE",  # I Lien Waiver (becomes checkbox)
                "",       # J Draw #
                "",       # K Notes
            ])
        last_item_idx = len(rows) - 1       # 0-based, last item of this section

        # Subtotal row
        sub_r = len(rows) + 1               # 1-based
        fi    = first_item_idx + 1          # 1-based first item
        li    = last_item_idx + 1           # 1-based last item
        rows.append([
            "Subtotal",
            f"=SUM(B{fi}:B{li})",           # B Budget subtotal
            f"=SUM(C{fi}:C{li})",           # C Change Orders subtotal
            f"=SUM(D{fi}:D{li})",           # D Revised Budget subtotal
            f"=SUM(E{fi}:E{li})",           # E Actual subtotal
            f"=D{sub_r}-E{sub_r}",          # F Difference subtotal
            f"=SUM(G{fi}:G{li})",           # G Billed subtotal
            "", "", "", "",                 # H, I, J, K
        ])
        meta["subtotal_rows"].append(len(rows) - 1)

        # Blank spacer between sections
        rows.append(["", "", "", "", "", "", "", "", "", "", ""])

    # ── Grand totals ────────────────────────────────────────────────────
    sub_refs_b = "+".join(f"B{r+1}" for r in meta["subtotal_rows"])
    sub_refs_c = "+".join(f"C{r+1}" for r in meta["subtotal_rows"])
    sub_refs_d = "+".join(f"D{r+1}" for r in meta["subtotal_rows"])
    sub_refs_e = "+".join(f"E{r+1}" for r in meta["subtotal_rows"])
    sub_refs_g = "+".join(f"G{r+1}" for r in meta["subtotal_rows"])
    lt_r = len(rows) + 1
    rows.append([
        "TOTAL COST OF LABOR & MATERIALS",
        f"={sub_refs_b}",           # B Budget
        f"={sub_refs_c}",           # C Change Orders
        f"={sub_refs_d}",           # D Revised Budget
        f"={sub_refs_e}",           # E Actual
        f"=D{lt_r}-E{lt_r}",        # F Difference
        f"={sub_refs_g}",           # G Billed
        "", "", "", "",             # H, I, J, K
    ])
    meta["labor_total_row"] = len(rows) - 1

    rows.append(["", "", "", "", "", "", "", "", "", "", ""])   # spacer

    # Overhead line — Chad fills in the amount
    oh_r = len(rows) + 1
    rows.append([
        "Total Overhead",
        "",                                      # B Budget
        "",                                      # C Change Orders
        f'=IF(B{oh_r}="","",B{oh_r})',           # D Revised Budget
        "",                                      # E Actual
        f'=IF(E{oh_r}="","",D{oh_r}-E{oh_r})',  # F Difference
        "",                                      # G Billed
        "", "", "", "",                          # H, I, J, K
    ])
    meta["overhead_row"] = len(rows) - 1

    # Profit / Builder's fee line
    pr_r = len(rows) + 1
    rows.append([
        "Total Profit / Builder's Fee",
        "",                                      # B Budget
        "",                                      # C Change Orders
        f'=IF(B{pr_r}="","",B{pr_r})',           # D Revised Budget
        "",                                      # E Actual
        f'=IF(E{pr_r}="","",D{pr_r}-E{pr_r})',  # F Difference
        "",                                      # G Billed
        "", "", "", "",                          # H, I, J, K
    ])
    meta["profit_row"] = len(rows) - 1

    rows.append(["", "", "", "", "", "", "", "", "", "", ""])   # spacer

    # Contract price
    lt1 = meta["labor_total_row"] + 1
    oh1 = meta["overhead_row"] + 1
    pr1 = meta["profit_row"] + 1
    cp_r = len(rows) + 1
    rows.append([
        "CONTRACT PRICE",
        f"=B{lt1}+B{oh1}+B{pr1}",           # B Budget
        f"=C{lt1}+C{oh1}+C{pr1}",           # C Change Orders
        f"=D{lt1}+D{oh1}+D{pr1}",           # D Revised Budget
        f"=E{lt1}+E{oh1}+E{pr1}",           # E Actual
        f"=D{cp_r}-E{cp_r}",                # F Difference
        f"=G{lt1}+G{oh1}+G{pr1}",           # G Billed
        "", "", "", "",                     # H, I, J, K
    ])
    meta["contract_price_row"] = len(rows) - 1

    return rows, meta


# ---------------------------------------------------------------------
# Sheet creation
# ---------------------------------------------------------------------

def create_cost_tracker_sheet(creds, project_name, folder_id):
    """Create the Cost Tracker + Finance Summary Google Sheet.

    Returns dict with `id` and `webViewLink`.
    """
    sheets_svc = build("sheets", "v4", credentials=creds)
    drive_svc  = build("drive",  "v3", credentials=creds)

    sheet_name = f"{project_name} — Cost Tracker"

    # 1. Create spreadsheet with two tabs
    spreadsheet = sheets_svc.spreadsheets().create(body={
        "properties": {"title": sheet_name},
        "sheets": [
            {"properties": {"sheetId": 0, "title": "Cost Tracker",
                            "gridProperties": {"frozenRowCount": 2}}},
            {"properties": {"sheetId": 1, "title": "Finance Summary",
                            "gridProperties": {"frozenRowCount": 1}}},
        ],
    }, fields="spreadsheetId,spreadsheetUrl").execute()

    sheet_id = spreadsheet["spreadsheetId"]

    # 2. Move from My Drive root to Finance Office folder
    drive_svc.files().update(
        fileId=sheet_id,
        addParents=folder_id,
        removeParents="root",
        fields="id, parents",
    ).execute()

    # 3. Build row data
    rows, meta = _build_tracker_rows(project_name)
    total_rows = len(rows)

    # 4. Write values to Cost Tracker tab
    sheets_svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": "Cost Tracker!A1", "values": rows}],
        },
    ).execute()

    # 5. Format
    _apply_tracker_formatting(sheets_svc, sheet_id, meta, total_rows)

    return {"id": sheet_id,
            "webViewLink": spreadsheet["spreadsheetUrl"],
            "meta": meta}


# ---------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------

def _apply_tracker_formatting(sheets_svc, sheet_id, meta, total_rows):
    """Apply professional formatting to the Cost Tracker tab (sheetId=0)."""
    sid = 0   # Cost Tracker tab

    # ── Color palette ──────────────────────────────────────────────────
    NAVY       = {"red": 0.13, "green": 0.23, "blue": 0.40}   # headers / title
    WHITE      = {"red": 1.00, "green": 1.00, "blue": 1.00}
    SECT_BG    = {"red": 0.87, "green": 0.91, "blue": 0.96}   # section header
    SECT_TEXT  = {"red": 0.13, "green": 0.23, "blue": 0.40}   # navy text
    SUB_BG     = {"red": 0.93, "green": 0.94, "blue": 0.96}   # subtotal row
    TOTAL_BG   = {"red": 0.24, "green": 0.37, "blue": 0.55}   # labor total
    CONTRACT   = {"red": 0.13, "green": 0.23, "blue": 0.40}   # contract price
    BAND_ODD   = {"red": 0.97, "green": 0.98, "blue": 1.00}
    BAND_EVEN  = WHITE
    BORDER     = {"red": 0.70, "green": 0.72, "blue": 0.75}
    BORDER_MED = {"red": 0.40, "green": 0.43, "blue": 0.48}
    RED_TEXT   = {"red": 0.80, "green": 0.10, "blue": 0.10}   # over-budget

    def _border(color=None, style="SOLID", width=1):
        c = color or BORDER
        return {"style": style, "width": width, "colorStyle": {"rgbColor": c}}

    def _cell_row(r_idx, bg, text_color, bold=False, size=10,
                  h_align="LEFT", v_align="MIDDLE", wrap="CLIP", padding=None):
        fmt = {
            "backgroundColor": bg,
            "textFormat": {
                "bold": bold,
                "foregroundColor": text_color,
                "fontSize": size,
            },
            "horizontalAlignment": h_align,
            "verticalAlignment": v_align,
            "wrapStrategy": wrap,
        }
        if padding:
            fmt["padding"] = padding
        return {
            "repeatCell": {
                "range": {"sheetId": sid,
                          "startRowIndex": r_idx, "endRowIndex": r_idx + 1},
                "cell": {"userEnteredFormat": fmt},
                "fields": ("userEnteredFormat(backgroundColor,textFormat,"
                           "horizontalAlignment,verticalAlignment,wrapStrategy"
                           + (",padding)" if padding else ")")),
            }
        }

    reqs = []

    # ── Title row ──────────────────────────────────────────────────────
    tr = meta["title_row"]
    reqs += [
        {"mergeCells": {
            "range": {"sheetId": sid, "startRowIndex": tr, "endRowIndex": tr + 1,
                      "startColumnIndex": 0, "endColumnIndex": 11},
            "mergeType": "MERGE_ALL",
        }},
        _cell_row(tr, NAVY, WHITE, bold=True, size=13,
                  h_align="CENTER", padding={"top": 10, "bottom": 10,
                                             "left": 12, "right": 12}),
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": tr, "endIndex": tr + 1},
            "properties": {"pixelSize": 44},
            "fields": "pixelSize",
        }},
    ]

    # ── Header row ─────────────────────────────────────────────────────
    hr = meta["header_row"]
    reqs += [
        _cell_row(hr, NAVY, WHITE, bold=True, size=10,
                  padding={"top": 6, "bottom": 6, "left": 8, "right": 8}),
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": hr, "endIndex": hr + 1},
            "properties": {"pixelSize": 30},
            "fields": "pixelSize",
        }},
        # Thick bottom border under headers
        {"updateBorders": {
            "range": {"sheetId": sid,
                      "startRowIndex": hr, "endRowIndex": hr + 1,
                      "startColumnIndex": 0, "endColumnIndex": 11},
            "bottom": _border(BORDER_MED, style="SOLID_MEDIUM", width=2),
        }},
    ]

    # ── Section header rows ────────────────────────────────────────────
    for r_idx in meta["section_header_rows"]:
        reqs += [
            _cell_row(r_idx, SECT_BG, SECT_TEXT, bold=True, size=9,
                      padding={"top": 4, "bottom": 4, "left": 10, "right": 8}),
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": r_idx, "endIndex": r_idx + 1},
                "properties": {"pixelSize": 24},
                "fields": "pixelSize",
            }},
        ]

    # ── Subtotal rows ──────────────────────────────────────────────────
    for r_idx in meta["subtotal_rows"]:
        reqs += [
            _cell_row(r_idx, SUB_BG, SECT_TEXT, bold=True, size=9),
            {"updateBorders": {
                "range": {"sheetId": sid,
                          "startRowIndex": r_idx, "endRowIndex": r_idx + 1,
                          "startColumnIndex": 0, "endColumnIndex": 11},
                "top": _border(BORDER_MED, style="SOLID"),
            }},
        ]

    # ── Labor total row ────────────────────────────────────────────────
    lt = meta["labor_total_row"]
    if lt is not None:
        reqs += [
            _cell_row(lt, TOTAL_BG, WHITE, bold=True, size=10),
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": lt, "endIndex": lt + 1},
                "properties": {"pixelSize": 28},
                "fields": "pixelSize",
            }},
        ]

    # ── Contract price row ─────────────────────────────────────────────
    cp = meta["contract_price_row"]
    if cp is not None:
        reqs += [
            _cell_row(cp, CONTRACT, WHITE, bold=True, size=11),
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": cp, "endIndex": cp + 1},
                "properties": {"pixelSize": 32},
                "fields": "pixelSize",
            }},
            {"updateBorders": {
                "range": {"sheetId": sid,
                          "startRowIndex": cp, "endRowIndex": cp + 1,
                          "startColumnIndex": 0, "endColumnIndex": 11},
                "top":    _border(BORDER_MED, style="SOLID_MEDIUM", width=2),
                "bottom": _border(BORDER_MED, style="SOLID_MEDIUM", width=2),
            }},
        ]

    # ── Item rows: banding, WRAP, TOP align ───────────────────────────
    special = (set(meta["section_header_rows"])
               | set(meta["subtotal_rows"])
               | {meta["title_row"], meta["header_row"],
                  meta["labor_total_row"],
                  meta["overhead_row"], meta["profit_row"],
                  meta["contract_price_row"]})
    for r_idx in range(2, total_rows):
        if r_idx in special:
            continue
        bg = BAND_ODD if r_idx % 2 == 0 else BAND_EVEN
        reqs.append({
            "repeatCell": {
                "range": {"sheetId": sid,
                          "startRowIndex": r_idx, "endRowIndex": r_idx + 1,
                          "startColumnIndex": 0, "endColumnIndex": 11},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": bg,
                    "wrapStrategy": "WRAP",
                    "verticalAlignment": "TOP",
                    "textFormat": {"fontSize": 9},
                }},
                "fields": ("userEnteredFormat(backgroundColor,wrapStrategy,"
                           "verticalAlignment,textFormat)"),
            }
        })

    # ── Currency format on B, C, D, E, F, G columns (indices 1–6) ─────
    reqs.append({
        "repeatCell": {
            "range": {"sheetId": sid,
                      "startRowIndex": 2, "endRowIndex": total_rows,
                      "startColumnIndex": 1, "endColumnIndex": 7},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "CURRENCY", "pattern": r'$#,##0'},
            }},
            "fields": "userEnteredFormat(numberFormat)",
        }
    })

    # ── Conditional format: over-budget (Difference < 0) → red text ───
    # Difference is column F (index 5)
    reqs.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": sid,
                            "startRowIndex": 2, "endRowIndex": total_rows,
                            "startColumnIndex": 5, "endColumnIndex": 6}],
                "booleanRule": {
                    "condition": {"type": "NUMBER_LESS", "values": [
                        {"userEnteredValue": "0"}
                    ]},
                    "format": {
                        "textFormat": {"foregroundColor": RED_TEXT, "bold": True}
                    },
                },
            },
            "index": 0,
        }
    })

    # ── Checkbox data validation for Lien Waiver column (I, index 8) ──
    reqs.append({
        "setDataValidation": {
            "range": {"sheetId": sid,
                      "startRowIndex": 2, "endRowIndex": total_rows,
                      "startColumnIndex": 8, "endColumnIndex": 9},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
        }
    })

    # ── Overall borders on the data grid ──────────────────────────────
    reqs.append({
        "updateBorders": {
            "range": {"sheetId": sid,
                      "startRowIndex": 1, "endRowIndex": total_rows,
                      "startColumnIndex": 0, "endColumnIndex": 11},
            "innerHorizontal": _border(),
            "innerVertical":   _border(),
            "left":            _border(),
            "right":           _border(),
            "top":             _border(),
            "bottom":          _border(),
        }
    })

    # ── Column widths (11 columns) ─────────────────────────────────────
    col_widths = [280, 110, 110, 110, 110, 120, 100, 160, 95, 70, 250]
    for i, px in enumerate(col_widths):
        reqs.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }
        })

    # ── Tab color (green) ──────────────────────────────────────────────
    reqs.append({
        "updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "tabColorStyle": {"rgbColor":
                               {"red": 0.18, "green": 0.49, "blue": 0.34}}},
            "fields": "tabColorStyle",
        }
    })

    # ── Freeze top 2 rows; auto-resize item rows ──────────────────────
    # Note: can't freeze a column when the title row has merged cells spanning
    # all columns — the merged title stays, column freeze is skipped.
    reqs += [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount",
        }},
        {"autoResizeDimensions": {
            "dimensions": {"sheetId": sid, "dimension": "ROWS",
                           "startIndex": 2, "endIndex": total_rows},
        }},
    ]

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": reqs}
    ).execute()


# ---------------------------------------------------------------------
# Finance Summary tab
# ---------------------------------------------------------------------

def ensure_finance_summary_tab(sheets_svc, sheet_id):
    """Return the sheetId of the 'Finance Summary' tab, creating it if needed."""
    meta = sheets_svc.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties"
    ).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == "Finance Summary":
            return s["properties"]["sheetId"]
    # Create it
    resp = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {
            "properties": {"title": "Finance Summary", "index": 1}
        }}]},
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


def write_finance_summary(sheets_svc, sheet_id, summary, project_name):
    """Write the Finance Summary tab with KPIs and section breakdown."""
    tab_sid = ensure_finance_summary_tab(sheets_svc, sheet_id)
    now = datetime.now().strftime("%b %d, %Y at %I:%M %p")

    budget   = summary.get("contract_budget", 0)
    actual   = summary.get("contract_actual", 0)
    billed   = summary.get("contract_billed", 0)
    diff     = budget - actual
    pct      = (actual / budget * 100) if budget else 0
    pct_bill = (billed / actual * 100) if actual else 0

    def _fmt(v):
        if v is None or v == "":
            return "—"
        try:
            return f"${float(v):,.0f}"
        except (ValueError, TypeError):
            return str(v)

    def _pct(v):
        return f"{v:.1f}%"

    health = "🟢 ON BUDGET"
    if actual > budget:
        health = "🔴 OVER BUDGET"
    elif budget and (actual / budget) > 0.95:
        health = "🟡 WATCH — < 5% remaining"

    rows = [
        # Title
        [f"FINANCE SUMMARY — {project_name.upper()}", "", ""],
        [f"Updated {now}", "", ""],
        ["", "", ""],

        # KPI strip headers
        ["FINANCIAL OVERVIEW", "", ""],
        ["", "", ""],
        ["  Contract Price (Budget)",   _fmt(budget),  "Total agreed contract value"],
        ["  Actual Spent (to Date)",    _fmt(actual),  "Sum of all actual costs entered"],
        ["  Budget Remaining",          _fmt(diff),    "Contract Price minus Actual Spent"],
        ["  % of Budget Used",          _pct(pct),     ""],
        ["  Total Billed to Client",    _fmt(billed),  "Amount invoiced to homeowner"],
        ["  Billed as % of Spent",      _pct(pct_bill) if actual else "—",
                                                       ""],
        ["", "", ""],

        # Budget health
        ["BUDGET HEALTH", "", ""],
        ["", "", ""],
        ["  Status", health, ""],
        ["", "", ""],

        # Known allowances snapshot
        ["KNOWN ALLOWANCES (from Spec)", "", ""],
        ["", "", ""],
        ["  Lighting Fixtures",       "$6,500",  "Allowance from spec"],
        ["  Plumbing Fixtures",       "$4,500",  "Allowance from spec"],
        ["  Countertops (all)",       "$14,000", "All baths + kitchen"],
        ["  Appliance Package",       "$12,000", "Allowance from spec"],
        ["  Wood Flooring (1,800 sf)", "$13,500", "$7.50/sqft installed"],
        ["  Landscaping",             "$4,500",  "Allowance from spec"],
        ["  Primary Shower Tile",     "$2,800",  "Allowance from spec"],
        ["  Guest Bath Tile (×2)",    "$2,400",  "Allowance from spec"],
        ["  Water Heater (tankless)", "$1,200",  "Allowance from spec"],
        ["  Kitchen Vent Hood",       "$1,800",  "Allowance from spec"],
        ["  Front Door",             "$2,200",  "Allowance from spec"],
        ["  Primary Closet System",   "$2,400",  "Allowance from spec"],
        ["", "", ""],
        ["  Total Known Allowances",  "$67,300", "Sum of above"],
        ["", "", ""],
        ["→ Open Cost Tracker tab to enter actuals per section.", "", ""],
    ]

    # Write values
    sheets_svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": "Finance Summary!A1", "values": rows}],
        },
    ).execute()

    # Format the Summary tab
    _apply_summary_formatting(sheets_svc, sheet_id, tab_sid, len(rows))


def _apply_summary_formatting(sheets_svc, sheet_id, sid, total_rows):
    """Apply dashboard-style formatting to the Finance Summary tab."""
    NAVY      = {"red": 0.13, "green": 0.23, "blue": 0.40}
    WHITE     = {"red": 1.00, "green": 1.00, "blue": 1.00}
    SECT_BG   = {"red": 0.87, "green": 0.91, "blue": 0.96}
    SECT_TEXT = {"red": 0.13, "green": 0.23, "blue": 0.40}
    LIGHT_BG  = {"red": 0.95, "green": 0.97, "blue": 1.00}

    def _border(style="SOLID"):
        return {"style": style, "width": 1,
                "colorStyle": {"rgbColor": {"red": 0.70, "green": 0.72, "blue": 0.75}}}

    reqs = [
        # Title row — navy
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 13},
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "MIDDLE",
                "padding": {"top": 10, "bottom": 10, "left": 12, "right": 12},
            }},
            "fields": ("userEnteredFormat(backgroundColor,textFormat,"
                       "horizontalAlignment,verticalAlignment,padding)"),
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 44}, "fields": "pixelSize",
        }},
        # Updated row — small italic
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"italic": True, "foregroundColor": LIGHT_BG, "fontSize": 9},
                "padding": {"left": 12},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,padding)",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 20}, "fields": "pixelSize",
        }},
        # Section labels (rows 3, 12, 15, 16 approx) — light blue-gray
        *[{"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": r, "endRowIndex": r + 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": SECT_BG,
                "textFormat": {"bold": True, "foregroundColor": SECT_TEXT, "fontSize": 9},
                "padding": {"top": 4, "bottom": 4, "left": 8, "right": 8},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,padding)",
        }} for r in (3, 12, 16)],
        # Data rows background
        {"repeatCell": {
            "range": {"sheetId": sid,
                      "startRowIndex": 2, "endRowIndex": total_rows,
                      "startColumnIndex": 0, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {
                "wrapStrategy": "WRAP",
                "verticalAlignment": "MIDDLE",
                "textFormat": {"fontSize": 9},
            }},
            "fields": "userEnteredFormat(wrapStrategy,verticalAlignment,textFormat)",
        }},
        # Column widths
        *[{"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": px}, "fields": "pixelSize",
        }} for i, px in enumerate([280, 160, 260])],
        # Freeze top row
        {"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        # Tab color — steel blue
        {"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "tabColorStyle": {"rgbColor":
                               {"red": 0.19, "green": 0.42, "blue": 0.73}}},
            "fields": "tabColorStyle",
        }},
    ]

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": reqs}
    ).execute()


# ---------------------------------------------------------------------
# Finance summary reader
# ---------------------------------------------------------------------

def read_finance_summary(sheets_svc, sheet_id):
    """Read key financial totals from the Cost Tracker tab.

    Returns a dict with contract_budget, contract_actual, contract_billed,
    and diff. Reads computed values (formulas resolved by Sheets).

    Column mapping (11-col schema):
      A=0 Line Item, B=1 Budget, C=2 Change Orders, D=3 Revised Budget,
      E=4 Actual, F=5 Difference, G=6 Billed
    """
    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="Cost Tracker!A:G",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    rows = result.get("values", [])

    summary = {
        "contract_budget":  0,
        "contract_actual":  0,
        "contract_billed":  0,
        "labor_budget":     0,
        "labor_actual":     0,
    }

    def _to_float(val):
        try:
            return float(val) if val not in ("", None) else 0
        except (ValueError, TypeError):
            return 0

    for row in rows:
        if not row:
            continue
        label = str(row[0]).strip().upper()
        b = _to_float(row[1] if len(row) > 1 else "")   # Budget
        e = _to_float(row[4] if len(row) > 4 else "")   # Actual (col E, index 4)
        g = _to_float(row[6] if len(row) > 6 else "")   # Billed (col G, index 6)

        if "CONTRACT PRICE" in label:
            summary["contract_budget"] = b
            summary["contract_actual"] = e
            summary["contract_billed"] = g
        elif "TOTAL COST OF LABOR" in label:
            summary["labor_budget"] = b
            summary["labor_actual"] = e

    summary["budget_remaining"] = (summary["contract_budget"]
                                   - summary["contract_actual"])
    pct = (summary["contract_actual"] / summary["contract_budget"] * 100
           if summary["contract_budget"] else 0)
    summary["pct_spent"] = pct
    return summary


# ---------------------------------------------------------------------
# Allowance Reconciliation tab
# ---------------------------------------------------------------------

ALLOWANCE_ITEMS = [
    ("Lighting Fixtures Allowance",          6500),
    ("Plumbing Fixtures Allowance",          4500),
    ("Countertops — All Baths & Kitchen",   14000),
    ("Appliance Package",                   12000),
    ("Wood Flooring (1,800 sqft)",          13500),
    ("Landscaping",                          4500),
    ("Primary Shower Tile",                  2800),
    ("Guest Bath Shower Tile (×2)",          2400),
    ("Water Heater (Tankless)",              1200),
    ("Kitchen Vent Hood",                    1800),
    ("Front Door",                           2200),
    ("Primary Closet System",               2400),
    ("Bath Hardware",                         800),
    ("Mirrors",                               600),
]


def add_allowance_tab(sheets_svc, sheet_id):
    """Find or create the Allowance Reconciliation tab. Returns its sheetId."""
    meta = sheets_svc.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties"
    ).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == "Allowance Recon":
            return s["properties"]["sheetId"]
    # Create it
    resp = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {
            "properties": {"title": "Allowance Recon"}
        }}]},
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


def write_allowance_tab(sheets_svc, sheet_id, project_name):
    """Write (or overwrite) the Allowance Recon tab with Whitfield allowance data.

    Columns:
      A  Allowance Item
      B  Spec Allowance ($)
      C  Client Selection
      D  Selection Cost ($)
      E  Delta ($)              =D{r}-B{r}
      F  Change Order #
      G  Status
      H  Notes
    """
    tab_sid = add_allowance_tab(sheets_svc, sheet_id)

    headers = [
        "Allowance Item", "Spec Allowance ($)", "Client Selection",
        "Selection Cost ($)", "Delta ($)", "Change Order #", "Status", "Notes",
    ]

    # Row 1 = headers (1-based); data starts at row 2
    data_rows = [headers]
    for name, budget in ALLOWANCE_ITEMS:
        r = len(data_rows) + 1          # 1-based sheet row
        data_rows.append([
            name,
            budget,
            "",                         # C Client Selection
            "",                         # D Selection Cost
            f"=D{r}-B{r}",             # E Delta
            "",                         # F Change Order #
            "Pending",                  # G Status
            "",                         # H Notes
        ])

    total_rows = len(data_rows)

    # Write values
    tab_title = "Allowance Recon"
    sheets_svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": f"{tab_title}!A1", "values": data_rows}],
        },
    ).execute()

    # ── Formatting ─────────────────────────────────────────────────────
    NAVY     = {"red": 0.13, "green": 0.23, "blue": 0.40}
    WHITE    = {"red": 1.00, "green": 1.00, "blue": 1.00}
    RED_TEXT = {"red": 0.80, "green": 0.10, "blue": 0.10}
    GOLD_TAB = {"red": 0.93, "green": 0.58, "blue": 0.11}

    reqs = [
        # Navy header row
        {"repeatCell": {
            "range": {"sheetId": tab_sid,
                      "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 10},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "padding": {"top": 6, "bottom": 6, "left": 6, "right": 6},
            }},
            "fields": ("userEnteredFormat(backgroundColor,textFormat,"
                       "horizontalAlignment,verticalAlignment,padding)"),
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": tab_sid, "dimension": "ROWS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 30},
            "fields": "pixelSize",
        }},
        # Column widths: [260, 130, 200, 130, 110, 110, 100, 250]
        *[{"updateDimensionProperties": {
            "range": {"sheetId": tab_sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": px},
            "fields": "pixelSize",
        }} for i, px in enumerate([260, 130, 200, 130, 110, 110, 100, 250])],
        # Currency format: cols B, D, E (indices 1, 3, 4)
        *[{"repeatCell": {
            "range": {"sheetId": tab_sid,
                      "startRowIndex": 1, "endRowIndex": total_rows,
                      "startColumnIndex": ci, "endColumnIndex": ci + 1},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "CURRENCY", "pattern": r'$#,##0'},
            }},
            "fields": "userEnteredFormat(numberFormat)",
        }} for ci in (1, 3, 4)],
        # Red text on Delta (col E, index 4) when < 0 (over allowance)
        {"addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": tab_sid,
                            "startRowIndex": 1, "endRowIndex": total_rows,
                            "startColumnIndex": 4, "endColumnIndex": 5}],
                "booleanRule": {
                    "condition": {"type": "NUMBER_LESS",
                                  "values": [{"userEnteredValue": "0"}]},
                    "format": {"textFormat": {"foregroundColor": RED_TEXT, "bold": True}},
                },
            },
            "index": 0,
        }},
        # Status dropdown: col G (index 6)
        {"setDataValidation": {
            "range": {"sheetId": tab_sid,
                      "startRowIndex": 1, "endRowIndex": total_rows,
                      "startColumnIndex": 6, "endColumnIndex": 7},
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [
                        {"userEnteredValue": "Pending"},
                        {"userEnteredValue": "Under Allowance"},
                        {"userEnteredValue": "Over Allowance"},
                        {"userEnteredValue": "Confirmed"},
                    ],
                },
                "strict": True,
                "showCustomUi": True,
            },
        }},
        # Tab color: gold
        {"updateSheetProperties": {
            "properties": {"sheetId": tab_sid,
                           "tabColorStyle": {"rgbColor": GOLD_TAB}},
            "fields": "tabColorStyle",
        }},
        # Freeze row 1
        {"updateSheetProperties": {
            "properties": {"sheetId": tab_sid,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
    ]

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": reqs}
    ).execute()

    return tab_sid


# ---------------------------------------------------------------------
# Programmatic Cost Tracker update helpers
# ---------------------------------------------------------------------

# Column letter → 0-based index mapping for the 11-column schema
_COL_INDEX = {"B": 1, "C": 2, "D": 3, "E": 4, "F": 5, "G": 6,
              "H": 7, "I": 8, "J": 9, "K": 10}


def _update_cost_tracker_col(sheets_svc, sheet_id, section_name,
                              amount_to_add, col_letter):
    """Add amount_to_add to an arbitrary column for a section's subtotal row.

    Reads Cost Tracker!A:K, locates the section header and its Subtotal row,
    reads the current value in `col_letter`, adds amount_to_add, writes back.

    Returns (row_number_1based, new_total).
    Raises ValueError if section or subtotal row not found.
    """
    col_idx = _COL_INDEX[col_letter.upper()]
    # Read enough columns to reach the target column
    read_range = f"Cost Tracker!A:{col_letter.upper()}"
    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=read_range,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    raw_rows = result.get("values", [])

    target = section_name.strip().upper()
    section_row_idx = None
    for i, row in enumerate(raw_rows):
        if row and str(row[0]).strip().upper() == target:
            section_row_idx = i
            break

    if section_row_idx is None:
        raise ValueError(f"Section '{section_name}' not found in Cost Tracker")

    subtotal_row_idx = None
    for i in range(section_row_idx + 1, len(raw_rows)):
        row = raw_rows[i]
        if row and str(row[0]).strip().lower() == "subtotal":
            subtotal_row_idx = i
            break

    if subtotal_row_idx is None:
        raise ValueError(
            f"No Subtotal row found after section '{section_name}'"
        )

    sub_row = raw_rows[subtotal_row_idx]
    try:
        current = (float(sub_row[col_idx])
                   if len(sub_row) > col_idx
                   and sub_row[col_idx] not in ("", None)
                   else 0.0)
    except (ValueError, TypeError):
        current = 0.0

    new_total = current + amount_to_add
    row_1based = subtotal_row_idx + 1

    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"Cost Tracker!{col_letter.upper()}{row_1based}",
        valueInputOption="USER_ENTERED",
        body={"values": [[new_total]]},
    ).execute()

    return (row_1based, new_total)


def update_cost_tracker_actual(sheets_svc, sheet_id, section_name, amount_to_add):
    """Add amount_to_add to the Actual ($) column (E) for a section's subtotal.

    Returns (row_number_1based, new_total).
    Raises ValueError if section not found.
    """
    return _update_cost_tracker_col(
        sheets_svc, sheet_id, section_name, amount_to_add, "E"
    )


def update_cost_tracker_billed(sheets_svc, sheet_id, section_name, amount_to_add):
    """Add amount_to_add to the Billed ($) column (G) for a section's subtotal.

    Returns (row_number_1based, new_total).
    Raises ValueError if section not found.
    """
    return _update_cost_tracker_col(
        sheets_svc, sheet_id, section_name, amount_to_add, "G"
    )


def add_actuals_log_row(sheets_svc, sheet_id, entry):
    """Append a row to the Actuals Log tab (create tab if needed).

    entry dict keys: date, vendor, amount, section, receipt_link (opt), notes (opt)
    """
    # Find or create "Actuals Log" tab
    meta = sheets_svc.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties"
    ).execute()

    tab_exists = False
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == "Actuals Log":
            tab_exists = True
            break

    if not tab_exists:
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {
                "properties": {"title": "Actuals Log"}
            }}]},
        ).execute()
        # Write headers
        headers = [["Date", "Vendor", "Amount ($)", "Section",
                    "Receipt Link", "Notes"]]
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range="Actuals Log!A1",
            valueInputOption="USER_ENTERED",
            body={"values": headers},
        ).execute()

    # Append the entry row
    row = [
        entry["date"],
        entry["vendor"],
        entry["amount"],
        entry["section"],
        entry.get("receipt_link", ""),
        entry.get("notes", ""),
    ]
    sheets_svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range="Actuals Log!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()


# ---------------------------------------------------------------------
# Invoices tab
# ---------------------------------------------------------------------

def ensure_invoices_tab(sheets_svc, sheet_id):
    """Find or create the Invoices tab. Returns its sheetId.

    If creating, writes headers and applies formatting immediately.
    """
    meta = sheets_svc.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties"
    ).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == "Invoices":
            return s["properties"]["sheetId"]

    # Create the tab
    resp = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {
            "properties": {"title": "Invoices"}
        }}]},
    ).execute()
    tab_sid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    # Write headers
    headers = [[
        "Invoice #", "Vendor", "Description", "Amount ($)",
        "Invoice Date", "Due Date", "Status", "Aging",
        "Job", "Source", "Notes",
    ]]
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="Invoices!A1",
        valueInputOption="USER_ENTERED",
        body={"values": headers},
    ).execute()

    # Apply formatting
    NAVY      = {"red": 0.13, "green": 0.23, "blue": 0.40}
    WHITE     = {"red": 1.00, "green": 1.00, "blue": 1.00}
    PURPLE    = {"red": 0.60, "green": 0.15, "blue": 0.60}

    col_widths = [100, 180, 220, 110, 110, 110, 120, 120, 160, 100, 250]
    reqs = [
        # Navy header
        {"repeatCell": {
            "range": {"sheetId": tab_sid,
                      "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 10},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "padding": {"top": 6, "bottom": 6, "left": 6, "right": 6},
            }},
            "fields": ("userEnteredFormat(backgroundColor,textFormat,"
                       "horizontalAlignment,verticalAlignment,padding)"),
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": tab_sid, "dimension": "ROWS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 30},
            "fields": "pixelSize",
        }},
        # Column widths
        *[{"updateDimensionProperties": {
            "range": {"sheetId": tab_sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": px},
            "fields": "pixelSize",
        }} for i, px in enumerate(col_widths)],
        # Status dropdown (col G = index 6) — placeholder; row range is generous
        {"setDataValidation": {
            "range": {"sheetId": tab_sid,
                      "startRowIndex": 1, "endRowIndex": 1000,
                      "startColumnIndex": 6, "endColumnIndex": 7},
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [
                        {"userEnteredValue": "Received"},
                        {"userEnteredValue": "Paid"},
                        {"userEnteredValue": "Disputed"},
                    ],
                },
                "strict": True,
                "showCustomUi": True,
            },
        }},
        # Tab color: purple
        {"updateSheetProperties": {
            "properties": {"sheetId": tab_sid,
                           "tabColorStyle": {"rgbColor": PURPLE}},
            "fields": "tabColorStyle",
        }},
        # Freeze row 1
        {"updateSheetProperties": {
            "properties": {"sheetId": tab_sid,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
    ]
    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": reqs}
    ).execute()

    return tab_sid


def add_invoice_row(sheets_svc, sheet_id, invoice):
    """Append one invoice to the Invoices tab.

    invoice dict keys:
      invoice_number, vendor, description, amount,
      invoice_date, due_date, status, job, source, notes
    """
    ensure_invoices_tab(sheets_svc, sheet_id)

    # Find the next empty row by reading col A
    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="Invoices!A:A",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    existing = result.get("values", [])
    r = len(existing) + 1   # 1-based row for the new entry

    # Aging formula: checks Status == "Paid" first, then due/invoice date aging
    aging_formula = (
        f'=IF(G{r}="Paid","Paid",'
        f'IF(ISBLANK(F{r}),'
        f'IF(ISBLANK(E{r}),"",'
        f'IF(TODAY()-E{r}>90,"90+ Days",'
        f'IF(TODAY()-E{r}>60,"60-90 Days",'
        f'IF(TODAY()-E{r}>30,"30-60 Days","Current")))),'
        f'IF(TODAY()-F{r}>90,"90+ Days",'
        f'IF(TODAY()-F{r}>60,"60-90 Days",'
        f'IF(TODAY()-F{r}>30,"30-60 Days","Current")))))'
    )

    row = [
        invoice.get("invoice_number", ""),
        invoice.get("vendor", ""),
        invoice.get("description", ""),
        invoice.get("amount", ""),
        invoice.get("invoice_date", ""),
        invoice.get("due_date", ""),
        invoice.get("status", "Received"),
        aging_formula,
        invoice.get("job", ""),
        invoice.get("source", ""),
        invoice.get("notes", ""),
    ]
    sheets_svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range="Invoices!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()


def get_aging_report(sheets_svc, sheet_id):
    """Read the Invoices tab and return a summary of outstanding invoices by aging bucket.

    Returns dict:
      {
        "current":  {"count": int, "total": float},
        "30_60":    {"count": int, "total": float},
        "60_90":    {"count": int, "total": float},
        "over_90":  {"count": int, "total": float},
        "paid":     {"count": int, "total": float},
        "all_invoices": list of invoice dicts,
      }
    """
    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="Invoices!A:K",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    raw_rows = result.get("values", [])

    report = {
        "current":  {"count": 0, "total": 0.0},
        "30_60":    {"count": 0, "total": 0.0},
        "60_90":    {"count": 0, "total": 0.0},
        "over_90":  {"count": 0, "total": 0.0},
        "paid":     {"count": 0, "total": 0.0},
        "all_invoices": [],
    }

    if not raw_rows:
        return report

    # Skip header row (index 0)
    for row in raw_rows[1:]:
        if not row:
            continue

        def _get(idx, default=""):
            return row[idx] if len(row) > idx else default

        inv_number   = _get(0)
        vendor       = _get(1)
        description  = _get(2)
        try:
            amount = float(_get(3, 0)) if _get(3, 0) not in ("", None) else 0.0
        except (ValueError, TypeError):
            amount = 0.0
        invoice_date = _get(4)
        due_date     = _get(5)
        status       = str(_get(6)).strip()
        aging_val    = str(_get(7)).strip()
        job          = _get(8)
        source       = _get(9)
        notes        = _get(10)

        inv_dict = {
            "invoice_number": inv_number,
            "vendor": vendor,
            "description": description,
            "amount": amount,
            "invoice_date": invoice_date,
            "due_date": due_date,
            "status": status,
            "aging": aging_val,
            "job": job,
            "source": source,
            "notes": notes,
        }
        report["all_invoices"].append(inv_dict)

        # Bucket assignment
        if status.lower() == "paid":
            report["paid"]["count"] += 1
            report["paid"]["total"] += amount
            continue

        # Use the aging column value; if it's a formula string or blank, compute
        aging_upper = aging_val.upper()
        if "90+" in aging_upper or "90" in aging_upper and "60" not in aging_upper:
            bucket = "over_90"
        elif "60-90" in aging_upper or "60" in aging_upper:
            bucket = "60_90"
        elif "30-60" in aging_upper or "30" in aging_upper:
            bucket = "30_60"
        elif "CURRENT" in aging_upper:
            bucket = "current"
        else:
            # Compute from dates if aging column is empty or unresolved formula
            ref_date = due_date if due_date not in ("", None) else invoice_date
            if ref_date not in ("", None):
                try:
                    # Sheets serial dates: days since Dec 30, 1899
                    if isinstance(ref_date, (int, float)):
                        from datetime import timedelta
                        origin = datetime(1899, 12, 30)
                        ref_dt = origin + timedelta(days=int(ref_date))
                        days_old = (datetime.now() - ref_dt).days
                    else:
                        # Try ISO string
                        ref_dt = datetime.fromisoformat(str(ref_date))
                        days_old = (datetime.now() - ref_dt).days
                    if days_old > 90:
                        bucket = "over_90"
                    elif days_old > 60:
                        bucket = "60_90"
                    elif days_old > 30:
                        bucket = "30_60"
                    else:
                        bucket = "current"
                except Exception:
                    bucket = "current"
            else:
                bucket = "current"

        report[bucket]["count"] += 1
        report[bucket]["total"] += amount

    return report


# ---------------------------------------------------------------------
# Change Orders tab
# ---------------------------------------------------------------------

CO_TAB_HEADERS = [
    "CO #", "Date", "Description", "Section", "Cost Delta ($)",
    "Schedule Impact (days)", "Status", "Doc Link", "Notes",
]

def ensure_change_orders_tab(sheets_svc, sheet_id):
    """Create the 'Change Orders' tab if it doesn't exist. Returns sheet_id."""
    meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    names = [s["properties"]["title"] for s in meta["sheets"]]
    if "Change Orders" in names:
        return

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": "Change Orders"}}}]},
    ).execute()

    # Write headers
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="Change Orders!A1:I1",
        valueInputOption="USER_ENTERED",
        body={"values": [CO_TAB_HEADERS]},
    ).execute()


def get_next_co_number(sheets_svc, sheet_id):
    """Return next CO number as a zero-padded string, e.g. 'CO-003'."""
    try:
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Change Orders!A:A",
        ).execute()
        rows = result.get("values", [])
        # rows[0] is the header; data starts at rows[1]
        count = max(0, len(rows) - 1)
    except Exception:
        count = 0
    return f"CO-{count + 1:03d}"


def add_change_order_row(sheets_svc, sheet_id, co):
    """Append a change order row to the Change Orders tab.

    co dict keys: co_number, date, description, section, cost_delta,
                  schedule_days, status, doc_link, notes
    """
    ensure_change_orders_tab(sheets_svc, sheet_id)
    row = [
        co.get("co_number", ""),
        co.get("date", ""),
        co.get("description", ""),
        co.get("section", ""),
        co.get("cost_delta", 0),
        co.get("schedule_days", 0),
        co.get("status", "Pending Approval"),
        co.get("doc_link", ""),
        co.get("notes", ""),
    ]
    sheets_svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range="Change Orders!A:I",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def update_cost_tracker_change_order(sheets_svc, sheet_id, section_name, delta):
    """Add delta to column C (Change Orders) for the given section.

    Finds the FIRST data row matching section_name in column A and adds delta
    to whatever is already in column C. Best-effort: logs and continues on miss.
    """
    try:
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Cost Tracker!A:C",
        ).execute()
        rows = result.get("values", [])
        # Find matching section row
        match_idx = None
        for i, row in enumerate(rows):
            if row and row[0].strip().lower() == section_name.strip().lower():
                match_idx = i
                break
        if match_idx is None:
            return  # section not found — silent no-op
        row_num = match_idx + 1  # 1-indexed for Sheets API
        # Read current C value
        col_c = rows[match_idx][2] if len(rows[match_idx]) > 2 else ""
        try:
            current = float(str(col_c).replace(",", "").replace("$", "")) if col_c else 0.0
        except ValueError:
            current = 0.0
        new_val = current + delta
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"Cost Tracker!C{row_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [[new_val]]},
        ).execute()
    except Exception:
        pass  # best-effort — don't crash the agent over a CO column miss


# ---------------------------------------------------------------------
# Finder
# ---------------------------------------------------------------------

def find_cost_tracker(drive_svc, folder_id, project_name):
    """Return the Cost Tracker sheet dict for project_name, or None."""
    sheet_name = f"{project_name} — Cost Tracker"
    query = (
        f"name='{sheet_name}' "
        f"and '{folder_id}' in parents "
        "and mimeType='application/vnd.google-apps.spreadsheet' "
        "and trashed=false"
    )
    files = drive_svc.files().list(
        q=query, fields="files(id,name,webViewLink)", pageSize=5
    ).execute().get("files", [])
    return files[0] if files else None


# ---------------------------------------------------------------------
# Cost Tracker summary — structured readout for hb-ask
# ---------------------------------------------------------------------
#
# When Chad asks "how much have I spent on Whitfield framing?" hb-ask
# could call read_drive_file on the Cost Tracker sheet and let Claude
# parse the raw CSV — but that costs ~$0.20-0.40 per question (large
# context) and Claude has to re-derive section totals every time.
#
# This function pre-aggregates the Cost Tracker into a clean structured
# summary: sections with their totals, line items with budget/actual/
# billed, and grand totals across the project. hb-ask's get_cost_tracker
# tool returns this — Claude reads structured data instead of CSV soup.
#
# Schema (from create_cost_tracker_sheet, ~line 232):
#   Row 1: TITLE
#   Row 2: column headers (Line Item / Budget / Change Orders / Revised /
#                          Actual / Difference / Billed / Sub-Vendor /
#                          Lien Waiver / Draw # / Notes)
#   Rows 3+: section headers (col A uppercase, B-K empty),
#            line items, subtotals (col A = "SUBTOTAL" or starts with TOTAL)

import re as _cost_re


def _is_section_header(row: list) -> bool:
    """Section headers are uppercase, with cols B-H (financial data) empty.

    Note: col I is the Lien Waiver checkbox which always renders as
    "TRUE" / "FALSE" — that's not real header content, so skip it.
    """
    if not row or not row[0]:
        return False
    first = str(row[0]).strip()
    if first != first.upper():
        return False
    if first.startswith(("TOTAL", "SUBTOTAL", "GRAND")):
        return False
    # Only inspect cols B-H (financial data) for "is this a header?"
    rest = " ".join(str(c) for c in row[1:8] if c)
    return rest.strip() == ""


def _is_subtotal_row(row: list) -> bool:
    if not row or not row[0]:
        return False
    first = str(row[0]).strip().upper()
    return first.startswith(("SUBTOTAL", "TOTAL"))


def _parse_currency(val) -> float | None:
    """Parse a Sheets cell value as a USD amount. Returns None if not a number."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    # Strip $, commas, parentheses
    s = s.replace("$", "").replace(",", "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def read_cost_tracker_summary(
    drive_svc, sheets_svc, folder_id: str, project_name: str
) -> dict | None:
    """Read a project's Cost Tracker and return a structured summary.

    Returns a dict shaped for direct use by Chad's chat UI / hb-ask:
        {
            "project_name": "Whitfield Residence",
            "cost_tracker_url": "https://...",
            "sections": [
                {
                    "name": "Permits & Fees",
                    "budget": 25000.0,
                    "actual": 18432.50,
                    "billed": 25000.0,
                    "diff_vs_budget": 6567.50,
                    "items": [
                        {"name": "Building permits:", "budget": 5000, "actual": 4250, "billed": 5000, "vendor": "..."},
                        ...
                    ],
                },
                ...
            ],
            "grand_totals": {
                "budget": 1234567.0,
                "actual": 543210.0,
                "billed": 800000.0,
                "diff_vs_budget": 691357.0,
                "pct_spent": 44.0,  # actual/budget * 100
            },
        }

    Returns None if the Cost Tracker doesn't exist for project_name.
    """
    tracker = find_cost_tracker(drive_svc, folder_id, project_name)
    if not tracker:
        return None

    res = sheets_svc.spreadsheets().values().get(
        spreadsheetId=tracker["id"],
        range="A1:K500",
    ).execute()
    rows = res.get("values", [])
    if not rows or len(rows) < 3:
        return {
            "project_name": project_name,
            "cost_tracker_url": tracker.get("webViewLink", ""),
            "sections": [],
            "grand_totals": {},
            "note": "Cost Tracker found but has no content yet",
        }

    sections: list[dict] = []
    current_section: dict | None = None
    grand_budget = 0.0
    grand_actual = 0.0
    grand_billed = 0.0

    # Skip rows 1-2 (title + headers)
    for row in rows[2:]:
        # Pad for safety
        padded = list(row) + [""] * (11 - len(row)) if len(row) < 11 else list(row)

        if _is_section_header(padded):
            # Open new section
            current_section = {
                "name": str(padded[0]).strip().title(),
                "items": [],
                "budget": 0.0,
                "actual": 0.0,
                "billed": 0.0,
                "diff_vs_budget": 0.0,
            }
            sections.append(current_section)
            continue

        if _is_subtotal_row(padded):
            # Subtotal closes the section. Use the row's totals directly
            # (they're computed by Sheets formulas).
            if current_section is not None:
                budget_sum = _parse_currency(padded[1])  # B Budget
                actual_sum = _parse_currency(padded[4])  # E Actual
                billed_sum = _parse_currency(padded[6])  # G Billed
                if budget_sum is not None:
                    current_section["budget"] = budget_sum
                    grand_budget += budget_sum
                if actual_sum is not None:
                    current_section["actual"] = actual_sum
                    grand_actual += actual_sum
                if billed_sum is not None:
                    current_section["billed"] = billed_sum
                    grand_billed += billed_sum
                current_section["diff_vs_budget"] = (
                    current_section["budget"] - current_section["actual"]
                )
            current_section = None  # close
            continue

        # Regular line item — must be inside an open section + must have a name
        name = str(padded[0]).strip() if padded[0] else ""
        if not name or current_section is None:
            continue

        # Skip the GRAND TOTAL or any totals-of-totals at the bottom
        if name.upper().startswith(("GRAND", "TOTAL")):
            continue

        item = {
            "name": name,
            "budget": _parse_currency(padded[1]),
            "change_orders": _parse_currency(padded[2]),
            "revised_budget": _parse_currency(padded[3]),
            "actual": _parse_currency(padded[4]),
            "billed": _parse_currency(padded[6]),
            "vendor": str(padded[7]).strip() if padded[7] else None,
            "notes": str(padded[10]).strip() if len(padded) > 10 and padded[10] else None,
        }
        current_section["items"].append(item)

    grand_diff = grand_budget - grand_actual
    pct_spent = round((grand_actual / grand_budget) * 100, 1) if grand_budget > 0 else 0.0

    return {
        "project_name": project_name,
        "cost_tracker_url": tracker.get("webViewLink", ""),
        "sections": sections,
        "grand_totals": {
            "budget": round(grand_budget, 2),
            "actual": round(grand_actual, 2),
            "billed": round(grand_billed, 2),
            "diff_vs_budget": round(grand_diff, 2),
            "pct_spent": pct_spent,
        },
    }


# ---------------------------------------------------------------------
# Lien Waivers tab
# ---------------------------------------------------------------------

LIEN_WAIVER_HEADERS = [
    "Date Filed", "Vendor", "Amount ($)", "Waiver Type",
    "Payment Date", "Payment Reference", "Notes",
]


def ensure_lien_waivers_tab(sheets_svc, sheet_id: str) -> int:
    """Find or create the Lien Waivers tab on the Cost Tracker. Returns its sheetId."""
    from home_builder_agent.config import LIEN_WAIVERS_TAB

    meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta.get("sheets", [])}

    if LIEN_WAIVERS_TAB in existing:
        return existing[LIEN_WAIVERS_TAB]

    resp = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": LIEN_WAIVERS_TAB}}}]},
    ).execute()
    tab_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{LIEN_WAIVERS_TAB}!A1",
        valueInputOption="RAW",
        body={"values": [LIEN_WAIVER_HEADERS]},
    ).execute()

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": tab_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
                {
                    "repeatCell": {
                        "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {"bold": True},
                                "backgroundColor": {"red": 0.18, "green": 0.18, "blue": 0.18},
                                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                            }
                        },
                        "fields": "userEnteredFormat(textFormat,backgroundColor,foregroundColor)",
                    }
                },
            ]
        },
    ).execute()

    return tab_id


def read_lien_waivers(sheets_svc, sheet_id: str) -> list[dict]:
    """Read all rows from Lien Waivers. Returns [] if tab doesn't exist."""
    from home_builder_agent.config import LIEN_WAIVERS_TAB
    try:
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{LIEN_WAIVERS_TAB}!A1:G500",
        ).execute()
    except Exception:
        return []

    rows = result.get("values", [])
    if not rows or len(rows) < 2:
        return []

    headers = rows[0]
    out = []
    for row in rows[1:]:
        padded = list(row) + [""] * (len(headers) - len(row))
        rec = dict(zip(headers, padded))
        if rec.get("Vendor", "").strip():
            out.append(rec)
    return out


def add_lien_waiver_row(sheets_svc, sheet_id: str, waiver: dict) -> None:
    """Append one waiver row. Creates the tab if needed.

    waiver keys: date_filed, vendor, amount, waiver_type,
                 payment_date, payment_reference, notes
    """
    from home_builder_agent.config import LIEN_WAIVERS_TAB
    ensure_lien_waivers_tab(sheets_svc, sheet_id)

    row = [
        waiver.get("date_filed", ""),
        waiver.get("vendor", ""),
        waiver.get("amount", ""),
        waiver.get("waiver_type", ""),
        waiver.get("payment_date", ""),
        waiver.get("payment_reference", ""),
        waiver.get("notes", ""),
    ]
    sheets_svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{LIEN_WAIVERS_TAB}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def read_actuals_log(sheets_svc, sheet_id: str) -> list[dict]:
    """Read all rows from the Actuals Log tab. Returns [] if tab doesn't exist."""
    try:
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Actuals Log!A1:F1000",
        ).execute()
    except Exception:
        return []

    rows = result.get("values", [])
    if not rows or len(rows) < 2:
        return []

    headers = rows[0]
    out = []
    for row in rows[1:]:
        padded = list(row) + [""] * (len(headers) - len(row))
        rec = dict(zip(headers, padded))
        if rec.get("Vendor", "").strip():
            out.append(rec)
    return out
