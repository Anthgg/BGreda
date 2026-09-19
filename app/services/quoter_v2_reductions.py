"""Reducciones del Cotizador V2: como bajar el precio si el cliente lo pide.

Fase 010J, hoja «Reducciones» del Excel final. Para cada palanca se estima el
subtotal que quedaria:

- otro horno, si cobraria menos por la misma quema;
- bajar el factor al minimo (nunca de x2);
- quitar la ilustracion;
- quitar los adicionales;
- hacer con personal interno lo que hoy hace personal externo;
- quema compartida en vez de exclusiva/urgente.

## Lo que este modulo NO hace

**No aplica nada.** Es de solo lectura: no escribe la cotizacion ni ningun
maestro, y la ruta no confirma la transaccion. Cada sugerencia se aplica, si
alguien quiere, desde la pantalla que la controla.

**No simula disponibilidad del taller** («produccion futura»): eso es la
planificacion de hornadas, y no pertenece a esta fase.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.quoter_v2_firing import SHARED, billed_load, firing_amount
from app.core.quoter_v2_pricing import quantize_money, to_base_currency
from app.core.quoter_v2_reductions import (
    estimated_subtotal,
    lowest_factor,
    savings_from_cost,
    savings_from_factor,
)
from app.models.quoter_v2 import V2FiringMode, V2Quotation
from app.models.quoter_v2_labor import V2QuotationLabor, V2WorkerType
from app.services.audit import AuditRecorder
from app.services.quoter_v2_firing import V2FiringService
from app.services.quoter_v2_pricing import V2PricingService

ZERO = Decimal(0)

REDUCTION_OTHER_KILN = "OTHER_KILN"
REDUCTION_MIN_FACTOR = "MIN_FACTOR"
REDUCTION_REMOVE_ILLUSTRATION = "REMOVE_ILLUSTRATION"
REDUCTION_REMOVE_EXTRAS = "REMOVE_EXTRAS"
REDUCTION_INTERNAL_STAFF = "INTERNAL_STAFF"
REDUCTION_SHARED_FIRING = "SHARED_FIRING"

WARN_REDUCTIONS_NO_FACTOR = "V2_REDUCTIONS_NO_FACTOR"


@dataclass(frozen=True)
class Reduction:
    """Una palanca y lo que dejaria el precio si se usara."""

    code: str
    #: Si la palanca sirve en esta cotizacion. Una que no ahorra nada se
    #: devuelve igual, apagada, para que la pantalla pueda decir por que.
    applicable: bool
    #: Cuanto baja el costo de produccion (cero para el factor).
    cost_reduction: Decimal
    #: Cuanto baja el precio sin IGV.
    savings: Decimal
    estimated_subtotal: Decimal
    #: Lo que se sugiere: el nombre del horno, el factor, etc.
    suggestion: str | None = None


@dataclass(frozen=True)
class ReductionsState:
    quotation: V2Quotation
    current_subtotal: Decimal
    commercial_factor: Decimal | None
    items: list[Reduction]
    warnings: list[str]


class V2ReductionsService:
    """Estima reducciones de precio. Nunca las aplica."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._firing = V2FiringService(session, audit)
        self._pricing = V2PricingService(session, audit)

    async def reductions(self, quotation_id: int) -> ReductionsState:
        # Primero la quema y despues el precio, igual que al editar: asi las
        # dos lecturas describen el mismo estado.
        quema = await self._firing.firing_state(quotation_id)
        precio = await self._pricing.pricing_state(quotation_id)
        quotation = precio.quotation

        factor = quotation.commercial_factor
        subtotal = quantize_money(
            to_base_currency(quotation.subtotal_amount, quotation.exchange_rate_snapshot)
        )
        if factor is None:
            return ReductionsState(
                quotation=quotation,
                current_subtotal=subtotal,
                commercial_factor=None,
                items=[],
                warnings=[WARN_REDUCTIONS_NO_FACTOR],
            )

        def por_costo(codigo: str, reduccion: Decimal, sugerencia: str | None = None) -> Reduction:
            ahorro = quantize_money(savings_from_cost(reduccion, factor))
            return Reduction(
                code=codigo,
                applicable=ahorro > ZERO,
                cost_reduction=quantize_money(max(reduccion, ZERO)),
                savings=ahorro,
                estimated_subtotal=estimated_subtotal(subtotal, ahorro),
                suggestion=sugerencia,
            )

        items: list[Reduction] = []

        barato = quema.cheaper_kiln
        items.append(
            por_costo(
                REDUCTION_OTHER_KILN,
                barato.savings if barato is not None else ZERO,
                barato.name if barato is not None else None,
            )
        )

        minimo = lowest_factor(quotation.commercial_factor_min_snapshot)
        ahorro_factor = quantize_money(
            savings_from_factor(quotation.production_cost_total, factor, minimo)
        )
        items.append(
            Reduction(
                code=REDUCTION_MIN_FACTOR,
                applicable=ahorro_factor > ZERO,
                cost_reduction=ZERO,
                savings=ahorro_factor,
                estimated_subtotal=estimated_subtotal(subtotal, ahorro_factor),
                suggestion=str(minimo),
            )
        )

        ilustracion = quotation.illustration_cost + sum(
            (linea.illustration_cost for linea in precio.lines), ZERO
        )
        items.append(por_costo(REDUCTION_REMOVE_ILLUSTRATION, ilustracion))
        items.append(por_costo(REDUCTION_REMOVE_EXTRAS, quotation.extras_cost_total))
        items.append(por_costo(REDUCTION_INTERNAL_STAFF, await self._external_labor(quotation)))
        items.append(por_costo(REDUCTION_SHARED_FIRING, self._exclusive_premium(quotation)))

        return ReductionsState(
            quotation=quotation,
            current_subtotal=subtotal,
            commercial_factor=factor,
            items=items,
            warnings=[],
        )

    async def _external_labor(self, quotation: V2Quotation) -> Decimal:
        """Lo que cuesta el personal EXTERNO: lo que se ahorra haciendolo en casa."""
        total = await self._session.scalar(
            select(func.coalesce(func.sum(V2QuotationLabor.labor_cost), 0)).where(
                V2QuotationLabor.v2_quotation_id == quotation.id,
                V2QuotationLabor.worker_type_snapshot == V2WorkerType.EXTERNAL,
            )
        )
        return Decimal(total or 0)

    @staticmethod
    def _exclusive_premium(quotation: V2Quotation) -> Decimal:
        """Lo que se cobra de mas por quemar en exclusiva en vez de compartida."""
        if quotation.firing_mode is not V2FiringMode.EXCLUSIVE:
            return ZERO
        capacidad = quotation.kiln_capacity_snapshot
        volumen = quotation.firing_total_volume_cm3 or ZERO
        if capacidad is None or capacidad <= ZERO or volumen <= ZERO:
            return ZERO
        carga = billed_load(volumen, capacidad, SHARED)
        compartida = ZERO
        if quotation.low_fire_enabled:
            compartida += firing_amount(carga, quotation.commercial_rate_low_snapshot or ZERO)
        if quotation.high_fire_enabled:
            compartida += firing_amount(carga, quotation.commercial_rate_high_snapshot or ZERO)
        return quotation.firing_commercial_total - quantize_money(compartida)


__all__ = [
    "REDUCTION_INTERNAL_STAFF",
    "REDUCTION_MIN_FACTOR",
    "REDUCTION_OTHER_KILN",
    "REDUCTION_REMOVE_EXTRAS",
    "REDUCTION_REMOVE_ILLUSTRATION",
    "REDUCTION_SHARED_FIRING",
    "Reduction",
    "ReductionsState",
    "V2ReductionsService",
]
