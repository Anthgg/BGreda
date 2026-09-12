"""Superficie HTTP de la quema del Cotizador V2.

Dos rutas sobre una misma cosa: leer la quema de una cotizacion y cambiarla.
Los hornos y sus tarifas NO se administran aqui —viven en la configuracion V2,
desde 010B— porque son politica del taller y valen para todo lo que se cotice
despues. Lo que esta ruta cambia afecta a UNA cotizacion.

## Por que es de administracion

Lo mismo que en 010D: el resto del Cotizador V2 ya era solo de administracion
desde 010A, y la quema expone el costo real del gas y la diferencia entre lo
que se cobra y lo que cuesta. Eso es informacion de margen.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from app.api.deps import AdminUserDep, DbSessionDep, V2FiringServiceDep
from app.schemas.quoter_v2_firing import (
    V2FiringIn,
    V2FiringLineOut,
    V2FiringOut,
    V2KilnOptionOut,
)
from app.services.quoter_v2_firing import FiringState

router = APIRouter(tags=["cotizador-v2"])


def _out(estado: FiringState) -> V2FiringOut:
    quotation = estado.quotation
    return V2FiringOut(
        production_type=quotation.production_type.value,
        customer_kind=quotation.customer_kind,
        kiln_id=quotation.kiln_id,
        kiln_name=quotation.kiln_name_snapshot,
        kiln_capacity_cm3=quotation.kiln_capacity_snapshot,
        total_volume_cm3=quotation.firing_total_volume_cm3,
        occupancy_percent=quotation.firing_occupancy_percent,
        firing_count=quotation.firing_count,
        # Una cotizacion de 010A tiene los dos en NULL: nacio antes de que
        # existiera la quema. Hacia fuera eso es «apagada», que es lo que de
        # hecho ocurre —no tiene hornadas ni costo—.
        low_fire_enabled=bool(quotation.low_fire_enabled),
        high_fire_enabled=bool(quotation.high_fire_enabled),
        low_fire_count=quotation.low_fire_count,
        high_fire_count=quotation.high_fire_count,
        batch_loads=list(estado.batch_loads),
        gas_cost_low=quotation.gas_cost_low_snapshot,
        gas_cost_high=quotation.gas_cost_high_snapshot,
        gas_low_is_override=quotation.gas_low_is_override,
        gas_high_is_override=quotation.gas_high_is_override,
        commercial_rate_low=quotation.commercial_rate_low_snapshot,
        commercial_rate_high=quotation.commercial_rate_high_snapshot,
        commercial_low_is_override=quotation.commercial_low_is_override,
        commercial_high_is_override=quotation.commercial_high_is_override,
        gas_total=quotation.firing_gas_total,
        commercial_total=quotation.firing_commercial_total,
        # Se calcula aqui y no se lee de la columna generada a proposito: sobre
        # un borrador acabado de recalcular, la columna todavia tiene el valor
        # anterior —la base la recalcula al confirmar— y devolverla daria una
        # diferencia que no corresponde a los dos totales que la acompanan.
        difference=quotation.firing_commercial_total - quotation.firing_gas_total,
        recommended_kiln_id=estado.recommended_kiln_id,
        kilns=[
            V2KilnOptionOut(
                kiln_id=horno.kiln_id,
                code=horno.code,
                name=horno.name,
                capacity_cm3=horno.capacity_cm3,
                active=horno.active,
                occupancy_percent=horno.occupancy_percent,
                firing_count=horno.firing_count,
                has_rates=horno.has_rates,
            )
            for horno in estado.kilns
        ],
        lines=[
            V2FiringLineOut(
                line_id=linea.id,
                product_name=linea.product_name_snapshot,
                quantity=linea.quantity,
                total_volume_cm3=linea.total_volume_cm3,
                occupancy_percent=linea.firing_occupancy_percent,
                volume_share_percent=linea.firing_volume_share_percent,
                commercial_cost=linea.firing_commercial_cost,
                gas_cost=linea.firing_gas_cost,
            )
            for linea in estado.lines
        ],
        warnings=estado.warnings,
    )


@router.get("/quotations-v2/{quotation_id}/firing", response_model=V2FiringOut)
async def read_v2_firing(
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2FiringServiceDep,
    _: AdminUserDep,
) -> V2FiringOut:
    """La quema de una cotizacion.

    No confirma la transaccion: lo que el recalculo deje en memoria sirve para
    responder y se descarta. Leer no cambia una cotizacion.
    """
    return _out(await service.firing_state(quotation_id))


@router.put("/quotations-v2/{quotation_id}/firing", response_model=V2FiringOut)
async def set_v2_firing(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2FiringIn,
    service: V2FiringServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2FiringOut:
    """Cambia el horno, los procesos, el tipo de cliente o las tarifas pactadas.

    `exclude_unset` es lo que hace que el cuerpo se aplique como un PARCHE: sin
    el, todo lo que la pantalla no mande llegaria aqui como `None` y se leeria
    como «quitalo», borrando en silencio un acuerdo de tarifa.

    El verbo es PUT, como en 010C y 010D, aunque el cuerpo sea parcial: lo que
    se envia es el estado de la quema de esa cotizacion, y tener ademas un
    PATCH seria una segunda puerta a lo mismo.
    """
    estado = await service.set_firing(
        quotation_id, payload.model_dump(exclude_unset=True), user=admin
    )
    resultado = _out(estado)
    await session.commit()
    return resultado
