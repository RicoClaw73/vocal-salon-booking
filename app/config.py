"""
Application configuration.

Uses pydantic-settings to load from env vars / .env file.
SQLite is the default for local dev; switch DATABASE_URL to a PostgreSQL
connection string for production.
"""

from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data" / "normalized"


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────
    # Default: SQLite file in project root (dev-friendly).
    # Production: postgresql+asyncpg://user:pass@host/db
    DATABASE_URL: str = f"sqlite+aiosqlite:///{BASE_DIR / 'salon.db'}"

    # ── App ───────────────────────────────────────────────────
    APP_TITLE: str = "Maison Éclat – Salon Booking API"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = True

    # ── Slot engine ───────────────────────────────────────────
    SLOT_GRANULARITY_MIN: int = 15  # Slots proposed every 15 min
    DEFAULT_BUFFER_MIN: int = 10
    CHEMICAL_BUFFER_MIN: int = 15
    MAX_ALTERNATIVE_SLOTS: int = 3

    # ── i18n placeholder ──────────────────────────────────────
    DEFAULT_LANG: str = "fr"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
