"""API de ordenes de produccion.

Solo dos rutas mueven inventario:

- `POST /{id}/start` en una orden Legacy o de muestra, que descuenta su
  material entero al arrancar;
- `POST /{id}/consumptions` en una orden V2 (Fase 010I), que descuenta un
  consumo real cada vez que el taller lo registra. Una orden V2 NO descuenta
  nada al arrancar.

Todo lo demas —crear, listar, leer, completar, anular— deja los saldos
exactamente como estaban.
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
    WorkshopUserDep,
)
from app.models.production import ProductionOrderStatus
from app.schemas.production import (
    ProductionConsumptionCreateIn,
    ProductionConsumptionOut,
    ProductionConsumptionPage,
    ProductionNoteCreateIn,
    ProductionNoteOut,
    ProductionOrderCreateIn,
    ProductionOrderOut,
    ProductionOrderPage,
    ProductionTimelineOut,
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
    #: Fase 010I. Filtro PROPIO y no `quotation` reutilizado: el id de una V2 y
    #: el de una Legacy son espacios distintos, y compartir el parametro haria
    #: que la misma cifra devolviera la orden de otra cotizacion.
    v2_quotation_id: QuotationFilterDep = None,
    limit: LimitDep = 50,
    offset: OffsetDep = 0,
) -> ProductionOrderPage:
    orders, total = await service.list_orders(
        status=order_status,
        quotation_id=quotation,
        v2_quotation_id=v2_quotation_id,
        limit=limit,
        offset=offset,
    )
    return await service.present_page(orders, total=total, limit=limit, offset=offset)


@router.post("", response_model=ProductionOrderOut, status_code=status.HTTP_201_CREATED)
async def create_production_order(
    payload: ProductionOrderCreateIn,
    service: ProductionOrderServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
    response: Response,
) -> ProductionOrderOut:
    """Crea la orden de una cotizacion confirmada, de una cotizacion V2 enviada a
    produccion (Fase 010I) o de una muestra.

    **No consume material** en ninguno de los casos.

    Pedirla dos veces para el mismo origen no crea una segunda: devuelve la que
    ya hay, con 200 en vez de 201, para que el cliente sepa que no acaba de
    crear nada.

    El origen de muestra esta aqui por las ITERACIONES. Lo normal es que la
    orden de una muestra nazca sola al cobrar su cotizacion de prototipo; una
    sucesora, en cambio, no tiene cotizacion propia que cobrar.
    """
    if payload.prototype_id is not None:
        order, created = await service.create_for_prototype_id(
            payload.prototype_id,
            stock_location_id=payload.stock_location_id,
            user=actor,
        )
    elif payload.v2_quotation_id is not None:
        # Fase 010I. Una cotizacion V2 que ya paso por «Enviar a produccion».
        order, created = await service.create_for_v2_quotation(
            payload.v2_quotation_id,
            stock_location_id=payload.stock_location_id,
            idempotency_key=payload.idempotency_key,
            user=actor,
        )
    else:
        assert payload.quotation_id is not None  # lo garantiza el esquema
        order, created = await service.create(
            quotation_id=payload.quotation_id,
            stock_location_id=payload.stock_location_id,
            idempotency_key=payload.idempotency_key,
            user=actor,
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
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> ProductionOrderOut:
    """Arranca la orden. En una Legacy o de muestra, descuenta el material preparado.

    En esas dos consume todo o no consume nada: si un solo material no alcanza,
    la transaccion se deshace entera y la orden sigue en CREATED.

    Fase 010I: una orden V2 arranca **sin descontar nada**. Su material se
    registra consumo a consumo, con lo que el taller gasto de verdad.

    Arrancar dos veces no consume dos veces.
    """
    order, _consumed = await service.start(order_id, user=actor)
    result = await service.present(order)
    await session.commit()
    return result


@router.post("/{order_id}/complete", response_model=ProductionOrderOut)
async def complete_production_order(
    order_id: int,
    service: ProductionOrderServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> ProductionOrderOut:
    """Cierra la orden. NO da de alta producto terminado ni crea una quema.

    Fase 010I, decision D3: una orden V2 cuya cotizacion planifico material
    inventariable —pasta, esmalte— necesita al menos un consumo real de cada
    clase antes de cerrar (`PRODUCTION_ORDER_CONSUMPTION_MISSING`, con la clase
    que falta en el detalle). Si no planifico ninguno, cierra sin consumos.
    """
    order, _changed = await service.complete(order_id, user=actor)
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
    """Anula una orden que aun no ha consumido nada. Una arrancada, no.

    Unica de las cuatro transiciones que sigue siendo de ADMIN (Fase 009J).
    Arrancar y completar son ejecucion: las hace quien esta en el taller.
    Anular es deshacer un compromiso de fabricacion ya tomado, y ademas ocupa
    para siempre la cotizacion de origen, que no admite una segunda orden. Esa
    decision es administrativa y no se delega sin que alguien lo decida.
    """
    order, _changed = await service.cancel(order_id, user=admin)
    result = await service.present(order)
    await session.commit()
    return result


# -- Fase 010I: consumo real ------------------------------------------------
@router.get("/{order_id}/consumptions", response_model=ProductionConsumptionPage)
async def list_production_consumptions(
    order_id: int,
    service: ProductionOrderServiceDep,
    _: CurrentUserDep,
) -> ProductionConsumptionPage:
    """El material real gastado en la orden, del mas antiguo al mas reciente."""
    return await service.list_consumptions(order_id)


@router.post(
    "/{order_id}/consumptions",
    response_model=ProductionConsumptionOut,
    status_code=status.HTTP_201_CREATED,
)
async def record_production_consumption(
    order_id: int,
    payload: ProductionConsumptionCreateIn,
    service: ProductionOrderServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
    response: Response,
) -> ProductionConsumptionOut:
    """Registra material REAL gastado en una orden V2 y lo descuenta del almacen.

    **Mueve inventario**, y solo por esta accion explicita: ni cotizar, ni
    emitir, ni enviar a produccion, ni crear o arrancar la orden descuentan
    nada. Es de TALLER (ADMIN u OPERATOR), igual que ajustar existencia.

    Reintentar con la misma `idempotency_key` no descuenta dos veces: devuelve
    el consumo que ya existe, con 200 en vez de 201. Si falta existencia, no se
    descuenta nada y responde `NEGATIVE_STOCK_NOT_ALLOWED`.
    """
    consumption, created = await service.record_consumption(order_id, payload, user=actor)
    [result] = await service.present_consumptions([consumption])
    await session.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return result


@router.post(
    "/{order_id}/notes",
    response_model=ProductionNoteOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_production_note(
    order_id: int,
    payload: ProductionNoteCreateIn,
    service: ProductionOrderServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
    response: Response,
) -> ProductionNoteOut:
    """Anade una nota o una QUEMA real al seguimiento de una orden V2.

    Fase 010I, decision D4. La quema es una nota estructurada —horno, tipo y
    cuando ocurrio— porque una hornada lleva piezas de varias ordenes. No
    mueve inventario. Mismo reintento que el consumo: 200 si la clave ya existia.
    """
    note, created = await service.add_note(order_id, payload, user=actor)
    result = service.present_note(note)
    await session.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return result


@router.get("/{order_id}/timeline", response_model=ProductionTimelineOut)
async def get_production_timeline(
    order_id: int,
    service: ProductionOrderServiceDep,
    _: CurrentUserDep,
) -> ProductionTimelineOut:
    """El seguimiento de la orden: estados, consumos, notas y quemas, en orden."""
    return await service.timeline(order_id)
