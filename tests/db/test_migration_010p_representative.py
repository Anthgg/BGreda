"""0041 -> 0045 sobre un fixture local representativo anterior a 010P."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.lot_reconciliation import load_lot_reconciliation
from app.db.session import normalize_database_url

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
MIGRATION_DB = "greda_test_010p_representative"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL no definida: se omiten las pruebas con base de datos",
)


def _url_for(database: str) -> str:
    parts = urlsplit(normalize_database_url(TEST_DATABASE_URL))
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "DATABASE_URL": _url_for(MIGRATION_DB)}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _upgrade(revision: str) -> subprocess.CompletedProcess[str]:
    result = _alembic("upgrade", revision)
    assert result.returncode == 0, f"upgrade {revision} fallo:\n{result.stdout}\n{result.stderr}"
    return result


@pytest.fixture
async def migration_engine() -> AsyncIterator[AsyncEngine]:
    """La fixture usa exclusivamente una base desechable cuyo nombre incluye test."""
    admin = create_async_engine(
        _url_for("postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"statement_cache_size": 0},
    )
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}"'))
            await connection.execute(text(f'CREATE DATABASE "{MIGRATION_DB}"'))
        engine = create_async_engine(
            _url_for(MIGRATION_DB), connect_args={"statement_cache_size": 0}
        )
        try:
            yield engine
        finally:
            await engine.dispose()
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}"'))
    finally:
        await admin.dispose()


async def _id(connection: AsyncConnection, sql: str, params: dict[str, object]) -> int:
    value = await connection.scalar(text(sql), params)
    assert value is not None
    return int(value)


async def _v2_quote(
    connection: AsyncConnection,
    *,
    code: str,
    status: str,
    production_type: str,
    total: str,
    issued_at: datetime | None = None,
    valid_until: date | None = None,
    expires_at: datetime | None = None,
    cancelled_at: datetime | None = None,
) -> int:
    fingerprint = "a" * 64 if issued_at is not None else None
    return await _id(
        connection,
        "INSERT INTO v2_quotations"
        " (code, status, production_type, issued_at, valid_until,"
        "  expires_at, cancelled_at, cancel_reason, commercial_fingerprint,"
        "  tax_percent_snapshot, total_amount)"
        " VALUES (:code, :status, :production_type, :issued_at, :valid_until,"
        "  :expires_at, :cancelled_at, :cancel_reason, :fingerprint, 18, :total)"
        " RETURNING id",
        {
            "code": code,
            "status": status,
            "production_type": production_type,
            "issued_at": issued_at,
            "valid_until": valid_until,
            "expires_at": expires_at,
            "cancelled_at": cancelled_at,
            "cancel_reason": "Fixture pre-010P" if cancelled_at is not None else None,
            "fingerprint": fingerprint,
            "total": Decimal(total),
        },
    )


async def _labor(
    connection: AsyncConnection,
    *,
    quote_id: int,
    worker_id: int,
    worker_name: str,
    worker_type: str,
    daily_rate: str,
    workday_hours: str,
    technique_id: int,
) -> int:
    return await _id(
        connection,
        "INSERT INTO v2_quotation_labor"
        " (v2_quotation_id, worker_id, worker_name_snapshot, worker_type_snapshot,"
        "  daily_rate_snapshot, workday_hours_snapshot, hourly_rate_snapshot, technique_id,"
        "  technique_name_snapshot, technique_unit_snapshot, standard_capacity_snapshot,"
        "  quantity, calculated_hours, final_hours, labor_cost)"
        " VALUES (:quote_id, :worker_id, :worker_name, :worker_type, :daily_rate,"
        "  :workday_hours, 15, :technique_id, 'Tecnica pre-010P', 'piezas', 40, 10, 2, 2, 0)"
        " RETURNING id",
        {
            "quote_id": quote_id,
            "worker_id": worker_id,
            "worker_name": worker_name,
            "worker_type": worker_type,
            "daily_rate": Decimal(daily_rate),
            "workday_hours": Decimal(workday_hours),
            "technique_id": technique_id,
        },
    )


async def _seed_pre_010p(connection: AsyncConnection) -> dict[str, int]:
    category_id = await _id(
        connection,
        "INSERT INTO product_categories (name, display_path)"
        " VALUES ('Fixture pre-010P', 'Fixture pre-010P') RETURNING id",
        {},
    )
    location_id = await _id(
        connection,
        "INSERT INTO stock_locations (name) VALUES ('Almacen fixture pre-010P') RETURNING id",
        {},
    )

    async def product(reference: str, name: str, kind: str, uom: str) -> int:
        return await _id(
            connection,
            "INSERT INTO products"
            " (internal_reference, name, product_type, product_category_id, base_uom_code)"
            " VALUES (:reference, :name, :kind, :category_id, :uom) RETURNING id",
            {
                "reference": reference,
                "name": name,
                "kind": kind,
                "category_id": category_id,
                "uom": uom,
            },
        )

    raw_id = await product("MIG010P-RAW", "Arcilla fixture", "RAW_MATERIAL", "g")
    prepared_a_id = await product("MIG010P-PREP-A", "Pasta lote A", "PREPARED_MATERIAL", "ml")
    prepared_b_id = await product("MIG010P-PREP-B", "Pasta lote B", "PREPARED_MATERIAL", "ml")
    finished_id = await product("MIG010P-FIN", "Taza fixture", "FINISHED_PRODUCT", "unit")

    external_worker_id = await _id(
        connection,
        "INSERT INTO v2_workers (name, worker_type, daily_rate, workday_hours)"
        " VALUES ('Trabajador externo fixture', 'EXTERNAL', 120, 8) RETURNING id",
        {},
    )
    internal_worker_id = await _id(
        connection,
        "INSERT INTO v2_workers (name, worker_type, daily_rate, workday_hours)"
        " VALUES ('Trabajador interno fixture', 'INTERNAL', 0, 8) RETURNING id",
        {},
    )
    technique_id = await _id(
        connection,
        "INSERT INTO v2_techniques (code, name, default_capacity_per_workday)"
        " VALUES ('MIG-010P', 'Tecnica fixture', 40) RETURNING id",
        {},
    )
    kiln_id = await _id(
        connection,
        "INSERT INTO kilns (code, name, capacity_volume_cm3)"
        " VALUES ('MIG-010P', 'Horno fixture', 90000) RETURNING id",
        {},
    )

    retail_draft_id = await _v2_quote(
        connection,
        code="MIG-010P-DRAFT-RETAIL",
        status="DRAFT",
        production_type="RETAIL",
        total="0",
    )
    wholesale_draft_id = await _v2_quote(
        connection,
        code="MIG-010P-DRAFT-WHOLESALE",
        status="DRAFT",
        production_type="WHOLESALE",
        total="0",
    )
    confirmed_id = await _v2_quote(
        connection,
        code="MIG-010P-CONFIRMED",
        status="CONFIRMED",
        production_type="RETAIL",
        total="123.45",
        issued_at=datetime(2024, 1, 1, tzinfo=UTC),
        valid_until=date(2024, 1, 31),
        expires_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    cancelled_id = await _v2_quote(
        connection,
        code="MIG-010P-CANCELLED",
        status="CANCELLED",
        production_type="RETAIL",
        total="55.25",
        cancelled_at=datetime(2024, 3, 1, tzinfo=UTC),
    )
    expired_id = await _v2_quote(
        connection,
        code="MIG-010P-EXPIRED",
        status="CONFIRMED",
        production_type="WHOLESALE",
        total="987.65",
        issued_at=datetime(2020, 1, 1, tzinfo=UTC),
        valid_until=date(2020, 1, 31),
        expires_at=datetime(2020, 2, 1, tzinfo=UTC),
    )

    labor_rows = [
        (retail_draft_id, external_worker_id, "EXTERNAL", "150", "8"),
        (retail_draft_id, internal_worker_id, "INTERNAL", "0", "8"),
        (wholesale_draft_id, external_worker_id, "EXTERNAL", "120", "6"),
        (confirmed_id, external_worker_id, "EXTERNAL", "999", "7"),
        (cancelled_id, external_worker_id, "EXTERNAL", "140", "8"),
        (expired_id, external_worker_id, "EXTERNAL", "130", "8"),
    ]
    for quote_id, worker_id, worker_type, daily_rate, workday_hours in labor_rows:
        await _labor(
            connection,
            quote_id=quote_id,
            worker_id=worker_id,
            worker_name="Snapshot historico",
            worker_type=worker_type,
            daily_rate=daily_rate,
            workday_hours=workday_hours,
            technique_id=technique_id,
        )

    recipe_a_id = await _id(
        connection,
        "INSERT INTO recipes (product_id, name) VALUES (:product_id, 'Receta pasta A')"
        " RETURNING id",
        {"product_id": prepared_a_id},
    )
    recipe_b_id = await _id(
        connection,
        "INSERT INTO recipes (product_id, name) VALUES (:product_id, 'Receta pasta B')"
        " RETURNING id",
        {"product_id": prepared_b_id},
    )
    version_a_id = await _id(
        connection,
        "INSERT INTO recipe_versions"
        " (recipe_id, version_number, status, yield_factor, base_total, fingerprint)"
        " VALUES (:recipe_id, 1, 'ACTIVE', 1, 100, :fingerprint) RETURNING id",
        {"recipe_id": recipe_a_id, "fingerprint": "b" * 64},
    )
    version_b_id = await _id(
        connection,
        "INSERT INTO recipe_versions"
        " (recipe_id, version_number, status, yield_factor, base_total, fingerprint)"
        " VALUES (:recipe_id, 1, 'ACTIVE', 1, 100, :fingerprint) RETURNING id",
        {"recipe_id": recipe_b_id, "fingerprint": "c" * 64},
    )
    await connection.execute(
        text("UPDATE recipes SET current_version_id = :version_id WHERE id = :recipe_id"),
        {"version_id": version_a_id, "recipe_id": recipe_a_id},
    )
    await connection.execute(
        text("UPDATE recipes SET current_version_id = :version_id WHERE id = :recipe_id"),
        {"version_id": version_b_id, "recipe_id": recipe_b_id},
    )
    preparation_a_id = await _id(
        connection,
        "INSERT INTO recipe_preparations"
        " (code, recipe_version_id, prepared_product_id, location_id, total_dry_weight_g,"
        "  water_amount_ml, final_yield_ml, solids_g_per_ml, batch_total_cost,"
        "  unit_cost_per_ml, idempotency_key)"
        " VALUES ('PREP-MIG-A', :version_id, :product_id, :location_id, 1000,"
        "  1000, 190, 1, 10, 0.05, 'prep-migration-a') RETURNING id",
        {
            "version_id": version_a_id,
            "product_id": prepared_a_id,
            "location_id": location_id,
        },
    )
    preparation_b_id = await _id(
        connection,
        "INSERT INTO recipe_preparations"
        " (code, recipe_version_id, prepared_product_id, location_id, total_dry_weight_g,"
        "  water_amount_ml, final_yield_ml, solids_g_per_ml, batch_total_cost,"
        "  unit_cost_per_ml, idempotency_key)"
        " VALUES ('PREP-MIG-B', :version_id, :product_id, :location_id, 1200,"
        "  1200, 200, 1, 12, 0.06, 'prep-migration-b') RETURNING id",
        {
            "version_id": version_b_id,
            "product_id": prepared_b_id,
            "location_id": location_id,
        },
    )

    legacy_quote_id = await _id(
        connection,
        "INSERT INTO quotations"
        " (code, status, product_id, quantity, commercial_factor_default_snapshot,"
        "  commercial_factor, source_fingerprint)"
        " VALUES ('MIG-010P-LEGACY', 'CONFIRMED', :product_id, 3, 1.25, 1.25, :fingerprint)"
        " RETURNING id",
        {"product_id": finished_id, "fingerprint": "d" * 64},
    )
    legacy_order_id = await _id(
        connection,
        "INSERT INTO production_orders"
        " (code, quotation_id, stock_location_id, qr_token, status, started_at)"
        " VALUES ('OT-MIG-010P-LEGACY', :quote_id, :location_id, :qr, 'STARTED', now())"
        " RETURNING id",
        {"quote_id": legacy_quote_id, "location_id": location_id, "qr": "l" * 48},
    )
    await connection.execute(
        text(
            "INSERT INTO production_order_lines"
            " (production_order_id, product_id, product_name_snapshot,"
            "  product_internal_reference_snapshot, quantity, recipe_id, recipe_version_id,"
            "  prepared_product_id, required_material_quantity, required_material_uom)"
            " VALUES (:order_id, :product_id, 'Taza fixture', 'MIG010P-FIN', 3,"
            "  :recipe_id, :version_id, :prepared_id, 15, 'ml')"
        ),
        {
            "order_id": legacy_order_id,
            "product_id": finished_id,
            "recipe_id": recipe_a_id,
            "version_id": version_a_id,
            "prepared_id": prepared_a_id,
        },
    )

    normal_handoff_id = await _id(
        connection,
        "INSERT INTO v2_production_handoffs (v2_quotation_id, commercial_fingerprint)"
        " VALUES (:quote_id, :fingerprint) RETURNING id",
        {"quote_id": confirmed_id, "fingerprint": "a" * 64},
    )
    v2_order_id = await _id(
        connection,
        "INSERT INTO production_orders"
        " (code, v2_handoff_id, stock_location_id, qr_token)"
        " VALUES ('OT-MIG-010P-V2', :handoff_id, :location_id, :qr) RETURNING id",
        {"handoff_id": normal_handoff_id, "location_id": location_id, "qr": "v" * 48},
    )

    prototype_id = await _id(
        connection,
        "INSERT INTO prototypes"
        " (code, name, product_id, stock_location_id, quantity, status, approval)"
        " VALUES ('PRO-MIG-010P', 'Prototipo fixture', :product_id, :location_id, 2,"
        "  'CREATED', 'PENDING') RETURNING id",
        {"product_id": finished_id, "location_id": location_id},
    )
    await connection.execute(
        text(
            "INSERT INTO prototype_material_lines"
            " (prototype_id, product_id, quantity, uom_code, product_name_snapshot,"
            "  product_internal_reference_snapshot, material_role, stage, quantity_actual)"
            " VALUES (:prototype_id, :product_id, 2, 'g', 'Arcilla fixture',"
            "  'MIG010P-RAW', 'BODY', 'PREPARATION', 2)"
        ),
        {"prototype_id": prototype_id, "product_id": raw_id},
    )

    firing_quote_id = await _id(
        connection,
        "INSERT INTO v2_firing_quotations"
        " (code, status, issued_at, valid_until, expires_at, commercial_fingerprint)"
        " VALUES ('MIG-010P-SOLO-QUEMA', 'CONFIRMED', :issued_at, :valid_until,"
        "  :expires_at, :fingerprint) RETURNING id",
        {
            "issued_at": datetime(2023, 1, 1, tzinfo=UTC),
            "valid_until": date(2023, 1, 31),
            "expires_at": datetime(2023, 2, 1, tzinfo=UTC),
            "fingerprint": "e" * 64,
        },
    )
    await connection.execute(
        text(
            "INSERT INTO v2_firing_quotation_lines"
            " (v2_firing_quotation_id, product_id, product_name_snapshot, quantity,"
            "  unit_volume_cm3, total_volume_cm3)"
            " VALUES (:quote_id, :product_id, 'Taza fixture', 5, 200, 1000)"
        ),
        {"quote_id": firing_quote_id, "product_id": finished_id},
    )
    firing_handoff_id = await _id(
        connection,
        "INSERT INTO v2_firing_production_handoffs"
        " (v2_firing_quotation_id, commercial_fingerprint)"
        " VALUES (:quote_id, :fingerprint) RETURNING id",
        {"quote_id": firing_quote_id, "fingerprint": "e" * 64},
    )
    solo_quema_order_id = await _id(
        connection,
        "INSERT INTO production_orders"
        " (code, v2_firing_handoff_id, stock_location_id, qr_token)"
        " VALUES ('OT-MIG-010P-SOLO-QUEMA', :handoff_id, :location_id, :qr) RETURNING id",
        {"handoff_id": firing_handoff_id, "location_id": location_id, "qr": "q" * 48},
    )

    for product_id, quantity in ((prepared_a_id, "165"), (prepared_b_id, "200"), (raw_id, "98")):
        await connection.execute(
            text(
                "INSERT INTO stock_balances (product_id, location_id, quantity)"
                " VALUES (:product_id, :location_id, :quantity)"
            ),
            {"product_id": product_id, "location_id": location_id, "quantity": Decimal(quantity)},
        )

    movements = [
        (prepared_a_id, "PREPARATION_IN", "190", "190", "ml", preparation_a_id, None, None),
        (prepared_a_id, "PREPARATION_OUT", "-20", "170", "ml", preparation_b_id, None, None),
        (prepared_b_id, "PREPARATION_IN", "200", "200", "ml", preparation_b_id, None, None),
        (
            prepared_a_id,
            "PRODUCTION_OUT",
            "-5",
            "165",
            "ml",
            preparation_a_id,
            legacy_order_id,
            None,
        ),
        (raw_id, "INITIAL_IMPORT", "100", "100", "g", None, None, None),
        (raw_id, "PROTOTYPE_OUT", "-2", "98", "g", None, None, prototype_id),
    ]
    for (
        product_id,
        movement_type,
        quantity,
        balance_after,
        uom,
        preparation_id,
        order_id,
        proto_id,
    ) in movements:
        await connection.execute(
            text(
                "INSERT INTO stock_movements"
                " (product_id, location_id, movement_type, quantity, balance_after,"
                "  uom_code, preparation_id, production_order_id, prototype_id, reason)"
                " VALUES (:product_id, :location_id, :movement_type, :quantity, :balance_after,"
                "  :uom, :preparation_id, :order_id, :prototype_id, 'Fixture pre-010P')"
            ),
            {
                "product_id": product_id,
                "location_id": location_id,
                "movement_type": movement_type,
                "quantity": Decimal(quantity),
                "balance_after": Decimal(balance_after),
                "uom": uom,
                "preparation_id": preparation_id,
                "order_id": order_id,
                "prototype_id": proto_id,
            },
        )

    return {
        "retail_draft": retail_draft_id,
        "wholesale_draft": wholesale_draft_id,
        "confirmed": confirmed_id,
        "cancelled": cancelled_id,
        "expired": expired_id,
        "external_worker": external_worker_id,
        "internal_worker": internal_worker_id,
        "technique": technique_id,
        "kiln": kiln_id,
        "raw": raw_id,
        "prepared_a": prepared_a_id,
        "prepared_b": prepared_b_id,
        "finished": finished_id,
        "location": location_id,
        "legacy_quote": legacy_quote_id,
        "legacy_order": legacy_order_id,
        "v2_order": v2_order_id,
        "prototype": prototype_id,
        "solo_quema_order": solo_quema_order_id,
    }


async def test_dataset_representativo_pre_010p_migra_de_0041_a_0045(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("0041")
    async with migration_engine.begin() as connection:
        ids = await _seed_pre_010p(connection)

    migration = _upgrade("0045")
    assert "LOT_RECONCILIATION_REPORT" in migration.stdout
    async with migration_engine.connect() as connection:
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        rules = dict(
            (
                await connection.execute(
                    text(
                        "SELECT code, pricing_rules_version FROM v2_quotations"
                        " WHERE id IN (:retail, :wholesale, :confirmed, :cancelled, :expired)"
                    ),
                    {
                        "retail": ids["retail_draft"],
                        "wholesale": ids["wholesale_draft"],
                        "confirmed": ids["confirmed"],
                        "cancelled": ids["cancelled"],
                        "expired": ids["expired"],
                    },
                )
            ).all()
        )
        retail_administration = await connection.scalar(
            text("SELECT administrative_cost_snapshot FROM v2_quotations WHERE id = :retail"),
            {"retail": ids["retail_draft"]},
        )
        legacy_snapshot = (
            await connection.execute(
                text(
                    "SELECT status, product_id, quantity, commercial_factor_default_snapshot,"
                    " commercial_factor, source_fingerprint FROM quotations WHERE id = :id"
                ),
                {"id": ids["legacy_quote"]},
            )
        ).one()
        snapshots = (
            await connection.execute(
                text(
                    "SELECT code, total_amount, tax_percent_snapshot, commercial_fingerprint"
                    " FROM v2_quotations"
                    " WHERE id IN (:confirmed, :cancelled, :expired) ORDER BY code"
                ),
                {
                    "confirmed": ids["confirmed"],
                    "cancelled": ids["cancelled"],
                    "expired": ids["expired"],
                },
            )
        ).all()
        assignment_origins = list(
            (
                await connection.scalars(
                    text("SELECT assignment_origin FROM v2_quotation_labor ORDER BY id")
                )
            ).all()
        )
        worker_snapshot_counts = dict(
            (
                await connection.execute(
                    text(
                        "SELECT v2_quotation_id, count(*) FROM v2_quotation_workers"
                        " WHERE v2_quotation_id IN "
                        "(:retail, :wholesale, :confirmed, :cancelled, :expired)"
                        " GROUP BY v2_quotation_id"
                    ),
                    {
                        "retail": ids["retail_draft"],
                        "wholesale": ids["wholesale_draft"],
                        "confirmed": ids["confirmed"],
                        "cancelled": ids["cancelled"],
                        "expired": ids["expired"],
                    },
                )
            ).all()
        )
        lots = (
            await connection.execute(
                text(
                    "SELECT preparation_id, product_id, location_id, quantity"
                    " FROM stock_lot_balances ORDER BY product_id"
                )
            )
        ).all()
        prepared_balance_differences = list(
            (
                await connection.execute(
                    text(
                        "SELECT b.product_id, b.location_id, b.quantity AS aggregate_quantity,"
                        " COALESCE(sum(l.quantity), 0) AS lot_quantity"
                        " FROM stock_balances b JOIN products p ON p.id = b.product_id"
                        " LEFT JOIN stock_lot_balances l"
                        " ON l.product_id = b.product_id AND l.location_id = b.location_id"
                        " WHERE p.product_type = 'PREPARED_MATERIAL'"
                        " GROUP BY b.product_id, b.location_id, b.quantity"
                        " HAVING b.quantity <> COALESCE(sum(l.quantity), 0)"
                    )
                )
            ).all()
        )
        negative_balances = await connection.scalar(
            text(
                "SELECT count(*) FROM ("
                " SELECT quantity FROM stock_balances WHERE quantity < 0"
                " UNION ALL SELECT quantity FROM stock_lot_balances WHERE quantity < 0"
                ") balances"
            )
        )
        preserved_counts = await connection.execute(
            text(
                "SELECT (SELECT count(*) FROM production_orders) AS orders,"
                " (SELECT count(*) FROM prototypes) AS prototypes,"
                " (SELECT count(*) FROM v2_firing_quotations) AS firing_quotes,"
                " (SELECT count(*) FROM stock_movements) AS movements"
            )
        )
        preserved = preserved_counts.mappings().one()
        lot_report, _ = await connection.run_sync(load_lot_reconciliation)

    assert revision == "0045"
    assert rules == {
        "MIG-010P-DRAFT-RETAIL": 2,
        "MIG-010P-DRAFT-WHOLESALE": 2,
        "MIG-010P-CONFIRMED": 1,
        "MIG-010P-CANCELLED": 1,
        "MIG-010P-EXPIRED": 1,
    }
    assert Decimal(retail_administration) == Decimal("0")
    assert tuple(legacy_snapshot) == (
        "CONFIRMED",
        ids["finished"],
        3,
        Decimal("1.25"),
        Decimal("1.25"),
        "d" * 64,
    )
    assert [(row[0], Decimal(row[1]), Decimal(row[2]), row[3]) for row in snapshots] == [
        ("MIG-010P-CANCELLED", Decimal("55.25"), Decimal("18"), None),
        ("MIG-010P-CONFIRMED", Decimal("123.45"), Decimal("18"), "a" * 64),
        ("MIG-010P-EXPIRED", Decimal("987.65"), Decimal("18"), "a" * 64),
    ]
    assert set(assignment_origins) == {"MANUAL"}
    assert worker_snapshot_counts == {
        ids["retail_draft"]: 2,
        ids["wholesale_draft"]: 1,
    }
    assert [(int(row[1]), Decimal(row[3])) for row in lots] == [
        (ids["prepared_a"], Decimal("165")),
        (ids["prepared_b"], Decimal("200")),
    ]
    assert prepared_balance_differences == []
    assert negative_balances == 0
    assert dict(preserved) == {"orders": 3, "prototypes": 1, "firing_quotes": 1, "movements": 6}
    assert not lot_report.blocks_migration
    assert all(row.status.value == "RECONCILED" for row in lot_report.rows)

    artifact = {
        "dataset": "synthetic local pre-010P migration fixture",
        "migration_path": "0041 -> 0042 -> 0043 -> 0044 -> 0045",
        "alembic_head": revision,
        "status": "PASS",
        "unreconciled_rows": sum(row.status.value == "UNRECONCILED" for row in lot_report.rows),
        "rows": [
            {
                "product_id": row.product_id,
                "location_id": row.location_id,
                "aggregate_balance": str(row.aggregate_balance),
                "reconstructed_lot_balance": str(row.reconstructed_lot_balance),
                "difference": str(row.difference),
                "status": row.status.value,
                "affected_movements": list(row.affected_movements),
            }
            for row in lot_report.rows
        ],
        "lots": [
            {
                "preparation_id": lot.preparation_id,
                "product_id": lot.product_id,
                "location_id": lot.location_id,
                "quantity": str(lot.quantity),
            }
            for lot in lot_report.lot_balances
        ],
    }
    artifact_path = REPO_ROOT / "artifacts" / "010P_W4" / "lot_reconciliation_representative.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
