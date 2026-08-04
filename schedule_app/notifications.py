"""Delivery of schedules to employees over email and SMS.

Two channels, each self-contained and each degrading gracefully:

  * Email  -- stdlib smtplib/ssl, configured through SMTP_* env vars.
  * SMS    -- Twilio, configured through TWILIO_* env vars. If the twilio
              package isn't installed, SMS is simply reported unavailable
              rather than crashing the app.

Both entry points return a (ok, message) tuple so the web layer can show a
clear result without needing to know how each channel works.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Sequence

# --------------------------------------------------------------------------- #
# Configuration helpers
# --------------------------------------------------------------------------- #

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def email_configured() -> bool:
    return bool(_env("SMTP_HOST") and _env("SMTP_USER") and _env("SMTP_PASSWORD"))


def sms_configured() -> bool:
    return bool(
        _env("TWILIO_ACCOUNT_SID")
        and _env("TWILIO_AUTH_TOKEN")
        and _env("TWILIO_FROM_NUMBER")
    )


# --------------------------------------------------------------------------- #
# Message formatting
# --------------------------------------------------------------------------- #

def _fmt_time(hhmm: str) -> str:
    """Turn 24h 'HH:MM' into a friendly '9:00 AM'; fall back to raw input."""
    try:
        from datetime import datetime

        return datetime.strptime(hhmm, "%H:%M").strftime("%-I:%M %p")
    except (ValueError, TypeError):
        return hhmm


def _fmt_date(iso: str) -> str:
    """Turn '2026-08-10' into 'Mon, Aug 10, 2026'; fall back to raw input."""
    try:
        from datetime import datetime

        return datetime.strptime(iso, "%Y-%m-%d").strftime("%a, %b %d, %Y")
    except (ValueError, TypeError):
        return iso


def build_schedule_text(employee_name: str, shifts: Sequence) -> str:
    """Plain-text schedule body shared by email and SMS."""
    if not shifts:
        return f"Hi {employee_name}, you have no upcoming shifts scheduled."

    lines = [f"Hi {employee_name}, here is your upcoming work schedule:", ""]
    for s in shifts:
        line = f"- {_fmt_date(s['work_date'])}: {_fmt_time(s['start_time'])}-{_fmt_time(s['end_time'])}"
        if s["location"]:
            line += f" @ {s['location']}"
        lines.append(line)
        if s["notes"]:
            lines.append(f"    Note: {s['notes']}")
    lines.append("")
    lines.append("Please reply if you have any questions. Thank you!")
    return "\n".join(lines)


def build_schedule_html(employee_name: str, shifts: Sequence) -> str:
    """Simple HTML body for the email version."""
    if not shifts:
        return f"<p>Hi {employee_name}, you have no upcoming shifts scheduled.</p>"

    rows = []
    for s in shifts:
        loc = s["location"] or ""
        note = f"<br><em>{s['notes']}</em>" if s["notes"] else ""
        rows.append(
            "<tr>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{_fmt_date(s['work_date'])}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>"
            f"{_fmt_time(s['start_time'])} &ndash; {_fmt_time(s['end_time'])}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{loc}{note}</td>"
            "</tr>"
        )
    return (
        f"<p>Hi {employee_name}, here is your upcoming work schedule:</p>"
        "<table style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>"
        "<tr>"
        "<th style='text-align:left;padding:6px 12px;border-bottom:2px solid #333'>Date</th>"
        "<th style='text-align:left;padding:6px 12px;border-bottom:2px solid #333'>Time</th>"
        "<th style='text-align:left;padding:6px 12px;border-bottom:2px solid #333'>Location</th>"
        "</tr>"
        + "".join(rows)
        + "</table>"
        "<p>Please reply if you have any questions. Thank you!</p>"
    )


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #

def send_email(to_address: str, subject: str, text_body: str, html_body: str = "") -> tuple[bool, str]:
    if not email_configured():
        return False, "Email is not configured (set SMTP_HOST, SMTP_USER, SMTP_PASSWORD)."
    if not to_address:
        return False, "Employee has no email address on file."

    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT", "587"))
    user = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")
    from_address = _env("SMTP_FROM") or user
    use_ssl = _env("SMTP_USE_SSL", "false").lower() in ("1", "true", "yes")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls(context=context)
                server.login(user, password)
                server.send_message(msg)
        return True, f"Email sent to {to_address}."
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        return False, f"Email failed: {exc}"


# --------------------------------------------------------------------------- #
# SMS (Twilio)
# --------------------------------------------------------------------------- #

def send_sms(to_number: str, body: str) -> tuple[bool, str]:
    if not sms_configured():
        return False, (
            "SMS is not configured (set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
            "TWILIO_FROM_NUMBER)."
        )
    if not to_number:
        return False, "Employee has no phone number on file."

    try:
        from twilio.rest import Client  # imported lazily so the app runs without twilio
    except ImportError:
        return False, "The 'twilio' package is not installed (pip install twilio)."

    sid = _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_AUTH_TOKEN")
    from_number = _env("TWILIO_FROM_NUMBER")

    try:
        client = Client(sid, token)
        message = client.messages.create(body=body, from_=from_number, to=to_number)
        return True, f"SMS sent to {to_number} (sid {message.sid})."
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        return False, f"SMS failed: {exc}"
