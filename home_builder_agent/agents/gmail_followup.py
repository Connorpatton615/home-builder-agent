"""gmail_followup.py — Gmail Follow-Up Checklist agent.

Reads recent Gmail threads, classifies via Claude Haiku, generates a
Chad-voice action checklist via Claude Sonnet.

Pipeline:
  1. Auth + list inbox threads (configurable lookback)
  2. Pull metadata + snippet for each
  3. Haiku classifies: needs-follow-up? urgency?
  4. Sonnet writes the consolidated checklist (with Chad communication rules)
  5. Print to stdout, optionally upload as a Google Doc

Cost: ~$0.05–0.10 for a typical 30–50 thread inbox scan.

CLI:
  hb-inbox                     # last 7 days, terminal output
  hb-inbox --days 14           # 2-week lookback
  hb-inbox --upload            # also save as Google Doc in GENERATED TIMELINES/
"""

import argparse
import io
import json
import re
from datetime import date, datetime

import markdown
from googleapiclient.http import MediaIoBaseUpload

from home_builder_agent.config import (
    CLASSIFIER_MODEL,
    DRIVE_FOLDER_PATH,
    GMAIL_DEFAULT_LOOKBACK_DAYS,
    GMAIL_MAX_THREADS_TO_CLASSIFY,
    HAIKU_INPUT_COST,
    HAIKU_OUTPUT_COST,
    WRITER_MODEL,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.claude_client import (
    haiku_cost,
    make_client,
    sonnet_cost,
)
from home_builder_agent.core.knowledge_base import load_comm_rules
from home_builder_agent.integrations import drive
from home_builder_agent.integrations import gmail as gmail_int


# ---------------------------------------------------------------------
# Classification (Haiku)
# ---------------------------------------------------------------------

def classify_thread(client, summary):
    """Ask Haiku: does this thread need follow-up from Chad?

    Returns (classification_dict, usage).
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


# ---------------------------------------------------------------------
# Checklist generation (Sonnet, Chad voice)
# ---------------------------------------------------------------------

def generate_checklist(client, threads_with_classifications, today=None):
    """Take all needs-followup threads and write a Chad-voice action checklist."""
    if today is None:
        today = date.today()

    comm_rules = load_comm_rules()

    thread_lines = []
    for t, c in threads_with_classifications:
        line = (
            f"- Thread: {t['subject']}\n"
            f"  From: {t['from_name']} <{t['from_email']}>\n"
            f"  Last activity: {t['days_old']} days ago | "
            f"{t['message_count']} messages | last from Chad: {t['last_from_me']}\n"
            f"  Snippet: {t['snippet']}\n"
            f"  Classification: urgency={c['urgency']}, reason={c['reason']}"
        )
        thread_lines.append(line)
    threads_text = "\n\n".join(thread_lines)

    system_prompt = f"""You are a project communication agent for Chad's Custom Homes.

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


# ---------------------------------------------------------------------
# Optional Doc upload
# ---------------------------------------------------------------------

def upload_checklist_doc(drive_svc, markdown_text, doc_name, parent_folder_id):
    """Upload the markdown checklist as a Google Doc."""
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
    return drive_svc.files().create(
        body=metadata, media_body=media, fields="id,webViewLink"
    ).execute()


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate a Chad-voice email follow-up checklist."
    )
    parser.add_argument("--days", type=int, default=GMAIL_DEFAULT_LOOKBACK_DAYS,
                        help=f"How many days back to scan "
                             f"(default: {GMAIL_DEFAULT_LOOKBACK_DAYS})")
    parser.add_argument("--upload", action="store_true",
                        help="Also upload the checklist as a Google Doc")
    parser.add_argument("--max-threads", type=int,
                        default=GMAIL_MAX_THREADS_TO_CLASSIFY,
                        help=f"Max threads to classify "
                             f"(default: {GMAIL_MAX_THREADS_TO_CLASSIFY})")
    args = parser.parse_args()

    print("Authenticating with Google...")
    creds = get_credentials()
    gmail_svc = gmail_int.gmail_service(creds)
    drive_svc = drive.drive_service(creds) if args.upload else None

    client = make_client()

    my_email = gmail_int.get_my_email(gmail_svc)
    print(f"  Account: {my_email}")

    print(f"\nListing inbox threads (last {args.days} days)...")
    threads = gmail_int.list_recent_threads(
        gmail_svc, days=args.days, max_results=args.max_threads
    )
    print(f"  Found {len(threads)} threads to inspect.")

    if not threads:
        print("\nNo recent threads. Nothing to do.")
        return

    print(f"\nClassifying threads via {CLASSIFIER_MODEL}...")
    needs_followup = []
    classifier_input_total = 0
    classifier_output_total = 0

    for i, thread in enumerate(threads, 1):
        summary = gmail_int.get_thread_summary(gmail_svc, thread["id"], my_email)
        if not summary:
            continue
        try:
            classification, usage = classify_thread(client, summary)
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
    checklist, writer_usage = generate_checklist(client, needs_followup)

    classifier_usd = (
        classifier_input_total * HAIKU_INPUT_COST / 1_000_000
        + classifier_output_total * HAIKU_OUTPUT_COST / 1_000_000
    )
    writer_usd = sonnet_cost(writer_usage)["total"]

    print("\n" + "=" * 60)
    print("FOLLOW-UP CHECKLIST")
    print("=" * 60)
    print()
    print(checklist)
    print()

    if args.upload and drive_svc:
        print("Uploading checklist as Google Doc...")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        doc_name = f"Email Follow-Ups – {timestamp}"
        try:
            folder_id = drive.find_folder_by_path(drive_svc, DRIVE_FOLDER_PATH)
            file = upload_checklist_doc(drive_svc, checklist, doc_name, folder_id)
            print(f"  Doc URL: {file['webViewLink']}")
        except Exception as e:
            print(f"  Upload failed: {e}")

    print("=" * 60)
    print(f"Cost:  classifier=${classifier_usd:.4f} "
          f"({len(threads)} threads × ~${classifier_usd/max(len(threads),1):.5f})")
    print(f"       writer=${writer_usd:.4f}")
    print(f"       TOTAL=${classifier_usd + writer_usd:.4f}")
    print()


if __name__ == "__main__":
    main()
