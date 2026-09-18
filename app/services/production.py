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
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import APIError
from app.models.audit import AuditAction, AuditEvent
from app.models.firings import Kiln
from app.models.inventory import MovementType, StockBalance, StockLocation, StockMovement
from app.models.masters import Product, ProductType, UnitOfMeasure, UomDimension
from app.models.production import (
    ProductionConsumption,
    ProductionConsumptionKind,
    ProductionNoteKind,
    ProductionOrder,
    ProductionOrderCommunication,
    ProductionOrderLine,
    ProductionOrderNote,
    ProductionOrderStatus,
    ProductionReadinessCode,
)
from app.models.prototype_quotations import (
    PrototypeQuotation,
    PrototypeQuotationPaymentStatus,
)
from app.models.prototypes import Prototype, PrototypeMaterialLine, PrototypeStatus
from app.models.quotations import (
    Quotation,
    QuotationItem,
    QuotationPaymentStatus,
    QuotationStatus,
)
from app.models.quoter_v2 import (
    V2ProductionHandoff,
    V2Quotation,
    V2QuotationProduct,
    V2QuotationStatus,
)
from app.models.recipes import Recipe
from app.models.sequence import SequenceType
from app.schemas.auth import AuthenticatedUser
from app.schemas.production import (
    ProductionCommunicationCreateIn,
    ProductionCommunicationOut,
    ProductionConsumptionCreateIn,
    ProductionConsumptionOut,
    ProductionConsumptionPage,
    ProductionNoteCreateIn,
    ProductionNoteOut,
    ProductionOrderLineOut,
    ProductionOrderOrigin,
    ProductionOrderOut,
    ProductionOrderPage,
    ProductionOrderSummaryOut,
    ProductionReadinessOut,
    ProductionTimelineEventOut,
    ProductionTimelineEventType,
    ProductionTimelineOut,
    ReadinessIssueOut,
)
from app.services import body_material as body_material_mod
from app.services.audit import AuditRecorder
from app.services.inventory import InventoryService
from app.services.prototypes import (
    PROTOTYPE_ENTITY,
    PrototypeService,
    assert_prototypes_approved,
)
from app.services.sequences import SequenceService

#: Entidad con la que se firman los eventos de auditoria del modulo.
PRODUCTION_ENTITY = "production_order"

#: Espacio de nombres del advisory lock de idempotencia. Distinto del que usan
#: las preparaciones: dos claves iguales en modulos distintos no deben
#: serializarse entre si.
IDEMPOTENCY_LOCK_NAMESPACE = 90109

#: Holgura para el reloj del navegador al fechar una nota: unos minutos por
#: delante no son el futuro, son dos relojes que no coinciden.
NOTE_CLOCK_SKEW = timedelta(minutes=5)

#: Orden de presentacion cuando dos hechos del seguimiento comparten instante.
_TIMELINE_RANK = {
    ProductionTimelineEventType.STATUS: 0,
    ProductionTimelineEventType.CONSUMPTION: 1,
    ProductionTimelineEventType.FIRING_NOTE: 2,
    ProductionTimelineEventType.NOTE: 3,
    ProductionTimelineEventType.COMMUNICATION: 4,
}

#: Unidad en la que la receta expresa el consumo por pieza. No es una eleccion
#: de este modulo: `material_grams_per_piece` ya viene en gramos desde la
#: cotizacion.
REQUIREMENT_UOM = "g"


def _decimal_or_none(value: Any) -> Decimal | None:
    """Lee un numero de un snapshot JSONB, que lo guarda como texto.

    Devuelve `None` ante cualquier cosa que no sea un numero. Un snapshot
    corrupto tiene que dejar la linea sin requerimiento —y por tanto
    bloqueada— en vez de colar un cero que descontaria de menos.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


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


class ProductionOrderPrototypeNotProducibleError(APIError):
    """La muestra ya se fabrico, se completo o se anulo. Fase 009K.4.

    409 y no 403: quien lo recibe puede tener todos los permisos y la respuesta
    seria la misma, porque lo que falla es el estado de la muestra.
    """

    status_code = 409
    code = "PRODUCTION_ORDER_PROTOTYPE_NOT_PRODUCIBLE"
    message = "Solo una muestra sin fabricar puede originar una orden de produccion"


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


class ProductionOrderV2NotSentError(APIError):
    """La cotizacion V2 no ha pasado por «Enviar a produccion». Fase 010I.

    Una orden V2 cuelga del PUENTE de 010H, y sin puente no hay de que colgarla.
    No se crea el puente aqui por su cuenta: enviar a produccion es otra
    decision, con otro permiso y otras comprobaciones —estado, vigencia—, y
    saltarsela desde el taller seria el segundo camino a la fabrica que esta
    fase evita.
    """

    status_code = 409
    code = "PRODUCTION_ORDER_V2_NOT_SENT"
    message = "La cotizacion V2 todavia no se ha enviado a produccion"


class ProductionOrderV2FingerprintMismatchError(APIError):
    """La cotizacion V2 ya no dice lo que decia al pasar a produccion. Fase 010I.

    El puente guarda una copia de la huella comercial. Si no coincide con la de
    la cotizacion, alguien ha tocado un documento emitido por debajo, y fabricar
    a partir de el seria fabricar algo que el cliente no acepto.
    """

    status_code = 409
    code = "PRODUCTION_ORDER_V2_FINGERPRINT_MISMATCH"
    message = "La cotizacion V2 cambio despues de enviarse a produccion"


class ProductionOrderIdempotencyKeyReusedError(APIError):
    """La clave de idempotencia ya se uso para OTRA orden. Fase 010I.

    Una clave identifica un reintento de la misma peticion. Llegar con ella
    pidiendo otra cosa es un error del cliente, y devolver la orden que ya
    tiene esa clave seria darle una orden ajena como si fuera la suya.
    """

    status_code = 409
    code = "PRODUCTION_ORDER_IDEMPOTENCY_KEY_REUSED"
    message = "Esa clave de idempotencia ya se uso para otra orden de produccion"


class ProductionConsumptionOrderNotV2Error(APIError):
    """El consumo explicito es solo de ordenes V2. Fase 010I.

    Una orden Legacy o de muestra ya descuenta su material al ARRANCAR, por
    receta o por las lineas de la muestra. Registrarle ademas consumos a mano
    descontaria el mismo material dos veces.
    """

    status_code = 409
    code = "PRODUCTION_CONSUMPTION_ORDER_NOT_V2"
    message = "Solo una orden de una cotizacion V2 registra consumos uno a uno"


class ProductionOrderNotConsumableError(APIError):
    """La orden esta finalizada o anulada. Fase 010I.

    Se consume en INICIO y en EN PROCESO. Una orden cerrada no gasta mas
    material, y una anulada no deberia haber gastado ninguno.
    """

    status_code = 409
    code = "PRODUCTION_ORDER_NOT_CONSUMABLE"
    message = "La orden esta finalizada o anulada y ya no admite consumos"


class ProductionConsumptionKeyReusedError(APIError):
    """La clave de idempotencia ya es de OTRO consumo. Fase 010I.

    Una clave identifica los reintentos de UN consumo. Llegar con ella pidiendo
    otro material u otra cantidad es un error del cliente: devolver el consumo
    que ya la tiene seria decir «hecho» sobre algo que no se hizo.
    """

    status_code = 409
    code = "PRODUCTION_CONSUMPTION_KEY_REUSED"
    message = "Esa clave de idempotencia ya se uso para otro consumo"


class ProductionConsumptionMaterialInvalidError(APIError):
    status_code = 422
    code = "PRODUCTION_CONSUMPTION_MATERIAL_INVALID"
    message = "El material no existe o esta desactivado"


class ProductionConsumptionLineInvalidError(APIError):
    """La pieza indicada no es de la cotizacion de esta orden. Fase 010I."""

    status_code = 422
    code = "PRODUCTION_CONSUMPTION_LINE_INVALID"
    message = "La pieza indicada no pertenece a la cotizacion de esta orden"


class ProductionOrderHasConsumptionsError(APIError):
    """Anular una orden que ya gasto material. Fase 010I, decision D1.

    La orden V2 puede consumir en INICIO, antes de arrancar, y anular desde
    INICIO no preguntaba si ya se habia descontado algo. Ahora se bloquea: anular
    no devuelve a los sacos lo que ya se uso, y fingir que si convertiria el
    inventario en una opinion. Si hubo un error, se corrige con un ajuste de
    inventario, que deja su propia evidencia y su propio responsable.
    """

    status_code = 409
    code = "PRODUCTION_ORDER_HAS_CONSUMPTIONS"
    message = "La orden ya tiene material consumido y no puede anularse"


class ProductionOrderNotStartableError(APIError):
    status_code = 409
    code = "PRODUCTION_ORDER_NOT_STARTABLE"
    message = "La orden no esta en un estado que permita arrancarla"


class ProductionOrderConsumptionMissingError(APIError):
    """Finalizar una orden V2 sin el material real que su cotizacion planifico.

    Fase 010I, decision D3. No es «cero consumos, no se puede»: una orden cuya
    cotizacion no planifico material inventariable termina sin consumir nada.
    Lo que se exige es que cada CLASE de material inventariable planificada
    —pasta, esmalte— tenga al menos un consumo real. El detalle dice cual falta.
    """

    status_code = 409
    code = "PRODUCTION_ORDER_CONSUMPTION_MISSING"
    message = "Falta registrar el material real antes de finalizar la orden"

    def __init__(self, kinds: Sequence[ProductionConsumptionKind]) -> None:
        super().__init__(details=[{"kind": kind.value} for kind in kinds])


class ProductionOrderNotV2Error(APIError):
    status_code = 409
    code = "PRODUCTION_ORDER_NOT_V2"
    message = "El seguimiento solo admite notas en ordenes de cotizaciones V2"


class ProductionNoteNotAllowedError(APIError):
    """Una nota en una orden anulada, o una quema antes de arrancar."""

    status_code = 409
    code = "PRODUCTION_NOTE_NOT_ALLOWED"
    message = "La orden no admite esta nota en su estado actual"


class ProductionNoteKeyReusedError(APIError):
    status_code = 409
    code = "PRODUCTION_NOTE_KEY_REUSED"
    message = "Esa clave ya registro otra nota distinta"


class ProductionNoteKilnInvalidError(APIError):
    status_code = 422
    code = "PRODUCTION_NOTE_KILN_INVALID"
    message = "El horno no existe o esta inactivo"


class ProductionNoteOccurredAtInvalidError(APIError):
    """La fecha de lo ocurrido es del futuro, o de antes de crear la orden."""

    status_code = 422
    code = "PRODUCTION_NOTE_OCCURRED_AT_INVALID"
    message = "La fecha debe estar entre la creacion de la orden y ahora"


class ProductionCommunicationKeyReusedError(APIError):
    status_code = 409
    code = "PRODUCTION_COMMUNICATION_KEY_REUSED"
    message = "Esa clave ya registro otra comunicacion distinta"


class ProductionCommunicationSentAtInvalidError(APIError):
    """El aviso dice haberse hecho en el futuro, o antes de existir la orden."""

    status_code = 422
    code = "PRODUCTION_COMMUNICATION_SENT_AT_INVALID"
    message = "La fecha del aviso debe estar entre la creacion de la orden y ahora"


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
        prototypes: PrototypeService | None = None,
    ) -> None:
        self._session = session
        self._audit = audit or AuditRecorder(session)
        self._sequences = sequences or SequenceService(session)
        self._inventory = inventory or InventoryService(session)
        # Fase 009K. Produccion no conoce el dominio de prototipos: solo le
        # pregunta si las muestras vigentes de ese pedido estan aprobadas.
        self._prototypes = prototypes or PrototypeService(
            session, self._audit, self._sequences, self._inventory
        )

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
        v2_quotation_id: int | None = None,
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
        if v2_quotation_id is not None:
            # A traves del puente: la orden V2 no guarda la cotizacion, guarda su
            # puente, que es lo unico que la base deja enlazar.
            condiciones.append(
                ProductionOrder.v2_handoff_id.in_(
                    select(V2ProductionHandoff.id).where(
                        V2ProductionHandoff.v2_quotation_id == v2_quotation_id
                    )
                )
            )

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
            linea = await self._line_from_item(order, item, position)
            # Fase 009K.4: `quotation_item_id` paso a ser anulable para que
            # quepan las lineas de una orden de MUESTRA. Una linea de
            # cotizacion sin item seguiria siendo un error, y la base ya no
            # puede impedirlo —no sabe de que origen es la orden—, asi que se
            # afirma aqui, donde si se sabe.
            assert linea.quotation_item_id is not None, (
                "una linea de orden de cotizacion siempre copia un item confirmado"
            )
            self._session.add(linea)
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

    async def get_by_v2_handoff(self, handoff_id: int) -> ProductionOrder | None:
        return await self._session.scalar(
            self._base_query().where(ProductionOrder.v2_handoff_id == handoff_id)
        )

    async def create_for_v2_quotation(
        self,
        v2_quotation_id: int,
        *,
        stock_location_id: int,
        idempotency_key: str | None,
        user: AuthenticatedUser,
    ) -> tuple[ProductionOrder, bool]:
        """Crea la orden de una cotizacion V2 ya enviada a produccion. Fase 010I.

        Devuelve `(orden, es_nueva)`. Es papeleo, igual que la de una cotizacion
        Legacy: reserva el correlativo, fija el almacen por defecto y no toca ni
        un gramo de inventario. En una orden V2 ni siquiera ARRANCAR descuenta:
        el consumo es un registro explicito de lo que el taller gasto de verdad.

        Idempotente por el hecho, en tres capas:

        1. la clave de idempotencia serializa los reintentos que la comparten;
        2. el bloqueo del PUENTE serializa cualquier creacion para la misma
           cotizacion, traiga la clave que traiga: la segunda encuentra la orden
           de la primera y la devuelve;
        3. el UNIQUE de `v2_handoff_id` es la garantia final, por si una via que
           no tomara el bloqueo llegara primero.

        No se copian lineas: la cotizacion V2 confirmada ya es inmutable y su
        huella esta congelada en el puente. Lo que se comprueba es justo eso, que
        la cotizacion siga diciendo lo que decia al pasar a produccion.
        """
        if idempotency_key:
            await self._lock_idempotency(idempotency_key)
            existing = await self._session.scalar(
                self._base_query().where(ProductionOrder.idempotency_key == idempotency_key)
            )
            if existing is not None:
                # La clave es de un reintento de ESTA peticion. Si trae la de
                # otra orden —otro origen—, devolverla seria entregar una orden
                # ajena como si fuera la pedida.
                if not await self._is_order_of_v2_quotation(existing, v2_quotation_id):
                    raise ProductionOrderIdempotencyKeyReusedError()
                return existing, False

        handoff = await self._session.scalar(
            select(V2ProductionHandoff)
            .where(V2ProductionHandoff.v2_quotation_id == v2_quotation_id)
            .with_for_update()
        )
        if handoff is None:
            if await self._session.get(V2Quotation, v2_quotation_id) is None:
                raise ProductionOrderNotFoundError("La cotizacion V2 no existe")
            raise ProductionOrderV2NotSentError()

        # La unicidad del ORIGEN manda sobre la clave: pedirla otra vez con otra
        # clave devuelve la misma orden, no una segunda.
        already = await self.get_by_v2_handoff(handoff.id)
        if already is not None:
            return already, False

        quotation = await self._session.get(V2Quotation, handoff.v2_quotation_id)
        assert quotation is not None  # la FK del puente lo garantiza
        if quotation.status is not V2QuotationStatus.CONFIRMED:
            # 010H impide anular una cotizacion con puente, asi que esto no
            # deberia verse nunca. Se afirma igual: fabricar algo que no esta
            # confirmado es exactamente lo que no puede pasar por un descuido.
            raise ProductionOrderQuotationNotConfirmedError()
        if quotation.commercial_fingerprint != handoff.commercial_fingerprint:
            raise ProductionOrderV2FingerprintMismatchError()

        location = await self._session.get(StockLocation, stock_location_id)
        if location is None or not location.active:
            raise ProductionOrderLocationInvalidError()

        order = ProductionOrder(
            code=await self._sequences.issue(SequenceType.PRODUCTION_ORDER, user_id=user.id),
            v2_handoff_id=handoff.id,
            stock_location_id=location.id,
            status=ProductionOrderStatus.CREATED,
            idempotency_key=idempotency_key,
            qr_token=secrets.token_urlsafe(32),
            created_by=user.id,
            created_by_name=user.display_name,
        )
        # SAVEPOINT, como en el puente de 010H: el bloqueo ya serializa, pero el
        # UNIQUE es la garantia final. Si algo llegara primero sin tomar el
        # bloqueo, aqui se devuelve su orden en vez de un 500.
        try:
            async with self._session.begin_nested():
                self._session.add(order)
                await self._session.flush()
        except IntegrityError:
            ganadora = await self.get_by_v2_handoff(handoff.id)
            if ganadora is None:
                raise
            return ganadora, False

        self._audit.record_action(
            entity_type=PRODUCTION_ENTITY,
            entity_id=str(order.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": order.code,
                "v2_quotation_id": quotation.id,
                "v2_quotation_code": quotation.code,
                "v2_handoff_id": handoff.id,
                "stock_location_id": location.id,
                "status": order.status.value,
            },
        )
        await self._session.refresh(order, ["lines"])
        return order, True

    async def _is_order_of_v2_quotation(self, order: ProductionOrder, v2_quotation_id: int) -> bool:
        if order.v2_handoff_id is None:
            return False
        handoff = await self._session.get(V2ProductionHandoff, order.v2_handoff_id)
        return handoff is not None and handoff.v2_quotation_id == v2_quotation_id

    async def get_by_prototype(self, prototype_id: int) -> ProductionOrder | None:
        """La orden de una muestra, si ya se creo."""
        return await self._session.scalar(
            self._base_query().where(ProductionOrder.prototype_id == prototype_id)
        )

    async def create_for_prototype(
        self,
        *,
        prototype: Prototype,
        stock_location_id: int,
        user: AuthenticatedUser,
    ) -> tuple[ProductionOrder, bool]:
        """Crea la orden que fabrica una muestra. Devuelve `(orden, es_nueva)`.

        Igual que su hermana de cotizacion: consume un correlativo y nada mas.
        No mueve inventario, no aprueba nada y no decide si la muestra sirve.

        Y con la misma idempotencia por el hecho: una muestra tiene como mucho
        una orden, y pedirla dos veces devuelve la misma. La unicidad de verdad
        la impone el UNIQUE de `prototype_id`, no esta comprobacion: dos cobros
        simultaneos de la misma cotizacion de prototipo pasarian los dos
        cualquier lectura previa.

        El almacen llega SIEMPRE de fuera y ya validado. No hay ubicacion por
        defecto ni aunque hoy solo exista una: el dia que haya dos, un default
        silencioso descontaria del almacen equivocado sin avisar.
        """
        ya = await self.get_by_prototype(prototype.id)
        if ya is not None:
            return ya, False

        location = await self._session.get(StockLocation, stock_location_id)
        if location is None or not location.active:
            raise ProductionOrderLocationInvalidError()

        order = ProductionOrder(
            code=await self._sequences.issue(SequenceType.PRODUCTION_ORDER, user_id=user.id),
            quotation_id=None,
            prototype_id=prototype.id,
            stock_location_id=location.id,
            status=ProductionOrderStatus.CREATED,
            idempotency_key=None,
            qr_token=secrets.token_urlsafe(32),
            created_by=user.id,
            created_by_name=user.display_name,
        )
        self._session.add(order)
        await self._session.flush()

        # UNA linea: la pieza. Las lineas de una orden de cotizacion son una
        # por producto cotizado; una muestra es una sola pieza, y su material
        # no vive aqui sino en `prototype_material_lines`, donde alguien lo
        # eligio a mano. Sintetizar una receta para que encajara en el modelo
        # de la cotizacion seria inventarse un dato tecnico.
        producto = (
            await self._session.get(Product, prototype.product_id)
            if prototype.product_id is not None
            else None
        )
        self._session.add(
            ProductionOrderLine(
                production_order_id=order.id,
                quotation_item_id=None,
                sort_order=0,
                product_id=prototype.product_id,
                product_name_snapshot=(producto.name if producto else prototype.name),
                product_internal_reference_snapshot=(
                    producto.internal_reference if producto else prototype.code
                ),
                quantity=prototype.quantity,
                # Las medidas se COPIAN, como en la rama de cotizacion. La hoja
                # de taller tiene que decir de que tamano es la pieza, y tiene
                # que seguir diciendo lo mismo dentro de un ano aunque el
                # maestro haya cambiado desde entonces.
                width_snapshot=producto.width if producto else None,
                height_snapshot=producto.height if producto else None,
                length_snapshot=producto.length if producto else None,
                depth_snapshot=producto.depth if producto else None,
            )
        )
        await self._session.flush()

        self._audit.record_action(
            entity_type=PRODUCTION_ENTITY,
            entity_id=str(order.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": order.code,
                "origin": "PROTOTYPE",
                "prototype_id": prototype.id,
                "prototype_code": prototype.code,
                "stock_location_id": location.id,
                "status": order.status.value,
                "line_count": 1,
            },
        )
        await self._session.refresh(order, ["lines"])
        return order, True

    async def create_for_prototype_id(
        self, prototype_id: int, *, stock_location_id: int, user: AuthenticatedUser
    ) -> tuple[ProductionOrder, bool]:
        """Igual que `create_for_prototype`, partiendo del id.

        Solo se le crea orden a una muestra que todavia no se ha fabricado. Una
        arrancada ya gasto su material por el camino que fuera, y darle ahora
        una orden arrancable seria invitar a gastarlo dos veces; una completada
        o anulada no tiene nada pendiente que fabricar.

        Ese limite es tambien lo que impide rellenar hacia atras: ninguna de las
        muestras historicas ya ejecutadas puede recibir una orden por aqui.
        """
        prototype = await self._prototypes.get(prototype_id)
        if prototype.status is not PrototypeStatus.CREATED:
            raise ProductionOrderPrototypeNotProducibleError()
        return await self.create_for_prototype(
            prototype=prototype, stock_location_id=stock_location_id, user=user
        )

    async def _line_from_item(
        self, order: ProductionOrder, item: QuotationItem, position: int
    ) -> ProductionOrderLine:
        """Copia una linea confirmada y resuelve el material que va a consumir.

        Dos origenes, y el orden importa:

        1. El MATERIAL BASE congelado en la cotizacion. Cuando existe, manda:
           el material, la cantidad y la unidad ya los decidio y los congelo
           quien confirmo, y aqui no se vuelve a resolver nada contra el
           maestro. Es lo que hace que el operador no pueda cambiar el material
           de una orden ni por descuido ni a proposito.
        2. El camino legacy, para las lineas confirmadas antes de que el
           material base existiera: se resuelve `recipe.product_id` contra el
           maestro vivo, como se ha hecho siempre. No se les fabrica un
           material base que nadie eligio.
        """
        stored = body_material_mod.stored_selection(item.production_snapshot)
        if stored is not None:
            return self._line_from_body_material(order, item, position, stored)

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

    def _line_from_body_material(
        self,
        order: ProductionOrder,
        item: QuotationItem,
        position: int,
        stored: dict[str, Any],
    ) -> ProductionOrderLine:
        """Deriva la linea del material base congelado en la cotizacion.

        Nada se resuelve contra el maestro: el material, la cantidad por pieza
        y la unidad se copian del snapshot. Si manana el maestro cambia de
        unidad o el preparado cambia de receta, esta orden sigue pidiendo lo
        que se contrato.

        La unidad viaja tal cual, y por eso aqui no hay ninguna conversion:
        la cantidad ya se expreso en la unidad base del material cuando se
        cotizo.
        """
        quantity_per_piece = _decimal_or_none(stored.get("quantity_per_piece"))
        required: Decimal | None = None
        if item.quantity is not None and quantity_per_piece is not None:
            required = quantity_per_piece * Decimal(item.quantity)

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
            # Procedencia del preparado, copiada del snapshot. NULL para una
            # materia prima: no tiene receta, y anotarle una seria inventarla.
            recipe_id=stored.get("recipe_id_used"),
            recipe_version_id=stored.get("recipe_version_id_used"),
            recipe_version_fingerprint_snapshot=stored.get("recipe_version_fingerprint_snapshot"),
            # Solo cuando el material se lleva en gramos. En cualquier otra
            # unidad esta columna mentiria, porque no tiene donde decir cual es.
            material_grams_per_piece=(
                quantity_per_piece if stored.get("uom") == REQUIREMENT_UOM else None
            ),
            prepared_product_id=stored.get("product_id"),
            required_material_quantity=required,
            required_material_uom=stored.get("uom") if required is not None else None,
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

        # Fase 009K.4. Los dos origenes se evaluan distinto porque su material
        # ES distinto: la cotizacion lo deriva de una receta congelada y la
        # muestra lo trae de una lista escrita a mano. Lo que comparten —el
        # almacen, la agregacion por producto, el bloqueo ordenado, el
        # «alcanza para todos o no arranca ninguno»— vive en `_stock_issues`.
        if order.prototype_id is not None:
            return await self._evaluate_prototype(order, lock=lock)

        # Fase 010I. Arrancar una orden V2 NO descuenta nada: su consumo es un
        # registro explicito de lo que el taller gasta de verdad, no la receta
        # entera de golpe. Por eso no hay requerimiento que comprobar aqui, y
        # tampoco receta: la orden V2 no tiene lineas Legacy de las que derivarla.
        if order.v2_handoff_id is not None:
            return issues, []

        # ---- 1. Lo que cada linea puede o no puede pedir -------------------
        per_product: dict[int, list[tuple[ProductionOrderLine, Decimal]]] = {}
        for line in order.lines:
            # Una linea derivada de material base no tiene por que traer
            # receta: una pieza de materia prima directa no se fabrica con
            # ninguna. Lo que hace falta para producir es saber QUE material y
            # CUANTO, y eso es `prepared_product_id` + `required_material_*`.
            # El aviso de receta se reserva para las lineas legacy, que sin
            # ella no tienen de donde sacar el material.
            if line.recipe_id is None and line.prepared_product_id is None:
                issues.append(self._line_issue(line, ProductionReadinessCode.MISSING_RECIPE))
                continue
            if line.quantity is None:
                issues.append(self._line_issue(line, ProductionReadinessCode.MISSING_QUANTITY))
                continue
            if line.required_material_quantity is None:
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

            converted = await self._to_stock_uom(
                line.required_material_quantity, prepared, line.required_material_uom
            )
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

    async def _prototype_lines(self, prototype_id: int) -> list[PrototypeMaterialLine]:
        """Las lineas de material de una muestra, en orden estable."""
        return list(
            (
                await self._session.execute(
                    select(PrototypeMaterialLine)
                    .where(PrototypeMaterialLine.prototype_id == prototype_id)
                    .order_by(PrototypeMaterialLine.sort_order, PrototypeMaterialLine.id)
                )
            )
            .scalars()
            .all()
        )

    async def _evaluate_prototype(
        self, order: ProductionOrder, *, lock: bool
    ) -> tuple[list[ReadinessIssue], list[MaterialRequirement]]:
        """Disponibilidad de una orden que fabrica una muestra.

        El material NO se deriva de ninguna receta: sale de las lineas que
        alguien eligio a mano en el prototipo, que es la autoridad. Aqui solo
        se comprueba que existan, que el producto siga siendo utilizable y que
        el almacen de LA ORDEN tenga saldo suficiente.

        Se agrega por producto antes de comprobar, por el mismo motivo que en
        la rama de cotizacion: dos lineas que piden 100 g y 200 g del mismo
        barro, comprobadas por separado contra 250 g, pasarian las dos y al
        descontar dejarian el saldo en negativo.
        """
        issues: list[ReadinessIssue] = []
        prototype = await self._session.get(Prototype, order.prototype_id)
        if prototype is None:
            issues.append(ReadinessIssue(code=ProductionReadinessCode.PROTOTYPE_MISSING))
            return issues, []

        if prototype.prototype_quotation_id is not None:
            cotizacion = await self._session.get(
                PrototypeQuotation, prototype.prototype_quotation_id
            )
            if (
                cotizacion is None
                or cotizacion.payment_status is not PrototypeQuotationPaymentStatus.PAID
            ):
                issues.append(
                    ReadinessIssue(code=ProductionReadinessCode.PROTOTYPE_QUOTATION_NOT_PAID)
                )

        lineas = await self._prototype_lines(prototype.id)
        if not lineas:
            # Sin materiales no hay nada que descontar, y una muestra que no
            # gasta nada no es una muestra: es una ficha sin llenar.
            issues.append(ReadinessIssue(code=ProductionReadinessCode.MISSING_MATERIAL_LINES))
            return issues, []

        por_producto: dict[int, Decimal] = {}
        for linea in lineas:
            por_producto[linea.product_id] = (
                por_producto.get(linea.product_id, Decimal(0)) + linea.quantity_planned
            )

        requirements: list[MaterialRequirement] = []
        for product_id in sorted(por_producto):
            # Orden estable de bloqueo, como en la rama de cotizacion: dos
            # arranques que tomen los mismos materiales en orden distinto se
            # abrazan y la base los mata por deadlock.
            producto = await self._session.get(Product, product_id)
            total = por_producto[product_id]
            if producto is None or producto.base_uom_code is None:
                issues.append(
                    ReadinessIssue(
                        code=ProductionReadinessCode.PREPARED_PRODUCT_NOT_RESOLVABLE,
                        prepared_product_id=product_id,
                        required_quantity=total,
                    )
                )
                continue

            balance_stmt = select(StockBalance).where(
                StockBalance.product_id == product_id,
                StockBalance.location_id == order.stock_location_id,
            )
            if lock:
                balance_stmt = balance_stmt.with_for_update()
            balance = await self._session.scalar(balance_stmt)

            if balance is None:
                issues.append(
                    ReadinessIssue(
                        code=ProductionReadinessCode.PREPARED_STOCK_MISSING,
                        prepared_product_id=product_id,
                        prepared_product_name=producto.name,
                        required_quantity=total,
                        available_quantity=Decimal(0),
                        uom=producto.base_uom_code,
                    )
                )
                continue
            if balance.quantity < total:
                issues.append(
                    ReadinessIssue(
                        code=ProductionReadinessCode.INSUFFICIENT_STOCK,
                        prepared_product_id=product_id,
                        prepared_product_name=producto.name,
                        required_quantity=total,
                        available_quantity=balance.quantity,
                        uom=producto.base_uom_code,
                    )
                )
                continue

            requirements.append(
                MaterialRequirement(
                    prepared_product_id=product_id,
                    quantity=total,
                    uom_code=producto.base_uom_code,
                    line_ids=tuple(linea.id for linea in lineas if linea.product_id == product_id),
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

    async def _to_stock_uom(
        self, quantity: Decimal, prepared: Product, source_uom: str | None = None
    ) -> Decimal | None:
        """Pasa un requerimiento a la unidad base del preparado.

        Cuando el requerimiento ya viene EN esa unidad no hay nada que
        convertir, y ese es el caso normal de una linea con material base: la
        cantidad por pieza se expreso desde el principio en la unidad del
        material. Un preparado que el almacen lleva en mililitros se consume en
        mililitros, sin puente ninguno.

        El resto es el camino legacy, donde el requerimiento siempre viene en
        gramos porque `material_grams_per_piece` no sabe decir otra cosa.
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
        if source_uom is not None and source_uom == prepared.base_uom_code:
            return quantity
        uom = await self._session.get(UnitOfMeasure, prepared.base_uom_code or "")
        if uom is None or not uom.active:
            return None
        if uom.dimension is not UomDimension.MASS:
            return None
        if uom.factor_to_base <= 0:
            return None
        # Solo se convierte desde gramos: es lo unico que el camino legacy sabe
        # producir. Una unidad distinta que no coincida con la del preparado no
        # se adivina.
        if source_uom is not None and source_uom != REQUIREMENT_UOM:
            return None
        return quantity / uom.factor_to_base

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

    async def _require_approved_prototypes(self, order: ProductionOrder) -> None:
        """Exige que las muestras vigentes de ese pedido esten aprobadas. Fase 009K.

        Va DESPUES del guardia de pago y ANTES de evaluar disponibilidad: el
        orden importa porque asi el mensaje que recibe quien pulsa habla de lo
        primero que falta, y porque un arranque que va a rechazarse no retiene
        el inventario mientras lo hace.

        Solo cuentan las muestras VIGENTES de cada cadena. Una rechazada con
        sucesora aprobada no bloquea: la decision vigente es la de la sucesora.
        Una cadena cuya vigente esta anulada tampoco: si se creo por error, no
        puede dejar el pedido sin producir para siempre.

        Y no bloquea nada cuando no hay muestras: la produccion normal de una
        cotizacion sin prototipo sigue exactamente igual que antes de 009K.
        """
        await assert_prototypes_approved(self._session, self._prototypes, order)

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

        # Fase 010I. Una orden V2 arranca SIN descontar nada: su material se
        # registra consumo a consumo, con lo que el taller gasto de verdad, y no
        # como la receta entera de golpe. Tampoco le tocan los guardias de la
        # rama Legacy: no hay cotizacion Legacy que cobrar —su puerta fue el
        # puente de 010H— ni muestras que aprobar. Va ANTES de ellos a proposito:
        # si cayera en la rama Legacy, preguntaria por el cobro de una cotizacion
        # que no existe y rechazaria siempre con «no pagada».
        if order.v2_handoff_id is not None:
            return await self._start_v2(order, user=user)

        # Fase 009K.4. Los dos guardias son de la rama de COTIZACION.
        #
        # El de pago porque una orden de muestra nace ya cobrada —se crea
        # dentro del propio cobro— y su comprobacion vive en la evaluacion. El
        # de aprobacion porque exigirle a una muestra estar aprobada para
        # poder fabricarla seria pedirle que se apruebe antes de existir: se
        # aprueba DESPUES, mirandola.
        if order.prototype_id is None:
            await self._require_paid_quotation(order)
            await self._require_approved_prototypes(order)

        issues, requirements = await self._evaluate(order, lock=True)
        if issues:
            # Nada se ha descontado: los cerrojos se sueltan al deshacer la
            # transaccion y el inventario queda exactamente como estaba.
            raise ProductionOrderNotReadyError(issues)

        location = await self._session.get(StockLocation, order.stock_location_id)
        assert location is not None
        prototype = (
            await self._session.get(Prototype, order.prototype_id)
            if order.prototype_id is not None
            else None
        )
        for requirement in requirements:
            prepared = await self._session.get(Product, requirement.prepared_product_id)
            assert prepared is not None
            if prototype is not None:
                # Fase 009K.4. Una muestra sigue saliendo del almacen como
                # `PROTOTYPE_OUT`. Cambiarlo a `PRODUCTION_OUT` porque ahora se
                # arranca desde una orden habria reescrito el significado de
                # todo el historico de inventario: los movimientos anteriores
                # dirian una cosa y los nuevos otra, para el mismo hecho.
                await self._inventory.apply_movement(
                    product=prepared,
                    location=location,
                    quantity=-requirement.quantity,
                    movement_type=MovementType.PROTOTYPE_OUT,
                    reason=f"Prototipo {prototype.code} · orden {order.code}",
                    user_id=user.id,
                    user_name=user.display_name,
                    prototype_id=prototype.id,
                )
            else:
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

        if prototype is not None:
            # Lo REAL se escribe aqui, en la misma transaccion que lo
            # descuenta, igual que hacia el arranque propio de la muestra. Si
            # algo falla despues, la transaccion se deshace entera y la columna
            # se queda nula: no puede haber consumo registrado sin movimiento
            # que lo respalde.
            for linea in await self._prototype_lines(prototype.id):
                linea.quantity_actual = linea.quantity_planned
            # El estado fisico de la muestra acompana al de su orden. No son
            # dos verdades: es la misma, y la orden es quien la manda.
            #
            # Y de paso la muestra anota de que almacen salio su material. El
            # CHECK `started_requires_origin` (0024) lo exige desde antes de
            # esta fase, y con razon: una muestra arrancada que no sabe de
            # donde salio el barro no puede explicar su propio consumo. Ahora
            # ese dato lo decide quien cobra y vive en la orden; copiarlo aqui
            # no es duplicar autoridad, es dejar el hecho escrito donde ya
            # vive `quantity_actual`.
            if prototype.stock_location_id is None:
                prototype.stock_location_id = order.stock_location_id
            if prototype.status is PrototypeStatus.CREATED:
                prototype.status = PrototypeStatus.STARTED
                prototype.started_at = datetime.now(UTC)

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

    async def _start_v2(
        self, order: ProductionOrder, *, user: AuthenticatedUser
    ) -> tuple[ProductionOrder, bool]:
        """INICIO -> EN PROCESO de una orden V2. Fase 010I. **No mueve inventario.**

        La orden ya viene bloqueada y en CREATED: lo comprobo `start`. El almacen
        de la orden se sigue exigiendo valido porque es el almacen por defecto de
        los consumos que vendran; arrancar contra un almacen dado de baja seria
        preparar consumos que despues no podrian registrarse.
        """
        issues, _ = await self._evaluate(order, lock=False)
        if issues:
            raise ProductionOrderNotReadyError(issues)

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
            # `consumed` vacio y dicho: en una orden V2 arrancar no consume, y el
            # historial tiene que poder contestarlo sin adivinar.
            metadata={"code": order.code, "transition": "START", "consumed": []},
        )
        return order, True

    # -- consumo real (Fase 010I) -------------------------------------------
    async def _consumption_by_key(self, key: str) -> ProductionConsumption | None:
        return await self._session.scalar(
            select(ProductionConsumption).where(ProductionConsumption.idempotency_key == key)
        )

    @staticmethod
    def _same_consumption(
        existing: ProductionConsumption,
        *,
        order_id: int,
        location_id: int | None,
        data: ProductionConsumptionCreateIn,
    ) -> bool:
        """¿Es el reintento del MISMO consumo? La nota no cuenta: no mueve stock.

        `location_id` es el almacen EFECTIVO —el pedido o, si no se pidio, el de
        la orden—. Compararlo sin resolver aceptaria un reintento «sin almacen»
        como si fuera un consumo que salio de otro.
        """
        return (
            existing.production_order_id == order_id
            and existing.product_id == data.product_id
            and existing.stock_location_id == location_id
            and existing.quantity == data.quantity
            and existing.kind == data.kind
            and existing.v2_quotation_product_id == data.v2_quotation_product_id
        )

    async def _effective_location_id(
        self, order_id: int, data: ProductionConsumptionCreateIn
    ) -> int | None:
        """El almacen del consumo: el pedido o, si no se pidio, el de la orden."""
        if data.stock_location_id is not None:
            return data.stock_location_id
        orden = await self._session.get(ProductionOrder, order_id)
        return orden.stock_location_id if orden is not None else None

    async def record_consumption(
        self, order_id: int, data: ProductionConsumptionCreateIn, *, user: AuthenticatedUser
    ) -> tuple[ProductionConsumption, bool]:
        """Registra material REAL gastado en una orden V2. `(consumo, es_nuevo)`.

        **Es lo unico que mueve inventario en una orden V2.** No se hace por
        cotizar, confirmar, generar el PDF, enviar a produccion, crear la orden
        ni arrancarla: solo cuando alguien registra que ese material salio.

        Todo en una transaccion y en este orden:

        1. bloqueo consultivo sobre la CLAVE: dos peticiones con la misma clave
           —doble clic, reintento de red— se serializan;
        2. si la clave ya tiene consumo, se devuelve ese y no se toca el stock
           (o 409 si la clave llega pidiendo otra cosa);
        3. bloqueo de la ORDEN: dos consumos de la misma orden se serializan, y
           nadie la anula o la cierra a la vez;
        4. validaciones: orden V2, en INICIO o EN PROCESO, material activo,
           almacen valido, pieza de ESTA cotizacion;
        5. SAVEPOINT con el movimiento y el registro JUNTOS. `apply_movement`
           bloquea el saldo y se niega a dejarlo negativo, asi que dos ordenes
           que piden 700 g de un saldo de 1000 no pueden llevarse las dos. Si el
           UNIQUE de la clave saltara aun asi, el SAVEPOINT deshace TAMBIEN el
           movimiento —envolver solo el registro dejaria un doble descuento— y
           la transaccion sigue utilizable para devolver el consumo ganador.
        """
        await self._lock_idempotency(data.idempotency_key)

        existing = await self._consumption_by_key(data.idempotency_key)
        if existing is not None:
            if not self._same_consumption(
                existing,
                order_id=order_id,
                location_id=await self._effective_location_id(order_id, data),
                data=data,
            ):
                raise ProductionConsumptionKeyReusedError()
            return existing, False

        order = await self.get(order_id, for_update=True)
        location_id = (
            data.stock_location_id
            if data.stock_location_id is not None
            else order.stock_location_id
        )
        if order.v2_handoff_id is None:
            raise ProductionConsumptionOrderNotV2Error()
        if order.status not in (ProductionOrderStatus.CREATED, ProductionOrderStatus.STARTED):
            raise ProductionOrderNotConsumableError()

        product = await self._session.get(Product, data.product_id)
        if product is None or not product.active:
            raise ProductionConsumptionMaterialInvalidError()

        location = await self._session.get(StockLocation, location_id)
        if location is None or not location.active:
            raise ProductionOrderLocationInvalidError()

        if data.v2_quotation_product_id is not None:
            await self._require_line_of_order(order, data.v2_quotation_product_id)

        consumption: ProductionConsumption | None = None
        try:
            async with self._session.begin_nested():
                movement = await self._inventory.apply_movement(
                    product=product,
                    location=location,
                    quantity=-data.quantity,
                    movement_type=MovementType.PRODUCTION_OUT,
                    reason=f"Orden de produccion {order.code} · consumo real",
                    user_id=user.id,
                    user_name=user.display_name,
                    production_order_id=order.id,
                )
                consumption = ProductionConsumption(
                    production_order_id=order.id,
                    v2_quotation_product_id=data.v2_quotation_product_id,
                    product_id=product.id,
                    stock_location_id=location.id,
                    kind=data.kind,
                    quantity=data.quantity,
                    # La unidad del SALDO, que es la del movimiento: la cantidad
                    # se registro en ella. `apply_movement` ya exige que exista.
                    uom_code=movement.uom_code,
                    unit_cost_snapshot=product.cost,
                    stock_movement_id=movement.id,
                    idempotency_key=data.idempotency_key,
                    note=data.note,
                    created_by=user.id,
                    created_by_name=user.display_name,
                )
                self._session.add(consumption)
                await self._session.flush()
        except IntegrityError:
            ganador = await self._consumption_by_key(data.idempotency_key)
            if ganador is None:
                raise
            if not self._same_consumption(
                ganador, order_id=order_id, location_id=location_id, data=data
            ):
                raise ProductionConsumptionKeyReusedError() from None
            return ganador, False

        assert consumption is not None
        self._audit.record_action(
            entity_type=PRODUCTION_ENTITY,
            entity_id=str(order.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": order.code,
                "event": "CONSUMPTION",
                "consumption_id": consumption.id,
                "stock_movement_id": consumption.stock_movement_id,
                "product_id": product.id,
                "stock_location_id": location.id,
                "kind": consumption.kind.value,
                "quantity": format(consumption.quantity, "f"),
                "uom": consumption.uom_code,
                "v2_quotation_product_id": consumption.v2_quotation_product_id,
            },
        )
        return consumption, True

    async def _require_line_of_order(self, order: ProductionOrder, line_id: int) -> None:
        """La pieza tiene que ser de la cotizacion V2 de ESTA orden."""
        pertenece = await self._session.scalar(
            select(V2QuotationProduct.id)
            .join(
                V2ProductionHandoff,
                V2ProductionHandoff.v2_quotation_id == V2QuotationProduct.v2_quotation_id,
            )
            .where(
                V2QuotationProduct.id == line_id,
                V2ProductionHandoff.id == order.v2_handoff_id,
            )
        )
        if pertenece is None:
            raise ProductionConsumptionLineInvalidError()

    async def list_consumptions(self, order_id: int) -> ProductionConsumptionPage:
        """Los consumos de una orden, del mas antiguo al mas reciente."""
        await self.get(order_id)  # 404 si la orden no existe
        filas = (
            (
                await self._session.execute(
                    select(ProductionConsumption)
                    .where(ProductionConsumption.production_order_id == order_id)
                    .order_by(ProductionConsumption.created_at, ProductionConsumption.id)
                )
            )
            .scalars()
            .all()
        )
        items = await self.present_consumptions(filas)
        return ProductionConsumptionPage(items=items, total=len(items))

    async def present_consumptions(
        self, consumptions: Sequence[ProductionConsumption]
    ) -> list[ProductionConsumptionOut]:
        """Material, almacen y saldo resultante de cada consumo, en tres consultas."""
        if not consumptions:
            return []
        productos = {
            fila.id: fila
            for fila in (
                await self._session.scalars(
                    select(Product).where(Product.id.in_({c.product_id for c in consumptions}))
                )
            ).all()
        }
        almacenes = {
            fila.id: fila
            for fila in (
                await self._session.scalars(
                    select(StockLocation).where(
                        StockLocation.id.in_({c.stock_location_id for c in consumptions})
                    )
                )
            ).all()
        }
        movimientos = {
            fila.id: fila
            for fila in (
                await self._session.scalars(
                    select(StockMovement).where(
                        StockMovement.id.in_({c.stock_movement_id for c in consumptions})
                    )
                )
            ).all()
        }
        return [
            ProductionConsumptionOut(
                id=c.id,
                production_order_id=c.production_order_id,
                v2_quotation_product_id=c.v2_quotation_product_id,
                product_id=c.product_id,
                product_name=productos[c.product_id].name,
                product_internal_reference=productos[c.product_id].internal_reference,
                stock_location_id=c.stock_location_id,
                stock_location_name=almacenes[c.stock_location_id].name,
                kind=c.kind,
                quantity=c.quantity,
                uom_code=c.uom_code,
                balance_after=movimientos[c.stock_movement_id].balance_after,
                stock_movement_id=c.stock_movement_id,
                note=c.note,
                created_by_name=c.created_by_name,
                created_at=c.created_at,
            )
            for c in consumptions
        ]

    async def _has_consumptions(self, order_id: int) -> bool:
        return (
            await self._session.scalar(
                select(ProductionConsumption.id)
                .where(ProductionConsumption.production_order_id == order_id)
                .limit(1)
            )
        ) is not None

    # -- preparacion para finalizar (Fase 010I, decision D3) -----------------
    async def required_consumption_kinds(
        self, order: ProductionOrder
    ) -> list[ProductionConsumptionKind]:
        """Las clases de material INVENTARIABLE que la cotizacion V2 planifico.

        PASTA si alguna pieza con cantidad lleva un material de cuerpo con peso;
        ESMALTE si alguna pieza con cantidad pide esmalte con material. Un
        material de tipo SERVICIO no se inventaria y no cuenta: exigir su
        consumo seria exigir un movimiento de stock que no puede existir.

        Se mira la CLASE y no el material concreto: el taller puede usar otra
        pasta u otro esmalte que los cotizados, y eso es un consumo real valido.
        Fuera de las ordenes V2, nada: su cierre sigue como estaba.
        """
        if order.v2_handoff_id is None:
            return []
        piezas = (
            await self._session.scalars(
                select(V2QuotationProduct)
                .join(
                    V2ProductionHandoff,
                    V2ProductionHandoff.v2_quotation_id == V2QuotationProduct.v2_quotation_id,
                )
                .where(
                    V2ProductionHandoff.id == order.v2_handoff_id,
                    V2QuotationProduct.quantity > 0,
                )
            )
        ).all()
        materiales = {
            m
            for pieza in piezas
            for m in (pieza.body_material_id, pieza.glaze_material_id)
            if m is not None
        }
        inventariables = (
            set(
                (
                    await self._session.scalars(
                        select(Product.id).where(
                            Product.id.in_(materiales),
                            Product.product_type != ProductType.SERVICE,
                        )
                    )
                ).all()
            )
            if materiales
            else set()
        )
        requeridas: list[ProductionConsumptionKind] = []
        if any(
            pieza.body_material_id in inventariables
            and pieza.body_unit_weight is not None
            and pieza.body_unit_weight > 0
            for pieza in piezas
        ):
            requeridas.append(ProductionConsumptionKind.BODY)
        if any(
            pieza.requires_glaze and pieza.glaze_material_id in inventariables for pieza in piezas
        ):
            requeridas.append(ProductionConsumptionKind.GLAZE)
        return requeridas

    async def pending_consumption_kinds(
        self, order: ProductionOrder
    ) -> list[ProductionConsumptionKind]:
        """Las clases requeridas que aun no tienen ni un consumo real."""
        requeridas = await self.required_consumption_kinds(order)
        if not requeridas:
            return []
        hechas = set(
            (
                await self._session.scalars(
                    select(ProductionConsumption.kind)
                    .where(ProductionConsumption.production_order_id == order.id)
                    .distinct()
                )
            ).all()
        )
        return [kind for kind in requeridas if kind not in hechas]

    # -- notas y quemas (Fase 010I, decision D4) ------------------------------
    async def _note_by_key(self, key: str) -> ProductionOrderNote | None:
        return await self._session.scalar(
            select(ProductionOrderNote).where(ProductionOrderNote.idempotency_key == key)
        )

    @staticmethod
    def _same_note(
        existing: ProductionOrderNote, *, order_id: int, data: ProductionNoteCreateIn
    ) -> bool:
        """¿Es el reintento de la MISMA nota? Todo cuenta, tambien la fecha."""
        return (
            existing.production_order_id == order_id
            and existing.kind == data.kind
            and existing.body == data.body
            and existing.kiln_id == data.kiln_id
            and existing.firing_type == data.firing_type
            and existing.occurred_at == data.occurred_at
        )

    async def add_note(
        self, order_id: int, data: ProductionNoteCreateIn, *, user: AuthenticatedUser
    ) -> tuple[ProductionOrderNote, bool]:
        """Anade una nota o una quema al seguimiento de una orden V2.

        Mismo esquema que el consumo: bloqueo sobre la clave, busqueda por
        clave, bloqueo de la orden, validaciones y SAVEPOINT. No toca inventario.

        Una nota vale en INICIO, EN PROCESO y FINALIZADO —lo que pasa despues de
        terminar tambien es seguimiento—; nunca en una orden anulada. Una quema
        exige la orden arrancada: antes de arrancar no hay piezas que quemar.
        """
        await self._lock_idempotency(data.idempotency_key)

        existing = await self._note_by_key(data.idempotency_key)
        if existing is not None:
            if not self._same_note(existing, order_id=order_id, data=data):
                raise ProductionNoteKeyReusedError()
            return existing, False

        order = await self.get(order_id, for_update=True)
        if order.v2_handoff_id is None:
            raise ProductionOrderNotV2Error()
        if order.status is ProductionOrderStatus.CANCELLED:
            raise ProductionNoteNotAllowedError()
        if (
            data.kind is ProductionNoteKind.FIRING_NOTE
            and order.status is ProductionOrderStatus.CREATED
        ):
            raise ProductionNoteNotAllowedError()

        ahora = datetime.now(UTC)
        occurred_at = data.occurred_at
        if occurred_at > ahora + NOTE_CLOCK_SKEW or occurred_at < order.created_at:
            raise ProductionNoteOccurredAtInvalidError()

        kiln: Kiln | None = None
        if data.kiln_id is not None:
            kiln = await self._session.get(Kiln, data.kiln_id)
            if kiln is None or not kiln.active:
                raise ProductionNoteKilnInvalidError()

        note: ProductionOrderNote | None = None
        try:
            async with self._session.begin_nested():
                note = ProductionOrderNote(
                    production_order_id=order.id,
                    kind=data.kind,
                    body=data.body,
                    kiln_id=kiln.id if kiln else None,
                    kiln_name_snapshot=kiln.name if kiln else None,
                    firing_type=data.firing_type,
                    occurred_at=occurred_at,
                    idempotency_key=data.idempotency_key,
                    created_by=user.id,
                    created_by_name=user.display_name,
                )
                self._session.add(note)
                await self._session.flush()
        except IntegrityError:
            ganadora = await self._note_by_key(data.idempotency_key)
            if ganadora is None:
                raise
            if not self._same_note(ganadora, order_id=order_id, data=data):
                raise ProductionNoteKeyReusedError() from None
            return ganadora, False

        assert note is not None
        self._audit.record_action(
            entity_type=PRODUCTION_ENTITY,
            entity_id=str(order.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": order.code,
                "event": note.kind.value,
                "note_id": note.id,
                "kiln_id": note.kiln_id,
                "firing_type": note.firing_type.value if note.firing_type else None,
                "occurred_at": note.occurred_at.isoformat(),
            },
        )
        return note, True

    @staticmethod
    def present_note(note: ProductionOrderNote) -> ProductionNoteOut:
        return ProductionNoteOut(
            id=note.id,
            production_order_id=note.production_order_id,
            kind=note.kind,
            body=note.body,
            kiln_id=note.kiln_id,
            kiln_name=note.kiln_name_snapshot,
            firing_type=note.firing_type,
            occurred_at=note.occurred_at,
            created_by_name=note.created_by_name,
            created_at=note.created_at,
        )

    # -- comunicaciones con el cliente (Fase 010I, decision D2) ----------------
    async def _communication_by_key(self, key: str) -> ProductionOrderCommunication | None:
        return await self._session.scalar(
            select(ProductionOrderCommunication).where(
                ProductionOrderCommunication.idempotency_key == key
            )
        )

    @staticmethod
    def _same_communication(
        existing: ProductionOrderCommunication,
        *,
        order_id: int,
        data: ProductionCommunicationCreateIn,
    ) -> bool:
        """¿Es el reintento del MISMO aviso? Canal, texto y fecha: todo cuenta."""
        return (
            existing.production_order_id == order_id
            and existing.channel == data.channel
            and existing.message == data.message
            and existing.sent_at == data.sent_at
        )

    async def record_communication(
        self,
        order_id: int,
        data: ProductionCommunicationCreateIn,
        *,
        user: AuthenticatedUser,
    ) -> tuple[ProductionOrderCommunication, bool]:
        """Deja constancia de un aviso al cliente que el taller YA hizo.

        **No envia nada** ni llama a ningun proveedor: WhatsApp es el canal
        declarado. Tampoco cambia el estado de la orden, ni el inventario, ni la
        cotizacion: avisar y fabricar son cosas distintas.

        Vale en CUALQUIER estado. «Puede pasar a recoger» se dice con la orden
        FINALIZADA, y «su pedido se anulo» con la orden anulada: bloquearlos
        dejaria sin registrar justo los avisos que mas importan.

        Mismo esquema que las notas: bloqueo sobre la clave, busqueda por
        clave, bloqueo de la orden, validaciones y SAVEPOINT.
        """
        await self._lock_idempotency(data.idempotency_key)

        existing = await self._communication_by_key(data.idempotency_key)
        if existing is not None:
            if not self._same_communication(existing, order_id=order_id, data=data):
                raise ProductionCommunicationKeyReusedError()
            return existing, False

        order = await self.get(order_id, for_update=True)
        if order.v2_handoff_id is None:
            raise ProductionOrderNotV2Error()

        # La misma ventana que las notas del seguimiento (bloque C): ni antes de
        # que la orden existiera ni en el futuro, salvo el desfase de relojes.
        if data.sent_at > datetime.now(UTC) + NOTE_CLOCK_SKEW or data.sent_at < order.created_at:
            raise ProductionCommunicationSentAtInvalidError()

        communication: ProductionOrderCommunication | None = None
        try:
            async with self._session.begin_nested():
                communication = ProductionOrderCommunication(
                    production_order_id=order.id,
                    channel=data.channel,
                    message=data.message,
                    sent_at=data.sent_at,
                    sent_by=user.id,
                    sent_by_name=user.display_name,
                    idempotency_key=data.idempotency_key,
                )
                self._session.add(communication)
                await self._session.flush()
        except IntegrityError:
            ganadora = await self._communication_by_key(data.idempotency_key)
            if ganadora is None:
                raise
            if not self._same_communication(ganadora, order_id=order_id, data=data):
                raise ProductionCommunicationKeyReusedError() from None
            return ganadora, False

        assert communication is not None
        self._audit.record_action(
            entity_type=PRODUCTION_ENTITY,
            entity_id=str(order.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": order.code,
                "event": "COMMUNICATION",
                "communication_id": communication.id,
                "channel": communication.channel.value,
                "sent_at": communication.sent_at.isoformat(),
            },
        )
        return communication, True

    @staticmethod
    def present_communication(
        communication: ProductionOrderCommunication,
    ) -> ProductionCommunicationOut:
        return ProductionCommunicationOut(
            id=communication.id,
            production_order_id=communication.production_order_id,
            channel=communication.channel,
            message=communication.message,
            sent_at=communication.sent_at,
            sent_by_name=communication.sent_by_name,
            created_at=communication.created_at,
        )

    # -- seguimiento ----------------------------------------------------------
    async def timeline(self, order_id: int) -> ProductionTimelineOut:
        """Todo lo que le paso a la orden, en el orden en que paso.

        No es una tabla: se arma con lo que ya esta guardado —los estados de la
        orden, sus consumos, sus notas, sus comunicaciones—, asi que no puede
        discrepar de ellos.
        Quien hizo cada cambio de estado sale de la auditoria, que ya lo
        registraba; la creacion, de la propia orden.
        """
        order = await self.get(order_id)
        eventos: list[ProductionTimelineEventOut] = [
            ProductionTimelineEventOut(
                type=ProductionTimelineEventType.STATUS,
                occurred_at=order.created_at,
                actor_name=order.created_by_name,
                status=ProductionOrderStatus.CREATED,
            )
        ]
        autores = {
            fila.new_value: fila.user_display_name
            for fila in (
                await self._session.scalars(
                    select(AuditEvent)
                    .where(
                        AuditEvent.entity_type == PRODUCTION_ENTITY,
                        AuditEvent.entity_id == str(order.id),
                        AuditEvent.field == "status",
                    )
                    .order_by(AuditEvent.id)
                )
            ).all()
        }
        for estado, momento in (
            (ProductionOrderStatus.STARTED, order.started_at),
            (ProductionOrderStatus.COMPLETED, order.completed_at),
            (ProductionOrderStatus.CANCELLED, order.cancelled_at),
        ):
            if momento is not None:
                eventos.append(
                    ProductionTimelineEventOut(
                        type=ProductionTimelineEventType.STATUS,
                        occurred_at=momento,
                        actor_name=autores.get(estado.value),
                        status=estado,
                    )
                )

        consumos = (
            await self._session.scalars(
                select(ProductionConsumption).where(
                    ProductionConsumption.production_order_id == order.id
                )
            )
        ).all()
        for consumo in await self.present_consumptions(consumos):
            eventos.append(
                ProductionTimelineEventOut(
                    type=ProductionTimelineEventType.CONSUMPTION,
                    occurred_at=consumo.created_at,
                    actor_name=consumo.created_by_name,
                    consumption=consumo,
                )
            )

        notas = (
            await self._session.scalars(
                select(ProductionOrderNote).where(
                    ProductionOrderNote.production_order_id == order.id
                )
            )
        ).all()
        for nota in notas:
            eventos.append(
                ProductionTimelineEventOut(
                    type=(
                        ProductionTimelineEventType.FIRING_NOTE
                        if nota.kind is ProductionNoteKind.FIRING_NOTE
                        else ProductionTimelineEventType.NOTE
                    ),
                    occurred_at=nota.occurred_at,
                    actor_name=nota.created_by_name,
                    note=self.present_note(nota),
                )
            )

        avisos = (
            await self._session.scalars(
                select(ProductionOrderCommunication).where(
                    ProductionOrderCommunication.production_order_id == order.id
                )
            )
        ).all()
        for aviso in avisos:
            eventos.append(
                ProductionTimelineEventOut(
                    type=ProductionTimelineEventType.COMMUNICATION,
                    occurred_at=aviso.sent_at,
                    actor_name=aviso.sent_by_name,
                    communication=self.present_communication(aviso),
                )
            )

        eventos.sort(key=_timeline_key)
        return ProductionTimelineOut(items=eventos)

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
        # Fase 010I, decision D3. Con la orden bloqueada: registrar un consumo
        # toma el mismo bloqueo, asi que lo que se ve aqui es lo que hay.
        pendientes = await self.pending_consumption_kinds(order)
        if pendientes:
            raise ProductionOrderConsumptionMissingError(pendientes)

        moment = datetime.now(UTC)
        order.status = ProductionOrderStatus.COMPLETED
        order.completed_at = moment
        order.updated_at = moment
        await self._mirror_prototype_status(
            order, PrototypeStatus.STARTED, PrototypeStatus.COMPLETED, moment, user=user
        )
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
        # Fase 010I, decision D1. Una orden V2 puede consumir ya en INICIO, asi
        # que «esta en CREATED» ha dejado de significar «no gasto nada». Se
        # pregunta con la orden bloqueada: registrar un consumo toma el mismo
        # bloqueo, de modo que consumir y anular a la vez no pueden terminar
        # los dos.
        if await self._has_consumptions(order.id):
            raise ProductionOrderHasConsumptionsError()

        moment = datetime.now(UTC)
        order.status = ProductionOrderStatus.CANCELLED
        order.cancelled_at = moment
        order.updated_at = moment
        await self._mirror_prototype_status(
            order, PrototypeStatus.CREATED, PrototypeStatus.CANCELLED, moment, user=user
        )
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

    async def _mirror_prototype_status(
        self,
        order: ProductionOrder,
        desde: PrototypeStatus,
        hasta: PrototypeStatus,
        momento: datetime,
        *,
        user: AuthenticatedUser,
    ) -> None:
        """Lleva a la muestra el estado fisico que acaba de tomar su orden.

        Fase 009K.4. La orden es quien manda sobre lo fisico, pero la muestra
        no puede quedarse contando otra cosa. Y no es cosmetico: aprobar o
        rechazar una muestra exige que este COMPLETED, asi que una orden
        terminada con la muestra todavia en STARTED la dejaria imposible de
        evaluar; y una orden anulada con la muestra viva la dejaria sin forma
        de fabricarse nunca, porque el UNIQUE de `prototype_id` impide crearle
        una segunda orden.

        Solo mueve lo FISICO. La aprobacion es otro eje y no se toca aqui:
        terminar de fabricar algo no es haberlo dado por bueno.
        """
        if order.prototype_id is None:
            return
        prototype = await self._session.get(Prototype, order.prototype_id)
        if prototype is None or prototype.status is not desde:
            # Una muestra que ya esta donde toca —o que llego por su propio
            # camino heredado— no se reescribe.
            return

        prototype.status = hasta
        if hasta is PrototypeStatus.COMPLETED:
            prototype.completed_at = momento
        elif hasta is PrototypeStatus.CANCELLED:
            prototype.cancelled_at = momento
        prototype.updated_at = momento
        self._audit.record_changes(
            entity_type=PROTOTYPE_ENTITY,
            entity_id=str(prototype.id),
            changes={"status": (desde.value, hasta.value)},
            user_id=user.id,
            user_display_name=user.display_name,
        )

    # -- presentacion -------------------------------------------------------
    @staticmethod
    def _origin_fields(
        order: ProductionOrder,
        *,
        quotation: Quotation | None,
        prototype: Prototype | None,
        prototype_quotation: PrototypeQuotation | None,
        v2_quotation: V2Quotation | None = None,
    ) -> dict[str, object]:
        """Los campos de origen de una orden. UN solo sitio que los arma.

        Detalle y listado salen de aqui a proposito: dos lugares decidiendo de
        donde viene una orden son dos respuestas que algun dia se separan, y la
        que se vea primero sera la que mande.

        Lo que si difiere entre los dos es COMO se traen los datos —uno a uno
        para una ficha, en bloque para una pagina—, y por eso la busqueda esta
        fuera de esta funcion y no dentro.

        Fase 010I: cada origen se pregunta por SU campo. Antes lo que no era
        muestra se daba por cotizacion Legacy, y con un tercer origen eso habria
        presentado una orden V2 como Legacy con la cotizacion en nulo.
        """
        vacio: dict[str, object] = {
            "quotation_id": None,
            "quotation_code": None,
            "prototype_id": None,
            "prototype_code": None,
            "prototype_quotation_id": None,
            "prototype_quotation_code": None,
            "v2_quotation_id": None,
            "v2_quotation_code": None,
        }
        if order.v2_handoff_id is not None:
            return {
                **vacio,
                "origin_type": ProductionOrderOrigin.V2_QUOTATION,
                "v2_quotation_id": v2_quotation.id if v2_quotation else None,
                "v2_quotation_code": v2_quotation.code if v2_quotation else None,
            }
        if order.prototype_id is not None:
            return {
                **vacio,
                "origin_type": ProductionOrderOrigin.PROTOTYPE,
                "prototype_id": order.prototype_id,
                "prototype_code": prototype.code if prototype else None,
                "prototype_quotation_id": (prototype_quotation.id if prototype_quotation else None),
                "prototype_quotation_code": (
                    prototype_quotation.code if prototype_quotation else None
                ),
            }
        return {
            **vacio,
            "origin_type": ProductionOrderOrigin.QUOTATION,
            "quotation_id": order.quotation_id,
            "quotation_code": quotation.code if quotation else None,
        }

    async def _v2_quotation_of(self, order: ProductionOrder) -> V2Quotation | None:
        """La cotizacion V2 de una orden, a traves de su puente."""
        if order.v2_handoff_id is None:
            return None
        return await self._session.scalar(
            select(V2Quotation)
            .join(V2ProductionHandoff, V2ProductionHandoff.v2_quotation_id == V2Quotation.id)
            .where(V2ProductionHandoff.id == order.v2_handoff_id)
        )

    async def _v2_quotations_for(self, orders: Iterable[ProductionOrder]) -> dict[int, V2Quotation]:
        """Las cotizaciones V2 de una pagina, por id de PUENTE, en una consulta."""
        handoff_ids = {order.v2_handoff_id for order in orders if order.v2_handoff_id is not None}
        if not handoff_ids:
            return {}
        filas = await self._session.execute(
            select(V2ProductionHandoff.id, V2Quotation)
            .join(V2Quotation, V2Quotation.id == V2ProductionHandoff.v2_quotation_id)
            .where(V2ProductionHandoff.id.in_(handoff_ids))
        )
        return dict(filas.tuples().all())

    async def _origin(self, order: ProductionOrder) -> dict[str, object]:
        """El origen de UNA orden, para la ficha."""
        muestra = (
            await self._session.get(Prototype, order.prototype_id)
            if order.prototype_id is not None
            else None
        )
        cpr = (
            await self._session.get(PrototypeQuotation, muestra.prototype_quotation_id)
            if muestra is not None and muestra.prototype_quotation_id is not None
            else None
        )
        ctz = (
            await self._session.get(Quotation, order.quotation_id)
            if order.prototype_id is None and order.quotation_id is not None
            else None
        )
        return self._origin_fields(
            order,
            quotation=ctz,
            prototype=muestra,
            prototype_quotation=cpr,
            v2_quotation=await self._v2_quotation_of(order),
        )

    async def _origins_for(self, orders: Sequence[ProductionOrder]) -> dict[int, dict[str, object]]:
        """El origen de TODA una pagina, en tres consultas y no en N.

        El listado es lo que se abre por costumbre; resolverlo orden por orden
        multiplicaba las idas a la base por el tamano de la pagina.
        """
        quotations = await self._quotations_for(orders)
        prototypes = await self._prototypes_for(orders)
        v2_quotations = await self._v2_quotations_for(orders)
        cpr_ids = {
            muestra.prototype_quotation_id
            for muestra in prototypes.values()
            if muestra.prototype_quotation_id is not None
        }
        cprs: dict[int, PrototypeQuotation] = {}
        if cpr_ids:
            filas = await self._session.execute(
                select(PrototypeQuotation).where(PrototypeQuotation.id.in_(cpr_ids))
            )
            cprs = {fila.id: fila for fila in filas.scalars().all()}

        resultado: dict[int, dict[str, object]] = {}
        for order in orders:
            muestra = prototypes.get(order.prototype_id) if order.prototype_id is not None else None
            resultado[order.id] = self._origin_fields(
                order,
                quotation=(
                    quotations.get(order.quotation_id) if order.quotation_id is not None else None
                ),
                prototype=muestra,
                prototype_quotation=(
                    cprs.get(muestra.prototype_quotation_id)
                    if muestra is not None and muestra.prototype_quotation_id is not None
                    else None
                ),
                v2_quotation=(
                    v2_quotations.get(order.v2_handoff_id)
                    if order.v2_handoff_id is not None
                    else None
                ),
            )
        return resultado

    async def present(self, order: ProductionOrder) -> ProductionOrderOut:
        """Arma la respuesta completa, con disponibilidad recalculada.

        La disponibilidad se calcula aqui, en el backend, y viaja como codigos.
        No se manda al navegador lo necesario para que la deduzca por su cuenta:
        una segunda implementacion de la regla es una segunda regla, y el dia
        que discrepen ganara la que no consume material.
        """
        quotation = (
            await self._session.get(Quotation, order.quotation_id)
            if order.quotation_id is not None
            else None
        )
        location = await self._session.get(StockLocation, order.stock_location_id)
        prepared = await self._prepared_products(order)
        readiness = await self.evaluate_readiness(order)
        # Fase 010I. El cliente de una orden V2 es el que quedo congelado en su
        # cotizacion V2 al emitirla, no el del maestro de hoy.
        v2_quotation = await self._v2_quotation_of(order)
        cliente = (
            quotation.customer_name_snapshot
            if quotation
            else v2_quotation.customer_name_snapshot
            if v2_quotation
            else None
        )

        return ProductionOrderOut(
            id=order.id,
            code=order.code,
            status=order.status,
            **await self._origin(order),  # type: ignore[arg-type]
            quotation_customer_name=cliente,
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
            pending_consumption_kinds=(
                await self.pending_consumption_kinds(order)
                if order.status in (ProductionOrderStatus.CREATED, ProductionOrderStatus.STARTED)
                else []
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
        locations = await self._location_names(orders)
        # Fase 009K.4: el origen se arma con el mismo ayudante que usa la
        # ficha. Que la lista dijera una cosa y el detalle otra sobre la misma
        # orden seria peor que no decirlo.
        origins = await self._origins_for(orders)
        return ProductionOrderPage(
            items=[
                ProductionOrderSummaryOut(
                    id=order.id,
                    code=order.code,
                    status=order.status,
                    **origins[order.id],  # type: ignore[arg-type]
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
        ids = {order.quotation_id for order in orders if order.quotation_id is not None}
        if not ids:
            return {}
        rows = await self._session.execute(select(Quotation).where(Quotation.id.in_(ids)))
        return {row.id: row for row in rows.scalars().all()}

    async def _prototypes_for(self, orders: Iterable[ProductionOrder]) -> dict[int, Prototype]:
        ids = {order.prototype_id for order in orders if order.prototype_id is not None}
        if not ids:
            return {}
        rows = await self._session.execute(select(Prototype).where(Prototype.id.in_(ids)))
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
    "ProductionOrderPrototypeNotProducibleError",
    "ProductionOrderQuotationNotConfirmedError",
    "ProductionOrderService",
    "ProductionReadiness",
    "ReadinessIssue",
]


def _timeline_key(evento: ProductionTimelineEventOut) -> tuple[datetime, int, int]:
    """Cronologico; a igual instante, estados antes que hechos, y por id."""
    detalle = evento.consumption or evento.note or evento.communication
    return (evento.occurred_at, _TIMELINE_RANK[evento.type], detalle.id if detalle else 0)
