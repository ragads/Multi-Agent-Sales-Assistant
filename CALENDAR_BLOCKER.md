# Note: the Scheduler / Google Calendar blocker, and how it was cleared

**Raised:** 2026-09-20 · **Resolved:** 2026-09-20

This note previously explained why Scheduler and Google Calendar were marked *Partial*. The
blocker is now cleared and every FR-5.x requirement has been executed against the real Google
Calendar API. The history is kept below because the root cause is a configuration boundary that
is easy to fall back into.

---

## Current state — all verified

| Capability | FR | State |
|---|---|---|
| Read the calendar's free/busy blocks | FR-5.2 (read) | Verified |
| Propose 2–3 real open slots in the visitor's timezone | FR-5.3, FR-5.4 | Verified |
| Create the event on the calendar | FR-5.2 (write) | Verified |
| Re-check immediately before insert; no double-booking under a race | FR-5.5 | Verified |
| Attach a Google Meet link | FR-5.6 | Verified |
| Send the visitor a calendar invite | FR-5.7 | Verified |
| Reschedule in place / cancel | FR-5.8 | Verified |
| Fall back honestly when booking fails | FR-8.1, FR-8.4 | Verified |

Reproduce with `python scripts/verify_calendar.py`, which drives the same MCP tool functions the
Scheduler agent calls:

```
0. Live MCP server on http://localhost:8931/mcp
  [PASS] running server uses the same credentials as .env - server=oauth_owner
3. Booking a real event (FR-5.5, FR-5.6, FR-5.7)
  [PASS] Google Meet link attached (FR-5.6) - https://meet.google.com/xxx-xxxx-xxx
  [PASS] visitor invited (FR-5.7) - visitor@example.com
4. Re-check blocks a double booking (FR-5.5)
  [PASS] second booking for the same slot refused - SLOT_TAKEN
5. Rescheduling it in place (FR-5.8)
  [PASS] same event id - no duplicate created
ALL CHECKS PASSED
```

The double-booking race is separately demonstrated by `python scripts/demo_race_condition.py`,
which fires two concurrent bookings at one slot and yields exactly one `BOOKED` and one
`SLOT_TAKEN`. Note that it leaves a real event on the calendar; delete it afterwards.

---

## What the blocker was

Every `create_event` attempt returned the same error from Google:

```
HTTP 403 forbiddenForServiceAccounts
Service accounts cannot invite attendees without Domain-Wide Delegation of Authority.
```

The app was authenticating with a **service account** — a robot identity. Google permits a service
account to read a shared calendar and create bare events on it, but refuses to let it add
attendees or attach a Meet conference. Adding an attendee is what sends the invite, so the whole
call was rejected before anything was written. Confirmed by isolating the three operations:

| Operation | Service account |
|---|---|
| Create a plain event | works |
| Attach a Meet conference | `400 Invalid conference type value` |
| Invite an attendee | `403 forbiddenForServiceAccounts` |

The exemption that would allow it, *Domain-Wide Delegation*, exists only for Google Workspace
accounts. The target calendar is a personal Gmail account, so that route was unavailable.

This was never a defect in the application. The call was well-formed, the retry logic correctly
classified the 403 as non-retryable, and the Scheduler fell back to collecting details for manual
follow-up — the FR-8.1 / FR-8.4 behaviour the spec asks for. It was an account-permission
boundary.

## The fix

The app now signs in **as the calendar owner** via OAuth, so Google treats every booking as that
person inviting someone, which is always permitted. `service()` selects the OAuth credential
automatically whenever `GOOGLE_OAUTH_REFRESH_TOKEN` is set — no code change was needed to switch.

`scripts/google_oauth_setup.py` performs the one-time sign-in and writes the refresh token
directly into `.env` (it is never printed, so it cannot end up in a terminal scrollback). Publish
the consent screen in the Google Cloud console afterwards, or the token expires after 7 days.

---

## Three defects found while clearing it

1. **A stale server silently serves old credentials.** The MCP server caches its Google client at
   first use, and prints its startup banner *before* binding the port. A second copy therefore logs
   `auth mode: oauth_owner` and only then dies with `[Errno 10048] only one usage of each socket
   address`, leaving the original process still serving. A restart can appear to succeed while
   changing nothing. Fixed by adding a `calendar_health` MCP tool that reports the credentials the
   *running* server holds; `verify_calendar.py` step 0 compares it against `.env` and fails loudly
   on a mismatch.

2. **Simulated auth failures were retried.** `_fail_if_simulating()` raised a bare `RuntimeError`,
   from which `classify()` cannot read a status code, so it fell through to `retryable=True` and
   retried three times — the opposite of FR-8.2. The production path was always correct (real
   401/403s hit `NON_RETRYABLE_STATUS`), but the simulation is exactly what the acceptance criteria
   ask to be demonstrated. Now returns structured errors like every other tool path:
   `SIMULATE_CALENDAR_AUTH_FAILURE=1` yields 1 attempt and `failed_gave_up`.

3. **Rate-limit 403s were treated as permanent.** Google signals throttling with `403
   rateLimitExceeded`, not `429`. All four error handlers classified any 403 as non-retryable, so a
   burst of bookings would drop to manual follow-up when a retry a second later would have
   succeeded. This surfaced as a genuine intermittent failure during verification. Classification is
   now centralised in `_http_err()`, which treats the four documented transient 403 reasons as
   retryable while keeping `forbiddenForServiceAccounts` permanent.

---

## If the OAuth route is ever lost

- **Use a different Google account** you can sign into. The calendar need not be the original one —
  sign in with the other account and point `GOOGLE_CALENDAR_ID` at it.
- **Google Workspace** — enable domain-wide delegation for the service account, authorise its
  client ID for the `calendar` scope in the admin console, and set `GOOGLE_IMPERSONATE_USER` in
  `.env`. The code picks that variable up automatically.

In either fallback, re-run `scripts/verify_calendar.py`; step 0 will confirm the running server is
actually using the credentials you think it is.
