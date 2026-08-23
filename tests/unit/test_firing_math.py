"""Pruebas de la matematica del costo de quema.

El caso de referencia no es un numero inventado para que la prueba pase: son
las celdas de la hoja «Costo de quema» del documento funcional. Si algun dia el
calculo deja de reproducirlas, esta prueba tiene que ponerse roja.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.firings import (
    ALLOWED_BRACKETS,
    FiringDimensionError,
    FiringEmptyError,
    LineInput,
    OccupancyFactorMissingError,
    SessionInput,
    compute_firing,
    line_volume,
    occupancy_bracket,
    physical_occupancy_percentage,
    resolve_factor,
    volume_share,
)

CHICO = 1
GRANDE = 2

#: Transcripcion de ``T15:V25``. Cada horno tiene su propia curva.
FACTORES_CHICO = [
    (1, 10, Decimal("2.0")),
    (11, 20, Decimal("1.9")),
    (21, 30, Decimal("1.8")),
    (31, 40, Decimal("1.7")),
    (41, 50, Decimal("1.6")),
    (51, 60, Decimal("1.4")),
    (61, 70, Decimal("1.3")),
    (71, 80, Decimal("1.2")),
    (81, 90, Decimal("1.1")),
    (91, 100, Decimal("1.0")),
]
FACTORES_GRANDE = [
    (1, 10, Decimal("3.0")),
    (11, 20, Decimal("2.8")),
    (21, 30, Decimal("2.6")),
    (31, 40, Decimal("2.3")),
    (41, 50, Decimal("2.1")),
    (51, 60, Decimal("1.9")),
    (61, 70, Decimal("1.7")),
    (71, 80, Decimal("1.4")),
    (81, 90, Decimal("1.2")),
    (91, 100, Decimal("1.0")),
]
TABLAS = {CHICO: FACTORES_CHICO, GRANDE: FACTORES_GRANDE}

SESIONES_REFERENCIA = [
    SessionInput("1:LOW", CHICO, "LOW", Decimal("90"), Decimal("17000")),
    SessionInput("1:HIGH", CHICO, "HIGH", Decimal("180"), Decimal("17000")),
    SessionInput("2:HIGH", GRANDE, "HIGH", Decimal("2000"), Decimal("200000")),
]

#: Las tres piezas de la hoja, con el horno que hace cada quema y el horno cuya
#: capacidad decide el tramo.
LINEAS_REFERENCIA = [
    LineInput(20, Decimal(18), Decimal(12), Decimal(3), ("1:LOW", "2:HIGH"), CHICO),
    LineInput(50, Decimal(1), Decimal(15), Decimal(3), ("1:LOW", "1:HIGH"), GRANDE),
    LineInput(12, Decimal(15), Decimal(12), Decimal(5), ("1:LOW", "1:HIGH"), GRANDE),
]


# ---------------------------------------------------------------------------
# Volumen
# ---------------------------------------------------------------------------
def test_volumen_unitario_y_total() -> None:
    unitario, total = line_volume(20, Decimal(18), Decimal(12), Decimal(3))
    assert unitario == Decimal(648)
    assert total == Decimal(12960)


def test_volumen_rechaza_cantidad_no_positiva() -> None:
    with pytest.raises(FiringDimensionError):
        line_volume(0, Decimal(1), Decimal(1), Decimal(1))


@pytest.mark.parametrize(
    "largo,ancho,alto",
    [(Decimal(0), Decimal(1), Decimal(1)), (Decimal(1), Decimal(-1), Decimal(1))],
)
def test_volumen_rechaza_dimensiones_no_positivas(
    largo: Decimal, ancho: Decimal, alto: Decimal
) -> None:
    with pytest.raises(FiringDimensionError):
        line_volume(1, largo, ancho, alto)


def test_volumen_conserva_decimales_sin_float() -> None:
    """Una dimension con decimales no se redondea antes de multiplicar."""
    unitario, total = line_volume(3, Decimal("1.5"), Decimal("2.5"), Decimal("0.1"))
    assert unitario == Decimal("0.375")
    assert total == Decimal("1.125")
    assert isinstance(total, Decimal)


# ---------------------------------------------------------------------------
# Ocupacion y tramos
# ---------------------------------------------------------------------------
def test_ocupacion_fisica_es_exacta() -> None:
    porcentaje = physical_occupancy_percentage(Decimal(12960), Decimal(17000))
    # 12960 / 17000 * 100, sin redondear: la hoja muestra 76.23529412.
    assert porcentaje.quantize(Decimal("0.00000001")) == Decimal("76.23529412")


@pytest.mark.parametrize(
    "porcentaje,tramo",
    [
        ("0.5", 10),
        ("1.125", 10),
        ("5.4", 10),
        ("10", 10),
        ("10.0001", 20),
        ("20", 20),
        ("76.235294", 80),
        ("80", 80),
        ("90.1", 100),
        ("100", 100),
    ],
)
def test_tramo_es_la_decena_superior(porcentaje: str, tramo: int) -> None:
    assert occupancy_bracket(Decimal(porcentaje)) == tramo


def test_tramo_no_pasa_de_cien() -> None:
    """Por encima del 100 % la tabla del negocio no continua."""
    assert occupancy_bracket(Decimal("153.4")) == 100


def test_tramos_permitidos_son_las_diez_decenas() -> None:
    assert ALLOWED_BRACKETS == (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)


def test_factor_sin_tramo_configurado_falla() -> None:
    with pytest.raises(OccupancyFactorMissingError):
        resolve_factor([(1, 10, Decimal("2.0"))], 80)


def test_factor_por_horno_difiere_en_el_mismo_tramo() -> None:
    """La tabla tiene una columna por horno: el tramo solo no basta."""
    assert resolve_factor(FACTORES_CHICO, 80) == Decimal("1.2")
    assert resolve_factor(FACTORES_GRANDE, 80) == Decimal("1.4")


# ---------------------------------------------------------------------------
# Reparto
# ---------------------------------------------------------------------------
def test_reparto_es_proporcional_al_volumen_no_a_la_cantidad() -> None:
    """Dos piezas grandes no pagan lo mismo que veinte pequenas del mismo volumen."""
    share = volume_share(Decimal(12960), Decimal(26010))
    assert share == Decimal(12960) / Decimal(26010)


def test_reparto_sin_volumen_total_falla() -> None:
    with pytest.raises(FiringEmptyError):
        volume_share(Decimal(10), Decimal(0))


def test_las_participaciones_suman_uno() -> None:
    resultado = compute_firing(SESIONES_REFERENCIA, LINEAS_REFERENCIA, TABLAS)
    assert sum(linea.share for linea in resultado.lines) == Decimal(1)


def test_hoja_vacia_falla() -> None:
    with pytest.raises(FiringEmptyError):
        compute_firing(SESIONES_REFERENCIA, [], TABLAS)
    with pytest.raises(FiringEmptyError):
        compute_firing([], LINEAS_REFERENCIA, TABLAS)


# ---------------------------------------------------------------------------
# Caso de referencia del documento funcional (§18)
# ---------------------------------------------------------------------------
def test_caso_de_referencia_reproduce_la_hoja() -> None:
    """Hoja «Costo de quema», fila 14: la pieza «Plato palta».

    - ``G14`` volumen de la linea: 12960 cm3
    - ``G19`` volumen total: 26010 cm3
    - ``Q14 = H14 + K14`` (chico baja + grande alta): 1041.384083
    - ``L14`` ocupacion contra el horno chico: 76.235 % -> tramo 71-80 %
    - ``V23`` factor del horno chico en ese tramo: 1.2
    - ``R14 = Q14 * V23``: 1249.6609
    """
    resultado = compute_firing(SESIONES_REFERENCIA, LINEAS_REFERENCIA, TABLAS)
    palta = resultado.lines[0]

    assert resultado.total_volume_cm3 == Decimal(26010)
    assert palta.total_volume_cm3 == Decimal(12960)
    assert palta.base_cost.quantize(Decimal("0.01")) == Decimal("1041.38")
    assert palta.occupancy_bracket == 80
    assert palta.occupancy_factor == Decimal("1.2")
    assert palta.allocated_cost.quantize(Decimal("0.01")) == Decimal("1249.66")


def test_caso_de_referencia_reproduce_tambien_las_otras_dos_piezas() -> None:
    """Filas 15 y 16: mismo mecanismo, factor del horno grande al 1-10 %."""
    resultado = compute_firing(SESIONES_REFERENCIA, LINEAS_REFERENCIA, TABLAS)
    buho, platos = resultado.lines[1], resultado.lines[2]

    assert buho.occupancy_bracket == 10
    assert buho.occupancy_factor == Decimal("3.0")
    assert buho.allocated_cost.quantize(Decimal("0.0001")) == Decimal("70.0692")

    assert platos.occupancy_factor == Decimal("3.0")
    assert platos.allocated_cost.quantize(Decimal("0.0001")) == Decimal("336.3322")


def test_el_costo_base_de_una_sesion_se_reparte_entero() -> None:
    """La suma de lo repartido de una sesion es su tarifa, ni mas ni menos."""
    resultado = compute_firing(SESIONES_REFERENCIA, LINEAS_REFERENCIA, TABLAS)
    por_clave = {sesion.key: sesion for sesion in resultado.sessions}

    # Las tres piezas pasan por la quema baja del horno chico: absorben los 90.
    assert por_clave["1:LOW"].subtotal == Decimal(90)
    # Solo la palta usa la quema alta del horno grande, y absorbe su parte.
    esperado = Decimal(12960) / Decimal(26010) * Decimal(2000)
    assert por_clave["2:HIGH"].subtotal == esperado


def test_factor_efectivo_de_la_hoja_es_el_ponderado() -> None:
    resultado = compute_firing(SESIONES_REFERENCIA, LINEAS_REFERENCIA, TABLAS)
    assert resultado.occupancy_factor == resultado.total_cost / resultado.subtotal


# ---------------------------------------------------------------------------
# Capacidad
# ---------------------------------------------------------------------------
def test_capacidad_exacta_al_cien_por_cien_no_se_marca_excedida() -> None:
    sesiones = [SessionInput("1:LOW", CHICO, "LOW", Decimal("90"), Decimal("1000"))]
    lineas = [LineInput(1, Decimal(10), Decimal(10), Decimal(10), ("1:LOW",), CHICO)]
    resultado = compute_firing(sesiones, lineas, TABLAS)

    assert resultado.lines[0].occupancy_percentage == Decimal(100)
    assert resultado.lines[0].occupancy_bracket == 100
    assert resultado.lines[0].occupancy_factor == Decimal("1.0")
    assert resultado.capacity_exceeded is False


def test_pasar_del_cien_por_cien_se_marca_excedido() -> None:
    sesiones = [SessionInput("1:LOW", CHICO, "LOW", Decimal("90"), Decimal("999"))]
    lineas = [LineInput(1, Decimal(10), Decimal(10), Decimal(10), ("1:LOW",), CHICO)]
    resultado = compute_firing(sesiones, lineas, TABLAS)

    assert resultado.lines[0].capacity_exceeded is True
    assert resultado.capacity_exceeded is True


# ---------------------------------------------------------------------------
# Precision
# ---------------------------------------------------------------------------
def test_todo_el_resultado_es_decimal_y_ningun_float() -> None:
    resultado = compute_firing(SESIONES_REFERENCIA, LINEAS_REFERENCIA, TABLAS)
    valores = [
        resultado.total_volume_cm3,
        resultado.subtotal,
        resultado.total_cost,
        resultado.occupancy_percentage,
        resultado.occupancy_factor,
        *(linea.base_cost for linea in resultado.lines),
        *(linea.allocated_cost for linea in resultado.lines),
        *(sesion.subtotal for sesion in resultado.sessions),
    ]
    assert all(isinstance(valor, Decimal) for valor in valores)
    assert not any(isinstance(valor, float) for valor in valores)


def test_el_horno_del_factor_por_omision_es_el_de_la_primera_sesion() -> None:
    lineas = [LineInput(1, Decimal(10), Decimal(10), Decimal(10), ("2:HIGH",), None)]
    resultado = compute_firing(SESIONES_REFERENCIA, lineas, TABLAS)
    assert resultado.lines[0].factor_kiln_id == GRANDE
