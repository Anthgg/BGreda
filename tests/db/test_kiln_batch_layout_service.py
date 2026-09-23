"""Pruebas del servicio de layout físico del horno (Fase 010M - M1 y M2).

Cubre:
1. Rechazo cuando el horno no tiene dimensiones lineales (422)
2. Geometría y separación derivadas desde la fuente productiva:
   - V2_QUOTATION: largo/ancho/alto de V2QuotationProduct, separación de V2Quotation
   - FIRING_V2: largo/ancho/alto de V2FiringQuotationLine, separación de V2FiringQuotation
   - INTERNAL: largo/ancho/alto de InternalLoadLine, separación de InternalLoad
3. Rechazo cuando la fuente productiva no tiene dimensiones suficientes (422)
4. Inmutabilidad de snapshots frente a mutaciones del horno o del maestro de productos
5. Creación inicial (expected_version=0 -> versión 1)
6. Actualización válida (expected_version=1 -> versión 2) y rechazo de versión obsoleta (409)
7. Estados de solo lectura (STARTED, COMPLETED, CANCELLED): GET permitido, PUT rechazado
8. Rechazo de asignación ajena al batch (422)
9. Suma de placements no excede la cantidad asignada
10. Contrato de idempotencia:
    - Mismo key + mismo payload: retorno idempotente (sin 409 por stale version ni duplicación)
    - Mismo key + diferente payload: conflicto 409
    - Diferente key + versión obsoleta: conflicto 409
11. Concurrencia en creación inicial sin 500
12. Validaciones geométricas M2:
    - Colisión 2D dentro del mismo nivel (422)
    - Placement fuera de los límites X o Y del horno (422)
    - Altura reservada excede altura útil del nivel (422)
    - Solapamiento vertical entre niveles (422)
    - Semántica física: quantity != 1 rechazado (422)
    - Atomicidad: fallo en un placement no consume versión ni altera el layout existente
    - Resumen operacional: placed_quantity y pending_quantity calculados correctamente
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.firing_quotation_v2 import (
    V2FiringMode,
    V2FiringProductionHandoff,
    V2FiringQuotation,
    V2FiringQuotationLine,
)
from app.models.firings import Kiln
from app.models.inventory import StockLocation
from app.models.kiln_batches import (
    InternalLoad,
    InternalLoadLine,
    KilnBatch,
    KilnBatchAssignment,
    KilnBatchSourceKind,
    KilnBatchStatus,
)
from app.models.production import ProductionOrder
from app.models.profile import UserRole
from app.models.quoter_v2 import (
    V2ProductionHandoff,
    V2Quotation,
    V2QuotationProduct,
)
from app.schemas.auth import AuthenticatedUser
from app.services.kiln_batch_layout import (
    KilnBatchLayoutService,
    KilnLayoutAlreadyExistsError,
    KilnLayoutAssignmentMismatchError,
    KilnLayoutCollisionError,
    KilnLayoutDimensionsMissingError,
    KilnLayoutHeightExceededError,
    KilnLayoutIdempotencyKeyReusedError,
    KilnLayoutLevelOutOfBoundsError,
    KilnLayoutLevelOverlapError,
    KilnLayoutNotEditableError,
    KilnLayoutNotFoundError,
    KilnLayoutOutOfBoundsError,
    KilnLayoutPhysicalQuantityInvalidError,
    KilnLayoutPieceDimensionsMissingError,
    KilnLayoutQuantityExceededError,
    KilnLayoutUnitIdentityInconsistentError,
    KilnLayoutUnitIdentityMissingError,
    KilnLayoutVersionConflictError,
    LevelSpec,
    PlacementSpec,
)
from tests.db.test_kiln_batches_service import _batch, _kiln, _load

pytestmark = pytest.mark.asyncio

USER = AuthenticatedUser(
    id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
    email="admin@empresa.com",
    display_name="Administrador",
    role=UserRole.ADMIN,
)


async def _kiln_with_dims(
    session: AsyncSession,
    code: str,
    *,
    width: Decimal = Decimal("60"),
    depth: Decimal = Decimal("50"),
    height: Decimal = Decimal("40"),
    capacity: Decimal = Decimal("500000"),
) -> Kiln:
    kiln = await _kiln(session, code, capacity=capacity)
    kiln.usable_width_cm = width
    kiln.usable_depth_cm = depth
    kiln.usable_height_cm = height
    await session.flush()
    return kiln


async def _assign_internal(
    session: AsyncSession,
    batch: KilnBatch,
    load: InternalLoad,
    line: InternalLoadLine,
    quantity: int,
) -> KilnBatchAssignment:
    assignment = KilnBatchAssignment(
        batch_id=batch.id,
        source_kind=KilnBatchSourceKind.INTERNAL,
        internal_load_id=load.id,
        internal_load_line_id=line.id,
        quantity=quantity,
        unit_volume_snapshot_cm3=line.unit_volume_cm3,
        assigned_volume_cm3=Decimal(quantity) * line.unit_volume_cm3,
        firing_mode=V2FiringMode.SHARED,
        product_name_snapshot=line.name,
    )
    session.add(assignment)
    await session.flush()
    return assignment


async def _assign_v2(
    session: AsyncSession,
    batch: KilnBatch,
    code: str,
    *,
    quantity: int,
    length: Decimal,
    width: Decimal,
    height: Decimal,
    separation: Decimal = Decimal("3"),
) -> tuple[KilnBatchAssignment, V2Quotation, V2QuotationProduct]:
    location = StockLocation(name=f"Almacen V2 {code}", active=True)
    session.add(location)
    await session.flush()

    quotation = V2Quotation(
        code=f"CTZ-V2-LAY-{code}",
        customer_name_snapshot=f"Cliente {code}",
        firing_mode=V2FiringMode.SHARED,
        low_fire_enabled=True,
        high_fire_enabled=False,
        kiln_id=batch.kiln_id,
        kiln_name_snapshot=batch.kiln_name_snapshot,
        kiln_capacity_snapshot=batch.capacity_snapshot_cm3,
        piece_separation_cm_snapshot=separation,
        firing_count=1,
        low_fire_count=1,
        high_fire_count=0,
        firing_billed_load=Decimal("1"),
    )
    session.add(quotation)
    await session.flush()

    q_product = V2QuotationProduct(
        v2_quotation_id=quotation.id,
        product_id=None,
        sort_order=1,
        product_name_snapshot=f"Pieza {code}",
        quantity=quantity,
        length_cm=length,
        width_cm=width,
        height_cm=height,
        unit_volume_cm3=(length + separation) * (width + separation) * (height + separation),
        total_volume_cm3=Decimal(quantity)
        * ((length + separation) * (width + separation) * (height + separation)),
    )
    handoff = V2ProductionHandoff(
        v2_quotation_id=quotation.id,
        commercial_fingerprint="b" * 64,
        created_by=USER.id,
        created_by_name=USER.display_name,
    )
    session.add_all([q_product, handoff])
    await session.flush()

    order = ProductionOrder(
        code=f"OP-V2-LAY-{code}",
        v2_handoff_id=handoff.id,
        stock_location_id=location.id,
        qr_token=f"qr-token-v2-lay-{code}-1234567890-abcdefghijklmnop",
        created_by=USER.id,
        created_by_name=USER.display_name,
    )
    session.add(order)
    await session.flush()

    assignment = KilnBatchAssignment(
        batch_id=batch.id,
        source_kind=KilnBatchSourceKind.V2_QUOTATION,
        production_order_id=order.id,
        v2_quotation_product_id=q_product.id,
        quantity=quantity,
        unit_volume_snapshot_cm3=q_product.unit_volume_cm3,
        assigned_volume_cm3=q_product.total_volume_cm3,
        firing_mode=V2FiringMode.SHARED,
        product_name_snapshot=q_product.product_name_snapshot,
    )
    session.add(assignment)
    await session.flush()
    return assignment, quotation, q_product


async def _assign_firing_v2(
    session: AsyncSession,
    batch: KilnBatch,
    code: str,
    *,
    quantity: int,
    length: Decimal,
    width: Decimal,
    height: Decimal,
    separation: Decimal = Decimal("2.5"),
) -> tuple[KilnBatchAssignment, V2FiringQuotationLine]:
    location = StockLocation(name=f"Almacen SQ {code}", active=True)
    session.add(location)
    await session.flush()

    fq = V2FiringQuotation(
        code=f"SQ-V2-{code}",
        customer_name_snapshot=f"Cliente SQ {code}",
        firing_mode=V2FiringMode.SHARED,
        low_fire_enabled=True,
        high_fire_enabled=False,
        kiln_id=batch.kiln_id,
        kiln_name_snapshot=batch.kiln_name_snapshot,
        kiln_capacity_snapshot=batch.capacity_snapshot_cm3,
        piece_separation_cm=separation,
        firing_count=1,
        billed_load=Decimal("1"),
    )
    session.add(fq)
    await session.flush()

    line = V2FiringQuotationLine(
        v2_firing_quotation_id=fq.id,
        sort_order=1,
        product_name_snapshot=f"Pieza SQ {code}",
        quantity=quantity,
        length_cm=length,
        width_cm=width,
        height_cm=height,
        unit_volume_cm3=(length + separation) * (width + separation) * (height + separation),
        total_volume_cm3=Decimal(quantity)
        * ((length + separation) * (width + separation) * (height + separation)),
    )
    handoff = V2FiringProductionHandoff(
        v2_firing_quotation_id=fq.id,
        commercial_fingerprint="c" * 64,
        created_by=USER.id,
        created_by_name=USER.display_name,
    )
    session.add_all([line, handoff])
    await session.flush()

    order = ProductionOrder(
        code=f"OP-SQ-LAY-{code}",
        v2_firing_handoff_id=handoff.id,
        stock_location_id=location.id,
        qr_token=f"qr-token-sq-lay-{code}-1234567890-abcdefghijklmnop",
        created_by=USER.id,
        created_by_name=USER.display_name,
    )
    session.add(order)
    await session.flush()

    assignment = KilnBatchAssignment(
        batch_id=batch.id,
        source_kind=KilnBatchSourceKind.FIRING_V2,
        production_order_id=order.id,
        v2_firing_quotation_line_id=line.id,
        quantity=quantity,
        unit_volume_snapshot_cm3=line.unit_volume_cm3,
        assigned_volume_cm3=line.total_volume_cm3,
        firing_mode=V2FiringMode.SHARED,
        product_name_snapshot=line.product_name_snapshot,
    )
    session.add(assignment)
    await session.flush()
    return assignment, line


# ---------------------------------------------------------------------------
# Pruebas
# ---------------------------------------------------------------------------


async def test_dimensiones_horno_faltantes_rechazan_layout(db_session: AsyncSession) -> None:
    """Si el horno no tiene usable_width/depth/height, el layout se rechaza con 422."""
    kiln = await _kiln(db_session, "K-NODIM")
    kiln.usable_width_cm = None
    kiln.usable_depth_cm = None
    kiln.usable_height_cm = None
    await db_session.flush()

    batch = await _batch(db_session, kiln, "HOR-NODIM")
    service = KilnBatchLayoutService(db_session)

    with pytest.raises(KilnLayoutDimensionsMissingError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=[],
            placements=[],
            user=USER,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_DIMENSIONS_MISSING"


async def test_crear_y_obtener_layout_con_geometria_derivada_internal(
    db_session: AsyncSession,
) -> None:
    """Las dimensiones y separación de placements INTERNAL se derivan del snapshot productivo."""
    kiln = await _kiln_with_dims(db_session, "K-LAY-INT")
    batch = await _batch(db_session, kiln, "HOR-LAY-INT")
    load, line = await _load(db_session, "L-INT", quantity=10, unit_volume=Decimal("100"))
    line.length_cm = Decimal("12.0")
    line.width_cm = Decimal("8.0")
    line.height_cm = Decimal("15.0")
    load.piece_separation_cm = Decimal("1.5")
    await db_session.flush()

    asgn = await _assign_internal(db_session, batch, load, line, quantity=10)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label="Placa 1",
            plate_thickness_cm=Decimal("1.5"),
        ),
    ]
    # M2: cada placement representa exactamente quantity = 1 en posiciones no colisionantes
    placements = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=1,
            level_index=0,
            x_cm=Decimal("5"),
            y_cm=Decimal("10"),
            rotation_degrees=0,
        ),
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=1,
            unit_index=None,
            quantity=1,
            level_index=0,
            x_cm=Decimal("20"),
            y_cm=Decimal("25"),
            rotation_degrees=90,
        ),
    ]

    result = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=placements,
        user=USER,
    )
    assert result.layout.version == 1
    assert len(result.placements) == 2
    assert result.placed_quantity == 2
    assert result.pending_quantity == 8
    assert result.invalid_quantity == 0

    # Verificar que el backend congeló largo, ancho, alto y separación desde la carga interna
    p1 = result.placements[0]
    assert p1.piece_length_cm_snapshot == Decimal("12.0")
    assert p1.piece_width_cm_snapshot == Decimal("8.0")
    assert p1.piece_height_cm_snapshot == Decimal("15.0")
    assert p1.separation_cm_snapshot == Decimal("1.5")
    assert p1.rotation_degrees == 0

    p2 = result.placements[1]
    assert p2.rotation_degrees == 90
    assert p2.separation_cm_snapshot == Decimal("1.5")


async def test_geometria_derivada_desde_v2_quotation(db_session: AsyncSession) -> None:
    """V2_QUOTATION deriva largo/ancho/alto de V2QuotationProduct y separación de cotización."""
    kiln = await _kiln_with_dims(db_session, "K-LAY-V2")
    batch = await _batch(db_session, kiln, "HOR-LAY-V2")
    asgn, _, _ = await _assign_v2(
        db_session,
        batch,
        "V2-SRC",
        quantity=5,
        length=Decimal("14.0"),
        width=Decimal("11.0"),
        height=Decimal("9.0"),
        separation=Decimal("3.0"),
    )

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    result = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=[
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=0,
                unit_index=None,
                quantity=1,
                level_index=0,
                x_cm=Decimal("0"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            )
        ],
        user=USER,
    )
    assert result.placed_quantity == 1
    assert result.pending_quantity == 4
    p = result.placements[0]
    assert p.piece_length_cm_snapshot == Decimal("14.0")
    assert p.piece_width_cm_snapshot == Decimal("11.0")
    assert p.piece_height_cm_snapshot == Decimal("9.0")
    assert p.separation_cm_snapshot == Decimal("3.0")


async def test_geometria_derivada_desde_firing_v2(db_session: AsyncSession) -> None:
    """Asignación de FIRING_V2 deriva medidas de V2FiringQuotationLine y sep de Solo Quema."""
    kiln = await _kiln_with_dims(db_session, "K-LAY-SQ")
    batch = await _batch(db_session, kiln, "HOR-LAY-SQ")
    asgn, _ = await _assign_firing_v2(
        db_session,
        batch,
        "SQ-SRC",
        quantity=8,
        length=Decimal("22.0"),
        width=Decimal("18.0"),
        height=Decimal("12.0"),
        separation=Decimal("2.0"),
    )

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    result = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=[
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=0,
                unit_index=None,
                quantity=1,
                level_index=0,
                x_cm=Decimal("0"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            )
        ],
        user=USER,
    )
    assert result.placed_quantity == 1
    assert result.pending_quantity == 7
    p = result.placements[0]
    assert p.piece_length_cm_snapshot == Decimal("22.0")
    assert p.piece_width_cm_snapshot == Decimal("18.0")
    assert p.piece_height_cm_snapshot == Decimal("12.0")
    assert p.separation_cm_snapshot == Decimal("2.0")


async def test_geometria_invalida_o_faltante_rechaza_con_422(db_session: AsyncSession) -> None:
    """Si la fuente productiva no tiene dimensiones suficientes, se rechaza con 422."""
    kiln = await _kiln_with_dims(db_session, "K-NODIM-PIECE")
    batch = await _batch(db_session, kiln, "HOR-NODIM-PIECE")
    asgn, _, q_product = await _assign_v2(
        db_session,
        batch,
        "V2-NODIM",
        quantity=3,
        length=Decimal("10"),
        width=Decimal("10"),
        height=Decimal("10"),
    )
    # Anular una dimensión en la línea de la cotización
    q_product.length_cm = None
    await db_session.flush()

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    with pytest.raises(KilnLayoutPieceDimensionsMissingError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=levels,
            placements=[
                PlacementSpec(
                    batch_assignment_id=asgn.id,
                    group_index=0,
                    unit_index=None,
                    quantity=1,
                    level_index=0,
                    x_cm=Decimal("0"),
                    y_cm=Decimal("0"),
                    rotation_degrees=0,
                )
            ],
            user=USER,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_PIECE_DIMENSIONS_MISSING"


async def test_crear_layout_cuando_ya_existe_con_version_0_falla(
    db_session: AsyncSession,
) -> None:
    """Si ya existe un layout, reintentar con expected_version=0 lanza 409."""
    kiln = await _kiln_with_dims(db_session, "K-EXISTS")
    batch = await _batch(db_session, kiln, "HOR-EXISTS")
    service = KilnBatchLayoutService(db_session)

    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=[],
        placements=[],
        user=USER,
    )

    with pytest.raises(KilnLayoutAlreadyExistsError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=[],
            placements=[],
            user=USER,
        )
    assert exc_info.value.status_code == 409


async def test_actualizar_layout_con_version_valida_y_stale_version(
    db_session: AsyncSession,
) -> None:
    """Actualizar con expected_version correcto incrementa versión; versión obsoleta lanza 409."""
    kiln = await _kiln_with_dims(db_session, "K-UPD")
    batch = await _batch(db_session, kiln, "HOR-UPD")
    service = KilnBatchLayoutService(db_session)

    res1 = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=[],
        placements=[],
        user=USER,
    )
    assert res1.layout.version == 1

    # Actualizar con versión 1 -> nueva versión 2
    res2 = await service.save_layout(
        batch.id,
        expected_version=1,
        levels=[],
        placements=[],
        user=USER,
    )
    assert res2.layout.version == 2

    # Intentar actualizar con versión obsoleta 1 -> 409
    with pytest.raises(KilnLayoutVersionConflictError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=1,
            levels=[],
            placements=[],
            user=USER,
        )
    assert exc_info.value.status_code == 409


async def test_snapshot_dimensiones_horno_inmutable(db_session: AsyncSession) -> None:
    """Modificar el maestro Kiln después de crear el layout NO altera el snapshot del layout."""
    kiln = await _kiln_with_dims(
        db_session,
        "K-SNAP",
        width=Decimal("60"),
        depth=Decimal("50"),
        height=Decimal("40"),
    )
    batch = await _batch(db_session, kiln, "HOR-SNAP")
    service = KilnBatchLayoutService(db_session)

    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=[],
        placements=[],
        user=USER,
    )

    # Modificar maestro
    kiln.usable_width_cm = Decimal("100")
    kiln.usable_depth_cm = Decimal("90")
    kiln.usable_height_cm = Decimal("80")
    await db_session.flush()

    got = await service.get_layout(batch.id)
    assert got.layout.kiln_width_cm_snapshot == Decimal("60")
    assert got.layout.kiln_depth_cm_snapshot == Decimal("50")
    assert got.layout.kiln_height_cm_snapshot == Decimal("40")


async def test_snapshot_pieza_inmutable_tras_mutar_maestro_y_origen(
    db_session: AsyncSession,
) -> None:
    """Mutar el producto maestro o la línea origen no altera las dimensiones del placement."""
    kiln = await _kiln_with_dims(db_session, "K-PSNAP")
    batch = await _batch(db_session, kiln, "HOR-PSNAP")
    asgn, quotation, q_prod = await _assign_v2(
        db_session,
        batch,
        "V2-MUT",
        quantity=5,
        length=Decimal("12.5"),
        width=Decimal("8.0"),
        height=Decimal("6.0"),
        separation=Decimal("0.5"),
    )

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=[
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=0,
                unit_index=None,
                quantity=1,
                level_index=0,
                x_cm=Decimal("0"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            )
        ],
        user=USER,
    )

    # Mutar cotización y producto original (respetando CHECK piece_separation <= 20)
    quotation.piece_separation_cm_snapshot = Decimal("15.0")
    q_prod.length_cm = Decimal("999")
    q_prod.width_cm = Decimal("999")
    q_prod.height_cm = Decimal("999")
    await db_session.flush()

    got = await service.get_layout(batch.id)
    p = got.placements[0]
    assert p.piece_length_cm_snapshot == Decimal("12.5")
    assert p.piece_width_cm_snapshot == Decimal("8.0")
    assert p.piece_height_cm_snapshot == Decimal("6.0")
    assert p.separation_cm_snapshot == Decimal("0.5")


async def test_estados_solo_lectura_rechazan_put_pero_permiten_get(
    db_session: AsyncSession,
) -> None:
    """Hornadas en STARTED, COMPLETED, CANCELLED permiten GET pero rechazan PUT con 409."""
    kiln = await _kiln_with_dims(db_session, "K-RO")
    service = KilnBatchLayoutService(db_session)

    for status in (
        KilnBatchStatus.STARTED,
        KilnBatchStatus.COMPLETED,
        KilnBatchStatus.CANCELLED,
    ):
        batch = await _batch(db_session, kiln, f"HOR-RO-{status.value}")
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=[],
            placements=[],
            user=USER,
        )

        now = datetime.now(UTC)
        batch.status = status
        if status == KilnBatchStatus.STARTED:
            batch.started_at = now
        elif status == KilnBatchStatus.COMPLETED:
            batch.started_at = now
            batch.completed_at = now
        elif status == KilnBatchStatus.CANCELLED:
            batch.cancelled_at = now
        await db_session.flush()

        view = await service.get_layout(batch.id)
        assert view.layout.batch_id == batch.id

        with pytest.raises(KilnLayoutNotEditableError) as exc_info:
            await service.save_layout(
                batch.id,
                expected_version=1,
                levels=[],
                placements=[],
                user=USER,
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "KILN_LAYOUT_NOT_EDITABLE"


async def test_foreign_assignment_rechazada(db_session: AsyncSession) -> None:
    """Un placement con assignment de otro batch es rechazado con 422."""
    kiln = await _kiln_with_dims(db_session, "K-FOR")
    batch_a = await _batch(db_session, kiln, "HOR-FOR-A")
    batch_b = await _batch(db_session, kiln, "HOR-FOR-B")

    load, line = await _load(db_session, "L-FOR", quantity=5, unit_volume=Decimal("100"))
    asgn_b = await _assign_internal(db_session, batch_b, load, line, quantity=5)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    with pytest.raises(KilnLayoutAssignmentMismatchError) as exc_info:
        await service.save_layout(
            batch_a.id,
            expected_version=0,
            levels=levels,
            placements=[
                PlacementSpec(
                    batch_assignment_id=asgn_b.id,
                    group_index=0,
                    unit_index=None,
                    quantity=1,
                    level_index=0,
                    x_cm=Decimal("0"),
                    y_cm=Decimal("0"),
                    rotation_degrees=0,
                )
            ],
            user=USER,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_ASSIGNMENT_MISMATCH"


async def test_cantidad_placements_no_supera_asignacion(db_session: AsyncSession) -> None:
    """Dos placements de quantity=1 <= 2 pasan; tres placements de quantity=1 > 2 fallan (409)."""
    kiln = await _kiln_with_dims(db_session, "K-QTY")
    batch = await _batch(db_session, kiln, "HOR-QTY")
    load, line = await _load(db_session, "L-QTY", quantity=2, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("1.0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=2)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    # 2 placements de quantity=1 en posiciones no solapadas
    res = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=[
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=0,
                unit_index=None,
                quantity=1,
                level_index=0,
                x_cm=Decimal("0"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            ),
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=1,
                unit_index=None,
                quantity=1,
                level_index=0,
                x_cm=Decimal("20"),
                y_cm=Decimal("20"),
                rotation_degrees=0,
            ),
        ],
        user=USER,
    )
    assert len(res.placements) == 2
    assert res.placed_quantity == 2
    assert res.pending_quantity == 0

    # 3 placements de quantity=1 > 2 asignados -> FAIL (409)
    with pytest.raises(KilnLayoutQuantityExceededError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=1,
            levels=levels,
            placements=[
                PlacementSpec(
                    batch_assignment_id=asgn.id,
                    group_index=0,
                    unit_index=None,
                    quantity=1,
                    level_index=0,
                    x_cm=Decimal("0"),
                    y_cm=Decimal("0"),
                    rotation_degrees=0,
                ),
                PlacementSpec(
                    batch_assignment_id=asgn.id,
                    group_index=1,
                    unit_index=None,
                    quantity=1,
                    level_index=0,
                    x_cm=Decimal("20"),
                    y_cm=Decimal("20"),
                    rotation_degrees=0,
                ),
                PlacementSpec(
                    batch_assignment_id=asgn.id,
                    group_index=2,
                    unit_index=None,
                    quantity=1,
                    level_index=0,
                    x_cm=Decimal("40"),
                    y_cm=Decimal("0"),
                    rotation_degrees=0,
                ),
            ],
            user=USER,
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "KILN_LAYOUT_QUANTITY_EXCEEDED"


async def test_reintento_idempotente_exacto_no_duplica_ni_falla_stale_version(
    db_session: AsyncSession,
) -> None:
    """PUT con expected_version=0 e idempotency_key: reintento exacto devuelve layout sin 409."""
    kiln = await _kiln_with_dims(db_session, "K-IDEM-EXACT")
    batch = await _batch(db_session, kiln, "HOR-IDEM-EXACT")
    load, line = await _load(db_session, "L-IDEM-E", quantity=5, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("1.0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    placements = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=1,
            level_index=0,
            x_cm=Decimal("0"),
            y_cm=Decimal("0"),
            rotation_degrees=0,
        )
    ]

    # Primer intento
    res1 = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=placements,
        user=USER,
        idempotency_key="idemp-layout-exact-001",
    )
    assert res1.layout.version == 1
    assert len(res1.placements) == 1

    # Reintento exacto con expected_version=0 y misma clave (simulando pérdida de respuesta de red)
    # NO debe lanzar 409 stale version, debe devolver el layout ya persistido
    res2 = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=placements,
        user=USER,
        idempotency_key="idemp-layout-exact-001",
    )
    assert res2.layout.version == 1
    assert len(res2.placements) == 1
    assert res2.layout.id == res1.layout.id


async def test_idempotencia_misma_clave_diferente_payload_falla_409(
    db_session: AsyncSession,
) -> None:
    """Misma idempotency_key con diferente payload lanza 409 KilnLayoutIdempotencyKeyReusedError."""
    kiln = await _kiln_with_dims(db_session, "K-IDEM-DIFF")
    batch = await _batch(db_session, kiln, "HOR-IDEM-DIFF")
    load, line = await _load(db_session, "L-IDEM-D", quantity=5, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("1.0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    p1 = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=1,
            level_index=0,
            x_cm=Decimal("0"),
            y_cm=Decimal("0"),
            rotation_degrees=0,
        )
    ]
    p2 = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=1,
            level_index=0,
            x_cm=Decimal("15"),
            y_cm=Decimal("15"),
            rotation_degrees=90,
        )
    ]

    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=p1,
        user=USER,
        idempotency_key="idemp-diff-001",
    )

    with pytest.raises(KilnLayoutIdempotencyKeyReusedError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=levels,
            placements=p2,
            user=USER,
            idempotency_key="idemp-diff-001",
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "KILN_LAYOUT_IDEMPOTENCY_KEY_REUSED"


async def test_idempotencia_diferente_clave_version_obsoleta_falla_409(
    db_session: AsyncSession,
) -> None:
    """Clave diferente con expected_version=0 cuando ya existe layout lanza 409."""
    kiln = await _kiln_with_dims(db_session, "K-IDEM-NEW")
    batch = await _batch(db_session, kiln, "HOR-IDEM-NEW")
    service = KilnBatchLayoutService(db_session)

    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=[],
        placements=[],
        user=USER,
        idempotency_key="idemp-first-001",
    )

    with pytest.raises(KilnLayoutAlreadyExistsError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=[],
            placements=[],
            user=USER,
            idempotency_key="idemp-second-002",
        )
    assert exc_info.value.status_code == 409


async def test_concurrencia_creacion_inicial(
    db_session: AsyncSession,
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    """Dos llamadas concurrentes de creación inicial: una gana y la otra da 409 controlado."""
    kiln = await _kiln_with_dims(db_session, "K-CONCURR")
    batch = await _batch(db_session, kiln, "HOR-CONCURR")
    await db_session.commit()

    async def _try_create(key: str) -> tuple[bool, str | None]:
        async with sessionmaker_for_tests() as sess:
            svc = KilnBatchLayoutService(sess)
            try:
                await svc.save_layout(
                    batch.id,
                    expected_version=0,
                    levels=[],
                    placements=[],
                    user=USER,
                    idempotency_key=key,
                )
                await sess.commit()
                return True, None
            except Exception as e:
                await sess.rollback()
                return False, type(e).__name__

    r1, r2 = await asyncio.gather(
        _try_create("concurr-key-1"),
        _try_create("concurr-key-2"),
    )

    # Una debe ganar (True) y la otra debe fallar de forma controlada (KilnLayoutAlreadyExistsError)
    assert {r1[0], r2[0]} == {True, False}
    failed_error = r1[1] if not r1[0] else r2[1]
    assert failed_error == "KilnLayoutAlreadyExistsError"


async def test_get_layout_inexistente_404(db_session: AsyncSession) -> None:
    """GET de un batch que existe pero no tiene layout devuelve 404."""
    kiln = await _kiln_with_dims(db_session, "K-404")
    batch = await _batch(db_session, kiln, "HOR-404")
    service = KilnBatchLayoutService(db_session)

    with pytest.raises(KilnLayoutNotFoundError) as exc_info:
        await service.get_layout(batch.id)
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "KILN_LAYOUT_NOT_FOUND"


# ---------------------------------------------------------------------------
# Validaciones Geométricas M2 en Servicio
# ---------------------------------------------------------------------------


async def test_layout_service_rechaza_colision_422(db_session: AsyncSession) -> None:
    """Dos placements en mismo nivel con solape lanzan 422 KILN_LAYOUT_COLLISION."""
    kiln = await _kiln_with_dims(db_session, "K-COL")
    batch = await _batch(db_session, kiln, "HOR-COL")
    load, line = await _load(db_session, "L-COL", quantity=5, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("2.0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    # Pieza 10x10 con sep 2 -> reservada 12x12
    # P1 en x=0, y=0 -> reservado [0..12, 0..12]
    # P2 en x=11, y=0 -> reservado [11..23, 0..12] -> solapa en [11..12]
    placements = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=1,
            level_index=0,
            x_cm=Decimal("0"),
            y_cm=Decimal("0"),
            rotation_degrees=0,
        ),
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=1,
            unit_index=None,
            quantity=1,
            level_index=0,
            x_cm=Decimal("11"),
            y_cm=Decimal("0"),
            rotation_degrees=0,
        ),
    ]

    with pytest.raises(KilnLayoutCollisionError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=levels,
            placements=placements,
            user=USER,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_COLLISION"


async def test_layout_service_rechaza_out_of_bounds_422(db_session: AsyncSession) -> None:
    """Placement que excede ancho o profundidad del horno lanza 422 KILN_LAYOUT_OUT_OF_BOUNDS."""
    kiln = await _kiln_with_dims(db_session, "K-OOB", width=Decimal("50"), depth=Decimal("50"))
    batch = await _batch(db_session, kiln, "HOR-OOB")
    load, line = await _load(db_session, "L-OOB", quantity=5, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    # x=45 para pieza de 10 -> right=55 > 50 (kiln_width)
    placements = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=1,
            level_index=0,
            x_cm=Decimal("45"),
            y_cm=Decimal("0"),
            rotation_degrees=0,
        )
    ]

    with pytest.raises(KilnLayoutOutOfBoundsError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=levels,
            placements=placements,
            user=USER,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_OUT_OF_BOUNDS"


async def test_layout_service_rechaza_height_exceeded_422(db_session: AsyncSession) -> None:
    """Pieza que excede altura de nivel lanza 422 KILN_LAYOUT_HEIGHT_EXCEEDED."""
    kiln = await _kiln_with_dims(db_session, "K-HGT")
    batch = await _batch(db_session, kiln, "HOR-HGT")
    load, line = await _load(db_session, "L-HGT", quantity=5, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("25.0")
    load.piece_separation_cm = Decimal("1.0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)

    service = KilnBatchLayoutService(db_session)
    # Nivel de altura útil 20 cm, pero pieza tiene 25 + 1 = 26 cm de altura reservada
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    placements = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=1,
            level_index=0,
            x_cm=Decimal("0"),
            y_cm=Decimal("0"),
            rotation_degrees=0,
        )
    ]

    with pytest.raises(KilnLayoutHeightExceededError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=levels,
            placements=placements,
            user=USER,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_HEIGHT_EXCEEDED"


async def test_layout_service_rechaza_solapamiento_niveles_422(db_session: AsyncSession) -> None:
    """Dos niveles que se solapan verticalmente lanzan 422 KILN_LAYOUT_LEVEL_OVERLAP."""
    kiln = await _kiln_with_dims(db_session, "K-LVL-OV")
    batch = await _batch(db_session, kiln, "HOR-LVL-OV")
    service = KilnBatchLayoutService(db_session)

    # Nivel 0: [0..25], Nivel 1: [20..35] -> solapan en [20..25]
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("25"),
            plate_label=None,
            plate_thickness_cm=None,
        ),
        LevelSpec(
            level_index=1,
            name="Nivel 1",
            z_cm=Decimal("20"),
            usable_height_cm=Decimal("15"),
            plate_label=None,
            plate_thickness_cm=None,
        ),
    ]

    with pytest.raises(KilnLayoutLevelOverlapError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=levels,
            placements=[],
            user=USER,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_LEVEL_OVERLAP"


async def test_layout_service_rechaza_nivel_out_of_bounds_422(
    db_session: AsyncSession,
) -> None:
    """Nivel que supera la altura del horno lanza 422 KILN_LAYOUT_LEVEL_OUT_OF_BOUNDS."""
    kiln = await _kiln_with_dims(db_session, "K-LVL-OOB", height=Decimal("60"))
    batch = await _batch(db_session, kiln, "HOR-LVL-OOB")
    service = KilnBatchLayoutService(db_session)

    # Nivel con z=50 y usable_height=20 -> total 70 > 60
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("50"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]

    with pytest.raises(KilnLayoutLevelOutOfBoundsError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=levels,
            placements=[],
            user=USER,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_LEVEL_OUT_OF_BOUNDS"


async def test_layout_service_rechaza_quantity_distinta_de_1_422(
    db_session: AsyncSession,
) -> None:
    """Un placement con quantity != 1 es rechazado con 422 KILN_LAYOUT_PHYSICAL_QUANTITY_INVALID."""
    kiln = await _kiln_with_dims(db_session, "K-QTY-1")
    batch = await _batch(db_session, kiln, "HOR-QTY-1")
    load, line = await _load(db_session, "L-QTY-1", quantity=5, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    placements = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=2,  # Inválido en M2
            level_index=0,
            x_cm=Decimal("0"),
            y_cm=Decimal("0"),
            rotation_degrees=0,
        )
    ]

    with pytest.raises(KilnLayoutPhysicalQuantityInvalidError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=levels,
            placements=placements,
            user=USER,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_PHYSICAL_QUANTITY_INVALID"


async def test_layout_service_atomicidad_rechazo_no_altera_version(
    db_session: AsyncSession,
) -> None:
    """Fallo por colisión mantiene layout previo intacto sin alterar versión."""
    kiln = await _kiln_with_dims(db_session, "K-ATOM")
    batch = await _batch(db_session, kiln, "HOR-ATOM")
    load, line = await _load(db_session, "L-ATOM", quantity=5, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("1.0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    # 1. Crear layout versión 1 válido
    p_init = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=1,
            level_index=0,
            x_cm=Decimal("0"),
            y_cm=Decimal("0"),
            rotation_degrees=0,
        )
    ]
    res1 = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=p_init,
        user=USER,
    )
    assert res1.layout.version == 1
    assert len(res1.placements) == 1

    # 2. Intentar actualizar con 1 placement válido + 1 placement colisionante
    p_invalid = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=1,
            level_index=0,
            x_cm=Decimal("0"),
            y_cm=Decimal("0"),
            rotation_degrees=0,
        ),
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=1,
            unit_index=None,
            quantity=1,
            level_index=0,
            x_cm=Decimal("5"),  # Colisión con [0..11]
            y_cm=Decimal("0"),
            rotation_degrees=0,
        ),
    ]
    with pytest.raises(KilnLayoutCollisionError):
        await service.save_layout(
            batch.id,
            expected_version=1,
            levels=levels,
            placements=p_invalid,
            user=USER,
        )

    # 3. GET confirma que el layout sigue en versión 1 con exactamente 1 placement
    got = await service.get_layout(batch.id)
    assert got.layout.version == 1
    assert len(got.placements) == 1
    assert got.placements[0].x_cm == Decimal("0")


# ---------------------------------------------------------------------------
# Sugerencia de Layout M3 en Servicio
# ---------------------------------------------------------------------------


async def test_suggest_layout_service_con_placements_existentes(
    db_session: AsyncSession,
) -> None:
    """Sugerencia empaqueta solo piezas pendientes respetando obstáculos existentes."""
    kiln = await _kiln_with_dims(db_session, "K-SUG-1", width=Decimal("60"), depth=Decimal("50"))
    batch = await _batch(db_session, kiln, "HOR-SUG-1")
    load, line = await _load(db_session, "L-SUG-1", quantity=10, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=10)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    # Guardar 6 placements ya colocados
    placements_existentes = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=i,
            quantity=1,
            level_index=0,
            x_cm=Decimal(f"{(i - 1) * 10}"),
            y_cm=Decimal("0"),
            rotation_degrees=0,
        )
        for i in range(1, 7)
    ]
    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=placements_existentes,
        user=USER,
    )

    # Solicitar sugerencia para las 4 piezas pendientes
    suggestion = await service.suggest_layout(batch.id, expected_version=1)

    assert suggestion.batch_id == batch.id
    assert suggestion.base_version == 1
    assert suggestion.total_pending == 4
    assert suggestion.suggested_count == 4
    assert suggestion.unplaced_count == 0
    assert len(suggestion.suggested_placements) == 4

    # Verificar que el layout persistido no cambió (sin mutación)
    layout_db = await service.get_layout(batch.id)
    assert layout_db.layout.version == 1
    assert len(layout_db.placements) == 6


async def test_suggest_layout_service_con_version_obsoleta_409(
    db_session: AsyncSession,
) -> None:
    """Si expected_version no coincide con el layout actual, lanza 409."""
    kiln = await _kiln_with_dims(db_session, "K-SUG-STALE")
    batch = await _batch(db_session, kiln, "HOR-SUG-STALE")
    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="N0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=[],
        user=USER,
    )

    with pytest.raises(KilnLayoutVersionConflictError):
        await service.suggest_layout(batch.id, expected_version=0)


async def test_suggest_layout_service_estado_no_editable_409(
    db_session: AsyncSession,
) -> None:
    """Si la hornada está en STARTED/COMPLETED/CANCELLED, suggest lanza 409."""
    kiln = await _kiln_with_dims(db_session, "K-SUG-RO")
    batch = await _batch(db_session, kiln, "HOR-SUG-RO")
    batch.status = KilnBatchStatus.STARTED
    batch.started_at = datetime.now(UTC)
    await db_session.flush()

    service = KilnBatchLayoutService(db_session)
    with pytest.raises(KilnLayoutNotEditableError):
        await service.suggest_layout(batch.id)


async def test_suggest_layout_service_sin_layout_previo_con_candidate_levels(
    db_session: AsyncSession,
) -> None:
    """Sin layout en DB pero con candidate_levels en request, devuelve sugerencia con version 0."""
    kiln = await _kiln_with_dims(db_session, "K-SUG-NEW", width=Decimal("60"), depth=Decimal("50"))
    batch = await _batch(db_session, kiln, "HOR-SUG-NEW")
    load, line = await _load(db_session, "L-SUG-NEW", quantity=2, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("0")
    await db_session.flush()
    await _assign_internal(db_session, batch, load, line, quantity=2)

    service = KilnBatchLayoutService(db_session)
    candidate_levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]

    suggestion = await service.suggest_layout(
        batch.id,
        expected_version=0,
        candidate_levels=candidate_levels,
    )
    assert suggestion.base_version == 0
    assert suggestion.total_pending == 2
    assert suggestion.suggested_count == 2

    # GET layout sigue devolviendo 404 porque no se persistió nada
    with pytest.raises(KilnLayoutNotFoundError):
        await service.get_layout(batch.id)


async def test_suggest_layout_service_rechaza_candidate_level_out_of_bounds_422(
    db_session: AsyncSession,
) -> None:
    """suggest_layout con candidate_level que excede la altura del horno
    lanza 422 KILN_LAYOUT_LEVEL_OUT_OF_BOUNDS.
    """
    kiln = await _kiln_with_dims(db_session, "K-SUG-LVL-OOB", height=Decimal("80"))
    batch = await _batch(db_session, kiln, "HOR-SUG-LVL-OOB")
    load, line = await _load(db_session, "L-SUG-LVL-OOB", quantity=1, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("0")
    await db_session.flush()
    await _assign_internal(db_session, batch, load, line, quantity=1)

    service = KilnBatchLayoutService(db_session)
    invalid_candidate_levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("70"),
            usable_height_cm=Decimal("20"),  # 70 + 20 = 90 > 80
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]

    with pytest.raises(KilnLayoutLevelOutOfBoundsError) as exc_info:
        await service.suggest_layout(
            batch.id,
            expected_version=0,
            candidate_levels=invalid_candidate_levels,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_LEVEL_OUT_OF_BOUNDS"


async def test_suggest_layout_service_rechaza_candidate_level_overlap_422(
    db_session: AsyncSession,
) -> None:
    """suggest_layout con candidate_levels que se solapan verticalmente
    lanza 422 KILN_LAYOUT_LEVEL_OVERLAP.
    """
    kiln = await _kiln_with_dims(db_session, "K-SUG-LVL-OV", height=Decimal("80"))
    batch = await _batch(db_session, kiln, "HOR-SUG-LVL-OV")
    load, line = await _load(db_session, "L-SUG-LVL-OV", quantity=1, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("0")
    await db_session.flush()
    await _assign_internal(db_session, batch, load, line, quantity=1)

    service = KilnBatchLayoutService(db_session)
    overlapping_candidate_levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("30"),
            plate_label=None,
            plate_thickness_cm=None,
        ),
        LevelSpec(
            level_index=1,
            name="Nivel 1",
            z_cm=Decimal("20"),
            usable_height_cm=Decimal("30"),  # Solapa en [20..30]
            plate_label=None,
            plate_thickness_cm=None,
        ),
    ]

    with pytest.raises(KilnLayoutLevelOverlapError) as exc_info:
        await service.suggest_layout(
            batch.id,
            expected_version=0,
            candidate_levels=overlapping_candidate_levels,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_LEVEL_OVERLAP"


async def test_suggest_layout_service_rechaza_unit_index_none_422(
    db_session: AsyncSession,
) -> None:
    """suggest_layout rechaza placements con unit_index=None
    con 422 KILN_LAYOUT_UNIT_IDENTITY_MISSING.
    """
    kiln = await _kiln_with_dims(
        db_session, "K-SUG-NO-UID", width=Decimal("60"), depth=Decimal("50")
    )
    batch = await _batch(db_session, kiln, "HOR-SUG-NO-UID")
    load, line = await _load(db_session, "L-SUG-NO-UID", quantity=2, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=2)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    # Guardar placement con unit_index=None
    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=[
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=0,
                unit_index=None,
                quantity=1,
                level_index=0,
                x_cm=Decimal("0"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            )
        ],
        user=USER,
    )

    with pytest.raises(KilnLayoutUnitIdentityMissingError) as exc_info:
        await service.suggest_layout(batch.id, expected_version=1)
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_UNIT_IDENTITY_MISSING"


async def test_suggest_layout_service_rechaza_unit_index_duplicado_422(
    db_session: AsyncSession,
) -> None:
    """suggest_layout rechaza placements con unit_index duplicados
    con 422 KILN_LAYOUT_UNIT_IDENTITY_INCONSISTENT.
    """
    kiln = await _kiln_with_dims(
        db_session, "K-SUG-DUP-UID", width=Decimal("60"), depth=Decimal("50")
    )
    batch = await _batch(db_session, kiln, "HOR-SUG-DUP-UID")
    load, line = await _load(db_session, "L-SUG-DUP-UID", quantity=3, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=3)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    # Guardar 2 placements ambos con unit_index=1
    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=[
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=0,
                unit_index=1,
                quantity=1,
                level_index=0,
                x_cm=Decimal("0"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            ),
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=1,
                unit_index=1,
                quantity=1,
                level_index=0,
                x_cm=Decimal("20"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            ),
        ],
        user=USER,
    )

    with pytest.raises(KilnLayoutUnitIdentityInconsistentError) as exc_info:
        await service.suggest_layout(batch.id, expected_version=1)
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_UNIT_IDENTITY_INCONSISTENT"


async def test_suggest_layout_service_rechaza_unit_index_fuera_de_rango_422(
    db_session: AsyncSession,
) -> None:
    """suggest_layout rechaza placements con unit_index fuera de rango [1..N]
    con 422 KILN_LAYOUT_UNIT_IDENTITY_INCONSISTENT.
    """
    kiln = await _kiln_with_dims(
        db_session, "K-SUG-OOR-UID", width=Decimal("60"), depth=Decimal("50")
    )
    batch = await _batch(db_session, kiln, "HOR-SUG-OOR-UID")
    load, line = await _load(db_session, "L-SUG-OOR-UID", quantity=2, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=2)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    # Guardar placement con unit_index=99 (> quantity=2)
    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=[
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=0,
                unit_index=99,
                quantity=1,
                level_index=0,
                x_cm=Decimal("0"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            )
        ],
        user=USER,
    )

    with pytest.raises(KilnLayoutUnitIdentityInconsistentError) as exc_info:
        await service.suggest_layout(batch.id, expected_version=1)
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "KILN_LAYOUT_UNIT_IDENTITY_INCONSISTENT"


async def test_suggest_layout_service_unidades_no_contiguas(
    db_session: AsyncSession,
) -> None:
    """suggest_layout calcula unidades pendientes cuando las existentes no son contiguas."""
    kiln = await _kiln_with_dims(
        db_session, "K-SUG-NONCONT", width=Decimal("60"), depth=Decimal("50")
    )
    batch = await _batch(db_session, kiln, "HOR-SUG-NONCONT")
    load, line = await _load(db_session, "L-SUG-NONCONT", quantity=5, unit_volume=Decimal("100"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)

    service = KilnBatchLayoutService(db_session)
    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel 0",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label=None,
            plate_thickness_cm=None,
        )
    ]
    # Asignación tiene quantity=5. Placements existentes usan unit_index: 1, 3, 5
    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=[
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=0,
                unit_index=1,
                quantity=1,
                level_index=0,
                x_cm=Decimal("0"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            ),
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=1,
                unit_index=3,
                quantity=1,
                level_index=0,
                x_cm=Decimal("15"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            ),
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=2,
                unit_index=5,
                quantity=1,
                level_index=0,
                x_cm=Decimal("30"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            ),
        ],
        user=USER,
    )

    suggestion = await service.suggest_layout(batch.id, expected_version=1)
    assert suggestion.total_pending == 2
    assert suggestion.suggested_count == 2
    suggested_uids = [p.unit_index for p in suggestion.suggested_placements]
    assert set(suggested_uids) == {2, 4}
