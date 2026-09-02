"""API de ordenes de produccion.

Solo `POST /{id}/start` mueve inventario. Todo lo demas —crear, listar, leer,
completar, anular— deja los saldos exactamente como estaban.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import (
    AdminUserDep,
    CurrentUserDep,
    DbSessionDep,
    ProductionOrderServiceDep,
    ProductionPdfServiceDep,
)
from app.models.production import ProductionOrderStatus
from app.schemas.production import (
    ProductionOrderCreateIn,
    ProductionOrderOut,
    ProductionOrderPage,
)

router = APIRouter(prefix="/production-orders", tags=["produccion"])

#: Filtros y paginacion del listado, declarados como el resto del proyecto.
StatusFilterDep = Annotated[ProductionOrderStatus | None, Query(alias="status")]
QuotationFilterDep = Annotated[int | None, Query(gt=0)]
LimitDep = Annotated[int, Query(ge=1, le=200)]
OffsetDep = Annotated[int, Query(ge=0)]


@router.get("", response_model=ProductionOrderPage)
async def list_production_orders(
    service: ProductionOrderServiceDep,
    _: CurrentUserDep,
    order_status: StatusFilterDep = None,
    quotation: QuotationFilterDep = None,
    limit: LimitDep = 50,
    offset: OffsetDep = 0,
) -> ProductionOrderPage:
    orders, total = await service.list_orders(
        status=order_status, quotation_id=quotation, limit=limit, offset=offset
    )
    return await service.present_page(orders, total=total, limit=limit, offset=offset)


@router.post("", response_model=ProductionOrderOut, status_code=status.HTTP_201_CREATED)
async def create_production_order(
    payload: ProductionOrderCreateIn,
    service: ProductionOrderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
    response: Response,
) -> ProductionOrderOut:
    """Crea la orden de una cotizacion confirmada. **No consume material.**

    Pedirla dos veces para la misma cotizacion no crea una segunda: devuelve la
    que ya hay, con 200 en vez de 201, para que el cliente sepa que no acaba de
    crear nada.
    """
    order, created = await service.create(
        quotation_id=payload.quotation_id,
        stock_location_id=payload.stock_location_id,
        idempotency_key=payload.idempotency_key,
        user=admin,
    )
    result = await service.present(order)
    await session.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return result


@router.get("/scan/{qr_token}", response_model=ProductionOrderOut)
async def scan_production_order(
    qr_token: str,
    service: ProductionOrderServiceDep,
    _: CurrentUserDep,
) -> ProductionOrderOut:
    """Resuelve el QR de una orden.

    Exige sesion como cualquier otra lectura: que el QR sea imprimible no
    convierte la orden en publica. Un token desconocido responde el mismo 404
    que un id inexistente, para no confirmar que tokens existen.
    """
    return await service.present(await service.get_by_qr_token(qr_token))


@router.get("/{order_id}", response_model=ProductionOrderOut)
async def get_production_order(
    order_id: int,
    service: ProductionOrderServiceDep,
    _: CurrentUserDep,
) -> ProductionOrderOut:
    """La orden con su disponibilidad recalculada.

    Va DESPUES de `/scan/{qr_token}`: FastAPI resuelve por orden de
    declaracion, y con esta delante `/scan/xxx` entraria por aqui con
    `order_id="scan"` y respondaria un 422 incomprensible.
    """
    return await service.present(await service.get(order_id))


@router.get(
    "/{order_id}/document",
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Hoja de taller de la orden, con su QR.",
        },
        404: {"description": "La orden no existe."},
    },
)
async def get_production_order_document(
    order_id: int,
    service: ProductionOrderServiceDep,
    pdf: ProductionPdfServiceDep,
    _: CurrentUserDep,
) -> Response:
    order = await service.get(order_id)
    content, filename = await pdf.render(order)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@router.post("/{order_id}/start", response_model=ProductionOrderOut)
async def start_production_order(
    order_id: int,
    service: ProductionOrderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> ProductionOrderOut:
    """Arranca la orden y descuenta el material preparado.

    **Es el unico endpoint de toda la fase que mueve inventario.** Consume todo
    o no consume nada: si un solo material no alcanza, la transaccion se
    deshace entera y la orden sigue en CREATED.

    Arrancar dos veces no consume dos veces.
    """
    order, _consumed = await service.start(order_id, user=admin)
    result = await service.present(order)
    await session.commit()
    return result


@router.post("/{order_id}/complete", response_model=ProductionOrderOut)
async def complete_production_order(
    order_id: int,
    service: ProductionOrderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> ProductionOrderOut:
    """Cierra la orden. NO da de alta producto terminado ni crea una quema."""
    order, _changed = await service.complete(order_id, user=admin)
    result = await service.present(order)
    await session.commit()
    return result


@router.post("/{order_id}/cancel", response_model=ProductionOrderOut)
async def cancel_production_order(
    order_id: int,
    service: ProductionOrderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> ProductionOrderOut:
    """Anula una orden que aun no ha consumido nada. Una arrancada, no."""
    order, _changed = await service.cancel(order_id, user=admin)
    result = await service.present(order)
    await session.commit()
    return result
