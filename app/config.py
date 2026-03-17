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

    # ── LLM provider (pilot wiring) ───────────────────────────
    # NOTE: current intent engine is still rule-based. These settings
    # prepare GPT/OpenAI wiring for upcoming integration.
    LLM_PROVIDER: str = "mock"  # mock|openai
    OPENAI_API_KEY: str = ""
    LLM_MODEL: str = "gpt-4o"

    # ── Security / hardening (Phase 4.3) ──────────────────────
    # Optional API key for voice endpoints.  When empty (default), auth
    # is disabled — convenient for local dev.  Set to a non-empty string
    # to require callers to send  X-API-Key: <value>.
    VOICE_API_KEY: str = ""

    # Rate limiting (in-memory, per-client-IP)
    RATE_LIMIT_PER_MINUTE: int = 60  # 0 = disabled

    # ── Telephony integration (Phase 5.3) ───────────────────────
    # Controls whether the /telephony/* endpoints accept inbound events.
    # Disabled by default — flip to True to start pilot ingestion.
    TELEPHONY_ENABLED: bool = False

    # When True, telephony events are processed through the full pipeline
    # but no outbound side-effects (TTS audio delivery, webhook callbacks)
    # are actually performed.  Useful for shadow/pilot validation.
    TELEPHONY_DRY_RUN: bool = True

    # Telephony provider adapter to use.
    #   "local"  → simulated provider (default, no credentials needed)
    #   "twilio" → Twilio-compatible webhook contract (scaffold)
    #   "vapi"   → Vapi-compatible webhook contract (scaffold)
    TELEPHONY_PROVIDER: str = "local"

    # Optional shared secret for verifying inbound telephony webhooks.
    # When empty, signature verification is skipped (dev/local mode).
    TELEPHONY_WEBHOOK_SECRET: str = ""

    # Maximum payload size (bytes) for inbound telephony events.
    TELEPHONY_MAX_PAYLOAD_BYTES: int = 256_000  # 250 KB

    # Event retention: max age (hours) before processed-event IDs are pruned.
    TELEPHONY_EVENT_TTL_HOURS: int = 24

    # ── Twilio credentials (Phase 5 — real integration) ─────────
    TWILIO_ACCOUNT_SID: str = ""   # ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    TWILIO_AUTH_TOKEN: str = ""    # Used for webhook signature verification
    TWILIO_PHONE_NUMBER: str = ""  # Your Twilio number e.g. +33XXXXXXXXX
    TWILIO_TRANSFER_NUMBER: str = "" # Human fallback number (optional)

    # ── Telephony – Phase 5.4: Pilot real-call flow ───────────
    # Twilio webhook URL for signature verification (must match the URL
    # configured in the Twilio console).  Only used when TELEPHONY_PROVIDER=twilio
    # and TELEPHONY_WEBHOOK_SECRET is non-empty.  Example:
    #   https://yourdomain.com/api/v1/telephony/inbound
    TWILIO_WEBHOOK_URL: str = ""

    # Shadow mode: when True, inbound events are fully processed but NO
    # booking-mutating side effects (booking creation, modification, cancel)
    # are committed.  Decision traces are persisted for operator review.
    # This is stricter than TELEPHONY_DRY_RUN which only suppresses TTS
    # delivery; shadow mode also prevents DB writes that change business state.
    TELEPHONY_SHADOW_MODE: bool = True

    # ── Redis (optional, Phase 5.4) ─────────────────────────────
    # When set, the idempotency guard uses Redis instead of in-memory dict.
    # Format: redis://[:password@]host:port/db  or  rediss://... for TLS.
    # Empty string (default) = use in-memory guard (no Redis dependency).
    REDIS_URL: str = ""

    # Prefix for Redis keys used by the telephony idempotency guard.
    REDIS_KEY_PREFIX: str = "salon:idem:"

    # TTL for Redis idempotency keys (seconds).  Default: matches event TTL.
    REDIS_IDEMPOTENCY_TTL_SECONDS: int = 86400  # 24h

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
