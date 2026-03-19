"""
Employee endpoints.

GET    /employees           – list all employees
GET    /employees/{id}      – single employee detail
POST   /employees           – create employee
PATCH  /employees/{id}      – update employee
DELETE /employees/{id}      – delete employee
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_tenant_from_slug
from app.database import get_db
from app.models import Employee, Tenant
from app.schemas import EmployeeCreate, EmployeeSlim, EmployeeUpdate

router = APIRouter(prefix="/employees", tags=["employees"])


@router.get("", response_model=list[EmployeeSlim])
async def list_employees(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> list[EmployeeSlim]:
    """Return all employees (slim view)."""
    result = await db.execute(
        select(Employee)
        .where(Employee.tenant_id == tenant.id)
        .order_by(Employee.prenom)
    )
    employees = list(result.scalars().all())
    return [EmployeeSlim.model_validate(e) for e in employees]


@router.get("/{employee_id}", response_model=EmployeeSlim)
async def get_employee(
    employee_id: str,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> EmployeeSlim:
    """Return a single employee by ID."""
    result = await db.execute(
        select(Employee).where(
            Employee.id == employee_id, Employee.tenant_id == tenant.id
        )
    )
    employee = result.scalars().first()
    if not employee:
        raise HTTPException(status_code=404, detail=f"Employé '{employee_id}' introuvable.")
    return EmployeeSlim.model_validate(employee)


@router.post("", response_model=EmployeeSlim, status_code=201)
async def create_employee(
    data: EmployeeCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> EmployeeSlim:
    """Create a new employee."""
    employee = Employee(
        id=uuid4().hex[:12],
        tenant_id=tenant.id,
        prenom=data.prenom,
        nom=data.nom,
        role=data.role,
        horaires_json=data.horaires_json,
        notes=data.notes,
        anciennete_ans=0,
        niveau="junior",
    )
    db.add(employee)
    await db.commit()
    await db.refresh(employee)
    return EmployeeSlim.model_validate(employee)


@router.patch("/{employee_id}", response_model=EmployeeSlim)
async def update_employee(
    employee_id: str,
    data: EmployeeUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> EmployeeSlim:
    """Update an employee's fields."""
    result = await db.execute(
        select(Employee).where(
            Employee.id == employee_id, Employee.tenant_id == tenant.id
        )
    )
    employee = result.scalars().first()
    if not employee:
        raise HTTPException(status_code=404, detail=f"Employé '{employee_id}' introuvable.")
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(employee, field, value)
    await db.commit()
    await db.refresh(employee)
    return EmployeeSlim.model_validate(employee)


@router.delete("/{employee_id}")
async def delete_employee(
    employee_id: str,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> dict:
    """Delete an employee."""
    result = await db.execute(
        select(Employee).where(
            Employee.id == employee_id, Employee.tenant_id == tenant.id
        )
    )
    employee = result.scalars().first()
    if not employee:
        raise HTTPException(status_code=404, detail=f"Employé '{employee_id}' introuvable.")
    await db.delete(employee)
    await db.commit()
    return {"deleted": employee_id}
