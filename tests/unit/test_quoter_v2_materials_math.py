"""Fase 010C — la aritmetica de materiales, con los ejemplos aprobados.

Cada numero de este fichero sale del Excel «Cotizador Greda V2» o de una regla
cerrada del proyecto. No son casos inventados para que el codigo pase: son los
casos que el negocio ya resolvio a mano, y el codigo tiene que dar lo mismo.

Los tres errores que estas pruebas existen para cazar son los que no fallan
solos: dividir por el importe sin transporte, escribir 0,015 donde va 0,15, y
aplicar la conversion g/ml dos veces. Ninguno lanza una excepcion; los tres
producen un precio creible y equivocado.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.quoter_v2_materials import (
    FALLBACK_ML_PER_GRAM,
    GLAZE_WEIGHT_RATIO,
    MaterialMathError,
    acquisition_total_cost,
    body_total_weight,
    cost_per_unit,
    effective_cost_per_unit,
    glaze_unit_weight,
    glaze_volume_ml,
    material_cost,
)


# ---------------------------------------------------------------------------
# Adquisicion: el transporte es parte del material
# ---------------------------------------------------------------------------
def test_el_transporte_entra_en_el_costo_de_adquisicion() -> None:
    """100 de compra mas 30 de transporte son 130. El ejemplo aprobado."""
    assert acquisition_total_cost(Decimal(100), Decimal(30)) == Decimal(130)


def test_costo_por_gramo_del_ejemplo_canonico() -> None:
    """100 kg por S/100 + S/30 de transporte = S/0,0013 por gramo.

    Este es EL numero de la fase. Calculado sobre los 100 sueltos daria
    0,001 —un 23 % menos— y ese error se multiplica por cada gramo de cada
    pieza de cada cotizacion sin que nada avise.
    """
    cien_kg_en_gramos = Decimal(100_000)

    resultado = cost_per_unit(Decimal(100), Decimal(30), cien_kg_en_gramos)

    assert resultado == Decimal("0.0013")


def test_sin_transporte_el_costo_es_solo_la_compra() -> None:
    assert cost_per_unit(Decimal(100), Decimal(0), Decimal(100_000)) == Decimal("0.001")


def test_una_cantidad_de_cero_no_produce_un_numero() -> None:
    """Decirlo aqui es mejor que propagar una division por cero hasta el precio."""
    with pytest.raises(MaterialMathError):
        cost_per_unit(Decimal(100), Decimal(30), Decimal(0))


@pytest.mark.parametrize(
    ("compra", "transporte"),
    [(Decimal(-1), Decimal(0)), (Decimal(0), Decimal(-1))],
)
def test_ni_la_compra_ni_el_transporte_admiten_negativos(
    compra: Decimal, transporte: Decimal
) -> None:
    with pytest.raises(MaterialMathError):
        acquisition_total_cost(compra, transporte)


def test_una_cantidad_enorme_no_pierde_precision() -> None:
    """Decimal, no float: con binario el resultado dejaria de ser exacto."""
    una_tonelada_en_gramos = Decimal(1_000_000)
    assert cost_per_unit(Decimal(1000), Decimal(300), una_tonelada_en_gramos) == Decimal("0.0013")


# ---------------------------------------------------------------------------
# Material regalado: lo que costo no es lo que vale
# ---------------------------------------------------------------------------
def test_un_material_regalado_puede_valorizarse() -> None:
    """Adquirirlo costo cero; regalar tambien el precio de venta es otra cosa."""
    assert effective_cost_per_unit(Decimal(0), Decimal("0.0012")) == Decimal("0.0012")


def test_sin_valorizacion_manda_el_costo_derivado() -> None:
    assert effective_cost_per_unit(Decimal("0.0013"), None) == Decimal("0.0013")


def test_una_valorizacion_en_cero_es_una_decision_y_se_respeta() -> None:
    """Alguien pudo decidir expresamente que este material no se valoriza.

    Por eso se distingue `None` —no hay decision— de `0` —la decision es cero—.
    """
    assert effective_cost_per_unit(Decimal("0.0013"), Decimal(0)) == Decimal(0)


def test_una_valorizacion_negativa_se_rechaza() -> None:
    with pytest.raises(MaterialMathError):
        effective_cost_per_unit(Decimal("0.0013"), Decimal("-0.0001"))


# ---------------------------------------------------------------------------
# Pasta
# ---------------------------------------------------------------------------
def test_peso_total_de_pasta_del_ejemplo_canonico() -> None:
    """500 g por pieza, 20 piezas, 10.000 g."""
    assert body_total_weight(Decimal(500), 20) == Decimal(10_000)


def test_costo_de_pasta_del_ejemplo_canonico() -> None:
    """10.000 g a S/0,0013 son S/13."""
    assert material_cost(Decimal(10_000), Decimal("0.0013")) == Decimal(13)


def test_cantidad_cero_da_peso_cero_y_no_un_error() -> None:
    """Una linea vacia es un estado legitimo de un borrador."""
    assert body_total_weight(Decimal(500), 0) == Decimal(0)


# ---------------------------------------------------------------------------
# Esmalte
# ---------------------------------------------------------------------------
def test_la_proporcion_de_esmalte_es_quince_por_ciento() -> None:
    """0,15. Ni 0,015 ni 1,5: el error mas facil de esta fase."""
    assert GLAZE_WEIGHT_RATIO == Decimal("0.15")


def test_esmalte_de_una_pieza_del_ejemplo_canonico() -> None:
    """500 g de pasta llevan 75 g de esmalte."""
    assert glaze_unit_weight(Decimal(500), requires_glaze=True) == Decimal(75)


def test_esmalte_total_del_ejemplo_canonico() -> None:
    """20 piezas de 75 g son 1.500 g."""
    unitario = glaze_unit_weight(Decimal(500), requires_glaze=True)
    assert body_total_weight(unitario, 20) == Decimal(1500)


def test_costo_de_esmalte_del_ejemplo_canonico() -> None:
    """1.500 g a S/0,20 el gramo son S/300."""
    assert material_cost(Decimal(1500), Decimal("0.20")) == Decimal(300)


def test_con_el_esmalte_apagado_el_peso_es_cero() -> None:
    """Y cero es cero: sin peso no hay costo, y no se cobra a escondidas."""
    assert glaze_unit_weight(Decimal(500), requires_glaze=False) == Decimal(0)


def test_apagar_el_esmalte_anula_tambien_su_costo() -> None:
    peso = glaze_unit_weight(Decimal(500), requires_glaze=False)
    assert material_cost(body_total_weight(peso, 20), Decimal("0.20")) == Decimal(0)


# ---------------------------------------------------------------------------
# Conversion g/ml
# ---------------------------------------------------------------------------
def test_sin_conversion_registrada_se_usa_la_de_reserva_y_se_dice() -> None:
    """1 g = 1 ml no es una verdad fisica: es una suposicion, y se declara."""
    millilitros, es_fallback = glaze_volume_ml(Decimal(1500), None)

    assert millilitros == Decimal(1500)
    assert es_fallback is True
    assert FALLBACK_ML_PER_GRAM == Decimal(1)


def test_la_conversion_registrada_gana_sobre_la_de_reserva() -> None:
    millilitros, es_fallback = glaze_volume_ml(Decimal(1500), Decimal("0.8"))

    assert millilitros == Decimal(1200)
    assert es_fallback is False


@pytest.mark.parametrize("conversion", [Decimal(0), Decimal("-0.5")])
def test_una_conversion_imposible_se_rechaza(conversion: Decimal) -> None:
    """Cero o negativa no es un dato pobre: daria un volumen con aspecto real."""
    with pytest.raises(MaterialMathError):
        glaze_volume_ml(Decimal(1500), conversion)


def test_la_conversion_no_se_aplica_dos_veces() -> None:
    """Convertir lo ya convertido da un numero plausible y equivocado.

    Se fija aqui porque el fallo no lanza nada: 1.500 g con 0,8 son 1.200 ml,
    y aplicarlo de nuevo daria 960 sin que nada avisara.
    """
    millilitros, _ = glaze_volume_ml(Decimal(1500), Decimal("0.8"))
    assert millilitros == Decimal(1200)
    assert millilitros != Decimal(1500) * Decimal("0.8") * Decimal("0.8")
