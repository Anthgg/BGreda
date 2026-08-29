"""Fase 009D — agua, rendimiento, concentracion, g <-> ml y el 15 %.

Tests puros sobre `app.core.preparations`: sin base de datos, para fijar la
regla de negocio sin depender del entorno.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.preparations import (
    ComponentShare,
    PreparationError,
    batch_total_cost,
    component_amounts,
    distribute_glaze,
    estimated_glaze_grams,
    grams_to_ml,
    ml_to_grams,
    solids_concentration_g_per_ml,
    unit_cost_per_ml,
)


def D(value: str) -> Decimal:
    return Decimal(value)


class TestComponentAmounts:
    def test_reparte_el_peso_seco_segun_los_porcentajes(self) -> None:
        # RECIPE_DRY_WEIGHT_CALCULATION: 50/30/20 sobre 1000 g.
        amounts = component_amounts(
            [
                ComponentShare(1, D("50"), D("0.10")),
                ComponentShare(2, D("30"), D("0.20")),
                ComponentShare(3, D("20"), D("0.50")),
            ],
            D("1000"),
        )
        assert [a.quantity_g for a in amounts] == [D("500"), D("300"), D("200")]
        # El peso repartido es exactamente el pedido: ni un gramo de mas.
        assert sum(a.quantity_g for a in amounts) == D("1000")

    def test_los_porcentajes_no_tienen_que_sumar_cien(self) -> None:
        # Base 100 + aditivo 5 = 105. Pedir 1050 g reparte 1000 y 50, no 1102,5:
        # el peso del lote es el que se pide, no el que salga de inflar la base.
        amounts = component_amounts(
            [ComponentShare(1, D("100"), D("0.10")), ComponentShare(2, D("5"), D("1.00"))],
            D("1050"),
        )
        assert [a.quantity_g for a in amounts] == [D("1000"), D("50")]

    def test_el_costo_de_linea_usa_el_precio_congelado(self) -> None:
        amounts = component_amounts([ComponentShare(1, D("100"), D("0.25"))], D("400"))
        assert amounts[0].unit_cost_snapshot == D("0.25")
        assert amounts[0].line_cost == D("100")

    def test_sin_componentes_es_un_error(self) -> None:
        with pytest.raises(PreparationError):
            component_amounts([], D("1000"))

    def test_peso_seco_cero_es_un_error(self) -> None:
        with pytest.raises(PreparationError):
            component_amounts([ComponentShare(1, D("100"), D("0.1"))], D("0"))


class TestBatchCost:
    def test_el_total_es_la_suma_exacta_de_las_lineas(self) -> None:
        # El caso del enunciado: 50 + 100 + 50 = 200.
        amounts = component_amounts(
            [
                ComponentShare(1, D("25"), D("0.20")),
                ComponentShare(2, D("50"), D("0.20")),
                ComponentShare(3, D("25"), D("0.20")),
            ],
            D("1000"),
        )
        total = batch_total_cost(amounts)
        assert total == D("200")
        assert sum(a.line_cost for a in amounts) == total


class TestYieldAndConcentration:
    def test_concentracion_y_costo_por_ml_con_mil_mililitros(self) -> None:
        # SOLIDS_PER_ML + COST_PER_ML del enunciado.
        assert solids_concentration_g_per_ml(D("200"), D("1000")) == D("0.200000000000")
        assert unit_cost_per_ml(D("200"), D("1000")) == D("0.200000000000")

    def test_mas_agua_rinde_mas_y_abarata_el_mililitro(self) -> None:
        # Mismo peso seco y mismo costo, mas rendimiento: baja el costo por ml
        # y baja la concentracion. El agua NO encarece los solidos.
        assert solids_concentration_g_per_ml(D("200"), D("1200")) == D("0.166666666667")
        assert unit_cost_per_ml(D("200"), D("1200")) == D("0.166666666667")

    def test_rendimiento_cero_es_un_error(self) -> None:
        with pytest.raises(PreparationError):
            solids_concentration_g_per_ml(D("200"), D("0"))
        with pytest.raises(PreparationError):
            unit_cost_per_ml(D("200"), D("0"))


class TestConversions:
    def test_grams_to_ml(self) -> None:
        # GRAMS_TO_ML del enunciado: 75 g a 0,20 g/ml -> 375 ml.
        assert grams_to_ml(D("75"), D("0.20")) == D("375")

    def test_ml_to_grams(self) -> None:
        assert ml_to_grams(D("375"), D("0.20")) == D("75")

    def test_ida_y_vuelta_conserva_el_valor(self) -> None:
        concentration = D("0.20")
        assert ml_to_grams(grams_to_ml(D("75"), concentration), concentration) == D("75")

    def test_no_se_asume_densidad_uno(self) -> None:
        # Con densidad 1, 75 g serian 75 ml. Con la concentracion real son 375.
        assert grams_to_ml(D("75"), D("0.20")) != D("75")

    def test_sin_concentracion_no_se_convierte(self) -> None:
        # MISSING_CONCENTRATION: es mejor negarse que inventar un factor.
        with pytest.raises(PreparationError):
            grams_to_ml(D("75"), D("0"))
        with pytest.raises(PreparationError):
            ml_to_grams(D("375"), D("0"))


class TestEstimatedGlaze:
    def test_el_caso_del_enunciado(self) -> None:
        # ESTIMATED_15_PERCENT: 500 g x 15 % x 20 piezas = 1500 g.
        assert estimated_glaze_grams(D("500"), 20, D("15")) == D("1500")

    def test_una_pieza_son_75_gramos(self) -> None:
        assert estimated_glaze_grams(D("500"), 1, D("15")) == D("75")

    def test_el_porcentaje_llega_como_quince_no_como_cero_coma_quince(self) -> None:
        # Misma convencion que `tax_percent`. Si alguien guardase 0,15 el
        # resultado seria 100 veces menor, y este test lo delata.
        assert estimated_glaze_grams(D("500"), 1, D("15")) == D("75")
        assert estimated_glaze_grams(D("500"), 1, D("0.15")) == D("0.75")

    def test_sin_peso_de_pieza_no_se_estima(self) -> None:
        # MISSING_PIECE_WEIGHT: no se inventa un peso.
        with pytest.raises(PreparationError):
            estimated_glaze_grams(D("0"), 20, D("15"))

    def test_porcentaje_fuera_de_rango(self) -> None:
        with pytest.raises(PreparationError):
            estimated_glaze_grams(D("500"), 1, D("0"))
        with pytest.raises(PreparationError):
            estimated_glaze_grams(D("500"), 1, D("101"))


class TestGlazeDistribution:
    def test_dos_esmaltes_reparten_el_total_sin_duplicarlo(self) -> None:
        # MULTI_GLAZE_NO_DOUBLE_COUNT: 75 g repartidos, no 75 + 75.
        allocations = distribute_glaze(D("75"), [D("1"), D("1")])
        assert sum(allocations) == D("75")
        assert allocations == (D("37.5"), D("37.5"))

    def test_reparto_desigual_reconcilia(self) -> None:
        allocations = distribute_glaze(D("75"), [D("40"), D("35")])
        assert sum(allocations) == D("75")
        assert allocations == (D("40"), D("35"))

    def test_tres_esmaltes_no_pierden_el_resto_del_redondeo(self) -> None:
        # 100 entre 3 no es exacto: el ultimo tramo absorbe la diferencia para
        # que la suma cuadre con el total.
        allocations = distribute_glaze(D("100"), [D("1"), D("1"), D("1")])
        assert sum(allocations) == D("100")
        assert len(allocations) == 3

    def test_un_solo_esmalte_recibe_todo(self) -> None:
        assert distribute_glaze(D("75"), [D("1")]) == (D("75"),)

    def test_sin_esmaltes_no_hay_reparto(self) -> None:
        with pytest.raises(PreparationError):
            distribute_glaze(D("75"), [])

    def test_participaciones_en_cero_son_un_error(self) -> None:
        with pytest.raises(PreparationError):
            distribute_glaze(D("75"), [D("0"), D("0")])
