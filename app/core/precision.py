"""Convencion de precision numerica del proyecto.

Decision de arquitectura, aplicable a todas las fases: el dinero y los costos
sensibles se representan con ``decimal.Decimal`` en Python y con ``NUMERIC`` en
PostgreSQL. ``float`` queda descartado para cualquier calculo monetario oficial
por su error de representacion binaria.

Se distinguen tres escalas porque no tienen los mismos requisitos:

- **Importes comerciales**: totales y subtotales que el cliente ve.
- **Costos unitarios**: el Plan v1.2 advierte que un insumo puede costar menos
  de S/ 0.01 por gramo; con dos decimales se redondearia a cero. Por eso los
  costos unitarios usan una escala mucho mas fina, independiente de la
  precision de presentacion.
- **Porcentajes**: IGV, factores y participaciones.

La precision de calculo nunca se reduce a la precision visual.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Numeric

#: Importes comerciales (totales, subtotales, precios de venta).
MONEY_PRECISION = 18
MONEY_SCALE = 6

#: Costos unitarios finos. Escala tomada del Plan v1.2, que la exige
#: explicitamente para insumos con precio por gramo muy pequeno.
UNIT_COST_PRECISION = 24
UNIT_COST_SCALE = 12

#: Cantidades fisicas (gramos, litros, piezas).
QUANTITY_PRECISION = 18
QUANTITY_SCALE = 6

#: Saldos y movimientos de inventario. Comparten escala con los costos
#: unitarios porque una receta puede consumir fracciones de gramo y el saldo
#: resultante no debe redondearse antes que el costo que lo valoriza.
STOCK_QUANTITY_PRECISION = 24
STOCK_QUANTITY_SCALE = 12

#: Porcentajes. Se almacena 18 para "18 %", nunca la fraccion 0.18.
PERCENT_PRECISION = 9
PERCENT_SCALE = 6

#: Resultados internos del cotizador. Se conserva mas precision que en los
#: importes de presentacion porque el costo asignado de una quema puede tener
#: muchas cifras decimales y no debe redondearse antes del precio unitario.
CALCULATION_PRECISION = 36
CALCULATION_SCALE = 18


def money_numeric() -> Numeric[Decimal]:
    """Columna para importes monetarios.

    ``asdecimal=True`` garantiza que el driver devuelva ``Decimal`` y nunca
    ``float``.
    """
    return Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True)


def unit_cost_numeric() -> Numeric[Decimal]:
    """Columna para costos unitarios que pueden ser muy pequenos."""
    return Numeric(UNIT_COST_PRECISION, UNIT_COST_SCALE, asdecimal=True)


def quantity_numeric() -> Numeric[Decimal]:
    """Columna para cantidades fisicas."""
    return Numeric(QUANTITY_PRECISION, QUANTITY_SCALE, asdecimal=True)


def stock_quantity_numeric() -> Numeric[Decimal]:
    """Columna para saldos y movimientos de inventario."""
    return Numeric(STOCK_QUANTITY_PRECISION, STOCK_QUANTITY_SCALE, asdecimal=True)


def percentage_numeric() -> Numeric[Decimal]:
    """Columna para porcentajes como el IGV."""
    return Numeric(PERCENT_PRECISION, PERCENT_SCALE, asdecimal=True)


def calculation_numeric() -> Numeric[Decimal]:
    """Columna para resultados intermedios y finales del cotizador."""
    return Numeric(CALCULATION_PRECISION, CALCULATION_SCALE, asdecimal=True)
