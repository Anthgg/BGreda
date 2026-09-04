"""Fase 009K.1 — el cargo comercial se cobra por lo que dice, no multiplicado.

Un cargo de prototipo no es una pieza que se fabrique: es un concepto que se
cobra. Lo que estas pruebas protegen es todo lo que NO debe pasarle —factor de
produccion, margen, costos fijos, quema— porque ese es el fallo que nadie
detecta: el importe sigue teniendo pinta de precio.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.core.pricing import (
    CommercialLinePricingInput,
    LinePricingInput,
    PricingError,
    price_commercial_line,
    price_line,
)
from app.services.quotation_builder import (
    QuotationBuilderService,
    QuotationCommercialLineWithoutProductError,
    _frozen_rounding_step,
)

PASO = Decimal("0.5")


def _cargo(**cambios: object) -> CommercialLinePricingInput:
    base: dict[str, object] = {
        "quantity": 1,
        "manual_net_amount": Decimal(50),
        "tax_percent": Decimal(0),
        "rounding_step": PASO,
        "currency": "PEN",
        "exchange_rate": None,
    }
    base.update(cambios)
    return CommercialLinePricingInput(**base)  # type: ignore[arg-type]


def test_el_cargo_vale_lo_que_se_escribio() -> None:
    """Sin impuesto, cincuenta son cincuenta."""
    resultado = price_commercial_line(_cargo())
    assert resultado.line_total_net == Decimal(50)
    assert resultado.line_total_tax == Decimal(0)
    assert resultado.line_total_gross == Decimal(50)


def test_el_factor_de_produccion_no_lo_toca() -> None:
    """COMMERCIAL_LINE_FACTOR_APPLIED: 0.

    Una linea de producto con costo tecnico 50 y factor 3 acaba en 150 antes
    del margen. El cargo con el mismo importe se queda en 50: no pasa por ahi.
    """
    linea = price_line(
        LinePricingInput(
            quantity=1,
            technical_cost=Decimal(50),
            production_factor=Decimal(3),
            fixed_cost_allocation=Decimal(0),
            markup_percent=Decimal(0),
            tax_percent=Decimal(0),
            rounding_step=PASO,
            currency="PEN",
        )
    )
    assert linea.line_total_net == Decimal(150)

    cargo = price_commercial_line(_cargo(manual_net_amount=Decimal(50)))
    assert cargo.line_total_net == Decimal(50)


def test_el_margen_del_producto_no_lo_toca() -> None:
    """COMMERCIAL_LINE_PRODUCT_MARKUP_APPLIED: 0.

    Con 100 % de margen una linea duplica; el cargo no. Cincuenta siguen siendo
    cincuenta aunque el pedido lleve el margen que lleve.
    """
    cargo = price_commercial_line(_cargo(manual_net_amount=Decimal(50)))
    assert cargo.line_total_net == Decimal(50)
    assert cargo.line_total_net != Decimal(100)


def test_el_impuesto_se_aplica_una_sola_vez() -> None:
    """100 netos con 18 % dan 118 brutos, y el neto reconstruido sigue siendo 100."""
    cargo = price_commercial_line(_cargo(manual_net_amount=Decimal(100), tax_percent=Decimal(18)))
    assert cargo.line_total_gross == Decimal(118)
    assert cargo.line_total_net == Decimal(100)
    assert cargo.line_total_tax == Decimal(18)


def test_en_dolares_el_importe_manual_no_se_convierte() -> None:
    """Quien escribe 200 cotizando en dolares quiere cobrar doscientos dolares.

    Es la misma regla que ya rige para el precio manual de una linea. Convertir
    aqui lo dividiria por la tasa y cobraria 53,33 sin que nadie lo pidiera.
    """
    cargo = price_commercial_line(
        _cargo(manual_net_amount=Decimal(200), currency="USD", exchange_rate=Decimal("3.75"))
    )
    assert cargo.line_total_net == Decimal(200)


def test_una_cotizacion_en_soles_no_admite_tasa() -> None:
    """La coherencia moneda/tasa se valida igual que en el resto del motor."""
    with pytest.raises(PricingError):
        price_commercial_line(_cargo(currency="PEN", exchange_rate=Decimal("3.75")))


def test_en_dolares_hace_falta_la_tasa() -> None:
    with pytest.raises(PricingError):
        price_commercial_line(_cargo(currency="USD", exchange_rate=None))


def test_el_redondeo_es_el_del_motor_y_va_hacia_arriba() -> None:
    """No hay un segundo redondeador: el documento tiene que seguir sumando."""
    cargo = price_commercial_line(
        _cargo(manual_net_amount=Decimal("10.10"), tax_percent=Decimal(0))
    )
    assert cargo.line_total_gross == Decimal("10.5")


def test_un_cargo_no_puede_ser_gratis_ni_negativo() -> None:
    for importe in (Decimal(0), Decimal(-1)):
        with pytest.raises(PricingError):
            price_commercial_line(_cargo(manual_net_amount=importe))


def test_la_cantidad_multiplica_el_cargo() -> None:
    cargo = price_commercial_line(_cargo(quantity=3, manual_net_amount=Decimal(50)))
    assert cargo.line_total_net == Decimal(150)


# ---------------------------------------------------------------------------
# El guard de contexto comercial, sin base de datos
# ---------------------------------------------------------------------------
class _LineaFalsa:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.production_snapshot = snapshot


class _CotizacionFalsa:
    def __init__(self, *lineas: _LineaFalsa) -> None:
        self.items = list(lineas)


def test_una_linea_de_producto_sin_plan_comercial_no_habilita_cargos() -> None:
    """PARTIAL_PRODUCT_WITHOUT_COMMERCIAL_PLAN.

    El guard mira que exista PLAN, no que existan filas. Hoy el builder escribe
    un plan en cada linea que guarda, asi que este estado no se alcanza por la
    API —solo lo tendrian filas anteriores a 009E, que son LEGACY y el builder
    ya rechaza—. Se prueba aqui, contra la condicion, porque fabricar el estado
    por HTTP obligaria a inventarselo.
    """
    with pytest.raises(QuotationCommercialLineWithoutProductError):
        QuotationBuilderService._ensure_commercial_context(
            _CotizacionFalsa(_LineaFalsa({}))  # type: ignore[arg-type]
        )

    with pytest.raises(QuotationCommercialLineWithoutProductError):
        QuotationBuilderService._ensure_commercial_context(
            _CotizacionFalsa()  # type: ignore[arg-type]
        )

    # Con plan congelado, pasa.
    QuotationBuilderService._ensure_commercial_context(
        _CotizacionFalsa(_LineaFalsa({"commercial_plan": {"rounding_step": "0.50"}}))  # type: ignore[arg-type]
    )


def test_sin_politica_congelada_el_redondeo_falla_en_vez_de_valer_cero() -> None:
    """COMMERCIAL_LINE_MISSING_POLICY_DEFAULT: 0.

    Cero no es un paso de redondeo: `ceil_to_step` lo rechaza. Devolverlo como
    relleno convertia la ausencia de politica en un 500 lejos de su causa. Que
    falte tiene que fallar aqui, donde todavia se puede explicar.
    """
    with pytest.raises(QuotationCommercialLineWithoutProductError):
        _frozen_rounding_step(_CotizacionFalsa(_LineaFalsa({})))  # type: ignore[arg-type]

    assert _frozen_rounding_step(
        _CotizacionFalsa(_LineaFalsa({"commercial_plan": {"rounding_step": "0.50"}}))  # type: ignore[arg-type]
    ) == Decimal("0.50")
