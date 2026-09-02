"""Ordenes de produccion: crear, evaluar, arrancar, completar y anular.

La regla que ordena todo el modulo es una sola: **solo ARRANCAR mueve
inventario**. Crear una orden es papeleo —congela que hay que fabricar y
reserva el correlativo— y no toca ni un gramo. Completar y anular tampoco.
Quien crea una orden por equivocacion no ha gastado nada; quien la arranca, si.

De ahi que la validacion cara —hay receta, hay gramos, hay preparado, alcanza
el stock— viva en un motor propio y de solo lectura, `evaluate_readiness`, que
la interfaz puede consultar tantas veces como quiera sin consecuencias. Arrancar
vuelve a ejecutar esa misma comprobacion, pero con los saldos bloqueados y
dentro de la transaccion que consume: entre mirar y descontar puede pasar otra
orden, y lo unico que vale es lo que se ve con el cerrojo puesto.
"""

from __future__ import annotations

import secrets
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import APIError
from app.models.audit import AuditAction
from app.models.inventory import MovementType, StockBalance, StockLocation
from app.models.masters import Product, ProductType, UnitOfMeasure, UomDimension
from app.models.production import (
    ProductionOrder,
    ProductionOrderLine,
    ProductionOrderStatus,
    ProductionReadinessCode,
)
from app.models.quotations import (
    Quotation,
    QuotationItem,
    QuotationPaymentStatus,
    QuotationStatus,
)
from app.models.recipes import Recipe
from app.models.sequence import SequenceType
from app.schemas.auth import AuthenticatedUser
from app.schemas.production import (
    ProductionOrderLineOut,
    ProductionOrderOut,
    ProductionOrderPage,
    ProductionOrderSummaryOut,
    ProductionReadinessOut,
    ReadinessIssueOut,
)
from app.services.audit import AuditRecorder
from app.services.inventory import InventoryService
from app.services.sequences import SequenceService

#: Entidad con la que se firman los eventos de auditoria del modulo.
PRODUCTION_ENTITY = "production_order"

#: Espacio de nombres del advisory lock de idempotencia. Distinto del que usan
#: las preparaciones: dos claves iguales en modulos distintos no deben
#: serializarse entre si.
IDEMPOTENCY_LOCK_NAMESPACE = 90109

#: Unidad en la que la receta expresa el consumo por pieza. No es una eleccion
#: de este modulo: `material_grams_per_piece` ya viene en gramos desde la
#: cotizacion.
REQUIREMENT_UOM = "g"

MAX_PAGE_SIZE = 200


@dataclass(frozen=True)
class ReadinessIssue:
    """Un motivo concreto de bloqueo.

    `production_order_line_id` es nulo en los problemas de existencia, que no
    son de una linea sino del conjunto: dos lineas que piden el mismo preparado
    comparten un unico saldo y un unico veredicto.
    """

    code: ProductionReadinessCode
    production_order_line_id: int | None = None
    quotation_item_id: int | None = None
    prepared_product_id: int | None = None
    prepared_product_name: str | None = None
    required_quantity: Decimal | None = None
    available_quantity: Decimal | None = None
    uom: str | None = None

    def as_detail(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "production_order_line_id": self.production_order_line_id,
            "quotation_item_id": self.quotation_item_id,
            "prepared_product_id": self.prepared_product_id,
            "prepared_product_name": self.prepared_product_name,
            "required_quantity": (
                format(self.required_quantity, "f") if self.required_quantity is not None else None
            ),
            "available_quantity": (
                format(self.available_quantity, "f")
                if self.available_quantity is not None
                else None
            ),
            "uom": self.uom,
        }


@dataclass(frozen=True)
class ProductionReadiness:
    ready: bool
    issues: tuple[ReadinessIssue, ...]


@dataclass(frozen=True)
class MaterialRequirement:
    """Cuanto preparado hace falta, ya convertido a la unidad del saldo."""

    prepared_product_id: int
    quantity: Decimal
    uom_code: str
    line_ids: tuple[int, ...]


# ---------------------------------------------------------------------------
# Errores de dominio
# ---------------------------------------------------------------------------
class ProductionOrderNotFoundError(APIError):
    status_code = 404
    code = "PRODUCTION_ORDER_NOT_FOUND"
    message = "La orden de produccion no existe"


class ProductionOrderQuotationNotConfirmedError(APIError):
    status_code = 409
    code = "PRODUCTION_ORDER_QUOTATION_NOT_CONFIRMED"
    message = "Solo una cotizacion confirmada puede originar una orden de produccion"


class ProductionOrderLocationInvalidError(APIError):
    status_code = 422
    code = "PRODUCTION_ORDER_LOCATION_INVALID"
    message = "La ubicacion de stock no existe o esta desactivada"


class ProductionOrderQuotationNotPaidError(APIError):
    """La cotizacion de origen no consta cobrada. Fase 009H.1.

    Es 409 y no 403 a proposito: no es un problema de permisos sino del estado
    del negocio. Quien lo recibe puede tener todo el permiso del mundo y la
    respuesta seguiria siendo la misma, porque lo que falta es el cobro. Un 403
    mandaria a buscar a un administrador que no puede arreglarlo dando permisos.

    **NULL tambien bloquea.** El eje de cobro admite tres valores y el nulo
    significa «no consta» —lo que hay en todo lo anterior a 009H—, no «pagada».
    Dejarlo pasar volveria la regla inoperante el dia que se escribio: hoy hay
    17 cotizaciones confirmadas en nulo y 2 pagadas. Registrar el cobro de una
    de esas 17 es posible y basta para desbloquearla.
    """

    status_code = 409
    code = "PRODUCTION_ORDER_QUOTATION_NOT_PAID"
    message = "La cotizacion debe estar pagada para iniciar la produccion"


class ProductionOrderNotStartableError(APIError):
    status_code = 409
    code = "PRODUCTION_ORDER_NOT_STARTABLE"
    message = "La orden no esta en un estado que permita arrancarla"


class ProductionOrderNotReadyError(APIError):
    """Falta algo para producir. Lleva el detalle para poder corregirlo."""

    status_code = 409
    code = "PRODUCTION_ORDER_NOT_READY"
    message = "La orden no puede arrancar todavia"

    def __init__(self, issues: Sequence[ReadinessIssue]) -> None:
        super().__init__(details=[issue.as_detail() for issue in issues])


class ProductionOrderNotCompletableError(APIError):
    status_code = 409
    code = "PRODUCTION_ORDER_NOT_COMPLETABLE"
    message = "Solo una orden arrancada puede completarse"


class ProductionOrderNotCancellableError(APIError):
    status_code = 409
    code = "PRODUCTION_ORDER_NOT_CANCELLABLE"
    message = "Una orden ya arrancada no puede anularse"


def _limit(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE_SIZE))


class ProductionOrderService:
    """Unica autoridad sobre las ordenes de produccion."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRecorder | None = None,
        sequences: SequenceService | None = None,
        inventory: InventoryService | None = None,
    ) -> None:
        self._session = session
        self._audit = audit or AuditRecorder(session)
        self._sequences = sequences or SequenceService(session)
        self._inventory = inventory or InventoryService(session)

    # -- lectura ------------------------------------------------------------
    def _base_query(self) -> Select[tuple[ProductionOrder]]:
        return select(ProductionOrder).options(selectinload(ProductionOrder.lines))

    async def get(self, order_id: int, *, for_update: bool = False) -> ProductionOrder:
        stmt = self._base_query().where(ProductionOrder.id == order_id)
        if for_update:
            # `of` evita que el FOR UPDATE se propague a las lineas cargadas
            # por selectinload, que se piden en otra consulta.
            stmt = stmt.with_for_update(of=ProductionOrder)
        order = await self._session.scalar(stmt)
        if order is None:
            raise ProductionOrderNotFoundError()
        return order

    async def get_by_quotation(self, quotation_id: int) -> ProductionOrder | None:
        return await self._session.scalar(
            self._base_query().where(ProductionOrder.quotation_id == quotation_id)
        )

    async def get_by_qr_token(self, token: str) -> ProductionOrder:
        """Resuelve el token opaco del QR.

        Devuelve el mismo 404 que un id inexistente: distinguir «token
        invalido» de «token valido de otra orden» convertiria el endpoint en un
        oraculo para adivinar tokens.
        """
        order = await self._session.scalar(
            self._base_query().where(ProductionOrder.qr_token == token)
        )
        if order is None:
            raise ProductionOrderNotFoundError()
        return order

    async def list_orders(
        self,
        *,
        status: ProductionOrderStatus | None = None,
        quotation_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ProductionOrder], int]:
        # Los filtros se arman una vez y se aplican a las DOS consultas. Contar
        # sobre una subconsulta que arrastra los `selectinload` del listado
        # funcionaba, pero contar y listar por caminos distintos es como se
        # acaba con un total que no cuadra con las filas.
        condiciones = []
        if status is not None:
            condiciones.append(ProductionOrder.status == status)
        if quotation_id is not None:
            condiciones.append(ProductionOrder.quotation_id == quotation_id)

        stmt = self._base_query()
        total_stmt = select(func.count()).select_from(ProductionOrder)
        for condicion in condiciones:
            stmt = stmt.where(condicion)
            total_stmt = total_stmt.where(condicion)
        total = await self._session.scalar(total_stmt)
        rows = await self._session.execute(
            stmt.order_by(ProductionOrder.created_at.desc(), ProductionOrder.id.desc())
            .limit(_limit(limit))
            .offset(max(0, offset))
        )
        return list(rows.unique().scalars().all()), int(total or 0)

    # -- creacion -----------------------------------------------------------
    async def _lock_idempotency(self, key: str) -> None:
        """Serializa los reintentos que comparten clave de idempotencia."""
        await self._session.execute(
            select(
                func.pg_advisory_xact_lock(
                    IDEMPOTENCY_LOCK_NAMESPACE, int(zlib.crc32(key.encode())) - 2**31
                )
            )
        )

    async def create(
        self,
        *,
        quotation_id: int,
        stock_location_id: int,
        idempotency_key: str | None,
        user: AuthenticatedUser,
    ) -> tuple[ProductionOrder, bool]:
        """Crea la orden de una cotizacion confirmada. Devuelve `(orden, es_nueva)`.

        NO mueve inventario, no crea preparaciones y no toca la quema. Lo unico
        que consume es un correlativo.

        Es idempotente por el hecho: una cotizacion tiene como mucho una orden,
        y pedirla dos veces devuelve la misma. La clave de idempotencia solo
        cubre el reintento de red; la unicidad de verdad la impone el UNIQUE de
        `quotation_id`, porque dos peticiones simultaneas pasan las dos
        cualquier comprobacion previa.
        """
        if idempotency_key:
            await self._lock_idempotency(idempotency_key)
            existing = await self._session.scalar(
                self._base_query().where(ProductionOrder.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return existing, False

        # Bloquear la cotizacion serializa las creaciones del mismo pedido:
        # sin esto, dos peticiones a la vez llegan juntas al INSERT y una muere
        # contra el UNIQUE con un error de integridad, que es una forma fea de
        # decir «ya existe».
        quotation = await self._session.scalar(
            select(Quotation).where(Quotation.id == quotation_id).with_for_update()
        )
        if quotation is None:
            raise ProductionOrderNotFoundError("La cotizacion no existe")
        if quotation.status is not QuotationStatus.CONFIRMED:
            raise ProductionOrderQuotationNotConfirmedError()
        # El eje de pago NO se consulta a proposito (Fase 009H). Producir no
        # exige haber cobrado: son hechos de ejes distintos, y atarlos aqui
        # bloquearia el taller por una gestion administrativa.

        already = await self.get_by_quotation(quotation_id)
        if already is not None:
            return already, False

        location = await self._session.get(StockLocation, stock_location_id)
        if location is None or not location.active:
            raise ProductionOrderLocationInvalidError()

        order = ProductionOrder(
            code=await self._sequences.issue(SequenceType.PRODUCTION_ORDER, user_id=user.id),
            quotation_id=quotation.id,
            stock_location_id=location.id,
            status=ProductionOrderStatus.CREATED,
            idempotency_key=idempotency_key,
            qr_token=secrets.token_urlsafe(32),
            created_by=user.id,
            created_by_name=user.display_name,
        )
        self._session.add(order)
        await self._session.flush()

        items = list(
            (
                await self._session.execute(
                    select(QuotationItem)
                    .where(QuotationItem.quotation_id == quotation.id)
                    .order_by(QuotationItem.sort_order, QuotationItem.id)
                )
            )
            .scalars()
            .all()
        )
        for position, item in enumerate(items):
            self._session.add(await self._line_from_item(order, item, position))
        await self._session.flush()

        self._audit.record_action(
            entity_type=PRODUCTION_ENTITY,
            entity_id=str(order.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": order.code,
                "quotation_id": quotation.id,
                "quotation_code": quotation.code,
                "stock_location_id": location.id,
                "status": order.status.value,
                "line_count": len(items),
            },
        )
        await self._session.refresh(order, ["lines"])
        return order, True

    async def _line_from_item(
        self, order: ProductionOrder, item: QuotationItem, position: int
    ) -> ProductionOrderLine:
        """Copia una linea confirmada y resuelve su material preparado.

        Lo que se copia queda congelado; lo unico que se resuelve contra el
        maestro vivo es `recipe.product_id`, y se resuelve AQUI, al crear, para
        que arrancar no dependa de que la receta siga apuntando manana al mismo
        preparado.
        """
        prepared_product_id: int | None = None
        if item.recipe_id is not None:
            recipe = await self._session.get(Recipe, item.recipe_id)
            if recipe is not None:
                prepared = await self._session.get(Product, recipe.product_id)
                # Solo vale un preparado de verdad. Si la receta apunta a otra
                # cosa, la linea queda sin resolver y el motor lo dira con su
                # codigo, en vez de descontar de un producto cualquiera.
                if prepared is not None and prepared.product_type is ProductType.PREPARED_MATERIAL:
                    prepared_product_id = prepared.id

        required: Decimal | None = None
        if item.quantity is not None and item.material_grams_per_piece is not None:
            required = item.material_grams_per_piece * Decimal(item.quantity)

        return ProductionOrderLine(
            production_order_id=order.id,
            quotation_item_id=item.id,
            sort_order=position,
            product_id=item.product_id,
            product_name_snapshot=item.product_name_snapshot,
            product_internal_reference_snapshot=item.product_internal_reference_snapshot,
            quantity=item.quantity,
            width_snapshot=item.product_width_snapshot,
            height_snapshot=item.product_height_snapshot,
            length_snapshot=item.product_length_snapshot,
            depth_snapshot=item.product_depth_snapshot,
            recipe_id=item.recipe_id,
            recipe_version_id=item.recipe_version_id,
            recipe_version_fingerprint_snapshot=item.recipe_version_fingerprint_snapshot,
            material_grams_per_piece=item.material_grams_per_piece,
            prepared_product_id=prepared_product_id,
            # El requerimiento se guarda en GRAMOS, que es como lo dice la
            # receta. La conversion a la unidad del saldo se hace al evaluar,
            # porque depende del maestro de unidades y ese si puede cambiar.
            required_material_quantity=required,
            required_material_uom=REQUIREMENT_UOM if required is not None else None,
        )

    # -- motor de disponibilidad -------------------------------------------
    async def evaluate_readiness(self, order: ProductionOrder) -> ProductionReadiness:
        """Dice si la orden puede arrancar. **No escribe nada.**

        Es la version consultable: no bloquea saldos, de modo que la interfaz
        puede pedirla cuantas veces quiera. Lo que vale para consumir es la
        evaluacion con cerrojo que hace `start`.
        """
        issues, _ = await self._evaluate(order, lock=False)
        return ProductionReadiness(ready=not issues, issues=tuple(issues))

    async def _evaluate(
        self, order: ProductionOrder, *, lock: bool
    ) -> tuple[list[ReadinessIssue], list[MaterialRequirement]]:
        issues: list[ReadinessIssue] = []

        location = await self._session.get(StockLocation, order.stock_location_id)
        if location is None or not location.active:
            issues.append(ReadinessIssue(code=ProductionReadinessCode.INVALID_STOCK_LOCATION))
            return issues, []

        # ---- 1. Lo que cada linea puede o no puede pedir -------------------
        per_product: dict[int, list[tuple[ProductionOrderLine, Decimal]]] = {}
        for line in order.lines:
            if line.recipe_id is None:
                issues.append(self._line_issue(line, ProductionReadinessCode.MISSING_RECIPE))
                continue
            if line.quantity is None:
                issues.append(self._line_issue(line, ProductionReadinessCode.MISSING_QUANTITY))
                continue
            if line.material_grams_per_piece is None or line.required_material_quantity is None:
                issues.append(
                    self._line_issue(line, ProductionReadinessCode.MISSING_MATERIAL_GRAMS)
                )
                continue
            if line.prepared_product_id is None:
                issues.append(
                    self._line_issue(line, ProductionReadinessCode.PREPARED_PRODUCT_NOT_RESOLVABLE)
                )
                continue

            prepared = await self._session.get(Product, line.prepared_product_id)
            if prepared is None or prepared.base_uom_code is None:
                issues.append(
                    self._line_issue(line, ProductionReadinessCode.PREPARED_PRODUCT_NOT_RESOLVABLE)
                )
                continue

            converted = await self._to_stock_uom(line.required_material_quantity, prepared)
            if converted is None:
                issues.append(
                    self._line_issue(
                        line,
                        ProductionReadinessCode.UNSUPPORTED_UOM_CONVERSION,
                        prepared_product_id=prepared.id,
                        prepared_product_name=prepared.name,
                        required_quantity=line.required_material_quantity,
                        uom=prepared.base_uom_code,
                    )
                )
                continue

            per_product.setdefault(prepared.id, []).append((line, converted))

        # ---- 2. El stock se mira UNA vez por preparado ---------------------
        #
        # Agregar antes de comprobar no es una optimizacion. Dos lineas que
        # piden 100 g y 200 g del mismo barniz, comprobadas por separado contra
        # 250 g de saldo, pasarian las dos y al descontar dejarian el saldo en
        # negativo. Juntas piden 300 y no alcanzan.
        requirements: list[MaterialRequirement] = []
        for prepared_id in sorted(per_product):
            # Orden estable de bloqueo: si una orden toma el barniz A y luego
            # el B mientras otra los toma al reves, se abrazan y la base las
            # mata por deadlock. Ordenando, la segunda simplemente espera.
            entries = per_product[prepared_id]
            prepared = await self._session.get(Product, prepared_id)
            assert prepared is not None and prepared.base_uom_code is not None
            total = sum((amount for _, amount in entries), Decimal(0))

            balance_stmt = select(StockBalance).where(
                StockBalance.product_id == prepared_id,
                StockBalance.location_id == order.stock_location_id,
            )
            if lock:
                balance_stmt = balance_stmt.with_for_update()
            balance = await self._session.scalar(balance_stmt)

            if balance is None:
                # Nunca hubo existencia aqui. Se distingue de «hay pero no
                # alcanza» porque no es el mismo problema: uno se arregla
                # preparando, el otro tambien, pero quien lo lee necesita saber
                # que ese material no se ha preparado nunca.
                issues.append(
                    ReadinessIssue(
                        code=ProductionReadinessCode.PREPARED_STOCK_MISSING,
                        prepared_product_id=prepared_id,
                        prepared_product_name=prepared.name,
                        required_quantity=total,
                        available_quantity=Decimal(0),
                        uom=prepared.base_uom_code,
                    )
                )
                continue
            if balance.quantity < total:
                issues.append(
                    ReadinessIssue(
                        code=ProductionReadinessCode.INSUFFICIENT_STOCK,
                        prepared_product_id=prepared_id,
                        prepared_product_name=prepared.name,
                        required_quantity=total,
                        available_quantity=balance.quantity,
                        uom=prepared.base_uom_code,
                    )
                )
                continue

            requirements.append(
                MaterialRequirement(
                    prepared_product_id=prepared_id,
                    quantity=total,
                    uom_code=prepared.base_uom_code,
                    line_ids=tuple(line.id for line, _ in entries),
                )
            )

        return issues, requirements

    @staticmethod
    def _line_issue(
        line: ProductionOrderLine,
        code: ProductionReadinessCode,
        *,
        prepared_product_id: int | None = None,
        prepared_product_name: str | None = None,
        required_quantity: Decimal | None = None,
        uom: str | None = None,
    ) -> ReadinessIssue:
        return ReadinessIssue(
            code=code,
            production_order_line_id=line.id,
            quotation_item_id=line.quotation_item_id,
            prepared_product_id=prepared_product_id,
            prepared_product_name=prepared_product_name,
            required_quantity=required_quantity,
            uom=uom,
        )

    async def _to_stock_uom(self, grams: Decimal, prepared: Product) -> Decimal | None:
        """Pasa un requerimiento en gramos a la unidad base del preparado.

        Devuelve `None` cuando la conversion no es legitima, y ese `None` es la
        parte importante:

        - Si el preparado se lleva en MASA (g, kg...) la conversion es un
          factor fijo del maestro de unidades y se usa ese, sin reinventarlo.
        - Si se lleva en VOLUMEN (ml) **no hay conversion posible aqui**. El
          puente gramos <-> mililitros es `solids_g_per_ml`, y esa cifra es de
          UN lote de preparacion concreto, no del producto: dos lotes del mismo
          barniz con distinta agua tienen concentraciones distintas. Suponer
          1 g = 1 ml, o promediar los lotes, daria un numero presentable y
          falso, y descontaria del almacen una cantidad que nadie decidio.
          Como la orden de produccion todavia no elige lote, se bloquea.
        """
        uom = await self._session.get(UnitOfMeasure, prepared.base_uom_code or "")
        if uom is None or not uom.active:
            return None
        if uom.dimension is not UomDimension.MASS:
            return None
        if uom.factor_to_base <= 0:
            return None
        return grams / uom.factor_to_base

    # -- arranque: el unico punto que mueve inventario ---------------------
    async def _require_paid_quotation(self, order: ProductionOrder) -> None:
        """Exige que la cotizacion de origen conste cobrada. Fase 009H.1.

        Se comprueba ANTES de evaluar disponibilidad, y por tanto antes de
        bloquear una sola fila de saldo: un arranque que va a rechazarse no
        tiene por que retener el inventario mientras lo hace.

        La cotizacion se lee con cerrojo para no cruzarse con quien la esta
        marcando pagada en ese mismo instante. Sin el, dos peticiones
        simultaneas podrian leer «impagada» y «pagada» del mismo estado y el
        resultado dependeria del orden en que el planificador las despierte.
        """
        quotation = await self._session.scalar(
            select(Quotation).where(Quotation.id == order.quotation_id).with_for_update()
        )
        if quotation is None or quotation.payment_status is not QuotationPaymentStatus.PAID:
            raise ProductionOrderQuotationNotPaidError()

    async def start(
        self, order_id: int, *, user: AuthenticatedUser
    ) -> tuple[ProductionOrder, bool]:
        """Consume el material y deja la orden en STARTED. `(orden, consumio_ahora)`.

        Todo ocurre en una sola transaccion y en este orden:

        1. bloquear la orden;
        2. si ya estaba arrancada, salir sin tocar nada;
        3. resolver requerimientos y bloquear los saldos EN ORDEN de producto;
        4. comprobar que alcanzan TODOS antes de descontar ninguno;
        5. descontar y dejar el movimiento que lo prueba;
        6. marcar STARTED.

        El punto 4 es lo que hace la operacion atomica de verdad: si un solo
        material no alcanza, no se ha descontado nada todavia y la excepcion
        deshace la transaccion entera. Nunca queda media orden consumida.

        El punto 2 es la idempotencia por el hecho fisico, no por la peticion:
        pulsar «Arrancar» dos veces no consume el material dos veces, y
        `started_at` sigue diciendo cuando se arranco de verdad.
        """
        order = await self.get(order_id, for_update=True)
        if order.status is ProductionOrderStatus.STARTED:
            return order, False
        if order.status is not ProductionOrderStatus.CREATED:
            raise ProductionOrderNotStartableError()

        await self._require_paid_quotation(order)

        issues, requirements = await self._evaluate(order, lock=True)
        if issues:
            # Nada se ha descontado: los cerrojos se sueltan al deshacer la
            # transaccion y el inventario queda exactamente como estaba.
            raise ProductionOrderNotReadyError(issues)

        location = await self._session.get(StockLocation, order.stock_location_id)
        assert location is not None
        for requirement in requirements:
            prepared = await self._session.get(Product, requirement.prepared_product_id)
            assert prepared is not None
            await self._inventory.apply_movement(
                product=prepared,
                location=location,
                quantity=-requirement.quantity,
                movement_type=MovementType.PRODUCTION_OUT,
                reason=f"Orden de produccion {order.code}",
                user_id=user.id,
                user_name=user.display_name,
                production_order_id=order.id,
            )

        moment = datetime.now(UTC)
        order.status = ProductionOrderStatus.STARTED
        order.started_at = moment
        order.updated_at = moment
        await self._session.flush()

        self._audit.record_changes(
            entity_type=PRODUCTION_ENTITY,
            entity_id=str(order.id),
            changes={
                "status": (ProductionOrderStatus.CREATED.value, order.status.value),
                "started_at": (None, moment.isoformat()),
            },
            user_id=user.id,
            user_display_name=user.display_name,
        )
        self._audit.record_action(
            entity_type=PRODUCTION_ENTITY,
            entity_id=str(order.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": order.code,
                "transition": "START",
                "consumed": [
                    {
                        "prepared_product_id": requirement.prepared_product_id,
                        "quantity": format(requirement.quantity, "f"),
                        "uom": requirement.uom_code,
                    }
                    for requirement in requirements
                ],
            },
        )
        return order, True

    # -- cierre y anulacion -------------------------------------------------
    async def complete(
        self, order_id: int, *, user: AuthenticatedUser
    ) -> tuple[ProductionOrder, bool]:
        """Marca la orden como terminada. **No crea producto terminado.**

        Fase 009I no da de alta existencia de producto acabado: no hay reglas
        acordadas sobre en que ubicacion entraria, con que merma ni con que
        valoracion, y una entrada inventada seria peor que ninguna.
        """
        order = await self.get(order_id, for_update=True)
        if order.status is ProductionOrderStatus.COMPLETED:
            return order, False
        if order.status is not ProductionOrderStatus.STARTED:
            raise ProductionOrderNotCompletableError()

        moment = datetime.now(UTC)
        order.status = ProductionOrderStatus.COMPLETED
        order.completed_at = moment
        order.updated_at = moment
        await self._session.flush()
        self._audit.record_changes(
            entity_type=PRODUCTION_ENTITY,
            entity_id=str(order.id),
            changes={
                "status": (ProductionOrderStatus.STARTED.value, order.status.value),
                "completed_at": (None, moment.isoformat()),
            },
            user_id=user.id,
            user_display_name=user.display_name,
        )
        return order, True

    async def cancel(
        self, order_id: int, *, user: AuthenticatedUser
    ) -> tuple[ProductionOrder, bool]:
        """Anula una orden que aun no ha consumido nada.

        Solo desde CREATED. Una orden arrancada ya gasto material, y anularla
        no lo devuelve: permitirlo dejaria el inventario contando una cosa y el
        documento diciendo otra. Tampoco hay reversion automatica; si hubo un
        error, se corrige con un ajuste de inventario, que deja su propia
        evidencia y su propio responsable.
        """
        order = await self.get(order_id, for_update=True)
        if order.status is ProductionOrderStatus.CANCELLED:
            return order, False
        if order.status is not ProductionOrderStatus.CREATED:
            raise ProductionOrderNotCancellableError()

        moment = datetime.now(UTC)
        order.status = ProductionOrderStatus.CANCELLED
        order.cancelled_at = moment
        order.updated_at = moment
        await self._session.flush()
        self._audit.record_changes(
            entity_type=PRODUCTION_ENTITY,
            entity_id=str(order.id),
            changes={
                "status": (ProductionOrderStatus.CREATED.value, order.status.value),
                "cancelled_at": (None, moment.isoformat()),
            },
            user_id=user.id,
            user_display_name=user.display_name,
        )
        return order, True

    # -- presentacion -------------------------------------------------------
    async def present(self, order: ProductionOrder) -> ProductionOrderOut:
        """Arma la respuesta completa, con disponibilidad recalculada.

        La disponibilidad se calcula aqui, en el backend, y viaja como codigos.
        No se manda al navegador lo necesario para que la deduzca por su cuenta:
        una segunda implementacion de la regla es una segunda regla, y el dia
        que discrepen ganara la que no consume material.
        """
        quotation = await self._session.get(Quotation, order.quotation_id)
        location = await self._session.get(StockLocation, order.stock_location_id)
        prepared = await self._prepared_products(order)
        readiness = await self.evaluate_readiness(order)

        return ProductionOrderOut(
            id=order.id,
            code=order.code,
            status=order.status,
            quotation_id=order.quotation_id,
            quotation_code=quotation.code if quotation else "",
            quotation_customer_name=quotation.customer_name_snapshot if quotation else None,
            quotation_payment_status=quotation.payment_status if quotation else None,
            stock_location_id=order.stock_location_id,
            stock_location_name=location.name if location else "",
            line_count=len(order.lines),
            created_at=order.created_at,
            started_at=order.started_at,
            completed_at=order.completed_at,
            cancelled_at=order.cancelled_at,
            qr_token=order.qr_token,
            lines=[self._present_line(line, prepared) for line in order.lines],
            readiness=ProductionReadinessOut(
                ready=readiness.ready,
                issues=[
                    ReadinessIssueOut.model_validate(issue.as_detail())
                    for issue in readiness.issues
                ],
            ),
        )

    @staticmethod
    def _present_line(
        line: ProductionOrderLine, prepared: dict[int, Product]
    ) -> ProductionOrderLineOut:
        product = prepared.get(line.prepared_product_id or 0)
        return ProductionOrderLineOut(
            id=line.id,
            quotation_item_id=line.quotation_item_id,
            sort_order=line.sort_order,
            product_id=line.product_id,
            product_name=line.product_name_snapshot,
            product_internal_reference=line.product_internal_reference_snapshot,
            quantity=line.quantity,
            width=line.width_snapshot,
            height=line.height_snapshot,
            length=line.length_snapshot,
            depth=line.depth_snapshot,
            recipe_id=line.recipe_id,
            recipe_version_id=line.recipe_version_id,
            material_grams_per_piece=line.material_grams_per_piece,
            prepared_product_id=line.prepared_product_id,
            prepared_product_name=product.name if product else None,
            prepared_product_internal_reference=(product.internal_reference if product else None),
            required_material_quantity=line.required_material_quantity,
            required_material_uom=line.required_material_uom,
        )

    async def present_page(
        self, orders: Sequence[ProductionOrder], *, total: int, limit: int, offset: int
    ) -> ProductionOrderPage:
        """Listado sin disponibilidad: calcularla por fila serian N consultas.

        Quien necesita saber si una orden puede arrancar abre la orden. El
        listado dice estado, origen y fechas, que es lo que se mira de un
        vistazo.
        """
        quotations = await self._quotations_for(orders)
        locations = await self._location_names(orders)
        return ProductionOrderPage(
            items=[
                ProductionOrderSummaryOut(
                    id=order.id,
                    code=order.code,
                    status=order.status,
                    quotation_id=order.quotation_id,
                    quotation_code=(
                        quotations[order.quotation_id].code
                        if order.quotation_id in quotations
                        else ""
                    ),
                    stock_location_id=order.stock_location_id,
                    stock_location_name=locations.get(order.stock_location_id, ""),
                    line_count=len(order.lines),
                    created_at=order.created_at,
                    started_at=order.started_at,
                    completed_at=order.completed_at,
                    cancelled_at=order.cancelled_at,
                )
                for order in orders
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def _quotations_for(self, orders: Iterable[ProductionOrder]) -> dict[int, Quotation]:
        ids = {order.quotation_id for order in orders}
        if not ids:
            return {}
        rows = await self._session.execute(select(Quotation).where(Quotation.id.in_(ids)))
        return {row.id: row for row in rows.scalars().all()}

    async def _location_names(self, orders: Iterable[ProductionOrder]) -> dict[int, str]:
        ids = {order.stock_location_id for order in orders}
        if not ids:
            return {}
        rows = await self._session.execute(select(StockLocation).where(StockLocation.id.in_(ids)))
        return {row.id: row.name for row in rows.scalars().all()}

    async def _prepared_products(self, order: ProductionOrder) -> dict[int, Product]:
        ids = {
            line.prepared_product_id for line in order.lines if line.prepared_product_id is not None
        }
        if not ids:
            return {}
        rows = await self._session.execute(select(Product).where(Product.id.in_(ids)))
        return {row.id: row for row in rows.scalars().all()}


__all__ = [
    "MaterialRequirement",
    "ProductionOrderLocationInvalidError",
    "ProductionOrderNotCancellableError",
    "ProductionOrderNotCompletableError",
    "ProductionOrderNotFoundError",
    "ProductionOrderNotReadyError",
    "ProductionOrderNotStartableError",
    "ProductionOrderQuotationNotConfirmedError",
    "ProductionOrderService",
    "ProductionReadiness",
    "ReadinessIssue",
]
