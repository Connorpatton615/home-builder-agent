"""auth.py — shared Google OAuth flow.

Every agent used to define its own get_credentials(); now they all call into
this one. Same token.json file is reused, same browser-consent flow on first
run with new scopes.

Why a single function and not per-agent: the OAuth scope drift was a real bug
source. Agent 1 had drive+docs+sheets+gmail; Agents 2/2.5 had drive+docs+sheets.
Whichever agent ran first wrote token.json with ITS scope set, which then
broke the others. Centralizing on the union of all scopes eliminates that.
"""

import os

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


def get_credentials():
    """Authenticate with Google. Returns Credentials usable for any Google API.

    Resolution order:
      1. If token.json exists and is valid → reuse it (no browser).
      2. If token.json is expired but has a refresh token → refresh silently.
      3. Otherwise → open browser for consent screen.

    First run with new scopes (e.g. adding gmail.readonly to a token that only
    had drive scopes) will fall through to step 3 because Google does NOT
    silently expand scope.
    """
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # Refresh can fail if the token was revoked or scopes changed;
                # fall through to fresh consent flow rather than crashing.
                creds = None

        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, GOOGLE_SCOPES
            )
            # prompt='consent' forces explicit grant of every scope each time
            # rather than silently reusing a prior partial authorization.
            creds = flow.run_local_server(port=0, prompt="consent")

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds
