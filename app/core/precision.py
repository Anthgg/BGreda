"""Convencion de precision numerica del proyecto.

Decision de arquitectura, aplicable a todas las fases: el dinero y los costos
sensibles se representan con ``decimal.Decimal`` en Python y con ``NUMERIC`` en
PostgreSQL. ``float`` queda descartado para cualquier calculo monetario oficial
por su error de representacion binaria.

La Fase 1 no crea tablas de costos; este modulo fija la convencion para que las
fases posteriores no la improvisen.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Numeric

#: Digitos totales de las columnas monetarias.
MONEY_PRECISION = 18
#: Digitos decimales. Seis permiten costos unitarios finos sin perder centimos.
MONEY_SCALE = 6

#: Precision para cantidades fisicas (gramos, litros, piezas).
QUANTITY_PRECISION = 18
QUANTITY_SCALE = 6

#: Cuantizador de presentacion para importes en moneda.
MONEY_QUANTUM = Decimal("0.01")


def money_numeric() -> Numeric[Decimal]:
    """Columna SQLAlchemy para importes monetarios.

    ``asdecimal=True`` garantiza que el driver devuelva ``Decimal`` y nunca
    ``float``.
    """
    return Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True)


def quantity_numeric() -> Numeric[Decimal]:
    """Columna SQLAlchemy para cantidades fisicas."""
    return Numeric(QUANTITY_PRECISION, QUANTITY_SCALE, asdecimal=True)
