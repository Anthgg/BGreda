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
    LinePricing,
    LinePricingInput,
    PricingError,
    allocate_fixed_costs,
    ceil_to_step,
    convert_net_to_quote_currency,
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


# ---------------------------------------------------------------------------
# Fase 009F — moneda de emisión y tipo de cambio manual
# ---------------------------------------------------------------------------


def linea_usd(
    *,
    tecnico: str = "125",
    factor: str = "3",
    cantidad: int = 1,
    markup: str = "0",
    igv: str = "18",
    tasa: str | None = "3.75",
    paso: str = "0.50",
    manual: str | None = None,
) -> LinePricing:
    """Una línea en dólares. El costo técnico sigue estando en soles."""
    return price_line(
        LinePricingInput(
            quantity=cantidad,
            technical_cost=D(tecnico),
            production_factor=D(factor),
            fixed_cost_allocation=D("0"),
            markup_percent=D(markup),
            tax_percent=D(igv),
            rounding_step=D(paso),
            currency="USD",
            exchange_rate=None if tasa is None else D(tasa),
            manual_net_unit=None if manual is None else D(manual),
        )
    )


class TestCurrencyConversion:
    def test_pen_no_convierte(self) -> None:
        """PEN_NO_CONVERSION: la moneda base no pasa por el divisor."""
        result = price_line(
            LinePricingInput(
                quantity=1,
                technical_cost=D("125"),
                production_factor=D("3"),
                fixed_cost_allocation=D("0"),
                markup_percent=D("0"),
                tax_percent=D("0"),
                rounding_step=D("0.50"),
            )
        )
        assert result.currency == "PEN"
        assert result.exchange_rate is None
        assert result.raw_net_unit == result.raw_net_unit_base == D("375")

    def test_usd_divide_por_la_tasa(self) -> None:
        """USD_DIVIDES_BY_RATE. 375 soles a 3,75 son 100 dólares.

        Multiplicar daría 1406,25: casi cuatro veces el número correcto y con
        toda la pinta de ser un precio. Es el error que nadie detecta a ojo.
        """
        result = linea_usd()
        assert result.raw_net_unit_base == D("375")
        assert result.raw_net_unit == D("100")
        assert result.currency == "USD"
        assert result.exchange_rate == D("3.75")

    def test_el_costo_interno_no_se_convierte(self) -> None:
        """Los pasos anteriores al neto siguen en soles aunque se emita en USD."""
        result = linea_usd()
        assert result.technical_cost == D("125")
        assert result.factored_cost == D("375")
        assert result.commercial_base_cost == D("375")
        assert result.commercial_base_unit_cost == D("375")

    def test_usd_sin_tasa_es_un_error(self) -> None:
        with pytest.raises(PricingError, match="EXCHANGE_RATE_REQUIRED"):
            linea_usd(tasa=None)

    @pytest.mark.parametrize("tasa", ["0", "-3.75"])
    def test_una_tasa_no_positiva_es_un_error(self, tasa: str) -> None:
        with pytest.raises(PricingError, match="mayor que cero"):
            linea_usd(tasa=tasa)

    def test_pen_con_tasa_es_un_error(self) -> None:
        # Guardar una tasa en una cotización en soles describe una conversión
        # que nunca ocurrió, y quien la lea creerá que hubo cambio de moneda.
        with pytest.raises(PricingError, match="PEN no lleva tipo de cambio"):
            price_line(
                LinePricingInput(
                    quantity=1,
                    technical_cost=D("100"),
                    production_factor=D("3"),
                    fixed_cost_allocation=D("0"),
                    markup_percent=D("0"),
                    tax_percent=D("18"),
                    rounding_step=D("0.50"),
                    currency="PEN",
                    exchange_rate=D("1"),
                )
            )

    def test_una_moneda_no_autorizada_es_un_error(self) -> None:
        with pytest.raises(PricingError, match="Moneda no admitida"):
            price_line(
                LinePricingInput(
                    quantity=1,
                    technical_cost=D("100"),
                    production_factor=D("3"),
                    fixed_cost_allocation=D("0"),
                    markup_percent=D("0"),
                    tax_percent=D("18"),
                    rounding_step=D("0.50"),
                    currency="EUR",
                    exchange_rate=D("4.10"),
                )
            )

    def test_convert_net_to_quote_currency_es_una_division(self) -> None:
        """EXCHANGE_RATE_SEMANTICS: `1 USD = X PEN`."""
        assert convert_net_to_quote_currency(D("375"), "USD", D("3.75")) == D("100")
        assert convert_net_to_quote_currency(D("375"), "PEN", None) == D("375")


class TestUsdTaxAndRounding:
    def test_el_igv_se_aplica_despues_de_convertir(self) -> None:
        """IGV_AFTER_CONVERSION. 100 USD netos al 18 % son 118 brutos."""
        result = linea_usd()
        assert result.raw_net_unit == D("100")
        assert result.raw_tax_unit == D("18.00")
        assert result.raw_gross_unit == D("118.00")

    @pytest.mark.parametrize("igv", ["0", "10", "18", "21"])
    def test_igv_dinamico_en_dolares(self, igv: str) -> None:
        """USD_DYNAMIC_TAX: la tasa entra por parámetro también en USD."""
        result = linea_usd(igv=igv)
        assert result.raw_gross_unit == D("100") * (Decimal(1) + D(igv) / Decimal(100))

    def test_ceiling_050_en_dolares(self) -> None:
        """USD_CEILING_050: el caso del enunciado, en céntimos de dólar.

        375,32 soles a 3,75 son 100,0853… netos; con 18 % el bruto crudo cae
        en 118,1006… y sube al siguiente medio dólar.
        """
        result = linea_usd(tecnico="375.32", factor="1")
        assert result.raw_net_unit_base == D("375.32")
        assert result.final_gross_unit == D("118.50")

    def test_ceiling_100_en_dolares(self) -> None:
        """USD_CEILING_100: la misma regla, otro paso."""
        result = linea_usd(tecnico="375.32", factor="1", paso="1.00")
        assert result.final_gross_unit == D("119.00")

    def test_reconstruccion_en_dolares(self) -> None:
        """USD_RECONSTRUCT_NET_TAX: neto + IGV es exactamente el bruto."""
        result = linea_usd(tecnico="375.32", factor="1")
        assert result.final_gross_unit == D("118.50")
        assert result.final_net_unit + result.final_tax_unit == result.final_gross_unit

    def test_el_paso_es_el_mismo_en_las_dos_monedas(self) -> None:
        # No hay política PEN=0,50 / USD=1,00: la regla es la misma y sólo
        # cambia la unidad monetaria.
        assert linea_usd().rounding_step == D("0.50")


class TestUsdManualPrice:
    def test_el_precio_manual_ya_esta_en_moneda_de_emision(self) -> None:
        """USD_MANUAL_PRICE_IS_QUOTE_CURRENCY_NET.

        Quien escribe 100 cotizando en dólares quiere cobrar cien dólares.
        """
        assert linea_usd(manual="100").raw_net_unit == D("100")

    def test_el_precio_manual_no_se_convierte_dos_veces(self) -> None:
        """MANUAL_PRICE_NOT_DOUBLE_CONVERTED.

        Dividirlo otra vez por 3,75 cobraría 26,67 dólares en vez de 100.
        """
        result = linea_usd(manual="100")
        assert result.raw_net_unit != D("100") / D("3.75")
        assert result.raw_net_unit == D("100")

    def test_el_precio_manual_en_dolares_pasa_por_igv_y_redondeo(self) -> None:
        result = linea_usd(manual="100.10")
        assert result.raw_gross_unit == D("100.10") * D("1.18")
        assert result.final_gross_unit == D("118.50")
        assert result.final_net_unit + result.final_tax_unit == D("118.50")

    def test_el_precio_manual_no_exime_de_declarar_la_tasa(self) -> None:
        # Sin tasa la cotización no es reproducible aunque el precio esté
        # escrito a mano: el documento diría dólares sin decir a cuánto.
        with pytest.raises(PricingError, match="EXCHANGE_RATE_REQUIRED"):
            linea_usd(manual="100", tasa=None)


class TestUsdMultiline:
    def test_el_total_usd_es_la_suma_sin_redondear_otra_vez(self) -> None:
        """USD_NO_SECOND_TOTAL_ROUNDING."""
        lineas = [
            linea_usd(tecnico=tecnico, cantidad=cantidad, markup=markup)
            for tecnico, cantidad, markup in (("125", 10, "100"), ("41", 7, "50"))
        ]
        total = sum((line.line_total_gross for line in lineas), Decimal(0))
        assert total == sum(
            (line.final_gross_unit * Decimal(line.quantity) for line in lineas), Decimal(0)
        )
        for line in lineas:
            assert line.final_gross_unit % D("0.50") == Decimal(0)
            assert line.line_total_net + line.line_total_tax == line.line_total_gross

    def test_todas_las_lineas_usan_la_misma_tasa(self) -> None:
        # El tipo de cambio es de la cotización, no de la línea: dos tasas en
        # un mismo documento harían un total que nadie puede explicar.
        lineas = [linea_usd(tecnico="125"), linea_usd(tecnico="41")]
        assert {line.exchange_rate for line in lineas} == {D("3.75")}
