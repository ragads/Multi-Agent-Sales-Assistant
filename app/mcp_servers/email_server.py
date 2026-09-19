"""Email MCP server backed by Resend (FR-6.2). Run: python -m app.mcp_servers.email_server"""
from __future__ import annotations
import os

import httpx
from fastmcp import FastMCP

from app.config import settings

mcp = FastMCP("closefuture-email")
RESEND_URL = "https://api.resend.com/emails"


def _render(summary: dict) -> str:
    v = summary.get("visitor", {})
    m = summary.get("meeting", {})
    q = summary.get("qualification", {})
    questions = "".join(f"<li>{x}</li>" for x in summary.get("key_questions", [])) or "<li>None recorded</li>"
    tier = summary.get("tier", "cold")
    colour = {"hot": "#dc2626", "warm": "#d97706", "cold": "#64748b"}.get(tier, "#64748b")
    booked = (
        f"<b>Booked:</b> {m.get('start_local', '-')}<br><b>Meet:</b> "
        f"<a href='{m.get('meet_link', '#')}'>{m.get('meet_link', '-')}</a>"
        if m.get("booked") else
        ("<b>No meeting booked.</b> Manual follow-up requested."
         if m.get("manual_followup") else "<b>No meeting booked.</b>")
    )
    flag = "" if summary.get("complete") else (
        "<p style='background:#fef3c7;padding:8px;border-radius:6px'>"
        "<b>Incomplete lead</b> - the visitor left mid-conversation. Details are partial.</p>")
    return f"""
<div style="font-family:system-ui,Arial,sans-serif;max-width:640px">
  <h2 style="margin-bottom:4px">New lead from the website assistant</h2>
  <p style="color:{colour};font-size:18px;margin-top:0">
     <b>{tier.upper()}</b> &middot; score {summary.get('lead_score', 0)}/100</p>
  {flag}
  <h3>Visitor</h3>
  <p>{v.get('name') or 'Unknown'}<br>{v.get('email') or 'no email captured'}<br>
     {v.get('company') or 'no company given'}<br>{v.get('timezone') or ''}</p>
  <h3>Qualification</h3>
  <p>Project: {q.get('project_type') or '-'}<br>Budget: {q.get('budget_hint') or '-'}<br>
     Timeline: {q.get('timeline') or '-'}<br>Role: {q.get('decision_role') or '-'}</p>
  <h3>Questions they asked</h3><ul>{questions}</ul>
  <h3>Meeting</h3><p>{booked}</p>
  <h3>Next step</h3><p>{summary.get('next_step', '-')}</p>
  <p><a href="{summary.get('conversation_url', '#')}">Open the full conversation log</a></p>
</div>"""


def _plain(summary: dict) -> str:
    v = summary.get("visitor", {})
    return (f"Lead: {v.get('name') or 'Unknown'} <{v.get('email') or 'no email'}>\n"
            f"Score {summary.get('lead_score', 0)}/100 ({summary.get('tier')})\n"
            f"Complete: {summary.get('complete')}\n"
            f"Meeting booked: {summary.get('meeting', {}).get('booked')}\n"
            f"Log: {summary.get('conversation_url')}\n")


def _send(subject: str, summary: dict) -> dict:
    if os.getenv("SIMULATE_EMAIL_OUTAGE") == "1":
        return {"status": "error", "error_code": "UPSTREAM_503", "retryable": True,
                "message": "email provider unavailable (simulated)", "agent": "email_mcp"}
    resp = httpx.post(
        RESEND_URL, timeout=20,
        headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
        json={"from": settings.EMAIL_FROM, "to": [settings.SALES_INBOX],
              "subject": subject, "html": _render(summary), "text": _plain(summary)},
    )
    if resp.status_code >= 400:
        return {"status": "error", "error_code": f"EMAIL_{resp.status_code}",
                "retryable": resp.status_code in (408, 429, 500, 502, 503, 504),
                "message": resp.text[:300], "agent": "email_mcp"}
    return {"status": "ok", "provider_id": resp.json().get("id")}


@mcp.tool()
def send_lead_summary(summary: dict) -> dict:
    """Email the structured lead summary to the sales inbox. `summary` is the Lead-Summary object."""
    name = summary.get("visitor", {}).get("name") or "Website visitor"
    prefix = "" if summary.get("complete") else "[PARTIAL] "
    subject = f"{prefix}Lead {summary.get('tier', '').upper()} {summary.get('lead_score', 0)}/100 - {name}"
    return _send(subject, summary)


@mcp.tool()
def update_lead_summary(summary: dict, previous_message_id: str = "") -> dict:
    """Send a corrected/updated summary for a session that was already reported (FR-6.6).

    Never a conflicting duplicate: the subject marks it as an update to the earlier mail.
    """
    name = summary.get("visitor", {}).get("name") or "Website visitor"
    subject = f"[UPDATED] Lead {summary.get('tier', '').upper()} {summary.get('lead_score', 0)}/100 - {name}"
    summary = dict(summary)
    summary["next_step"] = (f"Updates the earlier summary ({previous_message_id or 'previous email'}). "
                            + summary.get("next_step", ""))
    return _send(subject, summary)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8932, path="/mcp")
