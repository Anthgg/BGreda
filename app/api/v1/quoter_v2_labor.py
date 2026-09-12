"""Superficie HTTP de la mano de obra del Cotizador V2.

Tres grupos de rutas, separados porque son tres cosas distintas:

- `/quoter-v2/workers` y `/quoter-v2/techniques` — los maestros. Son politica
  del taller: afectan a lo que se cotice DESPUES, nunca a lo ya cotizado.
- `/quotations-v2/{id}/labor` — las tareas de una cotizacion, con quien, que y
  a que tarifa ya congelados.
- `/quotations-v2/{id}/illustration` y `/planning` — la ilustracion y los dias
  efectivos, que son uno por cotizacion.

## Por que todo esto es de administracion

Porque el jornal de una persona es informacion de su remuneracion. El resto del
Cotizador V2 ya era solo de administracion desde 010A, y abrir estos maestros a
todo el taller para «poder seleccionar» expondria cuanto cobra cada companero.
Quien cotiza en V2 es administrador, asi que la separacion que pide la fase
—cotizar no autoriza a cambiar maestros— se cumple sin necesidad de ensanchar
quien ve los sueldos.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.api.deps import AdminUserDep, DbSessionDep, V2LaborServiceDep
from app.core.quoter_v2_labor import units_per_hour
from app.models.quoter_v2 import V2Quotation
from app.models.quoter_v2_labor import V2QuotationLabor, V2Technique, V2Worker
from app.schemas.quoter_v2_labor import (
    V2IllustrationIn,
    V2IllustrationOut,
    V2LaborIn,
    V2LaborOut,
    V2LaborPage,
    V2PlanningIn,
    V2TechniqueCreateIn,
    V2TechniqueOut,
    V2TechniquePage,
    V2TechniqueUpdateIn,
    V2WorkerCreateIn,
    V2WorkerLoadOut,
    V2WorkerOut,
    V2WorkerPage,
    V2WorkerUpdateIn,
)

router = APIRouter(tags=["cotizador-v2"])


def _worker_out(worker: V2Worker, jornada: Decimal, tarifa: Decimal) -> V2WorkerOut:
    return V2WorkerOut(
        id=worker.id,
        name=worker.name,
        worker_type=worker.worker_type,
        active=worker.active,
        daily_rate=worker.daily_rate,
        workday_hours=worker.workday_hours,
        effective_workday_hours=jornada,
        hourly_rate=tarifa,
        notes=worker.notes,
        version=worker.version,
    )


def _technique_out(tecnica: V2Technique, jornada: Decimal) -> V2TechniqueOut:
    return V2TechniqueOut(
        id=tecnica.id,
        code=tecnica.code,
        name=tecnica.name,
        active=tecnica.active,
        default_capacity_per_workday=tecnica.default_capacity_per_workday,
        unit=tecnica.unit,
        requires_glaze=tecnica.requires_glaze,
        units_per_hour=units_per_hour(tecnica.default_capacity_per_workday, jornada),
        notes=tecnica.notes,
        version=tecnica.version,
    )


def _labor_out(fila: V2QuotationLabor, warnings: list[str]) -> V2LaborOut:
    return V2LaborOut(
        id=fila.id,
        sort_order=fila.sort_order,
        v2_quotation_product_id=fila.v2_quotation_product_id,
        worker_id=fila.worker_id,
        worker_name=fila.worker_name_snapshot,
        worker_type=fila.worker_type_snapshot,
        daily_rate=fila.daily_rate_snapshot,
        workday_hours=fila.workday_hours_snapshot,
        hourly_rate=fila.hourly_rate_snapshot,
        rate_overridden=fila.rate_overridden,
        technique_id=fila.technique_id,
        technique_name=fila.technique_name_snapshot,
        technique_unit=fila.technique_unit_snapshot,
        standard_capacity=fila.standard_capacity_snapshot,
        quantity=fila.quantity,
        calculated_hours=fila.calculated_hours,
        final_hours=fila.final_hours,
        hours_overridden=fila.hours_overridden,
        is_additional_personnel=fila.is_additional_personnel,
        labor_cost=fila.labor_cost,
        warnings=warnings,
    )


def _illustration_out(quotation: V2Quotation) -> V2IllustrationOut:
    return V2IllustrationOut(
        enabled=quotation.illustration_enabled,
        quantity=quotation.illustration_quantity,
        notes=quotation.illustration_notes,
        daily_rate=quotation.illustration_daily_rate_snapshot,
        workday_hours=quotation.illustration_workday_hours_snapshot,
        capacity_per_workday=quotation.illustration_capacity_snapshot,
        hourly_rate=quotation.illustration_hourly_rate_snapshot,
        hours=quotation.illustration_hours,
        cost=quotation.illustration_cost,
    )


# ---------------------------------------------------------------------------
# Maestro de trabajadores
# ---------------------------------------------------------------------------
@router.get("/quoter-v2/workers", response_model=V2WorkerPage)
async def list_v2_workers(
    service: V2LaborServiceDep,
    _: AdminUserDep,
    active_only: Annotated[bool, Query()] = False,
) -> V2WorkerPage:
    trabajadores = await service.list_workers(active_only=active_only)
    salida = []
    for worker in trabajadores:
        jornada = await service.resolve_workday_hours(worker)
        salida.append(_worker_out(worker, jornada, await service.hourly_rate_for(worker)))
    return V2WorkerPage(items=salida)


@router.post("/quoter-v2/workers", response_model=V2WorkerOut, status_code=status.HTTP_201_CREATED)
async def create_v2_worker(
    payload: V2WorkerCreateIn,
    service: V2LaborServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2WorkerOut:
    worker = await service.create_worker(payload.model_dump(), user=admin)
    jornada = await service.resolve_workday_hours(worker)
    resultado = _worker_out(worker, jornada, await service.hourly_rate_for(worker))
    await session.commit()
    return resultado


@router.put("/quoter-v2/workers/{worker_id}", response_model=V2WorkerOut)
async def update_v2_worker(
    worker_id: Annotated[int, Path(ge=1)],
    payload: V2WorkerUpdateIn,
    service: V2LaborServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2WorkerOut:
    """Cambia un trabajador. Afecta a lo que se cotice DESPUES, nunca a lo emitido."""
    # `exclude_unset`: lo que no vino no se toca. Mandar el objeto entero
    # convertiria cada campo ausente en un `None` y borraria datos que nadie
    # pidio borrar.
    datos = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    worker = await service.update_worker(
        worker_id, datos, expected_version=payload.expected_version, user=admin
    )
    jornada = await service.resolve_workday_hours(worker)
    resultado = _worker_out(worker, jornada, await service.hourly_rate_for(worker))
    await session.commit()
    return resultado


# ---------------------------------------------------------------------------
# Maestro de tecnicas
# ---------------------------------------------------------------------------
@router.get("/quoter-v2/techniques", response_model=V2TechniquePage)
async def list_v2_techniques(
    service: V2LaborServiceDep,
    _: AdminUserDep,
    active_only: Annotated[bool, Query()] = False,
) -> V2TechniquePage:
    jornada = await service.global_workday_hours()
    tecnicas = await service.list_techniques(active_only=active_only)
    return V2TechniquePage(items=[_technique_out(fila, jornada) for fila in tecnicas])


@router.post(
    "/quoter-v2/techniques", response_model=V2TechniqueOut, status_code=status.HTTP_201_CREATED
)
async def create_v2_technique(
    payload: V2TechniqueCreateIn,
    service: V2LaborServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2TechniqueOut:
    tecnica = await service.create_technique(payload.model_dump(), user=admin)
    resultado = _technique_out(tecnica, await service.global_workday_hours())
    await session.commit()
    return resultado


@router.put("/quoter-v2/techniques/{technique_id}", response_model=V2TechniqueOut)
async def update_v2_technique(
    technique_id: Annotated[int, Path(ge=1)],
    payload: V2TechniqueUpdateIn,
    service: V2LaborServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2TechniqueOut:
    datos = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    tecnica = await service.update_technique(
        technique_id, datos, expected_version=payload.expected_version, user=admin
    )
    resultado = _technique_out(tecnica, await service.global_workday_hours())
    await session.commit()
    return resultado


# ---------------------------------------------------------------------------
# Tareas de una cotizacion
# ---------------------------------------------------------------------------
async def _page(service: V2LaborServiceDep, quotation_id: int) -> V2LaborPage:
    tareas = await service.list_labor(quotation_id)
    carga = await service.workday_load(quotation_id)
    quotation = await service.quotation(quotation_id)
    return V2LaborPage(
        items=[_labor_out(fila, []) for fila in tareas],
        labor_cost=await service.labor_total(quotation_id),
        workday_load=[V2WorkerLoadOut(**fila) for fila in carga],
        suggested_work_days=max((int(fila["minimum_days"]) for fila in carga), default=0),
        effective_work_days=quotation.effective_work_days,
    )


@router.get("/quotations-v2/{quotation_id}/labor", response_model=V2LaborPage)
async def list_v2_quotation_labor(
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2LaborServiceDep,
    _: AdminUserDep,
) -> V2LaborPage:
    return await _page(service, quotation_id)


@router.post(
    "/quotations-v2/{quotation_id}/labor",
    response_model=V2LaborOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_v2_quotation_labor(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2LaborIn,
    service: V2LaborServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2LaborOut:
    fila, avisos = await service.add_labor(
        quotation_id, payload.model_dump(exclude_unset=True), user=admin
    )
    resultado = _labor_out(fila, avisos)
    await session.commit()
    return resultado


@router.put("/quotations-v2/{quotation_id}/labor/{labor_id}", response_model=V2LaborOut)
async def update_v2_quotation_labor(
    quotation_id: Annotated[int, Path(ge=1)],
    labor_id: Annotated[int, Path(ge=1)],
    payload: V2LaborIn,
    service: V2LaborServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2LaborOut:
    fila, avisos = await service.update_labor(
        quotation_id, labor_id, payload.model_dump(exclude_unset=True), user=admin
    )
    resultado = _labor_out(fila, avisos)
    await session.commit()
    return resultado


@router.delete(
    "/quotations-v2/{quotation_id}/labor/{labor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_v2_quotation_labor(
    quotation_id: Annotated[int, Path(ge=1)],
    labor_id: Annotated[int, Path(ge=1)],
    service: V2LaborServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> None:
    await service.delete_labor(quotation_id, labor_id, user=admin)
    await session.commit()


# ---------------------------------------------------------------------------
# Planificacion e ilustracion
# ---------------------------------------------------------------------------
@router.put("/quotations-v2/{quotation_id}/planning", response_model=V2LaborPage)
async def set_v2_planning(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2PlanningIn,
    service: V2LaborServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2LaborPage:
    """Cuantos dias de taller se van a usar. Lo decide una persona, no el sistema."""
    await service.set_planning(quotation_id, payload.effective_work_days, user=admin)
    resultado = await _page(service, quotation_id)
    await session.commit()
    return resultado


@router.get("/quotations-v2/{quotation_id}/illustration", response_model=V2IllustrationOut)
async def read_v2_illustration(
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2LaborServiceDep,
    _: AdminUserDep,
) -> V2IllustrationOut:
    return _illustration_out(await service.quotation(quotation_id))


@router.put("/quotations-v2/{quotation_id}/illustration", response_model=V2IllustrationOut)
async def set_v2_illustration(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2IllustrationIn,
    service: V2LaborServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2IllustrationOut:
    quotation = await service.set_illustration(
        quotation_id, payload.model_dump(exclude_unset=True), user=admin
    )
    resultado = _illustration_out(quotation)
    await session.commit()
    return resultado
