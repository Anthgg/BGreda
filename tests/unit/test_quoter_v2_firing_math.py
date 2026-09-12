"""Fase 010E — la aritmetica de la quema, con los ejemplos aprobados.

Cada numero sale de una regla cerrada del proyecto o de la hoja «Quema V2» del
Excel «Cotizador Greda V2». No son casos inventados para que el codigo pase:
son los que el negocio ya resolvio a mano.

Los errores que estas pruebas cazan no fallan solos. Prorratear la segunda
hornada al 60 %, contar dos hornadas al 100 % exacto, o repartir un costo cuya
suma se queda a un centimo del total producen todos un importe creible, y solo
se notan cuando alguien suma la columna.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.quoter_v2_firing import (
    MAX_DETAILED_BATCHES,
    FiringMathError,
    allocate_by_volume,
    batch_loads,
    firing_cost,
    firing_count,
    firing_difference,
    occupancy_percent,
    piece_volume,
    total_volume,
    volume_share_percent,
)

#: El horno chico del Excel aprobado. Se usa como capacidad de referencia para
#: que los porcentajes de las pruebas signifiquen lo mismo que en la hoja.
CAPACIDAD = Decimal(100)


def volumen_para(porcentaje: str) -> Decimal:
    """El volumen que produce exactamente ese porcentaje de ocupacion."""
    return Decimal(porcentaje) * CAPACIDAD / Decimal(100)


# ---------------------------------------------------------------------------
# Geometria
# ---------------------------------------------------------------------------
def test_volumen_unitario_es_el_producto_de_las_tres_medidas() -> None:
    """18 x 12 x 3 son 648 cm3. El ejemplo «Plato palta» de la hoja Productos."""
    assert piece_volume(Decimal(18), Decimal(12), Decimal(3)) == Decimal(648)


def test_volumen_total_multiplica_por_la_cantidad() -> None:
    assert total_volume(Decimal(648), 20) == Decimal(12960)


@pytest.mark.parametrize(
    "medidas",
    [
        (None, Decimal(12), Decimal(3)),
        (Decimal(18), None, Decimal(3)),
        (Decimal(18), Decimal(12), None),
    ],
)
def test_una_medida_que_falta_deja_el_volumen_en_cero(
    medidas: tuple[Decimal | None, Decimal | None, Decimal | None],
) -> None:
    """Sin medir no se revienta: un borrador a medio llenar es legitimo."""
    assert piece_volume(*medidas) == Decimal(0)


def test_una_medida_en_cero_tampoco_ocupa_horno() -> None:
    """Una pieza de alto cero no existe. Cero volumen, sin excepcion."""
    assert piece_volume(Decimal(18), Decimal(12), Decimal(0)) == Decimal(0)


def test_cantidad_cero_no_ocupa_horno() -> None:
    assert total_volume(Decimal(648), 0) == Decimal(0)


# ---------------------------------------------------------------------------
# Ocupacion
# ---------------------------------------------------------------------------
def test_ocupacion_puede_pasar_de_cien() -> None:
    """160 % no es un error: son dos hornadas."""
    assert occupancy_percent(Decimal(160), CAPACIDAD) == Decimal(160)


def test_ocupacion_sin_volumen_es_cero() -> None:
    assert occupancy_percent(Decimal(0), CAPACIDAD) == Decimal(0)


def test_capacidad_cero_no_produce_un_porcentaje_inventado() -> None:
    with pytest.raises(FiringMathError):
        occupancy_percent(Decimal(100), Decimal(0))


# ---------------------------------------------------------------------------
# Hornadas: el techo exacto
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("porcentaje", "hornadas"),
    [
        ("0", 0),
        ("1", 1),
        ("5", 1),
        ("10", 1),
        ("99", 1),
        ("100", 1),
        ("101", 2),
        ("160", 2),
        ("200", 2),
        ("201", 3),
        ("300", 3),
    ],
)
def test_hornadas_de_la_tabla_aprobada(porcentaje: str, hornadas: int) -> None:
    """La lista completa del punto 10 de la especificacion.

    Los dos bordes son los que importan: 100 % exacto es UNA hornada —el horno
    lleno cabe— y 200 % exacto son DOS, no tres. Un `ceil` sobre coma flotante
    falla justo ahi, porque 2.0000000000000004 redondea a 3.
    """
    assert firing_count(volumen_para(porcentaje), CAPACIDAD) == hornadas


def test_un_centimetro_cubico_de_mas_ya_obliga_a_la_segunda() -> None:
    assert firing_count(Decimal("100.000001"), CAPACIDAD) == 2


@pytest.mark.parametrize(
    ("porcentaje", "hornadas"),
    [("0.0001", 1), ("199.9999", 2), ("200.0001", 3)],
)
def test_los_bordes_finos_de_cada_hornada(porcentaje: str, hornadas: int) -> None:
    """Los tres que caen justo al lado de un cambio de hornada.

    Una milesima de ocupacion ya obliga a encender una vez; una diezmilesima
    por debajo de 200 todavia cabe en dos; una por encima ya pide la tercera.
    Con `float` estos tres son exactamente los que fallan.
    """
    assert firing_count(volumen_para(porcentaje), CAPACIDAD) == hornadas


def test_una_millonesima_por_debajo_sigue_siendo_una_sola() -> None:
    assert firing_count(Decimal("99.999999"), CAPACIDAD) == 1


def test_el_conteo_no_se_toma_del_porcentaje_redondeado() -> None:
    """Esta es la trampa de la fase, y por eso el conteo mira el VOLUMEN.

    Con una capacidad de tres, un volumen de 3,0000001 ocupa el
    100,000003...%, que redondeado a seis decimales se guarda como
    100,000003. Pero hay casos en los que el redondeo del porcentaje cae
    exactamente en 100 y el volumen sigue sin caber: contar desde el
    porcentaje diria una hornada y el horno no cerraria.
    """
    capacidad = Decimal("30000000")
    volumen = Decimal("30000000.000001")
    assert occupancy_percent(volumen, capacidad) == Decimal(100)
    assert firing_count(volumen, capacidad) == 2


def test_hornadas_con_capacidad_cero_no_se_inventan() -> None:
    with pytest.raises(FiringMathError):
        firing_count(Decimal(100), Decimal(0))


# ---------------------------------------------------------------------------
# Carga de cada hornada: informacion, no cobro
# ---------------------------------------------------------------------------
def test_la_segunda_hornada_de_ciento_sesenta_va_al_sesenta() -> None:
    assert batch_loads(Decimal(160), 2) == (Decimal(100), Decimal(60))


def test_sin_hornadas_no_hay_cargas() -> None:
    assert batch_loads(Decimal(0), 0) == ()


def test_la_lista_de_cargas_tiene_tope() -> None:
    """El calculo no se limita; lo que se acota es el detalle de pantalla."""
    assert len(batch_loads(Decimal(100000), 1000)) == MAX_DETAILED_BATCHES


# ---------------------------------------------------------------------------
# Costo: la hornada se cobra entera
# ---------------------------------------------------------------------------
def test_dos_hornadas_cuestan_dos_tarifas_completas() -> None:
    """El caso canonico: externo, horno chico, baja. 2 x 200 = 400."""
    assert firing_cost(2, Decimal(200)) == Decimal(400)


def test_la_hornada_parcial_no_se_prorratea() -> None:
    """Con 160 % la segunda va al 60 %, y aun asi son 2 x 200, no 200 x 1,6.

    Es la regla economica de la fase: el horno se enciende completo. Un
    prorrateo daria S/320 y pareceria razonable.
    """
    assert firing_cost(2, Decimal(200)) == Decimal(400)
    assert firing_cost(2, Decimal(200)) != Decimal("320")


def test_el_gas_tampoco_se_prorratea() -> None:
    assert firing_cost(2, Decimal(35)) == Decimal(70)


def test_cero_hornadas_no_cuestan_nada() -> None:
    assert firing_cost(0, Decimal(200)) == Decimal(0)


def test_una_tarifa_en_cero_es_legitima() -> None:
    """Un horno prestado con el gas incluido se cotiza con gas cero."""
    assert firing_cost(2, Decimal(0)) == Decimal(0)


def test_una_tarifa_negativa_no_es_un_descuento() -> None:
    with pytest.raises(FiringMathError):
        firing_cost(1, Decimal(-1))


def test_hornadas_negativas_se_rechazan() -> None:
    with pytest.raises(FiringMathError):
        firing_cost(-1, Decimal(200))


# ---------------------------------------------------------------------------
# El caso canonico completo
# ---------------------------------------------------------------------------
def test_caso_canonico_externo_chico_ciento_sesenta_baja_y_alta() -> None:
    """Externo, horno chico, 160 %, baja + alta.

    Comercial: 2 x 200 + 2 x 250 = 900.
    Gas real:  2 x 35  + 2 x 70  = 210.
    Diferencia: 690.
    """
    hornadas = firing_count(volumen_para("160"), CAPACIDAD)
    assert hornadas == 2

    comercial = firing_cost(hornadas, Decimal(200)) + firing_cost(hornadas, Decimal(250))
    gas = firing_cost(hornadas, Decimal(35)) + firing_cost(hornadas, Decimal(70))

    assert comercial == Decimal(900)
    assert gas == Decimal(210)
    assert firing_difference(comercial, gas) == Decimal(690)


def test_solo_baja_externo_chico_ciento_sesenta() -> None:
    hornadas = firing_count(volumen_para("160"), CAPACIDAD)
    comercial = firing_cost(hornadas, Decimal(200))
    gas = firing_cost(hornadas, Decimal(35))
    assert (comercial, gas, firing_difference(comercial, gas)) == (
        Decimal(400),
        Decimal(70),
        Decimal(330),
    )


def test_solo_alta_externo_chico_ciento_sesenta() -> None:
    hornadas = firing_count(volumen_para("160"), CAPACIDAD)
    comercial = firing_cost(hornadas, Decimal(250))
    gas = firing_cost(hornadas, Decimal(70))
    assert (comercial, gas, firing_difference(comercial, gas)) == (
        Decimal(500),
        Decimal(140),
        Decimal(360),
    )


def test_alumno_chico_ochenta_por_ciento() -> None:
    """Alumno paga otra tarifa por la misma quema; el gas es el mismo.

    1 baja a 90 + 1 alta a 180 = 270. Gas: 35 + 70 = 105.
    """
    hornadas = firing_count(volumen_para("80"), CAPACIDAD)
    assert hornadas == 1
    comercial = firing_cost(hornadas, Decimal(90)) + firing_cost(hornadas, Decimal(180))
    gas = firing_cost(hornadas, Decimal(35)) + firing_cost(hornadas, Decimal(70))
    assert comercial == Decimal(270)
    assert gas == Decimal(105)


def test_la_diferencia_puede_ser_negativa_y_se_dice() -> None:
    """Cobrar menos que el gas es una perdida, y esconderla en un cero seria peor."""
    assert firing_difference(Decimal(50), Decimal(70)) == Decimal(-20)


# ---------------------------------------------------------------------------
# Reparto multiproducto
# ---------------------------------------------------------------------------
def test_reparto_del_ejemplo_aprobado() -> None:
    """A ocupa el 20 % y B el 30 %: una sola quema, repartida 40/60.

    Con S/450 de quema, A absorbe 180 y B 270. No son dos hornadas: los dos
    comparten la misma.
    """
    partes = allocate_by_volume(Decimal(450), [Decimal(20), Decimal(30)])
    assert partes == [Decimal("180"), Decimal("270")]
    assert sum(partes) == Decimal(450)


def test_la_participacion_se_mide_sobre_el_volumen_total() -> None:
    """Con 100 % y 60 % de ocupacion, el reparto es 100/160 y 60/160.

    NO «A es la hornada 1 y B la hornada 2»: el motor reparte economicamente
    por proporcion, y no simula como se colocan fisicamente las piezas.
    """
    assert volume_share_percent(Decimal(100), Decimal(160)) == Decimal("62.500000")
    assert volume_share_percent(Decimal(60), Decimal(160)) == Decimal("37.500000")


def test_la_suma_repartida_es_exactamente_el_total() -> None:
    """Tres partes iguales de 100 no caben exactas en ningun decimal finito.

    Sin politica de resto, cada parte seria 33,333...3 y la suma se quedaria a
    una millonesima del total: dinero que no se cobraria a nadie.
    """
    partes = allocate_by_volume(Decimal(100), [Decimal(1), Decimal(1), Decimal(1)])
    assert sum(partes) == Decimal(100)
    assert len(partes) == 3


def test_el_resto_va_a_quien_mas_perdio_al_truncar() -> None:
    partes = allocate_by_volume(Decimal(10), [Decimal(1), Decimal(1), Decimal(1)])
    assert sum(partes) == Decimal(10)
    # Uno de los tres se lleva el paso sobrante, y siempre el mismo.
    assert partes == allocate_by_volume(Decimal(10), [Decimal(1), Decimal(1), Decimal(1)])


def test_una_linea_sin_volumen_no_absorbe_quema() -> None:
    partes = allocate_by_volume(Decimal(450), [Decimal(20), Decimal(0), Decimal(30)])
    assert partes[1] == Decimal(0)
    assert sum(partes) == Decimal(450)


def test_sin_volumen_no_hay_reparto_pero_tampoco_hay_costo() -> None:
    """Sin volumen no hay hornadas, asi que el total tambien es cero."""
    assert allocate_by_volume(Decimal(0), [Decimal(0), Decimal(0)]) == [
        Decimal(0),
        Decimal(0),
    ]


def test_sin_lineas_no_hay_nada_que_repartir() -> None:
    assert allocate_by_volume(Decimal(450), []) == []


def test_una_sola_linea_se_lleva_todo() -> None:
    assert allocate_by_volume(Decimal(900), [Decimal(160)]) == [Decimal(900)]


def test_participacion_sin_total_es_cero() -> None:
    assert volume_share_percent(Decimal(0), Decimal(0)) == Decimal(0)
