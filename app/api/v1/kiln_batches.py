"""API de planificacion de hornadas. Fases 010L y 010M."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import (
    AdminUserDep,
    CurrentUserDep,
    DbSessionDep,
    KilnBatchLayoutServiceDep,
    KilnBatchServiceDep,
    WorkshopUserDep,
)
from app.models.firings import FiringType
from app.models.kiln_batches import KilnBatchSourceKind, KilnBatchStatus
from app.schemas.kiln_batches import (
    FiringPlanLineOut,
    FiringPlanOut,
    KilnBatchAssignItemIn,
    KilnBatchAssignmentCreateIn,
    KilnBatchAssignmentOut,
    KilnBatchAssignmentReleaseIn,
    KilnBatchCancelIn,
    KilnBatchCreateIn,
    KilnBatchLayoutLevelOut,
    KilnBatchLayoutOut,
    KilnBatchLayoutPlacementOut,
    KilnBatchLayoutUpdate,
    KilnBatchMoveIn,
    KilnBatchMoveOut,
    KilnBatchOut,
    KilnBatchPage,
    KilnBatchSuggestionOut,
    KilnBatchUpdateIn,
)
from app.services.kiln_batch_layout import (
    LayoutView,
    LevelSpec,
    PlacementSpec,
)
from app.services.kiln_batches import AssignItem, BatchView, FiringPlan

router = APIRouter(prefix="/kiln-batches", tags=["hornadas"])

LimitDep = Annotated[int, Query(ge=1, le=200)]
OffsetDep = Annotated[int, Query(ge=0)]


def _items(items: list[KilnBatchAssignItemIn]) -> list[AssignItem]:
    return [AssignItem(line_id=item.line_id, quantity=item.quantity) for item in items]


def _batch_out(view: BatchView) -> KilnBatchOut:
    batch = view.batch
    return KilnBatchOut(
        id=batch.id,
        code=batch.code,
        kiln_id=batch.kiln_id,
        kiln_name_snapshot=batch.kiln_name_snapshot,
        firing_type=batch.firing_type,
        scheduled_date=batch.scheduled_date,
        status=batch.status,
        capacity_snapshot_cm3=batch.capacity_snapshot_cm3,
        assigned_volume_cm3=batch.assigned_volume_cm3,
        occupancy_percent=view.occupancy_percent,
        available_percent=view.available_percent,
        available_cm3=view.available_cm3,
        exclusive=batch.exclusive,
        version=batch.version,
        notes=batch.notes,
        started_at=batch.started_at,
        completed_at=batch.completed_at,
        cancelled_at=batch.cancelled_at,
        cancel_reason=batch.cancel_reason,
        assignments=[
            KilnBatchAssignmentOut(
                id=a.id,
                batch_id=a.batch_id,
                source_kind=a.source_kind,
                production_order_id=a.production_order_id,
                internal_load_id=a.internal_load_id,
                line_id=a.line_id,
                product_name=a.product_name,
                quantity=a.quantity,
                unit_volume_cm3=a.unit_volume_cm3,
                assigned_volume_cm3=a.assigned_volume_cm3,
                firing_mode=a.firing_mode,
            )
            for a in view.assignments
        ],
    )


def _plan_out(plan: FiringPlan) -> FiringPlanOut:
    lines: list[FiringPlanLineOut] = []
    for line in plan.source.lines:
        low = plan.progress.get(FiringType.LOW, {}).get(line.line_id)
        high = plan.progress.get(FiringType.HIGH, {}).get(line.line_id)
        lines.append(
            FiringPlanLineOut(
                line_id=line.line_id,
                product_name=line.product_name,
                quantity=line.quantity,
                unit_volume_cm3=line.unit_volume_cm3,
                required_low=low.required if low is not None else None,
                assigned_low=low.assigned if low is not None else None,
                remaining_low=low.remaining if low is not None else None,
                required_high=high.required if high is not None else None,
                assigned_high=high.assigned if high is not None else None,
                remaining_high=high.remaining if high is not None else None,
            )
        )
    return FiringPlanOut(
        source_kind=plan.source.kind,
        production_order_id=plan.source.production_order_id,
        internal_load_id=plan.source.internal_load_id,
        code=plan.source.code,
        origin_code=plan.source.origin_code,
        customer_name=plan.source.customer_name,
        firing_mode=plan.source.firing_mode,
        needs_low=plan.source.needs_low,
        needs_high=plan.source.needs_high,
        glaze_required=plan.source.glaze_required,
        open=plan.source.open,
        lines=lines,
        batches=[_batch_out(batch) for batch in plan.batches],
    )


@router.get("", response_model=KilnBatchPage)
async def list_kiln_batches(
    service: KilnBatchServiceDep,
    _: CurrentUserDep,
    kiln_id: Annotated[int | None, Query(gt=0)] = None,
    firing_type: FiringType | None = None,
    batch_status: Annotated[KilnBatchStatus | None, Query(alias="status")] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    source_kind: KilnBatchSourceKind | None = None,
    limit: LimitDep = 50,
    offset: OffsetDep = 0,
) -> KilnBatchPage:
    items, total = await service.list_batches(
        kiln_id=kiln_id,
        firing_type=firing_type,
        status=batch_status,
        date_from=date_from,
        date_to=date_to,
        source_kind=source_kind,
        limit=limit,
        offset=offset,
    )
    return KilnBatchPage(
        items=[_batch_out(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=KilnBatchOut, status_code=status.HTTP_201_CREATED)
async def create_kiln_batch(
    payload: KilnBatchCreateIn,
    service: KilnBatchServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
    response: Response,
) -> KilnBatchOut:
    batch, created = await service.create(
        kiln_id=payload.kiln_id,
        firing_type=payload.firing_type,
        scheduled_date=payload.scheduled_date,
        notes=payload.notes,
        idempotency_key=payload.idempotency_key,
        user=actor,
    )
    result = _batch_out(await service.get(batch.id))
    await session.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return result


@router.post("/moves", response_model=KilnBatchMoveOut)
async def move_kiln_batch_assignments(
    payload: KilnBatchMoveIn,
    service: KilnBatchServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> KilnBatchMoveOut:
    from_batch, to_batch = await service.move(
        from_batch_id=payload.from_batch_id,
        to_batch_id=payload.to_batch_id,
        production_order_id=payload.production_order_id,
        internal_load_id=payload.internal_load_id,
        items=_items(payload.items),
        idempotency_key=payload.idempotency_key,
        user=actor,
    )
    result = KilnBatchMoveOut(from_batch=_batch_out(from_batch), to_batch=_batch_out(to_batch))
    await session.commit()
    return result


@router.get("/production-orders/{order_id}/firing-plan", response_model=FiringPlanOut)
async def production_order_firing_plan(
    order_id: int,
    service: KilnBatchServiceDep,
    _: CurrentUserDep,
) -> FiringPlanOut:
    return _plan_out(await service.plan_for(production_order_id=order_id))


@router.get(
    "/production-orders/{order_id}/batch-suggestions",
    response_model=list[KilnBatchSuggestionOut],
)
async def production_order_batch_suggestions(
    order_id: int,
    firing_type: FiringType,
    service: KilnBatchServiceDep,
    _: CurrentUserDep,
) -> list[KilnBatchSuggestionOut]:
    return [
        KilnBatchSuggestionOut(**suggestion.__dict__)
        for suggestion in await service.suggestions(
            production_order_id=order_id, firing_type=firing_type
        )
    ]


@router.get("/{batch_id}", response_model=KilnBatchOut)
async def get_kiln_batch(
    batch_id: int,
    service: KilnBatchServiceDep,
    _: CurrentUserDep,
) -> KilnBatchOut:
    return _batch_out(await service.get(batch_id))


@router.put("/{batch_id}", response_model=KilnBatchOut)
async def update_kiln_batch(
    batch_id: int,
    payload: KilnBatchUpdateIn,
    service: KilnBatchServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> KilnBatchOut:
    batch = await service.update(
        batch_id,
        scheduled_date=payload.scheduled_date,
        notes=payload.notes,
        notes_set=payload.notes_set,
        expected_version=payload.expected_version,
        user=actor,
    )
    result = _batch_out(await service.get(batch.id))
    await session.commit()
    return result


@router.post("/{batch_id}/assignments", response_model=KilnBatchOut)
async def assign_kiln_batch(
    batch_id: int,
    payload: KilnBatchAssignmentCreateIn,
    service: KilnBatchServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> KilnBatchOut:
    result = _batch_out(
        await service.assign(
            batch_id,
            production_order_id=payload.production_order_id,
            internal_load_id=payload.internal_load_id,
            items=_items(payload.items),
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            user=actor,
        )
    )
    await session.commit()
    return result


@router.post("/{batch_id}/assignments/release", response_model=KilnBatchOut)
async def release_kiln_batch_assignments(
    batch_id: int,
    payload: KilnBatchAssignmentReleaseIn,
    service: KilnBatchServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> KilnBatchOut:
    result = _batch_out(
        await service.release(
            batch_id,
            production_order_id=payload.production_order_id,
            internal_load_id=payload.internal_load_id,
            items=_items(payload.items),
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            user=actor,
        )
    )
    await session.commit()
    return result


@router.post("/{batch_id}/start", response_model=KilnBatchOut)
async def start_kiln_batch(
    batch_id: int,
    service: KilnBatchServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> KilnBatchOut:
    result = _batch_out(await service.start(batch_id, user=actor))
    await session.commit()
    return result


@router.post("/{batch_id}/complete", response_model=KilnBatchOut)
async def complete_kiln_batch(
    batch_id: int,
    service: KilnBatchServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> KilnBatchOut:
    result = _batch_out(await service.complete(batch_id, user=actor))
    await session.commit()
    return result


@router.post("/{batch_id}/cancel", response_model=KilnBatchOut)
async def cancel_kiln_batch(
    batch_id: int,
    payload: KilnBatchCancelIn,
    service: KilnBatchServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> KilnBatchOut:
    result = _batch_out(await service.cancel(batch_id, reason=payload.reason, user=admin))
    await session.commit()
    return result


# ---------------------------------------------------------------------------
# Fase 010M: layout fisico del horno
# ---------------------------------------------------------------------------

def _layout_out(view: LayoutView) -> KilnBatchLayoutOut:
    """Convierte un LayoutView al schema de respuesta de la API."""
    return KilnBatchLayoutOut(
        batch_id=view.layout.batch_id,
        layout_id=view.layout.id,
        version=view.layout.version,
        kiln_width_cm_snapshot=view.layout.kiln_width_cm_snapshot,
        kiln_depth_cm_snapshot=view.layout.kiln_depth_cm_snapshot,
        kiln_height_cm_snapshot=view.layout.kiln_height_cm_snapshot,
        updated_at=view.layout.updated_at,
        levels=[
            KilnBatchLayoutLevelOut(
                id=lvl.id,
                level_index=lvl.level_index,
                name=lvl.name,
                z_cm=lvl.z_cm,
                usable_height_cm=lvl.usable_height_cm,
                plate_label=lvl.plate_label,
                plate_thickness_cm=lvl.plate_thickness_cm,
            )
            for lvl in view.levels
        ],
        placements=[
            KilnBatchLayoutPlacementOut(
                id=plc.id,
                batch_assignment_id=plc.batch_assignment_id,
                group_index=plc.group_index,
                unit_index=plc.unit_index,
                quantity=plc.quantity,
                level_index=plc.level_index,
                x_cm=plc.x_cm,
                y_cm=plc.y_cm,
                rotation_degrees=plc.rotation_degrees,
                piece_length_cm_snapshot=plc.piece_length_cm_snapshot,
                piece_width_cm_snapshot=plc.piece_width_cm_snapshot,
                piece_height_cm_snapshot=plc.piece_height_cm_snapshot,
                separation_cm_snapshot=plc.separation_cm_snapshot,
            )
            for plc in view.placements
        ],
    )


@router.get("/{batch_id}/layout", response_model=KilnBatchLayoutOut)
async def get_kiln_batch_layout(
    batch_id: int,
    layout_service: KilnBatchLayoutServiceDep,
    _: CurrentUserDep,
) -> KilnBatchLayoutOut:
    """Devuelve el layout fisico de una hornada.

    Funciona para cualquier estado (PLANNED, STARTED, COMPLETED, CANCELLED).
    Si todavia no existe layout devuelve 404.
    """
    return _layout_out(await layout_service.get_layout(batch_id))


@router.put("/{batch_id}/layout", response_model=KilnBatchLayoutOut)
async def save_kiln_batch_layout(
    batch_id: int,
    payload: KilnBatchLayoutUpdate,
    layout_service: KilnBatchLayoutServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
) -> KilnBatchLayoutOut:
    """Guarda (crea o reemplaza) el layout fisico de una hornada.

    Solo permitido cuando la hornada esta en estado PLANNED.

    - `expected_version = 0`: creacion inicial (no debe existir layout).
    - `expected_version = N`: actualizacion; debe coincidir con la version actual.

    El PUT es un reemplazo completo y atomico de todos los niveles y placements.
    """
    levels = [
        LevelSpec(
            level_index=lvl.level_index,
            name=lvl.name,
            z_cm=lvl.z_cm,
            usable_height_cm=lvl.usable_height_cm,
            plate_label=lvl.plate_label,
            plate_thickness_cm=lvl.plate_thickness_cm,
        )
        for lvl in payload.levels
    ]
    placements = [
        PlacementSpec(
            batch_assignment_id=p.batch_assignment_id,
            group_index=p.group_index,
            unit_index=p.unit_index,
            quantity=p.quantity,
            level_index=p.level_index,
            x_cm=p.x_cm,
            y_cm=p.y_cm,
            rotation_degrees=p.rotation_degrees,
            piece_length_cm_snapshot=p.piece_length_cm_snapshot,
            piece_width_cm_snapshot=p.piece_width_cm_snapshot,
            piece_height_cm_snapshot=p.piece_height_cm_snapshot,
            separation_cm_snapshot=p.separation_cm_snapshot,
        )
        for p in payload.placements
    ]
    result = _layout_out(
        await layout_service.save_layout(
            batch_id,
            expected_version=payload.expected_version,
            levels=levels,
            placements=placements,
            user=actor,
        )
    )
    await session.commit()
    return result

