"""sheets.py — Google Sheets API helpers.

Three concerns:
  1. Building the 3-tab Tracker sheet from structured project data
  2. Dashboard tab management (ensure exists, write metrics view)
  3. Visual formatting (conditional row colors, date formats, bold headers)

Status vocabulary and color palette also live here — they're rendering details,
not business logic, so they don't belong in config.py.
"""

from googleapiclient.discovery import build

# ---------------------------------------------------------------------
# Status vocabulary + color palette
# ---------------------------------------------------------------------

STATUS_DONE = "done"
STATUS_IN_PROGRESS = "in progress"
STATUS_BLOCKED = "blocked"
STATUS_DELAYED = "delayed"
STATUS_NOT_STARTED = "not started"

# Functional status emojis per Chad's communication rules — used in dashboard
# headers and labels for at-a-glance scannability (NOT decorative).
STATUS_EMOJI = {
    STATUS_DONE: "🟢",
    STATUS_IN_PROGRESS: "🟡",
    STATUS_BLOCKED: "🔴",
    STATUS_DELAYED: "🟠",
    STATUS_NOT_STARTED: "⚪",
}

# Row background colors used by conditional formatting + the dashboard tab.
# Values are RGB 0.0–1.0 dicts as the Sheets API expects.
STATUS_ROW_COLOR = {
    STATUS_DONE:        {"red": 0.83, "green": 0.92, "blue": 0.83},  # green
    STATUS_IN_PROGRESS: {"red": 1.00, "green": 0.95, "blue": 0.78},  # yellow
    STATUS_BLOCKED:     {"red": 0.96, "green": 0.78, "blue": 0.76},  # red
    STATUS_DELAYED:     {"red": 0.99, "green": 0.90, "blue": 0.80},  # orange
}


def sheets_service(creds):
    """Build a Sheets v4 service."""
    return build("sheets", "v4", credentials=creds)


def normalize_status(status):
    """Lowercase + strip a Status field for safe comparison."""
    return (status or "").strip().lower()


# ---------------------------------------------------------------------
# Tracker sheet construction
# ---------------------------------------------------------------------

def build_tracker_sheet(creds, project_data, sheet_name, parent_folder_id):
    """Create a 3-tab Google Sheet from structured project data.

    Tabs:
      Master Schedule  — phases (one row per phase)
      Action Items     — tasks (with checkbox column)
      Order Schedule   — material orders (with status dropdown)

    Returns: dict with `id` and `webViewLink`.
    """
    sheets = sheets_service(creds)
    drive = build("drive", "v3", credentials=creds)

    # 1. Create the spreadsheet with three tabs
    spreadsheet_body = {
        "properties": {"title": sheet_name},
        "sheets": [
            {"properties": {"sheetId": 0, "title": "Master Schedule",
                            "gridProperties": {"frozenRowCount": 1}}},
            {"properties": {"sheetId": 1, "title": "Action Items",
                            "gridProperties": {"frozenRowCount": 1}}},
            {"properties": {"sheetId": 2, "title": "Order Schedule",
                            "gridProperties": {"frozenRowCount": 1}}},
        ],
    }
    sheet = sheets.spreadsheets().create(
        body=spreadsheet_body, fields="spreadsheetId,spreadsheetUrl"
    ).execute()
    sheet_id = sheet["spreadsheetId"]

    # 2. Move the sheet from My Drive root to the right folder
    drive.files().update(
        fileId=sheet_id,
        addParents=parent_folder_id,
        removeParents="root",
        fields="id, parents",
    ).execute()

    # 3. Build values for each tab
    phase_values = [["#", "Phase", "Weeks", "Start", "End",
                     "Status", "Dependencies"]]
    for p in project_data.get("phases", []):
        phase_values.append([
            p.get("number", ""),
            p.get("name", ""),
            p.get("duration_weeks", ""),
            p.get("target_start", ""),
            p.get("target_end", ""),
            p.get("status", "Not Started"),
            ", ".join(p.get("dependencies", [])) if p.get("dependencies") else "",
        ])

    task_values = [["Phase", "Task", "Done", "Target Date", "Owner", "Notes"]]
    for t in project_data.get("tasks", []):
        task_values.append([
            t.get("phase", ""),
            t.get("description", ""),
            "FALSE",  # boolean cell — accepts string
            t.get("target_date", ""),
            t.get("owner", "Chad"),
            t.get("notes", ""),
        ])

    order_values = [["Item", "Supplier", "Order By", "Lead Time (wks)",
                     "Status", "Arrived?", "Notes"]]
    for o in project_data.get("orders", []):
        order_values.append([
            o.get("item", ""),
            o.get("supplier", ""),
            o.get("order_by_date", ""),
            o.get("lead_time_weeks", ""),
            o.get("status", "Not Ordered"),
            "FALSE",
            o.get("notes", ""),
        ])

    # 4. Push values to each tab
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": [
                {"range": "Master Schedule!A1", "values": phase_values},
                {"range": "Action Items!A1",     "values": task_values},
                {"range": "Order Schedule!A1",   "values": order_values},
            ],
        },
    ).execute()

    # 5. Apply data validation + visual polish
    requests = []

    # ── Color palette ─────────────────────────────────────────────────────────
    HEADER_BG   = {"red": 0.13, "green": 0.23, "blue": 0.40}  # deep navy
    HEADER_TEXT = {"red": 1.00, "green": 1.00, "blue": 1.00}  # white
    BAND_ODD    = {"red": 0.94, "green": 0.96, "blue": 0.99}  # light blue-gray
    BAND_EVEN   = {"red": 1.00, "green": 1.00, "blue": 1.00}  # white
    BORDER_SM   = {"red": 0.70, "green": 0.72, "blue": 0.75}  # soft gray
    BORDER_MED  = {"red": 0.40, "green": 0.43, "blue": 0.48}  # darker header bottom

    def _border(color, style="SOLID", width=1):
        return {"style": style, "width": width, "colorStyle": {"rgbColor": color}}

    # ── Headers: navy BG, white bold text, 30px tall ──────────────────────────
    for sid in (0, 1, 2):
        requests.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": HEADER_BG,
                "textFormat": {"bold": True, "foregroundColor": HEADER_TEXT,
                               "fontSize": 10},
                "verticalAlignment": "MIDDLE",
                "padding": {"top": 4, "bottom": 4, "left": 8, "right": 8},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,padding)",
        }})
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 30},
            "fields": "pixelSize",
        }})

    # ── Tab colors ────────────────────────────────────────────────────────────
    tab_colors = {
        0: {"red": 0.18, "green": 0.49, "blue": 0.34},  # green  — Master Schedule
        1: {"red": 0.93, "green": 0.58, "blue": 0.11},  # amber  — Action Items
        2: {"red": 0.60, "green": 0.15, "blue": 0.60},  # purple — Order Schedule
    }
    for sid, color in tab_colors.items():
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "tabColorStyle": {"rgbColor": color}},
            "fields": "tabColorStyle",
        }})

    # ── Banded rows ───────────────────────────────────────────────────────────
    for sid, row_end in ((0, 20), (1, 80), (2, 30)):
        requests.append({"addBanding": {"bandedRange": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": row_end,
                      "startColumnIndex": 0, "endColumnIndex": 7},
            "rowProperties": {
                "headerColor": HEADER_BG,
                "firstBandColor": BAND_ODD,
                "secondBandColor": BAND_EVEN,
            },
        }}})

    # ── Borders ───────────────────────────────────────────────────────────────
    for sid, row_end, col_end in ((0, 20, 7), (1, 80, 6), (2, 30, 7)):
        requests.append({"updateBorders": {
            "range": {"sheetId": sid, "startRowIndex": 0,
                      "endRowIndex": row_end, "startColumnIndex": 0,
                      "endColumnIndex": col_end},
            "top":             _border(BORDER_SM),
            "bottom":          _border(BORDER_SM),
            "left":            _border(BORDER_SM),
            "right":           _border(BORDER_SM),
            "innerHorizontal": _border(BORDER_SM),
            "innerVertical":   _border(BORDER_SM),
        }})
        requests.append({"updateBorders": {
            "range": {"sheetId": sid, "startRowIndex": 0,
                      "endRowIndex": 1, "startColumnIndex": 0,
                      "endColumnIndex": col_end},
            "bottom": _border(BORDER_MED, style="SOLID_MEDIUM", width=2),
        }})

    # ── Freeze first column on Action Items ───────────────────────────────────
    requests.append({"updateSheetProperties": {
        "properties": {"sheetId": 1,
                       "gridProperties": {"frozenRowCount": 1,
                                          "frozenColumnCount": 1}},
        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
    }})

    # ── Column widths ─────────────────────────────────────────────────────────
    col_widths = {
        0: [40, 200, 60, 110, 110, 120, 220],    # Master: #, Phase, Wks, Start, End, Status, Deps
        1: [140, 300, 55, 110, 80, 220],          # Actions: Phase, Task, Done, Date, Owner, Notes
        2: [220, 160, 110, 90, 130, 75, 220],     # Orders: Item, Supplier, By, Lead, Status, Arr, Notes
    }
    for sid, widths in col_widths.items():
        for i, px in enumerate(widths):
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }})

    # ── Text wrapping: WRAP data cells, CLIP headers ─────────────────────────
    for sid, row_end, col_end in ((0, 20, 7), (1, 80, 6), (2, 30, 7)):
        # Data rows: wrap + align to top
        requests.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1,
                      "endRowIndex": row_end, "startColumnIndex": 0,
                      "endColumnIndex": col_end},
            "cell": {"userEnteredFormat": {
                "wrapStrategy": "WRAP",
                "verticalAlignment": "TOP",
            }},
            "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
        }})
        # Header row: clip so it never wraps
        requests.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": col_end},
            "cell": {"userEnteredFormat": {
                "wrapStrategy": "CLIP",
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
        }})

    # ── Row heights: auto-size to content so wrapped text shows ──────────────
    for sid in (0, 1, 2):
        requests.append({"autoResizeDimensions": {
            "dimensions": {"sheetId": sid, "dimension": "ROWS",
                           "startIndex": 1, "endIndex": 80},
        }})

    # ── Data validation: Status dropdown Master Schedule col F (index 5) ──────
    requests.append({
        "setDataValidation": {
            "range": {"sheetId": 0, "startRowIndex": 1,
                      "startColumnIndex": 5, "endColumnIndex": 6},
            "rule": {
                "condition": {"type": "ONE_OF_LIST", "values": [
                    {"userEnteredValue": v} for v in
                    ["Not Started", "In Progress", "Done", "Blocked", "Delayed"]
                ]},
                "showCustomUi": True,
                "strict": False,
            }
        }
    })

    # ── Done checkbox on Action Items col C (index 2) ─────────────────────────
    requests.append({
        "setDataValidation": {
            "range": {"sheetId": 1, "startRowIndex": 1,
                      "startColumnIndex": 2, "endColumnIndex": 3},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True}
        }
    })

    # ── Order status dropdown on Order Schedule col E (index 4) ──────────────
    requests.append({
        "setDataValidation": {
            "range": {"sheetId": 2, "startRowIndex": 1,
                      "startColumnIndex": 4, "endColumnIndex": 5},
            "rule": {
                "condition": {"type": "ONE_OF_LIST", "values": [
                    {"userEnteredValue": v} for v in
                    ["Not Ordered", "Ordered", "In Production",
                     "Shipped", "Delivered"]
                ]},
                "showCustomUi": True,
                "strict": False,
            }
        }
    })

    # ── Arrived checkbox on Order Schedule col F (index 5) ───────────────────
    requests.append({
        "setDataValidation": {
            "range": {"sheetId": 2, "startRowIndex": 1,
                      "startColumnIndex": 5, "endColumnIndex": 6},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True}
        }
    })

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests}
    ).execute()

    return {"id": sheet_id, "webViewLink": sheet["spreadsheetUrl"]}


# ---------------------------------------------------------------------
# Master Schedule reads + writes
# ---------------------------------------------------------------------

def read_master_schedule(sheets_svc, sheet_id):
    """Read Master Schedule tab. Return list of phase dicts (one per row)."""
    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="Master Schedule!A1:G200",
    ).execute()
    rows = result.get("values", [])
    if not rows or len(rows) < 2:
        return []

    headers = rows[0]
    phases = []
    for row in rows[1:]:
        # Pad row to header length so missing trailing cells don't break dict
        padded = list(row) + [""] * (len(headers) - len(row))
        phase = dict(zip(headers, padded))
        if phase.get("Phase", "").strip():
            phases.append(phase)
    return phases


def compute_dashboard_metrics(phases, today=None):
    """Compute the dashboard metrics dict from a list of phase rows.

    Pure function — no I/O, no API calls. Takes the phase rows produced by
    read_master_schedule() and returns the dict that write_dashboard() expects.

    Lives here (in integrations/sheets) rather than in core because the dict
    keys mirror the Master Schedule column names exactly — moving sheet
    schema = updating this in one place.
    """
    from datetime import date as _date  # local to avoid module-level dep

    if today is None:
        today = _date.today()

    # Current phase = first non-Done phase
    current_idx = None
    for i, p in enumerate(phases):
        if normalize_status(p.get("Status")) != STATUS_DONE:
            current_idx = i
            break

    current = phases[current_idx] if current_idx is not None else None
    upcoming = (
        phases[current_idx + 1]
        if (current_idx is not None and current_idx + 1 < len(phases))
        else None
    )

    n_total = len(phases)
    n_done = sum(
        1 for p in phases if normalize_status(p.get("Status")) == STATUS_DONE
    )
    n_in_progress = sum(
        1 for p in phases
        if normalize_status(p.get("Status")) == STATUS_IN_PROGRESS
    )
    n_blocked = sum(
        1 for p in phases
        if normalize_status(p.get("Status")) == STATUS_BLOCKED
    )
    n_delayed = sum(
        1 for p in phases
        if normalize_status(p.get("Status")) == STATUS_DELAYED
    )

    pct_complete = (
        round((n_done + n_in_progress * 0.5) / n_total * 100, 0)
        if n_total else 0
    )

    original_completion = phases[-1].get("End", "") if phases else ""
    # Stage 1 doesn't compute revised completion (no cascade logic in the
    # READ path — that lives in the status updater). Show same value.
    revised_completion = original_completion

    current_status = current.get("Status", "") if current else ""
    current_status_emoji = STATUS_EMOJI.get(
        normalize_status(current_status), "⚪"
    )

    return {
        "today": today.isoformat(),
        "current_stage": current.get("Phase", "") if current else "Project complete",
        "current_status": current_status,
        "current_status_emoji": current_status_emoji,
        "current_start": current.get("Start", "") if current else "",
        "current_end": current.get("End", "") if current else "",
        "upcoming_stage": upcoming.get("Phase", "—") if upcoming else "—",
        "upcoming_start": upcoming.get("Start", "") if upcoming else "",
        "upcoming_end": upcoming.get("End", "") if upcoming else "",
        "pct_complete": pct_complete,
        "n_total_phases": n_total,
        "n_done_phases": n_done,
        "n_in_progress_phases": n_in_progress,
        "n_blocked_phases": n_blocked,
        "n_delayed_phases": n_delayed,
        "original_completion": original_completion,
        "revised_completion": revised_completion,
    }


def apply_phase_updates(sheets_svc, sheet_id, updates):
    """Write a list of (idx, phase_dict) updates back to Master Schedule.

    Master Schedule columns (in order): #, Phase, Weeks, Start, End, Status, Dependencies
    """
    if not updates:
        return

    data = []
    for phase_idx, phase in updates:
        # Header is row 1; phase data starts at row 2 (idx 0 = row 2)
        row_num = phase_idx + 2
        row_values = [
            phase.get("#", ""),
            phase.get("Phase", ""),
            phase.get("Weeks", ""),
            phase.get("Start", ""),
            phase.get("End", ""),
            phase.get("Status", ""),
            phase.get("Dependencies", ""),
        ]
        data.append({
            "range": f"Master Schedule!A{row_num}:G{row_num}",
            "values": [row_values],
        })

    sheets_svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()


# ---------------------------------------------------------------------
# Dashboard tab
# ---------------------------------------------------------------------

def ensure_dashboard_tab(sheets_svc, sheet_id):
    """Add a Dashboard tab if missing. Returns its sheetId either way."""
    spreadsheet = sheets_svc.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties"
    ).execute()

    for s in spreadsheet["sheets"]:
        if s["properties"]["title"] == "Dashboard":
            return s["properties"]["sheetId"]

    # Create at index 0 so it's what Chad sees first
    request = {
        "addSheet": {
            "properties": {
                "title": "Dashboard",
                "index": 0,
                "gridProperties": {"rowCount": 50, "columnCount": 4},
            }
        }
    }
    response = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": [request]}
    ).execute()
    return response["replies"][0]["addSheet"]["properties"]["sheetId"]


def write_dashboard(sheets_svc, sheet_id, dashboard_sheet_id, metrics, project_name):
    """Write the dashboard view to the Dashboard tab with formatting.

    `metrics` shape comes from compute_dashboard_metrics() — see the dashboard
    refresher agent for the dict schema.
    """
    pct = int(metrics["pct_complete"])
    pct_bar = _progress_bar_text(pct)
    status_with_emoji = (
        f"{metrics['current_status_emoji']} {metrics['current_status']}"
        if metrics['current_status'] else metrics['current_status_emoji']
    )

    layout = [
        [f"PROJECT DASHBOARD — {project_name}", "", "", ""],
        ["Last updated", metrics["today"], "", ""],
        ["", "", "", ""],
        ["CURRENT STAGE", "", "", ""],
        ["Phase", metrics["current_stage"], "", ""],
        ["Status", status_with_emoji, "", ""],
        ["Started", metrics["current_start"], "", ""],
        ["Phase ends", metrics["current_end"], "", ""],
        ["", "", "", ""],
        ["UPCOMING STAGE", "", "", ""],
        ["Next phase", metrics["upcoming_stage"], "", ""],
        ["Starts", metrics["upcoming_start"], "", ""],
        ["", "", "", ""],
        ["PROGRESS", "", "", ""],
        ["% Complete", f"{pct}%", pct_bar, ""],
        ["Phases complete",
         f"{metrics['n_done_phases']} of {metrics['n_total_phases']}", "", ""],
        ["🟡 Phases in progress", metrics["n_in_progress_phases"], "", ""],
        ["🔴 Phases blocked", metrics["n_blocked_phases"], "", ""],
        ["🟠 Phases delayed", metrics["n_delayed_phases"], "", ""],
        ["", "", "", ""],
        ["COMPLETION TARGET", "", "", ""],
        ["Original completion", metrics["original_completion"], "", ""],
        ["Revised completion", metrics["revised_completion"], "", ""],
        ["", "", "", ""],
        ["NOTES", "", "", ""],
        ["", "Update phase Status dropdowns on Master Schedule tab; the watcher",
         "", ""],
        ["", "auto-refreshes this view within 60 seconds. For natural-language",
         "", ""],
        ["", "updates that auto-cascade through the schedule, run", "", ""],
        ["", "  hb-update \"Phase 3 pushed 1 week\"", "", ""],
    ]

    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="Dashboard!A1",
        valueInputOption="USER_ENTERED",
        body={"values": layout},
    ).execute()

    requests = []

    # Title (row 0): bold, large, dark blue background, white text
    requests.append({
        "repeatCell": {
            "range": {"sheetId": dashboard_sheet_id,
                      "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "textFormat": {
                    "bold": True, "fontSize": 14,
                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                },
                "backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.6},
            }},
            "fields": "userEnteredFormat(textFormat,backgroundColor)",
        }
    })

    # Section header rows: bold, light green
    section_rows = [3, 9, 13, 20, 24]  # 0-indexed
    for row in section_rows:
        requests.append({
            "repeatCell": {
                "range": {"sheetId": dashboard_sheet_id,
                          "startRowIndex": row, "endRowIndex": row + 1,
                          "startColumnIndex": 0, "endColumnIndex": 4},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.85, "green": 0.92, "blue": 0.83},
                }},
                "fields": "userEnteredFormat(textFormat,backgroundColor)",
            }
        })

    # Label column for value rows: bold
    label_rows = [1, 4, 5, 6, 7, 10, 11, 14, 15, 16, 17, 18, 21, 22]
    for row in label_rows:
        requests.append({
            "repeatCell": {
                "range": {"sheetId": dashboard_sheet_id,
                          "startRowIndex": row, "endRowIndex": row + 1,
                          "startColumnIndex": 0, "endColumnIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat",
            }
        })

    # Auto-resize columns + extra width on label column
    requests.append({
        "autoResizeDimensions": {
            "dimensions": {"sheetId": dashboard_sheet_id,
                           "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": 4},
        }
    })
    requests.append({
        "updateDimensionProperties": {
            "range": {"sheetId": dashboard_sheet_id,
                      "dimension": "COLUMNS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 200},
            "fields": "pixelSize",
        }
    })

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests}
    ).execute()


def _progress_bar_text(pct, width=20):
    """Render a text-based progress bar like '████░░░░░░░░░░░░░░░░ 20%'."""
    pct_int = max(0, min(100, int(pct)))
    filled = int(pct_int * width / 100)
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------
# Visual formatting (conditional row colors, date formats)
# ---------------------------------------------------------------------

def apply_visual_formatting(sheets_svc, sheet_id):
    """Apply conditional row colors + date number formats to data tabs.

    Idempotent: clears existing conditional rules on each tab before adding,
    so re-runs (via the watcher) don't accumulate duplicate rules.

    Returns the count of formatting requests sent (for log clarity).
    """
    spreadsheet = sheets_svc.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets(properties(sheetId,title),conditionalFormats)",
    ).execute()

    tab_ids = {}
    rule_counts = {}
    for s in spreadsheet["sheets"]:
        title = s["properties"]["title"]
        tab_ids[title] = s["properties"]["sheetId"]
        rule_counts[title] = len(s.get("conditionalFormats", []))

    requests = []

    # Clear existing rules — delete index 0 repeatedly, each delete shifts
    # remaining rules down by one.
    for title in ("Master Schedule", "Order Schedule", "Action Items"):
        if title in tab_ids:
            tid = tab_ids[title]
            for _ in range(rule_counts.get(title, 0)):
                requests.append({
                    "deleteConditionalFormatRule": {"sheetId": tid, "index": 0}
                })

    # Master Schedule — color whole row by Status (column F = index 5)
    if "Master Schedule" in tab_ids:
        ms_id = tab_ids["Master Schedule"]
        for status_label in ("Done", "In Progress", "Blocked", "Delayed"):
            color = STATUS_ROW_COLOR[status_label.lower()]
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": ms_id,
                            "startRowIndex": 1, "endRowIndex": 100,
                            "startColumnIndex": 0, "endColumnIndex": 7,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue":
                                            f'=$F2="{status_label}"'}],
                            },
                            "format": {"backgroundColor": color},
                        },
                    },
                    "index": 0,
                }
            })
        # Date format on Start (D=3) and End (E=4) columns
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": ms_id,
                    "startRowIndex": 1, "endRowIndex": 100,
                    "startColumnIndex": 3, "endColumnIndex": 5,
                },
                "cell": {"userEnteredFormat": {
                    "numberFormat": {"type": "DATE", "pattern": "mmm dd, yyyy"}
                }},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    # Order Schedule — color by Status column E (index 4)
    if "Order Schedule" in tab_ids:
        os_id = tab_ids["Order Schedule"]
        order_status_colors = [
            ("Delivered",     STATUS_ROW_COLOR[STATUS_DONE]),
            ("Ordered",       STATUS_ROW_COLOR[STATUS_IN_PROGRESS]),
            ("In Production", STATUS_ROW_COLOR[STATUS_IN_PROGRESS]),
            ("Shipped",       STATUS_ROW_COLOR[STATUS_IN_PROGRESS]),
        ]
        for status_label, color in order_status_colors:
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": os_id,
                            "startRowIndex": 1, "endRowIndex": 200,
                            "startColumnIndex": 0, "endColumnIndex": 7,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue":
                                            f'=$E2="{status_label}"'}],
                            },
                            "format": {"backgroundColor": color},
                        },
                    },
                    "index": 0,
                }
            })
        # Date format on Order By (C=2)
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": os_id,
                    "startRowIndex": 1, "endRowIndex": 200,
                    "startColumnIndex": 2, "endColumnIndex": 3,
                },
                "cell": {"userEnteredFormat": {
                    "numberFormat": {"type": "DATE", "pattern": "mmm dd, yyyy"}
                }},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    # Action Items — color whole row when Done checkbox (col C=2) is TRUE
    if "Action Items" in tab_ids:
        ai_id = tab_ids["Action Items"]
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": ai_id,
                        "startRowIndex": 1, "endRowIndex": 500,
                        "startColumnIndex": 0, "endColumnIndex": 6,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": "=$C2=TRUE"}],
                        },
                        "format": {
                            "backgroundColor": STATUS_ROW_COLOR[STATUS_DONE],
                            "textFormat": {"strikethrough": True},
                        },
                    },
                },
                "index": 0,
            }
        })
        # Date format on Target Date (D=3)
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": ai_id,
                    "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": 3, "endColumnIndex": 4,
                },
                "cell": {"userEnteredFormat": {
                    "numberFormat": {"type": "DATE", "pattern": "mmm dd, yyyy"}
                }},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    if requests:
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": requests}
        ).execute()

    return len(requests)
