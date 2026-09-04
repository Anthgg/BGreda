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

#: Moneda base del sistema. Los costos, el inventario y los maestros viven
#: aqui y no se mueven: USD es moneda de EMISION, no de costeo.
BASE_CURRENCY = "PEN"

#: Monedas en las que se puede emitir una cotizacion. Fase 009F autoriza dos;
#: la tupla existe para que anadir una tercera sea una decision explicita y no
#: el efecto colateral de que alguien mande otro codigo.
SUPPORTED_CURRENCIES = ("PEN", "USD")


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


def convert_net_to_quote_currency(
    net_base: Decimal, currency: str, exchange_rate: Decimal | None
) -> Decimal:
    """Lleva un neto en PEN a la moneda en que se emite la cotización.

    La tasa dice **cuántos soles vale un dólar** (`1 USD = X PEN`), así que
    para pasar de soles a dólares se DIVIDE. Multiplicar con una tasa de 3,75
    da un número casi cuatro veces mayor que el correcto y con toda la pinta de
    ser un precio, que es exactamente el tipo de error que nadie detecta hasta
    que el cliente lo firma.

    No se cuantiza aquí: el redondeo del contrato ocurre una sola vez, sobre el
    bruto, y truncar el neto antes le robaría precisión.
    """
    if currency not in SUPPORTED_CURRENCIES:
        raise PricingError(f"Moneda no admitida: {currency}")
    if currency == BASE_CURRENCY:
        if exchange_rate is not None:
            # Guardar una tasa en una cotizacion en soles describe una
            # conversion que nunca ocurrio.
            raise PricingError("Una cotización en PEN no lleva tipo de cambio")
        return net_base
    if exchange_rate is None:
        raise PricingError("EXCHANGE_RATE_REQUIRED: falta el tipo de cambio")
    if exchange_rate <= ZERO:
        raise PricingError("El tipo de cambio debe ser mayor que cero")
    return net_base / exchange_rate


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
    #: Moneda en la que se EMITE la cotizacion. Los costos siguen en PEN.
    currency: str = BASE_CURRENCY
    #: Cuantos soles vale un dolar. Obligatorio para USD, prohibido para PEN.
    exchange_rate: Decimal | None = None


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
    #: Moneda de emision y tasa usada. Viajan con el resultado para que quien
    #: lo lea no tenga que deducir la moneda del simbolo.
    currency: str
    exchange_rate: Decimal | None
    #: Neto unitario ANTES de convertir, siempre en PEN. Es lo que permite
    #: explicar de donde sale el precio en dolares sin rehacer la cuenta.
    raw_net_unit_base: Decimal
    #: Neto unitario ya en la moneda de emision.
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

    # El neto que sale del margen esta en PEN, porque todo lo anterior lo esta:
    # el costo tecnico, el factor y los costos fijos son de produccion y no se
    # convierten. La moneda entra en el precio, no en el costeo.
    raw_net_unit_base = base_unit * (ONE + data.markup_percent / HUNDRED)

    if data.manual_net_unit is not None:
        if data.manual_net_unit < ZERO:
            raise PricingError("El precio comercial no puede ser negativo")
        # El precio manual YA esta en la moneda de emision: si el usuario
        # escribe 100 cotizando en dolares, quiere cobrar cien dolares.
        # Convertirlo otra vez lo dividiria por la tasa y cobraria 26,67.
        raw_net_unit = data.manual_net_unit
        # Aun asi se valida la coherencia moneda/tasa: un manual price no
        # convierte la cotizacion en un sitio donde el contrato no rige.
        convert_net_to_quote_currency(ZERO, data.currency, data.exchange_rate)
    else:
        raw_net_unit = convert_net_to_quote_currency(
            raw_net_unit_base, data.currency, data.exchange_rate
        )
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
        currency=data.currency,
        exchange_rate=data.exchange_rate,
        raw_net_unit_base=raw_net_unit_base,
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


@dataclass(frozen=True, slots=True)
class CommercialLinePricingInput:
    """Lo que hace falta para poner precio a un cargo comercial."""

    quantity: int
    #: El importe NETO que tecleo una persona, ya en la moneda de emision.
    #: Misma regla que el precio manual de una linea de producto: quien escribe
    #: 200 cotizando en dolares quiere cobrar doscientos dolares.
    manual_net_amount: Decimal
    tax_percent: Decimal
    rounding_step: Decimal
    currency: str
    exchange_rate: Decimal | None


@dataclass(frozen=True, slots=True)
class CommercialLinePricing:
    """El cargo ya valorado, con las mismas cifras que una linea de producto."""

    quantity: int
    net_unit: Decimal
    tax_percent: Decimal
    line_total_net: Decimal
    line_total_tax: Decimal
    line_total_gross: Decimal


def price_commercial_line(data: CommercialLinePricingInput) -> CommercialLinePricing:
    """Valora un cargo comercial: IGV y redondeo, nada mas.

    Lo que NO hace es la razon de que exista esta funcion aparte. Un cargo no
    recibe factor de produccion, ni margen, ni costos fijos, ni asignacion de
    quema: no es una pieza que se fabrique, es un concepto que se cobra. Si
    pasara por `price_line`, un cargo de 200 se cobraria multiplicado por el
    factor y por el margen, y nadie sabria de donde salio la diferencia.

    Lo que SI comparte es el final del camino —el mismo redondeo hacia arriba y
    la misma reconstruccion de neto e impuesto—, porque el documento tiene que
    sumar: si el cargo redondeara distinto que las lineas, el total del PDF
    dejaria de ser la suma de lo que el PDF enumera.
    """
    if data.quantity <= 0:
        raise PricingError("La cantidad del cargo debe ser un entero mayor que cero")
    if data.manual_net_amount <= ZERO:
        raise PricingError("El importe del cargo debe ser mayor que cero")
    if data.tax_percent < ZERO:
        raise PricingError("El porcentaje de impuesto no puede ser negativo")

    # Valida la coherencia moneda/tasa sin convertir el importe: el manual ya
    # esta en la moneda de emision.
    convert_net_to_quote_currency(ZERO, data.currency, data.exchange_rate)

    raw_net_unit = data.manual_net_amount
    raw_gross_unit = raw_net_unit + raw_net_unit * data.tax_percent / HUNDRED
    final_gross_unit = ceil_to_step(raw_gross_unit, data.rounding_step)
    final_net_unit, _final_tax_unit = reconstruct_net_and_tax(final_gross_unit, data.tax_percent)

    line_total_gross = final_gross_unit * Decimal(data.quantity)
    line_total_net = final_net_unit * Decimal(data.quantity)
    return CommercialLinePricing(
        quantity=data.quantity,
        net_unit=final_net_unit,
        tax_percent=data.tax_percent,
        line_total_net=line_total_net,
        line_total_tax=line_total_gross - line_total_net,
        line_total_gross=line_total_gross,
    )
