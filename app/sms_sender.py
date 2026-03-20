"""
SMS booking confirmation via Twilio REST API.

Sends a confirmation SMS to the client after create_booking succeeds.
Uses httpx directly — no Twilio SDK needed, consistent with ElevenLabs integration.

Silent no-op when:
  - TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_PHONE_NUMBER not configured
  - client_phone is missing, "anonymous", or "unknown"

Never raises — a failed SMS must never block the booking confirmation.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def _is_valid_phone(phone: str) -> bool:
    """Return True if phone is a plausible E.164 number."""
    return bool(_E164_RE.match(phone))

_DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MONTHS_FR = [
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def _format_date_fr(date_str: str, time_str: str) -> str:
    """'2026-03-20' + '14:30' → 'Vendredi 20 mars à 14h30'."""
    try:
        d = date.fromisoformat(date_str)
        day = _DAYS_FR[d.weekday()]
        month = _MONTHS_FR[d.month]
        h, m = time_str.split(":")
        time_label = f"{int(h)}h{m}" if int(m) != 0 else f"{int(h)}h"
        label = f"{day} {d.day} {month} à {time_label}"
        return label[0].upper() + label[1:]
    except Exception:
        return f"{date_str} à {time_str}"


def _build_sms(
    booking_id: int,
    svc_label: str,
    emp_name: str,
    date_str: str,
    time_str: str,
) -> str:
    """Build SMS body. Must stay under 160 GSM-7 chars (no emoji — would force UCS-2/70 chars)."""
    date_fr = _format_date_fr(date_str, time_str)
    emp_first = emp_name.split()[0] if emp_name else emp_name
    svc_short = svc_label[:28] + "…" if len(svc_label) > 28 else svc_label
    salon_name = settings.SALON_NAME_SHORT or settings.SALON_NAME
    salon_addr = settings.SALON_ADDRESS_SHORT
    addr_line = f"{salon_addr}\n" if salon_addr else ""
    body = (
        f"{salon_name} - RDV #{booking_id}\n"
        f"{svc_short} avec {emp_first}\n"
        f"{date_fr}\n"
        f"{addr_line}"
        f"Modif/annul: rappeler le salon."
    )
    return body[:160]


def _build_reminder_sms(
    svc_label: str,
    emp_name: str,
    date_str: str,
    time_str: str,
) -> str:
    """Build J-1 reminder SMS body. Must stay under 160 GSM-7 chars."""
    date_fr = _format_date_fr(date_str, time_str)
    emp_first = emp_name.split()[0] if emp_name else emp_name
    svc_short = svc_label[:28] + "…" if len(svc_label) > 28 else svc_label
    # "Demain" replaces the full date to save chars and sound natural
    day_label = date_fr.split(" à ")[0]  # e.g. "Vendredi 20 mars"
    time_label = date_fr.split(" à ")[1] if " à " in date_fr else time_str
    salon_name = settings.SALON_NAME_SHORT or settings.SALON_NAME
    salon_addr = settings.SALON_ADDRESS_SHORT
    addr_line = f"{salon_addr}\n" if salon_addr else ""
    body = (
        f"{salon_name} - Rappel RDV\n"
        f"{svc_short} avec {emp_first}\n"
        f"Demain {day_label} à {time_label}\n"
        f"{addr_line}"
        f"Modif/annul: rappeler le salon."
    )
    return body[:160]


async def send_owner_cancel_alert(
    booking_id: int,
    client_name: str,
    client_phone: str | None,
    start_time: "datetime",  # noqa: F821
) -> bool:
    """
    Send a brief SMS alert to the salon owner when a booking is cancelled.
    Returns True on success, False on failure. Never raises.
    """
    sid = settings.TWILIO_ACCOUNT_SID
    token = settings.TWILIO_AUTH_TOKEN
    from_number = settings.TWILIO_PHONE_NUMBER
    owner = settings.OWNER_PHONE

    if not (sid and token and from_number and owner):
        logger.debug("Cancel alert SMS skipped: credentials or OWNER_PHONE not configured")
        return False

    try:
        date_display = start_time.strftime("%d/%m à %Hh%M")
    except Exception:
        date_display = str(start_time)

    phone_line = f"\nTel: {client_phone}" if client_phone else ""
    body = (
        f"[Annulation RDV #{booking_id}]\n"
        f"Client: {client_name}{phone_line}\n"
        f"Prévu: {date_display}"
    )[:160]

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                url,
                data={"From": from_number, "To": owner, "Body": body},
                auth=(sid, token),
            )
        if resp.status_code in (200, 201):
            logger.info("Cancel alert SMS sent for booking #%d", booking_id)
            return True
        logger.warning("Cancel alert SMS HTTP %d: %s", resp.status_code, resp.text[:200])
        return False
    except httpx.TimeoutException:
        logger.warning("Cancel alert SMS timeout for booking #%d", booking_id)
        return False
    except Exception as exc:
        logger.warning("Cancel alert SMS error: %s", exc)
        return False


async def send_owner_reschedule_alert(
    booking_id: int,
    client_name: str,
    client_phone: str | None,
    old_start: datetime,
    new_start: datetime,
) -> bool:
    """
    Send a brief SMS alert to the salon owner when a booking is rescheduled.
    Returns True on success, False on failure. Never raises.
    """
    sid = settings.TWILIO_ACCOUNT_SID
    token = settings.TWILIO_AUTH_TOKEN
    from_number = settings.TWILIO_PHONE_NUMBER
    owner = settings.OWNER_PHONE

    if not (sid and token and from_number and owner):
        logger.debug("Reschedule alert SMS skipped: credentials or OWNER_PHONE not configured")
        return False

    try:
        old_display = old_start.strftime("%d/%m à %Hh%M")
        new_display = new_start.strftime("%d/%m à %Hh%M")
    except Exception:
        old_display = str(old_start)
        new_display = str(new_start)

    phone_line = f"\nTel: {client_phone}" if client_phone else ""
    body = (
        f"[Report RDV #{booking_id}]\n"
        f"Client: {client_name}{phone_line}\n"
        f"De: {old_display}\n"
        f"À: {new_display}"
    )[:160]

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                url,
                data={"From": from_number, "To": owner, "Body": body},
                auth=(sid, token),
            )
        if resp.status_code in (200, 201):
            logger.info("Reschedule alert SMS sent for booking #%d", booking_id)
            return True
        logger.warning("Reschedule alert SMS HTTP %d: %s", resp.status_code, resp.text[:200])
        return False
    except httpx.TimeoutException:
        logger.warning("Reschedule alert SMS timeout for booking #%d", booking_id)
        return False
    except Exception as exc:
        logger.warning("Reschedule alert SMS error: %s", exc)
        return False


async def send_owner_booking_alert(
    booking_id: int,
    svc_label: str,
    emp_name: str,
    date_str: str,
    time_str: str,
    client_name: str,
    client_phone: str | None,
) -> bool:
    """
    Send a brief SMS alert to the salon owner on each new booking.
    Returns True on success, False on failure. Never raises.
    """
    sid = settings.TWILIO_ACCOUNT_SID
    token = settings.TWILIO_AUTH_TOKEN
    from_number = settings.TWILIO_PHONE_NUMBER
    owner = settings.OWNER_PHONE

    if not (sid and token and from_number and owner):
        logger.debug("Owner alert SMS skipped: credentials or OWNER_PHONE not configured")
        return False

    date_fr = _format_date_fr(date_str, time_str)
    emp_first = emp_name.split()[0] if emp_name else emp_name
    svc_short = svc_label[:28] + "…" if len(svc_label) > 28 else svc_label
    phone_line = f"\nTel: {client_phone}" if client_phone else ""
    body = (
        f"[RDV #{booking_id}] {svc_short} / {emp_first}\n"
        f"{date_fr}\n"
        f"Client: {client_name}{phone_line}"
    )[:160]

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                url,
                data={"From": from_number, "To": owner, "Body": body},
                auth=(sid, token),
            )
        if resp.status_code in (200, 201):
            logger.info("Owner alert SMS sent for booking #%d", booking_id)
            return True
        logger.warning("Owner alert SMS HTTP %d: %s", resp.status_code, resp.text[:200])
        return False
    except httpx.TimeoutException:
        logger.warning("Owner alert SMS timeout for booking #%d", booking_id)
        return False
    except Exception as exc:
        logger.warning("Owner alert SMS error: %s", exc)
        return False


async def send_booking_reminder(
    client_phone: str,
    svc_label: str,
    emp_name: str,
    date_str: str,
    time_str: str,
) -> bool:
    """
    Send a J-1 reminder SMS via Twilio. Returns True on success, False on failure.
    Never raises.
    """
    sid = settings.TWILIO_ACCOUNT_SID
    token = settings.TWILIO_AUTH_TOKEN
    from_number = settings.TWILIO_PHONE_NUMBER

    if not (sid and token and from_number):
        logger.debug("Reminder SMS skipped: Twilio credentials not configured")
        return False

    if not client_phone or client_phone.lower() in ("anonymous", "unknown", "") or not _is_valid_phone(client_phone):
        logger.debug("Reminder SMS skipped: no valid client phone number")
        return False

    body = _build_reminder_sms(svc_label, emp_name, date_str, time_str)
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                url,
                data={"From": from_number, "To": client_phone, "Body": body},
                auth=(sid, token),
            )
        if resp.status_code in (200, 201):
            logger.info("Reminder SMS sent to …%s", client_phone[-4:])
            return True
        logger.warning("Reminder SMS HTTP %d: %s", resp.status_code, resp.text[:200])
        return False
    except httpx.TimeoutException:
        logger.warning("Reminder SMS timeout for %s", client_phone[-4:])
        return False
    except Exception as exc:
        logger.warning("Reminder SMS error: %s", exc)
        return False


async def send_booking_confirmation(
    client_phone: str,
    booking_id: int,
    svc_label: str,
    emp_name: str,
    date_str: str,
    time_str: str,
) -> bool:
    """
    Send SMS confirmation via Twilio. Returns True on success, False on failure.
    Never raises.
    """
    sid = settings.TWILIO_ACCOUNT_SID
    token = settings.TWILIO_AUTH_TOKEN
    from_number = settings.TWILIO_PHONE_NUMBER

    if not (sid and token and from_number):
        logger.debug("SMS skipped: Twilio credentials not configured")
        return False

    if not client_phone or client_phone.lower() in ("anonymous", "unknown", ""):
        logger.debug("SMS skipped: no valid client phone number")
        return False

    body = _build_sms(booking_id, svc_label, emp_name, date_str, time_str)
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                url,
                data={"From": from_number, "To": client_phone, "Body": body},
                auth=(sid, token),
            )
        if resp.status_code in (200, 201):
            logger.info("SMS sent to …%s for booking #%d", client_phone[-4:], booking_id)
            return True
        logger.warning("SMS HTTP %d: %s", resp.status_code, resp.text[:200])
        return False
    except httpx.TimeoutException:
        logger.warning("SMS timeout for booking #%d", booking_id)
        return False
    except Exception as exc:
        logger.warning("SMS error: %s", exc)
        return False
