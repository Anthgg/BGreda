"""API de prototipos.

Reparto de permisos, heredado de 009J: **el taller ejecuta, la administracion
decide.** Quien esta en el taller registra la muestra, elige sus materiales, la
arranca y la completa. Aprobar, rechazar y anular son decisiones administrativas
—condicionan si un pedido entero se fabrica— y no se delegan.

`POST /{id}/start` es el unico endpoint de todo el modulo que mueve inventario.
Crear, editar, poner materiales, completar, aprobar, rechazar, anular e iterar
dejan los saldos exactamente como estaban.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    AdminUserDep,
    CurrentUserDep,
    DbSessionDep,
    PrototypeServiceDep,
    WorkshopUserDep,
)
from app.models.prototypes import PrototypeApproval, PrototypeStatus
from app.schemas.prototypes import (
    PrototypeCreateIn,
    PrototypeDecisionIn,
    PrototypeMaterialsIn,
    PrototypeOut,
    PrototypePage,
    PrototypeSuccessorIn,
    PrototypeUpdateIn,
)
from app.services.prototypes import MaterialInput

router = APIRouter(prefix="/prototypes", tags=["prototipos"])

StatusFilterDep = Annotated[PrototypeStatus | None, Query(alias="status")]
ApprovalFilterDep = Annotated[PrototypeApproval | None, Query(alias="approval")]
QuotationFilterDep = Annotated[int | None, Query(gt=0)]
LimitDep = Annotated[int, Query(ge=1, le=200)]
OffsetDep = Annotated[int, Query(ge=0)]


@router.get("", response_model=PrototypePage)
async def list_prototypes(
    service: PrototypeServiceDep,
    _: CurrentUserDep,
    prototype_status: StatusFilterDep = None,
    approval: ApprovalFilterDep = None,
    quotation: QuotationFilterDep = None,
    limit: LimitDep = 50,
    offset: OffsetDep = 0,
) -> PrototypePage:
    rows, total = await service.list_prototypes(
        status=prototype_status,
        approval=approval,
        quotation_id=quotation,
        limit=limit,
        offset=offset,
    )
    return PrototypePage(
        items=[await service.present_summary(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=PrototypeOut, status_code=status.HTTP_201_CREATED)
async def create_prototype(
    payload: PrototypeCreateIn,
    service: PrototypeServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> PrototypeOut:
    """Registra la muestra. **No consume material.**

    La cotizacion, el producto y el almacen son opcionales: se prototipa
    justamente para decidir si la pieza merece existir, y eso ocurre antes de
    que haya pedido.
    """
    prototype = await service.create(
        name=payload.name,
        quantity=payload.quantity,
        quotation_id=payload.quotation_id,
        product_id=payload.product_id,
        stock_location_id=payload.stock_location_id,
        target_days=payload.target_days,
        notes=payload.notes,
        materials=[
            MaterialInput(product_id=item.product_id, quantity=item.quantity)
            for item in payload.materials
        ],
        user=actor,
    )
    result = await service.present(prototype)
    await session.commit()
    return result


@router.get("/{prototype_id}", response_model=PrototypeOut)
async def get_prototype(
    prototype_id: int,
    service: PrototypeServiceDep,
    _: CurrentUserDep,
) -> PrototypeOut:
    """La muestra con su disponibilidad recalculada. Solo lectura."""
    return await service.present(await service.get(prototype_id))


@router.put("/{prototype_id}", response_model=PrototypeOut)
async def update_prototype(
    prototype_id: int,
    payload: PrototypeUpdateIn,
    service: PrototypeServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> PrototypeOut:
    prototype = await service.update(
        prototype_id,
        name=payload.name,
        quantity=payload.quantity,
        quotation_id=payload.quotation_id,
        product_id=payload.product_id,
        stock_location_id=payload.stock_location_id,
        target_days=payload.target_days,
        notes=payload.notes,
        user=actor,
    )
    result = await service.present(prototype)
    await session.commit()
    return result


@router.put("/{prototype_id}/materials", response_model=PrototypeOut)
async def set_prototype_materials(
    prototype_id: int,
    payload: PrototypeMaterialsIn,
    service: PrototypeServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> PrototypeOut:
    """Fija lo que ESTA muestra va a gastar.

    No se deriva de la receta de la cotizacion ni del plan de esmaltes: una
    muestra sin esmaltar no lleva barniz porque nadie se lo puso, no porque una
    regla lo dedujera. **No mueve inventario.**
    """
    prototype = await service.set_materials(
        prototype_id,
        [
            MaterialInput(product_id=item.product_id, quantity=item.quantity)
            for item in payload.materials
        ],
        user=actor,
    )
    result = await service.present(prototype)
    await session.commit()
    return result


@router.post("/{prototype_id}/start", response_model=PrototypeOut)
async def start_prototype(
    prototype_id: int,
    service: PrototypeServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> PrototypeOut:
    """Arranca la muestra y descuenta lo que lleva.

    **Es el unico endpoint del modulo que mueve inventario.** Consume todo o no
    consume nada: si un solo material no alcanza, la transaccion se deshace
    entera y la muestra sigue en CREATED.

    Exige que la cotizacion de origen conste cobrada (Fase 009H.1). Sin
    cotizacion no hay nada que cobrar, y por tanto tampoco se arranca.

    Arrancar dos veces no consume dos veces.
    """
    prototype, _consumed = await service.start(prototype_id, user=actor)
    result = await service.present(prototype)
    await session.commit()
    return result


@router.post("/{prototype_id}/complete", response_model=PrototypeOut)
async def complete_prototype(
    prototype_id: int,
    service: PrototypeServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> PrototypeOut:
    """Cierra la fabricacion. NO consume otra vez y NO decide nada.

    Una muestra completada y sin evaluar queda COMPLETED + PENDING, y eso no es
    un estado a medias: es el estado normal mientras alguien la mira.
    """
    prototype, _changed = await service.complete(prototype_id, user=actor)
    result = await service.present(prototype)
    await session.commit()
    return result


@router.post("/{prototype_id}/approve", response_model=PrototypeOut)
async def approve_prototype(
    prototype_id: int,
    payload: PrototypeDecisionIn,
    service: PrototypeServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> PrototypeOut:
    """Da por buena la muestra. Administrativa, como anular una orden.

    **No crea ni arranca ninguna produccion.** Solo satisface el guardia que
    impide fabricar en serie sin muestra aprobada.
    """
    prototype = await service.approve(prototype_id, user=admin, note=payload.note)
    result = await service.present(prototype)
    await session.commit()
    return result


@router.post("/{prototype_id}/reject", response_model=PrototypeOut)
async def reject_prototype(
    prototype_id: int,
    payload: PrototypeDecisionIn,
    service: PrototypeServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> PrototypeOut:
    """Descarta la muestra. Lo rechazado no se reescribe: se itera."""
    prototype = await service.reject(prototype_id, user=admin, note=payload.note)
    result = await service.present(prototype)
    await session.commit()
    return result


@router.post("/{prototype_id}/cancel", response_model=PrototypeOut)
async def cancel_prototype(
    prototype_id: int,
    service: PrototypeServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> PrototypeOut:
    """Anula una muestra que todavia no gasto nada.

    Una arrancada no: anularla no devuelve el barro al saco. Administrativa por
    la misma razon que anular una orden de produccion.
    """
    prototype, _changed = await service.cancel(prototype_id, user=admin)
    result = await service.present(prototype)
    await session.commit()
    return result


@router.post(
    "/{prototype_id}/successor",
    response_model=PrototypeOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_prototype_successor(
    prototype_id: int,
    payload: PrototypeSuccessorIn,
    service: PrototypeServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> PrototypeOut:
    """Crea la siguiente iteracion a partir de una que no valio.

    Copia la intencion —pedido, producto, almacen, cantidad, materiales— y nada
    de la historia. La anterior se queda como estaba: un rechazo es un hecho, y
    reescribirlo borraria por que hubo que repetir la muestra.
    """
    prototype = await service.create_successor(prototype_id, user=actor, notes=payload.notes)
    result = await service.present(prototype)
    await session.commit()
    return result
