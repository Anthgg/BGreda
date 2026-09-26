"""Fase 010P W0 — reconciliacion historica de lotes, fijada en rojo (decision 3).

Autoridad: 010P_PLAN_REV2 §2 (migracion 0045). Del mas antiguo al mas nuevo
SOLO para lo historico sin lote; nunca inventar cantidades ni dejar saldos
negativos; toda diferencia inexplicable marca UNRECONCILED y bloquea la
migracion.

EN ROJO hasta W2: `reconcile_lots` levanta NotImplementedError.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.lot_reconciliation import (
    AggregateBalance,
    HistoricalMovement,
    LotBalance,
    LotEntry,
    LotReconciliationStatus,
    reconcile_lots,
)

D = Decimal
CELADON = 100  # producto preparado
TALLER = 1  # ubicacion
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def lote(pid: int, cantidad: str, dias: int, mov: int) -> LotEntry:
    return LotEntry(
        preparation_id=pid,
        product_id=CELADON,
        location_id=TALLER,
        quantity=D(cantidad),
        created_at=T0 + timedelta(days=dias),
        movement_id=mov,
    )


def mov(mid: int, cantidad: str, dias: int, preparacion: int | None = None) -> HistoricalMovement:
    return HistoricalMovement(
        movement_id=mid,
        product_id=CELADON,
        location_id=TALLER,
        quantity=D(cantidad),
        preparation_id=preparacion,
        created_at=T0 + timedelta(days=dias),
    )


def saldo(cantidad: str) -> AggregateBalance:
    return AggregateBalance(product_id=CELADON, location_id=TALLER, quantity=D(cantidad))


def por_lote(informe: object) -> dict[int, Decimal]:
    balances: tuple[LotBalance, ...] = informe.lot_balances  # type: ignore[attr-defined]
    return {fila.preparation_id: fila.quantity for fila in balances}


def test_consumos_ligados_y_sin_lote_del_mas_antiguo_al_mas_nuevo_cuadran() -> None:
    # Lote A (antiguo) 10 000 y lote B 5 000. 2 000 salieron de A con lote; 3 000
    # sin lote salen de A por antiguedad. Saldo agregado real: 10 000.
    informe = reconcile_lots(
        [lote(1, "10000", 0, 11), lote(2, "5000", 5, 12)],
        [mov(21, "-2000", 6, preparacion=1), mov(22, "-3000", 7)],
        [saldo("10000")],
    )
    assert por_lote(informe) == {1: D(5000), 2: D(5000)}
    (fila,) = informe.rows
    assert fila.status is LotReconciliationStatus.RECONCILED
    assert (fila.aggregate_balance, fila.reconstructed_lot_balance, fila.difference) == (
        D(10000),
        D(10000),
        D(0),
    )
    assert 22 in fila.affected_movements
    assert informe.blocks_migration is False


def test_un_consumo_sin_lote_que_agota_el_mas_antiguo_sigue_en_el_siguiente() -> None:
    informe = reconcile_lots(
        [lote(1, "1000", 0, 11), lote(2, "5000", 5, 12)],
        [mov(22, "-3000", 7)],
        [saldo("3000")],
    )
    assert por_lote(informe) == {1: D(0), 2: D(3000)}
    assert informe.rows[0].status is LotReconciliationStatus.RECONCILED


def test_una_diferencia_con_el_saldo_agregado_no_se_inventa_y_bloquea() -> None:
    informe = reconcile_lots([lote(1, "5000", 0, 11)], [], [saldo("4800")])
    (fila,) = informe.rows
    assert fila.status is LotReconciliationStatus.UNRECONCILED
    assert fila.difference == D(-200)
    assert por_lote(informe) == {1: D(5000)}  # ni se «corrige» el lote para cuadrar
    assert informe.blocks_migration is True


def test_consumir_sin_lote_mas_de_lo_que_hay_no_deja_negativos_y_bloquea() -> None:
    informe = reconcile_lots(
        [lote(1, "1000", 0, 11)],
        [mov(22, "-1500", 3)],
        [saldo("0")],
    )
    assert all(cantidad >= 0 for cantidad in por_lote(informe).values())
    assert informe.rows[0].status is LotReconciliationStatus.UNRECONCILED
    assert 22 in informe.rows[0].affected_movements
    assert informe.blocks_migration is True


def test_un_ajuste_positivo_sin_lote_no_se_puede_atribuir_y_bloquea() -> None:
    informe = reconcile_lots(
        [lote(1, "1000", 0, 11)],
        [mov(23, "200", 2)],
        [saldo("1200")],
    )
    assert informe.rows[0].status is LotReconciliationStatus.UNRECONCILED
    assert 23 in informe.rows[0].affected_movements
    assert por_lote(informe) == {1: D(1000)}


def test_un_ajuste_negativo_sin_lote_cuenta_como_consumo_sin_lote() -> None:
    informe = reconcile_lots(
        [lote(1, "1000", 0, 11), lote(2, "1000", 1, 12)],
        [mov(24, "-400", 2)],
        [saldo("1600")],
    )
    assert por_lote(informe) == {1: D(600), 2: D(1000)}
    assert informe.rows[0].status is LotReconciliationStatus.RECONCILED


def test_cada_producto_y_ubicacion_es_su_propia_fila() -> None:
    otra = LotEntry(
        preparation_id=3,
        product_id=CELADON,
        location_id=2,
        quantity=D(700),
        created_at=T0,
        movement_id=13,
    )
    informe = reconcile_lots(
        [lote(1, "1000", 0, 11), otra],
        [],
        [saldo("1000"), AggregateBalance(product_id=CELADON, location_id=2, quantity=D(700))],
    )
    assert {(f.product_id, f.location_id) for f in informe.rows} == {(CELADON, 1), (CELADON, 2)}
    assert informe.blocks_migration is False


def test_invariante_final_suma_de_lotes_igual_al_saldo_agregado() -> None:
    informe = reconcile_lots(
        [lote(1, "10000", 0, 11), lote(2, "5000", 5, 12)],
        [
            mov(21, "-2000", 6, preparacion=1),
            mov(22, "-3000", 7),
            mov(25, "-500", 8, preparacion=2),
        ],
        [saldo("9500")],
    )
    assert sum(por_lote(informe).values(), D(0)) == D(9500)
    assert informe.blocks_migration is False
