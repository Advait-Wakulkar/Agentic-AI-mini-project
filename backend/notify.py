"""Sending things to patients: calendar invites (.ics), email and SMS.

- Calendar invites are REAL files (RFC 5545); any calendar app can open them.
- Email goes to a SIMULATED outbox (stored in the `notifications` table and shown in the UI). If SMTP_HOST is set
  (for example a local Mailpit on port 1025) it is ALSO really sent over SMTP.
- SMS is always simulated: real texting needs a paid provider and real numbers, and every patient number here is a
  fictional 555-01XX number by design.

The recipient always comes from the patient record. No model ever chooses an address.
"""

import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from backend import config
from backend.db import IntegrityError


def make_ics(appt: dict, patient_name: str, address: str) -> str:
    """A standard VCALENDAR with one 30-minute VEVENT for this appointment."""
    start = datetime.combine(date.fromisoformat(appt["date"]), datetime.strptime(appt["time"], "%H:%M").time())
    end = start + timedelta(minutes=30)
    fmt = "%Y%m%dT%H%M%S"
    stamp = datetime.now().strftime(fmt)
    summary = f"Dental appointment with {appt['provider']} ({appt.get('reason') or 'visit'})"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Bright Smile Dental (fictional)//Scheduling Agent//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:appt-{appt['id']}@brightsmile.example",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{start.strftime(fmt)}",
        f"DTEND:{end.strftime(fmt)}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:Bright Smile Dental (fictional demo office). Patient: {patient_name}.",
        f"LOCATION:{address}",
        "BEGIN:VALARM", "TRIGGER:-PT2H", "ACTION:DISPLAY", "DESCRIPTION:Dental appointment in 2 hours", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "mailpit"}


def may_send_for_real(to: str) -> bool:
    """Real email goes out only to a local mail catcher, or to an address the owner allowlisted in .env.
    Everyone else stays in the simulated outbox, so the app can never email a stranger."""
    if not config.SMTP_HOST:
        return False
    return config.SMTP_HOST.lower() in _LOCAL_HOSTS or to.strip().lower() in config.REAL_CONTACT_ALLOWLIST


def _send_smtp(to: str, subject: str, body: str, ics: str | None) -> None:
    msg = EmailMessage()
    sender = config.MAIL_FROM
    if config.SMTP_USER and "brightsmile.example" in sender:      # Gmail etc. only send as the signed-in account
        sender = f"Bright Smile Dental (demo) <{config.SMTP_USER}>"
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(body + "\n\n(Sent by the Bright Smile Dental demo, a student project. Fictional office.)")
    if ics:
        msg.add_attachment(ics.encode(), maintype="text", subtype="calendar", filename="appointment.ics")
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as s:
        if config.SMTP_STARTTLS:
            s.starttls()
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)


def deliver(conn, appt: dict, profile: dict, kind: str, channel: str, body: str, subject: str | None = None,
            ics: str | None = None, sent_by: str = "reminder-agent", at: datetime | None = None) -> dict:
    """Send one email or SMS and record it. Returns the stored notification, or ok=False if already sent."""
    to = profile.get("email") if channel == "email" else profile.get("phone")
    if not to:
        return {"ok": False, "error": f"No {channel} contact on file for this patient."}
    status = "simulated"
    if channel == "email" and may_send_for_real(to):
        try:
            _send_smtp(to, subject or "Your appointment", body, ics)
            status = "sent"
        except (OSError, smtplib.SMTPException) as e:   # record the failure honestly, never pretend it was sent
            status = "failed"
            reason = type(e).__name__ if isinstance(e, smtplib.SMTPAuthenticationError) else str(e)[:120]
            body = body + f"\n\n[delivery failed: {reason}]"          # (never includes the password)
    try:
        nid = conn.insert(
            "INSERT INTO notifications (appointment_id, patient_alias, kind, channel, to_address, subject, body, ics,"
            " status, sent_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (appt["id"], appt["patient_alias"], kind, channel, to, subject, body, ics, status, sent_by,
             (at or datetime.now()).isoformat(timespec="seconds")))
    except IntegrityError:
        return {"ok": False, "duplicate": True, "error": f"A {kind} {channel} was already sent for this appointment."}
    return {"ok": status != "failed", "id": nid, "channel": channel, "to": to, "status": status,
            "simulated": status == "simulated", "invite_attached": bool(ics)}


def list_notifications(conn, patient_alias: str | None = None, limit: int = 100) -> list[dict]:
    sql = ("SELECT n.id, n.appointment_id, n.patient_alias, n.kind, n.channel, n.to_address, n.subject, n.body,"
           " n.ics IS NOT NULL AS has_invite, n.status, n.sent_by, n.created_at FROM notifications n")
    params: tuple = ()
    if patient_alias:
        sql, params = sql + " WHERE n.patient_alias = ?", (patient_alias,)
    return conn.query(sql + " ORDER BY n.id DESC LIMIT ?", params + (limit,))


def get_notification(conn, notification_id: int) -> dict | None:
    return conn.one("SELECT * FROM notifications WHERE id = ?", (notification_id,))
