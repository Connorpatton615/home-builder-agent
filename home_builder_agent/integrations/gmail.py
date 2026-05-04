"""gmail.py — Gmail API helpers.

Phase 1: read-only — list threads, read metadata, classify, extract bodies.
Phase 2: adds send capability for the morning brief agent.
"""

import base64
import re
from datetime import date, timedelta, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr, parsedate_to_datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


def gmail_service(creds):
    """Build a Gmail v1 service."""
    return build("gmail", "v1", credentials=creds)


def get_my_email(svc):
    """Return the authenticated user's email address ('me' resolved)."""
    profile = svc.users().getProfile(userId="me").execute()
    return profile.get("emailAddress", "")


def get_current_history_id(svc):
    """Return the user's current Gmail historyId as a string.

    Used by the inbox watcher to (re-)baseline its cursor when starting
    fresh or after the previous historyId has aged out (>~7 days).
    """
    profile = svc.users().getProfile(userId="me").execute()
    return str(profile["historyId"])


def list_inbox_message_added_since(svc, start_history_id):
    """Fetch INBOX messageAdded events since `start_history_id`.

    Returns (thread_ids, latest_history_id, baseline_expired):
      - thread_ids: set of thread IDs that received a new INBOX message
      - latest_history_id: highest historyId observed (next cursor value)
      - baseline_expired: True if Gmail no longer has `start_history_id`
        (it was older than ~7 days). Caller must re-baseline.

    Pagination is handled internally so no events are dropped on a busy
    inbox.
    """
    thread_ids = set()
    latest_history_id = start_history_id
    page_token = None

    while True:
        try:
            resp = svc.users().history().list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                pageToken=page_token,
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                return set(), None, True
            raise

        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added.get("message", {})
                if "INBOX" in msg.get("labelIds", []):
                    thread_ids.add(msg["threadId"])

        if "historyId" in resp:
            latest_history_id = str(resp["historyId"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return thread_ids, latest_history_id, False


def list_recent_threads(svc, days=7, max_results=100):
    """List inbox threads modified in the last N days. Returns list of {id, snippet, ...}."""
    after_date = (date.today() - timedelta(days=days)).isoformat()
    query = f"in:inbox after:{after_date}"

    threads = []
    page_token = None
    while len(threads) < max_results:
        result = svc.users().threads().list(
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


def get_thread_summary(svc, thread_id, my_email):
    """Fetch a thread's metadata + snippet for classification.

    Returns a dict with: thread_id, subject, from_name, from_email,
    last_from_me, snippet, days_old, message_count. Returns None if the
    thread has no messages (shouldn't happen with normal use).
    """
    thread = svc.users().threads().get(
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

    last_from_email = parseaddr(headers.get("from", ""))[1].lower()
    last_from_me = my_email.lower() in last_from_email

    # Parse the date — emails sometimes have weird formats; treat parse
    # failure as 'unknown age' (-1) rather than crashing.
    date_str = headers.get("date", "")
    try:
        last_date = parsedate_to_datetime(date_str)
        if last_date.tzinfo is None:
            last_date = last_date.replace(tzinfo=timezone.utc)
        days_old = (datetime.now(timezone.utc) - last_date).days
    except Exception:
        days_old = -1

    snippet = thread.get("snippet", "") or last_message.get("snippet", "")

    return {
        "thread_id": thread_id,
        "subject": headers.get("subject", "(no subject)"),
        "from_name": (parseaddr(headers.get("from", ""))[0]
                      or parseaddr(headers.get("from", ""))[1]),
        "from_email": last_from_email,
        "last_from_me": last_from_me,
        "snippet": snippet[:400],  # cap for token efficiency
        "days_old": days_old,
        "message_count": len(messages),
    }


# ---------------------------------------------------------------------------
# Message body / attachment helpers (Phase 2 — supplier-email watcher, etc.)
# ---------------------------------------------------------------------------

def _decode_payload(payload):
    """Recursively walk a Gmail message payload and return (plain_text, html_text).

    Prefers text/plain; also collects text/html as a fallback. Returns the
    first non-empty value found in depth-first order.
    """
    mime_type = payload.get("mimeType", "")
    parts = payload.get("parts", [])

    if parts:
        # Multipart — recurse into each child part
        plain_text = ""
        html_text = ""
        for part in parts:
            pt, ht = _decode_payload(part)
            if pt and not plain_text:
                plain_text = pt
            if ht and not html_text:
                html_text = ht
        return plain_text, html_text

    # Leaf part — try to decode body data
    body_data = payload.get("body", {}).get("data", "")
    if not body_data:
        return "", ""

    try:
        decoded = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
    except Exception:
        return "", ""

    if mime_type == "text/plain":
        return decoded, ""
    if mime_type == "text/html":
        return "", decoded
    return "", ""


def get_message_body(svc, message_id):
    """Fetch the decoded plain-text body of a Gmail message.

    Handles multipart messages by preferring text/plain parts.
    Falls back to text/html (stripped of tags) if no plain-text part.
    Returns empty string if body can't be decoded.
    """
    try:
        msg = svc.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        payload = msg.get("payload", {})
        plain_text, html_text = _decode_payload(payload)

        if plain_text:
            return plain_text[:4000]

        if html_text:
            # Strip HTML tags to get readable text
            stripped = re.sub(r"<[^>]+>", " ", html_text)
            # Collapse runs of whitespace
            stripped = re.sub(r"\s+", " ", stripped).strip()
            return stripped[:4000]

        return ""
    except Exception:
        return ""


def get_message_attachment_list(svc, message_id):
    """Return list of attachment metadata dicts for a message.

    Each dict has: {attachment_id, filename, mime_type, size_bytes}
    Returns empty list if no attachments or on error.
    """
    def _collect_attachments(payload):
        attachments = []
        filename = payload.get("filename", "")
        body = payload.get("body", {})
        attachment_id = body.get("attachmentId", "")
        if filename and attachment_id:
            attachments.append({
                "attachment_id": attachment_id,
                "filename": filename,
                "mime_type": payload.get("mimeType", ""),
                "size_bytes": body.get("size", 0),
            })
        for part in payload.get("parts", []):
            attachments.extend(_collect_attachments(part))
        return attachments

    try:
        msg = svc.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        return _collect_attachments(msg.get("payload", {}))
    except Exception:
        return []


def send_email(svc, to: str, subject: str, html_body: str,
               text_body: str = "", sender_name: str = "") -> dict:
    """Send an email via Gmail API.

    Args:
        svc:         Gmail v1 service (must have gmail.send scope)
        to:          Recipient address
        subject:     Email subject line
        html_body:   HTML version of the email
        text_body:   Plain-text fallback (optional; generated from html if blank)
        sender_name: Display name for the From header (optional)

    Returns:
        Gmail message resource dict (id, threadId, labelIds)
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"] = to

    if sender_name:
        me = svc.users().getProfile(userId="me").execute()
        msg["From"] = f"{sender_name} <{me['emailAddress']}>"

    if not text_body:
        # Strip HTML tags for the plain-text fallback
        text_body = re.sub(r"<[^>]+>", "", html_body)
        text_body = re.sub(r"\n{3,}", "\n\n", text_body).strip()

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html",  "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return svc.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()


def create_draft(svc, to: str, subject: str, html_body: str,
                 text_body: str = "", sender_name: str = "") -> dict:
    """Create a Gmail draft (does not send).

    Same signature as send_email — swap the two for send vs. review workflow.

    Returns:
        Gmail draft resource dict (id, message.id, message.threadId)
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"] = to

    if sender_name:
        me = svc.users().getProfile(userId="me").execute()
        msg["From"] = f"{sender_name} <{me['emailAddress']}>"

    if not text_body:
        text_body = re.sub(r"<[^>]+>", "", html_body)
        text_body = re.sub(r"\n{3,}", "\n\n", text_body).strip()

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html",  "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return svc.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()


def get_thread_message_ids(svc, thread_id):
    """Return list of message IDs in a thread, oldest first."""
    try:
        thread = svc.users().threads().get(
            userId="me", id=thread_id, format="minimal"
        ).execute()
        return [m["id"] for m in thread.get("messages", [])]
    except Exception:
        return []
