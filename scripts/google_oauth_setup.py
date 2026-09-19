"""One-time Google sign-in so the bot can book on YOUR calendar with Meet links and invites.

A service account can read a personal Gmail calendar, but Google refuses it both Meet conferences
("Invalid conference type value") and attendee invites ("forbiddenForServiceAccounts"). FR-5.6 and
FR-5.7 therefore need the calendar owner's own OAuth credentials.

1. Google Cloud console -> APIs & Services -> Credentials -> Create credentials -> OAuth client ID
   -> Application type "Desktop app". (First time: configure the consent screen, add yourself as a test
   user, and later click "Publish app" so the token does not expire after 7 days.)
2. Put the client id/secret in .env as GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET.
3. Run:  python scripts/google_oauth_setup.py
   A browser opens; sign in as the calendar owner. The refresh token is written straight into .env -
   it is never printed, so it cannot end up in a terminal scrollback or a pasted log.
4. Restart the calendar MCP server, then run: python scripts/verify_calendar.py
"""
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import settings  # noqa: E402

ENV_FILE = ROOT / ".env"
KEY = "GOOGLE_OAUTH_REFRESH_TOKEN"


def upsert_env(key: str, value: str) -> str:
    """Replace `key` in .env in place, or append it under the Google block. Returns what happened."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{key}=") or line.startswith(f"#{key}="):
            lines[i] = f"{key}={value}"
            ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return "replaced existing"
    # append after the last GOOGLE_* line so it lands in the Google block, not at the bottom
    last = max((i for i, l in enumerate(lines) if l.startswith("GOOGLE_")), default=len(lines) - 1)
    lines.insert(last + 1, f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "added"


if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
    raise SystemExit(
        "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are empty in .env.\n"
        "Create a Desktop-app OAuth client in the Google Cloud console first (see this file's docstring)."
    )

flow = InstalledAppFlow.from_client_config(
    {"installed": {"client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                   "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token",
                   "redirect_uris": ["http://localhost"]}},
    scopes=["https://www.googleapis.com/auth/calendar"])

print(f"Opening a browser. Sign in as {settings.GOOGLE_CALENDAR_ID} - the calendar the bot books on.")
print("If the browser does not open, copy the URL printed below into it manually.\n")
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                              open_browser=True,
                              authorization_prompt_message="Open this URL to authorise:\n{url}\n")

if not creds.refresh_token:
    raise SystemExit(
        "Google returned no refresh token. Revoke the app's access at "
        "https://myaccount.google.com/permissions and run this again - Google only issues a refresh "
        "token on the first consent for a client."
    )

what = upsert_env(KEY, creds.refresh_token)
print(f"\nOK - {KEY} {what} in {ENV_FILE} (value not shown).")
print("Next: restart the calendar MCP server, then run  python scripts/verify_calendar.py")
