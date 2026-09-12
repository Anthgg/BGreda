"""Fase 010D — la aritmetica de la mano de obra, con los ejemplos aprobados.

Cada numero sale de una regla cerrada del proyecto o del Excel «Cotizador Greda
V2». No son casos inventados para que el codigo pase: son los que el negocio ya
resolvio a mano.

Los errores que estas pruebas cazan no fallan solos. Cobrar una jornada entera
por tres horas de torno, invertir la division del rendimiento o sumar tres
jornadas a quien solo trabajo una producen todos un importe creible, y solo se
notan cuando el cliente compara dos presupuestos.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.quoter_v2_labor import (
    LaborMathError,
    exceeds_workday,
    hourly_rate,
    hours_required,
    labor_cost,
    minimum_work_days,
    units_per_hour,
)

JORNADA = Decimal(8)


# ---------------------------------------------------------------------------
# Tarifa por hora: el jornal entre la jornada
# ---------------------------------------------------------------------------
def test_tarifa_por_hora_del_ejemplo_aprobado() -> None:
    """S/120 en 8 horas son S/15 la hora."""
    assert hourly_rate(Decimal(120), JORNADA) == Decimal(15)


def test_tarifa_por_hora_de_ilustracion() -> None:
    """S/110 en 8 horas son S/13,75. Sale exacto, y tiene que seguir saliendo."""
    assert hourly_rate(Decimal(110), JORNADA) == Decimal("13.75")


def test_un_trabajador_interno_tambien_tiene_tarifa() -> None:
    """Tener sueldo no hace que su tiempo valga cero.

    Es la regla que mas veces se propone romper: «ya esta contratado, no
    cuesta». Cuesta, y no saber cuanto es la forma de descubrir que una linea
    de productos daba perdidas.
    """
    assert hourly_rate(Decimal(120), JORNADA) > Decimal(0)


def test_una_jornada_de_cero_horas_no_produce_tarifa() -> None:
    with pytest.raises(LaborMathError):
        hourly_rate(Decimal(120), Decimal(0))


def test_un_jornal_negativo_se_rechaza() -> None:
    with pytest.raises(LaborMathError):
        hourly_rate(Decimal(-1), JORNADA)


def test_un_jornal_de_cero_es_legitimo() -> None:
    """Distinto de negativo: puede haber colaboracion sin costo declarado."""
    assert hourly_rate(Decimal(0), JORNADA) == Decimal(0)


# ---------------------------------------------------------------------------
# Rendimiento estandar
# ---------------------------------------------------------------------------
def test_rendimiento_por_hora_del_ejemplo_aprobado() -> None:
    """50 piezas por jornada de 8 horas son 6,25 piezas por hora."""
    assert units_per_hour(Decimal(50), JORNADA) == Decimal("6.25")


def test_el_rendimiento_se_divide_no_se_multiplica() -> None:
    """Multiplicar daria 400 piezas por hora y 75 piezas saldrian en once minutos."""
    assert units_per_hour(Decimal(50), JORNADA) != Decimal(50) * JORNADA


@pytest.mark.parametrize("capacidad", [Decimal(0), Decimal(-10)])
def test_un_rendimiento_imposible_se_rechaza(capacidad: Decimal) -> None:
    with pytest.raises(LaborMathError):
        units_per_hour(capacidad, JORNADA)


# ---------------------------------------------------------------------------
# Horas requeridas
# ---------------------------------------------------------------------------
def test_horas_del_ejemplo_aprobado() -> None:
    """75 piezas a 50 por jornada de 8 horas son 12 horas."""
    assert hours_required(Decimal(75), Decimal(50), JORNADA) == Decimal(12)


def test_una_jornada_entera_de_trabajo_da_la_jornada() -> None:
    """50 piezas, que es justo lo que rinde una jornada, son 8 horas."""
    assert hours_required(Decimal(50), Decimal(50), JORNADA) == JORNADA


def test_una_pieza_no_cuesta_una_jornada() -> None:
    """El error de `ceil`: una pieza pediria un dia entero y costaria como tal."""
    horas = hours_required(Decimal(1), Decimal(50), JORNADA)

    assert horas == Decimal("0.16")
    assert horas < JORNADA


def test_cantidad_cero_da_cero_horas_y_no_un_error() -> None:
    """Una linea a medio llenar es un estado legitimo de un borrador."""
    assert hours_required(Decimal(0), Decimal(50), JORNADA) == Decimal(0)


def test_una_cantidad_negativa_se_rechaza() -> None:
    with pytest.raises(LaborMathError):
        hours_required(Decimal(-1), Decimal(50), JORNADA)


def test_una_cantidad_enorme_no_pierde_precision() -> None:
    """Decimal, no float: en binario el resultado dejaria de ser exacto."""
    assert hours_required(Decimal(1_000_000), Decimal(50), JORNADA) == Decimal(160_000)


def test_la_division_no_se_hace_dos_veces() -> None:
    """`cantidad x jornada / capacidad`, no `cantidad / (capacidad / jornada)`.

    Con capacidad 7 la division intermedia no es exacta, y hacerla primero
    arrastraria una cola de decimales a un resultado que debe ser limpio.
    """
    assert hours_required(Decimal(7), Decimal(7), JORNADA) == JORNADA


# ---------------------------------------------------------------------------
# Costo
# ---------------------------------------------------------------------------
def test_costo_del_ejemplo_aprobado() -> None:
    """12 horas a S/13,75 son S/165."""
    assert labor_cost(Decimal(12), Decimal("13.75")) == Decimal(165)


def test_costo_de_cinco_horas_a_quince() -> None:
    """El ejemplo del enunciado: 5 h x S/15 = S/75."""
    assert labor_cost(Decimal(5), Decimal(15)) == Decimal(75)


def test_tres_horas_cuestan_tres_horas_y_no_una_jornada() -> None:
    """La regla de la fase, dicha en una sola comprobacion."""
    tarifa = hourly_rate(Decimal(120), JORNADA)

    assert labor_cost(Decimal(3), tarifa) == Decimal(45)
    assert labor_cost(Decimal(3), tarifa) < Decimal(120)


def test_una_jornada_repartida_en_tres_tecnicas_se_cobra_una_vez() -> None:
    """3 h de torno + 2 h de asas + 3 h de vidriado son 8 h, no tres jornadas.

    Este es el fallo caro de la fase: cobrar tres jornadas completas a quien
    trabajo una. El importe sale creible —360 en vez de 120— y triplica el
    costo de mano de obra de la cotizacion entera.
    """
    tarifa = hourly_rate(Decimal(120), JORNADA)
    reparto = [Decimal(3), Decimal(2), Decimal(3)]

    total = sum((labor_cost(horas, tarifa) for horas in reparto), Decimal(0))

    assert sum(reparto) == JORNADA
    assert total == Decimal(120)


@pytest.mark.parametrize(
    ("horas", "tarifa"),
    [(Decimal(-1), Decimal(15)), (Decimal(1), Decimal(-15))],
)
def test_ni_las_horas_ni_la_tarifa_admiten_negativos(horas: Decimal, tarifa: Decimal) -> None:
    with pytest.raises(LaborMathError):
        labor_cost(horas, tarifa)


# ---------------------------------------------------------------------------
# Jornada excedida
# ---------------------------------------------------------------------------
def test_ocho_horas_exactas_no_avisan() -> None:
    """La comparacion es estricta: un aviso que salta siempre deja de leerse."""
    assert exceeds_workday(JORNADA, JORNADA) is False


def test_diez_horas_avisan() -> None:
    assert exceeds_workday(Decimal(10), JORNADA) is True


def test_el_aviso_no_trae_recargo() -> None:
    """Avisar no es decidir.

    Diez horas cuestan diez horas. Ni recargo nocturno, ni hora extra, ni
    multiplicador: la solucion —dia largo, dos dias o mas gente— la elige una
    persona, y hasta que la elija el costo es el de las horas.
    """
    tarifa = hourly_rate(Decimal(120), JORNADA)

    assert exceeds_workday(Decimal(10), JORNADA) is True
    assert labor_cost(Decimal(10), tarifa) == Decimal(150)


# ---------------------------------------------------------------------------
# Dias efectivos: una sugerencia, no un resultado
# ---------------------------------------------------------------------------
def test_diez_horas_caben_como_minimo_en_dos_dias() -> None:
    assert minimum_work_days(Decimal(10), JORNADA) == 2


def test_ocho_horas_son_un_dia() -> None:
    assert minimum_work_days(JORNADA, JORNADA) == 1


def test_media_jornada_sigue_siendo_un_dia() -> None:
    """Los dias no se fraccionan. Las horas si, y de ahi sale el costo."""
    assert minimum_work_days(Decimal(4), JORNADA) == 1


def test_sin_horas_no_hay_dias() -> None:
    assert minimum_work_days(Decimal(0), JORNADA) == 0


def test_la_sugerencia_no_decide_por_el_usuario() -> None:
    """Diez horas SUGIEREN dos dias, pero un dia largo es una opcion valida.

    Por eso esto devuelve un minimo y no «los dias»: lo que se guarde en la
    cotizacion sale de una decision humana, y es lo que 010F cobrara como
    espacio.
    """
    assert minimum_work_days(Decimal(10), JORNADA) == 2
    # Y nada aqui impide que quien planifica decida 1.
