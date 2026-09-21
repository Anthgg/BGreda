"""Pruebas del servicio de layout fisico del horno (Fase 010M - M1).

Cubre:
1. Rechazo cuando el horno no tiene dimensiones lineales (422)
2. Creacion inicial con expected_version = 0 (version 1)
3. Actualizacion con expected_version correcto (version incrementada)
4. Rechazo por version obsoleta (409)
5. Inmutabilidad de snapshot del horno tras modificacion del maestro
6. Inmutabilidad de snapshot de piezas
7. Estados de solo lectura (STARTED, COMPLETED, CANCELLED): GET funciona, PUT rechazado
8. Rechazo de assignment perteneciente a otro batch (422)
9. Validacion de rotacion (0/90 permitido)
10. Validacion de cantidad (suma de placements <= cantidad de la asignacion)
11. Idempotencia: reintentos con misma clave no duplican placements
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.firings import Kiln
from app.models.kiln_batches import (
    KilnBatch,
    KilnBatchAssignment,
    KilnBatchStatus,
)
from app.models.profile import UserRole
from app.schemas.auth import AuthenticatedUser
from app.services.kiln_batch_layout import (
    KilnBatchLayoutService,
    KilnLayoutAlreadyExistsError,
    KilnLayoutAssignmentMismatchError,
    KilnLayoutDimensionsMissingError,
    KilnLayoutNotEditableError,
    KilnLayoutNotFoundError,
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
) -> Kiln:
    kiln = await _kiln(session, code)
    kiln.usable_width_cm = width
    kiln.usable_depth_cm = depth
    kiln.usable_height_cm = height
    await session.flush()
    return kiln


async def _assign_raw(
    session: AsyncSession,
    batch: KilnBatch,
    load_id: int,
    line_id: int,
    quantity: int,
) -> KilnBatchAssignment:
    assignment = KilnBatchAssignment(
        batch_id=batch.id,
        source_kind="INTERNAL",
        internal_load_id=load_id,
        internal_load_line_id=line_id,
        quantity=quantity,
        unit_volume_snapshot_cm3=Decimal("100"),
        assigned_volume_cm3=Decimal(quantity * 100),
        firing_mode="SHARED",
        product_name_snapshot="Pieza test",
    )
    session.add(assignment)
    await session.flush()
    return assignment


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


async def test_crear_y_obtener_layout(db_session: AsyncSession) -> None:
    """Crear layout con expected_version=0 devuelve version=1 y almacena niveles y placements."""
    kiln = await _kiln_with_dims(db_session, "K-LAYOUT-1")
    batch = await _batch(db_session, kiln, "HOR-LAYOUT-1")
    load, line = await _load(db_session, "L-1", quantity=10, unit_volume=Decimal("100"))
    asgn = await _assign_raw(db_session, batch, load.id, line.id, quantity=10)

    service = KilnBatchLayoutService(db_session)

    levels = [
        LevelSpec(
            level_index=0,
            name="Nivel Inferior",
            z_cm=Decimal("0"),
            usable_height_cm=Decimal("20"),
            plate_label="Placa 1",
            plate_thickness_cm=Decimal("1.5"),
        ),
        LevelSpec(
            level_index=1,
            name="Nivel Superior",
            z_cm=Decimal("21.5"),
            usable_height_cm=Decimal("18.5"),
            plate_label="Placa 2",
            plate_thickness_cm=Decimal("1.5"),
        ),
    ]
    placements = [
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=0,
            unit_index=0,
            quantity=6,
            level_index=0,
            x_cm=Decimal("5"),
            y_cm=Decimal("10"),
            rotation_degrees=0,
            piece_length_cm_snapshot=Decimal("10"),
            piece_width_cm_snapshot=Decimal("8"),
            piece_height_cm_snapshot=Decimal("15"),
            separation_cm_snapshot=Decimal("1"),
        ),
        PlacementSpec(
            batch_assignment_id=asgn.id,
            group_index=1,
            unit_index=1,
            quantity=4,
            level_index=1,
            x_cm=Decimal("15"),
            y_cm=Decimal("20"),
            rotation_degrees=90,
            piece_length_cm_snapshot=Decimal("10"),
            piece_width_cm_snapshot=Decimal("8"),
            piece_height_cm_snapshot=Decimal("15"),
            separation_cm_snapshot=Decimal("1"),
        ),
    ]

    result = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=levels,
        placements=placements,
        user=USER,
    )

    assert result.layout.batch_id == batch.id
    assert result.layout.version == 1
    assert result.layout.kiln_width_cm_snapshot == Decimal("60")
    assert result.layout.kiln_depth_cm_snapshot == Decimal("50")
    assert result.layout.kiln_height_cm_snapshot == Decimal("40")
    assert len(result.levels) == 2
    assert len(result.placements) == 2

    # GET layout
    got = await service.get_layout(batch.id)
    assert got.layout.id == result.layout.id
    assert got.layout.version == 1
    assert len(got.levels) == 2
    assert len(got.placements) == 2
    assert got.placements[0].rotation_degrees == 0
    assert got.placements[1].rotation_degrees == 90


async def test_crear_layout_cuando_ya_existe_con_version_0_falla(
    db_session: AsyncSession,
) -> None:
    """Si ya existe un layout, reintentar con expected_version=0 lanza error."""
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
    """Actualizar con expected_version correcto incrementa la version; version vieja lanza 409."""
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

    # Actualizar con version 1 -> nueva version 2
    res2 = await service.save_layout(
        batch.id,
        expected_version=1,
        levels=[],
        placements=[],
        user=USER,
    )
    assert res2.layout.version == 2

    # Intentar actualizar con version obsoleta 1 -> 409
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
    """Modificar el maestro Kiln despues de crear el layout NO altera el snapshot del layout."""
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

    # GET layout
    got = await service.get_layout(batch.id)
    assert got.layout.kiln_width_cm_snapshot == Decimal("60")
    assert got.layout.kiln_depth_cm_snapshot == Decimal("50")
    assert got.layout.kiln_height_cm_snapshot == Decimal("40")


async def test_snapshot_pieza_en_placement(db_session: AsyncSession) -> None:
    """Las dimensiones de la pieza quedan congeladas en el placement."""
    kiln = await _kiln_with_dims(db_session, "K-PSNAP")
    batch = await _batch(db_session, kiln, "HOR-PSNAP")
    load, line = await _load(db_session, "L-PSNAP", quantity=5, unit_volume=Decimal("100"))
    asgn = await _assign_raw(db_session, batch, load.id, line.id, quantity=5)

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
                piece_length_cm_snapshot=Decimal("12.5"),
                piece_width_cm_snapshot=Decimal("8.0"),
                piece_height_cm_snapshot=Decimal("6.0"),
                separation_cm_snapshot=Decimal("0.5"),
            )
        ],
        user=USER,
    )

    # Modificar la linea original
    line.length_cm = Decimal("99")
    line.width_cm = Decimal("99")
    line.height_cm = Decimal("99")
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
        # Crear layout cuando aun estaba editable
        await service.save_layout(
            batch.id,
            expected_version=0,
            levels=[],
            placements=[],
            user=USER,
        )

        # Cambiar estado cumpliendo ck_kiln_batches_status_timestamps_coherent
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

        # GET debe pasar
        view = await service.get_layout(batch.id)
        assert view.layout.batch_id == batch.id

        # PUT debe fallar
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
    asgn_b = await _assign_raw(db_session, batch_b, load.id, line.id, quantity=5)

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
                    piece_length_cm_snapshot=Decimal("10"),
                    piece_width_cm_snapshot=Decimal("10"),
                    piece_height_cm_snapshot=Decimal("10"),
                    separation_cm_snapshot=Decimal("0"),
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
    asgn = await _assign_raw(db_session, batch, load.id, line.id, quantity=10)

    service = KilnBatchLayoutService(db_session)

    # 6 + 4 = 10 -> PASS
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
                piece_length_cm_snapshot=Decimal("5"),
                piece_width_cm_snapshot=Decimal("5"),
                piece_height_cm_snapshot=Decimal("5"),
                separation_cm_snapshot=Decimal("0"),
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
                piece_length_cm_snapshot=Decimal("5"),
                piece_width_cm_snapshot=Decimal("5"),
                piece_height_cm_snapshot=Decimal("5"),
                separation_cm_snapshot=Decimal("0"),
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
                    piece_length_cm_snapshot=Decimal("5"),
                    piece_width_cm_snapshot=Decimal("5"),
                    piece_height_cm_snapshot=Decimal("5"),
                    separation_cm_snapshot=Decimal("0"),
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
                    piece_length_cm_snapshot=Decimal("5"),
                    piece_width_cm_snapshot=Decimal("5"),
                    piece_height_cm_snapshot=Decimal("5"),
                    separation_cm_snapshot=Decimal("0"),
                ),
            ],
            user=USER,
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "KILN_LAYOUT_QUANTITY_EXCEEDED"


async def test_reintento_idempotente_no_duplica_placements(db_session: AsyncSession) -> None:
    """Enviar el mismo PUT con la misma idempotency_key devuelve el estado sin duplicar."""
    kiln = await _kiln_with_dims(db_session, "K-IDEM")
    batch = await _batch(db_session, kiln, "HOR-IDEM")
    load, line = await _load(db_session, "L-IDEM", quantity=5, unit_volume=Decimal("100"))
    asgn = await _assign_raw(db_session, batch, load.id, line.id, quantity=5)

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
            piece_length_cm_snapshot=Decimal("5"),
            piece_width_cm_snapshot=Decimal("5"),
            piece_height_cm_snapshot=Decimal("5"),
            separation_cm_snapshot=Decimal("0"),
        )
    ]

    res1 = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=[],
        placements=placements,
        user=USER,
        idempotency_key="idemp-layout-001",
    )
    assert res1.layout.version == 1
    assert len(res1.placements) == 1

    # Reintento con misma clave y mismo payload
    res2 = await service.save_layout(
        batch.id,
        expected_version=0,
        levels=[],
        placements=placements,
        user=USER,
        idempotency_key="idemp-layout-001",
    )
    assert res2.layout.version == 1
    assert len(res2.placements) == 1


async def test_get_layout_inexistente_404(db_session: AsyncSession) -> None:
    """GET de un batch que existe pero no tiene layout devuelve 404."""
    kiln = await _kiln_with_dims(db_session, "K-404")
    batch = await _batch(db_session, kiln, "HOR-404")
    service = KilnBatchLayoutService(db_session)

    with pytest.raises(KilnLayoutNotFoundError) as exc_info:
        await service.get_layout(batch.id)
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "KILN_LAYOUT_NOT_FOUND"
