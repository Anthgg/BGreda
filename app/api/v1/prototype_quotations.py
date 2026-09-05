"""Cotizador de Prototipos: cotizar, emitir, cobrar.

Rutas delgadas. Todo lo que decide dinero vive en el servicio, porque un router
no es un sitio donde poner reglas: nadie lo prueba aisladamente y el dia que
aparezca un segundo camino de entrada se saltaria entero.

Quien cotiza es administracion, igual que en el Cotizador de producto: poner un
precio no es ejecutar el trabajo. El taller ve el resultado en su tablero, pero
no emite el documento ni lo cobra.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import (
    AdminUserDep,
    DbSessionDep,
    PrototypeQuotationPdfServiceDep,
    PrototypeQuotationServiceDep,
)
from app.models.prototype_quotations import PrototypeQuotationStatus
from app.schemas.prototype_quotations import (
    PrototypeQuotationDraftIn,
    PrototypeQuotationListItemOut,
    PrototypeQuotationOut,
    PrototypeQuotationPage,
    PrototypeQuotationUpdateIn,
)

router = APIRouter(prefix="/prototype-quotations", tags=["cotizador-prototipos"])


@router.get("", response_model=PrototypeQuotationPage)
async def list_prototype_quotations(
    service: PrototypeQuotationServiceDep,
    _: AdminUserDep,
    status_filter: Annotated[PrototypeQuotationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PrototypeQuotationPage:
    """El listado no lleva desglose: nadie necesita el costeo entero para elegir."""
    filas, total = await service.list(status=status_filter, limit=limit, offset=offset)
    return PrototypeQuotationPage(
        items=[
            PrototypeQuotationListItemOut(
                id=fila.id,
                code=fila.code,
                status=fila.status,
                payment_status=fila.payment_status,
                customer_name=fila.customer_name_snapshot,
                description=fila.description,
                quantity=fila.quantity,
                commercial_gross_total=fila.commercial_gross_total,
                currency_code=fila.currency_code_snapshot,
                currency_symbol=fila.currency_symbol_snapshot,
                estimated_days=fila.estimated_days,
                confirmed_at=fila.confirmed_at,
            )
            for fila in filas
        ],
        total=total,
    )


@router.post("/preview", response_model=PrototypeQuotationOut)
async def preview_prototype_quotation(
    payload: PrototypeQuotationDraftIn,
    service: PrototypeQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> PrototypeQuotationOut:
    """Cuanto costaria y cuanto tardaria, sin guardar nada.

    Se calcula sobre una fila en memoria y la transaccion se deshace al final:
    ver un precio no puede gastar un correlativo ni dejar un borrador suelto
    cada vez que alguien mueve un dia arriba o abajo.
    """
    resultado = await service.preview(payload.model_dump(), user=admin)
    await session.rollback()
    return resultado


@router.post("", response_model=PrototypeQuotationOut, status_code=status.HTTP_201_CREATED)
async def create_prototype_quotation(
    payload: PrototypeQuotationDraftIn,
    service: PrototypeQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> PrototypeQuotationOut:
    fila = await service.create_draft(payload.model_dump(), user=admin)
    resultado = await service.present(fila)
    await session.commit()
    return resultado


@router.get("/{quotation_id}", response_model=PrototypeQuotationOut)
async def read_prototype_quotation(
    quotation_id: int,
    service: PrototypeQuotationServiceDep,
    _: AdminUserDep,
) -> PrototypeQuotationOut:
    return await service.present(await service.get(quotation_id))


@router.put("/{quotation_id}", response_model=PrototypeQuotationOut)
async def update_prototype_quotation(
    quotation_id: int,
    payload: PrototypeQuotationUpdateIn,
    service: PrototypeQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> PrototypeQuotationOut:
    datos = payload.model_dump()
    esperado = datos.pop("expected_updated_at", None)
    fila = await service.update_draft(quotation_id, datos, expected_updated_at=esperado, user=admin)
    resultado = await service.present(fila)
    await session.commit()
    return resultado


@router.post("/{quotation_id}/confirm", response_model=PrototypeQuotationOut)
async def confirm_prototype_quotation(
    quotation_id: int,
    service: PrototypeQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> PrototypeQuotationOut:
    """Emite el documento: le pone numero y congela el precio."""
    fila = await service.confirm(quotation_id, user=admin)
    resultado = await service.present(fila)
    await session.commit()
    return resultado


@router.post("/{quotation_id}/cancel", response_model=PrototypeQuotationOut)
async def cancel_prototype_quotation(
    quotation_id: int,
    service: PrototypeQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> PrototypeQuotationOut:
    fila = await service.cancel(quotation_id, user=admin)
    resultado = await service.present(fila)
    await session.commit()
    return resultado


@router.post("/{quotation_id}/mark-paid", response_model=PrototypeQuotationOut)
async def mark_prototype_quotation_paid(
    quotation_id: int,
    service: PrototypeQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> PrototypeQuotationOut:
    """Registra el cobro y habilita la muestra para el taller.

    No gasta material: eso ocurre al arrancarla. Devuelve la cotizacion con la
    muestra ya asociada, para que la pantalla pueda enlazarla sin pedirla otra
    vez.
    """
    fila, _muestra = await service.mark_paid(quotation_id, user=admin)
    resultado = await service.present(fila)
    await session.commit()
    return resultado


@router.get(
    "/{quotation_id}/pdf",
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Documento comercial de la cotizacion de prototipo.",
        },
        409: {"description": "La cotizacion todavia es un borrador."},
    },
)
async def download_prototype_quotation_pdf(
    quotation_id: int,
    service: PrototypeQuotationServiceDep,
    pdf: PrototypeQuotationPdfServiceDep,
    _: AdminUserDep,
) -> Response:
    """El documento del cliente.

    Una confirmada se dibuja con lo que congelo: ni tarifas de hoy ni impuesto
    de hoy. Un borrador no se descarga —todavia no tiene numero ni precio
    firme—, porque enviarlo invitaria a mandar un papel que puede cambiar
    manana y que no se puede referenciar por codigo.
    """
    fila = await service.get(quotation_id)
    contenido, nombre = await pdf.render(fila)
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{nombre}"'},
    )
