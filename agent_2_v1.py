"""
agent_2_v1.py — Project Orchestrator for Chad (Baldwin County, AL luxury custom homes).

Reads:
  - A project spec (Markdown file in the workspace folder)
  - Baldwin County construction reality reference (knowledge base)
  - Baldwin County supplier research (knowledge base)

Asks Claude (acting as Chad's senior PM) to generate a detailed construction
timeline grounded in Baldwin County code, climate, and verified luxury suppliers.

Saves the timeline as a Google Doc in your Drive Generated Timelines folder.

First run: opens a browser asking you to authorize Drive access.
After that: runs silently using saved token.json.
"""

import io
import json
import os
import re
from datetime import datetime

# Google's OAuth flow can return a different scope set than requested when a
# user has previously authorized the app — set BEFORE oauth libraries import
# so the warning is treated as non-fatal.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import markdown
from anthropic import Anthropic
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# --- Config ----------------------------------------------------------

# Workspace folder = the Cowork-mounted Drive folder. Agent reads/writes here.
WORKSPACE = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-Connorpatton615@icloud.com/"
    "My Drive/Home Building Agent V.1/Home Builder Agent V.1"
)

# Subfolder structure inside WORKSPACE:
#   AGENT CORE/                 ← the agent code lives here (this file)
#   KNOWLEDGE BASE/             ← Baldwin reference files (read each run)
#   PROJECT SPECS/              ← project specs the agent generates timelines for
#   GENERATED TIMELINES/        ← Drive uploads land here as Google Docs
#   ARCHIVE/                    ← old test files, no longer used

# Default project spec (override via SPEC_FILE env var if needed)
SPEC_FILENAME = os.environ.get("SPEC_FILE", "pelican_point.md")
SPEC_PATH = os.path.join(WORKSPACE, "PROJECT SPECS", SPEC_FILENAME)

# Knowledge base files — read at runtime so research updates flow to the agent
# without code changes. If you rename these, update the constants below.
KNOWLEDGE_BASE_DIR = "KNOWLEDGE BASE"
CONSTRUCTION_REF_FILE = "baldwin_county_construction_reference.md"
SUPPLIER_REF_FILE = "baldwin_county_supplier_research.md"
COMM_RULES_FILE = "chad_communication_rules.md"

# Drive folder hierarchy (from My Drive root) where the Google Doc lands.
# All inside the Cowork mount, so everything is in one tree now.
DRIVE_FOLDER_PATH = [
    "Home Building Agent V.1",
    "Home Builder Agent V.1",
    "GENERATED TIMELINES",
]

# Anthropic model and pricing (USD per million tokens)
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 36000
INPUT_COST_PER_M = 3.0
OUTPUT_COST_PER_M = 15.0
# Prompt-caching pricing for Sonnet 4.6:
#   cache write = base × 1.25 (slight premium for first write)
#   cache read  = base × 0.10 (90% discount for cached reads, within ~5-min TTL)
CACHE_WRITE_COST_PER_M = 3.75
CACHE_READ_COST_PER_M = 0.30

# Google OAuth — Drive (upload) + Docs (formatting) + Sheets (tracker workbook)
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

# Document formatting
# 1" = 72 pt. We use 0.75" margins (54 pt) which is tighter than default 1"
# but not so tight things feel cramped.
DOC_MARGIN_PT = 54

# Paragraph spacing applied to ALL paragraphs after upload — gives lists and
# sections visible breathing room without making the doc feel airy.
PARA_SPACE_BEFORE_PT = 6
PARA_SPACE_AFTER_PT = 8
PARA_LINE_SPACING_PCT = 115  # 115% line height (slightly above default)


# --- Knowledge base loading -----------------------------------------

def load_knowledge_base():
    """Read all knowledge base files. Return (construction, supplier, comm_rules) text."""
    construction_path = os.path.join(WORKSPACE, KNOWLEDGE_BASE_DIR, CONSTRUCTION_REF_FILE)
    supplier_path = os.path.join(WORKSPACE, KNOWLEDGE_BASE_DIR, SUPPLIER_REF_FILE)
    comm_rules_path = os.path.join(WORKSPACE, KNOWLEDGE_BASE_DIR, COMM_RULES_FILE)

    construction_text = ""
    supplier_text = ""
    comm_rules_text = ""

    if os.path.exists(construction_path):
        with open(construction_path) as f:
            construction_text = f.read()
        print(f"  Loaded construction reference: {len(construction_text):,} chars")
    else:
        print(f"  WARNING: Construction reference not found at {construction_path}")

    if os.path.exists(supplier_path):
        with open(supplier_path) as f:
            supplier_text = f.read()
        print(f"  Loaded supplier research:      {len(supplier_text):,} chars")
    else:
        print(f"  WARNING: Supplier research not found at {supplier_path}")

    if os.path.exists(comm_rules_path):
        with open(comm_rules_path) as f:
            comm_rules_text = f.read()
        print(f"  Loaded communication rules:    {len(comm_rules_text):,} chars")
    else:
        print(f"  WARNING: Communication rules not found at {comm_rules_path}")

    return construction_text, supplier_text, comm_rules_text


# --- Google Drive helpers --------------------------------------------

def get_google_credentials():
    """Authenticate with Google. Returns Credentials usable for any Google API."""
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
            # prompt='consent' forces the consent screen each time so every
            # requested scope must be granted explicitly (otherwise Google
            # may silently reuse a prior authorization with fewer scopes).
            creds = flow.run_local_server(port=0, prompt="consent")
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def google_drive_service(creds):
    """Build a Drive API service from credentials."""
    return build("drive", "v3", credentials=creds)


def find_folder_by_path(service, path):
    """Walk a folder name path and return the deepest folder's ID."""
    parent_id = "root"
    walked = []
    for name in path:
        walked.append(name)
        query = (
            f"name='{name}' "
            "and mimeType='application/vnd.google-apps.folder' "
            f"and '{parent_id}' in parents "
            "and trashed=false"
        )
        results = service.files().list(
            q=query, fields="files(id, name)"
        ).execute()
        folders = results.get("files", [])
        if not folders:
            raise FileNotFoundError(
                f"Folder not found in Drive: {' / '.join(walked)}"
            )
        parent_id = folders[0]["id"]
    return parent_id


def upload_as_google_doc(service, html, doc_name, parent_folder_id):
    """Upload HTML to Drive as a Google Doc (Drive does the conversion)."""
    metadata = {
        "name": doc_name,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [parent_folder_id],
    }
    media = MediaIoBaseUpload(
        io.BytesIO(html.encode("utf-8")),
        mimetype="text/html",
    )
    return service.files().create(
        body=metadata, media_body=media, fields="id, webViewLink"
    ).execute()


def ensure_archive_folder(service, parent_folder_id):
    """Find or create an ARCHIVE subfolder under parent. Returns its ID."""
    query = (
        "name='ARCHIVE' "
        "and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_folder_id}' in parents "
        "and trashed=false"
    )
    folders = service.files().list(
        q=query, fields="files(id)"
    ).execute().get("files", [])
    if folders:
        return folders[0]["id"]
    folder = service.files().create(
        body={
            "name": "ARCHIVE",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_folder_id],
        },
        fields="id",
    ).execute()
    return folder["id"]


def archive_existing_artifact(service, name, parent_folder_id, archive_folder_id):
    """If a file named `name` exists in parent_folder_id, move it to ARCHIVE
    with a timestamped name suffix so prior runs don't collide.

    Returns the number of files archived (0 if nothing existed).
    """
    query = (
        f"name='{name}' "
        f"and '{parent_folder_id}' in parents "
        "and trashed=false"
    )
    files = service.files().list(
        q=query, fields="files(id,name)"
    ).execute().get("files", [])
    if not files:
        return 0
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    for f in files:
        new_name = f"{f['name']} (archived {stamp})"
        service.files().update(
            fileId=f["id"],
            body={"name": new_name},
            addParents=archive_folder_id,
            removeParents=parent_folder_id,
            fields="id",
        ).execute()
    return len(files)


def extract_json_block(text):
    """Find a ```json ... ``` block in Claude's output and parse it.

    Returns a dict, or None if no parseable block is found.
    """
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
    """Remove the ```json ... ``` block from text so it doesn't appear in the doc."""
    pattern = r"```json\s*\n.*?\n```\s*"
    return re.sub(pattern, "", text, flags=re.DOTALL).rstrip()


def build_tracker_sheet(creds, project_data, sheet_name, parent_folder_id):
    """Create a 3-tab Google Sheet from structured project data.

    Tabs: Master Schedule (phases) | Action Items (tasks) | Order Schedule.
    Returns the file metadata dict (id, webViewLink).
    """
    sheets_service = build("sheets", "v4", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)

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
    sheet = sheets_service.spreadsheets().create(
        body=spreadsheet_body, fields="spreadsheetId,spreadsheetUrl"
    ).execute()
    sheet_id = sheet["spreadsheetId"]

    # 2. Move the sheet into the right Drive folder
    # (created sheets land at My Drive root by default; move to parent)
    drive_service.files().update(
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
            "FALSE",  # Sheets accepts string "FALSE"/"TRUE" for boolean cells
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
    sheets_service.spreadsheets().values().batchUpdate(
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

    # 5. Apply formatting: bold header rows, checkbox columns, status dropdowns
    format_requests = []

    # Bold headers on all three tabs
    for sid in (0, 1, 2):
        format_requests.append({
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.85, "green": 0.92, "blue": 0.83},
                }},
                "fields": "userEnteredFormat(textFormat,backgroundColor)",
            }
        })

    # Status dropdown on Master Schedule column F (index 5)
    format_requests.append({
        "setDataValidation": {
            "range": {"sheetId": 0, "startRowIndex": 1,
                      "startColumnIndex": 5, "endColumnIndex": 6},
            "rule": {
                "condition": {"type": "ONE_OF_LIST", "values": [
                    {"userEnteredValue": "Not Started"},
                    {"userEnteredValue": "In Progress"},
                    {"userEnteredValue": "Done"},
                    {"userEnteredValue": "Blocked"},
                    {"userEnteredValue": "Delayed"},
                ]},
                "showCustomUi": True,
                "strict": False,
            }
        }
    })

    # Checkbox column on Action Items column C (index 2)
    format_requests.append({
        "setDataValidation": {
            "range": {"sheetId": 1, "startRowIndex": 1,
                      "startColumnIndex": 2, "endColumnIndex": 3},
            "rule": {
                "condition": {"type": "BOOLEAN"},
                "strict": True,
            }
        }
    })

    # Order Schedule status dropdown column E (index 4)
    format_requests.append({
        "setDataValidation": {
            "range": {"sheetId": 2, "startRowIndex": 1,
                      "startColumnIndex": 4, "endColumnIndex": 5},
            "rule": {
                "condition": {"type": "ONE_OF_LIST", "values": [
                    {"userEnteredValue": "Not Ordered"},
                    {"userEnteredValue": "Ordered"},
                    {"userEnteredValue": "In Production"},
                    {"userEnteredValue": "Shipped"},
                    {"userEnteredValue": "Delivered"},
                ]},
                "showCustomUi": True,
                "strict": False,
            }
        }
    })

    # Order Schedule "Arrived?" checkbox column F (index 5)
    format_requests.append({
        "setDataValidation": {
            "range": {"sheetId": 2, "startRowIndex": 1,
                      "startColumnIndex": 5, "endColumnIndex": 6},
            "rule": {
                "condition": {"type": "BOOLEAN"},
                "strict": True,
            }
        }
    })

    # Auto-size all columns on each tab
    for sid in (0, 1, 2):
        format_requests.append({
            "autoResizeDimensions": {
                "dimensions": {"sheetId": sid, "dimension": "COLUMNS",
                               "startIndex": 0, "endIndex": 10}
            }
        })

    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": format_requests}
    ).execute()

    return {
        "id": sheet_id,
        "webViewLink": sheet["spreadsheetUrl"],
    }


def apply_doc_formatting(creds, doc_id):
    """Apply margins, paragraph spacing, and convert task markers to checkboxes.

    Uses the Google Docs API. Three layers of formatting:
      1. 0.75" margins all around (tighter than default 1", more breathing
         room than the cramped 0.5").
      2. Uniform paragraph spacing across the document for visual rhythm.
      3. Convert any paragraphs starting with the checkbox markers ("[ ]" or
         "[x]") into native Google Docs checkbox bullets, which Chad can
         actually click to check off.
    """
    docs_service = build("docs", "v1", credentials=creds)

    # --- 1. Set document-wide margins ---
    margin_request = {
        "updateDocumentStyle": {
            "documentStyle": {
                "marginTop":    {"magnitude": DOC_MARGIN_PT, "unit": "PT"},
                "marginBottom": {"magnitude": DOC_MARGIN_PT, "unit": "PT"},
                "marginLeft":   {"magnitude": DOC_MARGIN_PT, "unit": "PT"},
                "marginRight":  {"magnitude": DOC_MARGIN_PT, "unit": "PT"},
            },
            "fields": "marginTop,marginBottom,marginLeft,marginRight",
        }
    }
    docs_service.documents().batchUpdate(
        documentId=doc_id, body={"requests": [margin_request]}
    ).execute()

    # --- 2. Read doc to find the document range and checkbox markers ---
    doc = docs_service.documents().get(documentId=doc_id).execute()
    end_index = doc.get("body", {}).get("content", [])[-1].get(
        "endIndex", 1
    )

    # --- 3. Apply uniform paragraph spacing across the whole doc ---
    spacing_request = {
        "updateParagraphStyle": {
            "range": {"startIndex": 1, "endIndex": end_index - 1},
            "paragraphStyle": {
                "spaceAbove": {"magnitude": PARA_SPACE_BEFORE_PT, "unit": "PT"},
                "spaceBelow": {"magnitude": PARA_SPACE_AFTER_PT, "unit": "PT"},
                "lineSpacing": PARA_LINE_SPACING_PCT,
            },
            "fields": "spaceAbove,spaceBelow,lineSpacing",
        }
    }
    docs_service.documents().batchUpdate(
        documentId=doc_id, body={"requests": [spacing_request]}
    ).execute()

    # --- 4. Find checkbox markers and convert to native Docs checkboxes ---
    convert_checkboxes(docs_service, doc_id)


def convert_checkboxes(docs_service, doc_id):
    """Find paragraphs starting with [ ] or [x] and convert to checkbox bullets.

    Two passes:
      Pass A — for each marker paragraph, delete the literal marker text
      ("[ ] " or "[x] ") so only the task text remains. Track whether it
      was unchecked or checked.
      Pass B — apply BULLET_CHECKBOX preset to those paragraphs. If the
      original was [x], also visually check it (note: as of Docs API today,
      pre-checking via API is limited; checked items render but Chad may
      need to click once to confirm — acceptable for MVP).
    """
    # Re-read the doc since spacing changes shifted indices
    doc = docs_service.documents().get(documentId=doc_id).execute()

    # Walk paragraphs, find ones starting with checkbox markers
    targets = []  # list of (startIndex, endIndex, was_checked)
    for element in doc.get("body", {}).get("content", []):
        para = element.get("paragraph")
        if not para:
            continue
        # Concatenate text runs in this paragraph
        text = ""
        for el in para.get("elements", []):
            tr = el.get("textRun")
            if tr:
                text += tr.get("content", "")
        text_stripped = text.lstrip()
        if text_stripped.startswith("[ ] ") or text_stripped.startswith("[x] "):
            was_checked = text_stripped.startswith("[x] ")
            start_idx = element.get("startIndex")
            end_idx = element.get("endIndex")
            targets.append((start_idx, end_idx, was_checked, text))

    if not targets:
        return  # No checkboxes found; nothing to do

    # Process in REVERSE order so deletions don't shift earlier indices
    requests = []
    for start_idx, end_idx, was_checked, full_text in reversed(targets):
        # Find where "[ ] " or "[x] " starts within this paragraph
        marker_offset = full_text.find("[")
        marker_abs_start = start_idx + marker_offset
        marker_abs_end = marker_abs_start + 4  # "[ ] " or "[x] " is 4 chars

        # Delete the marker text
        requests.append({
            "deleteContentRange": {
                "range": {
                    "startIndex": marker_abs_start,
                    "endIndex": marker_abs_end,
                }
            }
        })

    # Send deletions first
    if requests:
        docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute()

    # Re-read again — indices shifted after deletions
    doc = docs_service.documents().get(documentId=doc_id).execute()

    # Find the now-marker-free task paragraphs and apply checkbox preset.
    # We identify them by looking at our original task text (minus the marker)
    # to re-locate them in the freshly-read doc.
    expected_task_texts = []
    for _, _, was_checked, full_text in targets:
        # Strip the marker from the original text
        stripped = full_text.replace("[ ] ", "", 1).replace("[x] ", "", 1)
        expected_task_texts.append(stripped.strip())

    # Walk the new doc and find paragraphs matching our task texts
    bullet_requests = []
    matched = set()
    for element in doc.get("body", {}).get("content", []):
        para = element.get("paragraph")
        if not para:
            continue
        text = ""
        for el in para.get("elements", []):
            tr = el.get("textRun")
            if tr:
                text += tr.get("content", "")
        text_clean = text.strip()
        for i, expected in enumerate(expected_task_texts):
            if i in matched:
                continue
            if text_clean == expected and expected:
                bullet_requests.append({
                    "createParagraphBullets": {
                        "range": {
                            "startIndex": element.get("startIndex"),
                            "endIndex": element.get("endIndex") - 1,
                        },
                        "bulletPreset": "BULLET_CHECKBOX",
                    }
                })
                matched.add(i)
                break

    if bullet_requests:
        docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": bullet_requests}
        ).execute()
        print(f"  Converted {len(bullet_requests)} task lines to checkboxes.")


# --- Prompt construction --------------------------------------------

def build_system_prompt(construction_ref, supplier_ref, comm_rules):
    """Build the system prompt as cacheable content blocks.

    Returns a list of content blocks compatible with messages.create(system=...).
    Each KB file gets its own cache_control marker so on subsequent runs (within
    the 5-minute cache TTL) the input cost on that block drops by ~90%.

    Anthropic allows up to 4 cache_control markers per request; we use exactly 4:
    role/principles, construction reference, supplier KB, communication rules.
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

    # Cache markers were removed 2026-04-25 after measuring no benefit:
    # Agent 2 runs take ~10 min (32K-token output stream) but Anthropic's
    # default ephemeral cache TTL is 5 min, so consecutive runs always see
    # cache misses. Worse, the 25% write premium added ~$0.023/run with no
    # offsetting reads. Fine-grained caching belongs on Agent 1 (Gmail)
    # where call frequency vs. TTL matches up. See memory: project_active_vs_ondemand.
    return [
        {"type": "text", "text": role_and_principles},
        {"type": "text", "text": construction_block},
        {"type": "text", "text": supplier_block},
        {"type": "text", "text": comm_block},
    ]


def build_user_prompt(spec_content):
    """Build the user-turn prompt with the project spec."""
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


# --- Main ------------------------------------------------------------

def main():
    # 1. Load API key + create Anthropic client
    load_dotenv()
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # 2. Load knowledge base
    print("Loading Baldwin County knowledge base...")
    construction_ref, supplier_ref, comm_rules = load_knowledge_base()

    # 3. Read project spec
    print(f"\nReading spec: {os.path.basename(SPEC_PATH)}")
    with open(SPEC_PATH) as f:
        spec_content = f.read()
    print(f"  ({len(spec_content):,} characters)")

    # 4. Build prompts (system is now a list of cacheable content blocks)
    system_prompt = build_system_prompt(construction_ref, supplier_ref, comm_rules)
    user_prompt = build_user_prompt(spec_content)
    total_system_chars = sum(len(b["text"]) for b in system_prompt)
    print(f"\nSystem prompt: {len(system_prompt)} cached blocks, "
          f"{total_system_chars:,} characters "
          f"(~{total_system_chars//4:,} tokens)")
    print(f"User prompt:   {len(user_prompt):,} characters")

    # 5. Call Claude (streaming because 24K-token generations can exceed
    #    the SDK's 10-minute non-streaming timeout)
    print(f"\nAsking Claude ({MODEL}) to generate the timeline...")
    print("  (streaming — dots show progress, ~2-4 minutes total)")
    print("  ", end="", flush=True)

    chunk_count = 0
    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        # Print a dot every ~50 chunks so we see progress without spamming
        for _ in stream.text_stream:
            chunk_count += 1
            if chunk_count % 50 == 0:
                print(".", end="", flush=True)
        response = stream.get_final_message()

    print()  # newline after the dots
    full_response = response.content[0].text
    print(f"  Generated {response.usage.output_tokens:,} output tokens.")

    # 6a. Extract structured JSON for the tracker sheet
    print("\nExtracting structured data for tracker sheet...")
    project_data = extract_json_block(full_response)
    if project_data:
        n_phases = len(project_data.get("phases", []))
        n_tasks = len(project_data.get("tasks", []))
        n_orders = len(project_data.get("orders", []))
        print(f"  Parsed: {n_phases} phases, {n_tasks} tasks, {n_orders} orders")
    else:
        print("  No JSON block found — sheet will be skipped.")

    # 6b. Strip the JSON from the markdown so it doesn't render in the doc
    timeline_md = strip_json_block(full_response)

    # 6c. Convert markdown → HTML for Drive upload
    # `toc` extension turns a [TOC] marker in the markdown into a
    # hyperlinked table of contents — click any heading to jump.
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

    # 7. Authenticate + upload to Drive
    print("\nAuthenticating with Google...")
    creds = get_google_credentials()
    drive_service = google_drive_service(creds)

    print(f"Finding folder: {' / '.join(DRIVE_FOLDER_PATH)}")
    folder_id = find_folder_by_path(drive_service, DRIVE_FOLDER_PATH)

    project_name = os.path.splitext(SPEC_FILENAME)[0].replace("_", " ").title()
    doc_name = f"Timeline – {project_name}"
    sheet_name = f"Tracker – {project_name}"

    # Idempotency: archive any existing Timeline/Tracker for this project
    # before creating new ones. Prevents duplicates from accumulating on re-runs.
    # Old versions stay in ARCHIVE/ subfolder (recoverable, just out of sight).
    archive_folder_id = ensure_archive_folder(drive_service, folder_id)
    n_archived = 0
    n_archived += archive_existing_artifact(
        drive_service, doc_name, folder_id, archive_folder_id
    )
    n_archived += archive_existing_artifact(
        drive_service, sheet_name, folder_id, archive_folder_id
    )
    if n_archived:
        print(f"Archived {n_archived} prior version(s) → ARCHIVE/")

    print(f"Uploading as Google Doc: {doc_name}")
    file = upload_as_google_doc(drive_service, full_html, doc_name, folder_id)

    # 7b. Apply formatting via Docs API: 0.75" margins, paragraph spacing,
    # convert [ ]/[x] markers to native Google Docs checkboxes.
    # Gracefully skip on failure so a Docs-API error doesn't lose the upload.
    try:
        print("Applying document formatting (margins, spacing, checkboxes)...")
        apply_doc_formatting(creds, file["id"])
    except Exception as e:
        print(f"  WARNING: Could not apply doc formatting: {e}")
        print("  (Doc was still uploaded; manual adjustment available.)")

    # 7c. Build the tracker sheet from the structured JSON (if available)
    # sheet_name was already set above (idempotent: same name = same artifact)
    sheet_url = None
    if project_data:
        try:
            print(f"\nBuilding tracker sheet: {sheet_name}")
            sheet_file = build_tracker_sheet(
                creds, project_data, sheet_name, folder_id
            )
            sheet_url = sheet_file["webViewLink"]
            print(f"  Sheet ready at: {sheet_url}")

            # Apply visual polish (conditional formatting, date formats)
            try:
                from agent_2_5_dashboard import apply_visual_formatting
                from googleapiclient.discovery import build as _build
                sheets_service = _build("sheets", "v4", credentials=creds)
                n = apply_visual_formatting(sheets_service, sheet_file["id"])
                print(f"  Visual polish applied ({n} formatting requests).")
            except Exception as e:
                print(f"  NOTE: Visual polish skipped: {e}")
        except Exception as e:
            print(f"  WARNING: Could not build tracker sheet: {e}")
            print("  (Doc still uploaded; rerun to retry the sheet.)")

    # 8. Report (with cache hit/miss breakdown)
    cache_create = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
    fresh_in = response.usage.input_tokens
    out = response.usage.output_tokens

    cache_create_cost = cache_create * CACHE_WRITE_COST_PER_M / 1_000_000
    cache_read_cost = cache_read * CACHE_READ_COST_PER_M / 1_000_000
    in_cost = fresh_in * INPUT_COST_PER_M / 1_000_000
    out_cost = out * OUTPUT_COST_PER_M / 1_000_000
    total = cache_create_cost + cache_read_cost + in_cost + out_cost

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print(f"Document: {doc_name}")
    print(f"  Doc URL:   {file['webViewLink']}")
    if sheet_url:
        print(f"  Sheet URL: {sheet_url}")
    print()
    print("Token usage:")
    if cache_create:
        print(f"  Cache write:  {cache_create:>7,} tokens (${cache_create_cost:.4f})")
    if cache_read:
        print(f"  Cache read:   {cache_read:>7,} tokens (${cache_read_cost:.4f})  ← 90% off")
    print(f"  Fresh input:  {fresh_in:>7,} tokens (${in_cost:.4f})")
    print(f"  Output:       {out:>7,} tokens (${out_cost:.4f})")
    print(f"  TOTAL:                        ${total:.4f}")
    if cache_read:
        # Estimate what cost would have been without caching
        without_cache = (
            (fresh_in + cache_read) * INPUT_COST_PER_M / 1_000_000
            + out_cost
        )
        savings = without_cache - total
        print(f"  (Saved ~${savings:.4f} via prompt cache hit)")
    print()


if __name__ == "__main__":
    main()
