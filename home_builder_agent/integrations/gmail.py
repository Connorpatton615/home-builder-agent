"""gmail.py — Gmail API helpers."""

from datetime import date, timedelta, datetime, timezone
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
