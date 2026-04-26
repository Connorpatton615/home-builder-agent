"""
agent_2_5_dashboard.py — Project Status Dashboard (Stage 1, read-only).

Reads the most recent Tracker sheet from GENERATED TIMELINES, computes
dashboard metrics (current stage, upcoming stage, % complete, revised
completion date), and adds/updates a "Dashboard" tab on the same sheet.

Stage 1 is read-only on phase data — only writes to the Dashboard tab.
The phase status data on Master Schedule tab is the source of truth; Chad
updates that tab and the Dashboard tab reflects whatever he's set.

Stages 2-3 (next session) will add: NL update parsing, cascade computation,
auto-update of Master Schedule based on chat-style notes.
"""

import os

# Allow OAuth scope flexibility (Google may return fewer scopes than requested
# when the user has previously authorized the app)
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from datetime import date

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# --- Config ----------------------------------------------------------

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

# Where to find the Tracker sheets (in Drive folder hierarchy from My Drive root)
DRIVE_FOLDER_PATH = [
    "Home Building Agent V.1",
    "Home Builder Agent V.1",
    "GENERATED TIMELINES",
]

# Status vocabulary expected in the Master Schedule "Status" column
STATUS_DONE = "done"
STATUS_IN_PROGRESS = "in progress"
STATUS_BLOCKED = "blocked"
STATUS_DELAYED = "delayed"
STATUS_NOT_STARTED = "not started"

# Status → indicator emoji (functional, per Chad's communication rules)
STATUS_EMOJI = {
    STATUS_DONE: "🟢",
    STATUS_IN_PROGRESS: "🟡",
    STATUS_BLOCKED: "🔴",
    STATUS_DELAYED: "🟠",
    STATUS_NOT_STARTED: "⚪",
}

# Status row colors (RGB 0.0–1.0). Used for conditional formatting + dashboard
STATUS_ROW_COLOR = {
    STATUS_DONE:        {"red": 0.83, "green": 0.92, "blue": 0.83},  # green
    STATUS_IN_PROGRESS: {"red": 1.00, "green": 0.95, "blue": 0.78},  # yellow
    STATUS_BLOCKED:     {"red": 0.96, "green": 0.78, "blue": 0.76},  # red
    STATUS_DELAYED:     {"red": 0.99, "green": 0.90, "blue": 0.80},  # orange
}


# --- Auth ------------------------------------------------------------

def get_credentials():
    """Authenticate with Google. Reuses token.json from agent_2_v1.py."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, GOOGLE_SCOPES
            )
            creds = flow.run_local_server(port=0, prompt="consent")
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


# --- Drive lookup ----------------------------------------------------

def find_latest_tracker(drive_service, folder_path):
    """Walk the folder path and return metadata for the most recent Tracker sheet."""
    parent_id = "root"
    walked = []
    for name in folder_path:
        walked.append(name)
        query = (
            f"name='{name}' "
            "and mimeType='application/vnd.google-apps.folder' "
            f"and '{parent_id}' in parents "
            "and trashed=false"
        )
        results = drive_service.files().list(
            q=query, fields="files(id,name)"
        ).execute()
        folders = results.get("files", [])
        if not folders:
            raise FileNotFoundError(
                f"Folder not found in Drive: {' / '.join(walked)}"
            )
        parent_id = folders[0]["id"]

    # Find the most recently modified Tracker spreadsheet in this folder
    query = (
        "name contains 'Tracker' "
        "and mimeType='application/vnd.google-apps.spreadsheet' "
        f"and '{parent_id}' in parents "
        "and trashed=false"
    )
    results = drive_service.files().list(
        q=query,
        orderBy="modifiedTime desc",
        fields="files(id,name,modifiedTime,webViewLink)",
    ).execute()
    sheets = results.get("files", [])
    if not sheets:
        raise FileNotFoundError(
            f"No Tracker sheets found in {' / '.join(folder_path)}"
        )
    return sheets[0]  # Most recent


# --- Sheet read ------------------------------------------------------

def read_master_schedule(sheets_service, sheet_id):
    """Read Master Schedule tab. Return list of phase dicts (one per row)."""
    result = sheets_service.spreadsheets().values().get(
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
        # Skip blank rows
        if phase.get("Phase", "").strip():
            phases.append(phase)
    return phases


# --- Metrics ---------------------------------------------------------

def normalize_status(status):
    """Lowercase + strip the Status field for comparison."""
    return (status or "").strip().lower()


def compute_dashboard_metrics(phases, today=None):
    """Compute the dashboard view from a list of phase dicts."""
    if today is None:
        today = date.today()

    # Find current phase = first phase that's not "Done"
    # (could be In Progress, Not Started, Blocked, Delayed)
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

    # % complete: count phases as 0/0.5/1 by status
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

    # Project completion = end of last phase
    original_completion = phases[-1].get("End", "") if phases else ""
    # In Stage 1 we don't compute revised completion (no cascade logic yet);
    # show same as original. Stages 2-3 will compute true revisions.
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


def progress_bar_text(pct, width=20):
    """Render a text-based progress bar like '████░░░░░░░░░░░░░░░░ 20%'."""
    pct_int = max(0, min(100, int(pct)))
    filled = int(pct_int * width / 100)
    return "█" * filled + "░" * (width - filled)


# --- Dashboard tab management ---------------------------------------

def ensure_dashboard_tab(sheets_service, sheet_id):
    """Add a Dashboard tab if missing. Return its sheetId either way."""
    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties"
    ).execute()

    for s in spreadsheet["sheets"]:
        if s["properties"]["title"] == "Dashboard":
            return s["properties"]["sheetId"]

    # Doesn't exist — create it as the FIRST tab so it's what Chad sees first
    request = {
        "addSheet": {
            "properties": {
                "title": "Dashboard",
                "index": 0,
                "gridProperties": {"rowCount": 50, "columnCount": 4},
            }
        }
    }
    response = sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": [request]}
    ).execute()
    return response["replies"][0]["addSheet"]["properties"]["sheetId"]


def write_dashboard(sheets_service, sheet_id, dashboard_sheet_id, metrics, project_name):
    """Write the dashboard view to the Dashboard tab with formatting."""

    # Layout — section header, label/value pairs
    pct = int(metrics["pct_complete"])
    pct_bar = progress_bar_text(pct)
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
        ["Phases complete", f"{metrics['n_done_phases']} of {metrics['n_total_phases']}", "", ""],
        ["🟡 Phases in progress", metrics["n_in_progress_phases"], "", ""],
        ["🔴 Phases blocked", metrics["n_blocked_phases"], "", ""],
        ["🟠 Phases delayed", metrics["n_delayed_phases"], "", ""],
        ["", "", "", ""],
        ["COMPLETION TARGET", "", "", ""],
        ["Original completion", metrics["original_completion"], "", ""],
        ["Revised completion", metrics["revised_completion"], "", ""],
        ["", "", "", ""],
        ["NOTES", "", "", ""],
        ["", "Update phase Status dropdowns on Master Schedule tab; re-run",
         "", ""],
        ["", "agent_2_5_dashboard.py to refresh this view. For natural-language",
         "", ""],
        ["", "updates that auto-cascade through the schedule, use",
         "", ""],
        ["", "agent_2_5_update.py \"Phase 3 pushed 1 week\" etc.",
         "", ""],
    ]

    # Push values
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="Dashboard!A1",
        valueInputOption="USER_ENTERED",
        body={"values": layout},
    ).execute()

    # Apply formatting
    requests = []

    # Title (row 0): bold, large, dark background, white text
    requests.append({
        "repeatCell": {
            "range": {"sheetId": dashboard_sheet_id,
                      "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True, "fontSize": 14,
                               "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.6},
            }},
            "fields": "userEnteredFormat(textFormat,backgroundColor)",
        }
    })

    # Section header rows: bold, light green background
    section_rows = [3, 9, 13, 20, 24]  # 0-indexed rows of section headers
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

    # Label column (column A) for value rows: bold gray
    label_rows = [1, 4, 5, 6, 7, 10, 11, 14, 15, 16, 17, 18, 21, 22]
    for row in label_rows:
        requests.append({
            "repeatCell": {
                "range": {"sheetId": dashboard_sheet_id,
                          "startRowIndex": row, "endRowIndex": row + 1,
                          "startColumnIndex": 0, "endColumnIndex": 1},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True},
                }},
                "fields": "userEnteredFormat.textFormat",
            }
        })

    # Auto-resize columns A-D
    requests.append({
        "autoResizeDimensions": {
            "dimensions": {"sheetId": dashboard_sheet_id,
                           "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": 4},
        }
    })

    # Set column A width to be a bit wider for label readability
    requests.append({
        "updateDimensionProperties": {
            "range": {"sheetId": dashboard_sheet_id,
                      "dimension": "COLUMNS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 200},
            "fields": "pixelSize",
        }
    })

    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests}
    ).execute()


def extract_project_name(tracker_name):
    """Pull project name from a Tracker sheet's name.

    Handles both naming conventions:
      'Tracker – Pelican Point'              (current — no timestamp)
      'Tracker – Pelican Point – 2026-04-25 11:10'  (legacy — pre-2026-04-25 cleanup)
    """
    parts = tracker_name.split(" – ")
    if len(parts) >= 2:
        return parts[1]
    return tracker_name


# --- Visual formatting (Option A: sheet polish) ---------------------

def apply_visual_formatting(sheets_service, sheet_id):
    """Apply conditional formatting + date formatting to all data tabs.

    - Master Schedule: row colored by Status column F (Done/InProgress/etc.)
    - Order Schedule: row colored by Status column E
    - Action Items:   row colored when Done checkbox (column C) is TRUE
    - Date columns formatted as 'MMM dd, yyyy' for human readability

    Idempotent: clears existing conditional rules on these tabs before adding,
    so re-runs don't accumulate duplicate rules.
    """
    # 1. Read existing tabs and rule counts
    spreadsheet = sheets_service.spreadsheets().get(
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

    # 2. Clear existing conditional rules on the data tabs (delete index 0
    # repeatedly — each delete shifts the next rule down to index 0)
    for title in ("Master Schedule", "Order Schedule", "Action Items"):
        if title in tab_ids:
            tid = tab_ids[title]
            for _ in range(rule_counts.get(title, 0)):
                requests.append({
                    "deleteConditionalFormatRule": {"sheetId": tid, "index": 0}
                })

    # 3. Master Schedule — color the whole row by Status (column F = index 5)
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

    # 4. Order Schedule — color by Status column E (index 4)
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
        # Date format on Order By column (C=2)
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

    # 5. Action Items — color whole row when Done checkbox is TRUE (col C=2)
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
        # Date format on Target Date column (D=3)
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

    # 6. Send all requests in a single batch
    if requests:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": requests}
        ).execute()

    return len(requests)


# --- Main ------------------------------------------------------------

def main():
    print("Authenticating with Google...")
    creds = get_credentials()
    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)

    print(f"\nFinding latest Tracker in: {' / '.join(DRIVE_FOLDER_PATH)}")
    tracker = find_latest_tracker(drive_service, DRIVE_FOLDER_PATH)
    project_name = extract_project_name(tracker["name"])
    print(f"  Found: {tracker['name']}")
    print(f"  Modified: {tracker['modifiedTime']}")
    print(f"  Project: {project_name}")

    print(f"\nReading Master Schedule tab...")
    phases = read_master_schedule(sheets_service, tracker["id"])
    print(f"  {len(phases)} phases loaded")

    print(f"\nComputing dashboard metrics...")
    metrics = compute_dashboard_metrics(phases)
    print(f"  Current stage:    {metrics['current_stage']}")
    print(f"  Current status:   {metrics['current_status']}")
    print(f"  Upcoming stage:   {metrics['upcoming_stage']}")
    print(f"  % Complete:       {int(metrics['pct_complete'])}%")
    print(f"  Phases done:      {metrics['n_done_phases']} of "
          f"{metrics['n_total_phases']}")
    print(f"  Original target:  {metrics['original_completion']}")
    print(f"  Revised target:   {metrics['revised_completion']}")

    print(f"\nEnsuring Dashboard tab exists...")
    dashboard_sheet_id = ensure_dashboard_tab(sheets_service, tracker["id"])

    print(f"\nWriting dashboard to sheet...")
    write_dashboard(sheets_service, tracker["id"], dashboard_sheet_id,
                    metrics, project_name)

    print(f"\nApplying visual formatting (colors, dates, conditional rules)...")
    n = apply_visual_formatting(sheets_service, tracker["id"])
    print(f"  {n} formatting requests applied across data tabs.")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print(f"Sheet:  {tracker['webViewLink']}")
    print(f"Update Master Schedule statuses → re-run this script → "
          f"Dashboard refreshes.")
    print()


if __name__ == "__main__":
    main()
