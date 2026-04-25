# Home Builder Agent

AI-powered project orchestrator for Chad's Custom Homes — Baldwin County, Alabama luxury custom home builder.

Built in Python using the Anthropic Claude API + Google Drive/Docs/Sheets APIs.

## What it does

Three agents that automate construction project documentation and tracking:

**Agent 2 (`agent_2_v1.py`)** — generates a detailed construction timeline from a project specification. Output: a polished Google Doc + a 3-tab interactive Google Sheet (Master Schedule, Action Items, Order Schedule). Reads three knowledge-base files at runtime: Baldwin County construction reference, supplier research, and the customer's communication style guide.

**Agent 2.5 Dashboard (`agent_2_5_dashboard.py`)** — reads the latest tracker sheet, computes status metrics (current stage, upcoming stage, % complete, revised completion date), and adds a Dashboard tab. Also applies visual formatting (conditional colors, date formats) across all tabs.

**Agent 2.5 Update (`agent_2_5_update.py`)** — takes a natural-language status update ("Phase 3 pushed 1 week"), parses it via Claude Haiku, computes cascade impact through the project's dependency graph, applies the changes to the spreadsheet, refreshes the Dashboard, and returns a Chad-style summary.

## Setup

```bash
# 1. Install dependencies
pip3 install anthropic python-dotenv markdown \
    google-auth google-auth-oauthlib google-api-python-client

# 2. Add credentials
# Create .env with your Anthropic key:
echo 'ANTHROPIC_API_KEY=sk-ant-api03-...' > .env

# 3. Add Google OAuth credentials
# Download from console.cloud.google.com and save as credentials.json

# 4. First run will open a browser for Google OAuth consent
python3 agent_2_v1.py
```

## Files in this repo

- `agent_2_v1.py` — main timeline generator (~$0.55 per run, drops to ~$0.20 with cache)
- `agent_2_5_dashboard.py` — dashboard refresh + visual formatting (no Claude cost)
- `agent_2_5_update.py` — natural-language status updater (~$0.02 per update)
- `hello_claude.py` — initial sanity-check script for the Anthropic SDK

## What's NOT in this repo (gitignored)

- `.env` — Anthropic API key
- `credentials.json` — Google OAuth client config
- `token.json` — saved Google auth token (created on first run)
- Project-specific knowledge base files (live in Drive workspace, not repo)
- Generated timelines (live in Drive workspace, not repo)

## Architecture

The agents read knowledge-base files (`baldwin_county_construction_reference.md`, `baldwin_county_supplier_research.md`, `chad_communication_rules.md`) from a Google Drive sync folder at runtime, so editing the knowledge base files in Drive immediately changes what the agent knows on the next run — no code change needed.

Outputs (timelines, sheets) land in the same Drive folder under `GENERATED TIMELINES/`.

## Cost economics

For a builder doing $1M+ luxury custom homes, per-project agent cost is well under $1 in Claude API charges. The PM hours saved vs. manual spreadsheet maintenance + one-off timeline drafts run into the thousands per project.

## License

Private — not for distribution.
