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

import heapq
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection


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


def _push_lot_to_fifo(
    preparation_id: int,
    *,
    lot_order: dict[int, tuple[datetime, int, int]],
    fifo_heap: list[tuple[datetime, int, int]],
    in_fifo_heap: set[int],
) -> None:
    if preparation_id in in_fifo_heap:
        return
    heapq.heappush(fifo_heap, lot_order[preparation_id])
    in_fifo_heap.add(preparation_id)


def reconcile_lots(
    entries: Sequence[LotEntry],
    movements: Sequence[HistoricalMovement],
    aggregates: Sequence[AggregateBalance],
) -> LotReconciliationReport:
    """Reconstruye saldos de lotes sin inventar existencias.

    Los eventos se aplican en orden cronologico. Solo los consumos sin lote
    reciben una atribucion oldest-first, y unicamente en esta reconciliacion
    historica. En cuanto una asignacion no puede justificarse, la fila queda
    UNRECONCILED y los lotes se mantienen en cero como minimo.
    """
    Key = tuple[int, int]
    entry_groups: dict[Key, list[LotEntry]] = {}
    movement_groups: dict[Key, list[HistoricalMovement]] = {}
    aggregate_values: dict[Key, Decimal] = {}

    for entry in entries:
        entry_groups.setdefault((entry.product_id, entry.location_id), []).append(entry)
    for movement in movements:
        movement_groups.setdefault((movement.product_id, movement.location_id), []).append(movement)
    for aggregate in aggregates:
        key = (aggregate.product_id, aggregate.location_id)
        aggregate_values[key] = aggregate_values.get(key, Decimal(0)) + aggregate.quantity

    keys = set(entry_groups) | set(movement_groups) | set(aggregate_values)
    rows: list[ReconciliationRow] = []
    lot_balances: list[LotBalance] = []

    for product_id, location_id in sorted(keys):
        key = (product_id, location_id)
        lot_quantity: dict[int, Decimal] = {}
        lot_order: dict[int, tuple[datetime, int, int]] = {}
        fifo_heap: list[tuple[datetime, int, int]] = []
        in_fifo_heap: set[int] = set()
        affected: set[int] = set()
        unreconciled = False

        events: list[tuple[datetime, int, int, LotEntry | HistoricalMovement]] = []
        for entry in entry_groups.get(key, []):
            # Event kind 0 makes a PREPARATION_IN with the same timestamp/id
            # visible before a later event; movement ids normally break ties.
            events.append((entry.created_at, entry.movement_id, 0, entry))
        for movement in movement_groups.get(key, []):
            events.append((movement.created_at, movement.movement_id, 1, movement))
        events.sort(key=lambda event: (event[0], event[1], event[2]))

        for _created_at, movement_id, kind, event in events:
            affected.add(movement_id)
            if kind == 0:
                assert isinstance(event, LotEntry)
                preparation_id = event.preparation_id
                if event.quantity <= 0:
                    unreconciled = True
                    lot_quantity.setdefault(preparation_id, Decimal(0))
                    lot_order.setdefault(
                        preparation_id, (event.created_at, event.movement_id, preparation_id)
                    )
                    continue
                if preparation_id not in lot_quantity:
                    lot_quantity[preparation_id] = Decimal(0)
                    lot_order[preparation_id] = (
                        event.created_at,
                        event.movement_id,
                        preparation_id,
                    )
                lot_quantity[preparation_id] += event.quantity
                _push_lot_to_fifo(
                    preparation_id,
                    lot_order=lot_order,
                    fifo_heap=fifo_heap,
                    in_fifo_heap=in_fifo_heap,
                )
                continue

            assert isinstance(event, HistoricalMovement)
            quantity = event.quantity
            if quantity == 0:
                unreconciled = True
                continue

            if quantity > 0:
                # A positive adjustment can only be tied to a lot that exists
                # at this point in history. Never turn it into a new lot.
                if event.preparation_id is None or event.preparation_id not in lot_quantity:
                    unreconciled = True
                    continue
                lot_quantity[event.preparation_id] += quantity
                _push_lot_to_fifo(
                    event.preparation_id,
                    lot_order=lot_order,
                    fifo_heap=fifo_heap,
                    in_fifo_heap=in_fifo_heap,
                )
                continue

            remaining = -quantity
            if event.preparation_id is not None:
                preparation_id = event.preparation_id
                available = lot_quantity.get(preparation_id)
                if available is None:
                    unreconciled = True
                    continue
                taken = min(available, remaining)
                lot_quantity[preparation_id] = available - taken
                if lot_quantity[preparation_id] > 0:
                    _push_lot_to_fifo(
                        preparation_id,
                        lot_order=lot_order,
                        fifo_heap=fifo_heap,
                        in_fifo_heap=in_fifo_heap,
                    )
                if taken < remaining:
                    unreconciled = True
                continue

            # Only this migration-time path is allowed to assign an old
            # unlinked consumption by age. Operational writes require a lot.
            while remaining > 0 and fifo_heap:
                _created_at, _entry_id, preparation_id = heapq.heappop(fifo_heap)
                in_fifo_heap.discard(preparation_id)
                available = lot_quantity[preparation_id]
                if available <= 0:
                    continue
                taken = min(available, remaining)
                lot_quantity[preparation_id] = available - taken
                remaining -= taken
                if lot_quantity[preparation_id] > 0:
                    _push_lot_to_fifo(
                        preparation_id,
                        lot_order=lot_order,
                        fifo_heap=fifo_heap,
                        in_fifo_heap=in_fifo_heap,
                    )
            if remaining > 0:
                unreconciled = True

        reconstructed = sum(lot_quantity.values(), Decimal(0))
        aggregate_balance = aggregate_values.get(key, Decimal(0))
        difference = aggregate_balance - reconstructed
        status = (
            LotReconciliationStatus.UNRECONCILED
            if unreconciled or difference != 0
            else LotReconciliationStatus.RECONCILED
        )
        rows.append(
            ReconciliationRow(
                product_id=product_id,
                location_id=location_id,
                aggregate_balance=aggregate_balance,
                reconstructed_lot_balance=reconstructed,
                difference=difference,
                affected_movements=tuple(sorted(affected)),
                status=status,
            )
        )
        lot_balances.extend(
            LotBalance(
                preparation_id=preparation_id,
                product_id=product_id,
                location_id=location_id,
                quantity=quantity,
            )
            for preparation_id, quantity in sorted(lot_quantity.items())
        )

    return LotReconciliationReport(rows=tuple(rows), lot_balances=tuple(lot_balances))


def load_lot_reconciliation(
    connection: Connection,
) -> tuple[LotReconciliationReport, dict[int, str]]:
    """Carga el historial preparado y lo evalua con la funcion de W0.

    Lo comparten la migracion 0045 y el comando de ensayo en seco. En el
    historial anterior a W2, ``preparation_id`` de PREPARATION_OUT identifica
    el lote NUEVO que se esta produciendo, no el lote consumido; para ese tipo
    solo ``source_preparation_id`` expresa el origen consumido.
    """
    movement_columns = {
        column["name"] for column in inspect(connection).get_columns("stock_movements")
    }
    if "source_preparation_id" in movement_columns:
        movement_source_query = text(
            """
            SELECT m.id, m.product_id, m.location_id, m.quantity,
                   CASE
                     WHEN m.movement_type = 'PREPARATION_OUT'
                       THEN m.source_preparation_id
                     ELSE COALESCE(m.source_preparation_id, m.preparation_id)
                   END AS source_preparation_id,
                   m.created_at
              FROM stock_movements AS m
              JOIN products AS p ON p.id = m.product_id
             WHERE p.product_type = 'PREPARED_MATERIAL'
               AND m.movement_type <> 'PREPARATION_IN'
             ORDER BY m.created_at, m.id
            """
        )
    else:
        movement_source_query = text(
            """
            SELECT m.id, m.product_id, m.location_id, m.quantity,
                   CASE WHEN m.movement_type = 'PREPARATION_OUT'
                        THEN NULL::integer ELSE m.preparation_id END
                       AS source_preparation_id,
                   m.created_at
              FROM stock_movements AS m
              JOIN products AS p ON p.id = m.product_id
             WHERE p.product_type = 'PREPARED_MATERIAL'
               AND m.movement_type <> 'PREPARATION_IN'
             ORDER BY m.created_at, m.id
            """
        )

    entries = [
        LotEntry(
            preparation_id=row["preparation_id"],
            product_id=row["product_id"],
            location_id=row["location_id"],
            quantity=row["quantity"],
            created_at=row["created_at"],
            movement_id=row["id"],
        )
        for row in connection.execute(
            text(
                """
                SELECT m.id, m.preparation_id, m.product_id, m.location_id,
                       m.quantity, m.created_at
                  FROM stock_movements AS m
                  JOIN products AS p ON p.id = m.product_id
                 WHERE p.product_type = 'PREPARED_MATERIAL'
                   AND m.movement_type = 'PREPARATION_IN'
                   AND m.preparation_id IS NOT NULL
                 ORDER BY m.created_at, m.id
                """
            )
        ).mappings()
    ]
    movements = [
        HistoricalMovement(
            movement_id=row["id"],
            product_id=row["product_id"],
            location_id=row["location_id"],
            quantity=row["quantity"],
            preparation_id=row["source_preparation_id"],
            created_at=row["created_at"],
        )
        for row in connection.execute(movement_source_query).mappings()
    ]
    aggregates = [
        AggregateBalance(
            product_id=row["product_id"],
            location_id=row["location_id"],
            quantity=row["quantity"],
        )
        for row in connection.execute(
            text(
                """
                SELECT b.product_id, b.location_id, b.quantity
                  FROM stock_balances AS b
                  JOIN products AS p ON p.id = b.product_id
                 WHERE p.product_type = 'PREPARED_MATERIAL'
                """
            )
        ).mappings()
    ]
    uom_codes = {
        row["id"]: row["base_uom_code"]
        for row in connection.execute(
            text(
                """
                SELECT id, base_uom_code FROM products
                 WHERE product_type = 'PREPARED_MATERIAL'
                """
            )
        ).mappings()
        if row["base_uom_code"] is not None
    }
    return reconcile_lots(entries, movements, aggregates), uom_codes
