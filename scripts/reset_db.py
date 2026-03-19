"""
Dev DB reset utility.

Drops all tables and recreates them from the current SQLAlchemy models.
Also creates the default tenant and seeds it with the default catalogue.

WARNING: destroys all data.  Dev use only.

Usage:
    python scripts/reset_db.py [--confirm]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import async_engine, async_session
from app.models import Base
from app.seed import seed_all
from app.settings_service import load_settings_from_db
from app.tenant_service import ensure_default_tenant


async def _reset() -> None:
    print("Dropping all tables...")
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    print("Creating all tables...")
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print("Creating default tenant and seeding...")
    async with async_session() as db:
        tenant = await ensure_default_tenant(db, default_api_key=settings.VOICE_API_KEY)
        counts = await seed_all(db, tenant_id=tenant.id)
        await load_settings_from_db(db, tenant_id=tenant.id)
        await db.commit()

    print(f"✓ Done. Default tenant: slug='{tenant.slug}', id={tenant.id}")
    print(f"  services={counts.get('services', 0)}, employees={counts.get('employees', 0)}")


def main() -> None:
    if "--confirm" not in sys.argv:
        print("This will DESTROY all data.")
        print("Run with --confirm to proceed.")
        sys.exit(1)
    asyncio.run(_reset())


if __name__ == "__main__":
    main()
