"""gmail_followup.py — Gmail Follow-Up Checklist agent.

Reads recent Gmail threads, classifies via Claude Haiku, generates a
Chad-voice action checklist via Claude Sonnet, AND now drafts
individual reply emails for HIGH-urgency threads.

Pipeline:
  1. Auth + list inbox threads (configurable lookback)
  2. Pull metadata + snippet for each
  3. Haiku classifies: needs-follow-up? urgency?
  4. For HIGH-urgency threads where Chad wasn't last sender: Sonnet
     drafts a reply in Chad's voice → gmail.create_draft → log to
     home_builder.draft_action so the morning view's judgment_queue
     surfaces it. (NEW — see step 4 below.)
  5. Sonnet writes the consolidated checklist (with Chad communication rules)
  6. Print to stdout, optionally upload as a Google Doc

Cost: ~$0.05–0.10 for a typical 30–50 thread inbox scan, +~$0.01 per
high-urgency reply drafted.

CLI:
  hb-inbox                     # last 7 days, terminal output
  hb-inbox --days 14           # 2-week lookback
  hb-inbox --upload            # also save as Google Doc in GENERATED TIMELINES/
  hb-inbox --no-drafts         # skip the per-thread reply drafting (saves $$)
"""

import argparse
import io
import json
import re
from datetime import date, datetime

import markdown
from googleapiclient.http import MediaIoBaseUpload

from home_builder_agent.classifiers.email import classify_thread
from home_builder_agent.config import (
    CLASSIFIER_MODEL,
    DRIVE_FOLDER_PATH,
    FINANCE_PROJECT_NAME,
    GMAIL_DEFAULT_LOOKBACK_DAYS,
    GMAIL_MAX_THREADS_TO_CLASSIFY,
    HAIKU_INPUT_COST,
    HAIKU_OUTPUT_COST,
    WRITER_MODEL,
)
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.core.chad_voice import chad_voice_system
from home_builder_agent.core.claude_client import (
    haiku_cost,
    make_client,
    sonnet_cost,
)
from home_builder_agent.core.knowledge_base import load_comm_rules
from home_builder_agent.integrations import drive
from home_builder_agent.integrations import gmail as gmail_int


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


# ---------------------------------------------------------------------
# Per-thread reply draft (Sonnet, Chad voice — author mode)
# ---------------------------------------------------------------------

_REPLY_DRAFT_OUTPUT_CONTRACT = """

Output requirements:
- JSON only. No markdown fence, no preamble.
- Two top-level keys: "subject" (string) and "body_html" (string).
- subject: prefix with "Re: " unless the original subject already starts
  with "Re:" / "RE:" / "Fwd:". Keep it concise.
- body_html: complete HTML email body. NO <html>/<head>/<body> tags —
  just the inner content. Use <p>, <ul>, <li>, <strong>. Sign off with
  Chad's first name on a new line. No formal letterhead — the relationship
  is established. Inline styles only if you need them; mostly let Gmail's
  default rendering do the work.
- Voice: Chad's. Direct, calm, status-led. No "I hope this finds you
  well" pleasantries. No "Just wanted to circle back" filler. Lead with
  the answer or the ask.
"""


def generate_reply_draft(
    client,
    thread_summary: dict,
    classification: dict,
):
    """Draft a Chad-voice reply to one thread.

    Uses chad_voice_system("author") — Chad will send this.

    Returns (subject, html_body, usage). Raises on Sonnet error or
    JSON parse failure (caller catches per-thread).
    """
    system_prompt = chad_voice_system("author") + _REPLY_DRAFT_OUTPUT_CONTRACT

    user_prompt = f"""Draft a reply to the following thread on Chad's behalf.

ORIGINAL SUBJECT: {thread_summary.get('subject', '(no subject)')}
FROM:             {thread_summary.get('from_name', '')} <{thread_summary.get('from_email', '')}>
LAST ACTIVITY:    {thread_summary.get('days_old', '?')} days ago
MESSAGE COUNT:    {thread_summary.get('message_count', '?')}
CLASSIFIER FLAG:  urgency={classification.get('urgency', '?')}, reason={classification.get('reason', '')}

LATEST SNIPPET:
{thread_summary.get('snippet', '(no snippet)')}

Draft Chad's reply. JSON only per the output contract."""

    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=1500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)

    parsed = json.loads(raw)
    subject = (parsed.get("subject") or "").strip()
    body_html = (parsed.get("body_html") or "").strip()

    if not subject:
        # Fallback — prepend Re: to the original subject
        orig = thread_summary.get("subject", "(no subject)")
        subject = orig if orig.lower().startswith(("re:", "fwd:")) else f"Re: {orig}"
    if not body_html:
        raise ValueError("Sonnet response missing body_html")

    return subject, body_html, response.usage


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
        description="Generate a Chad-voice email follow-up checklist + "
                    "draft replies for HIGH-urgency threads."
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
    parser.add_argument("--no-drafts", action="store_true",
                        help="Skip the per-thread reply drafting step "
                             "(saves ~$0.01/high-urgency thread). The "
                             "consolidated checklist is still generated.")
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

    # ─── New: per-thread reply drafting for HIGH urgency ────────────────────
    # For each high-urgency thread where Chad wasn't the last sender,
    # draft a reply via Sonnet (chad_voice author mode), create the
    # Gmail draft, and log a draft_action row so it surfaces in the
    # morning view's judgment_queue.
    drafts_created = 0
    drafts_failed = 0
    drafts_writer_usd = 0.0
    if not args.no_drafts:
        high_urgency = [
            (s, c) for s, c in needs_followup
            if (c.get("urgency") or "").lower() == "high"
            and not s.get("last_from_me")
        ]
        if high_urgency:
            print(
                f"\nDrafting replies for {len(high_urgency)} HIGH-urgency thread"
                f"{'s' if len(high_urgency) != 1 else ''}..."
            )

            # Lazy-import the engine pieces so non-DB users (legacy
            # callers, smoke tests) don't have to spin up a connection
            # if no high-urgency threads exist.
            try:
                from home_builder_agent.scheduling.draft_actions import (
                    DraftKind,
                    make_draft_action,
                )
                from home_builder_agent.scheduling.store_postgres import (
                    insert_draft_action,
                    load_project_by_name,
                )
                project_row = load_project_by_name(FINANCE_PROJECT_NAME)
            except Exception as e:
                print(
                    f"  ⚠️  Postgres not available — drafts will be created in "
                    f"Gmail but not registered in the morning queue: "
                    f"{type(e).__name__}: {e}"
                )
                project_row = None

            for summary, classification in high_urgency:
                short_subject = (summary.get("subject") or "")[:60]
                try:
                    subject, body_html, draft_usage = generate_reply_draft(
                        client, summary, classification,
                    )
                    drafts_writer_usd += sonnet_cost(draft_usage)["total"]

                    # Create the Gmail draft so Chad can review it in
                    # Gmail Drafts AND in the morning queue.
                    draft = gmail_int.create_draft(
                        gmail_svc,
                        to=summary.get("from_email", ""),
                        subject=subject,
                        html_body=body_html,
                    )
                    gmail_draft_id = draft.get("id")

                    # Log to home_builder.draft_action for the
                    # morning view's judgment_queue. Best-effort —
                    # skip silently if Postgres isn't reachable or
                    # the project row is missing (matches the
                    # change_order / client_update adapter pattern).
                    if project_row is not None:
                        try:
                            agent_summary = (
                                f"{summary.get('from_name', '?')}: "
                                f"{classification.get('reason', '')[:80] or short_subject}"
                            )
                            da = make_draft_action(
                                project_id=project_row["id"],
                                kind=DraftKind.GMAIL_REPLY_DRAFT,
                                originating_agent="hb-inbox",
                                summary=agent_summary,
                                subject_line=subject,
                                body_payload={
                                    "thread_id": summary.get("thread_id"),
                                    "original_subject": summary.get("subject"),
                                    "original_from": summary.get("from_email"),
                                    "original_from_name": summary.get("from_name"),
                                    "draft_subject": subject,
                                    "draft_body_html": body_html,
                                    "recipient": summary.get("from_email"),
                                    "classification_urgency": classification.get("urgency"),
                                    "classification_reason": classification.get("reason"),
                                },
                                external_ref=gmail_draft_id,
                                from_or_to=(
                                    f"From: {summary.get('from_name', '')} "
                                    f"<{summary.get('from_email', '')}>"
                                ).strip(),
                            )
                            insert_draft_action(da)
                            print(
                                f"  ✅  drafted reply to {short_subject!r:<60} "
                                f"(gmail_draft_id={gmail_draft_id[:12]}…, "
                                f"draft_action={da.id[:8]}…)"
                            )
                        except Exception as e:
                            msg = str(e).lower()
                            if "does not exist" in msg and "draft_action" in msg:
                                print(
                                    f"  ✅  drafted reply to {short_subject!r:<60} "
                                    f"(gmail draft id={gmail_draft_id[:12]}…; "
                                    "morning queue pending migration 007)"
                                )
                            else:
                                print(
                                    f"  ⚠️  drafted reply to {short_subject!r}, "
                                    f"but draft_action insert failed: "
                                    f"{type(e).__name__}: {e}"
                                )
                    else:
                        print(
                            f"  ✅  drafted reply to {short_subject!r:<60} "
                            f"(gmail draft id={gmail_draft_id[:12]}…; "
                            "morning queue skipped — no project row)"
                        )

                    drafts_created += 1
                except Exception as e:
                    drafts_failed += 1
                    print(
                        f"  ⚠️  draft for {short_subject!r} failed: "
                        f"{type(e).__name__}: {e}"
                    )

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
    print(f"       checklist writer=${writer_usd:.4f}")
    if drafts_created > 0 or drafts_failed > 0:
        print(
            f"       reply drafts=${drafts_writer_usd:.4f} "
            f"({drafts_created} drafted, {drafts_failed} failed)"
        )
    total_usd = classifier_usd + writer_usd + drafts_writer_usd
    print(f"       TOTAL=${total_usd:.4f}")
    print()


if __name__ == "__main__":
    main()
