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

from fastapi import APIRouter, Path, Query, status

from app.api.deps import AdminUserDep, DbSessionDep, V2QuotationServiceDep
from app.models.quoter_v2 import V2Quotation, V2QuotationStatus
from app.schemas.quoter_v2 import (
    V2QuotationCreateIn,
    V2QuotationListItemOut,
    V2QuotationOut,
    V2QuotationPage,
    V2QuotationUpdateIn,
)
from app.services.quoter_v2_firing import refresh_firing
from app.services.quoter_v2_pricing import refresh_pricing

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
        customer_kind=fila.customer_kind,
        tax_percent=fila.tax_percent_snapshot,
        currency_code=fila.currency_code_snapshot,
        currency_symbol=fila.currency_symbol_snapshot,
        exchange_rate=fila.exchange_rate_snapshot,
        validity_days=fila.validity_days_snapshot,
        workday_hours=fila.workday_hours_snapshot,
        space_service_cost_per_day=fila.space_service_cost_per_day_snapshot,
        administrative_cost=fila.administrative_cost_snapshot,
        commercial_factor=fila.commercial_factor,
        commercial_factor_min=fila.commercial_factor_min_snapshot,
        commercial_factor_max=fila.commercial_factor_max_snapshot,
        low_fire_enabled=fila.low_fire_enabled,
        high_fire_enabled=fila.high_fire_enabled,
        settings_version=fila.settings_version_snapshot,
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


@router.put("/{quotation_id}", response_model=V2QuotationOut)
async def update_v2_quotation(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2QuotationUpdateIn,
    service: V2QuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2QuotationOut:
    """Cambia la cabecera de un borrador: cliente, nombre, moneda, tipo.

    `exclude_unset` mantiene la semantica parcial de toda la familia. Aqui
    importa mas que en ningun otro sitio: el flujo de 010G deja volver atras, y
    abrir el primer paso para mirar no puede reescribir lo que ya se decidio.
    """
    fila = await service.update_draft(
        quotation_id, payload.model_dump(exclude_unset=True), user=admin
    )
    # La moneda y el tipo de produccion mueven el horno y, con el, todo el
    # precio: recalcular aqui evita que el resumen ensene cifras de antes.
    await refresh_firing(session, fila)
    await refresh_pricing(session, fila)
    # `updated_at` se calcula con `onupdate=func.now()`, de modo que el UPDATE
    # la deja expirada y leerla exige otra consulta. Se pide explicitamente:
    # dejar que el atributo se cargue solo revienta con `MissingGreenlet`,
    # porque una carga perezosa no puede esperar a nadie desde codigo sincrono.
    #
    # Primero el flush, para que los recalculos de arriba esten escritos: un
    # refresco sobre cambios sin consolidar los sustituye por lo que haya en la
    # base. Y SOLO `updated_at`: un refresco a ciegas expira tambien el resto
    # de la fila y obliga a releerla entera para nada.
    await session.flush()
    await session.refresh(fila, attribute_names=["updated_at"])
    resultado = _present(fila)
    await session.commit()
    return resultado


@router.get("/{quotation_id}", response_model=V2QuotationOut)
async def read_v2_quotation(
    # `ge=1`: un id no positivo se rechaza en la capa HTTP, sin llegar a
    # consultar la base para acabar en el mismo 404.
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2QuotationServiceDep,
    _: AdminUserDep,
) -> V2QuotationOut:
    return _present(await service.get(quotation_id))
