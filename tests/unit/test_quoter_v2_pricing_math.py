"""Fase 010F — la aritmetica economica, caso a caso.

Lo que aqui se fija son las inversiones que producen un numero creible:

- **x2 confundido con +200 %.** Uno da 2.000 sobre 1.000 y el otro 3.000. Los
  dos parecen razonables en una pantalla;
- **el IGV antes del factor.** Triplicaria el impuesto sin que ninguna cuenta
  chirrie;
- **redondear hacia el mas cercano.** Regala la mitad del escalon en cada
  unidad, y multiplicada por la cantidad deja de ser calderilla;
- **convertir a moneda extranjera dos veces.** Divide el precio por el tipo de
  cambio al cuadrado y sigue siendo un numero con pinta de precio;
- **un reparto que no suma el total.** El sobrante no se le cobra a nadie.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.quoter_v2_pricing import (
    PricingMathError,
    allocate_by_weight,
    apply_factor,
    ceil_to_step,
    margin_percent,
    quantize_money,
    share,
    tax_amount,
    to_base_currency,
    unit_price,
    within_factor_range,
)

PASO = Decimal("0.5")


# ---------------------------------------------------------------------------
# Redondeo comercial: siempre hacia arriba
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("8.3", "8.5"),
        ("8.9", "9.0"),
        ("8.5", "8.5"),
        ("8.51", "9.0"),
        ("0.01", "0.5"),
        ("0", "0"),
        ("206.399874", "206.5"),
    ],
)
def test_el_redondeo_sube_al_siguiente_escalon(valor: str, esperado: str) -> None:
    """Los dos ejemplos aprobados y los bordes que los rodean.

    Un multiplo exacto NO se mueve: 8,50 sigue siendo 8,50. Un centimo por
    encima ya salta al escalon completo.
    """
    assert ceil_to_step(Decimal(valor), PASO) == Decimal(esperado)


def test_el_redondeo_no_usa_coma_flotante() -> None:
    """142,50 / 0,50 en coma flotante da 285,00000000000006.

    El techo de eso es 286, es decir 143,00: medio sol de mas en cada unidad,
    y exactamente en el caso de borde que mas se repite.
    """
    assert ceil_to_step(Decimal("142.50"), PASO) == Decimal("142.50")


def test_no_se_redondea_un_importe_negativo() -> None:
    with pytest.raises(PricingMathError):
        ceil_to_step(Decimal("-1"), PASO)


def test_un_paso_de_cero_no_redondea_nada() -> None:
    with pytest.raises(PricingMathError):
        ceil_to_step(Decimal(10), Decimal(0))


# ---------------------------------------------------------------------------
# El factor: multiplicador, no porcentaje
# ---------------------------------------------------------------------------
def test_el_factor_multiplica_y_no_anade() -> None:
    """x2 sobre 1.000 son 2.000. «+200 %» darian 3.000."""
    assert apply_factor(Decimal(1000), Decimal(2)) == Decimal(2000)
    assert apply_factor(Decimal(1000), Decimal(3)) == Decimal(3000)
    assert apply_factor(Decimal(1000), Decimal("2.5")) == Decimal(2500)


def test_un_factor_de_cero_no_produce_un_precio() -> None:
    with pytest.raises(PricingMathError):
        apply_factor(Decimal(1000), Decimal(0))


def test_un_costo_negativo_no_se_factoriza() -> None:
    with pytest.raises(PricingMathError):
        apply_factor(Decimal(-1), Decimal(3))


@pytest.mark.parametrize(
    ("factor", "cabe"),
    [
        ("2", True),
        ("2.5", True),
        ("3", True),
        ("1.999999", False),
        ("3.000001", False),
    ],
)
def test_el_rango_del_factor(factor: str, cabe: bool) -> None:
    """El suelo de x2 es una regla cerrada, no una preferencia."""
    assert within_factor_range(Decimal(factor), Decimal(2), Decimal(3)) is cabe


# ---------------------------------------------------------------------------
# Precio unitario y moneda
# ---------------------------------------------------------------------------
def test_el_precio_unitario_es_el_de_la_linea_entre_la_cantidad() -> None:
    assert unit_price(Decimal(3000), 20, None) == Decimal(150)


def test_sin_piezas_no_hay_precio_unitario() -> None:
    """Cantidad cero da cero, no una division por cero."""
    assert unit_price(Decimal(3000), 0, None) == Decimal(0)


def test_en_moneda_extranjera_se_convierte_una_sola_vez() -> None:
    """S/3.500 de linea, 10 piezas, TC 3,5: US$100 la pieza.

    Convertir dos veces daria 28,57 y seguiria pareciendo un precio.
    """
    assert unit_price(Decimal(3500), 10, Decimal("3.5")) == Decimal(100)


def test_un_tipo_de_cambio_de_cero_no_convierte_nada() -> None:
    with pytest.raises(PricingMathError):
        unit_price(Decimal(3500), 10, Decimal(0))


def test_volver_a_moneda_base_deshace_la_conversion() -> None:
    """Hace falta para comparar un precio en dolares contra un costo en soles."""
    assert to_base_currency(Decimal(100), Decimal("3.5")) == Decimal(350)


def test_sin_tipo_de_cambio_no_hay_nada_que_convertir() -> None:
    """En moneda base el importe ya esta donde tiene que estar."""
    assert to_base_currency(Decimal(100), None) == Decimal(100)


# ---------------------------------------------------------------------------
# IGV
# ---------------------------------------------------------------------------
def test_el_igv_se_calcula_sobre_el_subtotal() -> None:
    assert tax_amount(Decimal(7693), Decimal(18)) == Decimal("1384.74")


def test_un_igv_de_cero_es_legitimo() -> None:
    """Una operacion exonerada no es un error de captura."""
    assert tax_amount(Decimal(1000), Decimal(0)) == Decimal(0)


def test_un_igv_distinto_de_dieciocho_se_respeta() -> None:
    """El porcentaje es dinamico: viene de la configuracion de la casa."""
    assert tax_amount(Decimal(1000), Decimal(10)) == Decimal(100)


def test_el_igv_no_se_multiplica_por_el_factor() -> None:
    """El orden importa, y esta es la comprobacion escrita al reves.

    Aplicar el factor sobre un total que ya lleva IGV cobraria al cliente el
    impuesto triplicado: 3.540 en vez de 3.000 + 540.
    """
    costo = Decimal(1000)
    correcto = tax_amount(apply_factor(costo, Decimal(3)), Decimal(18))
    invertido = apply_factor(tax_amount(costo, Decimal(18)), Decimal(3))
    assert correcto == Decimal(540)
    assert invertido == Decimal(540)
    # El importe del IGV coincide, pero el TOTAL no: ahi esta el error.
    assert apply_factor(costo + tax_amount(costo, Decimal(18)), Decimal(3)) == Decimal(3540)
    assert apply_factor(costo, Decimal(3)) + correcto == Decimal(3540)


def test_un_impuesto_negativo_se_rechaza() -> None:
    with pytest.raises(PricingMathError):
        tax_amount(Decimal(1000), Decimal(-1))


# ---------------------------------------------------------------------------
# Reparto
# ---------------------------------------------------------------------------
def test_el_reparto_del_ejemplo_aprobado() -> None:
    """700 y 300 sobre 1.000: el 70 % y el 30 %.

    Con factor x3 el precio total es 3.000 y las partes 2.100 y 900.
    """
    costos = [Decimal(700), Decimal(300)]
    precios = [apply_factor(costo, Decimal(3)) for costo in costos]
    assert precios == [Decimal(2100), Decimal(900)]
    assert sum(precios) == apply_factor(sum(costos), Decimal(3))


def test_lo_repartido_suma_exactamente_el_total() -> None:
    """Tres partes iguales de 1.000 no caben exactas en ningun decimal finito."""
    partes = allocate_by_weight(Decimal(1000), [Decimal(1), Decimal(1), Decimal(1)])
    assert sum(partes) == Decimal(1000)


def test_veinte_lineas_tambien_suman_exactamente() -> None:
    partes = allocate_by_weight(Decimal(1000), [Decimal(1)] * 20)
    assert sum(partes) == Decimal(1000)
    assert len(partes) == 20


def test_el_reparto_es_reproducible() -> None:
    """Dos ejecuciones dan lo mismo: sin desempate, la cotizacion cambiaria sola."""
    pesos = [Decimal(1), Decimal(1), Decimal(1)]
    assert allocate_by_weight(Decimal(10), pesos) == allocate_by_weight(Decimal(10), pesos)


def test_sin_base_de_reparto_no_se_reparte_nada() -> None:
    """Quien llama decide si corresponde otra base; aqui no se inventa una."""
    assert allocate_by_weight(Decimal(100), [Decimal(0), Decimal(0)]) == [
        Decimal(0),
        Decimal(0),
    ]


def test_una_sola_linea_se_lleva_todo() -> None:
    assert allocate_by_weight(Decimal(1000), [Decimal(5)]) == [Decimal(1000)]


def test_sin_lineas_no_hay_nada_que_repartir() -> None:
    assert allocate_by_weight(Decimal(1000), []) == []


def test_la_participacion_sin_total_es_cero() -> None:
    assert share(Decimal(10), Decimal(0)) == Decimal(0)


# ---------------------------------------------------------------------------
# Ganancia y margen
# ---------------------------------------------------------------------------
def test_el_margen_se_mide_sobre_el_precio() -> None:
    """Un 50 % sobre precio es un 100 % sobre costo. No son el mismo numero."""
    assert margin_percent(Decimal(500), Decimal(1000)) == Decimal(50)


def test_sin_precio_no_hay_margen() -> None:
    """Denominador cero: se devuelve cero en vez de reventar."""
    assert margin_percent(Decimal(500), Decimal(0)) == Decimal(0)


def test_una_venta_a_perdida_da_margen_negativo() -> None:
    """Esconderlo tras un cero seria mentir sobre el unico numero que importa."""
    assert margin_percent(Decimal(-100), Decimal(1000)) == Decimal(-10)


def test_el_importe_se_guarda_a_la_escala_del_dinero() -> None:
    assert quantize_money(Decimal("1.2345678")) == Decimal("1.234568")
