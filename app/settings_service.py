"""
Runtime settings service.

Provides a DB-backed override layer on top of pydantic-settings env vars.
On startup, load_settings_from_db() reads SalonSetting rows for a given tenant
and caches a per-tenant Settings copy in _tenant_settings.

get_tenant_settings(tenant_id) returns the per-tenant Settings object (or the
global settings singleton as fallback). All salon-specific code should use this
instead of the global `settings` when a tenant context is available.

Editable settings are defined in SETTINGS_METADATA — a declarative list that
drives both the API and the dashboard UI.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import SalonSetting

# ── Per-tenant settings cache ─────────────────────────────────────────────────
# Populated at startup by load_settings_from_db(). Key = tenant_id.

_tenant_settings: dict[int, Any] = {}


def get_tenant_settings(tenant_id: int) -> Any:
    """Return the per-tenant Settings copy, or the global settings as fallback."""
    return _tenant_settings.get(tenant_id, settings)


# ── Settings metadata ────────────────────────────────────────────────────────

SETTINGS_METADATA: list[dict[str, Any]] = [
    # ══════════════════════════════════════════
    # TAB: salon  (informations du salon)
    # ══════════════════════════════════════════

    # ── Informations générales ───────────────────────────────
    {
        "key": "SALON_NAME",
        "label": "Nom du salon",
        "description": "Nom complet utilisé dans les emails, SMS et messages vocaux.",
        "section": "Informations générales",
        "tab": "salon",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "SALON_NAME_SHORT",
        "label": "Nom court (SMS)",
        "description": "Version courte sans accents pour les SMS GSM-7 (ex: Maison Eclat).",
        "section": "Informations générales",
        "tab": "salon",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "SALON_ADDRESS_SHORT",
        "label": "Adresse courte",
        "description": "Adresse condensée affichée dans le pied de SMS (ex: 42 r. des Petits-Champs, Paris 2e).",
        "section": "Informations générales",
        "tab": "salon",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "OWNER_PHONE",
        "label": "Téléphone du gérant",
        "description": "Numéro qui reçoit les alertes SMS (nouveaux RDV, annulations).",
        "section": "Informations générales",
        "tab": "salon",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "SALON_EMAIL",
        "label": "Email du gérant",
        "description": "Adresse qui reçoit les alertes email (nouveaux RDV, demandes de rappel).",
        "section": "Informations générales",
        "tab": "salon",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "SALON_EMAIL_FROM",
        "label": "Adresse expéditeur email",
        "description": "Adresse affichée comme expéditeur dans les emails de notification.",
        "section": "Informations générales",
        "tab": "salon",
        "type": "str",
        "is_sensitive": False,
    },
    # ── Horaires (stored as JSON, rendered by custom UI) ─────
    {
        "key": "OPENING_HOURS",
        "label": "Horaires d'ouverture",
        "description": "JSON horaires par jour de la semaine.",
        "section": "Horaires",
        "tab": "salon-data",
        "type": "str",
        "is_sensitive": False,
    },

    # ══════════════════════════════════════════
    # TAB: vocal  (agent vocal & configuration)
    # ══════════════════════════════════════════

    # ── Agent vocal ──────────────────────────────────────────
    {
        "key": "AGENT_NAME",
        "label": "Nom de l'agent vocal",
        "description": "Prénom affiché dans le system prompt du LLM (ex: Marine).",
        "section": "Agent vocal",
        "tab": "vocal",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "AGENT_DESCRIPTION",
        "label": "Rôle de l'agent vocal",
        "description": "Description du rôle utilisée dans le system prompt (ex: réceptionniste IA).",
        "section": "Agent vocal",
        "tab": "vocal",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "GREETING_TEXT",
        "label": "Message d'accueil",
        "description": "Texte prononcé à l'arrivée de l'appel. Toute modification recrée le cache audio ElevenLabs.",
        "section": "Agent vocal",
        "tab": "vocal",
        "type": "str",
        "multiline": True,
        "is_sensitive": False,
    },
    {
        "key": "GOODBYE_TEXT",
        "label": "Message d'au revoir",
        "description": "Texte prononcé lors du raccroché.",
        "section": "Agent vocal",
        "tab": "vocal",
        "type": "str",
        "multiline": True,
        "is_sensitive": False,
    },
    {
        "key": "VOICEMAIL_TEXT",
        "label": "Message messagerie vocale",
        "description": "Texte prononcé avant que le client enregistre un message vocal.",
        "section": "Agent vocal",
        "tab": "vocal",
        "type": "str",
        "multiline": True,
        "is_sensitive": False,
    },
    # ── Twilio (SMS & Voix) ──────────────────────────────────
    {
        "key": "TWILIO_PHONE_NUMBER",
        "label": "N° Twilio du salon",
        "description": "Numéro utilisé pour l'envoi des SMS et la réception des appels (ex : +33XXXXXXXXX).",
        "section": "Twilio — SMS & Voix",
        "tab": "vocal",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "TWILIO_TRANSFER_NUMBER",
        "label": "N° de renvoi d'appel",
        "description": "Numéro vers lequel l'agent vocal peut transférer l'appel si besoin.",
        "section": "Twilio — SMS & Voix",
        "tab": "vocal",
        "type": "str",
        "is_sensitive": False,
    },
    {
        "key": "TWILIO_ACCOUNT_SID",
        "label": "Account SID",
        "description": "Identifiant de compte Twilio (format : ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx).",
        "section": "Twilio — SMS & Voix",
        "tab": "vocal",
        "type": "str",
        "is_sensitive": True,
    },
    {
        "key": "TWILIO_AUTH_TOKEN",
        "label": "Auth Token",
        "description": "Token d'authentification Twilio pour la signature des webhooks.",
        "section": "Twilio — SMS & Voix",
        "tab": "vocal",
        "type": "str",
        "is_sensitive": True,
    },
    # ── ElevenLabs (Synthèse vocale) ─────────────────────────
    {
        "key": "ELEVENLABS_API_KEY",
        "label": "Clé API",
        "description": "Clé API ElevenLabs pour la synthèse vocale de l'agent téléphonique.",
        "section": "ElevenLabs — Synthèse vocale",
        "tab": "vocal",
        "type": "str",
        "is_sensitive": True,
    },
    {
        "key": "ELEVENLABS_VOICE_ID",
        "label": "ID de voix",
        "description": "Identifiant de la voix ElevenLabs utilisée par l'agent (ex : lvQdCgwZfBuOzxyV5pxu).",
        "section": "ElevenLabs — Synthèse vocale",
        "tab": "vocal",
        "type": "str",
        "is_sensitive": False,
    },
    # ── Consentement RGPD vocal ──────────────────────────────
    {
        "key": "CONSENT_ENABLED",
        "label": "Consentement RGPD vocal activé",
        "description": "Joue un message de consentement CNIL avant l'accueil vocal. Recommandé pour tout appel enregistré.",
        "section": "Consentement RGPD vocal",
        "tab": "vocal",
        "type": "bool",
        "is_sensitive": False,
    },
    {
        "key": "CONSENT_TEXT",
        "label": "Message de consentement",
        "description": "Texte prononcé en début d'appel. Doit mentionner l'IA, l'enregistrement et la possibilité d'appuyer sur 1 pour refuser.",
        "section": "Consentement RGPD vocal",
        "tab": "vocal",
        "type": "str",
        "multiline": True,
        "is_sensitive": False,
    },
    {
        "key": "CONSENT_REFUSAL_TEXT",
        "label": "Message de refus",
        "description": "Texte prononcé lorsque le client appuie sur 1 pour refuser l'enregistrement. L'appel est raccroché après.",
        "section": "Consentement RGPD vocal",
        "tab": "vocal",
        "type": "str",
        "multiline": True,
        "is_sensitive": False,
    },
    # ── RGPD & Rétention ─────────────────────────────────────
    {
        "key": "SESSION_RETENTION_DAYS",
        "label": "Rétention sessions vocales (jours)",
        "description": "Suppression automatique des sessions et transcripts plus anciens que cette durée.",
        "section": "RGPD & Rétention",
        "tab": "vocal",
        "type": "int",
        "is_sensitive": False,
    },
    {
        "key": "CALLBACK_RETENTION_DAYS",
        "label": "Rétention demandes de rappel (jours)",
        "description": "Suppression automatique des demandes de rappel résolues plus anciennes que cette durée.",
        "section": "RGPD & Rétention",
        "tab": "vocal",
        "type": "int",
        "is_sensitive": False,
    },
    {
        "key": "PURGE_HOUR",
        "label": "Heure de purge RGPD",
        "description": "Heure à laquelle la purge automatique s'exécute chaque nuit (0-23, heure locale).",
        "section": "RGPD & Rétention",
        "tab": "vocal",
        "type": "int",
        "is_sensitive": False,
    },
    # ── Sécurité ─────────────────────────────────────────────
    {
        "key": "VOICE_API_KEY",
        "label": "Token admin dashboard",
        "description": "Clé requise pour accéder au dashboard. Laissez vide pour désactiver l'authentification.",
        "section": "Sécurité",
        "tab": "vocal",
        "type": "str",
        "is_sensitive": True,
    },

    # ══════════════════════════════════════════
    # TAB: sms  (notifications & templates SMS)
    # ══════════════════════════════════════════

    # ── Rappels SMS ──────────────────────────────────────────
    {
        "key": "REMINDER_ENABLED",
        "label": "Rappels SMS J-1 activés",
        "description": "Envoie automatiquement un SMS de rappel la veille de chaque rendez-vous.",
        "section": "Rappels SMS",
        "tab": "sms",
        "type": "bool",
        "is_sensitive": False,
    },
    {
        "key": "REMINDER_HOUR",
        "label": "Heure d'envoi des rappels",
        "description": "Heure à laquelle les rappels SMS sont envoyés (0-23, heure locale).",
        "section": "Rappels SMS",
        "tab": "sms",
        "type": "int",
        "is_sensitive": False,
    },
    # ── Templates SMS ────────────────────────────────────────
    {
        "key": "SMS_REMINDER_TEMPLATE",
        "label": "Template rappel J-1",
        "description": "Variables disponibles : {client_name}, {date}, {heure}, {prestation}, {coiffeur}",
        "section": "Templates SMS",
        "tab": "sms",
        "type": "str",
        "multiline": True,
        "is_sensitive": False,
    },
    {
        "key": "SMS_CONFIRMATION_TEMPLATE",
        "label": "Template confirmation RDV",
        "description": "Variables disponibles : {client_name}, {date}, {heure}, {prestation}, {coiffeur}",
        "section": "Templates SMS",
        "tab": "sms",
        "type": "str",
        "multiline": True,
        "is_sensitive": False,
    },
    {
        "key": "SMS_CANCEL_TEMPLATE",
        "label": "Template annulation",
        "description": "Variables disponibles : {client_name}, {date}, {heure}",
        "section": "Templates SMS",
        "tab": "sms",
        "type": "str",
        "multiline": True,
        "is_sensitive": False,
    },
    # ── Resend (Email) ───────────────────────────────────────
    {
        "key": "RESEND_API_KEY",
        "label": "Clé API Resend",
        "description": "Clé API Resend.com (re_xxxxxxxx…) pour l'envoi des emails de notification.",
        "section": "Resend — Email",
        "tab": "sms",
        "type": "str",
        "is_sensitive": True,
    },
]

_VALID_KEYS: frozenset[str] = frozenset(m["key"] for m in SETTINGS_METADATA)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _apply_to_settings_obj(target: Any, key: str, raw: str) -> None:
    """Cast `raw` to the expected Python type and set it on `target` settings obj."""
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
        setattr(target, key, value)
    except (ValueError, TypeError):
        pass  # ignore malformed DB values — env var default remains


def _mask(value: str) -> str:
    """Return a masked representation: first 4 chars + ••••••"""
    if not value:
        return ""
    return value[:4] + "••••••" if len(value) > 4 else "••••••"


# ── Public API ───────────────────────────────────────────────────────────────

async def load_settings_from_db(session: AsyncSession, tenant_id: int) -> None:
    """
    Load SalonSetting rows for `tenant_id` and cache a per-tenant Settings copy.
    Also patches the global `settings` object for backward compatibility when
    the default tenant is loaded.
    """
    result = await session.execute(
        select(SalonSetting).where(SalonSetting.tenant_id == tenant_id)
    )
    rows = result.scalars().all()

    # Build per-tenant settings copy from global defaults
    tenant_cfg = settings.model_copy()
    for row in rows:
        if row.key in _VALID_KEYS and row.value is not None:
            _apply_to_settings_obj(tenant_cfg, row.key, row.value)

    _tenant_settings[tenant_id] = tenant_cfg

    # Also patch global settings for the default tenant (backward compat)
    for row in rows:
        if row.key in _VALID_KEYS and row.value is not None:
            _apply_to_settings_obj(settings, row.key, row.value)


async def update_settings(
    session: AsyncSession,
    tenant_id: int,
    updates: dict[str, str],
) -> None:
    """
    Upsert DB rows for `updates` (key→raw_string) scoped to `tenant_id`,
    then refresh the in-memory cache for that tenant.
    Unknown keys are silently ignored.
    """
    for key, raw in updates.items():
        if key not in _VALID_KEYS:
            continue
        row = SalonSetting(tenant_id=tenant_id, key=key, value=raw)
        await session.merge(row)
    await session.commit()

    # Refresh cache for this tenant
    await load_settings_from_db(session, tenant_id)


def get_settings_with_values(tenant_id: int) -> list[dict[str, Any]]:
    """
    Return SETTINGS_METADATA enriched with the current effective value for `tenant_id`.
    Sensitive values are masked unless empty.
    """
    tenant_cfg = get_tenant_settings(tenant_id)
    result = []
    for meta in SETTINGS_METADATA:
        key = meta["key"]
        raw: Any = getattr(tenant_cfg, key, "")
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
