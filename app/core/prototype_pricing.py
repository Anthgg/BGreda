"""Costeo de una cotizacion de prototipo.

Motor propio, y no una variante del de produccion, porque el negocio es otro.
Una pieza de catalogo se cotiza por lo que cuesta FABRICARLA en serie —material,
quema, factor, margen—; un prototipo se cotiza por los DIAS que alguien va a
dedicarle. Esa es la frase de la reunion del 28/08 y es lo que fija la hoja
«Especificacion» del Excel v2: la variable principal son los dias.

De ahi salen tres reglas que no se pueden heredar de `price_line`:

- **Sin factor y sin margen.** El subtotal ES el costo base. El factor x3 y el
  margen pertenecen al costeo de produccion (009E) y meterlos aqui triplicaria
  el precio de una muestra.
- **El redondeo comercial SI se hereda, y no se reimplementa.** El Excel no lo
  describe porque, igual que en el Cotizador principal, la politica llego
  despues; pero es una regla de la casa y vale para todo documento que se
  firma. Se usan `ceil_to_step` y `reconstruct_net_and_tax` del motor comun:
  escribir aqui un segundo redondeo daria dos aritmeticas que algun dia
  discrepan, y ganaria la que nadie mira.
- **El matricero cuesta un precio FIJO.** Sus dias cuentan para el plazo, no
  multiplican su importe: `D13 = C13` en la hoja «Cotizador Prototipo». Es el
  error facil de este modelo, porque los otros dos conceptos si multiplican.

Todo en `Decimal`. Los importes se cuantizan a dos decimales una sola vez, al
final de cada concepto, para que la suma de las partes sea exactamente el total
que se imprime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.core.pricing import ceil_to_step, reconstruct_net_and_tax

ZERO = Decimal(0)
MONEY = Decimal("0.01")


def _money(valor: Decimal) -> Decimal:
    """Un importe con dos decimales, redondeando al mas cercano.

    Esto NO es el redondeo comercial: cuantiza un concepto interno para que la
    suma de las partes cuadre. El escalon comercial se aplica una sola vez, al
    bruto, con `ceil_to_step`.
    """
    return valor.quantize(MONEY, rounding=ROUND_HALF_UP)


class PrototypePricingError(ValueError):
    """Una entrada del costeo no cumple su contrato."""


@dataclass(frozen=True, slots=True)
class PrototypeMaterialInput:
    """Un material que el prototipo va a gastar, ya en su unidad de catalogo.

    `unit_cost` es el costo de UNA unidad base del material y lo resuelve el
    backend desde el maestro: aqui no se convierte nada. Convertir gramos a
    mililitros exigiria una densidad que el sistema no conoce, y suponerla 1
    es exactamente como se falsea un costo sin que nadie lo note.
    """

    product_id: int
    description: str
    quantity_per_prototype: Decimal
    uom_code: str
    unit_cost: Decimal


@dataclass(frozen=True, slots=True)
class PrototypeCostingInput:
    """Todo lo que hace falta para valorar una cotizacion de prototipo.

    Las tarifas llegan ya resueltas —el valor por defecto de configuracion o el
    que la cotizacion haya sobrescrito—, porque decidir cual de los dos manda
    es cosa del servicio, no del calculo.
    """

    quantity: int

    design_days: Decimal
    design_rate: Decimal
    artist_days: Decimal
    artist_rate: Decimal

    mold_maker_price: Decimal
    mold_maker_days: Decimal

    materials: tuple[PrototypeMaterialInput, ...]

    firing_rate: Decimal
    firing_batches: int
    firing_days_per_batch: int

    drying_days: Decimal
    adjustment_days: Decimal

    fixed_cost: Decimal
    tax_percent: Decimal
    #: Politica comercial de la casa, resuelta por el servicio desde la
    #: configuracion. No se decide aqui ni se escribe un valor por defecto: un
    #: paso inventado es un precio inventado.
    rounding_step: Decimal

    requested_at: date | None = None


@dataclass(frozen=True, slots=True)
class PrototypeMaterialCost:
    product_id: int
    description: str
    quantity_per_prototype: Decimal
    total_quantity: Decimal
    uom_code: str
    unit_cost: Decimal
    cost: Decimal


@dataclass(frozen=True, slots=True)
class PrototypeCosting:
    """El desglose completo. Interno: no todo esto va al documento del cliente."""

    design_cost: Decimal
    artist_cost: Decimal
    mold_maker_cost: Decimal
    materials_cost: Decimal
    firing_cost: Decimal
    fixed_cost: Decimal
    base_cost: Decimal

    #: Lo que da la aritmetica ANTES del escalon comercial. Viaja para poder
    #: explicar de donde sale el ajuste, no para imprimirlo.
    raw_tax: Decimal
    raw_gross_total: Decimal

    #: Los tres numeros del documento. Reconstruidos desde el bruto redondeado,
    #: de modo que NET + TAX == GROSS exacto.
    commercial_net_total: Decimal
    tax_percent: Decimal
    commercial_tax_total: Decimal
    commercial_gross_total: Decimal
    rounding_step: Decimal
    total_per_prototype: Decimal

    design_days: Decimal
    artist_days: Decimal
    mold_maker_days: Decimal
    drying_days: Decimal
    firing_days: int
    adjustment_days: Decimal
    estimated_days: Decimal
    target_date: date | None

    materials: tuple[PrototypeMaterialCost, ...] = field(default_factory=tuple)


def _validar(entrada: PrototypeCostingInput) -> None:
    if entrada.quantity <= 0:
        raise PrototypePricingError("La cantidad de muestras debe ser mayor que cero")
    for nombre, valor in (
        ("dias de diseno", entrada.design_days),
        ("dias de artista", entrada.artist_days),
        ("dias de matricero", entrada.mold_maker_days),
        ("dias de secado", entrada.drying_days),
        ("dias de ajuste", entrada.adjustment_days),
    ):
        if valor < ZERO:
            raise PrototypePricingError(f"Los {nombre} no pueden ser negativos")
    for nombre, valor in (
        ("tarifa de diseno", entrada.design_rate),
        ("tarifa de artista", entrada.artist_rate),
        ("precio del matricero", entrada.mold_maker_price),
        ("costo fijo", entrada.fixed_cost),
        ("tarifa de quema", entrada.firing_rate),
    ):
        if valor < ZERO:
            raise PrototypePricingError(f"La {nombre} no puede ser negativa")
    if entrada.firing_batches < 0:
        raise PrototypePricingError("El numero de hornadas no puede ser negativo")
    if entrada.firing_days_per_batch < 0:
        raise PrototypePricingError("Los dias por hornada no pueden ser negativos")
    if entrada.tax_percent < ZERO:
        raise PrototypePricingError("El impuesto no puede ser negativo")


def price_prototype(entrada: PrototypeCostingInput) -> PrototypeCosting:
    """Valora la cotizacion entera y calcula su plazo.

    El orden importa: cada concepto se cuantiza al cerrarse, y el costo base es
    la suma de conceptos ya cuantizados. Sumar en crudo y cuantizar al final
    daria un total que no coincide con la suma de las lineas impresas, y el
    cliente cuadra el documento sumando lo que ve.
    """
    _validar(entrada)

    design_cost = _money(entrada.design_days * entrada.design_rate)
    artist_cost = _money(entrada.artist_days * entrada.artist_rate)
    # El precio del matricero es FIJO: sus dias van al plazo, no al importe.
    mold_maker_cost = _money(entrada.mold_maker_price)

    materiales: list[PrototypeMaterialCost] = []
    materials_cost = ZERO
    for material in entrada.materials:
        if material.quantity_per_prototype < ZERO:
            raise PrototypePricingError("La cantidad de un material no puede ser negativa")
        if material.unit_cost < ZERO:
            raise PrototypePricingError("El costo unitario de un material no puede ser negativo")
        total_quantity = material.quantity_per_prototype * entrada.quantity
        costo = _money(total_quantity * material.unit_cost)
        materials_cost += costo
        materiales.append(
            PrototypeMaterialCost(
                product_id=material.product_id,
                description=material.description,
                quantity_per_prototype=material.quantity_per_prototype,
                total_quantity=total_quantity,
                uom_code=material.uom_code,
                unit_cost=material.unit_cost,
                cost=costo,
            )
        )

    firing_cost = _money(entrada.firing_rate * entrada.firing_batches)
    fixed_cost = _money(entrada.fixed_cost)

    base_cost = (
        design_cost + artist_cost + mold_maker_cost + materials_cost + firing_cost + fixed_cost
    )

    # Sin factor y sin margen: el neto de partida ES el costo base. Lo que si
    # se aplica es el escalon comercial, y una sola vez, sobre el bruto.
    raw_tax = _money(base_cost * entrada.tax_percent / Decimal(100))
    raw_gross_total = base_cost + raw_tax
    commercial_gross_total = ceil_to_step(raw_gross_total, entrada.rounding_step)
    # Se reconstruye en vez de conservar el neto crudo: el numero que se firma
    # es el bruto, y dejar el neto viejo al lado de un bruto redondeado daria
    # un encabezado que no suma.
    commercial_net_total, commercial_tax_total = reconstruct_net_and_tax(
        commercial_gross_total, entrada.tax_percent
    )
    total_per_prototype = _money(commercial_gross_total / entrada.quantity)

    firing_days = entrada.firing_days_per_batch * entrada.firing_batches
    estimated_days = (
        entrada.design_days
        + entrada.artist_days
        + entrada.mold_maker_days
        + entrada.drying_days
        + Decimal(firing_days)
        + entrada.adjustment_days
    )
    target_date = (
        entrada.requested_at + timedelta(days=int(estimated_days))
        if entrada.requested_at is not None
        else None
    )

    return PrototypeCosting(
        design_cost=design_cost,
        artist_cost=artist_cost,
        mold_maker_cost=mold_maker_cost,
        materials_cost=materials_cost,
        firing_cost=firing_cost,
        fixed_cost=fixed_cost,
        base_cost=base_cost,
        raw_tax=raw_tax,
        raw_gross_total=raw_gross_total,
        commercial_net_total=commercial_net_total,
        tax_percent=entrada.tax_percent,
        commercial_tax_total=commercial_tax_total,
        commercial_gross_total=commercial_gross_total,
        rounding_step=entrada.rounding_step,
        total_per_prototype=total_per_prototype,
        design_days=entrada.design_days,
        artist_days=entrada.artist_days,
        mold_maker_days=entrada.mold_maker_days,
        drying_days=entrada.drying_days,
        firing_days=firing_days,
        adjustment_days=entrada.adjustment_days,
        estimated_days=estimated_days,
        target_date=target_date,
        materials=tuple(materiales),
    )
