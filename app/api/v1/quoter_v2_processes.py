"""Procesos de la pieza y adicionales de la cotizacion (correccion 010H).

Tres grupos de rutas:

1. el maestro: que procesos necesita una pieza del catalogo;
2. los procesos de una cotizacion: los que trajo la pieza, los que se quitaron y
   los que se anadieron aqui, con su trabajador cuando lo tienen;
3. los adicionales: maestro de conceptos y los de cada cotizacion.

Ninguna devuelve un costo que el frontend tenga que calcular. El costo de un
proceso es el de su tarea, y la tarea la hace la mano de obra de 010D.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Path, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    AdminUserDep,
    DbSessionDep,
    V2ExtraServiceDep,
    V2ProcessServiceDep,
)
from app.models.quoter_v2 import V2QuotationProduct
from app.models.quoter_v2_labor import V2Technique
from app.schemas.quoter_v2_processes import (
    V2ExtraIn,
    V2ExtraOut,
    V2ExtraPage,
    V2ExtraUpdateIn,
    V2ProcessAssignIn,
    V2ProcessIn,
    V2ProcessOut,
    V2ProcessPage,
    V2ProcessQuantityIn,
    V2ProductTechniqueOut,
    V2ProductTechniquesIn,
    V2ProductTechniquesOut,
    V2QuotationExtraIn,
    V2QuotationExtraOut,
    V2QuotationExtraPage,
    V2QuotationExtraUpdateIn,
)
from app.services.quoter_v2_processes import ProcesoCalculado

router = APIRouter(tags=["cotizador-v2"])

_CERO = Decimal(0)


# ---------------------------------------------------------------- maestro
@router.get("/quoter-v2/products/{product_id}/techniques", response_model=V2ProductTechniquesOut)
async def list_v2_product_techniques(
    product_id: Annotated[int, Path(ge=1)],
    service: V2ProcessServiceDep,
    session: DbSessionDep,
    _: AdminUserDep,
) -> V2ProductTechniquesOut:
    filas = await service.techniques_of_product(product_id)
    nombres = await _nombres_de_tecnicas(session, [fila.technique_id for fila in filas])
    return V2ProductTechniquesOut(
        product_id=product_id,
        items=[
            V2ProductTechniqueOut(
                technique_id=fila.technique_id,
                technique_name=nombres.get(fila.technique_id, ""),
                sort_order=fila.sort_order,
                active=fila.active,
            )
            for fila in filas
        ],
    )


@router.put("/quoter-v2/products/{product_id}/techniques", response_model=V2ProductTechniquesOut)
async def set_v2_product_techniques(
    product_id: Annotated[int, Path(ge=1)],
    payload: V2ProductTechniquesIn,
    service: V2ProcessServiceDep,
    session: DbSessionDep,
    admin: AdminUserDep,
) -> V2ProductTechniquesOut:
    filas = await service.set_product_techniques(product_id, payload.technique_ids, user=admin)
    nombres = await _nombres_de_tecnicas(session, [fila.technique_id for fila in filas])
    resultado = V2ProductTechniquesOut(
        product_id=product_id,
        items=[
            V2ProductTechniqueOut(
                technique_id=fila.technique_id,
                technique_name=nombres.get(fila.technique_id, ""),
                sort_order=fila.sort_order,
                active=fila.active,
            )
            for fila in filas
        ],
    )
    await session.commit()
    return resultado


# ----------------------------------------------------- procesos de una CTZ
@router.get("/quotations-v2/{quotation_id}/processes", response_model=V2ProcessPage)
async def list_v2_processes(
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2ProcessServiceDep,
    session: DbSessionDep,
    _: AdminUserDep,
) -> V2ProcessPage:
    calculados = await service.list_processes(quotation_id)
    return await _pagina(session, calculados)


@router.post(
    "/quotations-v2/{quotation_id}/processes",
    response_model=V2ProcessOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_v2_process(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2ProcessIn,
    service: V2ProcessServiceDep,
    session: DbSessionDep,
    admin: AdminUserDep,
) -> V2ProcessOut:
    await service.add_process(quotation_id, payload.model_dump(exclude_unset=True), user=admin)
    pagina = await _pagina(session, await service.list_processes(quotation_id))
    await session.commit()
    return next(
        fila
        for fila in reversed(pagina.items)
        if fila.technique_id == payload.technique_id
        and fila.v2_quotation_product_id == payload.v2_quotation_product_id
    )


@router.put(
    "/quotations-v2/{quotation_id}/processes/{process_id}/quantity",
    response_model=V2ProcessPage,
)
async def set_v2_process_quantity(
    quotation_id: Annotated[int, Path(ge=1)],
    process_id: Annotated[int, Path(ge=1)],
    payload: V2ProcessQuantityIn,
    service: V2ProcessServiceDep,
    session: DbSessionDep,
    admin: AdminUserDep,
) -> V2ProcessPage:
    await service.set_quantity(quotation_id, process_id, payload.quantity, user=admin)
    pagina = await _pagina(session, await service.list_processes(quotation_id))
    await session.commit()
    return pagina


@router.post(
    "/quotations-v2/{quotation_id}/processes/{process_id}/assign",
    response_model=V2ProcessPage,
)
async def assign_v2_process(
    quotation_id: Annotated[int, Path(ge=1)],
    process_id: Annotated[int, Path(ge=1)],
    payload: V2ProcessAssignIn,
    service: V2ProcessServiceDep,
    session: DbSessionDep,
    admin: AdminUserDep,
) -> V2ProcessPage:
    """Pone a alguien a hacer el proceso. Aqui aparece el costo."""
    _, avisos = await service.assign_worker(quotation_id, process_id, payload.worker_id, user=admin)
    pagina = await _pagina(session, await service.list_processes(quotation_id))
    pagina.warnings = [*pagina.warnings, *avisos]
    await session.commit()
    return pagina


@router.delete(
    "/quotations-v2/{quotation_id}/processes/{process_id}/assign",
    response_model=V2ProcessPage,
)
async def unassign_v2_process(
    quotation_id: Annotated[int, Path(ge=1)],
    process_id: Annotated[int, Path(ge=1)],
    service: V2ProcessServiceDep,
    session: DbSessionDep,
    admin: AdminUserDep,
) -> V2ProcessPage:
    await service.unassign_worker(quotation_id, process_id, user=admin)
    pagina = await _pagina(session, await service.list_processes(quotation_id))
    await session.commit()
    return pagina


@router.delete(
    "/quotations-v2/{quotation_id}/processes/{process_id}",
    response_model=V2ProcessPage,
)
async def remove_v2_process(
    quotation_id: Annotated[int, Path(ge=1)],
    process_id: Annotated[int, Path(ge=1)],
    service: V2ProcessServiceDep,
    session: DbSessionDep,
    admin: AdminUserDep,
) -> V2ProcessPage:
    """Quita el proceso de ESTA cotizacion. El maestro de la pieza no cambia."""
    await service.remove_process(quotation_id, process_id, user=admin)
    pagina = await _pagina(session, await service.list_processes(quotation_id))
    await session.commit()
    return pagina


# ------------------------------------------------------------- adicionales
@router.get("/quoter-v2/extras", response_model=V2ExtraPage)
async def list_v2_extras(
    service: V2ExtraServiceDep,
    _: AdminUserDep,
    active_only: Annotated[bool, Query()] = False,
) -> V2ExtraPage:
    filas = await service.list_extras(active_only=active_only)
    return V2ExtraPage(items=[V2ExtraOut.model_validate(fila) for fila in filas])


@router.post("/quoter-v2/extras", response_model=V2ExtraOut, status_code=status.HTTP_201_CREATED)
async def create_v2_extra(
    payload: V2ExtraIn,
    service: V2ExtraServiceDep,
    session: DbSessionDep,
    admin: AdminUserDep,
) -> V2ExtraOut:
    fila = await service.create_extra(payload.model_dump(exclude_unset=True), user=admin)
    resultado = V2ExtraOut.model_validate(fila)
    await session.commit()
    return resultado


@router.put("/quoter-v2/extras/{extra_id}", response_model=V2ExtraOut)
async def update_v2_extra(
    extra_id: Annotated[int, Path(ge=1)],
    payload: V2ExtraUpdateIn,
    service: V2ExtraServiceDep,
    session: DbSessionDep,
    admin: AdminUserDep,
) -> V2ExtraOut:
    fila = await service.update_extra(extra_id, payload.model_dump(exclude_unset=True), user=admin)
    resultado = V2ExtraOut.model_validate(fila)
    await session.commit()
    return resultado


@router.get("/quotations-v2/{quotation_id}/extras", response_model=V2QuotationExtraPage)
async def list_v2_quotation_extras(
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2ExtraServiceDep,
    _: AdminUserDep,
) -> V2QuotationExtraPage:
    filas = await service.list_quotation_extras(quotation_id)
    return V2QuotationExtraPage(
        items=[V2QuotationExtraOut.model_validate(fila) for fila in filas],
        extras_cost_total=sum((fila.total_cost for fila in filas), start=_CERO),
    )


@router.post(
    "/quotations-v2/{quotation_id}/extras",
    response_model=V2QuotationExtraPage,
    status_code=status.HTTP_201_CREATED,
)
async def add_v2_quotation_extra(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2QuotationExtraIn,
    service: V2ExtraServiceDep,
    session: DbSessionDep,
    admin: AdminUserDep,
) -> V2QuotationExtraPage:
    _, avisos = await service.add_quotation_extra(
        quotation_id, payload.model_dump(exclude_unset=True), user=admin
    )
    filas = await service.list_quotation_extras(quotation_id)
    resultado = V2QuotationExtraPage(
        items=[V2QuotationExtraOut.model_validate(fila) for fila in filas],
        extras_cost_total=sum((fila.total_cost for fila in filas), start=_CERO),
        warnings=avisos,
    )
    await session.commit()
    return resultado


@router.put("/quotations-v2/{quotation_id}/extras/{extra_id}", response_model=V2QuotationExtraPage)
async def update_v2_quotation_extra(
    quotation_id: Annotated[int, Path(ge=1)],
    extra_id: Annotated[int, Path(ge=1)],
    payload: V2QuotationExtraUpdateIn,
    service: V2ExtraServiceDep,
    session: DbSessionDep,
    admin: AdminUserDep,
) -> V2QuotationExtraPage:
    _, avisos = await service.update_quotation_extra(
        quotation_id, extra_id, payload.model_dump(exclude_unset=True), user=admin
    )
    filas = await service.list_quotation_extras(quotation_id)
    resultado = V2QuotationExtraPage(
        items=[V2QuotationExtraOut.model_validate(fila) for fila in filas],
        extras_cost_total=sum((fila.total_cost for fila in filas), start=_CERO),
        warnings=avisos,
    )
    await session.commit()
    return resultado


@router.delete(
    "/quotations-v2/{quotation_id}/extras/{extra_id}", response_model=V2QuotationExtraPage
)
async def delete_v2_quotation_extra(
    quotation_id: Annotated[int, Path(ge=1)],
    extra_id: Annotated[int, Path(ge=1)],
    service: V2ExtraServiceDep,
    session: DbSessionDep,
    admin: AdminUserDep,
) -> V2QuotationExtraPage:
    await service.delete_quotation_extra(quotation_id, extra_id, user=admin)
    filas = await service.list_quotation_extras(quotation_id)
    resultado = V2QuotationExtraPage(
        items=[V2QuotationExtraOut.model_validate(fila) for fila in filas],
        extras_cost_total=sum((fila.total_cost for fila in filas), start=_CERO),
    )
    await session.commit()
    return resultado


# ------------------------------------------------------------------ utiles


async def _nombres_de_tecnicas(session: AsyncSession, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    filas = (
        await session.execute(
            select(V2Technique.id, V2Technique.name).where(V2Technique.id.in_(ids))
        )
    ).all()
    return {int(uno): nombre for uno, nombre in filas}


async def _pagina(session: AsyncSession, calculados: list[ProcesoCalculado]) -> V2ProcessPage:
    """Los procesos con el nombre de su pieza, para que la pantalla no lo busque."""
    lineas: dict[int, str | None] = {}
    if calculados:
        filas = (
            await session.execute(
                select(V2QuotationProduct.id, V2QuotationProduct.product_name_snapshot).where(
                    V2QuotationProduct.id.in_(
                        [uno.proceso.v2_quotation_product_id for uno in calculados]
                    )
                )
            )
        ).all()
        lineas = {int(uno): nombre for uno, nombre in filas}

    items = []
    for calculado in calculados:
        proceso = calculado.proceso
        tarea = calculado.tarea
        items.append(
            V2ProcessOut(
                id=proceso.id,
                v2_quotation_product_id=proceso.v2_quotation_product_id,
                product_name=lineas.get(proceso.v2_quotation_product_id),
                technique_id=proceso.technique_id,
                technique_name=proceso.technique.name,
                technique_unit=proceso.technique.unit,
                technique_active=proceso.technique.active,
                standard_capacity=proceso.technique.default_capacity_per_workday,
                manual_hours=proceso.technique.manual_hours,
                origin=str(proceso.origin),
                quantity=proceso.quantity,
                quantity_overridden=proceso.quantity_overridden,
                calculated_hours=calculado.calculated_hours,
                labor_id=tarea.id if tarea else None,
                worker_id=tarea.worker_id if tarea else None,
                worker_name=tarea.worker_name_snapshot if tarea else None,
                final_hours=tarea.final_hours if tarea else None,
                labor_cost=tarea.labor_cost if tarea else None,
                warnings=calculado.warnings,
            )
        )
    return V2ProcessPage(items=items)
