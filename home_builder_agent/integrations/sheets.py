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
                # No headerColor — row 1 is styled separately; headerColor
                # would re-color row 2 (the first banded row) navy.
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


def read_order_schedule(sheets_svc, sheet_id):
    """Read Order Schedule tab. Return list of order dicts (one per row)."""
    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="Order Schedule!A1:G200",
    ).execute()
    rows = result.get("values", [])
    if not rows or len(rows) < 2:
        return []

    headers = rows[0]
    orders = []
    for row in rows[1:]:
        padded = list(row) + [""] * (len(headers) - len(row))
        order = dict(zip(headers, padded))
        if order.get("Item", "").strip():
            orders.append(order)
    return orders


def compute_dashboard_metrics(phases, orders=None, today=None):
    """Compute the dashboard metrics dict from a list of phase rows.

    Pure function — no I/O, no API calls. Takes the phase rows produced by
    read_master_schedule() and optionally the order rows from
    read_order_schedule(). Returns the dict that write_dashboard() expects.

    The `orders` param is optional for backward compatibility — callers that
    don't pass it get the same metrics as before, minus order-aware fields.

    Lives here (in integrations/sheets) rather than in core because the dict
    keys mirror the Master Schedule column names exactly — moving sheet
    schema = updating this in one place.
    """
    from datetime import date as _date, datetime as _dt

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
    revised_completion = original_completion

    current_status = current.get("Status", "") if current else ""
    current_status_emoji = STATUS_EMOJI.get(
        normalize_status(current_status), "⚪"
    )

    # ── Named issue lists — what needs attention by name, not just count ──────
    blocked_phases = [
        p.get("Phase", "") for p in phases
        if normalize_status(p.get("Status")) == STATUS_BLOCKED
    ]
    delayed_phases = [
        p.get("Phase", "") for p in phases
        if normalize_status(p.get("Status")) == STATUS_DELAYED
    ]

    # ── Order-aware fields ────────────────────────────────────────────────────
    overdue_orders = []
    due_soon_orders = []
    if orders:
        inactive_statuses = {"delivered", "ordered", "in production", "shipped"}
        for o in orders:
            order_by_str = (o.get("Order By") or "").strip()
            status = normalize_status(o.get("Status", ""))
            if not order_by_str or status in inactive_statuses:
                continue
            try:
                order_by = _dt.strptime(order_by_str, "%b %d, %Y").date()
                days_until = (order_by - today).days
                entry = {
                    "item": o.get("Item", ""),
                    "supplier": o.get("Supplier", ""),
                    "order_by": order_by_str,
                }
                if days_until < 0:
                    overdue_orders.append({**entry, "days_overdue": abs(days_until)})
                elif days_until <= 14:
                    due_soon_orders.append({**entry, "days_until": days_until})
            except ValueError:
                pass

    # ── Overall health ────────────────────────────────────────────────────────
    if blocked_phases or overdue_orders:
        health, health_emoji = "ACTION NEEDED", "🔴"
    elif delayed_phases or due_soon_orders:
        health, health_emoji = "WATCH", "🟡"
    else:
        health, health_emoji = "ON TRACK", "🟢"

    # ── Next action — one sentence, most important thing to do right now ───────
    if blocked_phases:
        next_action = (
            f"Resolve block on {blocked_phases[0]} — "
            "this phase is stopping the schedule from moving forward"
        )
    elif overdue_orders:
        o = overdue_orders[0]
        next_action = (
            f"Place order immediately: {o['item']} from {o['supplier']} — "
            f"Order By date has passed ({o['order_by']})"
        )
    elif delayed_phases:
        next_action = (
            f"Review delay on {delayed_phases[0]} and assess impact on "
            f"{original_completion} completion deadline"
        )
    elif due_soon_orders:
        o = due_soon_orders[0]
        next_action = (
            f"Order {o['item']} from {o['supplier']} within "
            f"{o['days_until']} days — lead time window is closing"
        )
    elif current:
        # Parse current phase start date to give a meaningful prompt
        try:
            start_dt = _dt.strptime(current.get("Start", ""), "%b %d, %Y").date()
            days_to_start = (start_dt - today).days
            phase_name = current.get("Phase", "current phase")
            if days_to_start <= 0:
                next_action = (
                    f"{phase_name} is active — check in with your sub "
                    "for a progress update"
                )
            elif days_to_start <= 7:
                next_action = (
                    f"{phase_name} starts in {days_to_start} days — "
                    "confirm subs are scheduled and materials are ready"
                )
            else:
                next_action = (
                    f"Prepare for {phase_name} starting "
                    f"{current.get('Start', '')} — all systems green"
                )
        except (ValueError, TypeError):
            next_action = (
                f"Review {current.get('Phase', 'current phase')} "
                "and confirm next steps with your sub"
            )
    else:
        next_action = "All phases complete — project is in wrap-up"

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
        # Actionable fields
        "blocked_phases": blocked_phases,
        "delayed_phases": delayed_phases,
        "overdue_orders": overdue_orders,
        "due_soon_orders": due_soon_orders,
        "health": health,
        "health_emoji": health_emoji,
        "next_action": next_action,
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
    """Write the dashboard view to the Dashboard tab.

    Designed around the first 10 seconds: what changed → what matters →
    what needs attention → what to do next. Four zones, top to bottom.

    Layout (6 columns A–F, 26 rows):
      R0:  Title bar + health badge
      R1:  Subtitle (job code, last updated)
      R2:  spacer
      R3–5: KPI strip (Days out / Current Phase / % Done / Phases / Issues)
      R6:  spacer
      R7–10: Attention zone (green if clear, amber/red if issues)
      R11: spacer
      R12–13: Next Action (navy header + bold sentence)
      R14: spacer
      R15–18: Phase context (current / upcoming / completion)
      R19: spacer
      R20–21: Build progress bar
      R22: spacer
      R23–24: Phase status count boxes
      R25: footer hint
    """
    from datetime import date as _date, datetime as _dt

    D = dashboard_sheet_id
    pct = int(metrics["pct_complete"])
    today = _date.today()

    # ── Derived display values ────────────────────────────────────────────────
    try:
        compl_dt = _dt.strptime(metrics["original_completion"], "%b %d, %Y").date()
        days_out = (compl_dt - today).days
        days_str = str(max(0, days_out))
        compl_label = f"to {compl_dt.strftime('%b %Y')}"
    except (ValueError, TypeError):
        days_out = None
        days_str = "—"
        compl_label = ""

    health       = metrics.get("health", "ON TRACK")
    health_emoji = metrics.get("health_emoji", "🟢")
    next_action  = metrics.get("next_action", "Review current phase with your sub")
    n_not_started = (
        metrics["n_total_phases"] - metrics["n_done_phases"]
        - metrics["n_in_progress_phases"] - metrics["n_blocked_phases"]
        - metrics["n_delayed_phases"]
    )
    issue_count = metrics["n_blocked_phases"] + metrics["n_delayed_phases"]

    current_status_display = (
        f"{metrics['current_status_emoji']} {metrics['current_status']}"
        if metrics.get("current_status") else "⚪ Not Started"
    )
    phase_sub = (
        f"{current_status_display}  ·  "
        f"Phase {metrics['n_done_phases'] + 1} of {metrics['n_total_phases']}"
    )

    # ── Attention items (up to 3, padded to exactly 3 for stable layout) ─────
    items = []
    for ph in metrics.get("blocked_phases", []):
        items.append(f"   🔴  {ph} is BLOCKED — resolve before schedule can advance")
    for ph in metrics.get("delayed_phases", []):
        items.append(f"   🟠  {ph} is DELAYED — assess impact on deadline")
    for o in metrics.get("overdue_orders", [])[:2]:
        items.append(
            f"   ⚠   Order {o['item']} from {o['supplier']} — "
            f"Order By date has passed"
        )
    for o in metrics.get("due_soon_orders", [])[:1]:
        items.append(
            f"   📋  Order {o['item']} from {o['supplier']} — "
            f"due in {o['days_until']} days"
        )

    if items:
        attn_header = (
            f"   ⚠   {len(items)} ITEM{'S' if len(items) > 1 else ''} "
            f"NEED{'S' if len(items) == 1 else ''} ATTENTION"
        )
        attn_bg = {"red": 0.78, "green": 0.38, "blue": 0.04}
    else:
        attn_header = "   ✅   ALL CLEAR — no issues today"
        attn_bg     = {"red": 0.11, "green": 0.50, "blue": 0.27}
        items = [""]

    while len(items) < 3:
        items.append("")
    items = items[:3]

    pct_bar = _progress_bar_text(pct, width=54)

    # ── Values (26 rows × 6 cols) ─────────────────────────────────────────────
    layout = [
        # R0 title + health badge
        [project_name.upper(), "", "", "", f"{health_emoji}  {health}", ""],
        # R1 subtitle
        [f"Palmetto Custom Homes  ·  PCH-2026-007  ·  Fairhope, AL",
         "", "", "", f"Refreshed {metrics['today']}", ""],
        # R2 spacer
        [""] * 6,
        # R3 KPI labels
        ["DAYS TO DEADLINE", "CURRENT PHASE", "", "% DONE", "PHASES DONE", "ISSUES"],
        # R4 KPI values
        [days_str, metrics["current_stage"], "",
         f"{pct}%",
         f"{metrics['n_done_phases']} / {metrics['n_total_phases']}",
         str(issue_count)],
        # R5 KPI descriptors
        [compl_label, phase_sub, "", "complete", "done", "blocked or delayed"],
        # R6 spacer
        [""] * 6,
        # R7 attention header
        [attn_header, "", "", "", "", ""],
        # R8–R10 attention items
        [items[0], "", "", "", "", ""],
        [items[1], "", "", "", "", ""],
        [items[2], "", "", "", "", ""],
        # R11 spacer
        [""] * 6,
        # R12 next action header
        ["   ▶   YOUR NEXT ACTION", "", "", "", "", ""],
        # R13 next action text
        [f"   {next_action}", "", "", "", "", ""],
        # R14 spacer
        [""] * 6,
        # R15 phase context labels
        ["CURRENT PHASE", "", "UP NEXT", "", "COMPLETION", ""],
        # R16 phase names
        [metrics["current_stage"], "", metrics.get("upcoming_stage", "—"),
         "", metrics["original_completion"], ""],
        # R17 dates
        [f"{metrics['current_start']} → {metrics['current_end']}", "",
         f"Starts {metrics.get('upcoming_start', '—')}", "",
         f"Firm deadline", ""],
        # R18 status / days out
        [current_status_display, "", "—", "",
         f"{days_str} days out" if days_str != "—" else "—", ""],
        # R19 spacer
        [""] * 6,
        # R20 progress header
        ["BUILD PROGRESS", "",
         f"{pct}%  ·  {metrics['n_done_phases']} of "
         f"{metrics['n_total_phases']} phases complete",
         "", "", ""],
        # R21 progress bar
        [pct_bar, "", "", "", "", ""],
        # R22 spacer
        [""] * 6,
        # R23 count labels
        ["⚪  NOT STARTED", "✅  DONE", "🟡  IN PROGRESS",
         "🔴  BLOCKED", "🟠  DELAYED", "TOTAL"],
        # R24 count values
        [str(n_not_started), str(metrics["n_done_phases"]),
         str(metrics["n_in_progress_phases"]), str(metrics["n_blocked_phases"]),
         str(metrics["n_delayed_phases"]), str(metrics["n_total_phases"])],
        # R25 footer
        ["Update Status on Master Schedule tab — auto-refreshes every 60s  "
         "·  hb-update \"[phase] complete\" for natural-language cascades",
         "", "", "", "", ""],
    ]

    # ── Clear existing content + unmerge ─────────────────────────────────────
    sheets_svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range="Dashboard!A1:Z50"
    ).execute()

    meta = sheets_svc.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets(properties(sheetId),merges)"
    ).execute()
    unmerge_reqs = []
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == D:
            for m in s.get("merges", []):
                unmerge_reqs.append({"unmergeCells": {"range": m}})
    if unmerge_reqs:
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": unmerge_reqs}
        ).execute()

    # ── Write values ──────────────────────────────────────────────────────────
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="Dashboard!A1",
        valueInputOption="USER_ENTERED",
        body={"values": layout},
    ).execute()

    # ── Formatting batch ──────────────────────────────────────────────────────
    # Color palette — restrained: one neutral base, one accent, semantic only
    NAVY      = {"red": 0.13, "green": 0.23, "blue": 0.40}
    NAVY_DK   = {"red": 0.22, "green": 0.35, "blue": 0.55}
    NAVY_TEXT = {"red": 0.10, "green": 0.18, "blue": 0.32}
    WHITE     = {"red": 1.00, "green": 1.00, "blue": 1.00}
    LGRAY_BG  = {"red": 0.95, "green": 0.96, "blue": 0.97}
    MID_GRAY  = {"red": 0.55, "green": 0.55, "blue": 0.55}
    DARK_GRAY = {"red": 0.30, "green": 0.30, "blue": 0.30}
    DIVIDER   = {"red": 0.88, "green": 0.90, "blue": 0.93}
    ATTN_ITEM_BG = ({"red": 0.99, "green": 0.96, "blue": 0.92}
                    if items[0] else WHITE)
    # Status box colors — only used for the count strip
    BOX_GRAY   = {"red": 0.60, "green": 0.62, "blue": 0.65}
    BOX_GREEN  = {"red": 0.13, "green": 0.50, "blue": 0.28}
    BOX_AMBER  = {"red": 0.80, "green": 0.55, "blue": 0.08}
    BOX_RED    = {"red": 0.75, "green": 0.16, "blue": 0.16}
    BOX_ORANGE = {"red": 0.78, "green": 0.38, "blue": 0.06}
    # Health badge color
    if health == "ON TRACK":
        health_bg = BOX_GREEN
    elif health == "WATCH":
        health_bg = BOX_AMBER
    else:
        health_bg = BOX_RED
    # Issues number color
    issue_color = BOX_RED if issue_count > 0 else BOX_GREEN

    reqs = []

    def rng(r1, r2, c1, c2):
        return {"sheetId": D, "startRowIndex": r1, "endRowIndex": r2,
                "startColumnIndex": c1, "endColumnIndex": c2}

    def cell_fmt(r1, r2, c1, c2, **kw):
        return {"repeatCell": {
            "range": rng(r1, r2, c1, c2),
            "cell": {"userEnteredFormat": kw},
            "fields": "userEnteredFormat(" + ",".join(kw) + ")",
        }}

    def mrg(r1, r2, c1, c2):
        return {"mergeCells": {"range": rng(r1, r2, c1, c2),
                               "mergeType": "MERGE_ALL"}}

    def row_h(r, px):
        return {"updateDimensionProperties": {
            "range": {"sheetId": D, "dimension": "ROWS",
                      "startIndex": r, "endIndex": r + 1},
            "properties": {"pixelSize": px}, "fields": "pixelSize"}}

    def col_w(c, px):
        return {"updateDimensionProperties": {
            "range": {"sheetId": D, "dimension": "COLUMNS",
                      "startIndex": c, "endIndex": c + 1},
            "properties": {"pixelSize": px}, "fields": "pixelSize"}}

    def hdivider(r):
        return {"updateBorders": {
            "range": rng(r, r + 1, 0, 6),
            "bottom": {"style": "SOLID", "width": 1,
                       "colorStyle": {"rgbColor": DIVIDER}}}}

    # ── Grid: 6 cols, freeze top 2 rows, nav tab color ────────────────────────
    reqs += [
        {"updateSheetProperties": {
            "properties": {"sheetId": D,
                           "gridProperties": {"columnCount": 6,
                                              "frozenRowCount": 2},
                           "tabColorStyle": {"rgbColor": NAVY}},
            "fields": "gridProperties.columnCount,"
                      "gridProperties.frozenRowCount,tabColorStyle"}},
    ]

    # Column widths: A=140 B=200 C=150 D=110 E=140 F=110
    for c, px in enumerate([140, 200, 150, 110, 140, 110]):
        reqs.append(col_w(c, px))

    # Row heights
    heights = {0: 42, 1: 22, 2: 10, 3: 13, 4: 44, 5: 15, 6: 12,
               7: 28, 8: 22, 9: 22, 10: 22, 11: 12, 12: 24, 13: 34,
               14: 12, 15: 20, 16: 26, 17: 18, 18: 16, 19: 12,
               20: 20, 21: 26, 22: 12, 23: 14, 24: 34, 25: 16}
    for r, px in heights.items():
        reqs.append(row_h(r, px))

    # ── Reset all to white, no border ─────────────────────────────────────────
    reqs.append(cell_fmt(0, 26, 0, 6,
        backgroundColor=WHITE,
        textFormat={"fontSize": 9, "bold": False,
                    "foregroundColor": DARK_GRAY},
        wrapStrategy="CLIP",
        verticalAlignment="MIDDLE",
        horizontalAlignment="LEFT",
    ))

    # ── Merges ────────────────────────────────────────────────────────────────
    for m in [
        (0,1,0,4), (0,1,4,6),           # R0 title | health
        (1,2,0,4), (1,2,4,6),           # R1 subtitle | date
        (2,3,0,6),                       # R2 spacer
        (3,4,1,3), (4,5,1,3), (5,6,1,3),  # KPI B:C phase col
        (6,7,0,6),                       # R6 spacer
        (7,8,0,6),                       # R7 attn header
        (8,9,0,6), (9,10,0,6), (10,11,0,6),  # R8-10 items
        (11,12,0,6),                     # R11 spacer
        (12,13,0,6), (13,14,0,6),        # R12-13 next action
        (14,15,0,6),                     # R14 spacer
        (15,16,0,2), (15,16,2,4), (15,16,4,6),  # R15 phase labels
        (16,17,0,2), (16,17,2,4), (16,17,4,6),  # R16 phase names
        (17,18,0,2), (17,18,2,4), (17,18,4,6),  # R17 dates
        (18,19,0,2), (18,19,2,4), (18,19,4,6),  # R18 status
        (19,20,0,6),                     # R19 spacer
        (20,21,0,2), (20,21,2,6),        # R20 progress label | value
        (21,22,0,6),                     # R21 bar
        (22,23,0,6),                     # R22 spacer
        (25,26,0,6),                     # R25 footer
    ]:
        reqs.append(mrg(*m))

    # ── R0: Title bar ─────────────────────────────────────────────────────────
    reqs.append(cell_fmt(0,1,0,4,
        backgroundColor=NAVY,
        textFormat={"bold": True, "fontSize": 14, "foregroundColor": WHITE},
        verticalAlignment="MIDDLE", horizontalAlignment="LEFT",
        padding={"top":0,"bottom":0,"left":16,"right":0},
        wrapStrategy="CLIP",
    ))
    reqs.append(cell_fmt(0,1,4,6,
        backgroundColor=health_bg,
        textFormat={"bold": True, "fontSize": 9, "foregroundColor": WHITE},
        verticalAlignment="MIDDLE", horizontalAlignment="CENTER",
        wrapStrategy="CLIP",
    ))

    # ── R1: Subtitle ──────────────────────────────────────────────────────────
    reqs.append(cell_fmt(1,2,0,4,
        backgroundColor=NAVY_DK,
        textFormat={"fontSize": 8, "foregroundColor":
                    {"red":0.82,"green":0.86,"blue":0.93}},
        verticalAlignment="MIDDLE", horizontalAlignment="LEFT",
        padding={"top":0,"bottom":0,"left":16,"right":0},
        wrapStrategy="CLIP",
    ))
    reqs.append(cell_fmt(1,2,4,6,
        backgroundColor=NAVY_DK,
        textFormat={"fontSize": 8, "foregroundColor":
                    {"red":0.82,"green":0.86,"blue":0.93}},
        verticalAlignment="MIDDLE", horizontalAlignment="RIGHT",
        padding={"top":0,"bottom":0,"left":0,"right":12},
        wrapStrategy="CLIP",
    ))

    # ── R3: KPI labels ────────────────────────────────────────────────────────
    reqs.append(cell_fmt(3,4,0,6,
        textFormat={"bold": True, "fontSize": 8, "foregroundColor": MID_GRAY},
        horizontalAlignment="CENTER", verticalAlignment="BOTTOM",
        wrapStrategy="CLIP",
    ))
    # Phase label left-aligned
    reqs.append(cell_fmt(3,4,1,3,
        textFormat={"bold": True, "fontSize": 8, "foregroundColor": MID_GRAY},
        horizontalAlignment="LEFT", verticalAlignment="BOTTOM",
        padding={"top":0,"bottom":0,"left":8,"right":0}, wrapStrategy="CLIP",
    ))

    # ── R4: KPI values ────────────────────────────────────────────────────────
    reqs.append(cell_fmt(4,5,0,6,
        textFormat={"bold": True, "fontSize": 26, "foregroundColor": NAVY_TEXT},
        horizontalAlignment="CENTER", verticalAlignment="MIDDLE",
    ))
    # Phase name — smaller, left-aligned
    reqs.append(cell_fmt(4,5,1,3,
        textFormat={"bold": True, "fontSize": 12, "foregroundColor": NAVY_TEXT},
        horizontalAlignment="LEFT", verticalAlignment="MIDDLE",
        padding={"top":0,"bottom":0,"left":8,"right":0}, wrapStrategy="WRAP",
    ))
    # Issues — color-coded
    reqs.append(cell_fmt(4,5,5,6,
        textFormat={"bold": True, "fontSize": 26, "foregroundColor": issue_color},
        horizontalAlignment="CENTER", verticalAlignment="MIDDLE",
    ))

    # ── R5: KPI descriptors ───────────────────────────────────────────────────
    reqs.append(cell_fmt(5,6,0,6,
        textFormat={"fontSize": 8, "italic": True, "foregroundColor": MID_GRAY},
        horizontalAlignment="CENTER", verticalAlignment="TOP",
    ))
    reqs.append(cell_fmt(5,6,1,3,
        textFormat={"fontSize": 8, "italic": True, "foregroundColor": MID_GRAY},
        horizontalAlignment="LEFT", verticalAlignment="TOP",
        padding={"top":0,"bottom":0,"left":8,"right":0},
    ))
    reqs.append(hdivider(5))

    # ── R7: Attention header ──────────────────────────────────────────────────
    reqs.append(cell_fmt(7,8,0,6,
        backgroundColor=attn_bg,
        textFormat={"bold": True, "fontSize": 10, "foregroundColor": WHITE},
        verticalAlignment="MIDDLE", horizontalAlignment="LEFT",
        padding={"top":0,"bottom":0,"left":4,"right":0}, wrapStrategy="CLIP",
    ))
    # R8-10: attention items
    reqs.append(cell_fmt(8,11,0,6,
        backgroundColor=ATTN_ITEM_BG,
        textFormat={"fontSize": 9, "foregroundColor": DARK_GRAY},
        verticalAlignment="MIDDLE", horizontalAlignment="LEFT",
        padding={"top":0,"bottom":0,"left":4,"right":0}, wrapStrategy="CLIP",
    ))

    # ── R12: Next action header ───────────────────────────────────────────────
    reqs.append(cell_fmt(12,13,0,6,
        backgroundColor=NAVY,
        textFormat={"bold": True, "fontSize": 9, "foregroundColor": WHITE},
        verticalAlignment="MIDDLE", horizontalAlignment="LEFT",
        padding={"top":0,"bottom":0,"left":4,"right":0}, wrapStrategy="CLIP",
    ))
    # R13: Next action text
    reqs.append(cell_fmt(13,14,0,6,
        backgroundColor={"red":0.96,"green":0.97,"blue":0.99},
        textFormat={"bold": True, "fontSize": 10, "foregroundColor": NAVY_TEXT},
        verticalAlignment="MIDDLE", horizontalAlignment="LEFT",
        padding={"top":0,"bottom":0,"left":4,"right":12}, wrapStrategy="WRAP",
    ))

    # ── R15-18: Phase context (light gray band) ───────────────────────────────
    reqs.append(cell_fmt(15,19,0,6, backgroundColor=LGRAY_BG))
    # Section labels (R15)
    for c1, c2 in ((0,2), (2,4), (4,6)):
        reqs.append(cell_fmt(15,16,c1,c2,
            backgroundColor=LGRAY_BG,
            textFormat={"bold": True, "fontSize": 8, "foregroundColor": NAVY_DK},
            verticalAlignment="MIDDLE", horizontalAlignment="LEFT",
            padding={"top":0,"bottom":0,"left":10,"right":0}, wrapStrategy="CLIP",
        ))
    # Phase names (R16)
    for c1, c2 in ((0,2), (2,4), (4,6)):
        reqs.append(cell_fmt(16,17,c1,c2,
            backgroundColor=LGRAY_BG,
            textFormat={"bold": True, "fontSize": 11, "foregroundColor": NAVY_TEXT},
            verticalAlignment="MIDDLE", horizontalAlignment="LEFT",
            padding={"top":0,"bottom":0,"left":10,"right":0}, wrapStrategy="WRAP",
        ))
    # Dates + status (R17-18)
    for c1, c2 in ((0,2), (2,4), (4,6)):
        reqs.append(cell_fmt(17,19,c1,c2,
            backgroundColor=LGRAY_BG,
            textFormat={"fontSize": 9, "foregroundColor": MID_GRAY},
            verticalAlignment="MIDDLE", horizontalAlignment="LEFT",
            padding={"top":0,"bottom":0,"left":10,"right":0},
        ))
    reqs.append(hdivider(18))

    # ── R20-21: Progress ──────────────────────────────────────────────────────
    reqs.append(cell_fmt(20,21,0,2,
        textFormat={"bold": True, "fontSize": 9, "foregroundColor": NAVY_TEXT},
        verticalAlignment="MIDDLE", horizontalAlignment="LEFT",
        padding={"top":0,"bottom":0,"left":10,"right":0},
    ))
    reqs.append(cell_fmt(20,21,2,6,
        textFormat={"fontSize": 9, "foregroundColor": MID_GRAY},
        verticalAlignment="MIDDLE", horizontalAlignment="RIGHT",
        padding={"top":0,"bottom":0,"left":0,"right":10},
    ))
    bar_color = ({"red":0.13,"green":0.50,"blue":0.28}
                 if pct > 0 else {"red":0.78,"green":0.80,"blue":0.84})
    reqs.append(cell_fmt(21,22,0,6,
        textFormat={"fontSize": 10, "foregroundColor": bar_color},
        verticalAlignment="MIDDLE", horizontalAlignment="LEFT",
        wrapStrategy="CLIP",
        padding={"top":0,"bottom":0,"left":10,"right":0},
    ))
    reqs.append(hdivider(21))

    # ── R23-24: Status count boxes ────────────────────────────────────────────
    box_colors = [BOX_GRAY, BOX_GREEN, BOX_AMBER, BOX_RED, BOX_ORANGE, NAVY]
    for ci, bg in enumerate(box_colors):
        reqs.append(cell_fmt(23,25,ci,ci+1, backgroundColor=bg))
    reqs.append(cell_fmt(23,24,0,6,
        textFormat={"bold": True, "fontSize": 8, "foregroundColor": WHITE},
        horizontalAlignment="CENTER", verticalAlignment="MIDDLE",
        wrapStrategy="CLIP",
    ))
    reqs.append(cell_fmt(24,25,0,6,
        textFormat={"bold": True, "fontSize": 20, "foregroundColor": WHITE},
        horizontalAlignment="CENTER", verticalAlignment="MIDDLE",
    ))

    # ── R25: Footer ───────────────────────────────────────────────────────────
    reqs.append(cell_fmt(25,26,0,6,
        textFormat={"fontSize": 8, "italic": True,
                    "foregroundColor": {"red":0.68,"green":0.70,"blue":0.72}},
        verticalAlignment="MIDDLE", horizontalAlignment="CENTER",
        wrapStrategy="CLIP",
    ))

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": reqs}
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


# ---------------------------------------------------------------------------
# Procurement Alerts tab
# ---------------------------------------------------------------------------

PROCUREMENT_ALERTS_HEADERS = [
    "Date Fired", "Phase #", "Phase Name", "Alert Type",
    "Order By", "Phase Start", "Lead Time (wks)",
]


def ensure_procurement_tab(sheets_svc, sheet_id: str) -> int:
    """Ensure a 'Procurement Alerts' tab exists. Returns its sheetId.

    Creates the tab with a frozen header row if it doesn't exist.
    """
    from home_builder_agent.config import PROCUREMENT_ALERTS_TAB

    meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta.get("sheets", [])}

    if PROCUREMENT_ALERTS_TAB in existing:
        return existing[PROCUREMENT_ALERTS_TAB]

    # Create the tab
    resp = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": PROCUREMENT_ALERTS_TAB}}}]},
    ).execute()
    tab_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    # Write header row
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{PROCUREMENT_ALERTS_TAB}!A1",
        valueInputOption="RAW",
        body={"values": [PROCUREMENT_ALERTS_HEADERS]},
    ).execute()

    # Freeze header + bold it
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
                        "range": {
                            "sheetId": tab_id,
                            "startRowIndex": 0, "endRowIndex": 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.18, "green": 0.18, "blue": 0.18},
                                "textFormat": {
                                    "bold": True,
                                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                },
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat)",
                    }
                },
            ]
        },
    ).execute()

    return tab_id


def log_procurement_alerts(sheets_svc, sheet_id: str, alerts: list[dict]) -> int:
    """Append procurement alert rows to the Procurement Alerts tab.

    Args:
        sheets_svc: authenticated Sheets service
        sheet_id:   Tracker spreadsheet ID
        alerts:     list of alert dicts from check_procurement_alerts()

    Returns:
        Number of rows appended.
    """
    from home_builder_agent.config import PROCUREMENT_ALERTS_TAB

    if not alerts:
        return 0

    ensure_procurement_tab(sheets_svc, sheet_id)

    from datetime import date as _date
    today_str = _date.today().isoformat()

    rows = []
    for a in alerts:
        rows.append([
            today_str,
            str(a.get("phase_num", "")),
            a.get("phase_name", ""),
            a.get("alert_type", ""),
            a.get("order_by", "").isoformat() if a.get("order_by") else "",
            a.get("start", "").isoformat() if a.get("start") else "",
            str(a.get("lead_weeks", "")),
        ])

    sheets_svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{PROCUREMENT_ALERTS_TAB}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()

    return len(rows)


# ---------------------------------------------------------------------------
# Inspections tab
# ---------------------------------------------------------------------------

INSPECTIONS_TAB = "Inspections"
INSPECTIONS_HEADERS = [
    "Date", "Record Type", "Permit #", "Permit Type",
    "Inspection Type", "Status", "Inspector", "Notes",
]


def ensure_inspections_tab(sheets_svc, sheet_id: str) -> int:
    """Ensure an 'Inspections' tab exists. Returns its sheetId."""
    meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta.get("sheets", [])}

    if INSPECTIONS_TAB in existing:
        return existing[INSPECTIONS_TAB]

    resp = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": INSPECTIONS_TAB}}}]},
    ).execute()
    tab_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    # Write header row
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{INSPECTIONS_TAB}!A1",
        valueInputOption="RAW",
        body={"values": [INSPECTIONS_HEADERS]},
    ).execute()

    # Freeze + style header
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
                        "range": {
                            "sheetId": tab_id,
                            "startRowIndex": 0, "endRowIndex": 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.18, "green": 0.18, "blue": 0.18},
                                "textFormat": {
                                    "bold": True,
                                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                },
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat)",
                    }
                },
            ]
        },
    ).execute()

    return tab_id


def read_inspections(sheets_svc, sheet_id: str) -> list[dict]:
    """Read all rows from the Inspections tab. Returns [] if tab doesn't exist."""
    try:
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{INSPECTIONS_TAB}!A1:H500",
        ).execute()
    except Exception:
        return []

    rows = result.get("values", [])
    if not rows or len(rows) < 2:
        return []

    headers = rows[0]
    records = []
    for row in rows[1:]:
        padded = list(row) + [""] * (len(headers) - len(row))
        rec = dict(zip(headers, padded))
        if any(v.strip() for v in rec.values()):
            records.append(rec)
    return records


def log_inspection_record(sheets_svc, sheet_id: str, record: dict) -> None:
    """Append one record to the Inspections tab. Creates the tab if needed."""
    ensure_inspections_tab(sheets_svc, sheet_id)

    row = [
        record.get("date", ""),
        record.get("record_type", ""),
        record.get("permit_number", ""),
        record.get("permit_type", ""),
        record.get("inspection_type", ""),
        record.get("status", ""),
        record.get("inspector", ""),
        record.get("notes", ""),
    ]

    sheets_svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{INSPECTIONS_TAB}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


# ---------------------------------------------------------------------------
# Project Info tab — single-source for client/project metadata
# ---------------------------------------------------------------------------
#
# Used by per-project agents that need client name + email without per-call
# CLI flags (e.g., the weekly client-update cron). Pattern mirrors the
# Project Info tab on Spec Sheet 2026.xlsx — one row per field, values in
# column B.

PROJECT_INFO_TAB = "Project Info"

PROJECT_INFO_FIELDS = [
    ("Customer Name", ""),
    ("Customer Email", ""),
    ("Customer Phone", ""),
    ("Project Address", ""),
    ("Job Code", ""),
    ("Builder", "Palmetto Custom Homes"),
    ("Notes", ""),
]


def ensure_project_info_tab(sheets_svc, sheet_id: str) -> int:
    """Find or create the 'Project Info' tab on a Tracker. Returns its sheetId.

    On creation, seeds the tab with the canonical field names in column A
    and empty values (or sensible defaults) in column B. Chad fills in
    column B once per project; agents read those values.
    """
    meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta.get("sheets", [])}

    if PROJECT_INFO_TAB in existing:
        return existing[PROJECT_INFO_TAB]

    resp = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": PROJECT_INFO_TAB}}}]},
    ).execute()
    tab_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    # Seed rows
    rows = [["Field", "Value"]]
    for field, default in PROJECT_INFO_FIELDS:
        rows.append([field, default])

    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{PROJECT_INFO_TAB}!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()

    # Style the header row + first column
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
                                "backgroundColor": {"red": 0.18, "green": 0.18, "blue": 0.18},
                                "textFormat": {
                                    "bold": True,
                                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                },
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat)",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": tab_id,
                            "startRowIndex": 1, "endRowIndex": len(rows),
                            "startColumnIndex": 0, "endColumnIndex": 1,
                        },
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": tab_id, "dimension": "COLUMNS",
                                  "startIndex": 0, "endIndex": 1},
                        "properties": {"pixelSize": 180},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": tab_id, "dimension": "COLUMNS",
                                  "startIndex": 1, "endIndex": 2},
                        "properties": {"pixelSize": 360},
                        "fields": "pixelSize",
                    }
                },
            ]
        },
    ).execute()

    return tab_id


def read_project_info(sheets_svc, sheet_id: str) -> dict:
    """Read the Project Info tab and return field/value pairs as a dict.

    Returns {} if the tab doesn't exist. Empty values are returned as
    empty strings (NOT omitted) so callers can detect "field exists but
    Chad hasn't filled it in" vs "field doesn't exist on this tracker".
    """
    try:
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{PROJECT_INFO_TAB}!A1:B100",
        ).execute()
    except Exception:
        return {}

    rows = result.get("values", [])
    if not rows or len(rows) < 2:
        return {}

    info: dict[str, str] = {}
    for row in rows[1:]:  # skip header
        if not row:
            continue
        field = row[0].strip() if len(row) > 0 else ""
        value = row[1].strip() if len(row) > 1 else ""
        if field:
            info[field] = value
    return info


# ---------------------------------------------------------------------------
# Tracker write helpers — used by hb-chad input tools (dual-write, v1.2)
# ---------------------------------------------------------------------------
#
# Per ADR 2026-05-11 v1.2 ("Google Sheets canonical, Postgres query store"):
# hb-chad's three input tools (update_customer_info, update_schedule_date,
# reorder_phase) dual-write — Postgres + Sheets in one atomic transaction.
# Sheets is canonical; the bridge sync would overwrite Postgres-only writes
# within 5 minutes.
#
# Atomicity pattern (in the caller):
#   with connection() as conn:           # autocommit=False
#       with conn.cursor() as cur:
#           cur.execute("UPDATE ...")     # uncommitted
#           sheets.update_*(svc, ...)     # raises SheetsWriteError on failure
#   # On clean exit, Postgres COMMITs. On any exception (including
#   # SheetsWriteError), connection() rolls back.
#
# These helpers raise SheetsWriteError (a RuntimeError subclass) so callers
# can catch them specifically while letting the rollback fire.

class SheetsWriteError(RuntimeError):
    """Raised by the Tracker write helpers when a Sheets write can't
    complete (field/header/row not found, API error). Subclass of
    RuntimeError so callers that catch a broad Exception still see it;
    subclasses get caught specifically when the caller wants to
    distinguish "Sheets failed" from "Postgres failed".
    """


def update_project_info_field(sheets_svc, sheet_id, field_name, value):
    """Write a single Project Info value, addressed by its field-name row.

    The Project Info tab is a 2-column key/value table — column A holds
    field names (e.g. "Customer Email"), column B holds values. This
    helper looks up the row where A == ``field_name`` and writes
    ``value`` into B on that row.

    Idempotent: re-running with the same value is a no-op on Sheets
    side (the API call still fires but writes the same cell value).

    Raises:
        SheetsWriteError: if the Project Info tab is missing, empty,
                          or doesn't contain a row whose column A
                          matches ``field_name``.
    """
    try:
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{PROJECT_INFO_TAB}!A1:B100",
        ).execute()
    except Exception as e:  # API failure — propagate as SheetsWriteError
        raise SheetsWriteError(
            f"failed to read {PROJECT_INFO_TAB} tab: "
            f"{type(e).__name__}: {e}"
        ) from e

    rows = result.get("values", [])
    if not rows:
        raise SheetsWriteError(
            f"{PROJECT_INFO_TAB} tab is empty — cannot write {field_name!r}."
        )

    # Find the row whose column A matches field_name (case-sensitive —
    # the tab is seeded from PROJECT_INFO_FIELDS so the canonical
    # capitalization is known). Skip the header row.
    target_row_idx = None  # 1-indexed row number in Sheets
    for i, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        col_a = row[0].strip() if len(row) > 0 else ""
        if col_a == field_name:
            target_row_idx = i
            break

    if target_row_idx is None:
        raise SheetsWriteError(
            f"field {field_name!r} not found in column A of "
            f"{PROJECT_INFO_TAB} tab."
        )

    try:
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{PROJECT_INFO_TAB}!B{target_row_idx}",
            valueInputOption="USER_ENTERED",
            body={"values": [[value if value is not None else ""]]},
        ).execute()
    except Exception as e:
        raise SheetsWriteError(
            f"failed to write {field_name!r} = {value!r} to "
            f"{PROJECT_INFO_TAB}!B{target_row_idx}: "
            f"{type(e).__name__}: {e}"
        ) from e


def update_master_schedule_cell(
    sheets_svc, sheet_id, sequence_index, column_header, value
):
    """Write a single cell on Master Schedule, addressed by row's "#" + column header.

    The Master Schedule tab uses column A as a numeric "#" (sequence
    index) and row 1 as the header row. This helper locates the row
    whose A column equals ``sequence_index`` and the column whose
    header text matches ``column_header`` (e.g. "Start", "End",
    "Status", "Phase"), and writes ``value`` to that cell.

    Raises:
        SheetsWriteError: if the row, column, or tab can't be found.
    """
    try:
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Master Schedule!A1:Z200",
        ).execute()
    except Exception as e:
        raise SheetsWriteError(
            f"failed to read Master Schedule tab: "
            f"{type(e).__name__}: {e}"
        ) from e

    rows = result.get("values", [])
    if not rows or len(rows) < 2:
        raise SheetsWriteError(
            "Master Schedule tab is empty or has no data rows — cannot "
            f"write sequence_index={sequence_index}, "
            f"column={column_header!r}."
        )

    headers = rows[0]
    # Find the column index whose header text matches column_header.
    col_idx = None
    for i, h in enumerate(headers):
        if (h or "").strip() == column_header:
            col_idx = i
            break
    if col_idx is None:
        raise SheetsWriteError(
            f"column {column_header!r} not found in Master Schedule "
            f"header row. Got: {headers}."
        )

    # Find the row whose column A == sequence_index (compared as strings
    # because Sheets returns "3", not 3).
    target = str(sequence_index)
    target_row_idx = None  # 1-indexed Sheets row number
    for i, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        a = (row[0] or "").strip() if len(row) > 0 else ""
        if a == target:
            target_row_idx = i
            break

    if target_row_idx is None:
        raise SheetsWriteError(
            f"no row with #={sequence_index} found in Master Schedule "
            f"column A."
        )

    col_letter = _column_index_to_letter(col_idx)
    cell_range = f"Master Schedule!{col_letter}{target_row_idx}"
    try:
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=cell_range,
            valueInputOption="USER_ENTERED",
            body={"values": [[value if value is not None else ""]]},
        ).execute()
    except Exception as e:
        raise SheetsWriteError(
            f"failed to write {value!r} to {cell_range}: "
            f"{type(e).__name__}: {e}"
        ) from e


def update_master_schedule_sequence_indices(
    sheets_svc, sheet_id, phase_name_to_new_seq
):
    """Bulk-update the "#" column for multiple Master Schedule rows.

    Each row is located by its current phase name (column B); we
    rewrite its column A "#" value. Uses ``values.batchUpdate`` so the
    whole multi-row write hits Sheets in one HTTP call — partial
    progress on the wire is not possible.

    Args:
        sheets_svc: authenticated Sheets v4 service
        sheet_id:   target Tracker spreadsheet id
        phase_name_to_new_seq: ``{phase_name: new_sequence_index}``
            dict — exact match on column B (case-sensitive, stripped).

    Raises:
        SheetsWriteError: if any ``phase_name`` in the input dict
                          isn't found in Master Schedule column B,
                          or if the batchUpdate API call fails.
                          (Atomicity: no partial writes — we resolve
                          all rows up front, then issue ONE batch call.)
    """
    if not phase_name_to_new_seq:
        return  # no-op — nothing to write

    try:
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Master Schedule!A1:G200",
        ).execute()
    except Exception as e:
        raise SheetsWriteError(
            f"failed to read Master Schedule tab: "
            f"{type(e).__name__}: {e}"
        ) from e

    rows = result.get("values", [])
    if not rows or len(rows) < 2:
        raise SheetsWriteError(
            "Master Schedule tab is empty or has no data rows — cannot "
            "rewrite sequence indices."
        )

    # Build a {phase_name: row_index} map for every row currently in
    # the sheet, then validate every requested phase_name resolves.
    name_to_row: dict[str, int] = {}
    for i, row in enumerate(rows[1:], start=2):
        if not row or len(row) < 2:
            continue
        name = (row[1] or "").strip()
        if name:
            name_to_row[name] = i

    # Resolve all requested phase names BEFORE we issue any write. If
    # any is missing, raise — no partial-write window.
    resolved: list[tuple[int, int]] = []  # (row_idx, new_seq)
    for phase_name, new_seq in phase_name_to_new_seq.items():
        row_idx = name_to_row.get(phase_name)
        if row_idx is None:
            raise SheetsWriteError(
                f"phase {phase_name!r} not found in Master Schedule "
                f"column B — refusing to write a partial reorder."
            )
        resolved.append((row_idx, new_seq))

    # Single batchUpdate — one HTTP round-trip, all writes or none.
    data = [
        {
            "range": f"Master Schedule!A{row_idx}",
            "values": [[new_seq]],
        }
        for row_idx, new_seq in resolved
    ]
    try:
        sheets_svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()
    except Exception as e:
        raise SheetsWriteError(
            f"failed to batch-update Master Schedule # column: "
            f"{type(e).__name__}: {e}"
        ) from e


def _column_index_to_letter(idx):
    """Convert a 0-indexed column number to an A1-style letter.

    0 → A, 1 → B, ..., 25 → Z, 26 → AA, 27 → AB, ...
    Used internally by ``update_master_schedule_cell`` to build
    A1-notation ranges like 'Master Schedule!D5'.
    """
    if idx < 0:
        raise ValueError(f"column index must be ≥ 0 (got {idx})")
    letters = ""
    n = idx
    while True:
        letters = chr(ord("A") + (n % 26)) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return letters
