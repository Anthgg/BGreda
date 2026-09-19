"""Fase 010E, actualizada en 010J — la aritmetica de la quema.

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
    EXCLUSIVE,
    MAX_DETAILED_BATCHES,
    SHARED,
    FiringMathError,
    allocate_by_volume,
    batch_loads,
    billed_load,
    firing_amount,
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


def test_la_separacion_se_suma_a_cada_medida() -> None:
    """Fase 010J. (18+3)(12+3)(3+3) = 1890 cm3: «Plato palta» del Excel final."""
    assert piece_volume(Decimal(18), Decimal(12), Decimal(3), Decimal(3)) == Decimal(1890)


def test_separacion_cero_es_la_caja_sin_margen() -> None:
    assert piece_volume(Decimal(18), Decimal(12), Decimal(3), Decimal(0)) == Decimal(648)


def test_rotar_la_pieza_no_cambia_el_volumen() -> None:
    """Sin algoritmo de acomodo: la caja envolvente no depende del orden."""
    a = piece_volume(Decimal(1), Decimal(15), Decimal(3), Decimal(3))
    b = piece_volume(Decimal(15), Decimal(3), Decimal(1), Decimal(3))
    assert a == b == Decimal(432)


def test_sin_una_medida_la_separacion_no_inventa_volumen() -> None:
    """Una linea sin alto no ocupa 3 x 3 x 3: ocupa cero y se avisa."""
    assert piece_volume(Decimal(18), Decimal(12), None, Decimal(3)) == Decimal(0)


def test_una_separacion_negativa_se_rechaza() -> None:
    with pytest.raises(FiringMathError):
        piece_volume(Decimal(18), Decimal(12), Decimal(3), Decimal(-1))


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
# Costo (fase 010J): carga facturada por modo, por ciclo
# ---------------------------------------------------------------------------
#: Tarifas del horno chico del Excel final, cliente externo.
BAJA, ALTA, GAS_BAJA, GAS_ALTA = Decimal(200), Decimal(250), Decimal(35), Decimal(70)


def quema(porcentaje: str, modo: str, baja: bool = True, alta: bool = True) -> tuple[Decimal, ...]:
    """(carga, comercial, gas) de una ocupacion, como la hoja «Quema V2»."""
    carga = billed_load(volumen_para(porcentaje), CAPACIDAD, modo)
    comercial = (firing_amount(carga, BAJA) if baja else Decimal(0)) + (
        firing_amount(carga, ALTA) if alta else Decimal(0)
    )
    gas = (firing_amount(carga, GAS_BAJA) if baja else Decimal(0)) + (
        firing_amount(carga, GAS_ALTA) if alta else Decimal(0)
    )
    return carga, comercial, gas


@pytest.mark.parametrize(
    ("porcentaje", "carga", "comercial", "gas"),
    [
        ("10", "0.1", "45", "10.5"),
        ("50", "0.5", "225", "52.5"),
        ("100", "1", "450", "105"),
        ("101", "1.01", "454.5", "106.05"),
        ("250", "2.5", "1125", "262.5"),
    ],
)
def test_compartida_cobra_la_fraccion_de_horno(
    porcentaje: str, carga: str, comercial: str, gas: str
) -> None:
    """COMPARTIDA: ocupacion/100 x tarifa completa, en precio y en gas, por ciclo."""
    assert quema(porcentaje, SHARED) == (Decimal(carga), Decimal(comercial), Decimal(gas))


@pytest.mark.parametrize(
    ("porcentaje", "carga", "comercial", "gas"),
    [
        ("10", 1, "450", "105"),
        ("50", 1, "450", "105"),
        ("100", 1, "450", "105"),
        ("101", 2, "900", "210"),
        ("250", 3, "1350", "315"),
    ],
)
def test_exclusiva_cobra_hornadas_enteras(
    porcentaje: str, carga: int, comercial: str, gas: str
) -> None:
    """EXCLUSIVA/URGENTE: techo(ocupacion/100) hornadas completas, precio y gas."""
    assert quema(porcentaje, EXCLUSIVE) == (Decimal(carga), Decimal(comercial), Decimal(gas))


def test_las_hornadas_fisicas_no_dependen_del_modo() -> None:
    """Un 250 % se enciende tres veces en los dos modos; cambia lo que se cobra."""
    volumen = volumen_para("250")
    assert firing_count(volumen, CAPACIDAD) == 3
    assert batch_loads(Decimal(250), 3) == (Decimal(100), Decimal(100), Decimal(50))


def test_solo_baja_y_solo_alta_son_independientes() -> None:
    _carga, com_baja, gas_baja = quema("50", SHARED, alta=False)
    _carga, com_alta, gas_alta = quema("50", SHARED, baja=False)
    assert (com_baja, gas_baja) == (Decimal(100), Decimal("17.5"))
    assert (com_alta, gas_alta) == (Decimal(125), Decimal(35))
    assert quema("50", SHARED)[1:] == (com_baja + com_alta, gas_baja + gas_alta)


def test_sin_volumen_no_se_cobra_en_ningun_modo() -> None:
    assert billed_load(Decimal(0), CAPACIDAD, SHARED) == Decimal(0)
    assert billed_load(Decimal(0), CAPACIDAD, EXCLUSIVE) == Decimal(0)


def test_un_modo_desconocido_se_rechaza() -> None:
    with pytest.raises(FiringMathError):
        billed_load(Decimal(10), CAPACIDAD, "HALF")


def test_carga_con_capacidad_cero_no_se_inventa() -> None:
    with pytest.raises(FiringMathError):
        billed_load(Decimal(10), Decimal(0), SHARED)


def test_la_carga_se_calcula_sin_float() -> None:
    """85320 / 17000 no es exacto: doce decimales Decimal, nunca un float."""
    carga = billed_load(Decimal(85320), Decimal(17000), SHARED)
    assert isinstance(carga, Decimal)
    assert carga == Decimal("5.018823529412")


def test_una_tarifa_en_cero_es_legitima() -> None:
    """Un horno prestado con el gas incluido se cotiza con gas cero."""
    assert firing_amount(Decimal("1.5"), Decimal(0)) == Decimal(0)


def test_una_tarifa_negativa_no_es_un_descuento() -> None:
    with pytest.raises(FiringMathError):
        firing_amount(Decimal(1), Decimal(-1))


def test_una_carga_negativa_se_rechaza() -> None:
    with pytest.raises(FiringMathError):
        firing_amount(Decimal(-1), Decimal(200))


# ---------------------------------------------------------------------------
# El caso canonico del Excel final (hoja «Quema V2»)
# ---------------------------------------------------------------------------
def test_caso_canonico_excel_final_chico_compartida() -> None:
    """85 320 cm3 en el horno chico (17 000): 501,88 %, 6 hornadas fisicas.

    Compartida, externo, baja + alta: comercial 2258,470588, gas 526,976471.
    """
    volumen, capacidad = Decimal(85320), Decimal(17000)
    assert occupancy_percent(volumen, capacidad) == Decimal("501.882353")
    assert firing_count(volumen, capacidad) == 6
    carga = billed_load(volumen, capacidad, SHARED)
    comercial = firing_amount(carga, BAJA) + firing_amount(carga, ALTA)
    gas = firing_amount(carga, GAS_BAJA) + firing_amount(carga, GAS_ALTA)
    assert comercial.quantize(Decimal("0.000001")) == Decimal("2258.470588")
    assert gas.quantize(Decimal("0.000001")) == Decimal("526.976471")


def test_caso_canonico_excel_final_grande_y_exclusiva() -> None:
    """El mismo pedido en el grande (700 / 1200) cuesta 810,54; en chico exclusiva, 2700."""
    volumen = Decimal(85320)
    carga_grande = billed_load(volumen, Decimal(200000), SHARED)
    assert carga_grande == Decimal("0.4266")
    assert firing_amount(carga_grande, Decimal(700)) + firing_amount(
        carga_grande, Decimal(1200)
    ) == Decimal("810.54")
    carga_exclusiva = billed_load(volumen, Decimal(17000), EXCLUSIVE)
    assert carga_exclusiva == Decimal(6)
    assert firing_amount(carga_exclusiva, BAJA) + firing_amount(carga_exclusiva, ALTA) == Decimal(
        2700
    )


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
