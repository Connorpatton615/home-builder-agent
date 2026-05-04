"""config.py — single source of truth for paths, scopes, model names, pricing.

Every other module in the package imports its constants from here. If a path,
scope, or model name needs to change, there's exactly ONE place to change it.

Why centralize this:
- Phase 1 had each agent file redefine WORKSPACE, DRIVE_FOLDER_PATH, GOOGLE_SCOPES,
  pricing constants, etc. — five copies of the same hardcoded values, drifting
  out of sync over time. That's the bug Phase 2 starts to feel.
- Centralizing makes adding Agent N+1 a config-update + import, not a
  copy-paste-and-pray.
"""

import os

# ---------------------------------------------------------------------
# Filesystem paths
# ---------------------------------------------------------------------

# The Drive folder Cowork mounts and where all knowledge base, project specs,
# and generated artifacts live. The agent code USED to live inside this folder
# too (under AGENT CORE/) but post-restructure the code lives at ~/Projects/
# and only reads/writes data here.
WORKSPACE = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-Connorpatton615@icloud.com/"
    "My Drive/Home Building Agent V.1/Home Builder Agent V.1"
)

# Subfolder names inside WORKSPACE
KNOWLEDGE_BASE_DIR = "KNOWLEDGE BASE"
PROJECT_SPECS_DIR = "PROJECT SPECS"
GENERATED_TIMELINES_DIR = "GENERATED TIMELINES"
ARCHIVE_DIR = "ARCHIVE"
SITE_LOGS_DIR = "Site Logs"

# Knowledge-base filenames (read at runtime by every agent that needs them)
CONSTRUCTION_REF_FILE = "baldwin_county_construction_reference.md"
SUPPLIER_REF_FILE = "baldwin_county_supplier_research.md"
COMM_RULES_FILE = "chad_communication_rules.md"

# Default project spec used if the timeline generator is invoked with no args
DEFAULT_SPEC_FILENAME = "pelican_point.md"

# Drive folder hierarchy from My Drive root — the timeline generator and
# dashboard refresher walk this path to find/create artifacts.
DRIVE_FOLDER_PATH = [
    "Home Building Agent V.1",
    "Home Builder Agent V.1",
    GENERATED_TIMELINES_DIR,
]

# Finance Office — folder path and active project name.
# Changing FINANCE_PROJECT_NAME here switches hb-finance to a new project.
FINANCE_FOLDER_PATH = [
    "Home Building Agent V.1",
    "Home Builder Agent V.1",
    "Chad's Finance Office",
]
FINANCE_PROJECT_NAME = "Whitfield Residence"

# Receipts folder name inside Chad's Finance Office (auto-created by hb-receipt)
FINANCE_RECEIPTS_DIR = "Receipts"

# Invoice watcher settings
INVOICE_NOTIFY_THRESHOLD = 1000   # fire macOS notification for invoices >= $1,000
INVOICE_SIGNAL_WORDS = [          # subject/snippet keywords that flag invoice emails
    "invoice", "bill ", "billing", "statement", "payment due",
    "balance due", "amount due", "please pay", "remittance",
    "estimate", "proposal", "quote",
]

# ---------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------

# Credentials live in the project root next to .env (gitignored, never committed)
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

# Full scope set — every agent gets every scope so a single token.json works
# across all of them. The Gmail agent NEEDS gmail.readonly; the others don't,
# but having it in the bundle costs nothing and avoids re-auth on first run
# of the Gmail agent.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
]

# ---------------------------------------------------------------------
# Anthropic models
# ---------------------------------------------------------------------

# The "writer" model — used wherever Chad's voice or Baldwin grounding matters
# (timeline generation, status summaries, follow-up checklists)
WRITER_MODEL = "claude-sonnet-4-6"

# The "classifier/parser" model — used for cheap structured extraction
# (Gmail thread classification, NL update parsing)
CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------
# Anthropic pricing (USD per million tokens)
# ---------------------------------------------------------------------

# Prices change occasionally; keep them here so the cost reporting in agents
# stays accurate after a price update without grepping the codebase.
SONNET_INPUT_COST = 3.0
SONNET_OUTPUT_COST = 15.0
SONNET_CACHE_WRITE_COST = 3.75   # base × 1.25
SONNET_CACHE_READ_COST = 0.30    # base × 0.10 (90% off, ~5-min TTL)

HAIKU_INPUT_COST = 1.0
HAIKU_OUTPUT_COST = 5.0

# ---------------------------------------------------------------------
# Document formatting (Google Docs)
# ---------------------------------------------------------------------

# 1" = 72 pt. We use 0.75" margins (54 pt) — tighter than default 1" but not
# cramped. Same for paragraph spacing, tuned to Chad's preferred density.
DOC_MARGIN_PT = 54
PARA_SPACE_BEFORE_PT = 6
PARA_SPACE_AFTER_PT = 8
PARA_LINE_SPACING_PCT = 115

# ---------------------------------------------------------------------
# Behavior tuning
# ---------------------------------------------------------------------

# Timeline generator
TIMELINE_MAX_TOKENS = 36000

# Gmail follow-up agent
GMAIL_MAX_THREADS_TO_CLASSIFY = 100
GMAIL_DEFAULT_LOOKBACK_DAYS = 7

# Status updater
UPDATE_PARSER_MAX_TOKENS = 500
UPDATE_SUMMARY_MAX_TOKENS = 1500

# Watcher (dashboard)
WATCHER_TIMEOUT_SEC = 90       # hard kill if a poll exceeds this
WATCHER_SOCKET_TIMEOUT = 45    # blocking socket call timeout
WATCHER_MAX_ERRORS_PER_RUN = 5

# Watcher (inbox)
INBOX_WATCHER_TIMEOUT_SEC = 90
INBOX_WATCHER_NOTIFY_HIGH = True   # macOS notification on high-urgency hits

# Help desk agent
CLAUDE_COMMANDS_DIR = ".claude/commands"
HELP_DESK_DOC_FOLDER = [
    "Home Building Agent V.1",
    "Home Builder Agent V.1",
]
HELP_DESK_STATE_FILE = ".help_desk_state.json"

# ---------------------------------------------------------------------
# Morning Brief
# ---------------------------------------------------------------------

# Recipient email address.  Switch to Chad's real email before going live.
BRIEF_RECIPIENT_EMAIL = "aiwithconnor@gmail.com"

# Sender display name (Gmail "From" label)
BRIEF_SENDER_NAME = "Palmetto Custom Homes — AI Assistant"

# Default job-site coordinates for NOAA weather lookup.
# These point to central Baldwin County, AL (near Foley).
# Override per-project by setting env vars BRIEF_LAT / BRIEF_LNG.
import os as _os
BRIEF_SITE_LAT = float(_os.environ.get("BRIEF_LAT", "30.4883"))
BRIEF_SITE_LNG = float(_os.environ.get("BRIEF_LNG", "-87.7264"))

# Job-site address string (human-readable, appears in email header).
BRIEF_SITE_ADDRESS = _os.environ.get(
    "BRIEF_SITE_ADDRESS", "Baldwin County, AL"
)

# Morning brief Sonnet max tokens
BRIEF_MAX_TOKENS = 2000

# Inbox watcher state + log (read by morning brief to surface overnight alerts)
INBOX_WATCHER_STATE_FILE = ".inbox_watcher_state.json"
INBOX_WATCHER_LOG_FILE   = "inbox_watcher.log"

# ---------------------------------------------------------------------
# Change Order agent
# ---------------------------------------------------------------------

# Sub-folder created inside Chad's Finance Office to store CO documents.
CHANGE_ORDERS_DIR = "Change Orders"

# Default client email for CO approval drafts (override per-project or via --client-email).
CO_CLIENT_EMAIL = ""

# Max tokens for the CO document generator prompt.
CO_MAX_TOKENS = 2000

# ---------------------------------------------------------------------
# Procurement Alert System
# ---------------------------------------------------------------------

# Lead times (in weeks) keyed on lowercase substrings of phase names.
# When a phase name contains one of these keywords, the system checks
# whether today falls inside the ordering window (phase start - lead time).
# Edit this dict to tune for Chad's real suppliers — no code change needed.
PROCUREMENT_LEAD_TIMES: dict[str, int] = {
    "window":        8,   # Anderson/PGT windows — 8-week factory lead
    "door":          6,   # Exterior doors (entry, garage) — 6 weeks
    "truss":         6,   # Roof trusses — 6 weeks
    "lumber":        4,   # Dimensional lumber / framing package — 4 weeks
    "cabinet":      10,   # Custom cabinets — 10 weeks
    "appliance":     8,   # Appliances — 8 weeks
    "hvac":          6,   # HVAC equipment (AHU, condenser) — 6 weeks
    "elevator":     16,   # Elevator / lift — 16 weeks
    "generator":     8,   # Whole-home generator — 8 weeks
    "tile":          4,   # Tile / stone — 4 weeks
    "flooring":      4,   # Hardwood / LVP flooring — 4 weeks
    "roofing":       2,   # Roofing materials — 2 weeks
    "plumbing":      3,   # Plumbing fixtures / rough-in materials — 3 weeks
    "electrical":    3,   # Electrical fixtures / panel — 3 weeks
    "insulation":    2,   # Insulation — 2 weeks
    "drywall":       2,   # Drywall — 2 weeks
    "concrete":      2,   # Concrete / flatwork — 2 weeks
    "steel":         4,   # Structural steel / LVL beams — 4 weeks
    "pool":         12,   # Pool equipment / shell — 12 weeks
    "brick":         3,   # Brick / masonry — 3 weeks
    "stucco":        2,   # Stucco / exterior finish — 2 weeks
}

# How many days before the order-by date to start firing "upcoming" alerts.
PROCUREMENT_UPCOMING_DAYS = 14

# Tab name in the Tracker sheet where alerts are logged.
PROCUREMENT_ALERTS_TAB = "Procurement Alerts"

# ---------------------------------------------------------------------
# Lien Waiver Tracker
# ---------------------------------------------------------------------

# Tab name in the Cost Tracker sheet for the waiver log.
LIEN_WAIVERS_TAB = "Lien Waivers"

# Payments below this dollar amount are exempt from waiver tracking by default.
# Most luxury builds collect waivers from any sub paid > $500, but this is
# tunable per-tenant if Chad wants stricter or looser tracking.
LIEN_WAIVER_THRESHOLD = 500.0

# Days within which a waiver must be filed AFTER a payment to count as "matched".
# 60 days is a conservative default — if no waiver in 60 days it's likely missing.
LIEN_WAIVER_MATCH_WINDOW_DAYS = 60

# Dollar tolerance when matching waiver amount to payment amount.
LIEN_WAIVER_AMOUNT_TOLERANCE = 10.0
