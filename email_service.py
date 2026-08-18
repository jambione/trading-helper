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

Recipient defaults to the operator iCloud inbox; override with
``notify_to`` / ``notify_bcc`` in secrets.json if needed.
"""

import json
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

log = logging.getLogger(__name__)

_SECRETS_FILE = Path(__file__).parent / "config" / "secrets.json"

# ── Defaults (overridable via secrets.json / env) ─────────────────────────────

DEFAULT_NOTIFY_TO  = "jambione@icloud.com"
DEFAULT_NOTIFY_BCC = ""  # optional second inbox
FROM_NAME  = "Brasfield Momentum"

# Back-compat module attributes (tests / importers)
NOTIFY_TO  = DEFAULT_NOTIFY_TO
NOTIFY_BCC = DEFAULT_NOTIFY_BCC


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
    v = _secrets().get(key)
    if v is None or v == "":
        v = os.getenv(key.upper(), default)
    return str(v) if v is not None else default


def _notify_to() -> str:
    return _cfg("notify_to") or _cfg("smtp_notify_to") or DEFAULT_NOTIFY_TO


def _notify_bcc() -> str:
    return _cfg("notify_bcc") or _cfg("smtp_notify_bcc") or DEFAULT_NOTIFY_BCC


def _is_configured() -> bool:
    return bool(_cfg("smtp_host") and _cfg("smtp_user") and _cfg("smtp_pass"))


def smtp_status() -> dict:
    """Safe diagnostics for /api/meta or ops checks (no secrets)."""
    return {
        "configured": _is_configured(),
        "smtp_host": _cfg("smtp_host") or None,
        "smtp_port": int(_cfg("smtp_port", "587") or "587"),
        "smtp_user": _cfg("smtp_user") or None,
        "smtp_from": _cfg("smtp_from") or _cfg("smtp_user") or None,
        "notify_to": _notify_to(),
        "notify_bcc": _notify_bcc() or None,
        "has_password": bool(_cfg("smtp_pass")),
    }


# ── Public ────────────────────────────────────────────────────────────────────

def _send(msg, recipients, host, port, user, password, from_addr, label: str) -> bool:
    """Internal helper — sends a pre-built MIME message. Returns True on success."""
    try:
        if port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as server:
                server.login(user, password)
                server.sendmail(from_addr, recipients, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(user, password)
                server.sendmail(from_addr, recipients, msg.as_string())
        log.info("[EMAIL] %s sent to %s", label, ", ".join(recipients))
        return True
    except Exception as e:
        log.warning("[EMAIL] Failed to send %s: %s", label, e)
        return False


def send_login_email(username: str, success: bool, ip: str = "", ua: str = "", location: str = "") -> bool:
    """Notify on failed logins only.

    Successful machine/desk logins (OCR, momentum, engine) used to fire
    one SMTP send each and stall /api/state. Success is recorded in the
    login log; do not email it.
    """
    if success:
        return False
    if not _is_configured():
        log.warning("[EMAIL] SMTP not configured — skipping login email "
                    "(add smtp_host/smtp_user/smtp_pass to config/secrets.json)")
        return False

    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S ET")

    host      = _cfg("smtp_host")
    port      = int(_cfg("smtp_port", "587") or "587")
    user      = _cfg("smtp_user")
    password  = _cfg("smtp_pass")
    from_addr = _cfg("smtp_from") or user
    to_addr   = _notify_to()
    bcc_addr  = _notify_bcc()

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
    msg["To"]      = to_addr
    recipients = [to_addr]
    if bcc_addr and bcc_addr not in recipients:
        msg["Bcc"] = bcc_addr
        recipients.append(bcc_addr)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    return _send(msg, recipients, host, port, user, password, from_addr,
                 f"login email ({status_word})")


def send_suggestion_email(message: str, ip: str = "", ua: str = "") -> bool:
    """
    Send a 'New Suggestion' notification email.

    Returns True if SMTP accepted the message, False if not configured or
    the send failed. Always logs clearly either way.
    Runs synchronously — call from a thread to avoid blocking the event loop.
    """
    if not _is_configured():
        log.warning(
            "[EMAIL] SMTP not configured — suggestion saved to disk but NOT emailed. "
            "Add smtp_host, smtp_user, smtp_pass to config/secrets.json on the mini."
        )
        return False

    host      = _cfg("smtp_host")
    port      = int(_cfg("smtp_port", "587") or "587")
    user      = _cfg("smtp_user")
    password  = _cfg("smtp_pass")
    from_addr = _cfg("smtp_from") or user
    to_addr   = _notify_to()
    bcc_addr  = _notify_bcc()

    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S ET")

    # ── Build message ─────────────────────────────────────────────
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📬 Dashboard feedback — {now}"
    msg["From"]    = f"{FROM_NAME} <{from_addr}>"
    msg["To"]      = to_addr
    recipients = [to_addr]
    if bcc_addr and bcc_addr not in recipients:
        msg["Bcc"] = bcc_addr
        recipients.append(bcc_addr)

    # Plain-text body
    plain = f"New dashboard feedback received ({now}):\n\n{message}"
    if ip:
        plain += f"\n\nFrom IP: {ip}"
    if ua:
        plain += f"\nBrowser: {ua}"

    # HTML body
    safe_msg = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_msg = safe_msg.replace("\n", "<br>")
    html = f"""<!doctype html>
<html>
<body style="font-family:Arial,sans-serif;background:#f4f4f4;padding:20px">
  <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:8px;
              padding:28px 32px;box-shadow:0 2px 8px rgba(0,0,0,.08)">
    <h2 style="margin:0 0 16px;color:#1a2e4a;font-size:18px">
      📬 New Suggestion — Brasfield Momentum
    </h2>
    <p style="margin:0 0 12px;font-size:12px;color:#888">{now}</p>
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

    return _send(msg, recipients, host, port, user, password, from_addr, "suggestion email")


def _esc(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _compose(to_addr: str, subject: str, plain: str, html: str, label: str) -> bool:
    if not _is_configured():
        log.warning("[EMAIL] SMTP not configured — skipping %s", label)
        return False
    if not to_addr:
        return False
    host      = _cfg("smtp_host")
    port      = int(_cfg("smtp_port", "587") or "587")
    user      = _cfg("smtp_user")
    password  = _cfg("smtp_pass")
    from_addr = _cfg("smtp_from") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{FROM_NAME} <{from_addr}>"
    msg["To"]      = to_addr
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))
    return _send(msg, [to_addr], host, port, user, password, from_addr, label)


def _card_html(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html>
<body style="font-family:Arial,sans-serif;background:#0f172a;padding:24px;margin:0">
  <div style="max-width:480px;margin:0 auto;background:#1e293b;border-radius:10px;
              padding:28px 32px;box-shadow:0 4px 16px rgba(0,0,0,.4)">
    <h2 style="margin:0 0 6px;color:#f1f5f9;font-size:17px;font-weight:700">{title}</h2>
    <p style="margin:0 0 16px;color:#94a3b8;font-size:13px">Trader Bro / Brasfield Momentum</p>
    {body_html}
  </div>
</body>
</html>"""


def send_access_request_email(username: str, email: str, display_name: str) -> bool:
    """Notify the operator that someone asked for an account."""
    to_addr = _notify_to()
    subject = f"Access request — {username}"
    plain = (
        f"New access request\n\n"
        f"Name:     {display_name}\n"
        f"Username: {username}\n"
        f"Email:    {email}\n\n"
        f"Approve them from Admin → Users on the dashboard."
    )
    html = _card_html("New access request", f"""
      <p style="color:#e2e8f0;font-size:14px;line-height:1.6">
        <strong>{_esc(display_name)}</strong> asked for a login.<br>
        Username: <strong>{_esc(username)}</strong><br>
        Email: {_esc(email)}
      </p>
      <p style="color:#94a3b8;font-size:13px">Approve them from Admin → Users.</p>
    """)
    return _compose(to_addr, subject, plain, html, "access request")


def send_access_received_email(to_addr: str, display_name: str) -> bool:
    """Tell the applicant we have their request."""
    subject = "We received your Trader Bro access request"
    name = display_name or "there"
    plain = (
        f"Hi {name},\n\n"
        f"We received your request for a Trader Bro login. "
        f"You'll get another email when an admin approves it.\n"
    )
    html = _card_html("Request received", f"""
      <p style="color:#e2e8f0;font-size:14px;line-height:1.6">
        Hi {_esc(name)}, we have your request. You'll get another email
        when an admin approves your login.
      </p>
    """)
    return _compose(to_addr, subject, plain, html, "access received")


def send_access_approved_email(to_addr: str, display_name: str, login_url: str) -> bool:
    subject = "Your Trader Bro login is ready"
    name = display_name or "there"
    plain = (
        f"Hi {name},\n\n"
        f"Your account is approved. Sign in at:\n{login_url}\n"
    )
    html = _card_html("You're in", f"""
      <p style="color:#e2e8f0;font-size:14px;line-height:1.6">
        Hi {_esc(name)}, your account is approved.
      </p>
      <p><a href="{_esc(login_url)}" style="color:#7dd3fc">Sign in</a></p>
    """)
    return _compose(to_addr, subject, plain, html, "access approved")


def send_password_reset_email(to_addr: str, reset_url: str) -> bool:
    subject = "Reset your Trader Bro password"
    plain = (
        f"Reset your password using this link (expires in one hour):\n\n"
        f"{reset_url}\n\n"
        f"If you did not ask for this, you can ignore the email."
    )
    html = _card_html("Reset your password", f"""
      <p style="color:#e2e8f0;font-size:14px;line-height:1.6">
        Use this link within one hour to choose a new password.
      </p>
      <p><a href="{_esc(reset_url)}" style="color:#7dd3fc">Choose a new password</a></p>
      <p style="color:#94a3b8;font-size:12px">If you did not ask for this, ignore the email.</p>
    """)
    return _compose(to_addr, subject, plain, html, "password reset")
