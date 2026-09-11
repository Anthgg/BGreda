"""Superficie HTTP del Cotizador V2.

Ruta propia —`/api/v1/quotations-v2`— y no una variante de `/quotations`. Un
segmento distinto significa que ningun path de V2 puede caer por accidente en
un handler de Legacy ni al reves: no hay un solo patron que case con los dos.

Rutas delgadas, como el resto de la API: el router traduce HTTP y delega. Lo
que decidira dinero vivira en el servicio, donde se puede probar sin levantar
una peticion.

Quien cotiza es administracion, igual que en Legacy: poner un precio no es
ejecutar el trabajo.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import AdminUserDep, DbSessionDep, V2QuotationServiceDep
from app.models.quoter_v2 import V2Quotation, V2QuotationStatus
from app.schemas.quoter_v2 import (
    V2QuotationCreateIn,
    V2QuotationListItemOut,
    V2QuotationOut,
    V2QuotationPage,
)

router = APIRouter(prefix="/quotations-v2", tags=["cotizador-v2"])


def _present(fila: V2Quotation) -> V2QuotationOut:
    return V2QuotationOut(
        id=fila.id,
        code=fila.code,
        pricing_engine_version=fila.pricing_engine_version,
        status=fila.status,
        production_type=fila.production_type,
        customer_id=fila.customer_id,
        customer_name=fila.customer_name_snapshot,
        name=fila.name,
        notes=fila.notes,
        created_at=fila.created_at,
        updated_at=fila.updated_at,
    )


@router.get("", response_model=V2QuotationPage)
async def list_v2_quotations(
    service: V2QuotationServiceDep,
    _: AdminUserDep,
    status_filter: Annotated[V2QuotationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> V2QuotationPage:
    filas, total = await service.list(status=status_filter, limit=limit, offset=offset)
    return V2QuotationPage(
        items=[
            V2QuotationListItemOut(
                id=fila.id,
                code=fila.code,
                pricing_engine_version=fila.pricing_engine_version,
                status=fila.status,
                production_type=fila.production_type,
                customer_name=fila.customer_name_snapshot,
                name=fila.name,
                created_at=fila.created_at,
            )
            for fila in filas
        ],
        total=total,
    )


@router.post("", response_model=V2QuotationOut, status_code=status.HTTP_201_CREATED)
async def create_v2_quotation(
    payload: V2QuotationCreateIn,
    service: V2QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2QuotationOut:
    fila = await service.create_draft(payload.model_dump(), user=admin)
    resultado = _present(fila)
    await session.commit()
    return resultado


@router.get("/{quotation_id}", response_model=V2QuotationOut)
async def read_v2_quotation(
    quotation_id: int,
    service: V2QuotationServiceDep,
    _: AdminUserDep,
) -> V2QuotationOut:
    return _present(await service.get(quotation_id))
