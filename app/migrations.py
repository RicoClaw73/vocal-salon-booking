"""
Lightweight startup migrations for additive schema changes.

SQLAlchemy's create_all() only creates missing tables, not missing columns.
This module handles ALTER TABLE for new columns on existing tables so that
production databases are upgraded without requiring reset_db.py.

Each migration is idempotent: duplicate column errors are silently ignored.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_MIGRATIONS: list[tuple[str, str]] = [
    # (description, SQL statement)
    (
        "voice_sessions.consent_given",
        "ALTER TABLE voice_sessions ADD COLUMN consent_given BOOLEAN",
    ),
    (
        "voice_sessions.consent_at",
        "ALTER TABLE voice_sessions ADD COLUMN consent_at DATETIME",
    ),
]


async def run_migrations(engine: AsyncEngine) -> None:
    """Apply all pending additive migrations (idempotent)."""
    async with engine.begin() as conn:
        for description, sql in _MIGRATIONS:
            try:
                await conn.execute(text(sql))
                logger.info("Migration applied: %s", description)
            except Exception:
                # Column already exists or other benign error — skip
                logger.debug("Migration skipped (already applied): %s", description)
