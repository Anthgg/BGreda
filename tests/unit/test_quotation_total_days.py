"""Fase 009G.1 — los dias de la cotizacion son el maximo de sus lineas."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.schemas.quotation_builder import QuotationBuilderItemOut
from app.services.quotation_builder import QuotationBuilderService


def _linea(total_days: int, orden: int = 0) -> QuotationBuilderItemOut:
    return QuotationBuilderItemOut(
        product_id=1,
        product_internal_reference="LAB50001",
        product_name="Pieza",
        product_type="FINISHED_PRODUCT",
        product_uom="NIU",
        quantity=1,
        total_days=total_days,
        commercial_sale_unit_price=Decimal(10),
        commercial_subtotal=Decimal(10),
        commercial_total=Decimal(10),
        tax_percentage_snapshot=Decimal(18),
        tax_amount=Decimal("1.8"),
        final_unit_cost=Decimal(5),
        final_total_cost=Decimal(5),
        markup_percent=Decimal(100),
        effective_profit_total=Decimal(5),
        source_fingerprint="fp",
        complete=True,
        sort_order=orden,
    )


@pytest.mark.parametrize(
    ("dias", "esperado"),
    [
        # El caso del enunciado: una linea larga y otra corta dentro del mismo
        # periodo. Sumar daria 21 y cobraria dos veces dias que no existen.
        ([13, 8], 13),
        ([8, 13], 13),
        ([13], 13),
        ([13, 13], 13),
        ([0, 0], 0),
        ([], 0),
    ],
)
def test_los_dias_de_la_cotizacion_son_el_maximo(dias: list[int], esperado: int) -> None:
    lineas = [_linea(valor, orden) for orden, valor in enumerate(dias)]
    assert QuotationBuilderService._quotation_total_days(lineas) == esperado


def test_sumar_los_dias_seria_otra_cosa() -> None:
    """Deja escrito por que no es una suma, para que nadie lo 'arregle'.

    El alquiler del taller se paga por calendario, no por producto. Dos piezas
    que se hacen en la misma quincena ocupan una quincena, no dos.
    """
    lineas = [_linea(13, 0), _linea(8, 1)]
    assert QuotationBuilderService._quotation_total_days(lineas) == 13
    assert QuotationBuilderService._quotation_total_days(lineas) != 21
