"""
Seed the database from data/normalized/*.json files.

Idempotent: safe to re-run – uses merge (upsert) semantics.
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


async def seed_services(session: AsyncSession) -> int:
    """Insert/update services from services.json. Returns count."""
    data = _load_json("services.json")
    chemical_ids = _load_chemical_ids()
    count = 0
    for cat in data.get("categories", []):
        for svc in cat.get("services", []):
            service = Service(
                id=svc["id"],
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
    logger.info("Seeded %d services", count)
    return count


async def seed_employees(session: AsyncSession) -> int:
    """Insert/update employees + competencies from employees.json. Returns count."""
    data = _load_json("employees.json")
    count = 0
    for emp_data in data.get("employees", []):
        emp = Employee(
            id=emp_data["id"],
            prenom=emp_data["prenom"],
            nom=emp_data["nom"],
            role=emp_data["role"],
            anciennete_ans=emp_data["anciennete_ans"],
            niveau=emp_data["niveau"],
            horaires_json=json.dumps(emp_data["horaires"], ensure_ascii=False),
            notes=emp_data.get("notes"),
        )
        await session.merge(emp)

        # Competencies – delete old, re-insert
        existing = await session.execute(
            select(EmployeeCompetency).where(
                EmployeeCompetency.employee_id == emp_data["id"]
            )
        )
        for row in existing.scalars():
            await session.delete(row)
        await session.flush()

        for svc_id in emp_data.get("competences", []):
            comp = EmployeeCompetency(
                employee_id=emp_data["id"],
                service_id=svc_id,
            )
            await session.merge(comp)

        count += 1
    await session.commit()
    logger.info("Seeded %d employees with competencies", count)
    return count


async def seed_all(session: AsyncSession) -> dict:
    """Run all seeders. Returns summary dict."""
    svc_count = await seed_services(session)
    emp_count = await seed_employees(session)
    return {"services": svc_count, "employees": emp_count}
