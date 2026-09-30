"""Inventario: saldos derivados de movimientos.

Regla central: no hay ninguna ruta que escriba ``stock_balances`` sin crear
antes el ``stock_movements`` que lo justifica. El saldo es consecuencia del
historial, no un campo editable.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.models.inventory import (
    MovementType,
    StockBalance,
    StockLocation,
    StockLotBalance,
    StockMovement,
)
from app.models.masters import Product, ProductType, UnitOfMeasure
from app.models.production import ProductionOrder, ProductionOrderResult, ProductionOrderStatus
from app.models.quoter_v2 import V2ProductionHandoff, V2QuotationProduct
from app.models.recipes import PreparationStatus, RecipePreparation
from app.schemas.auth import AuthenticatedUser
from app.schemas.inventory import (
    StockAdjustmentCreate,
    StockDeliveryCreate,
    StockLocationCreate,
)

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


class OrphanPrototypeMovementError(APIError):
    """Fase 009K. Un consumo de muestra tiene que decir de que muestra sale.

    Es un error de programacion, no de uso: la comprobacion vive aqui —en el
    unico camino de escritura de existencias— porque es el sitio donde no se
    puede rodear. Un PROTOTYPE_OUT huerfano seria material gastado sin nadie a
    quien preguntarle en que.
    """

    status_code = 500
    code = "PROTOTYPE_MOVEMENT_WITHOUT_PROTOTYPE"
    message = "Un consumo de prototipo debe referenciar su prototipo"


class MissingUomError(APIError):
    status_code = 422
    code = "PRODUCT_WITHOUT_UOM"
    message = "El producto no tiene unidad de medida y no puede llevar existencia"


class PreparationLotRequiredError(APIError):
    status_code = 422
    code = "PREPARATION_LOT_REQUIRED"
    message = "El consumo de material preparado requiere un lote explicito"


class InvalidPreparationLotError(APIError):
    status_code = 422
    code = "PREPARATION_LOT_INVALID"
    message = "El lote no pertenece al material preparado y la ubicacion indicados"


class LotInsufficientStockError(APIError):
    status_code = 422
    code = "LOT_INSUFFICIENT_STOCK"
    message = "El lote elegido no tiene existencia suficiente"


class DeliveryOriginInvalidError(APIError):
    status_code = 422
    code = "DELIVERY_ORIGIN_INVALID"
    message = "El producto no corresponde al origen de entrega indicado"


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

    async def list_lots(
        self, *, product_id: int | None = None, location_id: int | None = None
    ) -> list[tuple[StockLotBalance, RecipePreparation]]:
        stmt = (
            select(StockLotBalance, RecipePreparation)
            .join(RecipePreparation, RecipePreparation.id == StockLotBalance.preparation_id)
            .where(
                StockLotBalance.quantity > 0,
                RecipePreparation.status == PreparationStatus.COMPLETED,
            )
        )
        if product_id is not None:
            stmt = stmt.where(StockLotBalance.product_id == product_id)
        if location_id is not None:
            stmt = stmt.where(StockLotBalance.location_id == location_id)
        stmt = stmt.order_by(RecipePreparation.prepared_at, RecipePreparation.id)
        return [tuple(row) for row in (await self._session.execute(stmt)).all()]

    async def _apply_prepared_lot_delta(
        self,
        *,
        product: Product,
        location: StockLocation,
        quantity: Decimal,
        movement_type: MovementType,
        preparation_id: int | None,
        source_preparation_id: int | None,
        expected_aggregate: Decimal,
    ) -> None:
        if product.product_type is not ProductType.PREPARED_MATERIAL:
            return

        if movement_type is MovementType.PREPARATION_IN:
            lot_id = preparation_id
            if lot_id is None or quantity <= 0:
                raise PreparationLotRequiredError()
            await self._session.execute(
                pg_insert(StockLotBalance)
                .values(
                    preparation_id=lot_id,
                    product_id=product.id,
                    location_id=location.id,
                    quantity=Decimal(0),
                    uom_code=product.base_uom_code,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        StockLotBalance.preparation_id,
                        StockLotBalance.location_id,
                    ]
                )
            )
        elif movement_type is MovementType.PREPARATION_OUT:
            lot_id = source_preparation_id
            if lot_id is None or quantity >= 0:
                raise PreparationLotRequiredError()
        else:
            lot_id = source_preparation_id or preparation_id
            if lot_id is None:
                raise PreparationLotRequiredError()

        preparation = await self._session.get(RecipePreparation, lot_id)
        if (
            preparation is None
            or preparation.prepared_product_id != product.id
            or preparation.location_id != location.id
            or preparation.status is not PreparationStatus.COMPLETED
        ):
            raise InvalidPreparationLotError()

        lot = await self._session.scalar(
            select(StockLotBalance)
            .where(
                StockLotBalance.preparation_id == lot_id,
                StockLotBalance.product_id == product.id,
                StockLotBalance.location_id == location.id,
            )
            .with_for_update()
        )
        if lot is None:
            raise InvalidPreparationLotError()
        lot_quantity = lot.quantity + quantity
        if lot_quantity < 0:
            raise LotInsufficientStockError()
        lot.quantity = lot_quantity

        lot_total = await self._session.scalar(
            select(func.coalesce(func.sum(StockLotBalance.quantity), Decimal(0))).where(
                StockLotBalance.product_id == product.id,
                StockLotBalance.location_id == location.id,
            )
        )
        if lot_total != expected_aggregate:
            raise RuntimeError("El saldo agregado y la suma de lotes preparados divergen")

    async def get_movement(
        self, movement_id: int
    ) -> tuple[StockMovement, Product, StockLocation] | None:
        stmt = (
            select(StockMovement, Product, StockLocation)
            .join(Product, Product.id == StockMovement.product_id)
            .join(StockLocation, StockLocation.id == StockMovement.location_id)
            .where(StockMovement.id == movement_id)
        )
        row = (await self._session.execute(stmt)).first()
        return tuple(row) if row is not None else None

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
        preparation_id: int | None = None,
        source_preparation_id: int | None = None,
        production_order_id: int | None = None,
        prototype_id: int | None = None,
        v2_quotation_id: int | None = None,
    ) -> StockMovement:
        """Aplica un delta y deja la evidencia que lo respalda.

        Es el unico camino de escritura de existencias de toda la aplicacion.
        """
        if product.base_uom_code is None:
            raise MissingUomError()

        # FOR UPDATE no bloquea una fila ausente. El upsert crea el saldo cero
        # canónico; una primera operación concurrente espera en la UNIQUE.
        await self._session.execute(
            pg_insert(StockBalance)
            .values(product_id=product.id, location_id=location.id, quantity=Decimal(0))
            .on_conflict_do_nothing(
                index_elements=[StockBalance.product_id, StockBalance.location_id]
            )
        )
        balance = await self._session.scalar(
            select(StockBalance)
            .where(
                StockBalance.product_id == product.id,
                StockBalance.location_id == location.id,
            )
            .with_for_update()
        )
        if balance is None:
            raise RuntimeError("Stock balance upsert did not produce a row")

        if movement_type is MovementType.PROTOTYPE_OUT and prototype_id is None:
            # Un consumo de muestra sin muestra detras seria un gasto sin
            # responsable: nadie podria decir en que se fue ese material.
            raise OrphanPrototypeMovementError()

        new_quantity = balance.quantity + quantity
        if new_quantity < 0:
            raise NegativeStockError()

        await self._apply_prepared_lot_delta(
            product=product,
            location=location,
            quantity=quantity,
            movement_type=movement_type,
            preparation_id=preparation_id,
            source_preparation_id=source_preparation_id,
            expected_aggregate=new_quantity,
        )

        if (
            product.product_type is ProductType.PREPARED_MATERIAL
            and movement_type is not MovementType.PREPARATION_IN
        ):
            source_preparation_id = source_preparation_id or preparation_id

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
            preparation_id=preparation_id,
            source_preparation_id=source_preparation_id,
            production_order_id=production_order_id,
            prototype_id=prototype_id,
            v2_quotation_id=v2_quotation_id,
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
            preparation_id=payload.preparation_id,
        )

    async def deliver(
        self, payload: StockDeliveryCreate, user: AuthenticatedUser
    ) -> tuple[StockMovement, Product, StockLocation]:
        product = await self._session.get(Product, payload.product_id)
        location = await self._session.get(StockLocation, payload.location_id)
        if product is None or location is None or not location.active:
            raise InventoryNotFoundError()
        if product.product_type is not ProductType.FINISHED_PRODUCT:
            raise DeliveryOriginInvalidError()

        order: ProductionOrder | None = None
        if payload.production_order_id is not None:
            order = await self._session.get(ProductionOrder, payload.production_order_id)
            if (
                order is None
                or order.status is not ProductionOrderStatus.COMPLETED
                or order.v2_firing_handoff_id is not None
            ):
                raise DeliveryOriginInvalidError()
            result_product_id = await self._session.scalar(
                select(ProductionOrderResult.product_id).where(
                    ProductionOrderResult.production_order_id == order.id,
                    ProductionOrderResult.product_id == product.id,
                    ProductionOrderResult.good_quantity > 0,
                )
            )
            if result_product_id is None:
                raise DeliveryOriginInvalidError()

        if payload.v2_quotation_id is not None:
            line_id = await self._session.scalar(
                select(V2QuotationProduct.id).where(
                    V2QuotationProduct.v2_quotation_id == payload.v2_quotation_id,
                    (V2QuotationProduct.product_id == product.id)
                    | (V2QuotationProduct.id == product.source_v2_quotation_product_id),
                )
            )
            if line_id is None:
                raise DeliveryOriginInvalidError()
            if order is not None:
                handoff = (
                    await self._session.get(V2ProductionHandoff, order.v2_handoff_id)
                    if order.v2_handoff_id is not None
                    else None
                )
                if handoff is None or handoff.v2_quotation_id != payload.v2_quotation_id:
                    raise DeliveryOriginInvalidError()

        movement = await self.apply_movement(
            product=product,
            location=location,
            quantity=-payload.quantity,
            movement_type=MovementType.DELIVERY_OUT,
            reason=payload.reason,
            user_id=user.id,
            user_name=user.display_name,
            production_order_id=order.id if order is not None else None,
            v2_quotation_id=payload.v2_quotation_id,
        )
        return movement, product, location
