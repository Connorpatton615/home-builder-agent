"""
agent_1_gmail.py — Gmail Follow-Up Checklist Agent.

Reads recent Gmail threads, identifies which ones need a follow-up from Chad,
classifies by urgency, and produces a Chad-voice action checklist.

Usage:
  python3 agent_1_gmail.py                   # Defaults to last 7 days, inbox
  python3 agent_1_gmail.py --days 14         # Look back 14 days
  python3 agent_1_gmail.py --upload          # Also upload checklist as Google Doc

Pipeline:
  1. Authenticate (reuses token.json if Gmail scope already granted)
  2. List recent inbox threads (configurable lookback window)
  3. For each thread, fetch headers + snippet
  4. Classify each via Claude Haiku: needs-follow-up? urgency?
  5. Aggregate, then send all needs-follow-up threads to Sonnet w/ comm rules
  6. Output: a Chad-voice Markdown checklist (terminal + optional Google Doc)

Cost per run depends on volume:
  - Haiku classification: ~$0.001 per thread
  - Sonnet checklist generation: ~$0.02-0.05 once
For a typical inbox of 30-50 recent threads: ~$0.05-0.10 per run.

REQUIRES: Gmail API enabled in Cloud Console + gmail.readonly scope on OAuth.
First run will trigger a browser re-auth to grant the new scope.
"""

import argparse
import io
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime

# Allow OAuth to handle Google's incremental authorization
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

WORKSPACE = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-Connorpatton615@icloud.com/"
    "My Drive/Home Building Agent V.1/Home Builder Agent V.1"
)
KNOWLEDGE_BASE_DIR = "KNOWLEDGE BASE"
COMM_RULES_FILE = "chad_communication_rules.md"

# Google OAuth — adds Gmail readonly to existing scopes
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.readonly",
]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

# Drive folder where the (optional) checklist Google Doc lands
DRIVE_FOLDER_PATH = [
    "Home Building Agent V.1",
    "Home Builder Agent V.1",
    "GENERATED TIMELINES",  # share the same output bucket for now
]

# Anthropic models
CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
WRITER_MODEL = "claude-sonnet-4-6"

# Pricing (USD per million tokens)
HAIKU_INPUT_COST = 1.0
HAIKU_OUTPUT_COST = 5.0
SONNET_INPUT_COST = 3.0
SONNET_OUTPUT_COST = 15.0

# How many threads to look at (caps the loop in case the inbox is huge)
MAX_THREADS_TO_CLASSIFY = 100


# --- Auth ------------------------------------------------------------

def get_credentials():
    """Authenticate with Google. Will trigger re-auth if Gmail scope is new."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, GOOGLE_SCOPES
            )
            creds = flow.run_local_server(port=0, prompt="consent")
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


# --- Gmail reading ---------------------------------------------------

def list_recent_threads(gmail_service, days=7, max_results=MAX_THREADS_TO_CLASSIFY):
    """List threads in the inbox modified in the last N days.

    Returns a list of thread metadata dicts (id, snippet, historyId).
    """
    after_date = (date.today() - timedelta(days=days)).isoformat()
    query = f"in:inbox after:{after_date}"

    threads = []
    page_token = None
    while len(threads) < max_results:
        result = gmail_service.users().threads().list(
            userId="me",
            q=query,
            maxResults=min(50, max_results - len(threads)),
            pageToken=page_token,
        ).execute()
        threads.extend(result.get("threads", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return threads[:max_results]


def get_thread_summary(gmail_service, thread_id, my_email):
    """Fetch a thread's headers + snippet for classification.

    Returns a dict suitable for sending to the classifier prompt.
    """
    thread = gmail_service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["Subject", "From", "To", "Cc", "Date"],
    ).execute()

    messages = thread.get("messages", [])
    if not messages:
        return None

    last_message = messages[-1]
    headers = {
        h["name"].lower(): h["value"]
        for h in last_message.get("payload", {}).get("headers", [])
    }

    # Determine if last message is from "me" (Chad/CP) — that flips
    # whether we're awaiting a reply or owe one
    last_from_email = parseaddr(headers.get("from", ""))[1].lower()
    last_from_me = my_email.lower() in last_from_email

    # Parse date
    date_str = headers.get("date", "")
    try:
        last_date = parsedate_to_datetime(date_str)
        if last_date.tzinfo is None:
            last_date = last_date.replace(tzinfo=timezone.utc)
        days_old = (datetime.now(timezone.utc) - last_date).days
    except Exception:
        last_date = None
        days_old = -1

    # Combine snippets from messages for context
    snippet = thread.get("snippet", "") or last_message.get("snippet", "")

    return {
        "thread_id": thread_id,
        "subject": headers.get("subject", "(no subject)"),
        "from_name": parseaddr(headers.get("from", ""))[0]
                     or parseaddr(headers.get("from", ""))[1],
        "from_email": last_from_email,
        "last_from_me": last_from_me,
        "snippet": snippet[:400],  # cap length for token efficiency
        "days_old": days_old,
        "message_count": len(messages),
    }


# --- Classification (Haiku) -----------------------------------------

def classify_thread(client, summary):
    """Ask Haiku: does this thread need follow-up from Chad?

    Returns dict: {needs_followup: bool, urgency: str, reason: str}
    """
    prompt = f"""Classify whether this email thread needs a follow-up from Chad (a custom home builder running $1M+ luxury projects in Baldwin County, AL).

THREAD METADATA:
- Subject: {summary['subject']}
- From: {summary['from_name']} <{summary['from_email']}>
- Last message from Chad? {summary['last_from_me']}
- Days since last message: {summary['days_old']}
- Message count in thread: {summary['message_count']}
- Snippet: {summary['snippet']}

Return ONLY a JSON object (no fence, no preamble):
{{
  "needs_followup": <true|false>,
  "urgency": "<high|medium|low|none>",
  "reason": "<one short phrase explaining why>"
}}

Classification rules:
- If last message is FROM Chad and >2 days old with no response → likely needs nudge (medium urgency).
- If last message is TO Chad (he hasn't replied) and >1 day old → he owes a response (high urgency if from a paying client/vendor; medium otherwise).
- Newsletter/marketing/notification → needs_followup: false, urgency: none.
- Automated bookings/confirmations → false unless action needed.
- Internal team CC where someone else is primary → false unless directly addressed.
- Be biased toward FALSE for ambiguous threads — Chad doesn't want a checklist full of noise."""

    response = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = {"needs_followup": False, "urgency": "none",
                  "reason": "classifier output unparseable"}

    return result, response.usage


# --- Checklist generation (Sonnet w/ comm rules) -------------------

def load_comm_rules():
    path = os.path.join(WORKSPACE, KNOWLEDGE_BASE_DIR, COMM_RULES_FILE)
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return ""


def generate_checklist(client, threads_with_classifications, today=None):
    """Take all needs-followup threads and write a Chad-voice action checklist."""
    if today is None:
        today = date.today()

    comm_rules = load_comm_rules()

    # Build the thread list for the prompt
    thread_lines = []
    for t, c in threads_with_classifications:
        line = (
            f"- Thread: {t['subject']}\n"
            f"  From: {t['from_name']} <{t['from_email']}>\n"
            f"  Last activity: {t['days_old']} days ago | {t['message_count']} messages | "
            f"last from Chad: {t['last_from_me']}\n"
            f"  Snippet: {t['snippet']}\n"
            f"  Classification: urgency={c['urgency']}, reason={c['reason']}"
        )
        thread_lines.append(line)
    threads_text = "\n\n".join(thread_lines)

    system_prompt = f"""You are a project communication agent for Palmetto Custom Homes.

Apply Chad's communication style strictly — direct, calm, status-led, scannable, no fluff.

<chad_communication_rules>
{comm_rules}
</chad_communication_rules>

You produce a Markdown email follow-up checklist that Chad can scan in 60 seconds and act on. Group by urgency (HIGH first, then MEDIUM, then LOW). For each item: who, what's needed, and a one-line recommended action. Use functional status emojis (🔴 high, 🟡 medium, ⚪ low) for at-a-glance scanning."""

    user_prompt = f"""Today: {today.isoformat()}

Below are the email threads that the classifier flagged as needing a follow-up from Chad. Generate an action checklist organized by urgency.

REQUIRED FORMAT:

# Email Follow-Up Checklist — {today.isoformat()}

## 🔴 HIGH (respond today)
- [ ] **{{Sender name}}** — {{1-line subject context}} → recommended action

## 🟡 MEDIUM (respond this week)
- [ ] **{{Sender name}}** — context → action

## ⚪ LOW (when there's time)
- [ ] **{{Sender name}}** — context → action

End with a one-line summary: "{{N}} threads need attention. {{N}} high priority."

If no threads in a category, omit that category entirely.

THREADS:
{threads_text}"""

    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return response.content[0].text, response.usage


# --- Optional Google Doc upload -------------------------------------

def get_my_email(gmail_service):
    """Get the authenticated user's email address."""
    profile = gmail_service.users().getProfile(userId="me").execute()
    return profile.get("emailAddress", "")


def find_drive_folder(drive_service, folder_path):
    """Walk a folder name path to find the deepest folder ID."""
    parent_id = "root"
    for name in folder_path:
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
            return None
        parent_id = folders[0]["id"]
    return parent_id


def upload_checklist_doc(drive_service, markdown_text, doc_name, parent_folder_id):
    """Upload the checklist as a Google Doc."""
    html_body = markdown.markdown(
        markdown_text, extensions=["tables", "fenced_code", "nl2br"]
    )
    full_html = (
        '<!DOCTYPE html><html><head>'
        '<meta charset="utf-8"><title>Follow-Up Checklist</title>'
        '</head><body>' + html_body + '</body></html>'
    )

    metadata = {
        "name": doc_name,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [parent_folder_id] if parent_folder_id else [],
    }
    media = MediaIoBaseUpload(
        io.BytesIO(full_html.encode("utf-8")), mimetype="text/html"
    )
    return drive_service.files().create(
        body=metadata, media_body=media, fields="id,webViewLink"
    ).execute()


# --- Main ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate a Chad-voice email follow-up checklist."
    )
    parser.add_argument("--days", type=int, default=7,
                        help="How many days back to scan (default: 7)")
    parser.add_argument("--upload", action="store_true",
                        help="Also upload the checklist as a Google Doc")
    parser.add_argument("--max-threads", type=int, default=MAX_THREADS_TO_CLASSIFY,
                        help=f"Max threads to classify (default: "
                             f"{MAX_THREADS_TO_CLASSIFY})")
    args = parser.parse_args()

    print("Authenticating with Google...")
    creds = get_credentials()
    gmail_service = build("gmail", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds) if args.upload else None

    load_dotenv()
    anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    my_email = get_my_email(gmail_service)
    print(f"  Account: {my_email}")

    print(f"\nListing inbox threads (last {args.days} days)...")
    threads = list_recent_threads(gmail_service, days=args.days,
                                  max_results=args.max_threads)
    print(f"  Found {len(threads)} threads to inspect.")

    if not threads:
        print("\nNo recent threads. Nothing to do.")
        return

    print(f"\nClassifying threads via {CLASSIFIER_MODEL}...")
    needs_followup = []
    classifier_input_total = 0
    classifier_output_total = 0

    for i, thread in enumerate(threads, 1):
        summary = get_thread_summary(gmail_service, thread["id"], my_email)
        if not summary:
            continue
        try:
            classification, usage = classify_thread(anthropic_client, summary)
        except Exception as e:
            print(f"  [{i}/{len(threads)}] {summary['subject'][:60]} — "
                  f"classifier error: {e}")
            continue

        classifier_input_total += usage.input_tokens
        classifier_output_total += usage.output_tokens

        marker = ("🔴" if classification.get("urgency") == "high"
                  else "🟡" if classification.get("urgency") == "medium"
                  else "⚪" if classification.get("needs_followup") else "  ")
        flag = "✓" if classification.get("needs_followup") else " "
        print(f"  [{i}/{len(threads)}] {marker} {flag} {summary['subject'][:70]}")

        if classification.get("needs_followup"):
            needs_followup.append((summary, classification))

    print(f"\n  {len(needs_followup)} of {len(threads)} threads need follow-up.")

    if not needs_followup:
        print("\nNothing actionable. Inbox zero today.")
        return

    print(f"\nGenerating Chad-voice checklist via {WRITER_MODEL}...")
    checklist, writer_usage = generate_checklist(
        anthropic_client, needs_followup
    )

    # Cost calc
    classifier_cost = (
        classifier_input_total * HAIKU_INPUT_COST / 1_000_000
        + classifier_output_total * HAIKU_OUTPUT_COST / 1_000_000
    )
    writer_cost = (
        writer_usage.input_tokens * SONNET_INPUT_COST / 1_000_000
        + writer_usage.output_tokens * SONNET_OUTPUT_COST / 1_000_000
    )

    print("\n" + "=" * 60)
    print("FOLLOW-UP CHECKLIST")
    print("=" * 60)
    print()
    print(checklist)
    print()

    if args.upload and drive_service:
        print("Uploading checklist as Google Doc...")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        doc_name = f"Email Follow-Ups – {timestamp}"
        folder_id = find_drive_folder(drive_service, DRIVE_FOLDER_PATH)
        try:
            file = upload_checklist_doc(drive_service, checklist,
                                        doc_name, folder_id)
            print(f"  Doc URL: {file['webViewLink']}")
        except Exception as e:
            print(f"  Upload failed: {e}")

    print("=" * 60)
    print(f"Cost:  classifier=${classifier_cost:.4f} "
          f"({len(threads)} threads × ~${classifier_cost/max(len(threads),1):.5f})")
    print(f"       writer=${writer_cost:.4f}")
    print(f"       TOTAL=${classifier_cost + writer_cost:.4f}")
    print()


if __name__ == "__main__":
    main()
