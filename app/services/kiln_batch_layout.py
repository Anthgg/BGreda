"""Persistencia del layout fisico de una hornada. Fase 010M — M1.

## Que hace este servicio

Permite guardar y recuperar el acomodo fisico de las piezas en el horno
para una hornada planificada (PLANNED). El layout incluye:

- Snapshot de las dimensiones utiles del horno al momento de crearlo.
- Los niveles (estantes) y sus propiedades.
- Los placements: donde va cada grupo de piezas, en que nivel, con que
  rotacion y con las medidas de la pieza congeladas en ese momento.

## Lo que M1 NO valida todavia

Colisiones fisicas, limites X/Y, separacion geometrica y altura contra nivel
son validaciones de M2/M3. M1 solo garantiza:

- Que el batch existe y es de esta empresa.
- Que el horno tiene dimensiones utiles configuradas.
- Que cada assignment pertenece al mismo batch que el layout.
- Que la suma de placements por assignment no supera la cantidad asignada.
- Que la version es la esperada (concurrencia optimista).
- Que la rotacion es solo 0 o 90.
- Que las cantidades son positivas.

## Concurrencia optimista

El layout lleva su propio contador `version`. El cliente manda
`expected_version` en el PUT:

- `expected_version == 0` → creacion inicial; rechaza si ya existe.
- `expected_version == N` → actualizacion; rechaza si el layout actual
  tiene version distinta de N (409 KILN_LAYOUT_VERSION_CONFLICT).

El PUT es un REEMPLAZO completo: borra todos los placements y niveles
anteriores y los escribe de nuevo, todo en una sola transaccion.

## Idempotencia

El idempotency_key opcional permite reintentos seguros. Dos llamadas con
la misma clave y el mismo contenido devuelven el mismo resultado; una
clave con contenido distinto es rechazada.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.models.audit import AuditAction
from app.models.firing_quotation_v2 import V2FiringQuotation, V2FiringQuotationLine
from app.models.firings import Kiln
from app.models.kiln_batches import (
    KILN_BATCH_EDITABLE,
    InternalLoad,
    InternalLoadLine,
    KilnBatch,
    KilnBatchAssignment,
    KilnBatchAssignmentStatus,
    KilnBatchLayout,
    KilnBatchLayoutLevel,
    KilnBatchLayoutPlacement,
    KilnBatchOperation,
    KilnBatchOperationKind,
    KilnBatchSourceKind,
)
from app.models.quoter_v2 import V2Quotation, V2QuotationProduct
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder

KILN_LAYOUT_ENTITY = "kiln_batch_layout"

#: Espacio de nombres de bloqueos consultivos de idempotencia del layout.
#: Distinto de la planificacion (90110) para no serializarse entre si.
LAYOUT_IDEMPOTENCY_LOCK_NAMESPACE = 90111


# ---------------------------------------------------------------------------
# Errores de negocio
# ---------------------------------------------------------------------------

class KilnLayoutBatchNotFoundError(APIError):
    status_code = 404
    code = "KILN_BATCH_NOT_FOUND"
    message = "La hornada no existe"


class KilnLayoutNotFoundError(APIError):
    status_code = 404
    code = "KILN_LAYOUT_NOT_FOUND"
    message = "Esta hornada todavia no tiene layout"


class KilnLayoutDimensionsMissingError(APIError):
    status_code = 422
    code = "KILN_LAYOUT_DIMENSIONS_MISSING"
    message = (
        "El horno no tiene dimensiones utiles configuradas "
        "(usable_width_cm, usable_depth_cm, usable_height_cm). "
        "Configurelas antes de crear el layout fisico"
    )


class KilnLayoutPieceDimensionsMissingError(APIError):
    status_code = 422
    code = "KILN_LAYOUT_PIECE_DIMENSIONS_MISSING"
    message = (
        "La asignación no cuenta con dimensiones geométricas suficientes "
        "(largo, ancho, alto) en su fuente productiva para crear el layout físico"
    )


class KilnLayoutNotEditableError(APIError):
    status_code = 409
    code = "KILN_LAYOUT_NOT_EDITABLE"
    message = "El layout solo se puede editar cuando la hornada esta PLANNED"


class KilnLayoutVersionConflictError(APIError):
    status_code = 409
    code = "KILN_LAYOUT_VERSION_CONFLICT"
    message = (
        "El layout fue modificado por otra persona o sesion. "
        "Vuelva a cargarlo antes de guardar"
    )


class KilnLayoutAlreadyExistsError(APIError):
    status_code = 409
    code = "KILN_LAYOUT_ALREADY_EXISTS"
    message = (
        "Ya existe un layout para esta hornada. "
        "Use expected_version > 0 para actualizarlo"
    )


class KilnLayoutAssignmentMismatchError(APIError):
    status_code = 422
    code = "KILN_LAYOUT_ASSIGNMENT_MISMATCH"
    message = "Una de las asignaciones no pertenece a esta hornada o ya fue liberada"


class KilnLayoutQuantityExceededError(APIError):
    status_code = 409
    code = "KILN_LAYOUT_QUANTITY_EXCEEDED"
    message = "Los placements de una asignacion superan la cantidad asignada en la hornada"


class KilnLayoutIdempotencyKeyReusedError(APIError):
    status_code = 409
    code = "KILN_LAYOUT_IDEMPOTENCY_KEY_REUSED"
    message = "Esa clave de idempotencia ya se uso con otro contenido de layout"


# ---------------------------------------------------------------------------
# Data Transfer Objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LevelSpec:
    """Especificacion de un nivel para el servicio."""

    level_index: int
    name: str | None
    z_cm: Decimal
    usable_height_cm: Decimal
    plate_label: str | None
    plate_thickness_cm: Decimal | None


@dataclass(frozen=True)
class PlacementSpec:
    """Especificacion de un placement para el servicio."""

    batch_assignment_id: int
    group_index: int
    unit_index: int | None
    quantity: int
    level_index: int
    x_cm: Decimal
    y_cm: Decimal
    rotation_degrees: int


@dataclass(frozen=True)
class LayoutView:
    """El layout listo para serializar a la API."""

    layout: KilnBatchLayout
    levels: tuple[KilnBatchLayoutLevel, ...]
    placements: tuple[KilnBatchLayoutPlacement, ...]


# ---------------------------------------------------------------------------
# Huella del contenido
# ---------------------------------------------------------------------------

def _layout_fingerprint(
    expected_version: int,
    levels: list[LevelSpec],
    placements: list[PlacementSpec],
) -> str:
    """SHA-256 canonico del contenido del layout para idempotencia."""
    payload = {
        "expected_version": expected_version,
        "levels": [
            {
                "level_index": s.level_index,
                "name": s.name,
                "z_cm": str(s.z_cm),
                "usable_height_cm": str(s.usable_height_cm),
                "plate_label": s.plate_label,
                "plate_thickness_cm": (
                    str(s.plate_thickness_cm)
                    if s.plate_thickness_cm is not None
                    else None
                ),
            }
            for s in sorted(levels, key=lambda x: x.level_index)
        ],
        "placements": [
            {
                "batch_assignment_id": p.batch_assignment_id,
                "group_index": p.group_index,
                "unit_index": p.unit_index,
                "quantity": p.quantity,
                "level_index": p.level_index,
                "x_cm": str(p.x_cm),
                "y_cm": str(p.y_cm),
                "rotation_degrees": p.rotation_degrees,
            }
            for p in sorted(
                placements,
                key=lambda x: (
                    x.batch_assignment_id,
                    x.group_index,
                    x.level_index,
                    str(x.x_cm),
                    str(x.y_cm),
                ),
            )
        ],
    }
    texto = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(texto.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------

class KilnBatchLayoutService:
    """Unica autoridad sobre el layout fisico de una hornada. Fase 010M."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRecorder | None = None,
    ) -> None:
        self._session = session
        self._audit = audit or AuditRecorder(session)

    # ------------------------------------------------------------------
    # Privados
    # ------------------------------------------------------------------

    async def _lock_idempotency(self, key: str) -> None:
        from sqlalchemy import func
        from sqlalchemy import select as sa_select

        await self._session.execute(
            sa_select(
                func.pg_advisory_xact_lock(
                    LAYOUT_IDEMPOTENCY_LOCK_NAMESPACE,
                    int(zlib.crc32(key.encode())) - 2**31,
                )
            )
        )

    async def _get_batch(self, batch_id: int, *, lock: bool = False) -> KilnBatch:
        q = select(KilnBatch).where(KilnBatch.id == batch_id)
        if lock:
            q = q.with_for_update()
        batch = await self._session.scalar(q)
        if batch is None:
            raise KilnLayoutBatchNotFoundError()
        return batch

    async def _get_kiln(self, kiln_id: int) -> Kiln:
        kiln = await self._session.get(Kiln, kiln_id)
        assert kiln is not None  # FK garantiza existencia
        return kiln

    async def _get_layout_for_batch(self, batch_id: int) -> KilnBatchLayout | None:
        return await self._session.scalar(
            select(KilnBatchLayout).where(KilnBatchLayout.batch_id == batch_id)
        )

    async def _get_layout_for_batch_locked(self, batch_id: int) -> KilnBatchLayout | None:
        return await self._session.scalar(
            select(KilnBatchLayout)
            .where(KilnBatchLayout.batch_id == batch_id)
            .with_for_update()
        )

    async def _load_full_layout(self, layout: KilnBatchLayout) -> LayoutView:
        """Carga los niveles y placements de un layout dado."""
        await self._session.refresh(layout)
        levels = (
            await self._session.scalars(
                select(KilnBatchLayoutLevel)
                .where(KilnBatchLayoutLevel.layout_id == layout.id)
                .order_by(KilnBatchLayoutLevel.level_index)
            )
        ).all()
        placements = (
            await self._session.scalars(
                select(KilnBatchLayoutPlacement)
                .where(KilnBatchLayoutPlacement.layout_id == layout.id)
                .order_by(KilnBatchLayoutPlacement.id)
            )
        ).all()
        return LayoutView(
            layout=layout,
            levels=tuple(levels),
            placements=tuple(placements),
        )

    def _audit_layout(
        self,
        layout: KilnBatchLayout,
        user: AuthenticatedUser,
        action: AuditAction,
        *,
        batch_code: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._audit.record_action(
            entity_type=KILN_LAYOUT_ENTITY,
            entity_id=str(layout.id),
            action=action,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"batch_id": layout.batch_id, "batch_code": batch_code, **(metadata or {})},
        )

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    async def get_layout(self, batch_id: int) -> LayoutView:
        """Devuelve el layout de una hornada. Funciona para cualquier estado."""
        # Verificar que la hornada existe.
        await self._get_batch(batch_id)
        layout = await self._get_layout_for_batch(batch_id)
        if layout is None:
            raise KilnLayoutNotFoundError()
        return await self._load_full_layout(layout)

    # ------------------------------------------------------------------
    # Resolución de Geometría de Asignaciones (Fase 010M)
    # ------------------------------------------------------------------

    async def resolve_assignments_geometry(
        self,
        assignments: list[KilnBatchAssignment],
    ) -> dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]:
        """Resuelve la geometría (largo, ancho, alto, separación) de asignaciones.

        Realiza carga por lotes para evitar consultas N+1.
        Devuelve dict: assignment_id -> (length_cm, width_cm, height_cm, separation_cm).
        Lanza KilnLayoutPieceDimensionsMissingError si faltan dimensiones o son <= 0.
        """
        result: dict[int, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
        if not assignments:
            return result

        # 1. V2_QUOTATION
        v2_asgns = [
            a
            for a in assignments
            if a.source_kind == KilnBatchSourceKind.V2_QUOTATION
            and a.v2_quotation_product_id is not None
        ]
        if v2_asgns:
            v2_product_ids = [a.v2_quotation_product_id for a in v2_asgns]
            v2_rows = (
                await self._session.execute(
                    select(
                        V2QuotationProduct.id,
                        V2QuotationProduct.length_cm,
                        V2QuotationProduct.width_cm,
                        V2QuotationProduct.height_cm,
                        V2Quotation.piece_separation_cm_snapshot,
                    )
                    .join(V2Quotation, V2Quotation.id == V2QuotationProduct.v2_quotation_id)
                    .where(V2QuotationProduct.id.in_(v2_product_ids))
                )
            ).all()
            v2_map = {row[0]: (row[1], row[2], row[3], row[4]) for row in v2_rows}
            for a in v2_asgns:
                assert a.v2_quotation_product_id is not None
                if a.v2_quotation_product_id not in v2_map:
                    raise KilnLayoutPieceDimensionsMissingError()
                length, width, height, sep = v2_map[a.v2_quotation_product_id]
                if (
                    length is None
                    or width is None
                    or height is None
                    or length <= 0
                    or width <= 0
                    or height <= 0
                ):
                    raise KilnLayoutPieceDimensionsMissingError()
                result[a.id] = (length, width, height, sep if sep is not None else Decimal(0))

        # 2. FIRING_V2
        fq_asgns = [
            a
            for a in assignments
            if a.source_kind == KilnBatchSourceKind.FIRING_V2
            and a.v2_firing_quotation_line_id is not None
        ]
        if fq_asgns:
            fq_line_ids = [a.v2_firing_quotation_line_id for a in fq_asgns]
            fq_rows = (
                await self._session.execute(
                    select(
                        V2FiringQuotationLine.id,
                        V2FiringQuotationLine.length_cm,
                        V2FiringQuotationLine.width_cm,
                        V2FiringQuotationLine.height_cm,
                        V2FiringQuotation.piece_separation_cm,
                    )
                    .join(
                        V2FiringQuotation,
                        V2FiringQuotation.id == V2FiringQuotationLine.v2_firing_quotation_id,
                    )
                    .where(V2FiringQuotationLine.id.in_(fq_line_ids))
                )
            ).all()
            fq_map = {row[0]: (row[1], row[2], row[3], row[4]) for row in fq_rows}
            for a in fq_asgns:
                assert a.v2_firing_quotation_line_id is not None
                if a.v2_firing_quotation_line_id not in fq_map:
                    raise KilnLayoutPieceDimensionsMissingError()
                length, width, height, sep = fq_map[a.v2_firing_quotation_line_id]
                if (
                    length is None
                    or width is None
                    or height is None
                    or length <= 0
                    or width <= 0
                    or height <= 0
                ):
                    raise KilnLayoutPieceDimensionsMissingError()
                result[a.id] = (length, width, height, sep if sep is not None else Decimal(0))

        # 3. INTERNAL
        int_asgns = [
            a
            for a in assignments
            if a.source_kind == KilnBatchSourceKind.INTERNAL
            and a.internal_load_line_id is not None
        ]
        if int_asgns:
            int_line_ids = [a.internal_load_line_id for a in int_asgns]
            int_rows = (
                await self._session.execute(
                    select(
                        InternalLoadLine.id,
                        InternalLoadLine.length_cm,
                        InternalLoadLine.width_cm,
                        InternalLoadLine.height_cm,
                        InternalLoad.piece_separation_cm,
                    )
                    .join(InternalLoad, InternalLoad.id == InternalLoadLine.load_id)
                    .where(InternalLoadLine.id.in_(int_line_ids))
                )
            ).all()
            int_map = {row[0]: (row[1], row[2], row[3], row[4]) for row in int_rows}
            for a in int_asgns:
                assert a.internal_load_line_id is not None
                if a.internal_load_line_id not in int_map:
                    raise KilnLayoutPieceDimensionsMissingError()
                length, width, height, sep = int_map[a.internal_load_line_id]
                if (
                    length is None
                    or width is None
                    or height is None
                    or length <= 0
                    or width <= 0
                    or height <= 0
                ):
                    raise KilnLayoutPieceDimensionsMissingError()
                result[a.id] = (length, width, height, sep if sep is not None else Decimal(0))

        # Verificar que todas las asignaciones tienen geometría válida
        for a in assignments:
            if a.id not in result:
                raise KilnLayoutPieceDimensionsMissingError()

        return result

    async def resolve_assignment_geometry(
        self,
        assignment: KilnBatchAssignment,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """Resuelve la geometría para una sola asignación."""
        mapping = await self.resolve_assignments_geometry([assignment])
        return mapping[assignment.id]

    # ------------------------------------------------------------------
    # PUT
    # ------------------------------------------------------------------

    async def save_layout(
        self,
        batch_id: int,
        *,
        expected_version: int,
        levels: list[LevelSpec],
        placements: list[PlacementSpec],
        user: AuthenticatedUser,
        idempotency_key: str | None = None,
    ) -> LayoutView:
        """Guarda (crea o reemplaza) el layout de una hornada.

        Solo permitido cuando batch.status == PLANNED.
        Es un REEMPLAZO completo en una sola transaccion.

        expected_version == 0 → creacion inicial.
        expected_version > 0  → actualizacion; debe coincidir con version actual.
        """
        # ── Idempotencia ──────────────────────────────────────────────────
        huella = _layout_fingerprint(expected_version, levels, placements)
        if idempotency_key:
            await self._lock_idempotency(idempotency_key)
            operacion_previa = await self._session.scalar(
                select(KilnBatchOperation).where(
                    KilnBatchOperation.idempotency_key == idempotency_key
                )
            )
            if operacion_previa is not None:
                if (
                    operacion_previa.kind is not KilnBatchOperationKind.LAYOUT
                    or operacion_previa.payload_fingerprint != huella
                    or operacion_previa.batch_id != batch_id
                ):
                    raise KilnLayoutIdempotencyKeyReusedError()
                # Reintento identico: devolver el estado actual.
                layout = await self._get_layout_for_batch(batch_id)
                if layout is None:
                    raise KilnLayoutNotFoundError()
                return await self._load_full_layout(layout)

        # ── Bloquear la hornada ────────────────────────────────────────────
        batch = await self._get_batch(batch_id, lock=True)

        # ── Solo PLANNED puede editar ───────────────────────────────────────
        if batch.status not in KILN_BATCH_EDITABLE:
            raise KilnLayoutNotEditableError()

        # ── Cargar el horno y verificar dimensiones ───────────────────────
        kiln = await self._get_kiln(batch.kiln_id)
        if (
            kiln.usable_width_cm is None
            or kiln.usable_depth_cm is None
            or kiln.usable_height_cm is None
        ):
            raise KilnLayoutDimensionsMissingError()

        # ── Bloquear el layout actual (si existe) y verificar version ──────
        existing_layout = await self._get_layout_for_batch_locked(batch_id)

        if expected_version == 0:
            # Creacion inicial: no debe existir layout.
            if existing_layout is not None:
                raise KilnLayoutAlreadyExistsError()
        else:
            # Actualizacion: version debe coincidir.
            if existing_layout is None:
                # El cliente cree que existe version N pero no hay nada.
                raise KilnLayoutVersionConflictError()
            if existing_layout.version != expected_version:
                raise KilnLayoutVersionConflictError()

        # ── Validar que todos los assignments pertenecen a este batch ──────
        assignment_ids = {p.batch_assignment_id for p in placements}
        geometry_by_assignment: dict[int, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
        if assignment_ids:
            active_assignments: dict[int, KilnBatchAssignment] = {}
            rows = (
                await self._session.scalars(
                    select(KilnBatchAssignment).where(
                        KilnBatchAssignment.id.in_(assignment_ids),
                        KilnBatchAssignment.batch_id == batch_id,
                        KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
                    )
                )
            ).all()
            for a in rows:
                active_assignments[a.id] = a

            # Verificar que todos los ids pedidos estan en el batch.
            missing = assignment_ids - set(active_assignments)
            if missing:
                raise KilnLayoutAssignmentMismatchError()

            # Verificar que los placements no superen la cantidad asignada.
            placement_quantities: dict[int, int] = defaultdict(int)
            for p in placements:
                placement_quantities[p.batch_assignment_id] += p.quantity

            for asgn_id, total in placement_quantities.items():
                asgn = active_assignments[asgn_id]
                if total > asgn.quantity:
                    raise KilnLayoutQuantityExceededError()

            # Derivar geometrías y separación desde la fuente productiva
            geometry_by_assignment = await self.resolve_assignments_geometry(
                list(active_assignments.values())
            )

        # ── Persistir en una sola transaccion ─────────────────────────────
        if existing_layout is None:
            # Crear layout nuevo con snapshot de dimensiones del horno.
            layout = KilnBatchLayout(
                batch_id=batch_id,
                kiln_width_cm_snapshot=kiln.usable_width_cm,
                kiln_depth_cm_snapshot=kiln.usable_depth_cm,
                kiln_height_cm_snapshot=kiln.usable_height_cm,
                version=1,
            )
            self._session.add(layout)
            await self._session.flush()
            new_version = 1
            action = AuditAction.CREATE
        else:
            # Actualizar: incrementar version, conservar snapshot de dimensiones.
            existing_layout.version = existing_layout.version + 1
            await self._session.flush()
            layout = existing_layout
            new_version = layout.version
            action = AuditAction.UPDATE

            # Borrar niveles y placements anteriores (cascade los elimina con
            # el layout pero aqui los borramos explicitamente para el caso de
            # actualizacion donde el layout persiste).
            for lvl in await self._session.scalars(
                select(KilnBatchLayoutLevel).where(
                    KilnBatchLayoutLevel.layout_id == layout.id
                )
            ):
                await self._session.delete(lvl)
            for plc in await self._session.scalars(
                select(KilnBatchLayoutPlacement).where(
                    KilnBatchLayoutPlacement.layout_id == layout.id
                )
            ):
                await self._session.delete(plc)
            await self._session.flush()

        # Insertar los nuevos niveles.
        for level_spec in levels:
            self._session.add(
                KilnBatchLayoutLevel(
                    layout_id=layout.id,
                    level_index=level_spec.level_index,
                    name=level_spec.name,
                    z_cm=level_spec.z_cm,
                    usable_height_cm=level_spec.usable_height_cm,
                    plate_label=level_spec.plate_label,
                    plate_thickness_cm=level_spec.plate_thickness_cm,
                )
            )

        # Insertar los nuevos placements con dimensiones congeladas desde la fuente productiva.
        for placement_spec in placements:
            length, width, height, sep = geometry_by_assignment[
                placement_spec.batch_assignment_id
            ]
            self._session.add(
                KilnBatchLayoutPlacement(
                    layout_id=layout.id,
                    batch_assignment_id=placement_spec.batch_assignment_id,
                    group_index=placement_spec.group_index,
                    unit_index=placement_spec.unit_index,
                    quantity=placement_spec.quantity,
                    level_index=placement_spec.level_index,
                    x_cm=placement_spec.x_cm,
                    y_cm=placement_spec.y_cm,
                    rotation_degrees=placement_spec.rotation_degrees,
                    piece_length_cm_snapshot=length,
                    piece_width_cm_snapshot=width,
                    piece_height_cm_snapshot=height,
                    separation_cm_snapshot=sep,
                )
            )

        await self._session.flush()

        # Registrar operacion de idempotencia.
        if idempotency_key:
            self._session.add(
                KilnBatchOperation(
                    idempotency_key=idempotency_key,
                    kind=KilnBatchOperationKind.LAYOUT,
                    payload_fingerprint=huella,
                    batch_id=batch_id,
                    created_by=user.id,
                )
            )

        # Auditar.
        self._audit_layout(
            layout,
            user,
            action,
            batch_code=batch.code,
            metadata={"version": new_version, "levels": len(levels), "placements": len(placements)},
        )

        # Refrescar y devolver.
        return await self._load_full_layout(layout)
