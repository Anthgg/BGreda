"""Fase 009C — la duracion de una hornada la pone el HORNO, no una constante.

El taller no tarda lo mismo en el horno pequeno que en el grande: 3 dias por
hornada frente a 4. Estos tests fijan esa regla sobre `app.core.firings`, que
es donde se decide, y comprueban lo que de verdad se rompia antes: sumar las
hornadas de baja y alta y multiplicarlas por un unico numero da un resultado
que no corresponde a ningun horno.

La configuracion vive en ``kilns.firing_days_per_batch`` (Alembic 0014); aqui
entra como dato de entrada, para que el calculo siga siendo puro.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.firings import LineInput, SessionInput, compute_firing

#: Factor 1 en todo tramo: estos tests miden dias, no el factor de ocupacion.
FLAT_FACTORS = {1: [(1, 100, Decimal(1))], 2: [(1, 100, Decimal(1))]}

SMALL_KILN_DAYS = 3
LARGE_KILN_DAYS = 4
#: Capacidades reales del taller. El horno grande no es "mas dias porque es
#: grande": son dos datos independientes, y por eso el pequeno podria
#: configurarse a 5 sin que nada en el codigo lo impida.
SMALL_CAPACITY = "17000"
LARGE_CAPACITY = "200000"


def _session(
    key: str,
    kiln_id: int,
    firing_type: str,
    *,
    capacity: str,
    days_per_batch: int,
    rate: str = "100",
) -> SessionInput:
    return SessionInput(
        key=key,
        kiln_id=kiln_id,
        firing_type=firing_type,
        rate=Decimal(rate),
        capacity=Decimal(capacity),
        days_per_batch=days_per_batch,
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


def _by_type(math: object) -> dict[str, object]:
    return {s.firing_type: s for s in math.sessions}  # type: ignore[attr-defined]


class TestKilnDaysAreReadFromTheKiln:
    def test_small_kiln_3_days(self) -> None:
        """SMALL_KILN_3_DAYS: una hornada en el horno pequeno son 3 dias."""
        math = compute_firing(
            [_session("1:LOW", 1, "LOW", capacity=SMALL_CAPACITY, days_per_batch=SMALL_KILN_DAYS)],
            [_line(1, "10", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert math.sessions[0].batches == 1
        assert math.sessions[0].days == 3
        assert math.total_days == 3

    def test_large_kiln_4_days(self) -> None:
        """LARGE_KILN_4_DAYS: la misma hornada en el grande son 4."""
        math = compute_firing(
            [_session("2:LOW", 2, "LOW", capacity=LARGE_CAPACITY, days_per_batch=LARGE_KILN_DAYS)],
            [_line(1, "10", ("2:LOW",), factor_kiln_id=2)],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert math.sessions[0].batches == 1
        assert math.sessions[0].days == 4
        assert math.total_days == 4

    def test_low_fire_uses_kiln_days(self) -> None:
        """LOW_FIRE_USES_KILN_DAYS: la quema baja no tiene duracion propia.

        Mismo tipo de quema en dos hornos distintos: los dias cambian con el
        horno, no con el hecho de ser baja.
        """
        small = compute_firing(
            [_session("1:LOW", 1, "LOW", capacity=SMALL_CAPACITY, days_per_batch=SMALL_KILN_DAYS)],
            [_line(1, "10", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        large = compute_firing(
            [_session("2:LOW", 2, "LOW", capacity=LARGE_CAPACITY, days_per_batch=LARGE_KILN_DAYS)],
            [_line(1, "10", ("2:LOW",), factor_kiln_id=2)],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert (small.total_days, large.total_days) == (3, 4)

    def test_high_fire_uses_kiln_days(self) -> None:
        """HIGH_FIRE_USES_KILN_DAYS: y la alta tampoco."""
        small = compute_firing(
            [
                _session(
                    "1:HIGH", 1, "HIGH", capacity=SMALL_CAPACITY, days_per_batch=SMALL_KILN_DAYS
                )
            ],
            [_line(1, "10", ("1:HIGH",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        large = compute_firing(
            [
                _session(
                    "2:HIGH", 2, "HIGH", capacity=LARGE_CAPACITY, days_per_batch=LARGE_KILN_DAYS
                )
            ],
            [_line(1, "10", ("2:HIGH",), factor_kiln_id=2)],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert (small.total_days, large.total_days) == (3, 4)

    def test_multibatch_multiplies_kiln_days(self) -> None:
        """MULTIBATCH_MULTIPLIES_KILN_DAYS: cada hornada suma los dias de SU horno."""
        # 2 piezas de 10x10x10 = 2000 cm3 en un horno de 1000 -> 2 hornadas.
        small = compute_firing(
            [_session("1:LOW", 1, "LOW", capacity="1000", days_per_batch=SMALL_KILN_DAYS)],
            [_line(2, "10", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert (small.total_batches, small.total_days) == (2, 6)

        large = compute_firing(
            [_session("2:LOW", 2, "LOW", capacity="1000", days_per_batch=LARGE_KILN_DAYS)],
            [_line(2, "10", ("2:LOW",), factor_kiln_id=2)],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert (large.total_batches, large.total_days) == (2, 8)

        # 10 hornadas: 30 dias en el pequeno, 40 en el grande.
        ten_small = compute_firing(
            [_session("1:LOW", 1, "LOW", capacity="1000", days_per_batch=SMALL_KILN_DAYS)],
            [_line(10, "10", ("1:LOW",))],
            FLAT_FACTORS,
            multi_batch=True,
        )
        assert (ten_small.total_batches, ten_small.total_days) == (10, 30)

    def test_mixed_kilns_total_days(self) -> None:
        """MIXED_KILNS_TOTAL_DAYS: el caso real de CTZ-2026-000129.

        300 jarras de 10x10x15 = 450 000 cm3. En el horno pequeno (17 000)
        caben en 27 hornadas; en el grande (200 000), en 3.

            27 x 3 = 81   y   3 x 4 = 12   ->   93 dias

        El calculo anterior sumaba primero las hornadas (27 + 3 = 30) y las
        multiplicaba por un 3 global: 90 dias, un numero que no corresponde a
        ningun horno. Este test es justo esa diferencia.
        """
        math = compute_firing(
            [
                _session(
                    "1:LOW", 1, "LOW", capacity=SMALL_CAPACITY, days_per_batch=SMALL_KILN_DAYS
                ),
                _session(
                    "2:HIGH", 2, "HIGH", capacity=LARGE_CAPACITY, days_per_batch=LARGE_KILN_DAYS
                ),
            ],
            [
                LineInput(
                    quantity=300,
                    length_cm=Decimal(10),
                    width_cm=Decimal(10),
                    height_cm=Decimal(15),
                    session_keys=("1:LOW", "2:HIGH"),
                    factor_kiln_id=1,
                )
            ],
            FLAT_FACTORS,
            multi_batch=True,
        )
        by_type = _by_type(math)
        assert by_type["LOW"].batches == 27  # type: ignore[attr-defined]
        assert by_type["HIGH"].batches == 3  # type: ignore[attr-defined]
        assert by_type["LOW"].days == 81  # type: ignore[attr-defined]
        assert by_type["HIGH"].days == 12  # type: ignore[attr-defined]
        assert math.total_batches == 30
        assert math.total_days == 93, "30 hornadas x 3 daria 90: el horno grande son 4 dias"

    def test_real_firing_sheet_does_not_plan_days(self) -> None:
        """Una hoja de quema real no planifica: sin duracion, cero dias.

        `days_per_batch` por omision es 0 para que el dominio de Quemas —que
        describe UNA hornada ya ocurrida— no herede la planificacion del
        Cotizador.
        """
        math = compute_firing(
            [
                SessionInput(
                    key="1:LOW",
                    kiln_id=1,
                    firing_type="LOW",
                    rate=Decimal(500),
                    capacity=Decimal(SMALL_CAPACITY),
                )
            ],
            [_line(1, "10", ("1:LOW",))],
            FLAT_FACTORS,
        )
        assert math.total_days == 0
