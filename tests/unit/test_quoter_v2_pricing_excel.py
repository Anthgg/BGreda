"""Fase 010F — el motor economico conciliado contra el Excel aprobado.

No son casos inventados para que el codigo pase: cada cifra esperada es la que
`Cotizador_Greda_V2_modelo.xlsx` tiene cacheada en su propia celda. El fixture
`tests/fixtures/excel_v2_modelo.py` explica de donde sale cada una.

## Que se concilia, y en que tres bloques

1. **el reparto de los costos generales.** El Excel reparte la quema por
   VOLUMEN, el espacio por HORAS y la administracion por COSTO DIRECTO. Estas
   pruebas comprueban las tres bases contra sus celdas;
2. **los totales de la cotizacion.** Costo real, costo de produccion, x2, x3,
   ganancia y margen;
3. **la cadena comercial.** Precio de la linea, unitario, redondeo hacia
   arriba, subtotal reconstruido, IGV y total.

## La unica divergencia, y esta acotada

El Excel lleva la ilustracion por PRODUCTO; el sistema, por COTIZACION, porque
asi lo fijo la regla aprobada de 010D. Eso mueve S/44 entre lineas y **no
cambia ningun total**. Las pruebas lo demuestran en los dos sentidos: los
globales salen identicos, y alimentando el reparto con el costo directo del
Excel —ilustracion incluida en su linea— el sistema reproduce celda a celda los
costos asignados de la hoja.

Dicho de otro modo: la maquinaria es la misma; lo que cambia es un dato de
entrada, y se cambia porque una regla aprobada lo manda.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.quoter_v2_pricing import (
    allocate_by_weight,
    apply_factor,
    ceil_to_step,
    margin_percent,
    share,
    tax_amount,
    unit_price,
)
from tests.fixtures.excel_v2_modelo import (
    ADMIN_PER_QUOTE,
    COMMERCIAL_FACTOR,
    EFFECTIVE_WORK_DAYS,
    EXCEL_SHEETS,
    EXCEL_TOLERANCE,
    FACTOR_MAX,
    FACTOR_MIN,
    LINES,
    ROUNDING_STEP,
    SPACE_PER_DAY,
    TAX_PERCENT,
    TOTAL_LABOR_HOURS,
    TOTAL_VOLUME_CM3,
    TOTALS,
)

CANTIDADES = [linea["quantity"] for linea in LINES]
VOLUMENES = [linea["total_volume_cm3"] for linea in LINES]
MATERIALES = [linea["materials_cost"] for linea in LINES]
MANO_DE_OBRA = [linea["labor_cost"] for linea in LINES]
HORAS = [linea["labor_hours"] for linea in LINES]
#: El costo directo TAL Y COMO LO ARMA EL EXCEL: con la ilustracion dentro de
#: la linea que la lleva. Es el dato de entrada que hace comparables los
#: repartos por linea.
DIRECTO_EXCEL = [
    linea["materials_cost"] + linea["labor_cost"] + linea["illustration_cost"] for linea in LINES
]


def casi_igual(actual: Decimal, esperado: Decimal) -> bool:
    """Si dos numeros son el mismo numero, con distinta representacion.

    El Excel calcula en coma flotante de doble precision y el sistema en
    `Decimal`: las ultimas cifras no tienen por que coincidir y exigir
    igualdad exacta convertiria la prueba en una comparacion de formatos.
    """
    return abs(actual - esperado) <= EXCEL_TOLERANCE


# ---------------------------------------------------------------------------
# El Excel existe y es el que se reviso
# ---------------------------------------------------------------------------
def test_el_libro_tiene_las_ocho_hojas_que_se_revisaron() -> None:
    """Deja escrito que se inventario el libro entero, no solo una hoja."""
    assert len(EXCEL_SHEETS) == 8
    for hoja in ("Configuración", "Cotizador V2", "Quema V2", "Productos", "Reglas"):
        assert hoja in EXCEL_SHEETS


def test_la_configuracion_del_excel_es_la_aprobada() -> None:
    """Los numeros que gobiernan la fase, tal y como estan en la hoja."""
    assert TAX_PERCENT == Decimal(18)
    assert ROUNDING_STEP == Decimal("0.5")
    assert (FACTOR_MIN, COMMERCIAL_FACTOR, FACTOR_MAX) == (Decimal(2), Decimal(3), Decimal(3))
    assert SPACE_PER_DAY == Decimal(140)
    assert ADMIN_PER_QUOTE == Decimal(200)


# ---------------------------------------------------------------------------
# 1. Las tres bases de reparto
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("indice", range(len(LINES)))
def test_la_participacion_por_volumen_coincide_con_el_excel(indice: int) -> None:
    """El horno se reparte por sitio ocupado. Columna AA de la hoja Productos."""
    actual = share(VOLUMENES[indice], TOTAL_VOLUME_CM3)
    assert casi_igual(actual, LINES[indice]["volume_share"])


@pytest.mark.parametrize("indice", range(len(LINES)))
def test_la_participacion_por_horas_coincide_con_el_excel(indice: int) -> None:
    """El taller se ocupa por tiempo. Columna AB."""
    actual = share(HORAS[indice], TOTAL_LABOR_HOURS)
    assert casi_igual(actual, LINES[indice]["hours_share"])


def test_la_quema_se_reparte_por_volumen_como_en_el_excel() -> None:
    """Columna AD: participacion en volumen por la tarifa comercial de quema."""
    partes = allocate_by_weight(TOTALS["firing_commercial"], VOLUMENES)
    for parte, linea in zip(partes, LINES, strict=True):
        assert casi_igual(parte, linea["firing_allocated"])
    assert sum(partes) == TOTALS["firing_commercial"]


def test_el_espacio_se_reparte_por_horas_como_en_el_excel() -> None:
    """Columna AE: participacion en horas por el costo del espacio."""
    partes = allocate_by_weight(TOTALS["space"], HORAS)
    for parte, linea in zip(partes, LINES, strict=True):
        assert casi_igual(parte, linea["space_allocated"])
    assert sum(partes) == TOTALS["space"]


def test_el_espacio_sale_de_los_dias_efectivos() -> None:
    """4 dias x S/140. Por dias EFECTIVOS de taller, no por vigencia."""
    assert Decimal(EFFECTIVE_WORK_DAYS) * SPACE_PER_DAY == TOTALS["space"]


# ---------------------------------------------------------------------------
# 2. Los costos asignados por linea
# ---------------------------------------------------------------------------
def test_el_costo_de_produccion_asignado_reproduce_la_hoja() -> None:
    """Columna AG, celda a celda.

    Se alimenta con el costo directo DEL EXCEL —con la ilustracion dentro de su
    linea— porque es justo lo que esta prueba quiere aislar: dado el mismo
    reparto de entrada, la maquinaria del sistema y la de la hoja dan lo mismo.
    """
    quemas = allocate_by_weight(TOTALS["firing_commercial"], VOLUMENES)
    espacios = allocate_by_weight(TOTALS["space"], HORAS)
    generales = allocate_by_weight(TOTALS["administration"], DIRECTO_EXCEL)

    for indice, linea in enumerate(LINES):
        asignado = DIRECTO_EXCEL[indice] + quemas[indice] + espacios[indice] + generales[indice]
        assert casi_igual(asignado, linea["production_allocated"]), linea["name"]


def test_el_costo_real_asignado_reproduce_la_hoja() -> None:
    """Columna AH: lo mismo pero con el GAS en vez de la tarifa de quema."""
    gases = allocate_by_weight(TOTALS["firing_gas"], VOLUMENES)
    espacios = allocate_by_weight(TOTALS["space"], HORAS)
    generales = allocate_by_weight(TOTALS["administration"], DIRECTO_EXCEL)

    for indice, linea in enumerate(LINES):
        asignado = DIRECTO_EXCEL[indice] + gases[indice] + espacios[indice] + generales[indice]
        assert casi_igual(asignado, linea["real_allocated"]), linea["name"]


def test_lo_repartido_suma_exactamente_el_costo_de_produccion() -> None:
    """Es la integridad que pide la fase: ni un centimo fuera del total."""
    quemas = allocate_by_weight(TOTALS["firing_commercial"], VOLUMENES)
    espacios = allocate_by_weight(TOTALS["space"], HORAS)
    generales = allocate_by_weight(TOTALS["administration"], DIRECTO_EXCEL)

    total = sum(DIRECTO_EXCEL) + sum(quemas) + sum(espacios) + sum(generales)
    assert casi_igual(total, TOTALS["production_cost"])


def test_la_ilustracion_cambia_de_linea_pero_no_el_total() -> None:
    """La divergencia declarada, medida.

    Con la ilustracion como costo general —lo que hace el sistema— el reparto
    por linea es otro, y el COSTO DE PRODUCCION de la cotizacion es exactamente
    el mismo. Esta prueba existe para que esa afirmacion no sea una nota al pie.
    """
    directo_sistema = [m + mo for m, mo in zip(MATERIALES, MANO_DE_OBRA, strict=True)]
    general_sistema = TOTALS["administration"] + TOTALS["illustration"]

    quemas = allocate_by_weight(TOTALS["firing_commercial"], VOLUMENES)
    espacios = allocate_by_weight(TOTALS["space"], HORAS)
    generales = allocate_by_weight(general_sistema, directo_sistema)

    asignados = [
        directo_sistema[i] + quemas[i] + espacios[i] + generales[i] for i in range(len(LINES))
    ]
    # El total es el mismo...
    assert casi_igual(sum(asignados), TOTALS["production_cost"])
    # ...y la primera linea NO recibe lo mismo que en la hoja, porque alli se
    # lleva los S/44 enteros y aqui solo la parte que le toca.
    assert not casi_igual(asignados[0], LINES[0]["production_allocated"])


# ---------------------------------------------------------------------------
# 3. Los totales de la cotizacion
# ---------------------------------------------------------------------------
def test_los_componentes_globales_coinciden_con_el_excel() -> None:
    assert sum(MATERIALES) == TOTALS["materials"]
    assert casi_igual(sum(MANO_DE_OBRA), TOTALS["labor"])
    assert sum(VOLUMENES) == TOTAL_VOLUME_CM3
    assert casi_igual(sum(HORAS), TOTAL_LABOR_HOURS)


def test_el_costo_de_produccion_global() -> None:
    """Materiales + mano de obra + ilustracion + QUEMA COMERCIAL + espacio + admin."""
    total = (
        TOTALS["materials"]
        + TOTALS["labor"]
        + TOTALS["illustration"]
        + TOTALS["firing_commercial"]
        + TOTALS["space"]
        + TOTALS["administration"]
        + TOTALS["extras"]
    )
    assert casi_igual(total, TOTALS["production_cost"])


def test_el_costo_real_global_lleva_el_gas_y_no_la_tarifa() -> None:
    """La diferencia entre las dos bases es exactamente la de la quema.

    Si alguien intercambiara los dos componentes, los dos totales seguirian
    siendo numeros creibles y el margen quedaria invertido. De ahi la
    comprobacion cruzada.
    """
    total = (
        TOTALS["materials"]
        + TOTALS["labor"]
        + TOTALS["illustration"]
        + TOTALS["firing_gas"]
        + TOTALS["space"]
        + TOTALS["administration"]
        + TOTALS["extras"]
    )
    assert casi_igual(total, TOTALS["real_cost"])
    assert casi_igual(TOTALS["production_cost"] - TOTALS["real_cost"], TOTALS["firing_difference"])


def test_el_precio_minimo_es_el_costo_por_dos() -> None:
    """Multiplicador, no un +200 % anadido encima."""
    assert casi_igual(apply_factor(TOTALS["production_cost"], FACTOR_MIN), TOTALS["price_min_x2"])


def test_el_precio_objetivo_es_el_costo_por_tres() -> None:
    assert casi_igual(
        apply_factor(TOTALS["production_cost"], FACTOR_MAX), TOTALS["price_target_x3"]
    )


# ---------------------------------------------------------------------------
# 4. La cadena comercial
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("indice", range(len(LINES)))
def test_el_precio_unitario_sin_redondear_coincide(indice: int) -> None:
    """Columna AJ: costo asignado x factor, entre la cantidad."""
    linea = LINES[indice]
    precio = apply_factor(linea["production_allocated"], COMMERCIAL_FACTOR)
    crudo = unit_price(precio, linea["quantity"], None)
    assert casi_igual(crudo, linea["unit_price_raw"])


@pytest.mark.parametrize("indice", range(len(LINES)))
def test_el_redondeo_comercial_coincide(indice: int) -> None:
    """Columna AK: ROUNDUP al multiplo de 0,50. 181,58 sube a 182."""
    linea = LINES[indice]
    assert ceil_to_step(linea["unit_price_raw"], ROUNDING_STEP) == linea["unit_price"]


@pytest.mark.parametrize("indice", range(len(LINES)))
def test_el_subtotal_de_la_linea_se_reconstruye(indice: int) -> None:
    """Columna AL: unitario REDONDEADO por la cantidad.

    No el precio de la linea antes de redondear: si se tomara aquel, el
    documento no cuadraria al sumarlo a mano.
    """
    linea = LINES[indice]
    assert linea["unit_price"] * Decimal(linea["quantity"]) == linea["line_subtotal"]


@pytest.mark.parametrize("indice", range(len(LINES)))
def test_el_igv_de_la_linea_coincide(indice: int) -> None:
    linea = LINES[indice]
    assert casi_igual(tax_amount(linea["line_subtotal"], TAX_PERCENT), linea["line_tax"])


def test_el_subtotal_el_igv_y_el_total_de_la_cotizacion() -> None:
    """Los tres numeros que iran al documento del cliente."""
    subtotal = sum(linea["line_subtotal"] for linea in LINES)
    impuesto = tax_amount(subtotal, TAX_PERCENT)
    assert subtotal == TOTALS["subtotal"]
    assert casi_igual(impuesto, TOTALS["tax"])
    assert casi_igual(subtotal + impuesto, TOTALS["total"])


def test_la_cadena_completa_desde_los_costos_asignados_del_excel() -> None:
    """De la columna AG al total, en una sola prueba.

    Es la conciliacion que mas importa: si algun eslabon —factor, division,
    redondeo, reconstruccion o IGV— estuviera en otro orden, este numero no
    saldria.
    """
    subtotal = Decimal(0)
    for linea in LINES:
        precio = apply_factor(linea["production_allocated"], COMMERCIAL_FACTOR)
        crudo = unit_price(precio, linea["quantity"], None)
        unitario = ceil_to_step(crudo, ROUNDING_STEP)
        subtotal += unitario * Decimal(linea["quantity"])

    impuesto = tax_amount(subtotal, TAX_PERCENT)
    assert subtotal == TOTALS["subtotal"]
    assert casi_igual(impuesto, TOTALS["tax"])
    assert casi_igual(subtotal + impuesto, TOTALS["total"])


def test_el_ajuste_por_redondeo_coincide() -> None:
    """Lo que el redondeo anadio sobre el precio objetivo: S/17,72.

    Se expone porque, sin este numero, nadie puede explicar por que el subtotal
    no es exactamente costo x factor.
    """
    assert casi_igual(TOTALS["subtotal"] - TOTALS["price_target_x3"], TOTALS["rounding_adjustment"])


# ---------------------------------------------------------------------------
# 5. Lo que deja
# ---------------------------------------------------------------------------
def test_la_ganancia_estimada_coincide() -> None:
    """Precio comercial SIN IGV menos costo REAL.

    Sin IGV porque el impuesto no es ingreso del taller, y contra el costo REAL
    porque es lo que de verdad sale del bolsillo.
    """
    assert casi_igual(TOTALS["subtotal"] - TOTALS["real_cost"], TOTALS["estimated_profit"])


def test_el_margen_efectivo_coincide() -> None:
    """Sobre el PRECIO, no sobre el costo. El Excel lo guarda como fraccion."""
    actual = margin_percent(TOTALS["estimated_profit"], TOTALS["subtotal"])
    assert casi_igual(actual / Decimal(100), TOTALS["effective_margin"])


def test_el_igv_no_entra_en_la_ganancia() -> None:
    """La comprobacion escrita contra el error, no contra el acierto.

    Contar el IGV como ingreso inflaria la ganancia en S/1.384,74 y el numero
    seguiria pareciendo razonable.
    """
    inflada = TOTALS["total"] - TOTALS["real_cost"]
    assert not casi_igual(inflada, TOTALS["estimated_profit"])
    assert casi_igual(inflada - TOTALS["estimated_profit"], TOTALS["tax"])
