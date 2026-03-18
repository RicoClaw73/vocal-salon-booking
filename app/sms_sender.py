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
from datetime import date

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

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
    date_fr = _format_date_fr(date_str, time_str)
    return (
        f"Maison Éclat — RDV #{booking_id} confirmé\n"
        f"{svc_label} avec {emp_name}\n"
        f"{date_fr}\n"
        f"📍 42 rue des Petits-Champs, 75002 Paris\n"
        f"Pour modifier ou annuler, rappelez-nous."
    )


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
