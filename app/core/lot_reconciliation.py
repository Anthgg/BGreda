"""Fase 010P — reconciliacion unica del stock historico por lote de preparacion.

Funcion pura: recibe lo que la migracion 0045 leera de la base y devuelve los
saldos por lote y el LOT_RECONCILIATION_REPORT. La migracion y el script de
ensayo en seco (`scripts/lot_reconciliation_report.py`) usan la MISMA funcion.

**W0 (preparacion en rojo).** Solo la INTERFAZ aprobada (decision 3 de
010P_PLAN_REV2). `reconcile_lots` levanta `NotImplementedError` hasta W2, y
`tests/unit/test_lot_reconciliation.py` esta en ROJO a proposito.

## Reglas (decision 3, aprobada con guarda obligatoria)

1. Reconstruir cada lote desde su `PREPARATION_IN`.
2. Aplicar los consumos ya ligados a su lote.
3. Aplicar los consumos historicos SIN lote del lote mas antiguo al mas nuevo.
   Solo aqui: en operacion normal el lote se elige siempre explicitamente.
4. Los ajustes negativos sin lote cuentan como consumo sin lote; un ajuste
   POSITIVO sin lote no se puede atribuir a ningun lote.
5. Comparar con el saldo agregado (`stock_balances`).

Nunca se inventa una cantidad ni queda un saldo negativo. Toda diferencia que
no se explica marca la fila UNRECONCILED, y basta una para bloquear la
migracion.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class LotReconciliationStatus(StrEnum):
    RECONCILED = "RECONCILED"
    UNRECONCILED = "UNRECONCILED"


@dataclass(frozen=True)
class LotEntry:
    """El `PREPARATION_IN` que dio origen a un lote."""

    preparation_id: int
    product_id: int
    location_id: int
    quantity: Decimal
    created_at: datetime
    movement_id: int


@dataclass(frozen=True)
class HistoricalMovement:
    """Un movimiento historico del producto preparado posterior a su entrada.

    `quantity` lleva signo: negativa es salida. `preparation_id` es None cuando
    el movimiento no dijo de que lote salia.
    """

    movement_id: int
    product_id: int
    location_id: int
    quantity: Decimal
    preparation_id: int | None
    created_at: datetime


@dataclass(frozen=True)
class AggregateBalance:
    """El saldo actual de `stock_balances` para un producto en una ubicacion."""

    product_id: int
    location_id: int
    quantity: Decimal


@dataclass(frozen=True)
class LotBalance:
    preparation_id: int
    product_id: int
    location_id: int
    quantity: Decimal


@dataclass(frozen=True)
class ReconciliationRow:
    """Una fila del LOT_RECONCILIATION_REPORT."""

    product_id: int
    location_id: int
    aggregate_balance: Decimal
    reconstructed_lot_balance: Decimal
    difference: Decimal
    affected_movements: tuple[int, ...]
    status: LotReconciliationStatus


@dataclass(frozen=True)
class LotReconciliationReport:
    rows: tuple[ReconciliationRow, ...]
    lot_balances: tuple[LotBalance, ...]

    @property
    def blocks_migration(self) -> bool:
        """Una sola fila sin conciliar detiene la migracion."""
        return any(row.status is LotReconciliationStatus.UNRECONCILED for row in self.rows)


def reconcile_lots(
    entries: Sequence[LotEntry],
    movements: Sequence[HistoricalMovement],
    aggregates: Sequence[AggregateBalance],
) -> LotReconciliationReport:
    raise NotImplementedError(
        "010P W2: reconciliacion aprobada en 010P_PLAN_REV2, aun sin implementar"
    )
