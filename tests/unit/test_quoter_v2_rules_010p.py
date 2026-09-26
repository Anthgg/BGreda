"""Fase 010P W0 -- las reglas comerciales v2, fijadas en rojo antes de implementarlas.

Autoridad: 010P_PLAN_REV2 (§1.1-§1.9 y las seis decisiones aprobadas). Cada
numero esperado esta calculado a mano aqui mismo, sin Excel: el Excel anterior
es LEGACY_REFERENCE_PRE_010P y ya no es oraculo.

Configuracion comun de los casos: jornada 8 h, espacio S/140 por dia
(S/17,50 por hora), administracion S/200. Trabajadores: Pedro EXT 120/8 h,
Juan EXT 100/8 h, Rosa EXT 90/6 h, Ana INT (no entra en el costo externo).

EN ROJO hasta W1: `app.core.quoter_v2_rules_010p` solo declara la interfaz y
cada funcion levanta NotImplementedError.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.quoter_v2_rules_010p import (
    WARN_WHOLESALE_DEFAULT_WORKER_LACKS_TECHNIQUE,
    WARN_WHOLESALE_EXTERNAL_WORKER_MISSING,
    WARN_WHOLESALE_MANUAL_WORKERS_KEPT,
    DefaultWorker,
    ExternalWorkerSnapshot,
    LaborAssignmentOrigin,
    ProcessAssignment,
    Reassignment,
    administrative_cost,
    allocate_by_active_minutes,
    external_labor_cost,
    line_active_minutes,
    minutes_to_hours,
    order_active_minutes,
    passive_space_suggestion,
    plan_wholesale_labor,
    production_cycles,
    space_cost,
    space_cost_per_hour,
    wholesale_suggested,
)
from app.models.quoter_v2 import V2ProductionType

D = Decimal
JORNADA = D(8)
ESPACIO_DIA = D(140)
ADMIN = D(200)

PEDRO = ExternalWorkerSnapshot(worker_id=1, daily_rate=D(120), workday_hours=D(8))
JUAN = ExternalWorkerSnapshot(worker_id=2, daily_rate=D(100), workday_hours=D(8))
ROSA = ExternalWorkerSnapshot(worker_id=3, daily_rate=D(90), workday_hours=D(6))
PEDRO_BIS = ExternalWorkerSnapshot(worker_id=4, daily_rate=D(120), workday_hours=D(8))


# ---------------------------------------------------------------------------
# Tiempo: ciclos, minutos por linea y paralelismo (§1.1)
# ---------------------------------------------------------------------------
class TestTiempo:
    def test_f1_ciclos_redondean_hacia_arriba_por_moldes(self) -> None:
        assert production_cycles(30, 3) == 10
        assert production_cycles(25, 1) == 25

    def test_f5_un_molde_incompleto_es_un_ciclo_mas(self) -> None:
        assert production_cycles(31, 3) == 11
        assert line_active_minutes(31, 3, D(20)) == D(220)

    def test_sin_moldes_no_hay_ciclos(self) -> None:
        with pytest.raises(ValueError):
            production_cycles(10, 0)

    def test_f1_minutos_de_cada_linea(self) -> None:
        assert line_active_minutes(30, 3, D(45)) == D(450)
        assert line_active_minutes(25, 1, D(12)) == D(300)

    def test_f7_linea_sin_tiempo_no_tiene_minutos(self) -> None:
        assert line_active_minutes(30, 3, None) is None

    def test_f1_el_pedido_dura_el_maximo_no_la_suma(self) -> None:
        minutos = order_active_minutes([D(450), D(300)])
        assert minutos == D(450)
        assert minutes_to_hours(minutos) == D("7.5")

    def test_f3_paralelo_cinco_y_seis_horas_son_seis(self) -> None:
        a = line_active_minutes(10, 1, D(30))
        b = line_active_minutes(12, 1, D(30))
        assert (a, b) == (D(300), D(360))
        assert minutes_to_hours(order_active_minutes([a, b])) == D(6)

    def test_f7_lineas_sin_tiempo_no_cuentan_y_sin_ninguna_es_cero(self) -> None:
        assert order_active_minutes([D(450), None]) == D(450)
        assert order_active_minutes([None, None]) == D(0)
        assert order_active_minutes([]) == D(0)


# ---------------------------------------------------------------------------
# Mano de obra externa: comercial vs real (§1.2, correccion del owner)
# ---------------------------------------------------------------------------
class TestExternos:
    def test_f1_dos_externos_distintos(self) -> None:
        costo = external_labor_cost(D("7.5"), [PEDRO, JUAN], JORNADA)
        assert costo.commercial == D("206.25")  # 7,5 x (15 + 12,5)
        assert costo.real == D(220)  # 1 jornal x 120 + 1 jornal x 100
        assert costo.gap == D("13.75")
        por_id = {fila.worker_id: fila for fila in costo.per_worker}
        assert por_id[1].hourly_equivalent == D(15)
        assert por_id[1].days_paid == 1
        assert (por_id[1].commercial_cost, por_id[1].real_cost) == (D("112.5"), D(120))
        assert (por_id[2].commercial_cost, por_id[2].real_cost) == (D("93.75"), D(100))

    def test_f2_ejemplo_del_owner_diez_horas_un_externo(self) -> None:
        costo = external_labor_cost(D(10), [PEDRO], JORNADA)
        assert costo.commercial == D(150)  # 10 x 15
        assert costo.real == D(240)  # ceil(10/8) = 2 jornales x 120
        assert costo.gap == D(90)

    def test_f2b_dos_externos_iguales(self) -> None:
        costo = external_labor_cost(D(10), [PEDRO, PEDRO_BIS], JORNADA)
        assert costo.commercial == D(300)
        assert costo.real == D(480)

    def test_f4_cada_externo_con_su_propia_jornada(self) -> None:
        costo = external_labor_cost(D(10), [ROSA], JORNADA)
        assert costo.commercial == D(150)  # 90 / 6 = 15 por hora
        assert costo.real == D(180)  # ceil(10/6) = 2 jornales x 90
        assert costo.per_worker[0].days_paid == 2

    def test_f6_sin_externos_no_hay_costo(self) -> None:
        costo = external_labor_cost(D(10), [], JORNADA)
        assert (costo.commercial, costo.real, costo.gap) == (D(0), D(0), D(0))

    def test_f7_sin_tiempo_activo_no_se_paga_ningun_jornal(self) -> None:
        costo = external_labor_cost(D(0), [PEDRO], JORNADA)
        assert (costo.commercial, costo.real) == (D(0), D(0))

    def test_f9_sin_jornada_propia_vale_la_de_la_cotizacion(self) -> None:
        sin_jornada = ExternalWorkerSnapshot(worker_id=9, daily_rate=D(120), workday_hours=None)
        costo = external_labor_cost(D(10), [sin_jornada], JORNADA)
        assert costo.per_worker[0].workday_hours == JORNADA
        assert (costo.commercial, costo.real) == (D(150), D(240))

    def test_el_real_nunca_es_menor_que_el_comercial(self) -> None:
        for horas in (D("0.5"), D(8), D("8.01"), D(16), D("23.9")):
            costo = external_labor_cost(horas, [PEDRO, ROSA], JORNADA)
            assert costo.real >= costo.commercial
            assert costo.gap == costo.real - costo.commercial


# ---------------------------------------------------------------------------
# Espacio y tiempo pasivo (§1.3, §1.4)
# ---------------------------------------------------------------------------
class TestEspacio:
    def test_costo_por_hora_sale_del_dia_y_la_jornada(self) -> None:
        assert space_cost_per_hour(ESPACIO_DIA, JORNADA) == D("17.5")

    def test_f1_espacio_por_hora_activa(self) -> None:
        assert space_cost(D("7.5"), D("17.5")) == D("131.25")

    def test_f3_espacio_de_seis_horas_no_de_once(self) -> None:
        assert space_cost(D(6), D("17.5")) == D(105)

    def test_f1_tiempo_pasivo_es_solo_una_sugerencia(self) -> None:
        assert passive_space_suggestion(D(24), D("17.5")) == D(420)
        assert passive_space_suggestion(D(0), D("17.5")) == D(0)


# ---------------------------------------------------------------------------
# Administracion y umbral por mayor (§1.5, §1.7)
# ---------------------------------------------------------------------------
class TestAdministracionYUmbral:
    def test_f1_por_menor_no_paga_administracion(self) -> None:
        assert administrative_cost(V2ProductionType.RETAIL, ADMIN) == D(0)

    def test_f8_por_mayor_paga_lo_configurado(self) -> None:
        assert administrative_cost(V2ProductionType.WHOLESALE, ADMIN) == ADMIN

    def test_f1_el_umbral_mira_el_total_del_pedido(self) -> None:
        # 30 + 25 = 55 > 50, aunque ninguna linea sola lo supere.
        total = 30 + 25
        assert wholesale_suggested(V2ProductionType.RETAIL, total, 50, declined=False) is True

    def test_igualar_el_umbral_no_es_superarlo(self) -> None:
        assert wholesale_suggested(V2ProductionType.RETAIL, 50, 50, declined=False) is False

    def test_sin_umbral_configurado_no_se_sugiere(self) -> None:
        assert wholesale_suggested(V2ProductionType.RETAIL, 500, None, declined=False) is False

    def test_rechazada_no_se_vuelve_a_sugerir(self) -> None:
        assert wholesale_suggested(V2ProductionType.RETAIL, 55, 50, declined=True) is False

    def test_ya_por_mayor_no_se_sugiere(self) -> None:
        assert wholesale_suggested(V2ProductionType.WHOLESALE, 55, 50, declined=False) is False


# ---------------------------------------------------------------------------
# Reparto por peso normalizado (decision 5)
# ---------------------------------------------------------------------------
MINUTOS_F1 = (D(450), D(300))
CANTIDADES_F1 = (D(30), D(25))


class TestReparto:
    @pytest.mark.parametrize(
        ("total", "esperado"),
        [
            (D("131.25"), [D("78.75"), D("52.5")]),  # espacio
            (D("206.25"), [D("123.75"), D("82.5")]),  # externo comercial
            (D(220), [D(132), D(88)]),  # externo real
        ],
        ids=["espacio", "externo-comercial", "externo-real"],
    )
    def test_f1_sesenta_cuarenta_y_suma_exacta(
        self, total: Decimal, esperado: list[Decimal]
    ) -> None:
        partes = allocate_by_active_minutes(total, MINUTOS_F1, CANTIDADES_F1)
        assert partes == esperado
        assert sum(partes, D(0)) == total

    def test_tercios_reconstruyen_exactamente_el_total(self) -> None:
        partes = allocate_by_active_minutes(D(100), [D(100)] * 3, [D(1)] * 3)
        assert sum(partes, D(0)) == D(100)
        assert max(partes) - min(partes) <= D("1e-18")

    def test_f3_se_reparte_el_total_del_pedido_no_horas_de_linea_por_costo_hora(self) -> None:
        # 6 h de pedido x 17,50 = 105. Multiplicar cada linea (5 h y 6 h) daria
        # 192,50: tiempo cobrado dos veces. Lo repartido tiene que sumar 105.
        partes = allocate_by_active_minutes(D(105), [D(300), D(360)], [D(10), D(12)])
        assert sum(partes, D(0)) == D(105)
        assert partes != [D(300) / 60 * D("17.5"), D(360) / 60 * D("17.5")]

    def test_sin_minutos_se_reparte_por_cantidad(self) -> None:
        partes = allocate_by_active_minutes(D(110), [D(0), D(0)], [D(30), D(25)])
        assert partes == [D(60), D(50)]

    def test_sin_minutos_ni_cantidad_a_partes_iguales(self) -> None:
        partes = allocate_by_active_minutes(D(10), [D(0), D(0)], [D(0), D(0)])
        assert partes == [D(5), D(5)]


# ---------------------------------------------------------------------------
# Aceptar «Por mayor»: defaults laborales, sin pisar lo manual (decision 4)
# ---------------------------------------------------------------------------
TORNO, ASA, VIDRIADO = 10, 11, 12
INTERNA_RETAIL = 50
EXTERNO_DEFAULT = DefaultWorker(worker_id=60, technique_ids=frozenset({TORNO, ASA}))
ELEGIDO_A_MANO = 70


def _proceso(
    pid: int, tecnica: int, worker: int | None, origen: LaborAssignmentOrigin | None
) -> ProcessAssignment:
    return ProcessAssignment(process_id=pid, technique_id=tecnica, worker_id=worker, origin=origen)


class TestPorMayorPersonal:
    def test_lo_puesto_por_defecto_pasa_al_externo_por_defecto(self) -> None:
        plan = plan_wholesale_labor(
            [_proceso(1, TORNO, INTERNA_RETAIL, LaborAssignmentOrigin.DEFAULT)], EXTERNO_DEFAULT
        )
        assert plan.reassignments == (Reassignment(process_id=1, worker_id=60),)
        assert plan.warnings == ()

    def test_lo_elegido_a_mano_se_conserva_y_se_avisa(self) -> None:
        plan = plan_wholesale_labor(
            [_proceso(1, TORNO, ELEGIDO_A_MANO, LaborAssignmentOrigin.MANUAL)], EXTERNO_DEFAULT
        )
        assert plan.reassignments == ()
        assert WARN_WHOLESALE_MANUAL_WORKERS_KEPT in plan.warnings

    def test_lo_sin_asignar_recibe_el_externo_por_defecto(self) -> None:
        plan = plan_wholesale_labor([_proceso(1, ASA, None, None)], EXTERNO_DEFAULT)
        assert plan.reassignments == (Reassignment(process_id=1, worker_id=60),)

    def test_sin_externo_por_defecto_no_se_toca_nada_y_se_avisa(self) -> None:
        plan = plan_wholesale_labor(
            [
                _proceso(1, TORNO, INTERNA_RETAIL, LaborAssignmentOrigin.DEFAULT),
                _proceso(2, ASA, None, None),
            ],
            None,
        )
        assert plan.reassignments == ()
        assert WARN_WHOLESALE_EXTERNAL_WORKER_MISSING in plan.warnings

    def test_si_el_externo_no_sabe_la_tecnica_no_se_reasigna_a_ciegas(self) -> None:
        plan = plan_wholesale_labor(
            [_proceso(1, VIDRIADO, INTERNA_RETAIL, LaborAssignmentOrigin.DEFAULT)], EXTERNO_DEFAULT
        )
        assert plan.reassignments == ()
        assert WARN_WHOLESALE_DEFAULT_WORKER_LACKS_TECHNIQUE in plan.warnings

    def test_mezcla_default_manual_y_sin_asignar(self) -> None:
        plan = plan_wholesale_labor(
            [
                _proceso(1, TORNO, INTERNA_RETAIL, LaborAssignmentOrigin.DEFAULT),
                _proceso(2, ASA, ELEGIDO_A_MANO, LaborAssignmentOrigin.MANUAL),
                _proceso(3, ASA, None, None),
                _proceso(4, VIDRIADO, None, None),
            ],
            EXTERNO_DEFAULT,
        )
        assert plan.reassignments == (
            Reassignment(process_id=1, worker_id=60),
            Reassignment(process_id=3, worker_id=60),
        )
        assert set(plan.warnings) == {
            WARN_WHOLESALE_MANUAL_WORKERS_KEPT,
            WARN_WHOLESALE_DEFAULT_WORKER_LACKS_TECHNIQUE,
        }

    def test_si_ya_es_el_externo_por_defecto_no_hay_cambio(self) -> None:
        plan = plan_wholesale_labor(
            [_proceso(1, TORNO, 60, LaborAssignmentOrigin.DEFAULT)], EXTERNO_DEFAULT
        )
        assert plan.reassignments == ()
