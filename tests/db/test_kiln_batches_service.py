from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.firings import FiringType, Kiln
from app.models.inventory import StockLocation
from app.models.kiln_batches import (
    InternalLoad,
    InternalLoadLine,
    KilnBatch,
    KilnBatchAssignment,
    KilnBatchAssignmentStatus,
)
from app.models.production import ProductionOrder
from app.models.profile import UserRole
from app.models.quoter_v2 import (
    V2FiringMode,
    V2ProductionHandoff,
    V2Quotation,
    V2QuotationProduct,
)
from app.schemas.auth import AuthenticatedUser
from app.services.kiln_batches import (
    AssignItem,
    KilnBatchCapacityExceededError,
    KilnBatchExclusiveError,
    KilnBatchExclusiveNeedsEmptyError,
    KilnBatchService,
)

pytestmark = pytest.mark.asyncio


USER = AuthenticatedUser(
    id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
    email="admin@empresa.com",
    display_name="Administrador",
    role=UserRole.ADMIN,
)


async def _kiln(session: AsyncSession, code: str, capacity: Decimal = Decimal("1000")) -> Kiln:
    kiln = Kiln(
        code=code,
        name=f"Horno {code}",
        capacity_volume_cm3=capacity,
        firing_days_per_batch=3,
        active=True,
    )
    session.add(kiln)
    await session.flush()
    return kiln


async def _load(
    session: AsyncSession,
    code: str,
    *,
    quantity: int,
    unit_volume: Decimal,
    low: bool = True,
    high: bool = False,
) -> tuple[InternalLoad, InternalLoadLine]:
    load = InternalLoad(
        code=code,
        name=f"Carga {code}",
        low_fire_required=low,
        high_fire_required=high,
        piece_separation_cm=Decimal("0"),
        created_by=USER.id,
        created_by_name=USER.display_name,
    )
    session.add(load)
    await session.flush()
    line = InternalLoadLine(
        load_id=load.id,
        sort_order=1,
        name=f"Pieza {code}",
        quantity=quantity,
        length_cm=Decimal("1"),
        width_cm=Decimal("1"),
        height_cm=unit_volume,
        unit_volume_cm3=unit_volume,
        total_volume_cm3=Decimal(quantity) * unit_volume,
    )
    session.add(line)
    await session.flush()
    return load, line


async def _batch(
    session: AsyncSession,
    kiln: Kiln,
    code: str,
    *,
    firing_type: FiringType = FiringType.LOW,
) -> KilnBatch:
    batch = KilnBatch(
        code=code,
        kiln_id=kiln.id,
        firing_type=firing_type,
        scheduled_date=date.today(),
        kiln_name_snapshot=kiln.name,
        capacity_snapshot_cm3=kiln.capacity_volume_cm3,
        created_by=USER.id,
        created_by_name=USER.display_name,
    )
    session.add(batch)
    await session.flush()
    return batch


async def _v2_order(
    session: AsyncSession,
    code: str,
    *,
    kiln: Kiln,
    firing_mode: V2FiringMode,
    quantity: int,
    unit_volume: Decimal,
) -> tuple[ProductionOrder, V2QuotationProduct]:
    location = StockLocation(name=f"Almacen {code}", active=True)
    quotation = V2Quotation(
        code=f"CTZ-V2-{code}",
        customer_name_snapshot=f"Cliente {code}",
        firing_mode=firing_mode,
        low_fire_enabled=True,
        high_fire_enabled=False,
        kiln_id=kiln.id,
        kiln_name_snapshot=kiln.name,
        kiln_capacity_snapshot=kiln.capacity_volume_cm3,
        firing_count=1,
        low_fire_count=1,
        high_fire_count=0,
        firing_billed_load=Decimal("1"),
    )
    session.add_all([location, quotation])
    await session.flush()
    product = V2QuotationProduct(
        v2_quotation_id=quotation.id,
        sort_order=1,
        product_name_snapshot=f"Pieza {code}",
        quantity=quantity,
        unit_volume_cm3=unit_volume,
        total_volume_cm3=Decimal(quantity) * unit_volume,
    )
    handoff = V2ProductionHandoff(
        v2_quotation_id=quotation.id,
        commercial_fingerprint="a" * 64,
        created_by=USER.id,
        created_by_name=USER.display_name,
    )
    session.add_all([product, handoff])
    await session.flush()
    order = ProductionOrder(
        code=f"OP-{code}",
        v2_handoff_id=handoff.id,
        stock_location_id=location.id,
        qr_token=f"qr-token-{code}-1234567890-abcdefghijklmnopqrstuvwxyz",
        created_by=USER.id,
        created_by_name=USER.display_name,
    )
    session.add(order)
    await session.flush()
    return order, product


async def _assign(
    session: AsyncSession,
    batch_id: int,
    load_id: int,
    line_id: int,
    quantity: int,
    *,
    idempotency_key: str | None = None,
) -> None:
    await KilnBatchService(session).assign(
        batch_id,
        production_order_id=None,
        internal_load_id=load_id,
        items=[AssignItem(line_id=line_id, quantity=quantity)],
        expected_version=None,
        idempotency_key=idempotency_key,
        user=USER,
    )


async def _assign_order(
    session: AsyncSession,
    batch_id: int,
    order_id: int,
    line_id: int,
    quantity: int,
    *,
    idempotency_key: str | None = None,
) -> None:
    await KilnBatchService(session).assign(
        batch_id,
        production_order_id=order_id,
        internal_load_id=None,
        items=[AssignItem(line_id=line_id, quantity=quantity)],
        expected_version=None,
        idempotency_key=idempotency_key,
        user=USER,
    )


async def test_asignacion_idempotente_no_duplica_piezas(db_session: AsyncSession) -> None:
    kiln = await _kiln(db_session, "K-IDEM")
    load, line = await _load(db_session, "CI-IDEM", quantity=10, unit_volume=Decimal("50"))
    batch = await _batch(db_session, kiln, "HOR-IDEM")

    await _assign(db_session, batch.id, load.id, line.id, 4, idempotency_key="idem-asignar-001")
    await _assign(db_session, batch.id, load.id, line.id, 4, idempotency_key="idem-asignar-001")

    assignments = (
        await db_session.scalars(
            select(KilnBatchAssignment).where(
                KilnBatchAssignment.batch_id == batch.id,
                KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
            )
        )
    ).all()
    await db_session.refresh(batch, ["assigned_volume_cm3"])

    assert len(assignments) == 1
    assert assignments[0].quantity == 4
    assert batch.assigned_volume_cm3 == Decimal("200")


async def test_reparte_una_carga_de_120_por_ciento_en_dos_hornadas(
    db_session: AsyncSession,
) -> None:
    kiln = await _kiln(db_session, "K-SPLIT")
    load, line = await _load(db_session, "CI-SPLIT", quantity=120, unit_volume=Decimal("10"))
    full = await _batch(db_session, kiln, "HOR-SPLIT-100")
    rest = await _batch(db_session, kiln, "HOR-SPLIT-020")

    await _assign(db_session, full.id, load.id, line.id, 100, idempotency_key="split-100")
    await _assign(db_session, rest.id, load.id, line.id, 20, idempotency_key="split-020")

    await db_session.refresh(full, ["assigned_volume_cm3"])
    await db_session.refresh(rest, ["assigned_volume_cm3"])
    assigned = await db_session.scalar(
        select(func.sum(KilnBatchAssignment.quantity)).where(
            KilnBatchAssignment.internal_load_id == load.id,
            KilnBatchAssignment.internal_load_line_id == line.id,
            KilnBatchAssignment.status == KilnBatchAssignmentStatus.ACTIVE,
        )
    )

    assert full.assigned_volume_cm3 == Decimal("1000")
    assert rest.assigned_volume_cm3 == Decimal("200")
    assert assigned == 120


async def test_concurrencia_serializa_capacidad_y_rechaza_el_segundo_exceso(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    kiln = await _kiln(db_session, "K-CONC")
    base_load, base_line = await _load(
        db_session, "CI-CONC-BASE", quantity=65, unit_volume=Decimal("10")
    )
    first_load, first_line = await _load(
        db_session, "CI-CONC-030", quantity=30, unit_volume=Decimal("10")
    )
    second_load, second_line = await _load(
        db_session, "CI-CONC-020", quantity=20, unit_volume=Decimal("10")
    )
    batch = await _batch(db_session, kiln, "HOR-CONC")
    await _assign(db_session, batch.id, base_load.id, base_line.id, 65, idempotency_key="conc-base")
    await db_session.commit()

    async def try_assign(load_id: int, line_id: int, quantity: int, key: str) -> str:
        async with sessionmaker_for_tests() as session:
            try:
                await _assign(session, batch.id, load_id, line_id, quantity, idempotency_key=key)
                await session.commit()
                return "ok"
            except KilnBatchCapacityExceededError:
                await session.rollback()
                return "capacity"

    results = await asyncio.gather(
        try_assign(first_load.id, first_line.id, 30, "conc-030"),
        try_assign(second_load.id, second_line.id, 20, "conc-020"),
    )

    async with sessionmaker_for_tests() as session:
        saved = await session.get(KilnBatch, batch.id)
        assert saved is not None
        assert saved.assigned_volume_cm3 in {Decimal("850"), Decimal("950")}

    assert sorted(results) == ["capacity", "ok"]


async def test_exclusiva_necesita_hornada_vacia_y_bloquea_otras_ordenes(
    db_session: AsyncSession,
) -> None:
    kiln = await _kiln(db_session, "K-EXCL")
    shared_order, shared_line = await _v2_order(
        db_session,
        "EXCL-SHARED",
        kiln=kiln,
        firing_mode=V2FiringMode.SHARED,
        quantity=5,
        unit_volume=Decimal("20"),
    )
    exclusive_order, exclusive_line = await _v2_order(
        db_session,
        "EXCL-OWNER",
        kiln=kiln,
        firing_mode=V2FiringMode.EXCLUSIVE,
        quantity=5,
        unit_volume=Decimal("20"),
    )
    intruder_order, intruder_line = await _v2_order(
        db_session,
        "EXCL-INTR",
        kiln=kiln,
        firing_mode=V2FiringMode.SHARED,
        quantity=5,
        unit_volume=Decimal("20"),
    )
    occupied = await _batch(db_session, kiln, "HOR-EXCL-OCC")
    exclusive = await _batch(db_session, kiln, "HOR-EXCL")

    await _assign_order(
        db_session,
        occupied.id,
        shared_order.id,
        shared_line.id,
        1,
        idempotency_key="excl-shared-first",
    )
    with pytest.raises(KilnBatchExclusiveNeedsEmptyError):
        await _assign_order(
            db_session,
            occupied.id,
            exclusive_order.id,
            exclusive_line.id,
            1,
            idempotency_key="excl-needs-empty",
        )

    await _assign_order(
        db_session,
        exclusive.id,
        exclusive_order.id,
        exclusive_line.id,
        1,
        idempotency_key="excl-owner",
    )
    await db_session.refresh(exclusive, ["exclusive"])
    assert exclusive.exclusive is True

    with pytest.raises(KilnBatchExclusiveError):
        await _assign_order(
            db_session,
            exclusive.id,
            intruder_order.id,
            intruder_line.id,
            1,
            idempotency_key="excl-intruder",
        )
