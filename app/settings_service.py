"""
Runtime settings service.

Provides a DB-backed override layer on top of pydantic-settings env vars.
On startup, load_settings_from_db() reads SalonSetting rows and patches the
global `settings` object so all consumers (SMS, email, reminder loop, etc.)
pick up the DB values transparently.

Editable settings are defined in SETTINGS_METADATA — a declarative list that
drives both the API and the dashboard UI.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import SalonSetting

# ── Settings metadata ────────────────────────────────────────────────────────

SETTINGS_METADATA: list[dict[str, Any]] = [
    # ── Salon & Gérant ───────────────────────────────────────
    {
        "key": "OWNER_PHONE",
        "label": "Téléphone du gérant",
        "description": "Numéro qui reçoit les alertes SMS (nouveaux RDV, annulations).",
        "section": "Salon & Gérant",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "SALON_EMAIL",
        "label": "Email du gérant",
        "description": "Adresse qui reçoit les alertes email (nouveaux RDV, demandes de rappel).",
        "section": "Salon & Gérant",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "SALON_EMAIL_FROM",
        "label": "Adresse expéditeur email",
        "description": "Adresse affichée comme expéditeur dans les emails de notification.",
        "section": "Salon & Gérant",
        "type": "str",
        "is_sensitive": False,
    },
    # ── Twilio SMS ───────────────────────────────────────────
    {
        "key": "TWILIO_ACCOUNT_SID",
        "label": "Twilio Account SID",
        "description": "Identifiant de compte Twilio (commence par AC…).",
        "section": "SMS (Twilio)",
        "type": "str",
        "is_sensitive": True,
    },
    {
        "key": "TWILIO_AUTH_TOKEN",
        "label": "Twilio Auth Token",
        "description": "Token d'authentification Twilio.",
        "section": "SMS (Twilio)",
        "type": "str",
        "is_sensitive": True,
    },
    {
        "key": "TWILIO_PHONE_NUMBER",
        "label": "N° Twilio du salon",
        "description": "Numéro Twilio utilisé pour envoyer les SMS (ex : +33XXXXXXXXX).",
        "section": "SMS (Twilio)",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "TWILIO_TRANSFER_NUMBER",
        "label": "N° de renvoi d'appel",
        "description": "Numéro humain vers lequel l'agent vocal peut transférer l'appel.",
        "section": "SMS (Twilio)",
        "type": "str",
        "is_sensitive": False,
    },
    # ── Resend Email ─────────────────────────────────────────
    {
        "key": "RESEND_API_KEY",
        "label": "Clé API Resend",
        "description": "Clé API Resend.com pour l'envoi des emails de notification.",
        "section": "Email (Resend)",
        "type": "str",
        "is_sensitive": True,
    },
    # ── Rappels SMS ──────────────────────────────────────────
    {
        "key": "REMINDER_ENABLED",
        "label": "Rappels SMS J-1 activés",
        "description": "Envoie automatiquement un SMS de rappel la veille de chaque rendez-vous.",
        "section": "Rappels SMS",
        "type": "bool",
        "is_sensitive": False,
    },
    {
        "key": "REMINDER_HOUR",
        "label": "Heure d'envoi des rappels",
        "description": "Heure à laquelle les rappels SMS sont envoyés (0-23, heure locale).",
        "section": "Rappels SMS",
        "type": "int",
        "is_sensitive": False,
    },
    # ── RGPD & Rétention ─────────────────────────────────────
    {
        "key": "SESSION_RETENTION_DAYS",
        "label": "Rétention sessions vocales (jours)",
        "description": "Suppression automatique des sessions et transcripts plus anciens que cette durée.",
        "section": "RGPD & Rétention",
        "type": "int",
        "is_sensitive": False,
    },
    {
        "key": "CALLBACK_RETENTION_DAYS",
        "label": "Rétention demandes de rappel (jours)",
        "description": "Suppression automatique des demandes de rappel résolues plus anciennes que cette durée.",
        "section": "RGPD & Rétention",
        "type": "int",
        "is_sensitive": False,
    },
    {
        "key": "PURGE_HOUR",
        "label": "Heure de purge RGPD",
        "description": "Heure à laquelle la purge automatique s'exécute chaque nuit (0-23, heure locale).",
        "section": "RGPD & Rétention",
        "type": "int",
        "is_sensitive": False,
    },
    # ── Agent Vocal ──────────────────────────────────────────
    {
        "key": "ELEVENLABS_API_KEY",
        "label": "Clé API ElevenLabs",
        "description": "Clé API ElevenLabs pour la synthèse vocale de l'agent téléphonique.",
        "section": "Agent Vocal",
        "type": "str",
        "is_sensitive": True,
    },
    {
        "key": "ELEVENLABS_VOICE_ID",
        "label": "ID de voix ElevenLabs",
        "description": "Identifiant de la voix ElevenLabs utilisée par l'agent (ex : lvQdCgwZfBuOzxyV5pxu).",
        "section": "Agent Vocal",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "VOICE_API_KEY",
        "label": "Clé API admin (dashboard)",
        "description": "Token requis pour accéder au dashboard admin. Laissez vide pour désactiver l'auth.",
        "section": "Agent Vocal",
        "type": "str",
        "is_sensitive": True,
    },
]

_VALID_KEYS: frozenset[str] = frozenset(m["key"] for m in SETTINGS_METADATA)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _apply_to_settings(key: str, raw: str) -> None:
    """Cast `raw` to the expected Python type and patch the global settings object."""
    meta = next((m for m in SETTINGS_METADATA if m["key"] == key), None)
    if meta is None:
        return
    try:
        if meta["type"] == "bool":
            value: Any = raw.lower() in ("1", "true", "yes", "on")
        elif meta["type"] == "int":
            value = int(raw)
        else:
            value = raw
        setattr(settings, key, value)
    except (ValueError, TypeError):
        pass  # ignore malformed DB values — env var default remains


def _mask(value: str) -> str:
    """Return a masked representation: first 4 chars + ••••••"""
    if not value:
        return ""
    return value[:4] + "••••••" if len(value) > 4 else "••••••"


# ── Public API ───────────────────────────────────────────────────────────────

async def load_settings_from_db(session: AsyncSession) -> None:
    """
    Called once at app startup.
    Reads all SalonSetting rows and patches the global `settings` object.
    """
    result = await session.execute(select(SalonSetting))
    rows = result.scalars().all()
    for row in rows:
        if row.key in _VALID_KEYS and row.value is not None:
            _apply_to_settings(row.key, row.value)


async def update_settings(session: AsyncSession, updates: dict[str, str]) -> None:
    """
    Upsert DB rows for `updates` (key→raw_string) then patch `settings` in memory.
    Unknown keys are silently ignored.
    """
    for key, raw in updates.items():
        if key not in _VALID_KEYS:
            continue
        # merge() does INSERT or UPDATE based on primary key — works with SQLite & PostgreSQL
        row = SalonSetting(key=key, value=raw)
        await session.merge(row)
        _apply_to_settings(key, raw)
    await session.commit()


def get_settings_with_values() -> list[dict[str, Any]]:
    """
    Return SETTINGS_METADATA enriched with the current effective value of each setting.
    Sensitive values are masked unless empty.
    """
    result = []
    for meta in SETTINGS_METADATA:
        key = meta["key"]
        raw: Any = getattr(settings, key, "")
        # Normalise to string for the API response
        if isinstance(raw, bool):
            str_val = "true" if raw else "false"
        else:
            str_val = str(raw) if raw is not None else ""

        is_set = bool(str_val)
        display_val = _mask(str_val) if (meta["is_sensitive"] and is_set) else str_val

        result.append({
            **meta,
            "value": display_val,
            "is_set": is_set,
        })
    return result
