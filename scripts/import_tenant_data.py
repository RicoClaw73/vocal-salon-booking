"""
Import services and/or employees for an existing tenant from CSV files.

Usage:
    python scripts/import_tenant_data.py --slug <slug> --services services.csv
    python scripts/import_tenant_data.py --slug <slug> --employees employees.csv
    python scripts/import_tenant_data.py --slug <slug> --services s.csv --employees e.csv

CSV formats
-----------
services.csv columns (order matters, header required):
    id, category_id, category_label, label, description,
    prix_eur, duree_min, genre, longueur, is_chemical, notes

employees.csv columns (order matters, header required):
    id, prenom, nom, role, anciennete_ans, niveau, competences,
    mardi_debut, mardi_fin, mardi_pause_debut, mardi_pause_fin,
    mercredi_debut, mercredi_fin, mercredi_pause_debut, mercredi_pause_fin,
    jeudi_debut, jeudi_fin, jeudi_pause_debut, jeudi_pause_fin,
    vendredi_debut, vendredi_fin, vendredi_pause_debut, vendredi_pause_fin,
    samedi_debut, samedi_fin,
    notes

Notes:
- IDs in the CSV should NOT include the tenant prefix — the script adds `{slug}_` automatically.
- `competences` is a semicolon-separated list of service IDs (without prefix).
- Empty debut/fin = day off (the day is omitted from the schedule JSON).
- `is_chemical`: 1/true/yes = True; anything else = False.
- Existing rows are upserted (safe to re-run).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

# Ensure project root is on sys.path when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.database import async_session
from app.models import Employee, EmployeeCompetency, Service
from app.tenant_service import get_tenant_by_slug


# ── helpers ──────────────────────────────────────────────────


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "oui")


def _parse_schedule(row: dict) -> dict:
    """
    Build the horaires JSON dict from per-day CSV columns.
    Days with empty `debut` or `fin` are considered off and excluded.
    """
    days = ["mardi", "mercredi", "jeudi", "vendredi", "samedi"]
    horaires: dict = {}
    for day in days:
        debut = row.get(f"{day}_debut", "").strip()
        fin = row.get(f"{day}_fin", "").strip()
        if not debut or not fin:
            continue  # day off
        entry: dict = {"debut": debut, "fin": fin}
        if day != "samedi":
            pause_debut = row.get(f"{day}_pause_debut", "").strip()
            pause_fin = row.get(f"{day}_pause_fin", "").strip()
            if pause_debut and pause_fin:
                entry["pause"] = {"debut": pause_debut, "fin": pause_fin}
        horaires[day] = entry
    return horaires


# ── importers ────────────────────────────────────────────────


async def import_services(session, tenant_id: int, id_prefix: str, csv_path: Path) -> int:
    count = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_id = row["id"].strip()
            if not raw_id:
                continue
            svc_id = f"{id_prefix}{raw_id}"
            service = Service(
                id=svc_id,
                tenant_id=tenant_id,
                category_id=row.get("category_id", "").strip(),
                category_label=row.get("category_label", "").strip(),
                label=row.get("label", "").strip(),
                description=row.get("description", "").strip(),
                prix_eur=float(row.get("prix_eur", 0) or 0),
                duree_min=int(row.get("duree_min", 30) or 30),
                genre=row.get("genre", "mixte").strip() or "mixte",
                longueur=row.get("longueur", "tout").strip() or "tout",
                is_chemical=_parse_bool(row.get("is_chemical", "")),
                notes=row.get("notes", "").strip() or None,
            )
            await session.merge(service)
            count += 1
    await session.commit()
    return count


async def import_employees(session, tenant_id: int, id_prefix: str, csv_path: Path) -> int:
    count = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_id = row["id"].strip()
            if not raw_id:
                continue
            emp_id = f"{id_prefix}{raw_id}"
            horaires = _parse_schedule(row)

            emp = Employee(
                id=emp_id,
                tenant_id=tenant_id,
                prenom=row.get("prenom", "").strip(),
                nom=row.get("nom", "").strip(),
                role=row.get("role", "").strip(),
                anciennete_ans=int(row.get("anciennete_ans", 0) or 0),
                niveau=row.get("niveau", "confirme").strip() or "confirme",
                horaires_json=json.dumps(horaires, ensure_ascii=False),
                notes=row.get("notes", "").strip() or None,
            )
            await session.merge(emp)

            # Competencies: delete existing for (tenant, employee), then re-insert
            existing = await session.execute(
                select(EmployeeCompetency).where(
                    EmployeeCompetency.tenant_id == tenant_id,
                    EmployeeCompetency.employee_id == emp_id,
                )
            )
            for comp_row in existing.scalars():
                await session.delete(comp_row)
            await session.flush()

            raw_competences = row.get("competences", "").strip()
            if raw_competences:
                for svc_raw in raw_competences.split(";"):
                    svc_raw = svc_raw.strip()
                    if not svc_raw:
                        continue
                    comp = EmployeeCompetency(
                        tenant_id=tenant_id,
                        employee_id=emp_id,
                        service_id=f"{id_prefix}{svc_raw}",
                    )
                    await session.merge(comp)

            count += 1
    await session.commit()
    return count


# ── main ─────────────────────────────────────────────────────


async def _main(slug: str, services_path: Path | None, employees_path: Path | None) -> None:
    async with async_session() as db:
        tenant = await get_tenant_by_slug(db, slug)
        if not tenant:
            print(f"[ERREUR] Aucun tenant trouvé avec le slug '{slug}'.")
            print("         Créez-le d'abord avec : python scripts/create_tenant.py --slug <slug> --name <name>")
            sys.exit(1)

        id_prefix = f"{slug}_"
        print(f"Tenant : {tenant.name!r} (id={tenant.id}, slug={slug})")
        print(f"Préfixe IDs : {id_prefix!r}")
        print()

        if services_path:
            print(f"Import services depuis {services_path} …")
            count = await import_services(db, tenant.id, id_prefix, services_path)
            print(f"  ✓ {count} service(s) importé(s)")

        if employees_path:
            print(f"Import employés depuis {employees_path} …")
            count = await import_employees(db, tenant.id, id_prefix, employees_path)
            print(f"  ✓ {count} employé(s) importé(s)")

        if not services_path and not employees_path:
            print("[INFO] Aucun fichier CSV fourni. Utilisez --services et/ou --employees.")
            sys.exit(0)

        print()
        print("Import terminé.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import services/employees for an existing tenant.")
    parser.add_argument("--slug", required=True, help="Slug du tenant existant (e.g. 'salon-dupont')")
    parser.add_argument("--services", metavar="FILE", help="CSV des services à importer")
    parser.add_argument("--employees", metavar="FILE", help="CSV des employés à importer")
    args = parser.parse_args()

    services_path = Path(args.services) if args.services else None
    employees_path = Path(args.employees) if args.employees else None

    if services_path and not services_path.exists():
        print(f"[ERREUR] Fichier introuvable : {services_path}")
        sys.exit(1)
    if employees_path and not employees_path.exists():
        print(f"[ERREUR] Fichier introuvable : {employees_path}")
        sys.exit(1)

    asyncio.run(_main(slug=args.slug, services_path=services_path, employees_path=employees_path))


if __name__ == "__main__":
    main()
