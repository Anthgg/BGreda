"""Endpoints de inventario.

No existe ninguna ruta que escriba el saldo directamente: se registra un
ajuste con su motivo y el backend deriva el saldo del movimiento.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import AdminUserDep, CurrentUserDep, DbSessionDep, InventoryServiceDep
from app.schemas.common import ErrorResponse
from app.schemas.inventory import (
    StockAdjustmentCreate,
    StockBalanceOut,
    StockBalancePage,
    StockLocationCreate,
    StockLocationOut,
    StockMovementOut,
    StockMovementPage,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Sin sesion valida"},
    403: {"model": ErrorResponse, "description": "Requiere rol ADMIN"},
    404: {"model": ErrorResponse, "description": "El registro no existe"},
    422: {"model": ErrorResponse, "description": "Movimiento invalido"},
}

LimitDep = Annotated[int, Query(ge=1, le=200)]
OffsetDep = Annotated[int, Query(ge=0)]


@router.get("/locations", response_model=list[StockLocationOut], responses=_ERRORS)
async def list_locations(_: CurrentUserDep, service: InventoryServiceDep) -> list[StockLocationOut]:
    return [StockLocationOut.model_validate(item) for item in await service.list_locations()]


@router.post(
    "/locations",
    response_model=StockLocationOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def create_location(
    payload: StockLocationCreate,
    _: AdminUserDep,
    service: InventoryServiceDep,
    session: DbSessionDep,
) -> StockLocationOut:
    location = await service.create_location(payload)
    await session.commit()
    await session.refresh(location)
    return StockLocationOut.model_validate(location)


@router.get("", response_model=StockBalancePage, responses=_ERRORS)
async def list_stock(
    _: CurrentUserDep,
    service: InventoryServiceDep,
    search: str | None = None,
    product_id: int | None = None,
    location_id: int | None = None,
    limit: LimitDep = 50,
    offset: OffsetDep = 0,
) -> StockBalancePage:
    rows, total = await service.list_balances(
        product_id=product_id,
        location_id=location_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return StockBalancePage(
        items=[
            StockBalanceOut(
                product_id=product.id,
                internal_reference=product.internal_reference,
                product_name=product.name,
                location_id=location.id,
                location_name=location.name,
                uom_code=product.base_uom_code,
                quantity=balance.quantity,
            )
            for balance, product, location in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/movements", response_model=StockMovementPage, responses=_ERRORS)
async def list_movements(
    _: CurrentUserDep,
    service: InventoryServiceDep,
    product_id: int | None = None,
    location_id: int | None = None,
    limit: LimitDep = 50,
    offset: OffsetDep = 0,
) -> StockMovementPage:
    rows, total = await service.list_movements(
        product_id=product_id, location_id=location_id, limit=limit, offset=offset
    )
    return StockMovementPage(
        items=[
            StockMovementOut(
                id=movement.id,
                product_id=product.id,
                internal_reference=product.internal_reference,
                product_name=product.name,
                location_id=location.id,
                location_name=location.name,
                movement_type=movement.movement_type,
                quantity=movement.quantity,
                balance_after=movement.balance_after,
                uom_code=movement.uom_code,
                reason=movement.reason,
                import_batch_id=movement.import_batch_id,
                created_by=movement.created_by,
                created_by_name=movement.created_by_name,
                created_at=movement.created_at,
            )
            for movement, product, location in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/adjustments",
    response_model=StockMovementOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def create_adjustment(
    payload: StockAdjustmentCreate,
    user: AdminUserDep,
    service: InventoryServiceDep,
    session: DbSessionDep,
) -> StockMovementOut:
    """Ajusta existencia dejando siempre el movimiento que lo justifica."""
    movement = await service.adjust(payload, user)
    await session.commit()
    rows, _total = await service.list_movements(product_id=movement.product_id, limit=1)
    stored, product, location = rows[0]
    return StockMovementOut(
        id=stored.id,
        product_id=product.id,
        internal_reference=product.internal_reference,
        product_name=product.name,
        location_id=location.id,
        location_name=location.name,
        movement_type=stored.movement_type,
        quantity=stored.quantity,
        balance_after=stored.balance_after,
        uom_code=stored.uom_code,
        reason=stored.reason,
        import_batch_id=stored.import_batch_id,
        created_by=stored.created_by,
        created_by_name=stored.created_by_name,
        created_at=stored.created_at,
    )
