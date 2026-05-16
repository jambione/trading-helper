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

    # ── Send ──────────────────────────────────────────────────────
    recipients = [NOTIFY_TO, NOTIFY_BCC]
    try:
        if port == 465:
            # SSL from the start
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=10) as server:
                server.login(user, password)
                server.sendmail(from_addr, recipients, msg.as_string())
        else:
            # STARTTLS (port 587 or 25)
            with smtplib.SMTP(host, port, timeout=10) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(user, password)
                server.sendmail(from_addr, recipients, msg.as_string())

        log.info(f"[EMAIL] Suggestion email sent to {NOTIFY_TO} (bcc: {NOTIFY_BCC})")

    except Exception as e:
        # Non-fatal — log and continue; the suggestion is still saved
        log.warning(f"[EMAIL] Failed to send suggestion email: {e}")
