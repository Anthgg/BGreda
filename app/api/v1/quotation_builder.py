"""API segura y transaccional del Cotizador multiproducto."""

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import (
    AdminUserDep,
    CurrentUserDep,
    DbSessionDep,
    QuotationBuilderServiceDep,
)
from app.schemas.quotation_builder import (
    BodyMaterialOptionOut,
    BodyMaterialOptionPage,
    CommercialLineIn,
    QuotationBuilderConfirmIn,
    QuotationBuilderCreateIn,
    QuotationBuilderDraftIn,
    QuotationBuilderOut,
    QuotationBuilderUpdateIn,
)

router = APIRouter(prefix="/quotation-builder", tags=["cotizador"])


@router.post("/preview", response_model=QuotationBuilderOut)
async def preview_quotation_builder(
    payload: QuotationBuilderDraftIn,
    service: QuotationBuilderServiceDep,
    _: CurrentUserDep,
) -> QuotationBuilderOut:
    """Simula costos y produccion sin persistir ni consumir correlativos."""

    return await service.preview(payload)


@router.post(
    "/pdf-preview",
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Documento PDF comercial de previsualización del borrador.",
        },
        409: {"description": "Borrador incompleto o sin productos."},
    },
)
async def preview_quotation_builder_pdf(
    payload: QuotationBuilderDraftIn,
    service: QuotationBuilderServiceDep,
    _: CurrentUserDep,
) -> Response:
    """Genera la previsualizacion comercial en PDF de un borrador en memoria."""
    pdf_bytes, filename = await service.render_pdf_preview(payload)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@router.get(
    "/{quotation_id}/pdf-preview",
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Documento PDF comercial de previsualización del borrador guardado.",
        },
        404: {"description": "Cotización no encontrada."},
        409: {"description": "Borrador incompleto o sin productos."},
    },
)
async def get_quotation_builder_pdf_preview(
    quotation_id: int,
    service: QuotationBuilderServiceDep,
    _: CurrentUserDep,
) -> Response:
    """Genera la previsualizacion comercial en PDF de un borrador guardado por ID."""
    pdf_bytes, filename = await service.get_pdf_preview(quotation_id)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@router.get("/body-materials", response_model=BodyMaterialOptionPage)
async def list_body_materials(
    service: QuotationBuilderServiceDep,
    _: CurrentUserDep,
    search: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> BodyMaterialOptionPage:
    """Materiales que pueden formar el cuerpo de una pieza.

    Va ANTES de `/{quotation_id}`: declarada despues, FastAPI leeria
    «body-materials» como un identificador de cotizacion y responderia 422.
    """
    items, total = await service.list_body_materials(search=search, limit=limit, offset=offset)
    return BodyMaterialOptionPage(
        items=[BodyMaterialOptionOut.model_validate(item) for item in items], total=total
    )


@router.post("", response_model=QuotationBuilderOut, status_code=status.HTTP_201_CREATED)
async def create_quotation_builder(
    payload: QuotationBuilderCreateIn,
    service: QuotationBuilderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationBuilderOut:
    result = await service.create(payload, user=admin)
    await session.commit()
    return result


@router.get("/{quotation_id}", response_model=QuotationBuilderOut)
async def get_quotation_builder(
    quotation_id: int,
    service: QuotationBuilderServiceDep,
    _: CurrentUserDep,
) -> QuotationBuilderOut:
    return await service.get(quotation_id)


@router.put("/{quotation_id}", response_model=QuotationBuilderOut)
async def update_quotation_builder(
    quotation_id: int,
    payload: QuotationBuilderUpdateIn,
    service: QuotationBuilderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationBuilderOut:
    draft = QuotationBuilderDraftIn.model_validate(
        payload.model_dump(exclude={"expected_updated_at"})
    )
    result = await service.update(
        quotation_id,
        draft,
        expected_updated_at=payload.expected_updated_at,
        user=admin,
    )
    await session.commit()
    return result


@router.post("/{quotation_id}/confirm", response_model=QuotationBuilderOut)
async def confirm_quotation_builder(
    quotation_id: int,
    payload: QuotationBuilderConfirmIn,
    service: QuotationBuilderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationBuilderOut:
    result = await service.confirm(
        quotation_id,
        expected_updated_at=payload.expected_updated_at,
        user=admin,
    )
    await session.commit()
    return result


@router.post("/{quotation_id}/cancel", response_model=QuotationBuilderOut)
async def cancel_quotation_builder(
    quotation_id: int,
    service: QuotationBuilderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationBuilderOut:
    result = await service.cancel(quotation_id, user=admin)
    await session.commit()
    return result


@router.post("/{quotation_id}/mark-paid", response_model=QuotationBuilderOut)
async def mark_quotation_builder_paid(
    quotation_id: int,
    service: QuotationBuilderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationBuilderOut:
    """Registra el cobro de una cotizacion confirmada.

    Mismo permiso que confirmar y anular: son las tres transiciones
    comerciales del documento. No mueve inventario ni toca precios.
    """
    result = await service.mark_paid(quotation_id, user=admin)
    await session.commit()
    return result


@router.post("/{quotation_id}/duplicate", response_model=QuotationBuilderOut)
async def duplicate_quotation_builder(
    quotation_id: int,
    service: QuotationBuilderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationBuilderOut:
    result = await service.duplicate(quotation_id, user=admin)
    await session.commit()
    return result


# ---------------------------------------------------------------------------
# Cargos comerciales (Fase 009K.1)
#
# Subrecurso de la cotizacion, no modulo aparte: viven y mueren con ella y solo
# se pueden tocar mientras es borrador. La lista viaja dentro de la cotizacion,
# asi que aqui solo hacen falta las tres mutaciones.
# ---------------------------------------------------------------------------
@router.post(
    "/{quotation_id}/commercial-lines",
    response_model=QuotationBuilderOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_commercial_line(
    quotation_id: int,
    payload: CommercialLineIn,
    service: QuotationBuilderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationBuilderOut:
    """Anade un cargo comercial al borrador.

    Devuelve la cotizacion entera y no solo el cargo: anadirlo mueve el
    subtotal, el IGV y el total, y quien lo anade necesita ver esos numeros sin
    tener que pedirlos otra vez.
    """
    result = await service.add_commercial_line(quotation_id, payload, user=admin)
    await session.commit()
    return result


@router.put("/{quotation_id}/commercial-lines/{line_id}", response_model=QuotationBuilderOut)
async def update_commercial_line(
    quotation_id: int,
    line_id: int,
    payload: CommercialLineIn,
    service: QuotationBuilderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationBuilderOut:
    result = await service.update_commercial_line(quotation_id, line_id, payload, user=admin)
    await session.commit()
    return result


@router.delete("/{quotation_id}/commercial-lines/{line_id}", response_model=QuotationBuilderOut)
async def delete_commercial_line(
    quotation_id: int,
    line_id: int,
    service: QuotationBuilderServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationBuilderOut:
    result = await service.delete_commercial_line(quotation_id, line_id, user=admin)
    await session.commit()
    return result
