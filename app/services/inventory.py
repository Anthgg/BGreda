"""Inventario: saldos derivados de movimientos.

Regla central: no hay ninguna ruta que escriba ``stock_balances`` sin crear
antes el ``stock_movements`` que lo justifica. El saldo es consecuencia del
historial, no un campo editable.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.models.inventory import MovementType, StockBalance, StockLocation, StockMovement
from app.models.masters import Product, UnitOfMeasure
from app.schemas.auth import AuthenticatedUser
from app.schemas.inventory import StockAdjustmentCreate, StockLocationCreate

MAX_PAGE_SIZE = 200


class InventoryNotFoundError(APIError):
    status_code = 404
    code = "INVENTORY_NOT_FOUND"
    message = "El registro de inventario no existe"


class NegativeStockError(APIError):
    """Politica por defecto de Fase 3: una operacion normal no deja negativos."""

    status_code = 422
    code = "NEGATIVE_STOCK_NOT_ALLOWED"
    message = "El movimiento dejaria existencia negativa"


class MissingUomError(APIError):
    status_code = 422
    code = "PRODUCT_WITHOUT_UOM"
    message = "El producto no tiene unidad de medida y no puede llevar existencia"


def _limit(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE_SIZE))


class InventoryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- ubicaciones --------------------------------------------------------
    async def list_locations(self) -> list[StockLocation]:
        stmt = select(StockLocation).order_by(StockLocation.name)
        return list((await self._session.execute(stmt)).scalars().all())

    async def create_location(self, payload: StockLocationCreate) -> StockLocation:
        location = StockLocation(name=payload.name, active=payload.active)
        self._session.add(location)
        await self._session.flush()
        return location

    async def get_or_create_location(self, name: str) -> StockLocation:
        existing = await self._session.scalar(
            select(StockLocation).where(StockLocation.name == name)
        )
        if existing is not None:
            return existing
        location = StockLocation(name=name)
        self._session.add(location)
        await self._session.flush()
        return location

    # -- saldos -------------------------------------------------------------
    def _balance_query(
        self, *, product_id: int | None, location_id: int | None, search: str | None
    ) -> Select[tuple[StockBalance, Product, StockLocation]]:
        stmt = (
            select(StockBalance, Product, StockLocation)
            .join(Product, Product.id == StockBalance.product_id)
            .join(StockLocation, StockLocation.id == StockBalance.location_id)
        )
        if product_id is not None:
            stmt = stmt.where(StockBalance.product_id == product_id)
        if location_id is not None:
            stmt = stmt.where(StockBalance.location_id == location_id)
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(
                Product.name.ilike(pattern) | Product.internal_reference.ilike(pattern)
            )
        return stmt

    async def list_balances(
        self,
        *,
        product_id: int | None = None,
        location_id: int | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[tuple[StockBalance, Product, StockLocation]], int]:
        stmt = self._balance_query(product_id=product_id, location_id=location_id, search=search)
        total = await self._session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = await self._session.execute(
            stmt.order_by(Product.internal_reference, StockLocation.name)
            .limit(_limit(limit))
            .offset(max(0, offset))
        )
        return [tuple(row) for row in rows.all()], int(total or 0)

    async def list_movements(
        self,
        *,
        product_id: int | None = None,
        location_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[tuple[StockMovement, Product, StockLocation]], int]:
        stmt = (
            select(StockMovement, Product, StockLocation)
            .join(Product, Product.id == StockMovement.product_id)
            .join(StockLocation, StockLocation.id == StockMovement.location_id)
        )
        if product_id is not None:
            stmt = stmt.where(StockMovement.product_id == product_id)
        if location_id is not None:
            stmt = stmt.where(StockMovement.location_id == location_id)
        total = await self._session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = await self._session.execute(
            stmt.order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
            .limit(_limit(limit))
            .offset(max(0, offset))
        )
        return [tuple(row) for row in rows.all()], int(total or 0)

    # -- escritura ----------------------------------------------------------
    async def apply_movement(
        self,
        *,
        product: Product,
        location: StockLocation,
        quantity: Decimal,
        movement_type: MovementType,
        reason: str | None,
        user_id: uuid.UUID | None,
        user_name: str | None,
        import_batch_id: int | None = None,
    ) -> StockMovement:
        """Aplica un delta y deja la evidencia que lo respalda.

        Es el unico camino de escritura de existencias de toda la aplicacion.
        """
        if product.base_uom_code is None:
            raise MissingUomError()

        balance = await self._session.scalar(
            select(StockBalance)
            .where(
                StockBalance.product_id == product.id,
                StockBalance.location_id == location.id,
            )
            .with_for_update()
        )
        if balance is None:
            balance = StockBalance(
                product_id=product.id, location_id=location.id, quantity=Decimal(0)
            )
            self._session.add(balance)

        new_quantity = balance.quantity + quantity
        if new_quantity < 0:
            raise NegativeStockError()

        balance.quantity = new_quantity
        movement = StockMovement(
            product_id=product.id,
            location_id=location.id,
            movement_type=movement_type,
            quantity=quantity,
            balance_after=new_quantity,
            uom_code=product.base_uom_code,
            reason=reason,
            import_batch_id=import_batch_id,
            created_by=user_id,
            created_by_name=user_name,
        )
        self._session.add(movement)
        await self._session.flush()
        return movement

    async def adjust(
        self, payload: StockAdjustmentCreate, user: AuthenticatedUser
    ) -> StockMovement:
        product = await self._session.get(Product, payload.product_id)
        if product is None:
            raise InventoryNotFoundError("El producto no existe")
        location = await self._session.get(StockLocation, payload.location_id)
        if location is None:
            raise InventoryNotFoundError("La ubicacion no existe")
        if product.base_uom_code is not None:
            if await self._session.get(UnitOfMeasure, product.base_uom_code) is None:
                raise MissingUomError()
        return await self.apply_movement(
            product=product,
            location=location,
            quantity=payload.quantity,
            movement_type=MovementType.ADJUSTMENT,
            reason=payload.reason,
            user_id=user.id,
            user_name=user.display_name,
        )
