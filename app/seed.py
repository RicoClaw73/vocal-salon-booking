"""
Seed the database from data/normalized/*.json files.

Idempotent: safe to re-run – uses merge (upsert) semantics for the default
tenant. For non-default tenants, create_tenant.py passes an id_prefix so
that service and employee IDs remain globally unique (e.g. "salon-dupont_coupe_femme_court").
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR
from app.models import Employee, EmployeeCompetency, Service

logger = logging.getLogger(__name__)

# Chemical services get a 15-min post-service buffer (vs 10 standard).
# Loaded from scheduling-rules.json at seed time.
_CHEMICAL_IDS: set[str] = set()


def _load_json(filename: str) -> dict:
    path = DATA_DIR / filename
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_chemical_ids() -> set[str]:
    """Read the list of chemical service IDs from scheduling-rules.json."""
    rules = _load_json("scheduling-rules.json")
    return set(
        rules.get("regles_temporelles", {})
        .get("buffer_entre_rdv", {})
        .get("services_chimiques", [])
    )


async def seed_services(
    session: AsyncSession,
    tenant_id: int,
    id_prefix: str = "",
) -> int:
    """Insert/update services from services.json for `tenant_id`. Returns count."""
    data = _load_json("services.json")
    chemical_ids = _load_chemical_ids()
    count = 0
    for cat in data.get("categories", []):
        for svc in cat.get("services", []):
            svc_id = f"{id_prefix}{svc['id']}" if id_prefix else svc["id"]
            service = Service(
                id=svc_id,
                tenant_id=tenant_id,
                category_id=cat["id"],
                category_label=cat["label"],
                label=svc["label"],
                description=svc.get("description", ""),
                prix_eur=svc["prix_eur"],
                duree_min=svc["duree_min"],
                genre=svc.get("genre", "mixte"),
                longueur=svc.get("longueur", "tout"),
                is_chemical=svc["id"] in chemical_ids,
                notes=svc.get("notes"),
            )
            await session.merge(service)
            count += 1
    await session.commit()
    logger.info("Seeded %d services for tenant_id=%d", count, tenant_id)
    return count


async def seed_employees(
    session: AsyncSession,
    tenant_id: int,
    id_prefix: str = "",
) -> int:
    """Insert/update employees + competencies for `tenant_id`. Returns count."""
    data = _load_json("employees.json")
    count = 0
    for emp_data in data.get("employees", []):
        emp_id = f"{id_prefix}{emp_data['id']}" if id_prefix else emp_data["id"]
        emp = Employee(
            id=emp_id,
            tenant_id=tenant_id,
            prenom=emp_data["prenom"],
            nom=emp_data["nom"],
            role=emp_data["role"],
            anciennete_ans=emp_data["anciennete_ans"],
            niveau=emp_data["niveau"],
            horaires_json=json.dumps(emp_data["horaires"], ensure_ascii=False),
            notes=emp_data.get("notes"),
        )
        await session.merge(emp)

        # Competencies – delete old ones for this (tenant, employee), re-insert
        existing = await session.execute(
            select(EmployeeCompetency).where(
                EmployeeCompetency.tenant_id == tenant_id,
                EmployeeCompetency.employee_id == emp_id,
            )
        )
        for row in existing.scalars():
            await session.delete(row)
        await session.flush()

        for svc_id in emp_data.get("competences", []):
            prefixed_svc_id = f"{id_prefix}{svc_id}" if id_prefix else svc_id
            comp = EmployeeCompetency(
                tenant_id=tenant_id,
                employee_id=emp_id,
                service_id=prefixed_svc_id,
            )
            await session.merge(comp)

        count += 1
    await session.commit()
    logger.info("Seeded %d employees with competencies for tenant_id=%d", count, tenant_id)
    return count


async def seed_all(
    session: AsyncSession,
    tenant_id: int,
    id_prefix: str = "",
) -> dict:
    """Run all seeders for `tenant_id`. Returns summary dict."""
    svc_count = await seed_services(session, tenant_id, id_prefix)
    emp_count = await seed_employees(session, tenant_id, id_prefix)
    return {"services": svc_count, "employees": emp_count}
