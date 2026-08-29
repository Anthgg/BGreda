"""Matematica de preparaciones: agua, rendimiento, concentracion y g <-> ml.

## Que decide este modulo

Una receta dice **proporciones**. Preparar dice **cuanto**. Este modulo hace el
puente entre ambas cosas y, sobre todo, resuelve la conversion entre gramos de
solidos y mililitros de preparado, que no es una conversion de unidad.

## Por que g -> ml no es una unidad mas

Convertir kg a g es un factor fijo. Convertir gramos de solidos a mililitros de
esmalte preparado depende de **cuanta agua lleva ese lote concreto**: 200 g de
solidos en 1000 ml dan 0,2 g/ml, y los mismos 200 g en 1200 ml dan 0,1666... El
factor no vive en la unidad, vive en la preparacion. Por eso `MASS` y `VOLUME`
son dimensiones separadas en `units_of_measure` y el puente esta aqui.

No se asume densidad 1: que un gramo de agua ocupe un mililitro no significa
que un gramo de esmalte lo haga.

## Por que el agua no encarece los solidos

El agua aumenta el rendimiento, no el costo de los ingredientes. Con mas agua
el mismo lote rinde mas mililitros, asi que el costo POR MILILITRO baja; el
costo total del lote es el de los solidos que se echaron y no cambia.

Todo con `Decimal`. Con float, 0.1 + 0.2 no es 0.3 y un costo por mililitro
arrastraria ese error a cada pieza cotizada.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

#: Escala de las cantidades fisicas y de la concentracion. Coincide con
#: `quantity_numeric()` para que redondear aqui no contradiga a la base.
QUANTITY_SCALE = Decimal("0.000000000001")
#: Escala del dinero, igual que `money_numeric()`.
MONEY_SCALE = Decimal("0.000001")

ZERO = Decimal(0)


class PreparationError(ValueError):
    """Error de dominio al calcular o registrar una preparacion."""


def _quantize_quantity(value: Decimal) -> Decimal:
    return value.quantize(QUANTITY_SCALE)


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_SCALE)


@dataclass(frozen=True)
class ComponentShare:
    """Un componente de la receta y el porcentaje que le corresponde."""

    product_id: int
    percentage: Decimal
    #: Costo por gramo del insumo en el momento de preparar. Se congela.
    unit_cost_per_g: Decimal


@dataclass(frozen=True)
class ComponentAmount:
    """Cuanto se consume de un componente y cuanto cuesta."""

    product_id: int
    quantity_g: Decimal
    unit_cost_snapshot: Decimal
    line_cost: Decimal


def component_amounts(
    components: Sequence[ComponentShare], total_dry_weight_g: Decimal
) -> tuple[ComponentAmount, ...]:
    """Reparte el peso seco del lote entre los componentes de la receta.

    El porcentaje es sobre el total de porcentajes declarados, no sobre 100:
    una receta con base 100 y aditivos 5 suma 105, y cada componente recibe su
    parte de esos 105. Asi el peso seco pedido se respeta exactamente en vez de
    fabricar un 5 % de mas.
    """
    if total_dry_weight_g <= 0:
        raise PreparationError("El peso seco del lote debe ser mayor que cero")
    if not components:
        raise PreparationError("La receta no tiene componentes")

    total_percentage = sum((c.percentage for c in components), ZERO)
    if total_percentage <= 0:
        raise PreparationError("Los porcentajes de la receta deben sumar mas que cero")

    amounts: list[ComponentAmount] = []
    for component in components:
        quantity = _quantize_quantity(total_dry_weight_g * component.percentage / total_percentage)
        if quantity <= 0:
            continue
        amounts.append(
            ComponentAmount(
                product_id=component.product_id,
                quantity_g=quantity,
                unit_cost_snapshot=component.unit_cost_per_g,
                line_cost=_quantize_money(quantity * component.unit_cost_per_g),
            )
        )
    if not amounts:
        raise PreparationError("Ningun componente recibe cantidad: revise los porcentajes")
    return tuple(amounts)


def batch_total_cost(amounts: Sequence[ComponentAmount]) -> Decimal:
    """Costo del lote: la suma de sus lineas, ni mas ni menos.

    Se suman las lineas ya redondeadas —no se recalcula sobre el total— para
    que `SUM(line_cost) == batch_total_cost` sea exacto y el lote se pueda
    explicar linea por linea sin que falte ni sobre un centimo.
    """
    return _quantize_money(sum((a.line_cost for a in amounts), ZERO))


def solids_concentration_g_per_ml(total_dry_weight_g: Decimal, final_yield_ml: Decimal) -> Decimal:
    """Gramos de solidos por mililitro de preparado."""
    if total_dry_weight_g <= 0:
        raise PreparationError("El peso seco debe ser mayor que cero")
    if final_yield_ml <= 0:
        raise PreparationError("El rendimiento final debe ser mayor que cero")
    return _quantize_quantity(total_dry_weight_g / final_yield_ml)


def unit_cost_per_ml(total_cost: Decimal, final_yield_ml: Decimal) -> Decimal:
    """Costo de un mililitro del preparado.

    Aqui es donde el agua se nota: sube el rendimiento y el costo por mililitro
    baja, aunque el costo del lote sea el mismo.
    """
    if total_cost < 0:
        raise PreparationError("El costo del lote no puede ser negativo")
    if final_yield_ml <= 0:
        raise PreparationError("El rendimiento final debe ser mayor que cero")
    return _quantize_quantity(total_cost / final_yield_ml)


def grams_to_ml(grams: Decimal, solids_g_per_ml: Decimal) -> Decimal:
    """Gramos de solidos equivalentes -> mililitros de preparado."""
    if grams < 0:
        raise PreparationError("Los gramos no pueden ser negativos")
    if solids_g_per_ml <= 0:
        raise PreparationError(
            "Sin concentracion registrada no se puede convertir gramos a mililitros"
        )
    return _quantize_quantity(grams / solids_g_per_ml)


def ml_to_grams(millilitres: Decimal, solids_g_per_ml: Decimal) -> Decimal:
    """Mililitros de preparado -> gramos de solidos equivalentes."""
    if millilitres < 0:
        raise PreparationError("Los mililitros no pueden ser negativos")
    if solids_g_per_ml <= 0:
        raise PreparationError(
            "Sin concentracion registrada no se puede convertir mililitros a gramos"
        )
    return _quantize_quantity(millilitres * solids_g_per_ml)


def estimated_glaze_grams(
    piece_weight_g: Decimal, quantity: int, glaze_percent: Decimal
) -> Decimal:
    """Esmalte estimado para cotizar, en gramos de solidos equivalentes.

    `glaze_percent` llega como porcentaje (15 significa 15 %), igual que
    `tax_percent` en `commercial_settings`. La division entre 100 se hace aqui,
    una sola vez, para que nadie tenga que recordar si el valor guardado era
    0,15 o 15.

    Es una ESTIMACION para cotizar. No descuenta inventario: el consumo real al
    vender pertenece a 009H.
    """
    if piece_weight_g <= 0:
        raise PreparationError("El peso de la pieza debe ser mayor que cero")
    if quantity <= 0:
        raise PreparationError("La cantidad debe ser mayor que cero")
    if glaze_percent <= 0 or glaze_percent > 100:
        raise PreparationError("El porcentaje de esmalte debe estar entre 0 y 100")
    per_piece = piece_weight_g * glaze_percent / Decimal(100)
    return _quantize_quantity(per_piece * Decimal(quantity))


def glaze_cost(millilitres: Decimal, unit_cost_per_ml: Decimal) -> Decimal:
    """Costo estimado de una asignacion de esmalte.

    Se cobra por mililitro y no por gramo porque el costo congelado del lote es
    por mililitro de preparado: el agua ya esta descontada de esa cifra.
    """
    if millilitres < 0:
        raise PreparationError("Los mililitros no pueden ser negativos")
    if unit_cost_per_ml < 0:
        raise PreparationError("El costo por mililitro no puede ser negativo")
    return _quantize_money(millilitres * unit_cost_per_ml)


def distribute_glaze(total_grams: Decimal, shares: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """Reparte el consumo estimado entre varios esmaltes.

    El porcentaje estimado describe el consumo TOTAL de la pieza. Usar dos
    esmaltes no gasta el doble de esmalte: gasta el mismo, repartido. Por eso
    aqui se reparte un total y no se multiplica por esmalte.

    El resto del redondeo se acumula en el ultimo tramo para que la suma
    reconcilie exactamente con el total. Repartir 100 entre 3 da 33,33 + 33,33 +
    33,34, no tres veces 33,33 y un centimo perdido.
    """
    if total_grams < 0:
        raise PreparationError("El consumo total no puede ser negativo")
    if not shares:
        raise PreparationError("Hace falta al menos un esmalte para repartir")
    total_share = sum(shares, ZERO)
    if total_share <= 0:
        raise PreparationError("Las participaciones deben sumar mas que cero")

    allocations: list[Decimal] = []
    running = ZERO
    for share in shares[:-1]:
        value = _quantize_quantity(total_grams * share / total_share)
        allocations.append(value)
        running += value
    allocations.append(_quantize_quantity(total_grams - running))
    return tuple(allocations)
