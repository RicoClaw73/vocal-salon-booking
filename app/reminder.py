"""
J-1 appointment reminder loop.

Runs as a background asyncio task.  Every hour, checks whether it is time
to send reminders (controlled by REMINDER_HOUR in config).  When triggered,
queries all confirmed bookings scheduled for tomorrow that have not yet
received a reminder, sends an SMS, and marks reminder_sent = True.

Design decisions:
- reminder_sent flag on Booking prevents duplicates across restarts.
- Checks run every 5 minutes so the system catches the reminder window even
  if started mid-hour.
- Any individual SMS failure is logged but does not abort the batch.
- REMINDER_ENABLED=False (default) is a safe no-op.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import async_session
from app.models import Booking, BookingStatus
from app.sms_sender import send_booking_reminder

logger = logging.getLogger(__name__)

# Check interval (seconds). 5 min granularity is enough for an hourly window.
_CHECK_INTERVAL = 300

# Track the last date reminders were sent to avoid multiple batches in one day.
_last_reminder_date: date | None = None


async def _send_tomorrow_reminders() -> int:
    """
    Query and send reminders for all confirmed bookings scheduled tomorrow.

    Returns the number of reminders sent successfully.
    """
    tomorrow = date.today() + timedelta(days=1)
    sent = 0

    async with async_session() as db:
        result = await db.execute(
            select(Booking)
            .options(selectinload(Booking.service), selectinload(Booking.employee))
            .where(
                and_(
                    Booking.start_time >= tomorrow,
                    Booking.start_time < tomorrow + timedelta(days=1),
                    Booking.status == BookingStatus.confirmed,
                    Booking.reminder_sent.is_(False),
                    Booking.client_phone.is_not(None),
                )
            )
            .order_by(Booking.start_time)
        )
        bookings = result.scalars().all()

        logger.info("Reminder batch: %d booking(s) for %s", len(bookings), tomorrow)

        for booking in bookings:
            if not booking.client_phone:
                continue

            svc_label = booking.service.label if booking.service else ""
            emp_name = (
                f"{booking.employee.prenom} {booking.employee.nom}"
                if booking.employee
                else ""
            )
            date_str = booking.start_time.strftime("%Y-%m-%d")
            time_str = booking.start_time.strftime("%H:%M")

            ok = await send_booking_reminder(
                client_phone=booking.client_phone,
                svc_label=svc_label,
                emp_name=emp_name,
                date_str=date_str,
                time_str=time_str,
            )

            booking.reminder_sent = True  # mark even if SMS failed (avoid spam)
            if ok:
                sent += 1
                logger.info(
                    "Reminder sent: booking #%d → …%s",
                    booking.id,
                    booking.client_phone[-4:],
                )
            else:
                logger.warning(
                    "Reminder SMS failed for booking #%d (marked to avoid retry)",
                    booking.id,
                )

        await db.commit()

    return sent


async def reminder_loop() -> None:
    """
    Background asyncio task — checks every 5 minutes and fires reminders
    once per day at REMINDER_HOUR (local time).

    Exits only if REMINDER_ENABLED is False (checked each iteration so the
    setting can be toggled at runtime via env reload — requires restart).
    """
    global _last_reminder_date

    logger.info(
        "Reminder loop started (enabled=%s, hour=%dh, check every %ds)",
        settings.REMINDER_ENABLED,
        settings.REMINDER_HOUR,
        _CHECK_INTERVAL,
    )

    while True:
        await asyncio.sleep(_CHECK_INTERVAL)

        if not settings.REMINDER_ENABLED:
            continue

        from datetime import datetime
        now = datetime.now()
        today = now.date()

        # Fire once per day at the configured hour
        if now.hour == settings.REMINDER_HOUR and _last_reminder_date != today:
            _last_reminder_date = today
            try:
                count = await _send_tomorrow_reminders()
                logger.info("Reminder batch complete: %d SMS sent for %s", count, today)
            except Exception as exc:
                logger.error("Reminder batch error: %s", exc)
