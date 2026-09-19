"""One-time Google sign-in so the bot can book on YOUR calendar with Meet links and invites.

1. Google Cloud console -> APIs & Services -> Credentials -> Create credentials -> OAuth client ID
   -> Application type "Desktop app". (First time: configure the consent screen, add yourself as a test
   user, and later click "Publish app" so the token does not expire after 7 days.)
2. Put the client id/secret in .env as GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET.
3. Run:  python scripts/google_oauth_setup.py   -> sign in in the browser -> paste the printed
   GOOGLE_OAUTH_REFRESH_TOKEN line into .env, then restart the calendar MCP server.
"""
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402

flow = InstalledAppFlow.from_client_config(
    {"installed": {"client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                   "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token",
                   "redirect_uris": ["http://localhost"]}},
    scopes=["https://www.googleapis.com/auth/calendar"])
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
print("Add this line to .env:")
print("GOOGLE_OAUTH_REFRESH_TOKEN=" + creds.refresh_token)
