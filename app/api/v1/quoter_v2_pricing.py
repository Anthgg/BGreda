"""Superficie HTTP del motor economico del Cotizador V2.

Dos rutas sobre una misma cosa: leer el resultado economico de una cotizacion y
cambiar su factor comercial. No hay mas que decidir aqui —los costos los fijan
010C a 010E— y por eso el cuerpo de entrada tiene un solo campo.

## Por que es de administracion

Devuelve el costo real, el costo de produccion, el gas y la ganancia. Es la
informacion interna del taller entera: lo que el cliente ve llegara en el PDF
de 010H, y son otros numeros.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Path

from app.api.deps import AdminUserDep, DbSessionDep, V2PricingServiceDep
from app.schemas.quoter_v2_pricing import V2PricingIn, V2PricingLineOut, V2PricingOut
from app.services.quoter_v2_pricing import PricingState

router = APIRouter(tags=["cotizador-v2"])


def _out(estado: PricingState) -> V2PricingOut:
    quotation = estado.quotation
    return V2PricingOut(
        materials_cost=quotation.materials_cost_total,
        labor_cost=quotation.labor_cost_total,
        illustration_cost=quotation.illustration_cost,
        space_cost=quotation.space_cost,
        administration_cost=quotation.administrative_cost_snapshot or Decimal(0),
        gas_cost=quotation.firing_gas_total,
        firing_commercial_cost=quotation.firing_commercial_total,
        # Se calcula aqui y no se lee de la columna generada: sobre un borrador
        # recien recalculado la columna todavia tiene el valor anterior —la
        # base la recalcula al confirmar— y devolverla daria una diferencia que
        # no corresponde a los dos totales que la acompanan.
        firing_difference=quotation.firing_commercial_total - quotation.firing_gas_total,
        direct_cost=quotation.direct_cost_total,
        real_cost=quotation.real_cost_total,
        production_cost=quotation.production_cost_total,
        commercial_factor=quotation.commercial_factor,
        factor_min=quotation.commercial_factor_min_snapshot,
        factor_max=quotation.commercial_factor_max_snapshot,
        price_min=quotation.price_min,
        price_target=quotation.price_target,
        negotiated_price=quotation.negotiated_price,
        currency_code=quotation.currency_code_snapshot,
        exchange_rate=quotation.exchange_rate_snapshot,
        tax_percent=quotation.tax_percent_snapshot,
        rounding_step=quotation.rounding_step_snapshot,
        subtotal=quotation.subtotal_amount,
        tax=quotation.tax_amount,
        total=quotation.total_amount,
        rounding_adjustment=quotation.rounding_adjustment,
        estimated_profit=quotation.estimated_profit,
        effective_margin_percent=quotation.effective_margin_percent,
        lines=[
            V2PricingLineOut(
                line_id=linea.id,
                product_name=linea.product_name_snapshot,
                quantity=linea.quantity,
                direct_cost=linea.direct_cost,
                firing_cost=linea.firing_commercial_cost,
                gas_cost=linea.firing_gas_cost,
                space_cost=linea.allocated_space_cost,
                general_cost=linea.allocated_general_cost,
                production_cost=linea.allocated_production_cost,
                real_cost=linea.allocated_real_cost,
                line_price=linea.line_price,
                unit_price_raw=linea.unit_price_raw,
                unit_price=linea.unit_price,
                line_subtotal=linea.line_subtotal,
                line_tax=linea.line_tax,
                line_total=linea.line_total,
                profit=linea.allocated_profit,
            )
            for linea in estado.lines
        ],
        warnings=estado.warnings,
    )


@router.get("/quotations-v2/{quotation_id}/pricing", response_model=V2PricingOut)
async def read_v2_pricing(
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2PricingServiceDep,
    _: AdminUserDep,
) -> V2PricingOut:
    """El resultado economico de una cotizacion.

    No confirma la transaccion: lo que el recalculo deje en memoria sirve para
    responder y se descarta. Leer no cambia un precio.
    """
    return _out(await service.pricing_state(quotation_id))


@router.put("/quotations-v2/{quotation_id}/pricing", response_model=V2PricingOut)
async def set_v2_pricing(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2PricingIn,
    service: V2PricingServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2PricingOut:
    """Cambia el factor comercial de la cotizacion.

    `exclude_unset` mantiene la semantica parcial del resto de la familia: lo
    que la pantalla no manda no llega aqui como `None` y no se lee como
    «quitalo».
    """
    estado = await service.set_pricing(
        quotation_id, payload.model_dump(exclude_unset=True), user=admin
    )
    resultado = _out(estado)
    await session.commit()
    return resultado
