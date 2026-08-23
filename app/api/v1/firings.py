"""Rutas REST de hornos, tarifas y hojas de quema.

Reparto de permisos (§27): ADMIN captura y decide; OPERATOR consulta y simula.
La restriccion la impone la dependencia del backend, no la interfaz.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    AdminUserDep,
    CurrentUserDep,
    DbSessionDep,
    FiringServiceDep,
    KilnServiceDep,
)
from app.models.firings import FiringStatus, FiringType
from app.schemas.firings import (
    FiringCalculateIn,
    FiringCalculateOut,
    FiringIn,
    FiringOut,
    FiringPage,
    KilnCreate,
    KilnOccupancyFactorIn,
    KilnOccupancyFactorOut,
    KilnOut,
    KilnPage,
    KilnRateIn,
    KilnRateOut,
    KilnUpdate,
)

router = APIRouter(tags=["quemas"])


# ---------------------------------------------------------------------------
# Maestro de hornos
# ---------------------------------------------------------------------------
@router.get("/kilns", response_model=KilnPage)
async def list_kilns(
    service: KilnServiceDep,
    _: CurrentUserDep,
    search: str | None = Query(None, description="Busqueda por nombre o codigo"),
    active: bool | None = Query(None, description="Filtrar por estado activo"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> KilnPage:
    """Lista los hornos con su tarifa vigente y su tabla de factores."""
    items, total = await service.list_kilns(
        search=search, active=active, limit=limit, offset=offset
    )
    return KilnPage(items=items, total=total, limit=limit, offset=offset)


@router.post("/kilns", response_model=KilnOut, status_code=status.HTTP_201_CREATED)
async def create_kiln(
    payload: KilnCreate,
    service: KilnServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> KilnOut:
    """Crea un horno. El codigo lo genera el backend."""
    kiln = await service.create_kiln(payload, user=admin)
    await session.commit()
    return kiln


@router.get("/kilns/{kiln_id}", response_model=KilnOut)
async def get_kiln(kiln_id: int, service: KilnServiceDep, _: CurrentUserDep) -> KilnOut:
    return await service.get_kiln(kiln_id)


@router.put("/kilns/{kiln_id}", response_model=KilnOut)
async def update_kiln(
    kiln_id: int,
    payload: KilnUpdate,
    service: KilnServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> KilnOut:
    """Actualiza nombre, capacidad, estado o notas del horno."""
    kiln = await service.update_kiln(kiln_id, payload, user=admin)
    await session.commit()
    return kiln


@router.put(
    "/kilns/{kiln_id}/occupancy-factors",
    response_model=list[KilnOccupancyFactorOut],
)
async def set_kiln_occupancy_factors(
    kiln_id: int,
    payload: list[KilnOccupancyFactorIn],
    service: KilnServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> list[KilnOccupancyFactorOut]:
    """Configura o reemplaza la tabla completa de factores de ocupacion del horno."""
    factors = await service.set_occupancy_factors(kiln_id, payload, user=admin)
    await session.commit()
    return factors


# ---------------------------------------------------------------------------
# Tarifas
# ---------------------------------------------------------------------------
@router.get("/kilns/{kiln_id}/rates", response_model=list[KilnRateOut])
async def list_kiln_rates(
    kiln_id: int, service: KilnServiceDep, _: CurrentUserDep
) -> list[KilnRateOut]:
    """Historial completo de tarifas del horno, vigentes y cerradas."""
    return await service.list_rates(kiln_id)


@router.post(
    "/kilns/{kiln_id}/rates",
    response_model=KilnRateOut,
    status_code=status.HTTP_201_CREATED,
)
async def set_kiln_rate(
    kiln_id: int,
    payload: KilnRateIn,
    service: KilnServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> KilnRateOut:
    """Fija la tarifa vigente cerrando la anterior.

    No sobrescribe: las quemas ya confirmadas conservan el importe que se les
    aplico.
    """
    rate = await service.set_rate(kiln_id, payload, user=admin)
    await session.commit()
    return rate


# ---------------------------------------------------------------------------
# Simulador
# ---------------------------------------------------------------------------
@router.post("/firings/calculate", response_model=FiringCalculateOut)
async def calculate_firing(
    payload: FiringCalculateIn,
    service: FiringServiceDep,
    _: CurrentUserDep,
) -> FiringCalculateOut:
    """Simula el costo de una hoja de quema.

    Operacion de solo lectura: no inserta, no actualiza, no consume correlativo
    y no genera ningun movimiento de inventario.
    """
    return await service.calculate(payload)


# ---------------------------------------------------------------------------
# Hojas de quema
# ---------------------------------------------------------------------------
@router.get("/firings", response_model=FiringPage)
async def list_firings(
    service: FiringServiceDep,
    _: CurrentUserDep,
    search: Annotated[str | None, Query(description="Busqueda por codigo")] = None,
    firing_status: Annotated[FiringStatus | None, Query(alias="status")] = None,
    kiln_id: Annotated[int | None, Query(description="Solo hojas que usan este horno")] = None,
    firing_type: Annotated[
        FiringType | None, Query(description="Solo hojas con este tipo de quema")
    ] = None,
    date_from: Annotated[date | None, Query(description="Fecha de quema desde")] = None,
    date_to: Annotated[date | None, Query(description="Fecha de quema hasta")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FiringPage:
    """Listado con busqueda, filtros y paginacion resueltos en el servidor."""
    items, total = await service.list_firings(
        search=search,
        status=firing_status,
        kiln_id=kiln_id,
        firing_type=firing_type,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return FiringPage(items=items, total=total, limit=limit, offset=offset)


@router.post("/firings", response_model=FiringOut, status_code=status.HTTP_201_CREATED)
async def create_firing(
    payload: FiringIn,
    service: FiringServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> FiringOut:
    """Crea una hoja en borrador. El correlativo lo emite el backend."""
    firing = await service.create(payload, user=admin)
    await session.commit()
    return firing


@router.get("/firings/{firing_id}", response_model=FiringOut)
async def get_firing(firing_id: int, service: FiringServiceDep, _: CurrentUserDep) -> FiringOut:
    """Detalle completo con sesiones, piezas y reparto."""
    return await service.get(firing_id)


@router.put("/firings/{firing_id}", response_model=FiringOut)
async def update_firing(
    firing_id: int,
    payload: FiringIn,
    service: FiringServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> FiringOut:
    """Actualiza una hoja en borrador y recalcula sus costos."""
    firing = await service.update(firing_id, payload, user=admin)
    await session.commit()
    return firing


@router.post("/firings/{firing_id}/confirm", response_model=FiringOut)
async def confirm_firing(
    firing_id: int,
    service: FiringServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> FiringOut:
    """Confirma la hoja: recalcula, valida capacidad y congela los snapshots."""
    firing = await service.confirm(firing_id, user=admin)
    await session.commit()
    return firing


@router.post("/firings/{firing_id}/cancel", response_model=FiringOut)
async def cancel_firing(
    firing_id: int,
    service: FiringServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> FiringOut:
    """Anula la hoja. No se elimina ninguna fila."""
    firing = await service.cancel(firing_id, user=admin)
    await session.commit()
    return firing
