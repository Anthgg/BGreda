"""Fase 010K — la aritmetica de Solo Quema V2 (hoja «Solo Quema» del Excel final).

Funciones puras, sin base de datos. La geometria, la ocupacion, las hornadas y
la carga facturada NO se reescriben: son las de 010J (`app.core.quoter_v2_firing`).
Aqui vive solo lo que es propio del servicio de quema:

    por horno    ocupacion = Σ vol / capacidad x 100      hornadas = techo(ocupacion/100)
                 carga     = ocupacion/100 (compartida) | hornadas (exclusiva)
                 comercial = carga x (tarifa baja + tarifa alta)   (los ciclos encendidos)
                 gas       = carga x (gas baja + gas alta)
    vidriado     material = gramos totales x costo por gramo
                 mano de obra (opcional) = horas x tarifa; el interno no suma
    base         = quema comercial + vidriado + MO de vidriado    (el gas NO entra)
    subtotal     = techo(base x factor / TC, 0,50)     redondeo sobre el TOTAL
    IGV          = subtotal x IGV          total = subtotal + IGV
    costo real   = gas + vidriado + MO de vidriado
    ganancia     = subtotal (en PEN) - costo real      margen = ganancia / subtotal

El redondeo es sobre el total y no por pieza porque el documento de Solo
Quema no tiene precio por linea (hoja «PDF Quema»).

El factor va de x1,00 a x2,00 y se aplica UNA vez sobre la base. No es el
factor de fabricacion (x2..x10).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.core.quoter_v2_firing import (
    EXCLUSIVE,
    SHARED,
    FiringMathError,
    batch_loads,
    billed_load,
    firing_amount,
    firing_count,
    occupancy_percent,
)
from app.core.quoter_v2_pricing import (
    ceil_to_step,
    margin_percent,
    quantize_money,
    tax_amount,
    to_base_currency,
)

ZERO = Decimal(0)
FACTOR_MIN = Decimal("1.00")
FACTOR_MAX = Decimal("2.00")


class FiringQuotationMathError(ValueError):
    """Una entrada con la que no se puede calcular el servicio."""


@dataclass(frozen=True)
class CycleRates:
    """Tarifa (lo que se cobra) y gas (lo que cuesta) por hornada completa.

    `None` en un ciclo = el horno no tiene tarifa para ese ciclo.
    """

    commercial_low: Decimal | None
    commercial_high: Decimal | None
    gas_low: Decimal | None
    gas_high: Decimal | None


@dataclass(frozen=True)
class ModeQuote:
    """Lo que costaria la quema en un horno con un modo."""

    billed_load: Decimal
    commercial: Decimal
    gas: Decimal


@dataclass(frozen=True)
class KilnQuote:
    """Un horno con la carga del pedido: ocupacion y los dos modos."""

    occupancy_percent: Decimal
    firing_count: int
    batch_loads: tuple[Decimal, ...]
    shared: ModeQuote | None
    exclusive: ModeQuote | None


def validate_factor(factor: Decimal) -> Decimal:
    """El factor Solo Quema: x1,00 a x2,00, ambos incluidos."""
    if factor < FACTOR_MIN or factor > FACTOR_MAX:
        raise FiringQuotationMathError(
            "El factor de Solo Quema tiene que estar entre x1,00 y x2,00"
        )
    return factor


def _rates_complete(rates: CycleRates, low: bool, high: bool) -> bool:
    if low and (rates.commercial_low is None or rates.gas_low is None):
        return False
    return not (high and (rates.commercial_high is None or rates.gas_high is None))


def mode_quote(load: Decimal, rates: CycleRates, *, low: bool, high: bool) -> ModeQuote:
    """Comercial y gas de una carga, sumando solo los ciclos encendidos."""
    comercial = ZERO
    gas = ZERO
    if low:
        comercial += firing_amount(load, rates.commercial_low or ZERO)
        gas += firing_amount(load, rates.gas_low or ZERO)
    if high:
        comercial += firing_amount(load, rates.commercial_high or ZERO)
        gas += firing_amount(load, rates.gas_high or ZERO)
    return ModeQuote(
        billed_load=load, commercial=quantize_money(comercial), gas=quantize_money(gas)
    )


def kiln_quote(
    volume_cm3: Decimal,
    capacity_cm3: Decimal,
    rates: CycleRates,
    *,
    low: bool,
    high: bool,
) -> KilnQuote:
    """Ocupacion y precio del pedido en un horno, en compartida Y en exclusiva.

    Los dos modos se calculan siempre: el comparador los ensena lado a lado para
    que quien cotiza vea cuanto sube la exclusiva. Sin tarifa para algun ciclo
    encendido, los importes son `None`: comparar contra un cero inventado
    sugeriria el horno equivocado.
    """
    try:
        ocupacion = occupancy_percent(volume_cm3, capacity_cm3)
        hornadas = firing_count(volume_cm3, capacity_cm3)
        compartida = billed_load(volume_cm3, capacity_cm3, SHARED)
        exclusiva = billed_load(volume_cm3, capacity_cm3, EXCLUSIVE)
    except FiringMathError as error:
        raise FiringQuotationMathError(str(error)) from error
    completas = _rates_complete(rates, low, high)
    return KilnQuote(
        occupancy_percent=ocupacion,
        firing_count=hornadas,
        batch_loads=batch_loads(ocupacion, hornadas),
        shared=mode_quote(compartida, rates, low=low, high=high) if completas else None,
        exclusive=mode_quote(exclusiva, rates, low=low, high=high) if completas else None,
    )


def glaze_material_cost(grams: Decimal, cost_per_gram: Decimal) -> Decimal:
    """Vidriado: gramos totales x costo por gramo (hoja «Solo Quema», K14)."""
    if grams < ZERO:
        raise FiringQuotationMathError("Los gramos de esmalte no pueden ser negativos")
    if cost_per_gram < ZERO:
        raise FiringQuotationMathError("El costo por gramo no puede ser negativo")
    return quantize_money(grams * cost_per_gram)


@dataclass(frozen=True)
class ServicePrice:
    base_amount: Decimal
    commercial_price: Decimal
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    real_cost: Decimal
    profit: Decimal
    margin_percent: Decimal


def service_price(
    *,
    firing_commercial: Decimal,
    firing_gas: Decimal,
    glaze_material: Decimal,
    glaze_labor: Decimal,
    factor: Decimal,
    tax_percent: Decimal,
    rounding_step: Decimal | None,
    exchange_rate: Decimal | None,
) -> ServicePrice:
    """El precio del servicio, de la base al total (hoja «Solo Quema», K15:K20).

    El factor se aplica una sola vez sobre la base y el redondeo hacia arriba al
    escalon es sobre el subtotal entero. En moneda extranjera la base se lleva a
    esa moneda antes de redondear; la ganancia vuelve a la base para compararla
    con el costo, que siempre esta en soles.
    """
    validate_factor(factor)
    base = firing_commercial + glaze_material + glaze_labor
    comercial = base * factor
    en_moneda = comercial if exchange_rate is None else comercial / exchange_rate
    if rounding_step is None or rounding_step <= ZERO:
        subtotal = quantize_money(en_moneda)
    else:
        subtotal = ceil_to_step(en_moneda, rounding_step)
    impuesto = tax_amount(subtotal, tax_percent)
    real = firing_gas + glaze_material + glaze_labor
    subtotal_base = to_base_currency(subtotal, exchange_rate)
    ganancia = subtotal_base - real
    return ServicePrice(
        base_amount=base,
        commercial_price=comercial,
        subtotal=subtotal,
        tax=impuesto,
        total=subtotal + impuesto,
        real_cost=real,
        profit=ganancia,
        margin_percent=margin_percent(ganancia, subtotal_base),
    )


def volume_shares(volumes: Sequence[Decimal]) -> list[Decimal]:
    """Participacion de cada pieza en el volumen total, en porcentaje."""
    total = sum(volumes, ZERO)
    if total <= ZERO:
        return [ZERO for _ in volumes]
    return [(volumen / total * 100).quantize(Decimal("0.000001")) for volumen in volumes]


__all__ = [
    "FACTOR_MAX",
    "FACTOR_MIN",
    "CycleRates",
    "FiringQuotationMathError",
    "KilnQuote",
    "ModeQuote",
    "ServicePrice",
    "glaze_material_cost",
    "kiln_quote",
    "mode_quote",
    "service_price",
    "validate_factor",
    "volume_shares",
]
