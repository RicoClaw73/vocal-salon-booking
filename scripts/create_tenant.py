"""
Tenant provisioning CLI.

Usage:
    python scripts/create_tenant.py --slug mysalon --name "Mon Salon" [--api-key <key>]
    python scripts/create_tenant.py --slug mysalon --name "Mon Salon" --agent-name Léa

Creates a new tenant row in the database, seeds it with default services
and employees, and pre-fills the core salon_settings (name, greeting texts,
agent name) so the agent never falls back to the "Maison Éclat" defaults.

Non-default tenants get service/employee IDs prefixed with `{slug}_` to avoid
collisions with the default tenant's IDs (which use the original seed IDs).
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import unicodedata
from pathlib import Path

# Ensure the project root is on sys.path when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.database import async_session
from app.models import SalonSetting, Tenant
from app.seed import seed_all
from app.tenant_service import create_tenant, get_tenant_by_slug


def _strip_accents(text: str) -> str:
    """Return text with accents removed (GSM-7 safe for SMS)."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def _build_initial_settings(
    tenant_id: int,
    name: str,
    agent_name: str,
) -> list[SalonSetting]:
    """Build the initial SalonSetting rows derived from the tenant name."""
    agent_desc = "réceptionniste IA"
    name_short = _strip_accents(name)

    greeting = (
        f"Bonjour et bienvenue chez {name} ! "
        f"Je suis {agent_name}, {agent_desc} du salon. "
        f"Je peux vous aider à prendre rendez-vous, modifier ou annuler une réservation. "
        f"Comment puis-je vous aider ?"
    )
    goodbye = f"Merci d'avoir appelé {name}. À bientôt !"
    voicemail = (
        "Je vous passe en messagerie vocale. "
        "Veuillez laisser votre message après le signal sonore. "
        "Le salon vous rappellera dès que possible."
    )

    initial: dict[str, str] = {
        "SALON_NAME": name,
        "SALON_NAME_SHORT": name_short,
        "AGENT_NAME": agent_name,
        "AGENT_DESCRIPTION": agent_desc,
        "GREETING_TEXT": greeting,
        "GOODBYE_TEXT": goodbye,
        "VOICEMAIL_TEXT": voicemail,
    }
    return [
        SalonSetting(tenant_id=tenant_id, key=k, value=v)
        for k, v in initial.items()
    ]


async def _main(slug: str, name: str, api_key: str, agent_name: str) -> None:
    async with async_session() as db:
        # Check uniqueness
        existing = await get_tenant_by_slug(db, slug)
        if existing:
            print(f"[ERROR] Un tenant avec le slug '{slug}' existe déjà (id={existing.id}).")
            sys.exit(1)

        # Also check api_key uniqueness
        from sqlalchemy import select as _select
        result = await db.execute(_select(Tenant).where(Tenant.api_key == api_key))
        if result.scalars().first():
            print(f"[ERROR] La clé API '{api_key}' est déjà utilisée.")
            sys.exit(1)

        tenant = await create_tenant(db, slug=slug, name=name, api_key=api_key)
        await db.flush()

        # Seed services + employees with prefixed IDs
        id_prefix = f"{slug}_"
        counts = await seed_all(db, tenant_id=tenant.id, id_prefix=id_prefix)

        # Pre-fill core salon_settings so the agent doesn't fall back to defaults
        settings_rows = _build_initial_settings(
            tenant_id=tenant.id, name=name, agent_name=agent_name
        )
        for row in settings_rows:
            await db.merge(row)
        await db.commit()

        print(f"✓ Tenant créé:")
        print(f"  id        : {tenant.id}")
        print(f"  slug      : {tenant.slug}")
        print(f"  name      : {tenant.name}")
        print(f"  api_key   : {tenant.api_key}")
        print(f"  services  : {counts.get('services', 0)} seeded")
        print(f"  employees : {counts.get('employees', 0)} seeded")
        print(f"  settings  : {len(settings_rows)} pré-remplis")
        print()
        print("Configurez X-API-Key dans vos requêtes avec la clé ci-dessus.")
        print(f"Pour Twilio, ajoutez ?tenant={slug} à l'URL du webhook.")
        print()
        print("Pensez à compléter dans le dashboard :")
        print("  - Onglet Gérant : adresse, téléphone gérant, email, rappels SMS")
        print("  - Onglet Technique : Twilio SID/Token/N°, ElevenLabs clé/voice ID")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new tenant.")
    parser.add_argument("--slug", required=True, help="URL-safe identifier (e.g. 'salon-paris')")
    parser.add_argument("--name", required=True, help="Human-readable salon name")
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key (generated randomly if omitted)",
    )
    parser.add_argument(
        "--agent-name",
        default="Marine",
        help="First name of the voice agent (default: Marine)",
    )
    args = parser.parse_args()

    api_key = args.api_key or secrets.token_urlsafe(32)
    asyncio.run(
        _main(
            slug=args.slug,
            name=args.name,
            api_key=api_key,
            agent_name=args.agent_name,
        )
    )


if __name__ == "__main__":
    main()
