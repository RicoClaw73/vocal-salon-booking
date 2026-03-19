"""
Email notification via Resend REST API.

Sends an alert to the salon when a client leaves a voicemail callback request.

Silent no-op when:
  - RESEND_API_KEY not configured
  - SALON_EMAIL not configured

Never raises — a failed email must never block the recording callback.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"


async def send_owner_booking_email(
    booking_id: int,
    svc_label: str,
    emp_name: str,
    date_str: str,
    time_str: str,
    client_name: str,
    client_phone: str | None,
) -> bool:
    """
    Send an email to the salon owner on each new booking via Resend.
    Returns True on success, False on any failure. Never raises.
    """
    if not settings.RESEND_API_KEY or not settings.SALON_EMAIL:
        logger.debug("Owner booking email skipped: Resend/SALON_EMAIL not configured")
        return False

    phone_display = client_phone or "—"
    # Format date nicely: "2026-03-23" → "23/03/2026"
    try:
        from datetime import date as _date
        d = _date.fromisoformat(date_str)
        date_display = d.strftime("%d/%m/%Y")
    except Exception:
        date_display = date_str

    html = (
        "<div style='font-family:sans-serif;color:#1e293b;max-width:560px'>"
        "<h2 style='margin:0 0 16px'>Nouveau rendez-vous #{booking_id}</h2>"
        "<table style='border-collapse:collapse;width:100%'>"
        "<tr><td style='padding:6px 0;color:#64748b;width:130px'>Prestation</td>"
        f"<td style='padding:6px 0'><strong>{svc_label}</strong></td></tr>"
        "<tr><td style='padding:6px 0;color:#64748b'>Coiffeur·se</td>"
        f"<td style='padding:6px 0'>{emp_name}</td></tr>"
        "<tr><td style='padding:6px 0;color:#64748b'>Date</td>"
        f"<td style='padding:6px 0'>{date_display} à {time_str}</td></tr>"
        "<tr><td style='padding:6px 0;color:#64748b'>Client</td>"
        f"<td style='padding:6px 0'>{client_name}</td></tr>"
        "<tr><td style='padding:6px 0;color:#64748b'>Téléphone</td>"
        f"<td style='padding:6px 0'>{phone_display}</td></tr>"
        "</table>"
        "<p style='margin-top:24px;color:#94a3b8;font-size:13px'>"
        "Maison Éclat — Tableau de bord → Réservations</p>"
        "</div>"
    ).format(booking_id=booking_id)

    subject = f"[Maison Éclat] Nouveau RDV #{booking_id} — {client_name}"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                _RESEND_URL,
                json={
                    "from": settings.SALON_EMAIL_FROM,
                    "to": [settings.SALON_EMAIL],
                    "subject": subject,
                    "html": html,
                },
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            )
        if resp.status_code in (200, 201):
            logger.info("Owner booking email sent for booking #%d", booking_id)
            return True
        logger.warning("Owner booking email HTTP %d: %s", resp.status_code, resp.text[:200])
        return False
    except httpx.TimeoutException:
        logger.warning("Owner booking email timeout for booking #%d", booking_id)
        return False
    except Exception as exc:
        logger.warning("Owner booking email error: %s", exc)
        return False


async def send_callback_notification(
    caller_phone: str | None,
    recording_url: str | None,
    transcription: str | None,
    callback_id: int,
    created_at: datetime,
) -> bool:
    """
    Send an email to the salon notifying of a new callback request.
    Returns True on success, False on any failure.
    """
    if not settings.RESEND_API_KEY or not settings.SALON_EMAIL:
        logger.debug("Email notification skipped: Resend/SALON_EMAIL not configured")
        return False

    phone_display = caller_phone or "Numéro inconnu"
    date_str = created_at.strftime("%d/%m/%Y à %Hh%M")

    lines = [
        "<h2 style='margin:0 0 16px'>Nouvelle demande de rappel</h2>",
        f"<p><strong>N° :</strong> {phone_display}</p>",
        f"<p><strong>Reçue le :</strong> {date_str}</p>",
        f"<p><strong>Réf. :</strong> #{callback_id}</p>",
    ]
    if recording_url:
        lines.append(
            f"<p><strong>Enregistrement :</strong> "
            f'<a href="{recording_url}">Écouter le message</a></p>'
        )
    if transcription:
        lines.append(
            f"<p><strong>Transcription :</strong></p>"
            f"<blockquote style='border-left:3px solid #e2e8f0;margin:0;padding:8px 16px;"
            f"color:#475569'>{transcription}</blockquote>"
        )
    else:
        lines.append("<p><em>Transcription en cours…</em></p>")

    lines.append(
        "<p style='margin-top:24px;color:#94a3b8;font-size:13px'>"
        "Maison Éclat — Tableau de bord → onglet Rappels</p>"
    )

    html = (
        "<div style='font-family:sans-serif;color:#1e293b;max-width:560px'>"
        + "\n".join(lines)
        + "</div>"
    )

    subject = (
        f"[Maison Éclat] Rappel #{callback_id} — {phone_display}"
    )

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                _RESEND_URL,
                json={
                    "from": settings.SALON_EMAIL_FROM,
                    "to": [settings.SALON_EMAIL],
                    "subject": subject,
                    "html": html,
                },
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            )
        if resp.status_code in (200, 201):
            logger.info("Callback email sent for request #%d", callback_id)
            return True
        logger.warning("Callback email HTTP %d: %s", resp.status_code, resp.text[:200])
        return False
    except httpx.TimeoutException:
        logger.warning("Callback email timeout for request #%d", callback_id)
        return False
    except Exception as exc:
        logger.warning("Callback email error: %s", exc)
        return False
