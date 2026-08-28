"""Fase 009C — hornadas necesarias y su efecto en costo y dias.

Tests puros sobre `app.core.firings`: no tocan base de datos, asi que corren
en milisegundos y fijan la regla de negocio sin depender del entorno.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.firings import (
    FiringError,
    LineInput,
    SessionInput,
    compute_firing,
    required_batches,
)

#: Tabla de factores plana: factor 1 en todo tramo, para que los tests midan
#: el efecto de las hornadas y no el del factor de ocupacion.
FLAT_FACTORS = {1: [(1, 100, Decimal(1))], 2: [(1, 100, Decimal(1))]}


class TestRequiredBatches:
    def test_capacidad_exacta_es_una_sola_hornada(self) -> None:
        assert required_batches(Decimal(100), Decimal(100)) == 1

    def test_un_centimetro_cubico_de_mas_obliga_a_la_segunda(self) -> None:
        assert required_batches(Decimal(101), Decimal(100)) == 2

    def test_redondea_siempre_hacia_arriba(self) -> None:
        # 250/100 = 2.5 -> 3: no existe "media hornada".
        assert required_batches(Decimal(250), Decimal(100)) == 3

    def test_por_debajo_de_la_capacidad_es_una_hornada(self) -> None:
        assert required_batches(Decimal(99), Decimal(100)) == 1

    def test_sin_volumen_no_hay_hornadas(self) -> None:
        assert required_batches(Decimal(0), Decimal(100)) == 0

    def test_decimales_no_arrastran_error_de_float(self) -> None:
        # Con float, 0.3/0.1 da 2.9999... y el techo saldria 3 por suerte;
        # con 3*0.1 el resultado seria 4. Decimal exacto -> 3.
        assert required_batches(Decimal("0.3"), Decimal("0.1")) == 3

    def test_capacidad_cero_es_un_error_de_configuracion(self) -> None:
        with pytest.raises(FiringError):
            required_batches(Decimal(10), Decimal(0))


def _session(key: str, kiln_id: int, firing_type: str, rate: str, capacity: str) -> SessionInput:
    return SessionInput(
        key=key,
        kiln_id=kiln_id,
        firing_type=firing_type,
        rate=Decimal(rate),
        capacity=Decimal(capacity),
    )


def _line(quantity: int, side: str, keys: tuple[str, ...], factor_kiln_id: int = 1) -> LineInput:
    return LineInput(
        quantity=quantity,
        length_cm=Decimal(side),
        width_cm=Decimal(side),
        height_cm=Decimal(side),
        session_keys=keys,
        factor_kiln_id=factor_kiln_id,
    )


class TestMultiBatchFiring:
    def test_una_hornada_cuesta_la_tarifa_completa(self) -> None:
        # 1 pieza de 10x10x10 = 1000 cm3, horno de 1000 -> exactamente 1.
        math = compute_firing(
            [_session("1:LOW", 1, "LOW", "500", "1000")],
            [_line(1, "10", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert math.sessions[0].batches == 1
        assert math.total_batches == 1
        assert math.total_cost == Decimal(500)

    def test_tres_hornadas_multiplican_el_costo_por_tres(self) -> None:
        # 3 piezas de 1000 cm3 = 3000 cm3 en un horno de 1000 -> 3 hornadas.
        math = compute_firing(
            [_session("1:LOW", 1, "LOW", "500", "1000")],
            [_line(3, "10", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert math.sessions[0].batches == 3
        assert math.total_cost == Decimal(1500)

    def test_capacidad_mas_uno_son_dos_hornadas(self) -> None:
        # 2 piezas = 2000 cm3 en horno de 1999 -> no entra en una.
        math = compute_firing(
            [_session("1:LOW", 1, "LOW", "500", "1999")],
            [_line(2, "10", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert math.sessions[0].batches == 2
        assert math.total_cost == Decimal(1000)

    def test_baja_y_alta_se_calculan_por_separado(self) -> None:
        # Mismo volumen, hornos de capacidad distinta: la baja entra en 1
        # hornada y la alta necesita 3. No se asume el mismo numero.
        math = compute_firing(
            [
                _session("1:LOW", 1, "LOW", "400", "3000"),
                _session("2:HIGH", 2, "HIGH", "600", "1000"),
            ],
            [_line(3, "10", ("1:LOW", "2:HIGH"))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        by_type = {s.firing_type: s for s in math.sessions}
        assert by_type["LOW"].batches == 1
        assert by_type["HIGH"].batches == 3
        assert math.total_batches == 4
        # 400*1 + 600*3 = 2200
        assert math.total_cost == Decimal(2200)

    def test_solo_quema_baja_no_cobra_la_alta(self) -> None:
        math = compute_firing(
            [_session("1:LOW", 1, "LOW", "400", "1000")],
            [_line(2, "10", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert {s.firing_type for s in math.sessions} == {"LOW"}
        assert math.total_batches == 2
        assert math.total_cost == Decimal(800)

    def test_solo_quema_alta_no_cobra_la_baja(self) -> None:
        math = compute_firing(
            [_session("2:HIGH", 2, "HIGH", "600", "1000")],
            [_line(3, "10", ("2:HIGH",), factor_kiln_id=2)],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert {s.firing_type for s in math.sessions} == {"HIGH"}
        assert math.total_batches == 3
        assert math.total_cost == Decimal(1800)

    def test_multiproducto_comparte_hornada(self) -> None:
        # Dos piezas de 1000 cm3 cada una en un horno de 3000: caben juntas
        # en UNA hornada. El sistema ya sumaba volumenes por sesion y esta
        # fase no lo cambia.
        math = compute_firing(
            [_session("1:LOW", 1, "LOW", "500", "3000")],
            [_line(1, "10", ("1:LOW",)), _line(1, "10", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert math.sessions[0].batches == 1
        assert math.total_cost == Decimal(500)

    def test_multiproducto_que_no_cabe_junto_reparte_en_dos_hornadas(self) -> None:
        math = compute_firing(
            [_session("1:LOW", 1, "LOW", "500", "1500")],
            [_line(1, "10", ("1:LOW",)), _line(1, "10", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert math.sessions[0].batches == 2
        assert math.total_cost == Decimal(1000)

    def test_multi_batch_apagado_conserva_el_comportamiento_anterior(self) -> None:
        # Una hoja de quema REAL describe una hornada fisica: exceder la
        # capacidad sigue avisando y el costo NO se multiplica.
        math = compute_firing(
            [_session("1:LOW", 1, "LOW", "500", "1000")],
            [_line(3, "10", ("1:LOW",))],
            FLAT_FACTORS,
        )
        assert math.sessions[0].batches == 1
        assert math.total_cost == Decimal(500)
        assert math.capacity_exceeded is True

    def test_multi_batch_resuelve_el_exceso_en_vez_de_alertarlo(self) -> None:
        math = compute_firing(
            [_session("1:LOW", 1, "LOW", "500", "1000")],
            [_line(3, "10", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert math.capacity_exceeded is False

    def test_dimensiones_mayores_aumentan_las_hornadas(self) -> None:
        # Misma cantidad, pieza mas grande (medida efectiva de Fase 009B):
        # 1 pieza de 20x20x20 = 8000 cm3 no entra donde si entraba la de
        # 10x10x10 = 1000 cm3.
        small = compute_firing(
            [_session("1:LOW", 1, "LOW", "500", "2000")],
            [_line(1, "10", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        large = compute_firing(
            [_session("1:LOW", 1, "LOW", "500", "2000")],
            [_line(1, "20", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert small.sessions[0].batches == 1
        assert large.sessions[0].batches == 4
        assert large.total_cost == Decimal(2000)
