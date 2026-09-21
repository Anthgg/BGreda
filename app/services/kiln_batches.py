"""Planificacion de hornadas: crear, asignar, quitar, mover, arrancar. Fase 010L.

## Quien garantiza que

- **La base**, que una hornada nunca pase del 100 %: un trigger mantiene su
  volumen asignado por deltas, otro impide escribirlo a mano, y un CHECK lo topa
  en la capacidad. Ver `app/models/kiln_batches.py`.
- **Este servicio**, todo lo que la base no puede saber: que el ciclo coincida,
  que la hornada siga planificada, que la pieza sea de ese pedido, que no se
  asignen mas piezas de las que tiene la linea, que una exclusiva no se comparta.
  Lo comprueba BAJO BLOQUEO y devuelve un error que dice que paso, en vez de
  dejar que la base conteste con una violacion de restriccion.

## Orden de bloqueos

Siempre el mismo, para que dos operaciones no se esperen en cruz:

1. el PADRE —la orden o la carga interna—, que serializa los repartos de las
   mismas piezas: dos personas no pueden asignar a la vez las ultimas 5 tazas;
2. las HORNADAS, en orden ascendente de id: mover A→B y B→A a la vez no se
   interbloquean, porque las dos toman primero la de id menor.

## Lo que este servicio NUNCA toca

La cotizacion y su PDF. Mover un pedido del horno chico al grande no cambia lo
que el cliente pago: el ahorro es margen interno del taller. Nada de aqui lee
ni escribe un importe.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.kiln_batches import (
    BatchCandidate,
    LineCycles,
    Requirement,
    Suggestion,
    assignment_volume,
    available_cm3,
    available_percent,
    fits,
    high_start_violations,
    occupancy_percent,
    suggest,
)
from app.core.quoter_v2_lifecycle import business_date
from app.models.audit import AuditAction
from app.models.firing_quotation_v2 import (
    V2FiringProductionHandoff,
    V2FiringQuotation,
    V2FiringQuotationLine,
)
from app.models.firings import FiringType, Kiln
from app.models.kiln_batches import (
    KILN_BATCH_EDITABLE,
    InternalLoad,
    InternalLoadLine,
    KilnBatch,
    KilnBatchAssignment,
    KilnBatchAssignmentStatus,
    KilnBatchOperation,
    KilnBatchOperationKind,
    KilnBatchSourceKind,
    KilnBatchStatus,
)
from app.models.production import ProductionOrder, ProductionOrderStatus
from app.models.quoter_v2 import V2FiringMode, V2ProductionHandoff, V2Quotation, V2QuotationProduct
from app.models.sequence import SequenceType
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder
from app.services.sequences import SequenceService

KILN_BATCH_ENTITY = "kiln_batch"

#: Espacio de nombres del bloqueo consultivo de las claves de idempotencia de la
#: planificacion. Distinto del de produccion (90109): dos claves iguales en
#: modulos distintos no deben serializarse entre si.
IDEMPOTENCY_LOCK_NAMESPACE = 90110

#: Estados de la orden en los que se puede planificar su quema. Una anulada no
#: se quema, y una terminada ya se quemo.
ORDER_PLANNABLE = frozenset({ProductionOrderStatus.CREATED, ProductionOrderStatus.STARTED})

ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------
class KilnBatchNotFoundError(APIError):
    status_code = 404
    code = "KILN_BATCH_NOT_FOUND"
    message = "La hornada no existe"


class KilnBatchKilnInvalidError(APIError):
    status_code = 422
    code = "KILN_BATCH_KILN_INVALID"
    message = "El horno no existe o esta dado de baja"


class KilnBatchDateInPastError(APIError):
    status_code = 422
    code = "KILN_BATCH_DATE_IN_PAST"
    message = "La fecha de una hornada planificada no puede ser anterior a hoy"


class KilnBatchNotEditableError(APIError):
    status_code = 409
    code = "KILN_BATCH_NOT_EDITABLE"
    message = "La hornada ya no esta planificada: no se le pueden cambiar las piezas"


class KilnBatchVersionConflictError(APIError):
    status_code = 409
    code = "KILN_BATCH_VERSION_CONFLICT"
    message = "La hornada fue modificada por otra persona. Vuelva a cargarla antes de guardar"


class KilnBatchCapacityExceededError(APIError):
    status_code = 409
    code = "KILN_BATCH_CAPACITY_EXCEEDED"
    message = "Esas piezas no caben en la capacidad estimada disponible de la hornada"


class KilnBatchCycleMismatchError(APIError):
    status_code = 422
    code = "KILN_BATCH_CYCLE_MISMATCH"
    message = "Ese pedido no necesita el ciclo de esta hornada"


class KilnBatchExclusiveError(APIError):
    status_code = 409
    code = "KILN_BATCH_EXCLUSIVE"
    message = "La hornada esta reservada en exclusiva para otro pedido"


class KilnBatchExclusiveNeedsEmptyError(APIError):
    status_code = 409
    code = "KILN_BATCH_EXCLUSIVE_NEEDS_EMPTY"
    message = "Un pedido exclusivo solo puede ir a una hornada vacia"


class KilnBatchSourceNotFoundError(APIError):
    status_code = 404
    code = "KILN_BATCH_SOURCE_NOT_FOUND"
    message = "La orden o la carga interna no existe"


class KilnBatchSourceNotPlannableError(APIError):
    status_code = 422
    code = "KILN_BATCH_SOURCE_NOT_PLANNABLE"
    message = (
        "Esa orden no se puede planificar en hornadas: solo las que vienen del "
        "Cotizador V2 o de Solo Quema tienen el volumen de sus piezas congelado"
    )


class KilnBatchSourceClosedError(APIError):
    status_code = 409
    code = "KILN_BATCH_SOURCE_CLOSED"
    message = "La orden esta terminada o anulada: ya no se planifica"


class KilnBatchForeignLineError(APIError):
    status_code = 422
    code = "KILN_BATCH_FOREIGN_LINE"
    message = "Una de las piezas no pertenece a ese pedido"


class KilnBatchLineNotPlannableError(APIError):
    status_code = 422
    code = "KILN_BATCH_LINE_NOT_PLANNABLE"
    message = "Una de las piezas no tiene medidas: no ocupa horno y no se puede asignar"


class KilnBatchTooManyPiecesError(APIError):
    status_code = 409
    code = "KILN_BATCH_TOO_MANY_PIECES"
    message = "Se estan asignando mas piezas de las que quedan por planificar en ese ciclo"


class KilnBatchAssignmentNotFoundError(APIError):
    status_code = 404
    code = "KILN_BATCH_ASSIGNMENT_NOT_FOUND"
    message = "Esa asignacion no existe en esta hornada o ya se quito"


class KilnBatchEmptyError(APIError):
    status_code = 409
    code = "KILN_BATCH_EMPTY"
    message = "Una hornada sin piezas no se enciende"


class KilnBatchLowNotCompletedError(APIError):
    status_code = 409
    code = "KILN_BATCH_LOW_NOT_COMPLETED"
    message = (
        "Hay piezas que necesitan quema baja y todavia no la completaron. "
        "Una pieza sin bizcochar no puede ir a alta"
    )


class KilnBatchMoveInvalidError(APIError):
    status_code = 422
    code = "KILN_BATCH_MOVE_INVALID"
    message = "Solo se mueven piezas entre dos hornadas distintas del mismo ciclo"


class KilnBatchTransitionError(APIError):
    status_code = 409
    code = "KILN_BATCH_TRANSITION_INVALID"
    message = "La hornada no esta en un estado que permita ese paso"


class KilnBatchIdempotencyKeyReusedError(APIError):
    status_code = 409
    code = "KILN_BATCH_IDEMPOTENCY_KEY_REUSED"
    message = "Esa clave de idempotencia ya se uso con otro contenido"


# ---------------------------------------------------------------------------
# Lo que el servicio sabe de un pedido
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanLine:
    """Una linea planificable de un pedido: su volumen ya incluye la separacion."""

    source_kind: KilnBatchSourceKind
    line_id: int
    product_name: str
    quantity: int
    unit_volume_cm3: Decimal

    @property
    def key(self) -> str:
        return f"{self.source_kind.value}:{self.line_id}"


@dataclass(frozen=True)
class PlanSource:
    """Un pedido —orden de produccion o carga interna— visto desde el horno."""

    kind: KilnBatchSourceKind
    production_order_id: int | None
    internal_load_id: int | None
    code: str
    origin_code: str | None
    customer_name: str | None
    firing_mode: V2FiringMode
    needs_low: bool
    needs_high: bool
    glaze_required: bool
    open: bool
    lines: tuple[PlanLine, ...]

    def needs(self, firing_type: FiringType) -> bool:
        return self.needs_low if firing_type is FiringType.LOW else self.needs_high

    def line(self, line_id: int) -> PlanLine | None:
        return next((linea for linea in self.lines if linea.line_id == line_id), None)


@dataclass(frozen=True)
class AssignItem:
    line_id: int
    quantity: int


@dataclass
class CycleProgress:
    """De una linea y un ciclo: cuantas piezas hay, cuantas estan asignadas."""

    required: int
    assigned: int = 0

    @property
    def remaining(self) -> int:
        return max(self.required - self.assigned, 0)


@dataclass(frozen=True)
class AssignmentView:
    id: int
    batch_id: int
    source_kind: KilnBatchSourceKind
    production_order_id: int | None
    internal_load_id: int | None
    line_id: int
    product_name: str
    quantity: int
    unit_volume_cm3: Decimal
    assigned_volume_cm3: Decimal
    firing_mode: V2FiringMode


@dataclass(frozen=True)
class BatchView:
    """Una hornada con sus asignaciones activas, lista para contar."""

    batch: KilnBatch
    assignments: tuple[AssignmentView, ...]
    occupancy_percent: Decimal
    available_percent: Decimal
    available_cm3: Decimal


@dataclass(frozen=True)
class FiringPlan:
    """Como va la planificacion de un pedido, ciclo a ciclo."""

    source: PlanSource
    #: `progress[firing_type][line_id]`
    progress: dict[FiringType, dict[int, CycleProgress]]
    batches: tuple[BatchView, ...]

    def remaining_cm3(self, firing_type: FiringType) -> Decimal:
        total = ZERO
        for linea in self.source.lines:
            avance = self.progress.get(firing_type, {}).get(linea.line_id)
            if avance is not None:
                total += Decimal(avance.remaining) * linea.unit_volume_cm3
        return total


@dataclass
class _Operacion:
    """El registro de idempotencia de una operacion, si traia clave."""

    key: str | None
    fingerprint: str | None
    replay_batch_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def payload_fingerprint(kind: KilnBatchOperationKind, payload: dict[str, Any]) -> str:
    """SHA-256 del contenido CANONICO de una peticion.

    Canonico: claves ordenadas y sin espacios. Dos peticiones que dicen lo mismo
    con otro orden de campos son el mismo reintento.
    """
    texto = json.dumps({"kind": kind.value, **payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(texto.encode()).hexdigest()


class KilnBatchService:
    """Unica autoridad sobre las hornadas y lo que viaja en ellas."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRecorder | None = None,
        sequences: SequenceService | None = None,
    ) -> None:
        self._session = session
        self._audit = audit or AuditRecorder(session)
        self._sequences = sequences or SequenceService(session)

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------
    async def db_now(self) -> datetime:
        instante = await self._session.scalar(select(func.clock_timestamp()))
        assert instante is not None
        return instante

    async def today(self) -> date:
        """Hoy en LIMA. Nunca `date.today()`: el servidor no esta en Lima."""
        return business_date(await self.db_now())

    async def _lock_idempotency(self, key: str) -> None:
        await self._session.execute(
            select(
                func.pg_advisory_xact_lock(
                    IDEMPOTENCY_LOCK_NAMESPACE, int(zlib.crc32(key.encode())) - 2**31
                )
            )
        )

    async def _begin_operation(
        self,
        key: str | None,
        kind: KilnBatchOperationKind,
        payload: dict[str, Any],
    ) -> _Operacion:
        """Reconoce un reintento. Devuelve la hornada a repetir, si lo es."""
        if not key:
            return _Operacion(key=None, fingerprint=None)
        huella = payload_fingerprint(kind, payload)
        await self._lock_idempotency(key)
        previa = await self._session.scalar(
            select(KilnBatchOperation).where(KilnBatchOperation.idempotency_key == key)
        )
        if previa is None:
            return _Operacion(key=key, fingerprint=huella)
        if previa.kind is not kind or previa.payload_fingerprint != huella:
            raise KilnBatchIdempotencyKeyReusedError()
        return _Operacion(key=key, fingerprint=huella, replay_batch_id=previa.batch_id)

    def _record_operation(
        self,
        operacion: _Operacion,
        kind: KilnBatchOperationKind,
        batch_id: int,
        user: AuthenticatedUser,
    ) -> None:
        if operacion.key is None or operacion.fingerprint is None:
            return
        self._session.add(
            KilnBatchOperation(
                idempotency_key=operacion.key,
                kind=kind,
                payload_fingerprint=operacion.fingerprint,
                batch_id=batch_id,
                created_by=user.id,
            )
        )

    def _audit_batch(
        self,
        batch: KilnBatch,
        user: AuthenticatedUser,
        action: AuditAction,
        metadata: dict[str, Any],
    ) -> None:
        self._audit.record_action(
            entity_type=KILN_BATCH_ENTITY,
            entity_id=str(batch.id),
            action=action,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"code": batch.code, **metadata},
        )

    async def _locked_batch(self, batch_id: int) -> KilnBatch:
        batch = await self._session.scalar(
            select(KilnBatch).where(KilnBatch.id == batch_id).with_for_update()
        )
        if batch is None:
            raise KilnBatchNotFoundError()
        return batch

    async def _locked_batches(self, batch_ids: Iterable[int]) -> dict[int, KilnBatch]:
        """Bloquea varias hornadas SIEMPRE en orden ascendente de id."""
        salida: dict[int, KilnBatch] = {}
        for batch_id in sorted(set(batch_ids)):
            salida[batch_id] = await self._locked_batch(batch_id)
        return salida

    @staticmethod
    def _check_version(batch: KilnBatch, expected_version: int | None) -> None:
        if expected_version is not None and batch.version != expected_version:
            raise KilnBatchVersionConflictError()

    @staticmethod
    def _check_editable(batch: KilnBatch) -> None:
        if batch.status not in KILN_BATCH_EDITABLE:
            raise KilnBatchNotEditableError()

    # ------------------------------------------------------------------
    # Pedidos
    # ------------------------------------------------------------------
    async def resolve_order(self, order_id: int, *, lock: bool) -> PlanSource:
        """Una orden de produccion vista desde el horno, con SUS lineas."""
        consulta = select(ProductionOrder).where(ProductionOrder.id == order_id)
        if lock:
            consulta = consulta.with_for_update()
        orden = await self._session.scalar(consulta)
        if orden is None:
            raise KilnBatchSourceNotFoundError()
        abierta = orden.status in ORDER_PLANNABLE

        if orden.v2_handoff_id is not None:
            puente = await self._session.get(V2ProductionHandoff, orden.v2_handoff_id)
            assert puente is not None  # la FK lo garantiza
            cotizacion = await self._session.get(V2Quotation, puente.v2_quotation_id)
            assert cotizacion is not None
            productos = (
                await self._session.scalars(
                    select(V2QuotationProduct)
                    .where(V2QuotationProduct.v2_quotation_id == cotizacion.id)
                    .order_by(V2QuotationProduct.sort_order, V2QuotationProduct.id)
                )
            ).all()
            return PlanSource(
                kind=KilnBatchSourceKind.V2_QUOTATION,
                production_order_id=orden.id,
                internal_load_id=None,
                code=orden.code,
                origin_code=cotizacion.code,
                customer_name=cotizacion.customer_name_snapshot,
                firing_mode=cotizacion.firing_mode,
                needs_low=bool(cotizacion.low_fire_enabled),
                needs_high=bool(cotizacion.high_fire_enabled),
                glaze_required=False,
                open=abierta,
                lines=tuple(
                    PlanLine(
                        source_kind=KilnBatchSourceKind.V2_QUOTATION,
                        line_id=p.id,
                        product_name=p.product_name_snapshot or f"Pieza {p.id}",
                        quantity=p.quantity,
                        unit_volume_cm3=p.unit_volume_cm3,
                    )
                    for p in productos
                ),
            )

        if orden.v2_firing_handoff_id is not None:
            puente_sq = await self._session.get(
                V2FiringProductionHandoff, orden.v2_firing_handoff_id
            )
            assert puente_sq is not None
            servicio = await self._session.get(V2FiringQuotation, puente_sq.v2_firing_quotation_id)
            assert servicio is not None
            lineas = (
                await self._session.scalars(
                    select(V2FiringQuotationLine)
                    .where(V2FiringQuotationLine.v2_firing_quotation_id == servicio.id)
                    .order_by(V2FiringQuotationLine.sort_order, V2FiringQuotationLine.id)
                )
            ).all()
            return PlanSource(
                kind=KilnBatchSourceKind.FIRING_V2,
                production_order_id=orden.id,
                internal_load_id=None,
                code=orden.code,
                origin_code=servicio.code,
                customer_name=servicio.customer_name_snapshot,
                firing_mode=servicio.firing_mode,
                needs_low=servicio.low_fire_enabled,
                needs_high=servicio.high_fire_enabled,
                glaze_required=servicio.glaze_enabled,
                open=abierta,
                lines=tuple(
                    PlanLine(
                        source_kind=KilnBatchSourceKind.FIRING_V2,
                        line_id=linea.id,
                        product_name=linea.product_name_snapshot or f"Pieza {linea.id}",
                        quantity=linea.quantity,
                        unit_volume_cm3=linea.unit_volume_cm3,
                    )
                    for linea in lineas
                ),
            )

        # Legacy o prototipo: no tienen un volumen aprobado con separacion.
        raise KilnBatchSourceNotPlannableError()

    async def resolve_load(self, load_id: int, *, lock: bool) -> PlanSource:
        consulta = select(InternalLoad).where(InternalLoad.id == load_id)
        if lock:
            consulta = consulta.with_for_update()
        carga = await self._session.scalar(consulta)
        if carga is None:
            raise KilnBatchSourceNotFoundError()
        lineas = (
            await self._session.scalars(
                select(InternalLoadLine)
                .where(InternalLoadLine.load_id == carga.id)
                .order_by(InternalLoadLine.sort_order, InternalLoadLine.id)
            )
        ).all()
        return PlanSource(
            kind=KilnBatchSourceKind.INTERNAL,
            production_order_id=None,
            internal_load_id=carga.id,
            code=carga.code,
            origin_code=None,
            customer_name=None,
            firing_mode=V2FiringMode.SHARED,
            needs_low=carga.low_fire_required,
            needs_high=carga.high_fire_required,
            glaze_required=False,
            open=carga.cancelled_at is None,
            lines=tuple(
                PlanLine(
                    source_kind=KilnBatchSourceKind.INTERNAL,
                    line_id=linea.id,
                    product_name=linea.name,
                    quantity=linea.quantity,
                    unit_volume_cm3=linea.unit_volume_cm3,
                )
                for linea in lineas
            ),
        )

    async def _resolve(
        self, *, production_order_id: int | None, internal_load_id: int | None, lock: bool
    ) -> PlanSource:
        if (production_order_id is None) == (internal_load_id is None):
            raise KilnBatchSourceNotFoundError("Indique una orden o una carga interna, no las dos")
        if production_order_id is not None:
            return await self.resolve_order(production_order_id, lock=lock)
        assert internal_load_id is not None
        return await self.resolve_load(internal_load_id, lock=lock)

    @staticmethod
    def _line_column(kind: KilnBatchSourceKind) -> Any:
        if kind is KilnBatchSourceKind.V2_QUOTATION:
            return KilnBatchAssignment.v2_quotation_product_id
        if kind is KilnBatchSourceKind.FIRING_V2:
            return KilnBatchAssignment.v2_firing_quotation_line_id
        return KilnBatchAssignment.internal_load_line_id

    @staticmethod
    def _line_id_of(asignacion: KilnBatchAssignment) -> int:
        valor = (
            asignacion.v2_quotation_product_id
            or asignacion.v2_firing_quotation_line_id
            or asignacion.internal_load_line_id
        )
        assert valor is not None  # el CHECK de coherencia lo garantiza
        return valor

    @staticmethod
    def _parent_filter(source: PlanSource) -> Any:
        if source.production_order_id is not None:
            return KilnBatchAssignment.production_order_id == source.production_order_id
        return KilnBatchAssignment.internal_load_id == source.internal_load_id

    async def _active_of_source(
        self, source: PlanSource
    ) -> list[tuple[KilnBatchAssignment, KilnBatch]]:
        filas = (
            await self._session.execute(
                select(KilnBatchAssignment, KilnBatch)
                .join(KilnBatch, KilnBatch.id == KilnBatchAssignment.batch_id)
                .where(
                    self._parent_filter(source),
                    KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
                )
                .order_by(KilnBatch.scheduled_date, KilnBatch.id, KilnBatchAssignment.id)
            )
        ).all()
        return [(a, b) for a, b in filas]

    async def _progress(self, source: PlanSource) -> dict[FiringType, dict[int, CycleProgress]]:
        """Por ciclo y linea: piezas requeridas y asignadas en hornadas vivas."""
        avance: dict[FiringType, dict[int, CycleProgress]] = {}
        for ciclo in (FiringType.LOW, FiringType.HIGH):
            if not source.needs(ciclo):
                continue
            avance[ciclo] = {
                linea.line_id: CycleProgress(required=linea.quantity)
                for linea in source.lines
                if linea.unit_volume_cm3 > ZERO
            }
        for asignacion, hornada in await self._active_of_source(source):
            por_linea = avance.get(hornada.firing_type)
            if por_linea is None:
                continue
            linea_avance = por_linea.get(self._line_id_of(asignacion))
            if linea_avance is not None:
                linea_avance.assigned += asignacion.quantity
        return avance

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------
    async def _views(self, batches: Sequence[KilnBatch]) -> list[BatchView]:
        """Las hornadas con sus asignaciones activas, en UNA consulta mas."""
        if not batches:
            return []
        filas = (
            await self._session.scalars(
                select(KilnBatchAssignment)
                .where(
                    KilnBatchAssignment.batch_id.in_([b.id for b in batches]),
                    KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
                )
                .order_by(KilnBatchAssignment.id)
            )
        ).all()
        por_hornada: dict[int, list[AssignmentView]] = defaultdict(list)
        for a in filas:
            por_hornada[a.batch_id].append(
                AssignmentView(
                    id=a.id,
                    batch_id=a.batch_id,
                    source_kind=a.source_kind,
                    production_order_id=a.production_order_id,
                    internal_load_id=a.internal_load_id,
                    line_id=self._line_id_of(a),
                    product_name=a.product_name_snapshot,
                    quantity=a.quantity,
                    unit_volume_cm3=a.unit_volume_snapshot_cm3,
                    assigned_volume_cm3=a.assigned_volume_cm3,
                    firing_mode=a.firing_mode,
                )
            )
        return [
            BatchView(
                batch=b,
                assignments=tuple(por_hornada.get(b.id, [])),
                occupancy_percent=occupancy_percent(b.assigned_volume_cm3, b.capacity_snapshot_cm3),
                available_percent=available_percent(b.assigned_volume_cm3, b.capacity_snapshot_cm3),
                available_cm3=available_cm3(b.assigned_volume_cm3, b.capacity_snapshot_cm3),
            )
            for b in batches
        ]

    async def get(self, batch_id: int) -> BatchView:
        batch = await self._session.get(KilnBatch, batch_id, populate_existing=True)
        if batch is None:
            raise KilnBatchNotFoundError()
        (vista,) = await self._views([batch])
        return vista

    def _list_query(
        self,
        *,
        kiln_id: int | None,
        firing_type: FiringType | None,
        status: KilnBatchStatus | None,
        date_from: date | None,
        date_to: date | None,
        source_kind: KilnBatchSourceKind | None,
    ) -> Select[tuple[KilnBatch]]:
        consulta = select(KilnBatch)
        if kiln_id is not None:
            consulta = consulta.where(KilnBatch.kiln_id == kiln_id)
        if firing_type is not None:
            consulta = consulta.where(KilnBatch.firing_type == firing_type)
        if status is not None:
            consulta = consulta.where(KilnBatch.status == status)
        if date_from is not None:
            consulta = consulta.where(KilnBatch.scheduled_date >= date_from)
        if date_to is not None:
            consulta = consulta.where(KilnBatch.scheduled_date <= date_to)
        if source_kind is not None:
            consulta = consulta.where(
                select(KilnBatchAssignment.id)
                .where(
                    KilnBatchAssignment.batch_id == KilnBatch.id,
                    KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
                    KilnBatchAssignment.source_kind == source_kind,
                )
                .exists()
            )
        return consulta

    async def list_batches(
        self,
        *,
        kiln_id: int | None = None,
        firing_type: FiringType | None = None,
        status: KilnBatchStatus | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        source_kind: KilnBatchSourceKind | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[BatchView], int]:
        """Una pagina de hornadas. Dos consultas en total, sean cuantas sean."""
        base = self._list_query(
            kiln_id=kiln_id,
            firing_type=firing_type,
            status=status,
            date_from=date_from,
            date_to=date_to,
            source_kind=source_kind,
        )
        total = await self._session.scalar(select(func.count()).select_from(base.subquery()))
        pagina = (
            await self._session.scalars(
                base.order_by(KilnBatch.scheduled_date, KilnBatch.id).limit(limit).offset(offset)
            )
        ).all()
        return await self._views(pagina), int(total or 0)

    async def plan_for(
        self, *, production_order_id: int | None = None, internal_load_id: int | None = None
    ) -> FiringPlan:
        source = await self._resolve(
            production_order_id=production_order_id,
            internal_load_id=internal_load_id,
            lock=False,
        )
        progreso = await self._progress(source)
        filas = await self._active_of_source(source)
        hornadas: dict[int, KilnBatch] = {}
        for _, hornada in filas:
            hornadas.setdefault(hornada.id, hornada)
        return FiringPlan(
            source=source,
            progress=progreso,
            batches=tuple(await self._views(list(hornadas.values()))),
        )

    # ------------------------------------------------------------------
    # Sugerencias
    # ------------------------------------------------------------------
    async def suggestions(
        self,
        *,
        firing_type: FiringType,
        production_order_id: int | None = None,
        internal_load_id: int | None = None,
    ) -> list[Suggestion]:
        """Hornadas compatibles para lo que falta de un ciclo. Nunca asigna."""
        plan = await self.plan_for(
            production_order_id=production_order_id, internal_load_id=internal_load_id
        )
        source = plan.source
        if not source.open or not source.needs(firing_type):
            return []
        avance = plan.progress.get(firing_type, {})
        pendientes = [
            linea
            for linea in source.lines
            if linea.unit_volume_cm3 > ZERO
            and avance.get(linea.line_id, CycleProgress(0)).remaining > 0
        ]
        if not pendientes:
            return []
        requisito = Requirement(
            firing_type=firing_type.value,
            remaining_cm3=plan.remaining_cm3(firing_type),
            smallest_piece_cm3=min(linea.unit_volume_cm3 for linea in pendientes),
            exclusive=source.firing_mode is V2FiringMode.EXCLUSIVE,
        )
        hoy = await self.today()
        candidatas = (
            await self._session.scalars(
                select(KilnBatch).where(
                    KilnBatch.firing_type == firing_type,
                    KilnBatch.status == KilnBatchStatus.PLANNED,
                    KilnBatch.scheduled_date >= hoy,
                )
            )
        ).all()
        # Una hornada exclusiva de ESTE pedido no le esta cerrada a el.
        propias = {v.batch.id for v in plan.batches}
        return suggest(
            (
                BatchCandidate(
                    batch_id=b.id,
                    kiln_id=b.kiln_id,
                    kiln_name=b.kiln_name_snapshot,
                    firing_type=b.firing_type.value,
                    scheduled_date=b.scheduled_date,
                    editable=b.status in KILN_BATCH_EDITABLE,
                    exclusive=b.exclusive and b.id not in propias,
                    capacity_cm3=b.capacity_snapshot_cm3,
                    # Para un pedido exclusivo, su propia hornada exclusiva cuenta
                    # como vacia de OTROS: puede seguir llenandola.
                    assigned_cm3=(
                        ZERO if requisito.exclusive and b.id in propias else b.assigned_volume_cm3
                    ),
                )
                for b in candidatas
            ),
            requisito,
            hoy,
        )

    # ------------------------------------------------------------------
    # Crear y reprogramar
    # ------------------------------------------------------------------
    async def create(
        self,
        *,
        kiln_id: int,
        firing_type: FiringType,
        scheduled_date: date,
        notes: str | None,
        idempotency_key: str | None,
        user: AuthenticatedUser,
    ) -> tuple[KilnBatch, bool]:
        """Abre una hornada planificada. Congela la capacidad del horno de HOY."""
        payload = {
            "kiln_id": kiln_id,
            "firing_type": firing_type.value,
            "scheduled_date": scheduled_date.isoformat(),
            "notes": notes,
        }
        operacion = await self._begin_operation(
            idempotency_key, KilnBatchOperationKind.CREATE_BATCH, payload
        )
        if operacion.replay_batch_id is not None:
            existente = await self._session.get(KilnBatch, operacion.replay_batch_id)
            assert existente is not None
            return existente, False

        horno = await self._session.get(Kiln, kiln_id)
        if horno is None or not horno.active:
            raise KilnBatchKilnInvalidError()
        if scheduled_date < await self.today():
            raise KilnBatchDateInPastError()

        batch = KilnBatch(
            code=await self._sequences.issue(SequenceType.KILN_BATCH, user_id=user.id),
            kiln_id=horno.id,
            firing_type=firing_type,
            scheduled_date=scheduled_date,
            status=KilnBatchStatus.PLANNED,
            kiln_name_snapshot=horno.name,
            capacity_snapshot_cm3=horno.capacity_volume_cm3,
            notes=notes,
            created_by=user.id,
            created_by_name=user.display_name,
        )
        self._session.add(batch)
        await self._session.flush()
        self._record_operation(operacion, KilnBatchOperationKind.CREATE_BATCH, batch.id, user)
        self._audit_batch(
            batch,
            user,
            AuditAction.CREATE,
            {
                "kiln_id": horno.id,
                "firing_type": firing_type.value,
                "scheduled_date": scheduled_date.isoformat(),
                "capacity_cm3": format(horno.capacity_volume_cm3, "f"),
            },
        )
        await self._session.flush()
        return batch, True

    async def update(
        self,
        batch_id: int,
        *,
        scheduled_date: date | None,
        notes: str | None,
        notes_set: bool,
        expected_version: int,
        user: AuthenticatedUser,
    ) -> KilnBatch:
        batch = await self._locked_batch(batch_id)
        self._check_version(batch, expected_version)
        self._check_editable(batch)
        cambios: dict[str, tuple[Any, Any]] = {}
        if scheduled_date is not None and scheduled_date != batch.scheduled_date:
            if scheduled_date < await self.today():
                raise KilnBatchDateInPastError()
            cambios["scheduled_date"] = (
                batch.scheduled_date.isoformat(),
                scheduled_date.isoformat(),
            )
            batch.scheduled_date = scheduled_date
        if notes_set and notes != batch.notes:
            cambios["notes"] = (batch.notes, notes)
            batch.notes = notes
        if cambios:
            batch.version += 1
            self._audit.record_changes(
                entity_type=KILN_BATCH_ENTITY,
                entity_id=str(batch.id),
                changes=cambios,
                user_id=user.id,
                user_display_name=user.display_name,
            )
            await self._session.flush()
        return batch

    # ------------------------------------------------------------------
    # Asignar
    # ------------------------------------------------------------------
    async def _exclusive_owner(self, batch_id: int) -> tuple[int | None, int | None] | None:
        """De quien es la exclusiva de una hornada, si la tiene."""
        fila = (
            await self._session.execute(
                select(
                    KilnBatchAssignment.production_order_id,
                    KilnBatchAssignment.internal_load_id,
                )
                .where(
                    KilnBatchAssignment.batch_id == batch_id,
                    KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
                )
                .limit(1)
            )
        ).first()
        if fila is None:
            return None
        return fila[0], fila[1]

    async def _check_exclusivity(self, batch: KilnBatch, source: PlanSource) -> None:
        propio = (source.production_order_id, source.internal_load_id)
        dueno = await self._exclusive_owner(batch.id)
        if batch.exclusive and dueno is not None and dueno != propio:
            raise KilnBatchExclusiveError()
        if source.firing_mode is V2FiringMode.EXCLUSIVE:
            otros = await self._session.scalar(
                select(func.count())
                .select_from(KilnBatchAssignment)
                .where(
                    KilnBatchAssignment.batch_id == batch.id,
                    KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
                    ~self._parent_filter(source),
                )
            )
            if otros:
                raise KilnBatchExclusiveNeedsEmptyError()

    async def _place(
        self,
        batch: KilnBatch,
        source: PlanSource,
        items: Sequence[AssignItem],
        user: AuthenticatedUser,
    ) -> list[dict[str, Any]]:
        """Coloca piezas en una hornada YA bloqueada. Valida todo antes de escribir."""
        self._check_editable(batch)
        if not source.open:
            raise KilnBatchSourceClosedError()
        if not source.needs(batch.firing_type):
            raise KilnBatchCycleMismatchError()
        await self._check_exclusivity(batch, source)

        avance = (await self._progress(source)).get(batch.firing_type, {})
        pedidas: dict[int, int] = defaultdict(int)
        for item in items:
            if item.quantity <= 0:
                raise KilnBatchTooManyPiecesError("Cada pieza asignada tiene que ser al menos 1")
            pedidas[item.line_id] += item.quantity

        extra = ZERO
        plan: list[tuple[PlanLine, int]] = []
        for line_id, cantidad in pedidas.items():
            linea = source.line(line_id)
            if linea is None:
                raise KilnBatchForeignLineError()
            if linea.unit_volume_cm3 <= ZERO:
                raise KilnBatchLineNotPlannableError()
            restante = avance.get(line_id, CycleProgress(0)).remaining
            if cantidad > restante:
                raise KilnBatchTooManyPiecesError(
                    f"De «{linea.product_name}» quedan {restante} piezas por planificar "
                    f"en este ciclo y se pidieron {cantidad}"
                )
            extra += assignment_volume(cantidad, linea.unit_volume_cm3)
            plan.append((linea, cantidad))

        # El contador de la base es la verdad: se relee bajo el bloqueo.
        await self._session.refresh(batch, ["assigned_volume_cm3", "exclusive"])
        if not fits(batch.assigned_volume_cm3, batch.capacity_snapshot_cm3, extra):
            libre = available_cm3(batch.assigned_volume_cm3, batch.capacity_snapshot_cm3)
            raise KilnBatchCapacityExceededError(
                f"Esas piezas ocupan {format(extra, 'f')} cm3 y la hornada tiene "
                f"{format(libre, 'f')} cm3 de capacidad estimada disponible"
            )

        columna = self._line_column(source.kind)
        colocadas: list[dict[str, Any]] = []
        for linea, cantidad in plan:
            existente = await self._session.scalar(
                select(KilnBatchAssignment).where(
                    KilnBatchAssignment.batch_id == batch.id,
                    KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
                    columna == linea.line_id,
                )
            )
            if existente is not None:
                existente.quantity += cantidad
                existente.assigned_volume_cm3 = assignment_volume(
                    existente.quantity, existente.unit_volume_snapshot_cm3
                )
            else:
                self._session.add(
                    KilnBatchAssignment(
                        batch_id=batch.id,
                        source_kind=source.kind,
                        production_order_id=source.production_order_id,
                        internal_load_id=source.internal_load_id,
                        v2_quotation_product_id=(
                            linea.line_id
                            if source.kind is KilnBatchSourceKind.V2_QUOTATION
                            else None
                        ),
                        v2_firing_quotation_line_id=(
                            linea.line_id if source.kind is KilnBatchSourceKind.FIRING_V2 else None
                        ),
                        internal_load_line_id=(
                            linea.line_id if source.kind is KilnBatchSourceKind.INTERNAL else None
                        ),
                        quantity=cantidad,
                        unit_volume_snapshot_cm3=linea.unit_volume_cm3,
                        assigned_volume_cm3=assignment_volume(cantidad, linea.unit_volume_cm3),
                        firing_mode=source.firing_mode,
                        product_name_snapshot=linea.product_name,
                        created_by=user.id,
                        created_by_name=user.display_name,
                    )
                )
            colocadas.append({"line": linea.key, "quantity": cantidad})
        await self._flush_capacity()
        # Primero las piezas y despues la reserva: la base no deja marcar como
        # exclusiva una hornada vacia.
        if source.firing_mode is V2FiringMode.EXCLUSIVE:
            batch.exclusive = True
        batch.version += 1
        await self._session.flush()
        return colocadas

    async def _flush_capacity(self) -> None:
        """Vacia la sesion. Si aun asi la base topa la capacidad, se traduce."""
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError as error:
            if "assigned_within_capacity" in str(error.orig):
                raise KilnBatchCapacityExceededError() from error
            raise

    async def assign(
        self,
        batch_id: int,
        *,
        production_order_id: int | None,
        internal_load_id: int | None,
        items: Sequence[AssignItem],
        expected_version: int | None,
        idempotency_key: str | None,
        user: AuthenticatedUser,
    ) -> BatchView:
        """Pone piezas de UN pedido en UNA hornada, de una vez o nada."""
        payload = {
            "batch_id": batch_id,
            "production_order_id": production_order_id,
            "internal_load_id": internal_load_id,
            "items": sorted([[i.line_id, i.quantity] for i in items]),
        }
        operacion = await self._begin_operation(
            idempotency_key, KilnBatchOperationKind.ASSIGN, payload
        )
        if operacion.replay_batch_id is not None:
            return await self.get(operacion.replay_batch_id)

        source = await self._resolve(
            production_order_id=production_order_id,
            internal_load_id=internal_load_id,
            lock=True,
        )
        batch = await self._locked_batch(batch_id)
        self._check_version(batch, expected_version)
        colocadas = await self._place(batch, source, items, user)
        self._record_operation(operacion, KilnBatchOperationKind.ASSIGN, batch.id, user)
        self._audit_batch(
            batch,
            user,
            AuditAction.UPDATE,
            {"transition": "ASSIGN", "source": source.code, "pieces": colocadas},
        )
        await self._session.flush()
        return await self.get(batch.id)

    # ------------------------------------------------------------------
    # Quitar y mover
    # ------------------------------------------------------------------
    async def _take_out(
        self,
        batch: KilnBatch,
        source: PlanSource,
        items: Sequence[AssignItem],
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Saca piezas de una hornada YA bloqueada. Una fila vaciada queda RELEASED."""
        self._check_editable(batch)
        columna = self._line_column(source.kind)
        sacadas: list[dict[str, Any]] = []
        for item in items:
            linea = source.line(item.line_id)
            if linea is None:
                raise KilnBatchForeignLineError()
            fila = await self._session.scalar(
                select(KilnBatchAssignment).where(
                    KilnBatchAssignment.batch_id == batch.id,
                    KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
                    self._parent_filter(source),
                    columna == item.line_id,
                )
            )
            if fila is None:
                raise KilnBatchAssignmentNotFoundError()
            if item.quantity <= 0 or item.quantity > fila.quantity:
                raise KilnBatchTooManyPiecesError(
                    f"En esta hornada hay {fila.quantity} piezas de «{linea.product_name}»"
                )
            if item.quantity == fila.quantity:
                fila.status = KilnBatchAssignmentStatus.RELEASED
                fila.released_at = now
            else:
                fila.quantity -= item.quantity
                fila.assigned_volume_cm3 = assignment_volume(
                    fila.quantity, fila.unit_volume_snapshot_cm3
                )
            sacadas.append({"line": linea.key, "quantity": item.quantity})
        await self._session.flush()
        batch.version += 1
        await self._session.flush()
        await self._session.refresh(batch, ["assigned_volume_cm3", "exclusive"])
        return sacadas

    async def release(
        self,
        batch_id: int,
        *,
        production_order_id: int | None,
        internal_load_id: int | None,
        items: Sequence[AssignItem],
        expected_version: int | None,
        idempotency_key: str | None,
        user: AuthenticatedUser,
    ) -> BatchView:
        """Quita piezas de una hornada planificada. No borra la orden ni la carga."""
        payload = {
            "batch_id": batch_id,
            "production_order_id": production_order_id,
            "internal_load_id": internal_load_id,
            "items": sorted([[i.line_id, i.quantity] for i in items]),
        }
        operacion = await self._begin_operation(
            idempotency_key, KilnBatchOperationKind.RELEASE, payload
        )
        if operacion.replay_batch_id is not None:
            return await self.get(operacion.replay_batch_id)

        source = await self._resolve(
            production_order_id=production_order_id,
            internal_load_id=internal_load_id,
            lock=True,
        )
        batch = await self._locked_batch(batch_id)
        self._check_version(batch, expected_version)
        sacadas = await self._take_out(batch, source, items, await self.db_now())
        self._record_operation(operacion, KilnBatchOperationKind.RELEASE, batch.id, user)
        self._audit_batch(
            batch,
            user,
            AuditAction.UPDATE,
            {"transition": "RELEASE", "source": source.code, "pieces": sacadas},
        )
        await self._session.flush()
        return await self.get(batch.id)

    async def move(
        self,
        *,
        from_batch_id: int,
        to_batch_id: int,
        production_order_id: int | None,
        internal_load_id: int | None,
        items: Sequence[AssignItem],
        idempotency_key: str | None,
        user: AuthenticatedUser,
    ) -> tuple[BatchView, BatchView]:
        """Pasa piezas de una hornada a otra en UNA transaccion: o todo o nada."""
        if from_batch_id == to_batch_id:
            raise KilnBatchMoveInvalidError()
        payload = {
            "from_batch_id": from_batch_id,
            "to_batch_id": to_batch_id,
            "production_order_id": production_order_id,
            "internal_load_id": internal_load_id,
            "items": sorted([[i.line_id, i.quantity] for i in items]),
        }
        operacion = await self._begin_operation(
            idempotency_key, KilnBatchOperationKind.MOVE, payload
        )
        if operacion.replay_batch_id is not None:
            return await self.get(from_batch_id), await self.get(to_batch_id)

        source = await self._resolve(
            production_order_id=production_order_id,
            internal_load_id=internal_load_id,
            lock=True,
        )
        hornadas = await self._locked_batches([from_batch_id, to_batch_id])
        origen, destino = hornadas[from_batch_id], hornadas[to_batch_id]
        if origen.firing_type is not destino.firing_type:
            raise KilnBatchMoveInvalidError()
        self._check_editable(origen)
        self._check_editable(destino)

        sacadas = await self._take_out(origen, source, items, await self.db_now())
        colocadas = await self._place(destino, source, items, user)
        self._record_operation(operacion, KilnBatchOperationKind.MOVE, destino.id, user)
        self._audit_batch(
            origen,
            user,
            AuditAction.UPDATE,
            {
                "transition": "MOVE_OUT",
                "to": destino.code,
                "source": source.code,
                "pieces": sacadas,
            },
        )
        self._audit_batch(
            destino,
            user,
            AuditAction.UPDATE,
            {
                "transition": "MOVE_IN",
                "from": origen.code,
                "source": source.code,
                "pieces": colocadas,
            },
        )
        await self._session.flush()
        return await self.get(origen.id), await self.get(destino.id)

    # ------------------------------------------------------------------
    # Arrancar, completar, anular
    # ------------------------------------------------------------------
    async def _lock_parents_of(self, batch: KilnBatch) -> list[KilnBatchAssignment]:
        """Bloquea, en orden, los pedidos que viajan en la hornada."""
        activas = list(
            (
                await self._session.scalars(
                    select(KilnBatchAssignment)
                    .where(
                        KilnBatchAssignment.batch_id == batch.id,
                        KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
                    )
                    .order_by(KilnBatchAssignment.id)
                )
            ).all()
        )
        ordenes = sorted({a.production_order_id for a in activas if a.production_order_id})
        cargas = sorted({a.internal_load_id for a in activas if a.internal_load_id})
        for orden_id in ordenes:
            await self._session.execute(
                select(ProductionOrder.id).where(ProductionOrder.id == orden_id).with_for_update()
            )
        for carga_id in cargas:
            await self._session.execute(
                select(InternalLoad.id).where(InternalLoad.id == carga_id).with_for_update()
            )
        return activas

    async def _check_low_before_high(
        self, batch: KilnBatch, activas: Sequence[KilnBatchAssignment]
    ) -> None:
        """Ninguna pieza que necesita bizcocho entra cruda en alta."""
        if batch.firing_type is not FiringType.HIGH or not activas:
            return
        lineas: list[LineCycles] = []
        en_esta: dict[str, int] = defaultdict(int)
        claves: dict[str, tuple[KilnBatchSourceKind, int]] = {}
        vistos: set[tuple[int | None, int | None]] = set()
        for a in activas:
            clave = f"{a.source_kind.value}:{self._line_id_of(a)}"
            en_esta[clave] += a.quantity
            claves[clave] = (a.source_kind, self._line_id_of(a))
            vistos.add((a.production_order_id, a.internal_load_id))
        for orden_id, carga_id in vistos:
            source = await self._resolve(
                production_order_id=orden_id, internal_load_id=carga_id, lock=False
            )
            for linea in source.lines:
                lineas.append(
                    LineCycles(
                        line_key=linea.key,
                        needs_low=source.needs_low,
                        needs_high=source.needs_high,
                    )
                )

        ya_en_alta: dict[str, int] = defaultdict(int)
        bizcochadas: dict[str, int] = defaultdict(int)
        filtros = []
        for kind, line_id in claves.values():
            filtros.append(self._line_column(kind) == line_id)
        filas = (
            await self._session.execute(
                select(KilnBatchAssignment, KilnBatch)
                .join(KilnBatch, KilnBatch.id == KilnBatchAssignment.batch_id)
                .where(
                    KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
                    KilnBatch.id != batch.id,
                    or_(*filtros),
                )
            )
        ).all()
        for a, hornada in filas:
            clave = f"{a.source_kind.value}:{self._line_id_of(a)}"
            if (
                hornada.firing_type is FiringType.LOW
                and hornada.status is KilnBatchStatus.COMPLETED
            ):
                bizcochadas[clave] += a.quantity
            elif hornada.firing_type is FiringType.HIGH and hornada.status in (
                KilnBatchStatus.STARTED,
                KilnBatchStatus.COMPLETED,
            ):
                ya_en_alta[clave] += a.quantity
        fallos = high_start_violations(lineas, dict(en_esta), dict(ya_en_alta), dict(bizcochadas))
        if fallos:
            nombres = sorted(
                {
                    a.product_name_snapshot
                    for a in activas
                    if f"{a.source_kind.value}:{self._line_id_of(a)}" in fallos
                }
            )
            raise KilnBatchLowNotCompletedError(
                "Todavia no completaron su quema baja: " + ", ".join(nombres)
            )

    async def start(self, batch_id: int, *, user: AuthenticatedUser) -> BatchView:
        batch = await self._locked_batch(batch_id)
        if batch.status is KilnBatchStatus.STARTED:
            return await self.get(batch.id)
        if batch.status is not KilnBatchStatus.PLANNED:
            raise KilnBatchTransitionError()
        activas = await self._lock_parents_of(batch)
        if not activas:
            raise KilnBatchEmptyError()
        await self._check_low_before_high(batch, activas)
        batch.status = KilnBatchStatus.STARTED
        batch.started_at = await self.db_now()
        batch.version += 1
        self._audit_batch(batch, user, AuditAction.UPDATE, {"transition": "START"})
        await self._session.flush()
        return await self.get(batch.id)

    async def complete(self, batch_id: int, *, user: AuthenticatedUser) -> BatchView:
        batch = await self._locked_batch(batch_id)
        if batch.status is KilnBatchStatus.COMPLETED:
            return await self.get(batch.id)
        if batch.status is not KilnBatchStatus.STARTED:
            raise KilnBatchTransitionError()
        batch.status = KilnBatchStatus.COMPLETED
        batch.completed_at = await self.db_now()
        batch.version += 1
        self._audit_batch(batch, user, AuditAction.UPDATE, {"transition": "COMPLETE"})
        await self._session.flush()
        return await self.get(batch.id)

    async def cancel(
        self, batch_id: int, *, reason: str | None, user: AuthenticatedUser
    ) -> BatchView:
        """Anula una planificada. No borra: libera sus piezas y conserva la historia."""
        batch = await self._locked_batch(batch_id)
        if batch.status is KilnBatchStatus.CANCELLED:
            return await self.get(batch.id)
        if batch.status is not KilnBatchStatus.PLANNED:
            raise KilnBatchTransitionError(
                "Solo se anula una hornada planificada. Una arrancada ya esta en el horno"
            )
        ahora = await self.db_now()
        activas = await self._lock_parents_of(batch)
        for a in activas:
            a.status = KilnBatchAssignmentStatus.RELEASED
            a.released_at = ahora
        await self._session.flush()
        batch.status = KilnBatchStatus.CANCELLED
        batch.cancelled_at = ahora
        batch.cancel_reason = reason
        batch.version += 1
        self._audit_batch(
            batch,
            user,
            AuditAction.UPDATE,
            {"transition": "CANCEL", "released": len(activas), "reason": reason},
        )
        await self._session.flush()
        return await self.get(batch.id)


__all__ = [
    "KILN_BATCH_ENTITY",
    "AssignItem",
    "AssignmentView",
    "BatchView",
    "CycleProgress",
    "FiringPlan",
    "KilnBatchService",
    "PlanLine",
    "PlanSource",
    "payload_fingerprint",
]
