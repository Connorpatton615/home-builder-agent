"""gmail.py — Gmail API helpers.

Gmail integration is read-only — Phase 1 only LISTS threads and reads metadata
to classify them. No sending, no labels, no archiving from this code. The
Gmail follow-up agent generates a checklist Doc that Chad clicks through
manually.

Phase 2 may add the supplier-email watcher, which will also be read-only but
will scan thread bodies for lead-time changes. Those helpers will land here
when that lands.
"""

from datetime import date, timedelta, datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

from googleapiclient.discovery import build


def gmail_service(creds):
    """Build a Gmail v1 service."""
    return build("gmail", "v1", credentials=creds)


def get_my_email(svc):
    """Return the authenticated user's email address ('me' resolved)."""
    profile = svc.users().getProfile(userId="me").execute()
    return profile.get("emailAddress", "")


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
