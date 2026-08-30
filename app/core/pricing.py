"""Motor comercial de Fase 009E: factor, costos fijos, margen, IGV y redondeo.

## Qué decide este módulo

El orden canónico de una línea, y por qué cada paso va donde va:

    COSTO_TECNICO
      x FACTOR_PRODUCCION          (3 por defecto; es de PRODUCCION, no margen)
    = COSTO_FACTORADO
      + ASIGNACION_COSTOS_FIJOS    (prorrateo del total de la cotizacion)
    = COSTO_COMERCIAL_BASE
      / cantidad  x (1 + markup)
    = PRECIO_NETO_CRUDO
      x (1 + IGV)
    = PRECIO_BRUTO_CRUDO
      → CEILING al paso contractual
    = PRECIO_BRUTO_FINAL          ← este es el precio del contrato
      → se reconstruyen neto e IGV desde el bruto
      x cantidad
    = TOTAL_LINEA

## Por qué se redondea el BRUTO y no el neto

El precio que el cliente paga y firma es el bruto. Redondear el neto y sumarle
el IGV después produce un bruto con céntimos arbitrarios —lo que hacía la regla
anterior— y el documento acaba diciendo un número que nadie eligió. Redondeando
el bruto y reconstruyendo hacia atrás, `NET + TAX == GROSS` es exacto por
construcción y el total del contrato es un número redondo de verdad.

## Por qué CEILING y nunca «al más cercano»

Redondear hacia abajo regala dinero en cada pieza de cada cotización. Hacia
arriba, como mucho se cobra un céntimo de más, que es recuperable negociando;
lo regalado no vuelve. La regla anterior (nearest 0,50) bajaba en la mitad de
los casos.

## Por qué el total no se vuelve a redondear

Cada línea ya tiene un precio unitario contractual redondeado. Redondear
además la suma haría que el total no coincidiera con la suma de las líneas que
el propio documento enumera, y el cliente sabe sumar.

Todo con `Decimal`. Con float, `0.1 + 0.2` no es `0.3` y un céntimo de error
por pieza se multiplica por la cantidad.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)

#: Escala monetaria de presentación y de los importes contractuales.
MONEY = Decimal("0.01")

#: Factor de PRODUCCION por defecto. Multiplica el costo técnico y no tiene
#: nada que ver con el margen: son dos pasos distintos y consecutivos.
#:
#: Vive aquí como constante y no en `commercial_settings` porque hacerlo
#: configurable exige una columna nueva (Alembic 0016, sin autorizar). El
#: override por cotización sí funciona ya.
DEFAULT_PRODUCTION_FACTOR = Decimal(3)

#: Pasos de redondeo contractual admitidos.
ROUNDING_STEPS = (Decimal("0.50"), Decimal("1.00"))
DEFAULT_ROUNDING_STEP = Decimal("0.50")


class PricingError(ValueError):
    """Una entrada no permite calcular un precio comercial válido."""


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Redondea SIEMPRE hacia arriba al múltiplo de `step`.

    Un valor que ya es múltiplo exacto no se mueve: 142,50 con paso 0,50 sigue
    siendo 142,50. Un céntimo por encima salta al siguiente escalón completo.
    """
    if value < ZERO:
        raise PricingError("No se redondea un importe negativo")
    if step not in ROUNDING_STEPS:
        admitidos = ", ".join(str(item) for item in ROUNDING_STEPS)
        raise PricingError(f"Paso de redondeo no admitido: {step}. Admitidos: {admitidos}")
    # to_integral_value con ROUND_CEILING sobre el cociente: entero exacto, sin
    # pasar por float y sin que 142.50/0.50 = 285.00000000000006 rompa el caso
    # de borde.
    steps = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return (steps * step).quantize(MONEY)


def allocate_fixed_costs(
    factored_costs: Sequence[Decimal], total_fixed_cost: Decimal
) -> tuple[Decimal, ...]:
    """Reparte el costo fijo TOTAL de la cotización entre sus líneas.

    El peso de cada línea es su costo factorado sobre el total factorado. El
    costo fijo es de la cotización entera: el alquiler del taller no se duplica
    porque el pedido lleve dos piezas distintas.

    El residuo de la cuantización se asigna a la ÚLTIMA línea para que
    `sum(asignaciones) == total_fixed_cost` sea exacto. Repartir 320 entre tres
    partes iguales da 106,67 + 106,67 + 106,66, no tres veces 106,67 con un
    céntimo perdido.

    Con base factorada cero no hay peso con el que repartir, y repartir a
    partes iguales sería inventarse una regla. Se bloquea.
    """
    if total_fixed_cost < ZERO:
        raise PricingError("El costo fijo total no puede ser negativo")
    if not factored_costs:
        raise PricingError("No hay líneas entre las que repartir los costos fijos")
    if any(cost < ZERO for cost in factored_costs):
        raise PricingError("Un costo factorado no puede ser negativo")

    total_factored = sum(factored_costs, ZERO)
    if total_factored <= ZERO:
        raise PricingError(
            "FIXED_COST_ALLOCATION_BASE_ZERO: sin costo factorado no hay base "
            "con la que repartir los costos fijos"
        )

    allocations: list[Decimal] = []
    running = ZERO
    for cost in factored_costs[:-1]:
        share = (total_fixed_cost * cost / total_factored).quantize(MONEY)
        allocations.append(share)
        running += share
    allocations.append((total_fixed_cost - running).quantize(MONEY))
    return tuple(allocations)


def reconstruct_net_and_tax(gross: Decimal, tax_percent: Decimal) -> tuple[Decimal, Decimal]:
    """Desde el bruto contractual, hacia atrás: neto e IGV.

    Se reconstruye en vez de conservar el neto crudo porque el número firmado
    es el bruto. `NET + TAX == GROSS` sale exacto porque el IGV se obtiene
    restando, no volviendo a multiplicar.
    """
    if gross < ZERO:
        raise PricingError("El importe bruto no puede ser negativo")
    if tax_percent < ZERO:
        raise PricingError("El porcentaje de IGV no puede ser negativo")
    rate = ONE + (tax_percent / HUNDRED)
    net = (gross / rate).quantize(MONEY, rounding=ROUND_HALF_UP)
    return net, gross - net


@dataclass(frozen=True, slots=True)
class LinePricingInput:
    """Lo que hace falta para poner precio a una línea."""

    quantity: int
    #: Costo técnico TOTAL de la línea (materiales + quema asignada + mano de
    #: obra). No incluye costos fijos: esos son de la cotización.
    technical_cost: Decimal
    production_factor: Decimal
    #: Parte de los costos fijos de la cotización que le toca a esta línea.
    fixed_cost_allocation: Decimal
    markup_percent: Decimal
    tax_percent: Decimal
    rounding_step: Decimal
    #: Precio neto unitario escrito a mano por el usuario. Cuando existe
    #: sustituye al que sale del markup, pero NO se salta el redondeo: el
    #: contrato sigue exigiendo un bruto redondo, asi que el precio tecleado
    #: entra como neto crudo y pasa por el mismo camino que cualquier otro.
    manual_net_unit: Decimal | None = None


@dataclass(frozen=True, slots=True)
class LinePricing:
    """Cada transición del cálculo, para poder explicar el precio entero."""

    technical_cost: Decimal
    production_factor: Decimal
    factored_cost: Decimal
    fixed_cost_allocation: Decimal
    commercial_base_cost: Decimal
    commercial_base_unit_cost: Decimal
    markup_percent: Decimal
    raw_net_unit: Decimal
    tax_percent: Decimal
    raw_tax_unit: Decimal
    raw_gross_unit: Decimal
    rounding_step: Decimal
    final_gross_unit: Decimal
    #: Lo que el redondeo contractual añadió al bruto crudo. Siempre >= 0.
    rounding_adjustment_unit: Decimal
    final_net_unit: Decimal
    final_tax_unit: Decimal
    quantity: int
    line_total_gross: Decimal
    line_total_net: Decimal
    line_total_tax: Decimal


def price_line(data: LinePricingInput) -> LinePricing:
    """Aplica el orden canónico completo a una línea."""
    if data.quantity <= 0:
        raise PricingError("La cantidad debe ser un entero mayor que cero")
    if data.technical_cost < ZERO:
        raise PricingError("El costo técnico no puede ser negativo")
    if data.production_factor <= ZERO:
        raise PricingError("El factor de producción debe ser mayor que cero")
    if data.fixed_cost_allocation < ZERO:
        raise PricingError("La asignación de costos fijos no puede ser negativa")
    if data.markup_percent < ZERO:
        raise PricingError("El porcentaje de ganancia no puede ser negativo")
    if data.tax_percent < ZERO:
        raise PricingError("El porcentaje de IGV no puede ser negativo")

    factored = data.technical_cost * data.production_factor
    base = factored + data.fixed_cost_allocation
    base_unit = base / Decimal(data.quantity)

    if data.manual_net_unit is not None:
        if data.manual_net_unit < ZERO:
            raise PricingError("El precio comercial no puede ser negativo")
        raw_net_unit = data.manual_net_unit
    else:
        raw_net_unit = base_unit * (ONE + data.markup_percent / HUNDRED)
    raw_tax_unit = raw_net_unit * data.tax_percent / HUNDRED
    raw_gross_unit = raw_net_unit + raw_tax_unit

    final_gross_unit = ceil_to_step(raw_gross_unit, data.rounding_step)
    final_net_unit, final_tax_unit = reconstruct_net_and_tax(final_gross_unit, data.tax_percent)

    # El total de línea es el precio contractual por la cantidad. No se vuelve
    # a redondear: el cliente suma las líneas del documento y le tiene que dar
    # el total del documento.
    line_total_gross = final_gross_unit * Decimal(data.quantity)
    line_total_net = final_net_unit * Decimal(data.quantity)

    return LinePricing(
        technical_cost=data.technical_cost,
        production_factor=data.production_factor,
        factored_cost=factored,
        fixed_cost_allocation=data.fixed_cost_allocation,
        commercial_base_cost=base,
        commercial_base_unit_cost=base_unit,
        markup_percent=data.markup_percent,
        raw_net_unit=raw_net_unit,
        tax_percent=data.tax_percent,
        raw_tax_unit=raw_tax_unit,
        raw_gross_unit=raw_gross_unit,
        rounding_step=data.rounding_step,
        final_gross_unit=final_gross_unit,
        rounding_adjustment_unit=final_gross_unit - raw_gross_unit.quantize(MONEY),
        final_net_unit=final_net_unit,
        final_tax_unit=final_tax_unit,
        quantity=data.quantity,
        line_total_gross=line_total_gross,
        line_total_net=line_total_net,
        line_total_tax=line_total_gross - line_total_net,
    )
