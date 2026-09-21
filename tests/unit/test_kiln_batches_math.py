"""La aritmetica de la planificacion de hornadas, sin base de datos. Fase 010L.

Los casos son los del encargo, con sus numeros: 65 + 20, 65 + 40, exclusiva,
compartida, 120 % en dos hornadas, baja y alta, y sugerencias de 3 a 2.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.kiln_batches import (
    BatchCandidate,
    KilnBatchMathError,
    LineCycles,
    Requirement,
    assignment_volume,
    available_cm3,
    available_percent,
    fits,
    high_start_violations,
    max_pieces_that_fit,
    occupancy_percent,
    suggest,
)

HOY = date(2026, 9, 21)
CAPACIDAD = Decimal(1000)


def _hornada(
    batch_id: int,
    asignado: str,
    *,
    ciclo: str = "LOW",
    editable: bool = True,
    exclusiva: bool = False,
    fecha: date = HOY,
    horno: int = 1,
) -> BatchCandidate:
    return BatchCandidate(
        batch_id=batch_id,
        kiln_id=horno,
        kiln_name=f"Horno {horno}",
        firing_type=ciclo,
        scheduled_date=fecha,
        editable=editable,
        exclusive=exclusiva,
        capacity_cm3=CAPACIDAD,
        assigned_cm3=Decimal(asignado),
    )


def _pedido(pendiente: str, *, pieza: str = "10", ciclo: str = "LOW", exclusiva: bool = False):
    return Requirement(
        firing_type=ciclo,
        remaining_cm3=Decimal(pendiente),
        smallest_piece_cm3=Decimal(pieza),
        exclusive=exclusiva,
    )


class TestCapacidad:
    def test_65_mas_20_queda_en_85(self) -> None:
        assert fits(Decimal(650), CAPACIDAD, Decimal(200))
        assert occupancy_percent(Decimal(850), CAPACIDAD) == Decimal("85.000000")
        assert available_percent(Decimal(850), CAPACIDAD) == Decimal("15.000000")

    def test_65_mas_40_se_rechaza_y_no_queda_en_105(self) -> None:
        assert not fits(Decimal(650), CAPACIDAD, Decimal(400))

    def test_llenar_exactamente_el_100_si_se_puede(self) -> None:
        """Sin tolerancia en ningun sentido: el 100 % justo cabe, el 100,0001 no."""
        assert fits(Decimal(650), CAPACIDAD, Decimal(350))
        assert not fits(Decimal(650), CAPACIDAD, Decimal("350.000001"))

    def test_lo_libre_nunca_es_negativo(self) -> None:
        assert available_cm3(Decimal(1000), CAPACIDAD) == Decimal(0)

    def test_una_capacidad_nula_es_un_error_y_no_un_cero(self) -> None:
        with pytest.raises(KilnBatchMathError):
            occupancy_percent(Decimal(0), Decimal(0))

    @pytest.mark.parametrize(("cantidad", "unitario"), [(0, "10"), (-3, "10"), (2, "0")])
    def test_una_asignacion_sin_piezas_o_sin_volumen_no_existe(
        self, cantidad: int, unitario: str
    ) -> None:
        with pytest.raises(KilnBatchMathError):
            assignment_volume(cantidad, Decimal(unitario))

    def test_120_por_ciento_se_parte_en_100_mas_20(self) -> None:
        """Una orden que pide 1,2 hornadas llena una y deja el 20 % para otra."""
        unitario = Decimal(100)
        piezas = 12  # 1200 cm3 = 120 % de una hornada de 1000
        en_la_primera = max_pieces_that_fit(CAPACIDAD, unitario)
        assert en_la_primera == 10
        resto = piezas - en_la_primera
        assert occupancy_percent(assignment_volume(en_la_primera, unitario), CAPACIDAD) == Decimal(
            "100.000000"
        )
        assert occupancy_percent(assignment_volume(resto, unitario), CAPACIDAD) == Decimal(
            "20.000000"
        )


class TestSugerencias:
    def test_de_tres_hornadas_solo_se_sugieren_las_dos_compatibles(self) -> None:
        candidatas = [
            _hornada(1, "650"),
            _hornada(2, "300"),
            _hornada(3, "0", ciclo="HIGH"),  # ciclo incompatible
        ]
        ids = [s.batch_id for s in suggest(candidatas, _pedido("200"), HOY)]
        assert sorted(ids) == [1, 2]

    def test_no_se_sugiere_lo_arrancado_lo_completado_lo_pasado_ni_lo_lleno(self) -> None:
        candidatas = [
            _hornada(1, "0", editable=False),  # arrancada o completada
            _hornada(2, "0", fecha=date(2026, 9, 20)),  # ayer
            _hornada(3, "1000"),  # llena
            _hornada(4, "995", ciclo="LOW"),  # no cabe ni la pieza mas pequena
            _hornada(5, "0"),  # la unica valida
        ]
        ids = [s.batch_id for s in suggest(candidatas, _pedido("200", pieza="10"), HOY)]
        assert ids == [5]

    def test_una_hornada_exclusiva_no_ofrece_su_libre_a_nadie(self) -> None:
        """Ocupa el 20 % y el 80 % restante NO aparece como disponible."""
        candidatas = [_hornada(1, "200", exclusiva=True)]
        assert suggest(candidatas, _pedido("100"), HOY) == []

    def test_un_pedido_exclusivo_solo_va_a_hornadas_vacias(self) -> None:
        candidatas = [_hornada(1, "300"), _hornada(2, "0")]
        ids = [s.batch_id for s in suggest(candidatas, _pedido("200", exclusiva=True), HOY)]
        assert ids == [2]

    def test_una_compartida_con_libre_si_recibe_otra_compatible(self) -> None:
        candidatas = [_hornada(1, "200")]
        (sugerencia,) = suggest(candidatas, _pedido("200"), HOY)
        assert sugerencia.available_percent == Decimal("80.000000")
        assert sugerencia.covers_all

    def test_primero_las_que_admiten_todo_luego_la_fecha_luego_el_ajuste(self) -> None:
        candidatas = [
            _hornada(1, "900"),  # admite solo 100 de 300: parcial
            _hornada(2, "0", fecha=date(2026, 9, 25)),  # admite todo, mas tarde
            _hornada(3, "600", fecha=date(2026, 9, 23)),  # admite todo, antes, justa
            _hornada(4, "0", fecha=date(2026, 9, 23)),  # admite todo, antes, holgada
        ]
        ids = [s.batch_id for s in suggest(candidatas, _pedido("300"), HOY)]
        assert ids == [3, 4, 2, 1]

    def test_mismo_input_mismo_orden(self) -> None:
        candidatas = [_hornada(i, str(i * 10), horno=i % 3) for i in range(1, 30)]
        primera = suggest(candidatas, _pedido("150"), HOY)
        segunda = suggest(list(reversed(candidatas)), _pedido("150"), HOY)
        assert primera == segunda

    def test_una_parcial_dice_cuanto_cubriria(self) -> None:
        (sugerencia,) = suggest([_hornada(1, "900")], _pedido("400"), HOY)
        assert not sugerencia.covers_all
        assert sugerencia.covered_percent == Decimal("25.000000")

    def test_nada_pendiente_nada_que_sugerir(self) -> None:
        assert suggest([_hornada(1, "0")], _pedido("0"), HOY) == []


class TestBajaAntesQueAlta:
    LINEA = LineCycles(line_key="L1", needs_low=True, needs_high=True)

    def test_sin_baja_completada_la_alta_no_arranca(self) -> None:
        assert high_start_violations([self.LINEA], {"L1": 5}, {}, {}) == ["L1"]

    def test_con_la_baja_completada_si(self) -> None:
        assert high_start_violations([self.LINEA], {"L1": 5}, {}, {"L1": 5}) == []

    def test_se_evalua_por_cantidad_si_la_baja_se_partio(self) -> None:
        """Bizcochadas 6 de 10: en alta pueden arrancar 6, no 10."""
        assert high_start_violations([self.LINEA], {"L1": 6}, {}, {"L1": 6}) == []
        assert high_start_violations([self.LINEA], {"L1": 10}, {}, {"L1": 6}) == ["L1"]

    def test_las_altas_ya_arrancadas_cuentan_contra_lo_bizcochado(self) -> None:
        """6 bizcochadas, 4 ya en otra alta: en esta solo caben 2."""
        assert high_start_violations([self.LINEA], {"L1": 2}, {"L1": 4}, {"L1": 6}) == []
        assert high_start_violations([self.LINEA], {"L1": 3}, {"L1": 4}, {"L1": 6}) == ["L1"]

    def test_una_pieza_que_solo_necesita_alta_no_se_bloquea(self) -> None:
        """Solo Quema de una pieza que el cliente trae ya bizcochada."""
        solo_alta = LineCycles(line_key="L2", needs_low=False, needs_high=True)
        assert high_start_violations([solo_alta], {"L2": 8}, {}, {}) == []
