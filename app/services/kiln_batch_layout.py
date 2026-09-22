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

from sqlalchemy import func, select
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
from app.services.kiln_layout_geometry import (
    BoundingBox2D,
    LevelGeometry,
    PlacementGeometry,
    get_reserved_footprint,
    validate_layout_geometry,
    validate_level_geometry,
)
from app.services.kiln_layout_packing import (
    PieceToPack,
    SuggestedPlacement,
    UnplacedPiece,
    suggest_layout_packing,
)

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


class KilnLayoutOutOfBoundsError(APIError):
    status_code = 422
    code = "KILN_LAYOUT_OUT_OF_BOUNDS"
    message = "El placement excede los límites físicos utilizables del horno (ancho o profundidad)"


class KilnLayoutHeightExceededError(APIError):
    status_code = 422
    code = "KILN_LAYOUT_HEIGHT_EXCEEDED"
    message = "La altura reservada de la pieza excede la altura útil del nivel asignado"


class KilnLayoutCollisionError(APIError):
    status_code = 422
    code = "KILN_LAYOUT_COLLISION"
    message = "Dos o más piezas colisionan o superponen sus áreas reservadas en el mismo nivel"


class KilnLayoutLevelOutOfBoundsError(APIError):
    status_code = 422
    code = "KILN_LAYOUT_LEVEL_OUT_OF_BOUNDS"
    message = "La configuración de un nivel excede la altura del horno o es inválida"


class KilnLayoutLevelOverlapError(APIError):
    status_code = 422
    code = "KILN_LAYOUT_LEVEL_OVERLAP"
    message = "Dos o más niveles se solapan verticalmente en el horno"


class KilnLayoutLevelNotFoundError(APIError):
    status_code = 422
    code = "KILN_LAYOUT_LEVEL_NOT_FOUND"
    message = "El placement hace referencia a un nivel que no existe en el layout"


class KilnLayoutPhysicalQuantityInvalidError(APIError):
    status_code = 422
    code = "KILN_LAYOUT_PHYSICAL_QUANTITY_INVALID"
    message = "Cada placement físico debe tener cantidad exactamente 1"


class KilnLayoutUnitIdentityMissingError(APIError):
    status_code = 422
    code = "KILN_LAYOUT_UNIT_IDENTITY_MISSING"
    message = (
        "Existen placements persistidos sin unit_index para una asignación; "
        "se requiere identidad explícita para auto-packing"
    )


class KilnLayoutUnitIdentityInconsistentError(APIError):
    status_code = 422
    code = "KILN_LAYOUT_UNIT_IDENTITY_INCONSISTENT"
    message = (
        "Los unit_index de los placements existentes son inconsistentes "
        "(duplicados o fuera de rango [1..N])"
    )


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
    placed_quantity: int
    pending_quantity: int
    invalid_quantity: int = 0


@dataclass(frozen=True)
class LayoutSuggestionView:
    """Resultado de la sugerencia de layout lista para serializar."""

    batch_id: int
    base_version: int
    total_pending: int
    suggested_count: int
    unplaced_count: int
    levels_used: list[int]
    suggested_placements: list[SuggestedPlacement]
    unplaced_pieces: list[UnplacedPiece]


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
        placed_quantity = sum(p.quantity for p in placements)
        total_assigned = (
            await self._session.scalar(
                select(func.coalesce(func.sum(KilnBatchAssignment.quantity), 0)).where(
                    KilnBatchAssignment.batch_id == layout.batch_id,
                    KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
                )
            )
            or 0
        )
        pending_quantity = max(0, int(total_assigned) - placed_quantity)
        return LayoutView(
            layout=layout,
            levels=tuple(levels),
            placements=tuple(placements),
            placed_quantity=placed_quantity,
            pending_quantity=pending_quantity,
            invalid_quantity=0,
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

        # ── Validar geometría física del layout (Fase 010M - M2) ─────────
        kiln_width = (
            existing_layout.kiln_width_cm_snapshot
            if existing_layout is not None
            else kiln.usable_width_cm
        )
        kiln_depth = (
            existing_layout.kiln_depth_cm_snapshot
            if existing_layout is not None
            else kiln.usable_depth_cm
        )
        kiln_height = (
            existing_layout.kiln_height_cm_snapshot
            if existing_layout is not None
            else kiln.usable_height_cm
        )

        level_geoms = [
            LevelGeometry(
                level_index=lvl.level_index,
                z_cm=lvl.z_cm,
                usable_height_cm=lvl.usable_height_cm,
            )
            for lvl in levels
        ]

        placement_geoms: list[PlacementGeometry] = []
        for idx, p in enumerate(placements):
            geom = geometry_by_assignment.get(p.batch_assignment_id)
            if geom is None:
                raise KilnLayoutPieceDimensionsMissingError()
            length, width, height, sep = geom
            placement_geoms.append(
                PlacementGeometry(
                    index=idx,
                    batch_assignment_id=p.batch_assignment_id,
                    quantity=p.quantity,
                    level_index=p.level_index,
                    x_cm=p.x_cm,
                    y_cm=p.y_cm,
                    rotation_degrees=p.rotation_degrees,
                    piece_length_cm=length,
                    piece_width_cm=width,
                    piece_height_cm=height,
                    separation_cm=sep,
                )
            )

        try:
            validate_layout_geometry(
                kiln_width=kiln_width,
                kiln_depth=kiln_depth,
                kiln_height=kiln_height,
                levels=level_geoms,
                placements=placement_geoms,
            )
        except ValueError as e:
            msg = str(e)
            if msg.startswith("OUT_OF_BOUNDS:"):
                raise KilnLayoutOutOfBoundsError(msg) from e
            elif msg.startswith("HEIGHT_EXCEEDED:"):
                raise KilnLayoutHeightExceededError(msg) from e
            elif msg.startswith("COLLISION:"):
                raise KilnLayoutCollisionError(msg) from e
            elif msg.startswith("LEVEL_OUT_OF_BOUNDS:"):
                raise KilnLayoutLevelOutOfBoundsError(msg) from e
            elif msg.startswith("LEVEL_OVERLAP:"):
                raise KilnLayoutLevelOverlapError(msg) from e
            elif msg.startswith("LEVEL_NOT_FOUND:"):
                raise KilnLayoutLevelNotFoundError(msg) from e
            elif msg.startswith("PHYSICAL_QUANTITY_INVALID:"):
                raise KilnLayoutPhysicalQuantityInvalidError(msg) from e
            else:
                raise KilnLayoutOutOfBoundsError(msg) from e

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
            metadata={
                "version": new_version,
                "levels": len(levels),
                "placements": len(placements),
            },
        )

        # Refrescar y devolver.
        return await self._load_full_layout(layout)

    # ------------------------------------------------------------------
    # SUGGEST (Fase 010M - M3)
    # ------------------------------------------------------------------

    async def suggest_layout(
        self,
        batch_id: int,
        *,
        expected_version: int | None = None,
        candidate_levels: list[LevelSpec] | None = None,
    ) -> LayoutSuggestionView:
        """Calcula una sugerencia de layout físico sin persistirla.

        Solo permitido cuando batch.status == PLANNED.
        NO modifica la base de datos, no incrementa versión ni registra auditoría.
        """
        # 1. Verificar hornada
        batch = await self._get_batch(batch_id)

        # 2. Solo PLANNED puede calcular sugerencias
        if batch.status not in KILN_BATCH_EDITABLE:
            raise KilnLayoutNotEditableError()

        # 3. Cargar horno y verificar dimensiones útiles
        kiln = await self._get_kiln(batch.kiln_id)
        if (
            kiln.usable_width_cm is None
            or kiln.usable_depth_cm is None
            or kiln.usable_height_cm is None
        ):
            raise KilnLayoutDimensionsMissingError()

        # 4. Cargar layout actual (si existe) y verificar versión esperada
        existing_layout = await self._get_layout_for_batch(batch_id)

        if expected_version is not None:
            if existing_layout is None:
                if expected_version != 0:
                    raise KilnLayoutVersionConflictError()
            elif existing_layout.version != expected_version:
                raise KilnLayoutVersionConflictError()

        base_version = existing_layout.version if existing_layout is not None else 0

        # 5. Obtener niveles y placements existentes como obstáculos fijos
        levels_geom: list[LevelGeometry] = []
        existing_boxes: list[BoundingBox2D] = []
        existing_placements: list[KilnBatchLayoutPlacement] = []

        if existing_layout is not None:
            layout_view = await self._load_full_layout(existing_layout)
            for lvl in layout_view.levels:
                levels_geom.append(
                    LevelGeometry(
                        level_index=lvl.level_index,
                        z_cm=lvl.z_cm,
                        usable_height_cm=lvl.usable_height_cm,
                    )
                )
            for pl in layout_view.placements:
                existing_placements.append(pl)
                fp = get_reserved_footprint(
                    piece_length=pl.piece_length_cm_snapshot,
                    piece_width=pl.piece_width_cm_snapshot,
                    piece_height=pl.piece_height_cm_snapshot,
                    separation=pl.separation_cm_snapshot,
                    rotation_degrees=pl.rotation_degrees,
                )
                existing_boxes.append(
                    BoundingBox2D(
                        placement_index=pl.id,
                        batch_assignment_id=pl.batch_assignment_id,
                        level_index=pl.level_index,
                        left=pl.x_cm,
                        right=pl.x_cm + fp.x_size,
                        bottom=pl.y_cm,
                        top=pl.y_cm + fp.y_size,
                        height=fp.z_size,
                    )
                )
        elif candidate_levels:
            for clvl in candidate_levels:
                levels_geom.append(
                    LevelGeometry(
                        level_index=clvl.level_index,
                        z_cm=clvl.z_cm,
                        usable_height_cm=clvl.usable_height_cm,
                    )
                )
        else:
            raise KilnLayoutNotFoundError()

        # Validar niveles con motor M2
        try:
            validate_level_geometry(levels_geom, kiln.usable_height_cm)
        except ValueError as e:
            msg = str(e)
            if "LEVEL_OVERLAP" in msg:
                raise KilnLayoutLevelOverlapError(msg) from e
            raise KilnLayoutLevelOutOfBoundsError(msg) from e

        # 6. Cargar asignaciones activas de la hornada
        assignments = (
            await self._session.scalars(
                select(KilnBatchAssignment)
                .where(
                    KilnBatchAssignment.batch_id == batch_id,
                    KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
                )
                .order_by(KilnBatchAssignment.id)
            )
        ).all()

        # 7. Resolver geometría de asignaciones
        geom_map = await self.resolve_assignments_geometry(list(assignments))

        # 8. Identidad de unidades lógicas y cálculo de pendientes
        pieces_to_pack: list[PieceToPack] = []
        for asgn in assignments:
            asgn_placements = [
                p for p in existing_placements if p.batch_assignment_id == asgn.id
            ]

            # Validar unit_index en placements existentes
            used_indices: list[int] = []
            for p in asgn_placements:
                if p.unit_index is None:
                    raise KilnLayoutUnitIdentityMissingError(
                        f"El placement {p.id} de la asignación {asgn.id} no tiene unit_index "
                        f"definido. Se requiere identidad explícita para auto-packing."
                    )
                if p.unit_index < 1 or p.unit_index > asgn.quantity:
                    raise KilnLayoutUnitIdentityInconsistentError(
                        f"El placement {p.id} de la asignación {asgn.id} tiene unit_index "
                        f"{p.unit_index} fuera de rango [1..{asgn.quantity}]."
                    )
                used_indices.append(p.unit_index)

            if len(used_indices) != len(set(used_indices)):
                raise KilnLayoutUnitIdentityInconsistentError(
                    f"La asignación {asgn.id} tiene unit_index duplicados en sus placements "
                    f"existentes: {used_indices}."
                )

            # Calcular identidades pendientes: {1..quantity} - used_indices
            all_indices = set(range(1, asgn.quantity + 1))
            pending_indices = sorted(all_indices - set(used_indices))

            length, width, height, sep = geom_map[asgn.id]
            for u_idx in pending_indices:
                pieces_to_pack.append(
                    PieceToPack(
                        batch_assignment_id=asgn.id,
                        group_index=0,
                        unit_index=u_idx,
                        piece_length_cm=length,
                        piece_width_cm=width,
                        piece_height_cm=height,
                        separation_cm=sep,
                    )
                )

        # 9. Ejecutar motor de auto-packing puro
        result = suggest_layout_packing(
            kiln_width=kiln.usable_width_cm,
            kiln_depth=kiln.usable_depth_cm,
            kiln_height=kiln.usable_height_cm,
            levels=levels_geom,
            existing_boxes=existing_boxes,
            pieces_to_pack=pieces_to_pack,
        )

        # 10. Validar compatibilidad del layout final (existentes + sugeridos) con motor M2
        all_placements_geom: list[PlacementGeometry] = []
        for idx, pl in enumerate(existing_placements):
            all_placements_geom.append(
                PlacementGeometry(
                    index=idx,
                    batch_assignment_id=pl.batch_assignment_id,
                    quantity=pl.quantity,
                    level_index=pl.level_index,
                    x_cm=pl.x_cm,
                    y_cm=pl.y_cm,
                    rotation_degrees=pl.rotation_degrees,
                    piece_length_cm=pl.piece_length_cm_snapshot,
                    piece_width_cm=pl.piece_width_cm_snapshot,
                    piece_height_cm=pl.piece_height_cm_snapshot,
                    separation_cm=pl.separation_cm_snapshot,
                )
            )
        start_idx = len(existing_placements)
        for idx, sp in enumerate(result.suggested_placements):
            all_placements_geom.append(
                PlacementGeometry(
                    index=start_idx + idx,
                    batch_assignment_id=sp.batch_assignment_id,
                    quantity=sp.quantity,
                    level_index=sp.level_index,
                    x_cm=sp.x_cm,
                    y_cm=sp.y_cm,
                    rotation_degrees=sp.rotation_degrees,
                    piece_length_cm=sp.piece_length_cm_snapshot,
                    piece_width_cm=sp.piece_width_cm_snapshot,
                    piece_height_cm=sp.piece_height_cm_snapshot,
                    separation_cm=sp.separation_cm_snapshot,
                )
            )

        try:
            validate_layout_geometry(
                kiln_width=kiln.usable_width_cm,
                kiln_depth=kiln.usable_depth_cm,
                kiln_height=kiln.usable_height_cm,
                levels=levels_geom,
                placements=all_placements_geom,
            )
        except ValueError as e:
            msg = str(e)
            if "COLLISION" in msg:
                raise KilnLayoutCollisionError(msg) from e
            if "OUT_OF_BOUNDS" in msg:
                raise KilnLayoutOutOfBoundsError(msg) from e
            if "HEIGHT_EXCEEDED" in msg:
                raise KilnLayoutHeightExceededError(msg) from e
            raise KilnLayoutOutOfBoundsError(msg) from e

        return LayoutSuggestionView(
            batch_id=batch_id,
            base_version=base_version,
            total_pending=result.total_pending,
            suggested_count=result.suggested_count,
            unplaced_count=result.unplaced_count,
            levels_used=result.levels_used,
            suggested_placements=result.suggested_placements,
            unplaced_pieces=result.unplaced_pieces,
        )
