"""
Employee endpoints.

GET /employees           – list all employees
GET /employees/{id}      – single employee detail
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Employee
from app.schemas import EmployeeSlim

router = APIRouter(prefix="/employees", tags=["employees"])


@router.get("", response_model=list[EmployeeSlim])
async def list_employees(
    db: AsyncSession = Depends(get_db),
) -> list[EmployeeSlim]:
    """Return all employees (slim view)."""
    result = await db.execute(select(Employee).order_by(Employee.prenom))
    employees = list(result.scalars().all())
    return [EmployeeSlim.model_validate(e) for e in employees]


@router.get("/{employee_id}", response_model=EmployeeSlim)
async def get_employee(
    employee_id: str,
    db: AsyncSession = Depends(get_db),
) -> EmployeeSlim:
    """Return a single employee by ID."""
    employee = await db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail=f"Employé '{employee_id}' introuvable.")
    return EmployeeSlim.model_validate(employee)
