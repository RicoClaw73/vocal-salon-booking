"""
Application configuration.

Uses pydantic-settings to load from env vars / .env file.
SQLite is the default for local dev; switch DATABASE_URL to a PostgreSQL
connection string for production.

Provider selection (Phase 4):
  STT_PROVIDER / TTS_PROVIDER control which speech providers are used.
  Default is "mock" (no credentials needed). Set to a real provider name
  (e.g. "deepgram", "elevenlabs") **and** provide the matching API key to
  activate the real provider.  If the key is missing or empty the factory
  will fall back to mock automatically — so CI / local dev never breaks.

  See .env.example for the full list of supported variables.
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

    # ── Voice providers (Phase 4) ─────────────────────────────
    # Provider names: "mock" (default), "deepgram", "whisper", "google"
    STT_PROVIDER: str = "mock"
    STT_API_KEY: str = ""
    STT_MODEL: str = ""  # Provider-specific model override (e.g. "nova-2")

    # Provider names: "mock" (default), "elevenlabs", "google"
    TTS_PROVIDER: str = "mock"
    TTS_API_KEY: str = ""
    TTS_VOICE_ID: str = ""  # Provider-specific voice ID
    TTS_MODEL: str = ""  # Provider-specific model override

    # ── Security / hardening (Phase 4.3) ──────────────────────
    # Optional API key for voice endpoints.  When empty (default), auth
    # is disabled — convenient for local dev.  Set to a non-empty string
    # to require callers to send  X-API-Key: <value>.
    VOICE_API_KEY: str = ""

    # Rate limiting (in-memory, per-client-IP)
    RATE_LIMIT_PER_MINUTE: int = 60  # 0 = disabled

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
