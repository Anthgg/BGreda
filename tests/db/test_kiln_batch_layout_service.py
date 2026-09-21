"""Pruebas del servicio de layout físico del horno (Fase 010M - M1).

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
    KilnLayoutDimensionsMissingError,
    KilnLayoutIdempotencyKeyReusedError,
    KilnLayoutNotEditableError,
    KilnLayoutNotFoundError,
    KilnLayoutPieceDimensionsMissingError,
    KilnLayoutQuantityExceededError,
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
        code=f"OP-SQ-{code}",
        v2_firing_handoff_id=handoff.id,
        stock_location_id=location.id,
        qr_token=f"qr-token-sq-{code}-1234567890-abcdefghijklmnopqrst",
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


async def test_dimensiones_horno_faltantes_rechazan_layout(db_session: AsyncSession) -> None:
    """Un horno sin usable_width/depth/height_cm rechaza la creacion del layout con error 422."""
    kiln = await _kiln(db_session, "K-NODIMS")
    batch = await _batch(db_session, kiln, "HOR-NODIMS")
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
    placements = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=6,
            level_index=0,
            x_cm=Decimal("5"),
            y_cm=Decimal("10"),
            rotation_degrees=0,
        ),
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=1,
            unit_index=None,
            quantity=4,
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
    result = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=[],
        placements=[
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=0,
                unit_index=None,
                quantity=5,
                level_index=0,
                x_cm=Decimal("0"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            )
        ],
        user=USER,
    )
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
    result = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=[],
        placements=[
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=0,
                unit_index=None,
                quantity=8,
                level_index=0,
                x_cm=Decimal("0"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            )
        ],
        user=USER,
    )
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
    with pytest.raises(KilnLayoutPieceDimensionsMissingError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=[],
            placements=[
                PlacementSpec(
                    batch_assignment_id=asgn.id,
                    group_index=0,
                    unit_index=None,
                    quantity=3,
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
    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=[],
        placements=[
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=0,
                unit_index=None,
                quantity=5,
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
    with pytest.raises(KilnLayoutAssignmentMismatchError) as exc_info:
        await service.save_layout(
            batch_a.id,
            expected_version=0,
            levels=[],
            placements=[
                PlacementSpec(
                    batch_assignment_id=asgn_b.id,
                    group_index=0,
                    unit_index=None,
                    quantity=5,
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
    """Suma de placements 6 + 4 <= 10 pasa; 6 + 5 > 10 falla con 409."""
    kiln = await _kiln_with_dims(db_session, "K-QTY")
    batch = await _batch(db_session, kiln, "HOR-QTY")
    load, line = await _load(db_session, "L-QTY", quantity=10, unit_volume=Decimal("100"))
    asgn = await _assign_internal(db_session, batch, load, line, quantity=10)

    service = KilnBatchLayoutService(db_session)
    res = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=[],
        placements=[
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=0,
                unit_index=None,
                quantity=6,
                level_index=0,
                x_cm=Decimal("0"),
                y_cm=Decimal("0"),
                rotation_degrees=0,
            ),
            PlacementSpec(
                batch_assignment_id=asgn.id,
                group_index=1,
                unit_index=None,
                quantity=4,
                level_index=0,
                x_cm=Decimal("10"),
                y_cm=Decimal("10"),
                rotation_degrees=90,
            ),
        ],
        user=USER,
    )
    assert len(res.placements) == 2

    # 6 + 5 = 11 > 10 -> FAIL (409)
    with pytest.raises(KilnLayoutQuantityExceededError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=1,
            levels=[],
            placements=[
                PlacementSpec(
                    batch_assignment_id=asgn.id,
                    group_index=0,
                    unit_index=None,
                    quantity=6,
                    level_index=0,
                    x_cm=Decimal("0"),
                    y_cm=Decimal("0"),
                    rotation_degrees=0,
                ),
                PlacementSpec(
                    batch_assignment_id=asgn.id,
                    group_index=1,
                    unit_index=None,
                    quantity=5,
                    level_index=0,
                    x_cm=Decimal("10"),
                    y_cm=Decimal("10"),
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
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)

    service = KilnBatchLayoutService(db_session)
    placements = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=5,
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
        levels=[],
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
        levels=[],
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
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)

    service = KilnBatchLayoutService(db_session)
    p1 = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=None,
            quantity=2,
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
            quantity=3,
            level_index=0,
            x_cm=Decimal("5"),
            y_cm=Decimal("5"),
            rotation_degrees=90,
        )
    ]

    await service.save_layout(
        batch.id,
        expected_version=0,
        levels=[],
        placements=p1,
        user=USER,
        idempotency_key="idemp-diff-001",
    )

    with pytest.raises(KilnLayoutIdempotencyKeyReusedError) as exc_info:
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=[],
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
