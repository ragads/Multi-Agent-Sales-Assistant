# Note: why Scheduler / Google Calendar are marked Partial

**Status date:** 2026-09-20

Two components in the architecture are not fully verified. This note explains exactly what
works, what doesn't, why, and how to clear it.

---

## What works today

| Capability | FR | State |
|---|---|---|
| Read the calendar's free/busy blocks | FR-5.2 (read half) | Working |
| Propose 2–3 real open slots, in the visitor's timezone | FR-5.3, FR-5.4 | Working |
| Fall back honestly when booking fails | FR-8.1, FR-8.4 | Working |

## What doesn't

| Capability | FR | State |
|---|---|---|
| Create the event on the calendar | FR-5.2 (write half) | Blocked |
| Attach a Google Meet link | FR-5.6 | Blocked |
| Send the visitor a calendar invite | FR-5.7 | Blocked |
| Re-check / no double-booking under a race | FR-5.5 | Untested — needs a real booking first |
| Reschedule / cancel an existing booking | FR-5.8 | Untested — needs a real booking first |

---

## The cause

7 of 7 `create_event` attempts returned the same error from Google:

```
HTTP 403 forbiddenForServiceAccounts
Service accounts cannot invite attendees without Domain-Wide Delegation of Authority.
```

The app currently authenticates with a **service account** — a robot identity
(`GOOGLE_SERVICE_ACCOUNT_JSON` in `.env`). Google permits a service account to read a shared
calendar and to create events on it, but **refuses to let it add attendees**. Adding an attendee
is what sends the invite, so the whole `create_event` call is rejected before anything is written.

The exemption that would allow it, *Domain-Wide Delegation*, only exists for **Google Workspace**
accounts. The target calendar (`sudharaga327@gmail.com`) is a personal Gmail account, so that
route is unavailable.

**This is not a defect in the application.** The call is well-formed, the retry logic correctly
classified the 403 as non-retryable and stopped after one attempt instead of wasting three, and
the Scheduler fell back to collecting details for manual follow-up — which is precisely the
FR-8.1 / FR-8.4 behaviour the spec asks for. The blocker is an account-permission boundary, not
application logic.

---

## The fix — sign in as yourself (OAuth)

Rather than acting as a robot, the app signs in **as the calendar owner**, once. Google then
treats every booking as that person inviting someone, which is always permitted.

`GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` are already set in `.env`. Only the
refresh token is missing.

1. **Run the setup script** from the project root:

   ```
   .venv\Scripts\python scripts\google_oauth_setup.py
   ```

   It starts a local listener and opens your browser.

2. **Sign in** with the account that owns the calendar.

3. **Get past the unverified-app warning.** Because the OAuth client is in Testing mode, Google
   shows *"Google hasn't verified this app"*. Click **Advanced → Go to [app name] (unsafe)**.
   This is expected: you own both the app and the account.

4. **Allow** the requested `calendar` scope.

5. **Copy the printed line** into `.env`:

   ```
   GOOGLE_OAUTH_REFRESH_TOKEN=1//0g...
   ```

6. **Restart the calendar MCP server.** It reads the credential once at startup
   (`app/mcp_servers/calendar_server.py`), so the token is not picked up until it restarts.

   ```
   .venv\Scripts\python -m app.mcp_servers.calendar_server
   ```

7. **Publish the consent screen** in the Google Cloud console (OAuth consent screen → *Publish
   app*). While the client stays in Testing mode the refresh token expires after 7 days.

Once the token is in place, `service()` selects the OAuth credential instead of the service
account automatically — no code change is required.

---

## What to verify afterwards

The five rows above should all be re-tested, in this order:

1. **Book a call end to end** through the widget → expect a real event, a `hangoutLink`, and an
   invite in the visitor's inbox (FR-5.2, FR-5.6, FR-5.7).
2. **Race condition** → `PYTHONPATH=. .venv\Scripts\python scripts\demo_race_condition.py`.
   Expect exactly one `BOOKED` and one `SLOT_TAKEN` (FR-5.5).
3. **Reschedule and cancel** → as a returning visitor, ask to move the booked call, then to
   cancel it. Expect the same event modified, never a duplicate (FR-5.8).

These three have never executed against a real booking. Treat them as unknown rather than
working — six defects were found in the parts of the system that *could* be exercised, so the
untested paths deserve the same scrutiny.

---

## If the OAuth route stays unavailable

- **Use a different Google account** you can sign into. The calendar does not have to be the
  original one — sign in with the other account and point `GOOGLE_CALENDAR_ID` at it.
- **Google Workspace** — if a Workspace domain is available, enable domain-wide delegation for
  the service account, authorise its client ID for the `calendar` scope in the admin console, and
  set `GOOGLE_IMPERSONATE_USER` in `.env`. The code picks that variable up automatically.
