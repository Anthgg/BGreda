"""Roundtrip real de las migraciones 010P W2 en PostgreSQL local de pruebas."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.session import normalize_database_url

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
MIGRATION_DB = "greda_test_migration_0045"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL no definida: se omiten las pruebas con base de datos",
)


def _url_for(database: str) -> str:
    parts = urlsplit(normalize_database_url(TEST_DATABASE_URL))
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("W2 requiere una PostgreSQL local; no se usa una base remota")
    if "test" not in parts.path.rsplit("/", maxsplit=1)[-1].casefold():
        pytest.skip("W2 requiere que el nombre de TEST_DATABASE_URL contenga 'test'")
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


def _upgrade(revision: str) -> None:
    result = _alembic("upgrade", revision)
    assert result.returncode == 0, f"upgrade {revision} fallo:\n{result.stdout}\n{result.stderr}"


async def _revision(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        value = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert value is not None
    return str(value)


@pytest.fixture
async def migration_engine() -> AsyncIterator[AsyncEngine]:
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


async def test_0044_0045_esquema_y_roundtrip(migration_engine: AsyncEngine) -> None:
    _upgrade("0044")
    assert await _revision(migration_engine) == "0044"
    async with migration_engine.connect() as connection:
        profile_columns = set(
            (
                await connection.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_schema = 'public' AND table_name = 'profiles'"
                    )
                )
            ).all()
        )
        capability_constraint = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_constraint"
                " WHERE conrelid = 'profiles'::regclass AND contype = 'c'"
                " AND pg_get_constraintdef(oid) LIKE '%MASTERS_QUICK_CREATE%'"
            )
        )
    assert "capabilities" in profile_columns
    assert capability_constraint == 1
    result = _alembic("downgrade", "0043")
    assert result.returncode == 0, f"downgrade 0044 fallo:\n{result.stdout}\n{result.stderr}"
    assert await _revision(migration_engine) == "0043"
    _upgrade("0044")

    _upgrade("0045")
    assert await _revision(migration_engine) == "0045"
    async with migration_engine.connect() as connection:
        tables = set(
            (
                await connection.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables"
                        " WHERE table_schema = 'public'"
                    )
                )
            ).all()
        )
        movement_columns = set(
            (
                await connection.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_schema = 'public' AND table_name = 'stock_movements'"
                    )
                )
            ).all()
        )
        movement_types = await connection.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conrelid = 'stock_movements'::regclass AND contype = 'c'"
                " AND pg_get_constraintdef(oid) LIKE '%PRODUCTION_IN%'"
            )
        )
        foreign_keys = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_constraint"
                " WHERE conname = 'fk_products_source_v2_quotation_product_v2_quotation_products'"
                " AND contype = 'f'"
            )
        )
        lot_constraints = set(
            (
                await connection.scalars(
                    text(
                        "SELECT conname FROM pg_constraint"
                        " WHERE conrelid = 'stock_lot_balances'::regclass"
                    )
                )
            ).all()
        )
        result_constraints = set(
            (
                await connection.scalars(
                    text(
                        "SELECT conname FROM pg_constraint"
                        " WHERE conrelid = 'production_order_results'::regclass"
                    )
                )
            ).all()
        )
        prototype_result_constraints = set(
            (
                await connection.scalars(
                    text(
                        "SELECT conname FROM pg_constraint"
                        " WHERE conrelid = 'prototype_results'::regclass"
                    )
                )
            ).all()
        )
        custom_category_count = await connection.scalar(
            text(
                "SELECT count(*) FROM product_categories"
                " WHERE name = 'Piezas personalizadas'"
                " AND display_path = 'Piezas personalizadas'"
            )
        )
        trace_columns = await connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema = 'public' AND column_name = 'preparation_id'"
                " AND table_name IN ('production_consumptions', 'prototype_material_lines')"
            )
        )
    assert {
        "stock_lot_balances",
        "production_order_results",
        "prototype_results",
    } <= tables
    assert {"source_preparation_id", "v2_quotation_id"} <= movement_columns
    assert "PRODUCTION_IN" in movement_types
    assert "DELIVERY_OUT" in movement_types
    assert foreign_keys == 1
    assert trace_columns == 2
    assert {
        "fk_stock_lot_balances_preparation_id_recipe_preparations",
        "fk_stock_lot_balances_product_id_products",
        "fk_stock_lot_balances_location_id_stock_locations",
        "fk_stock_lot_balances_uom_code_units_of_measure",
        "uq_stock_lot_balances_preparation_location",
        "ck_stock_lot_balances_quantity_not_negative",
    } <= lot_constraints
    assert {
        "fk_por_order",
        "fk_por_legacy_line",
        "fk_por_v2_product",
        "fk_por_firing_line",
        "fk_production_order_results_product_id_products",
        "ck_production_order_results_exactly_one_origin_line",
        "ck_production_order_results_started_non_negative",
        "ck_production_order_results_good_non_negative",
        "ck_production_order_results_scrap_non_negative",
        "ck_production_order_results_result_matches_started",
        "uq_production_results_order_legacy_line",
        "uq_production_results_order_v2_line",
        "uq_production_results_order_firing_line",
    } <= result_constraints
    assert {
        "pk_prototype_results",
        "fk_pr_prototype",
        "fk_pr_product",
        "ck_prototype_results_started_non_negative",
        "ck_prototype_results_good_non_negative",
        "ck_prototype_results_scrap_non_negative",
        "ck_prototype_results_result_matches_started",
    } <= prototype_result_constraints
    assert custom_category_count == 1

    result = _alembic("downgrade", "0044")
    assert result.returncode == 0, f"downgrade 0045 fallo:\n{result.stdout}\n{result.stderr}"
    assert await _revision(migration_engine) == "0044"
    async with migration_engine.connect() as connection:
        category_count_after_downgrade = await connection.scalar(
            text(
                "SELECT count(*) FROM product_categories"
                " WHERE name = 'Piezas personalizadas'"
                " AND display_path = 'Piezas personalizadas'"
            )
        )
    assert category_count_after_downgrade == 1
    _upgrade("0045")
    assert await _revision(migration_engine) == "0045"
