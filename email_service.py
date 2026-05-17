"""
email_service.py — Outbound email notifications via SMTP

Reads config from secrets.json (takes priority) or environment variables.
Uses Python stdlib only — no external packages required.

Required secrets.json fields:
  {
    "smtp_host":  "mail.jbrasfield.com",   // your SMTP server hostname
    "smtp_port":  587,                      // 587 = STARTTLS, 465 = SSL
    "smtp_user":  "noreply@jbrasfield.com", // SMTP login username
    "smtp_pass":  "your-smtp-password",    // SMTP login password
    "smtp_from":  "noreply@jbrasfield.com" // From address (optional, defaults to smtp_user)
  }

The recipient address and display name are hardcoded here since they
don't change — update NOTIFY_TO / FROM_NAME below if needed.
"""

import json
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

log = logging.getLogger(__name__)

_SECRETS_FILE = Path(__file__).parent / "secrets.json"

# ── Static config ─────────────────────────────────────────────────────────────

NOTIFY_TO  = "trading@jbrasfield.com"   # always send suggestions here
NOTIFY_BCC = "jon@jbrasfield.com"       # blind copy for delivery confirmation
FROM_NAME  = "Brasfield Momentum"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _secrets() -> dict:
    try:
        if _SECRETS_FILE.exists():
            return json.loads(_SECRETS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _cfg(key: str, default: str = "") -> str:
    import os
    return _secrets().get(key) or os.getenv(key.upper(), default)


def _is_configured() -> bool:
    return bool(_cfg("smtp_host") and _cfg("smtp_user") and _cfg("smtp_pass"))


# ── Public ────────────────────────────────────────────────────────────────────

def _send(msg, recipients, host, port, user, password, from_addr, label: str):
    """Internal helper — sends a pre-built MIME message."""
    try:
        if port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=10) as server:
                server.login(user, password)
                server.sendmail(from_addr, recipients, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=10) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(user, password)
                server.sendmail(from_addr, recipients, msg.as_string())
        log.info("[EMAIL] %s sent to %s", label, NOTIFY_TO)
    except Exception as e:
        log.warning("[EMAIL] Failed to send %s: %s", label, e)


def send_login_email(username: str, success: bool, ip: str = "", ua: str = "", location: str = ""):
    """
    Send a login-attempt notification email.
    Fires for both successful and failed logins.
    Silently skips if SMTP is not configured.
    Runs synchronously — call from a background thread.
    """
    if not _is_configured():
        return

    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S ET")

    host      = _cfg("smtp_host")
    port      = int(_cfg("smtp_port", "587"))
    user      = _cfg("smtp_user")
    password  = _cfg("smtp_pass")
    from_addr = _cfg("smtp_from") or user

    status_word  = "SUCCESS" if success else "FAILED"
    status_emoji = "✅" if success else "❌"
    status_color = "#22c55e" if success else "#ef4444"
    subject      = f"{status_emoji} Login {status_word} — {username}"

    # ── Plain text ──────────────────────────────────────────────
    plain = (
        f"Login attempt on Brasfield Momentum\n\n"
        f"Result:   {status_word}\n"
        f"Username: {username}\n"
        f"Time:     {now}\n"
        f"IP:       {ip or '—'}\n"
    )
    if location:
        plain += f"Location: {location}\n"
    if ua:
        plain += f"Browser:  {ua}\n"

    # ── HTML ────────────────────────────────────────────────────
    loc_row = f"<tr><td style='color:#888;padding:3px 8px 3px 0'>Location</td><td><strong>{location}</strong></td></tr>" if location else ""
    ua_row  = f"<tr><td style='color:#888;padding:3px 8px 3px 0'>Browser</td><td style='font-size:11px;color:#aaa'>{ua}</td></tr>" if ua else ""

    html = f"""<!doctype html>
<html>
<body style="font-family:Arial,sans-serif;background:#0f172a;padding:24px;margin:0">
  <div style="max-width:480px;margin:0 auto;background:#1e293b;border-radius:10px;
              padding:28px 32px;box-shadow:0 4px 16px rgba(0,0,0,.4)">
    <h2 style="margin:0 0 6px;color:#f1f5f9;font-size:17px;font-weight:700">
      {status_emoji} Login {status_word}
    </h2>
    <p style="margin:0 0 20px;color:#94a3b8;font-size:13px">Brasfield Momentum Dashboard</p>
    <div style="background:#0f172a;border-left:4px solid {status_color};
                border-radius:6px;padding:14px 18px;margin-bottom:0">
      <table style="border-collapse:collapse;font-size:14px;color:#e2e8f0;width:100%">
        <tr><td style="color:#888;padding:3px 8px 3px 0">Status</td>
            <td><strong style="color:{status_color}">{status_word}</strong></td></tr>
        <tr><td style="color:#888;padding:3px 8px 3px 0">Username</td>
            <td><strong>{username}</strong></td></tr>
        <tr><td style="color:#888;padding:3px 8px 3px 0">Time</td>
            <td>{now}</td></tr>
        <tr><td style="color:#888;padding:3px 8px 3px 0">IP</td>
            <td>{ip or '—'}</td></tr>
        {loc_row}
        {ua_row}
      </table>
    </div>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{FROM_NAME} <{from_addr}>"
    msg["To"]      = NOTIFY_TO
    msg["Bcc"]     = NOTIFY_BCC
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    _send(msg, [NOTIFY_TO, NOTIFY_BCC], host, port, user, password, from_addr, f"login email ({status_word})")


def send_suggestion_email(message: str, ip: str = "", ua: str = ""):
    """
    Send a 'New Suggestion' notification email.
    Silently skips if SMTP is not configured in secrets.json.
    Runs synchronously — call from a thread to avoid blocking the event loop.
    """
    if not _is_configured():
        log.debug("[EMAIL] SMTP not configured — skipping suggestion email")
        return

    host      = _cfg("smtp_host")
    port      = int(_cfg("smtp_port", "587"))
    user      = _cfg("smtp_user")
    password  = _cfg("smtp_pass")
    from_addr = _cfg("smtp_from") or user

    # ── Build message ─────────────────────────────────────────────
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "New Suggestion"
    msg["From"]    = f"{FROM_NAME} <{from_addr}>"
    msg["To"]      = NOTIFY_TO
    msg["Bcc"]     = NOTIFY_BCC

    # Plain-text body
    plain = f"New suggestion received:\n\n{message}"
    if ip:
        plain += f"\n\nFrom IP: {ip}"
    if ua:
        plain += f"\nBrowser: {ua}"

    # HTML body
    safe_msg = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<!doctype html>
<html>
<body style="font-family:Arial,sans-serif;background:#f4f4f4;padding:20px">
  <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:8px;
              padding:28px 32px;box-shadow:0 2px 8px rgba(0,0,0,.08)">
    <h2 style="margin:0 0 16px;color:#1a2e4a;font-size:18px">
      📬 New Suggestion — Brasfield Momentum
    </h2>
    <div style="background:#f8fafc;border-left:4px solid #3888ff;padding:12px 16px;
                border-radius:4px;font-size:15px;color:#222;line-height:1.6">
      {safe_msg}
    </div>
    {"<p style='margin:14px 0 0;font-size:12px;color:#888'>From IP: " + ip + "</p>" if ip else ""}
  </div>
</body>
</html>"""

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    _send(msg, [NOTIFY_TO, NOTIFY_BCC], host, port, user, password, from_addr, "suggestion email")
