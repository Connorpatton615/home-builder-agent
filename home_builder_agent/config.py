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

# Watcher
WATCHER_TIMEOUT_SEC = 90       # hard kill if a poll exceeds this
WATCHER_SOCKET_TIMEOUT = 45    # blocking socket call timeout
WATCHER_MAX_ERRORS_PER_RUN = 5
