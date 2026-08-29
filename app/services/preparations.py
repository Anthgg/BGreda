"""Preparar una receta: la transformacion fisica materia prima -> preparado.

## Que hace exactamente

Preparar NO es simular. Cuando el taller mezcla los insumos y les echa agua,
esas materias primas dejan de existir como tales y aparece un material
preparado. Este servicio registra ese hecho en una sola transaccion: descuenta
lo consumido, crea el lote con su costo congelado y da de alta el preparado.

## Lo que este servicio NO hace

No descuenta esmalte porque una cotizacion se pague. Cotizar simula; vender
consume. Son dos hechos distintos y el segundo pertenece a 009H. Mezclarlos
haria que pedir un presupuesto vaciara el almacen.
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.errors import APIError
from app.core.preparations import (
    ComponentShare,
    PreparationError,
    batch_total_cost,
    component_amounts,
    solids_concentration_g_per_ml,
    unit_cost_per_ml,
)
from app.core.recipes import normalize_component_unit_cost_to_grams
from app.models.inventory import MovementType, StockBalance, StockLocation
from app.models.masters import Product, ProductType
from app.models.recipes import (
    PreparationStatus,
    RecipeLine,
    RecipePreparation,
    RecipePreparationLine,
    RecipeVersion,
)
from app.models.sequence import SequenceType
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder
from app.services.inventory import InventoryService
from app.services.sequences import SequenceService

#: Espacio de nombres propio para los advisory locks de idempotencia, distinto
#: del que usan los codigos de maestros de costos.
IDEMPOTENCY_LOCK_NAMESPACE: Final = 9_0032


class PreparationNotFoundError(APIError):
    code = "PREPARATION_NOT_FOUND"
    status_code = 404
    message = "La preparacion no existe"


class PreparationValidationError(APIError):
    code = "PREPARATION_INVALID"
    status_code = 422
    message = "No se puede registrar la preparacion"


@dataclass(frozen=True)
class Shortfall:
    """Un ingrediente que no alcanza, con lo justo para explicarlo."""

    product_id: int
    internal_reference: str
    name: str
    required_g: Decimal
    available_g: Decimal


class InsufficientStockError(APIError):
    code = "PREPARATION_INSUFFICIENT_STOCK"
    status_code = 409
    message = "No hay materia prima suficiente para preparar la receta"

    def __init__(self, shortfalls: Sequence[Shortfall]) -> None:
        super().__init__(
            details=[
                {
                    "product_id": s.product_id,
                    "internal_reference": s.internal_reference,
                    "name": s.name,
                    "required": str(s.required_g),
                    "available": str(s.available_g),
                }
                for s in shortfalls
            ]
        )


class PreparationService:
    """Registra preparaciones fisicas de receta."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder | None = None) -> None:
        self._session = session
        self._audit = audit or AuditRecorder(session)
        self._inventory = InventoryService(session)
        self._sequences = SequenceService(session)

    async def _lock_idempotency(self, key: str) -> None:
        """Serializa las peticiones que comparten clave de idempotencia.

        Sin esto, dos envios simultaneos de la misma preparacion pasarian los
        dos la comprobacion de existencia y uno moriria contra el UNIQUE con un
        error de integridad, que es una forma fea de decir "ya estaba hecho".
        Con el lock, el segundo espera, encuentra la preparacion del primero y
        la devuelve.

        `zlib.crc32` da un entero estable a partir de la clave; un advisory lock
        solo necesita eso, no una identidad criptografica.
        """
        await self._session.execute(
            select(
                func.pg_advisory_xact_lock(
                    IDEMPOTENCY_LOCK_NAMESPACE, int(zlib.crc32(key.encode())) - 2**31
                )
            )
        )

    async def _existing(self, idempotency_key: str) -> RecipePreparation | None:
        return await self._session.scalar(
            select(RecipePreparation).where(RecipePreparation.idempotency_key == idempotency_key)
        )

    async def get(self, preparation_id: int) -> RecipePreparation:
        row = await self._session.scalar(
            select(RecipePreparation)
            .where(RecipePreparation.id == preparation_id)
            .options(
                joinedload(RecipePreparation.prepared_product),
                selectinload(RecipePreparation.lines).joinedload(
                    RecipePreparationLine.component_product
                ),
            )
        )
        if row is None:
            raise PreparationNotFoundError()
        return row

    async def list_preparations(
        self, *, recipe_id: int | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[RecipePreparation], int]:
        stmt = select(RecipePreparation)
        if recipe_id is not None:
            stmt = stmt.join(RecipeVersion).where(RecipeVersion.recipe_id == recipe_id)
        total = await self._session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = await self._session.execute(
            stmt.options(
                joinedload(RecipePreparation.prepared_product),
                selectinload(RecipePreparation.lines).joinedload(
                    RecipePreparationLine.component_product
                ),
            )
            .order_by(RecipePreparation.id.desc())
            .limit(max(1, min(limit, 200)))
            .offset(max(0, offset))
        )
        return list(rows.scalars().unique()), int(total or 0)

    async def prepare(
        self,
        *,
        recipe_version_id: int,
        location_id: int,
        total_dry_weight_g: Decimal,
        water_amount_ml: Decimal,
        final_yield_ml: Decimal,
        idempotency_key: str,
        user: AuthenticatedUser,
    ) -> tuple[RecipePreparation, bool]:
        """Registra una preparacion. Devuelve `(lote, se_creo_ahora)`.

        El orden importa y es el siguiente:

        1. lock por clave de idempotencia y salida temprana si ya existe;
        2. validar receta, destino y ubicacion;
        3. repartir el peso seco entre los componentes;
        4. bloquear los saldos afectados EN ORDEN DE `product_id`;
        5. comprobar que TODOS alcanzan antes de tocar ninguno;
        6. crear el lote y sus lineas con el costo congelado;
        7. movimientos de salida y de entrada.

        El punto 4 fija un orden de bloqueo comun a todas las preparaciones: si
        una toma el caolin y luego el cuarzo mientras otra los toma al reves,
        se abrazan y la base las mata por deadlock. Ordenando, la segunda
        simplemente espera.

        El punto 5 es lo que hace la operacion atomica de verdad: si un solo
        ingrediente no alcanza, no se ha descontado nada todavia.
        """
        await self._lock_idempotency(idempotency_key)
        existing = await self._existing(idempotency_key)
        if existing is not None:
            return existing, False

        # Se cargan receta y lineas de una vez: `RecipeVersion.recipe` y
        # `.lines` son perezosas, y tocarlas despues en contexto async dispara
        # IO sincrono (MissingGreenlet). Aqui se sabe exactamente que hace
        # falta, asi que se pide explicitamente.
        version = await self._session.scalar(
            select(RecipeVersion)
            .where(RecipeVersion.id == recipe_version_id)
            .options(
                joinedload(RecipeVersion.recipe),
                selectinload(RecipeVersion.lines).joinedload(RecipeLine.component_product),
            )
        )
        if version is None:
            raise PreparationNotFoundError("La version de receta no existe")

        recipe = version.recipe
        prepared_product = await self._session.get(Product, recipe.product_id)
        if prepared_product is None:
            raise PreparationNotFoundError("El producto preparado de la receta no existe")
        if prepared_product.product_type is not ProductType.PREPARED_MATERIAL:
            raise PreparationValidationError(
                "La receta debe producir un material preparado, no "
                f"{prepared_product.product_type.value}"
            )

        location = await self._session.get(StockLocation, location_id)
        if location is None:
            raise PreparationNotFoundError("La ubicacion no existe")

        try:
            amounts = component_amounts(
                [
                    ComponentShare(
                        product_id=line.component_product_id,
                        percentage=line.percentage,
                        unit_cost_per_g=normalize_component_unit_cost_to_grams(
                            line.component_product.cost,
                            line.component_product.base_uom_code,
                        ),
                    )
                    for line in version.lines
                ],
                total_dry_weight_g,
            )
            total_cost = batch_total_cost(amounts)
            concentration = solids_concentration_g_per_ml(total_dry_weight_g, final_yield_ml)
            cost_per_ml = unit_cost_per_ml(total_cost, final_yield_ml)
        except PreparationError as error:
            raise PreparationValidationError(str(error)) from error

        # Orden estable de bloqueo: evita el abrazo mortal entre preparaciones
        # que comparten ingredientes.
        ordered = sorted(amounts, key=lambda a: a.product_id)

        components: dict[int, Product] = {}
        shortfalls: list[Shortfall] = []
        for amount in ordered:
            component = await self._session.get(Product, amount.product_id)
            if component is None:
                raise PreparationNotFoundError(
                    f"El insumo {amount.product_id} de la receta no existe"
                )
            components[amount.product_id] = component
            balance = await self._session.scalar(
                select(StockBalance)
                .where(
                    StockBalance.product_id == component.id,
                    StockBalance.location_id == location.id,
                )
                .with_for_update()
            )
            available = balance.quantity if balance is not None else Decimal(0)
            if available < amount.quantity_g:
                shortfalls.append(
                    Shortfall(
                        product_id=component.id,
                        internal_reference=component.internal_reference,
                        name=component.name,
                        required_g=amount.quantity_g,
                        available_g=available,
                    )
                )
        if shortfalls:
            # Nada se ha descontado: los locks se sueltan al deshacer la
            # transaccion y el inventario queda exactamente como estaba.
            raise InsufficientStockError(shortfalls)

        preparation = RecipePreparation(
            code=await self._sequences.issue(SequenceType.PREPARATION, user_id=user.id),
            recipe_version_id=version.id,
            prepared_product_id=prepared_product.id,
            location_id=location.id,
            total_dry_weight_g=total_dry_weight_g,
            water_amount_ml=water_amount_ml,
            final_yield_ml=final_yield_ml,
            solids_g_per_ml=concentration,
            batch_total_cost=total_cost,
            unit_cost_per_ml=cost_per_ml,
            idempotency_key=idempotency_key,
            status=PreparationStatus.COMPLETED,
            created_by=user.id,
            created_by_name=user.display_name,
        )
        self._session.add(preparation)
        await self._session.flush()

        for amount in ordered:
            self._session.add(
                RecipePreparationLine(
                    preparation_id=preparation.id,
                    component_product_id=amount.product_id,
                    quantity_g=amount.quantity_g,
                    unit_cost_snapshot=amount.unit_cost_snapshot,
                    line_cost=amount.line_cost,
                )
            )
            await self._inventory.apply_movement(
                product=components[amount.product_id],
                location=location,
                quantity=-amount.quantity_g,
                movement_type=MovementType.PREPARATION_OUT,
                reason=f"Preparacion {preparation.code}",
                user_id=user.id,
                user_name=user.display_name,
                preparation_id=preparation.id,
            )

        # El preparado entra en su propia unidad base: si el producto se lleva
        # en ml, entran los mililitros rendidos; si se lleva en gramos, los
        # gramos de solidos. Mezclarlo seria contar agua como materia.
        prepared_quantity = (
            final_yield_ml if prepared_product.base_uom_code == "ml" else total_dry_weight_g
        )
        await self._inventory.apply_movement(
            product=prepared_product,
            location=location,
            quantity=prepared_quantity,
            movement_type=MovementType.PREPARATION_IN,
            reason=f"Preparacion {preparation.code}",
            user_id=user.id,
            user_name=user.display_name,
            preparation_id=preparation.id,
        )

        await self._session.flush()
        return preparation, True
