"""API segura y transaccional del Cotizador multiproducto."""

from fastapi import APIRouter, status

from app.api.deps import (
    AdminUserDep,
    CurrentUserDep,
    DbSessionDep,
    QuotationBuilderServiceDep,
)
from app.schemas.quotation_builder import (
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
