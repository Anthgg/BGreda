"""Fase 010J — la aritmetica del bloque «Reducciones», con los numeros del Excel final."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.quoter_v2_reductions import (
    FACTOR_FLOOR,
    ReductionMathError,
    estimated_subtotal,
    lowest_factor,
    savings_from_cost,
    savings_from_factor,
)

SUBTOTAL = Decimal("10055")
COSTO = Decimal("3347.830588")
FACTOR = Decimal(3)


def test_horno_grande_como_en_el_excel() -> None:
    """La quema baja S/1447,930588 de costo: x3 son 4343,79 menos de precio."""
    ahorro = savings_from_cost(Decimal("1447.930588"), FACTOR)
    assert ahorro == Decimal("4343.791764")
    assert estimated_subtotal(SUBTOTAL, ahorro) == Decimal("5711.208236")


def test_bajar_al_factor_minimo_como_en_el_excel() -> None:
    ahorro = savings_from_factor(COSTO, FACTOR, Decimal(2))
    assert ahorro == COSTO
    assert estimated_subtotal(SUBTOTAL, ahorro) == Decimal("6707.169412")


def test_quitar_la_ilustracion_como_en_el_excel() -> None:
    assert estimated_subtotal(SUBTOTAL, savings_from_cost(Decimal(44), FACTOR)) == Decimal(9923)


def test_el_factor_sugerido_nunca_baja_de_dos() -> None:
    assert lowest_factor(None) == FACTOR_FLOOR
    assert lowest_factor(Decimal("1.5")) == Decimal(2)
    assert lowest_factor(Decimal("2.5")) == Decimal("2.5")
    with pytest.raises(ReductionMathError):
        savings_from_factor(COSTO, FACTOR, Decimal("1.9"))


def test_un_factor_ya_minimo_no_ahorra() -> None:
    assert savings_from_factor(COSTO, Decimal(2), Decimal(2)) == Decimal(0)


def test_sin_reduccion_de_costo_no_hay_ahorro() -> None:
    assert savings_from_cost(Decimal(0), FACTOR) == Decimal(0)
    assert savings_from_cost(Decimal(-5), FACTOR) == Decimal(0)


def test_un_factor_por_debajo_de_dos_es_un_error() -> None:
    with pytest.raises(ReductionMathError):
        savings_from_cost(Decimal(10), Decimal("1.5"))


def test_el_estimado_nunca_es_negativo() -> None:
    assert estimated_subtotal(Decimal(100), Decimal(500)) == Decimal(0)
