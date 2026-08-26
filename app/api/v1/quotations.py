"""Rutas REST de maestros de costos y cotizaciones de Fase 005."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    AdminUserDep,
    CurrentUserDep,
    DbSessionDep,
    QuotationServiceDep,
)
from app.models.quotations import QuotationStatus
from app.schemas.quotations import (
    AdditionalCreate,
    AdditionalOut,
    AdditionalPage,
    AdditionalUpdate,
    OtherCostCreate,
    OtherCostOut,
    OtherCostPage,
    OtherCostUpdate,
    ProductPriceUpdateOut,
    QuotationCalculateIn,
    QuotationCalculateOut,
    QuotationConfirmIn,
    QuotationCreateIn,
    QuotationOut,
    QuotationPage,
    QuotationUpdateIn,
    TechniqueCreate,
    TechniqueOut,
    TechniquePage,
    TechniqueUpdate,
)

router = APIRouter(tags=["cotizaciones"])


@router.get("/techniques", response_model=TechniquePage)
async def list_techniques(
    service: QuotationServiceDep,
    _: CurrentUserDep,
    search: str | None = Query(None),
    active: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> TechniquePage:
    items, total = await service.list_techniques(
        search=search, active=active, limit=limit, offset=offset
    )
    return TechniquePage(
        items=[TechniqueOut.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/techniques", response_model=TechniqueOut, status_code=status.HTTP_201_CREATED)
async def create_technique(
    payload: TechniqueCreate,
    service: QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> TechniqueOut:
    item = await service.create_technique(payload, admin)
    await session.commit()
    return TechniqueOut.model_validate(item)


@router.put("/techniques/{technique_id}", response_model=TechniqueOut)
async def update_technique(
    technique_id: int,
    payload: TechniqueUpdate,
    service: QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> TechniqueOut:
    item = await service.update_technique(technique_id, payload, admin)
    await session.commit()
    await session.refresh(item)
    return TechniqueOut.model_validate(item)


@router.get("/additionals", response_model=AdditionalPage)
async def list_additionals(
    service: QuotationServiceDep,
    _: CurrentUserDep,
    search: str | None = Query(None),
    active: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> AdditionalPage:
    items, total = await service.list_additionals(
        search=search, active=active, limit=limit, offset=offset
    )
    return AdditionalPage(
        items=[AdditionalOut.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/additionals", response_model=AdditionalOut, status_code=status.HTTP_201_CREATED)
async def create_additional(
    payload: AdditionalCreate,
    service: QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> AdditionalOut:
    item = await service.create_additional(payload, admin)
    await session.commit()
    return AdditionalOut.model_validate(item)


@router.put("/additionals/{additional_id}", response_model=AdditionalOut)
async def update_additional(
    additional_id: int,
    payload: AdditionalUpdate,
    service: QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> AdditionalOut:
    item = await service.update_additional(additional_id, payload, admin)
    await session.commit()
    await session.refresh(item)
    return AdditionalOut.model_validate(item)


@router.get("/other-costs", response_model=OtherCostPage)
async def list_other_costs(
    service: QuotationServiceDep,
    _: CurrentUserDep,
    search: str | None = Query(None),
    active: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> OtherCostPage:
    items, total = await service.list_other_costs(
        search=search, active=active, limit=limit, offset=offset
    )
    return OtherCostPage(
        items=[OtherCostOut.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/other-costs", response_model=OtherCostOut, status_code=status.HTTP_201_CREATED)
async def create_other_cost(
    payload: OtherCostCreate,
    service: QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> OtherCostOut:
    item = await service.create_other_cost(payload, admin)
    await session.commit()
    return OtherCostOut.model_validate(item)


@router.put("/other-costs/{other_cost_id}", response_model=OtherCostOut)
async def update_other_cost(
    other_cost_id: int,
    payload: OtherCostUpdate,
    service: QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> OtherCostOut:
    item = await service.update_other_cost(other_cost_id, payload, admin)
    await session.commit()
    await session.refresh(item)
    return OtherCostOut.model_validate(item)


@router.post("/quotations/calculate", response_model=QuotationCalculateOut)
async def calculate_quotation(
    payload: QuotationCalculateIn,
    service: QuotationServiceDep,
    _: CurrentUserDep,
) -> QuotationCalculateOut:
    return await service.calculate(payload)


@router.get("/quotations", response_model=QuotationPage)
async def list_quotations(
    service: QuotationServiceDep,
    _: CurrentUserDep,
    search: str | None = Query(None),
    quotation_status: Annotated[QuotationStatus | None, Query(alias="status")] = None,
    product_id: Annotated[int | None, Query(alias="product")] = None,
    customer_id: Annotated[int | None, Query(alias="customer")] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> QuotationPage:
    items, total = await service.list_quotations(
        search=search,
        status=quotation_status,
        product_id=product_id,
        customer_id=customer_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return QuotationPage(items=items, total=total, limit=limit, offset=offset)


@router.post("/quotations", response_model=QuotationOut, status_code=status.HTTP_201_CREATED)
async def create_quotation(
    payload: QuotationCreateIn,
    service: QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationOut:
    result = await service.create(payload, user=admin)
    await session.commit()
    return result


@router.get("/quotations/{quotation_id}", response_model=QuotationOut)
async def get_quotation(
    quotation_id: int, service: QuotationServiceDep, _: CurrentUserDep
) -> QuotationOut:
    return await service.get(quotation_id)


@router.put("/quotations/{quotation_id}", response_model=QuotationOut)
async def update_quotation(
    quotation_id: int,
    payload: QuotationUpdateIn,
    service: QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationOut:
    result = await service.update(quotation_id, payload, user=admin)
    await session.commit()
    return result


@router.post("/quotations/{quotation_id}/confirm", response_model=QuotationOut)
async def confirm_quotation(
    quotation_id: int,
    payload: QuotationConfirmIn,
    service: QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationOut:
    result = await service.confirm(
        quotation_id, accept_source_changes=payload.accept_source_changes, user=admin
    )
    await session.commit()
    return result


@router.post("/quotations/{quotation_id}/cancel", response_model=QuotationOut)
async def cancel_quotation(
    quotation_id: int,
    service: QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationOut:
    result = await service.cancel(quotation_id, user=admin)
    await session.commit()
    return result


@router.post(
    "/quotations/{quotation_id}/duplicate",
    response_model=QuotationOut,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_quotation(
    quotation_id: int,
    service: QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> QuotationOut:
    result = await service.duplicate(quotation_id, user=admin)
    await session.commit()
    return result


@router.post(
    "/quotations/{quotation_id}/update-product-price",
    response_model=ProductPriceUpdateOut,
)
async def update_product_price(
    quotation_id: int,
    service: QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> ProductPriceUpdateOut:
    result = await service.update_product_price(quotation_id, user=admin)
    await session.commit()
    return result
