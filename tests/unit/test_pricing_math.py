"""Fase 009E — factor, costos fijos, margen, IGV y redondeo contractual.

Tests puros sobre `app.core.pricing`: sin base de datos, para fijar la regla
comercial sin depender del entorno. Los importes son `Decimal` en todas partes;
un céntimo de error por pieza se multiplica por la cantidad.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.pricing import (
    DEFAULT_PRODUCTION_FACTOR,
    LinePricingInput,
    PricingError,
    allocate_fixed_costs,
    ceil_to_step,
    price_line,
    reconstruct_net_and_tax,
)


def D(value: str) -> Decimal:
    return Decimal(value)


class TestProductionFactor:
    def test_el_factor_por_defecto_es_tres(self) -> None:
        assert DEFAULT_PRODUCTION_FACTOR == Decimal(3)

    def test_el_factor_multiplica_el_costo_tecnico(self) -> None:
        # FACTOR_3: 100 x 3 = 300. Es un factor de PRODUCCION: entra antes de
        # los costos fijos y antes del margen.
        result = price_line(
            LinePricingInput(
                quantity=1,
                technical_cost=D("100"),
                production_factor=D("3"),
                fixed_cost_allocation=D("0"),
                markup_percent=D("0"),
                tax_percent=D("0"),
                rounding_step=D("0.50"),
            )
        )
        assert result.factored_cost == D("300")
        assert result.commercial_base_cost == D("300")

    def test_el_factor_cero_es_un_error(self) -> None:
        with pytest.raises(PricingError):
            price_line(
                LinePricingInput(
                    quantity=1,
                    technical_cost=D("100"),
                    production_factor=D("0"),
                    fixed_cost_allocation=D("0"),
                    markup_percent=D("0"),
                    tax_percent=D("0"),
                    rounding_step=D("0.50"),
                )
            )


class TestFixedCostAllocation:
    def test_el_caso_del_enunciado(self) -> None:
        # FIXED_COST_ALLOCATION: 600 y 400 factorados, 320 de fijos.
        assert allocate_fixed_costs([D("600"), D("400")], D("320")) == (D("192.00"), D("128.00"))

    def test_la_suma_reconcilia_exactamente(self) -> None:
        # El caso que delata un reparto que pierde céntimos: 320 entre tres.
        allocations = allocate_fixed_costs([D("100"), D("100"), D("100")], D("320"))
        assert sum(allocations) == D("320")
        assert len(allocations) == 3

    def test_el_costo_fijo_no_depende_del_numero_de_productos(self) -> None:
        # FIXED_COST_DUPLICATED_PER_PRODUCT: NO. Es el defecto que 009E corrige:
        # antes, cada línea cobraba el alquiler y el administrativo enteros.
        for lineas in (1, 2, 10):
            allocations = allocate_fixed_costs([D("100")] * lineas, D("320"))
            assert sum(allocations) == D("320"), f"{lineas} líneas cobran 320, ni más ni menos"

    def test_reparte_en_proporcion_al_costo_factorado(self) -> None:
        # Una línea que cuesta el triple carga el triple de fijos.
        a, b = allocate_fixed_costs([D("300"), D("100")], D("400"))
        assert a == D("300.00")
        assert b == D("100.00")

    def test_base_cero_se_bloquea_en_vez_de_repartir_a_partes_iguales(self) -> None:
        # FIXED_COST_ALLOCATION_BASE_ZERO. Repartir por igual sería inventarse
        # una regla que nadie decidió.
        with pytest.raises(PricingError, match="FIXED_COST_ALLOCATION_BASE_ZERO"):
            allocate_fixed_costs([D("0"), D("0")], D("320"))

    def test_sin_lineas_no_hay_reparto(self) -> None:
        with pytest.raises(PricingError):
            allocate_fixed_costs([], D("320"))


class TestCeilingRounding:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("142.01", "142.50"),
            ("142.50", "142.50"),
            ("142.51", "143.00"),
            ("143.00", "143.00"),
            ("143.01", "143.50"),
        ],
    )
    def test_paso_050(self, raw: str, expected: str) -> None:
        """CEILING_050: los casos obligatorios del enunciado, literales."""
        assert ceil_to_step(D(raw), D("0.50")) == D(expected)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("142.01", "143.00"),
            ("142.99", "143.00"),
            ("143.00", "143.00"),
            ("143.01", "144.00"),
        ],
    )
    def test_paso_100(self, raw: str, expected: str) -> None:
        """CEILING_100."""
        assert ceil_to_step(D(raw), D("1.00")) == D(expected)

    def test_un_valor_exacto_no_se_mueve(self) -> None:
        # EXACT_BOUNDARY_NO_CHANGE. Es el borde que rompe una implementación
        # con float: 142.50 / 0.50 da 285.00000000000006 y subiría a 143.
        assert ceil_to_step(D("142.50"), D("0.50")) == D("142.50")
        assert ceil_to_step(D("143.00"), D("1.00")) == D("143.00")
        assert ceil_to_step(D("0.00"), D("0.50")) == D("0.00")

    def test_nunca_redondea_hacia_abajo(self) -> None:
        # La diferencia con la regla anterior: 8.10 bajaba a 8.00. Ahora sube.
        assert ceil_to_step(D("8.10"), D("0.50")) == D("8.50")
        assert ceil_to_step(D("8.24"), D("0.50")) == D("8.50")

    def test_un_paso_no_admitido_se_rechaza(self) -> None:
        with pytest.raises(PricingError, match="Paso de redondeo no admitido"):
            ceil_to_step(D("10"), D("0.10"))


class TestNetTaxReconstruction:
    def test_neto_mas_igv_es_exactamente_el_bruto(self) -> None:
        # NET_PLUS_TAX_EQUALS_GROSS. Se resta en vez de volver a multiplicar,
        # así que la identidad es exacta por construcción.
        for gross in ("142.50", "143.00", "0.50", "1000.00", "7.77"):
            net, tax = reconstruct_net_and_tax(D(gross), D("18"))
            assert net + tax == D(gross), gross

    def test_reconstruye_desde_el_bruto_redondeado(self) -> None:
        # RECONSTRUCT_NET_FROM_ROUNDED_GROSS: 118 al 18 % son 100 netos.
        net, tax = reconstruct_net_and_tax(D("118.00"), D("18"))
        assert net == D("100.00")
        assert tax == D("18.00")

    def test_sin_igv_el_neto_es_el_bruto(self) -> None:
        net, tax = reconstruct_net_and_tax(D("142.50"), D("0"))
        assert net == D("142.50")
        assert tax == D("0")


class TestDynamicTax:
    @pytest.mark.parametrize("tax", ["0", "10", "18", "21"])
    def test_el_igv_no_esta_fijado_en_dieciocho(self, tax: str) -> None:
        """DYNAMIC_TAX: la tasa entra por parámetro y cambia el resultado."""
        result = price_line(
            LinePricingInput(
                quantity=1,
                technical_cost=D("100"),
                production_factor=D("1"),
                fixed_cost_allocation=D("0"),
                markup_percent=D("0"),
                tax_percent=D(tax),
                rounding_step=D("0.50"),
            )
        )
        assert result.raw_gross_unit == D("100") * (Decimal(1) + D(tax) / Decimal(100))


class TestCanonicalCase:
    """Un caso trazable de punta a punta, con cada transición comprobada."""

    def test_caso_canonico_completo(self) -> None:
        result = price_line(
            LinePricingInput(
                quantity=10,
                technical_cost=D("100"),
                production_factor=D("3"),
                fixed_cost_allocation=D("192"),
                markup_percent=D("100"),
                tax_percent=D("18"),
                rounding_step=D("0.50"),
            )
        )

        assert result.technical_cost == D("100")
        assert result.factored_cost == D("300")  # 100 x 3
        assert result.fixed_cost_allocation == D("192")
        assert result.commercial_base_cost == D("492")  # 300 + 192
        assert result.commercial_base_unit_cost == D("49.2")  # / 10
        assert result.raw_net_unit == D("98.4")  # x (1 + 100 %)
        assert result.raw_tax_unit == D("17.712")  # x 18 %
        assert result.raw_gross_unit == D("116.112")
        assert result.final_gross_unit == D("116.50")  # CEILING a 0,50
        assert result.rounding_adjustment_unit == D("0.39")  # 116.50 - 116.11

        # El neto se reconstruye desde el bruto contractual, no se conserva.
        assert result.final_net_unit == D("98.73")
        assert result.final_tax_unit == D("17.77")
        assert result.final_net_unit + result.final_tax_unit == result.final_gross_unit

        assert result.line_total_gross == D("1165.00")  # 116.50 x 10
        assert result.line_total_net + result.line_total_tax == result.line_total_gross


class TestMarkupPerLine:
    def test_cada_linea_lleva_su_propio_margen(self) -> None:
        """PER_PRODUCT_MARKUP: 100 % y 50 % conviven en la misma cotización."""

        def precio(markup: str) -> Decimal:
            return price_line(
                LinePricingInput(
                    quantity=1,
                    technical_cost=D("100"),
                    production_factor=D("1"),
                    fixed_cost_allocation=D("0"),
                    markup_percent=D(markup),
                    tax_percent=D("0"),
                    rounding_step=D("0.50"),
                )
            ).raw_net_unit

        assert precio("100") == D("200")
        assert precio("50") == D("150")
        assert precio("0") == D("100")


class TestNoSecondRounding:
    def test_el_total_es_la_suma_de_las_lineas_sin_redondear_otra_vez(self) -> None:
        """NO_SECOND_TOTAL_ROUNDING.

        El total del documento tiene que ser la suma de las líneas que el
        propio documento enumera. El cliente sabe sumar.
        """
        lineas = [
            price_line(
                LinePricingInput(
                    quantity=cantidad,
                    technical_cost=D(costo),
                    production_factor=D("3"),
                    fixed_cost_allocation=D("100"),
                    markup_percent=D(markup),
                    tax_percent=D("18"),
                    rounding_step=D("0.50"),
                )
            )
            for cantidad, costo, markup in ((7, "33", "100"), (13, "41", "50"), (3, "17", "80"))
        ]

        total = sum((line.line_total_gross for line in lineas), Decimal(0))
        assert total == sum(
            (line.final_gross_unit * Decimal(line.quantity) for line in lineas), Decimal(0)
        )
        # Y ninguna línea perdió su precio unitario redondo por el camino.
        for line in lineas:
            assert line.final_gross_unit % D("0.50") == Decimal(0)
