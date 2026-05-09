"""auth.py — shared Google OAuth flow.

Every agent used to define its own get_credentials(); now they all call into
this one. Same token.json file is reused, same browser-consent flow on first
run with new scopes.

Why a single function and not per-agent: the OAuth scope drift was a real bug
source. Agent 1 had drive+docs+sheets+gmail; Agents 2/2.5 had drive+docs+sheets.
Whichever agent ran first wrote token.json with ITS scope set, which then
broke the others. Centralizing on the union of all scopes eliminates that.

Production-hardening note (2026-05-09):
  Refresh-failure used to fall through to flow.run_local_server() which
  blocks on browser consent. In a launchd context that's a silent hang —
  the job stays "running" forever, no heartbeat updates, no output, no
  Chad-visible error. Now we TTY-guard: if refresh fails AND we're not
  attached to a terminal, raise OAuthRefreshFailedError loudly so the
  watchdog catches the staleness within 25h instead of the job hanging
  for days.
"""

import os
import sys

# CRITICAL: must set BEFORE google_auth_oauthlib imports — Google may return
# slightly different scope set than requested (e.g. when user has previously
# authorized the app), and without this env var that becomes a fatal warning.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from home_builder_agent.config import (
    CREDENTIALS_FILE,
    GOOGLE_SCOPES,
    TOKEN_FILE,
)


class OAuthRefreshFailedError(RuntimeError):
    """Raised by get_credentials() when token refresh fails AND we cannot
    interactively re-prompt for consent (no TTY attached). Caller should
    let this propagate so the launchd job exits with error and the
    watchdog catches the heartbeat staleness rather than the agent
    hanging on flow.run_local_server() waiting for a browser that will
    never come.
    """


def _is_interactive() -> bool:
    """True iff stdin is attached to a TTY. False inside launchd jobs,
    Modal sandboxes, Railway workers, and any other daemon context.
    """
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def get_credentials():
    """Authenticate with Google. Returns Credentials usable for any Google API.

    Resolution order:
      1. If token.json exists and is valid → reuse it (no browser).
      2. If token.json is expired but has a refresh token → refresh silently.
      3. Interactive context only: open browser for consent screen.
      4. Daemon context (launchd, etc.): raise OAuthRefreshFailedError so
         the job exits loudly + watchdog catches the staleness.

    First run with new scopes (e.g. adding gmail.readonly to a token that only
    had drive scopes) will fall through to step 3 because Google does NOT
    silently expand scope. In a daemon context that re-consent requirement
    becomes step 4 — the job dies, Connor sees the alert, and runs the agent
    interactively once to refresh consent.
    """
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)

    refresh_error: Exception | None = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                # Refresh can fail if the token was revoked or scopes changed;
                # capture the error and fall through to interactive consent
                # if available, or raise loudly if we're in a daemon.
                refresh_error = e
                creds = None

        if not creds or not creds.valid:
            if not _is_interactive():
                # Daemon context — re-prompting on a browser will hang
                # forever. Raise loudly so launchd records exit code 1,
                # the watchdog notices the heartbeat goes stale, and
                # Connor gets paged within 25h instead of "huh, why
                # haven't I heard from the agent in 4 days?"
                cause = f": {type(refresh_error).__name__}: {refresh_error}" if refresh_error else ""
                raise OAuthRefreshFailedError(
                    f"Google OAuth credentials need refresh{cause}. "
                    f"This is a daemon context (no TTY) so the consent "
                    f"flow cannot run automatically. Recovery: run any "
                    f"hb-* agent interactively from Terminal once to "
                    f"trigger the browser consent flow, then the launchd "
                    f"jobs will pick up the refreshed token.json."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, GOOGLE_SCOPES
            )
            # prompt='consent' forces explicit grant of every scope each time
            # rather than silently reusing a prior partial authorization.
            creds = flow.run_local_server(port=0, prompt="consent")

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds
