"""Fase 010J — la aritmetica del bloque «Reducciones» del Excel final.

Funciones puras. Cada alternativa se estima como en la hoja «Reducciones»:

    ahorro    = reduccion de costo de produccion x factor actual
    estimado  = subtotal actual - ahorro

salvo bajar el factor, cuyo ahorro es costo de produccion x (factor - minimo).
Es una ESTIMACION antes del redondeo por linea: el subtotal real, si alguien
aplica el cambio, sale del recalculo completo.

Nada de esto se aplica solo. El modulo calcula numeros; cambiar el horno, el
factor o la ilustracion es una decision de quien cotiza.
"""

from __future__ import annotations

from decimal import Decimal

ZERO = Decimal(0)

#: El suelo del factor comercial. Regla cerrada del negocio: ninguna
#: sugerencia baja de x2, diga lo que diga la configuracion.
FACTOR_FLOOR = Decimal(2)


class ReductionMathError(ValueError):
    """Una entrada con la que no se puede estimar una reduccion."""


def savings_from_cost(cost_reduction: Decimal, factor: Decimal) -> Decimal:
    """Cuanto baja el precio si el costo de produccion baja en `cost_reduction`.

    El precio es costo x factor, asi que un sol menos de costo son `factor`
    soles menos de precio.
    """
    if factor < FACTOR_FLOOR:
        raise ReductionMathError("El factor comercial no puede ser menor que x2")
    if cost_reduction <= ZERO:
        return ZERO
    return cost_reduction * factor


def lowest_factor(minimum_snapshot: Decimal | None) -> Decimal:
    """El factor mas bajo que se puede sugerir: el minimo congelado, nunca < x2."""
    if minimum_snapshot is None:
        return FACTOR_FLOOR
    return max(minimum_snapshot, FACTOR_FLOOR)


def savings_from_factor(
    production_cost: Decimal, factor: Decimal, suggested_factor: Decimal
) -> Decimal:
    """Cuanto baja el precio al pasar del factor actual al sugerido."""
    if suggested_factor < FACTOR_FLOOR:
        raise ReductionMathError("Ninguna sugerencia puede bajar el factor de x2")
    if production_cost <= ZERO or factor <= suggested_factor:
        return ZERO
    return production_cost * (factor - suggested_factor)


def estimated_subtotal(current_subtotal: Decimal, savings: Decimal) -> Decimal:
    """El subtotal estimado tras la reduccion. Nunca negativo."""
    return max(current_subtotal - savings, ZERO)


__all__ = [
    "FACTOR_FLOOR",
    "ReductionMathError",
    "estimated_subtotal",
    "lowest_factor",
    "savings_from_cost",
    "savings_from_factor",
]
