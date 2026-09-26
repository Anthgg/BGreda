"""La 0042 W1 contra PostgreSQL: schema, reglas de backfill y rollback seguro."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.session import normalize_database_url

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
MIGRATION_DB = "greda_test_migration_0042"
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


def _upgrade(revision: str) -> None:
    result = _alembic("upgrade", revision)
    assert result.returncode == 0, f"upgrade {revision} fallo:\n{result.stdout}\n{result.stderr}"


@pytest.fixture
async def migration_engine() -> AsyncIterator[AsyncEngine]:
    admin = create_async_engine(
        _url_for("postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"statement_cache_size": 0},
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}"'))
            await conn.execute(text(f'CREATE DATABASE "{MIGRATION_DB}"'))
        engine = create_async_engine(
            _url_for(MIGRATION_DB), connect_args={"statement_cache_size": 0}
        )
        try:
            yield engine
        finally:
            await engine.dispose()
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}"'))
    finally:
        await admin.dispose()


async def _quotation(
    engine: AsyncEngine,
    *,
    code: str,
    status: str,
    production_type: str,
    administrative_cost: str,
    space_per_day: str | None,
    workday_hours: str | None,
    validity_days: int = 30,
    created_at: str = "2026-01-01 00:00:00+00",
) -> int:
    emitted_at = datetime.fromisoformat(created_at) if status == "CONFIRMED" else None
    cancelled_at = (
        datetime.fromisoformat(created_at) + timedelta(days=1) if status == "CANCELLED" else None
    )
    sql = text(
        "INSERT INTO v2_quotations"
        " (code, status, production_type, administrative_cost_snapshot,"
        "  space_service_cost_per_day_snapshot, workday_hours_snapshot,"
        "  validity_days_snapshot, created_at, issued_at, valid_until, expires_at,"
        "  commercial_fingerprint, cancelled_at)"
        " VALUES (:code, :status, :production_type, :administrative_cost,"
        "  :space_per_day, :workday_hours, :validity_days, :created_at, :issued_at,"
        "  :valid_until, :expires_at, :fingerprint, :cancelled_at) RETURNING id"
    )
    created_at_value = datetime.fromisoformat(created_at)
    async with engine.begin() as conn:
        value = await conn.scalar(
            sql,
            {
                "code": code,
                "status": status,
                "production_type": production_type,
                "administrative_cost": Decimal(administrative_cost),
                "space_per_day": None if space_per_day is None else Decimal(space_per_day),
                "workday_hours": None if workday_hours is None else Decimal(workday_hours),
                "validity_days": validity_days,
                "created_at": created_at_value,
                "issued_at": emitted_at,
                "valid_until": (
                    None
                    if emitted_at is None
                    else (emitted_at + timedelta(days=validity_days)).date()
                ),
                "expires_at": (
                    None if emitted_at is None else emitted_at + timedelta(days=validity_days)
                ),
                "fingerprint": "a" * 64 if emitted_at is not None else None,
                "cancelled_at": cancelled_at,
            },
        )
    assert value is not None
    return int(value)


async def _revision(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        value = await conn.scalar(text("SELECT version_num FROM alembic_version"))
    assert value is not None
    return str(value)


async def _columns(engine: AsyncEngine) -> dict[tuple[str, str], tuple[str, str]]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT table_name, column_name, data_type, is_nullable"
                    " FROM information_schema.columns WHERE table_schema = 'public'"
                    " AND table_name IN ('v2_quotation_products', 'v2_quotations',"
                    " 'v2_commercial_settings')"
                )
            )
        ).all()
    return {(table, column): (dtype, nullable) for table, column, dtype, nullable in rows}


async def test_0042_esquema_upgrade_downgrade_upgrade(migration_engine: AsyncEngine) -> None:
    _upgrade("0041")
    _upgrade("0042")
    columns = await _columns(migration_engine)
    expected = {
        ("v2_quotation_products", "production_time_per_unit_minutes"),
        ("v2_quotation_products", "mold_count"),
        ("v2_quotation_products", "line_active_minutes"),
        ("v2_quotation_products", "allocated_external_commercial_cost"),
        ("v2_quotation_products", "allocated_external_real_cost"),
        ("v2_quotations", "pricing_rules_version"),
        ("v2_quotations", "space_cost_per_hour_snapshot"),
        ("v2_quotations", "space_cost_per_hour_override"),
        ("v2_quotations", "passive_time_hours"),
        ("v2_quotations", "wholesale_threshold_snapshot"),
        ("v2_quotations", "wholesale_suggestion_declined_at"),
        ("v2_quotations", "active_production_minutes"),
        ("v2_quotations", "commercial_external_labor_cost"),
        ("v2_quotations", "real_external_labor_cost"),
        ("v2_commercial_settings", "wholesale_quantity_threshold"),
        ("v2_commercial_settings", "retail_default_worker_id"),
        ("v2_commercial_settings", "wholesale_default_worker_id"),
    }
    assert expected <= columns.keys()
    assert columns[("v2_quotation_products", "production_time_per_unit_minutes")] == (
        "numeric",
        "YES",
    )
    assert columns[("v2_quotation_products", "mold_count")] == ("integer", "NO")
    assert columns[("v2_quotations", "pricing_rules_version")] == ("smallint", "NO")
    assert columns[("v2_quotations", "space_cost_per_hour_snapshot")] == ("numeric", "YES")
    assert columns[("v2_quotations", "space_cost_per_hour_override")] == ("numeric", "YES")
    assert columns[("v2_quotations", "passive_time_hours")] == ("numeric", "NO")
    assert columns[("v2_quotations", "wholesale_threshold_snapshot")] == ("integer", "YES")
    assert columns[("v2_quotations", "wholesale_suggestion_declined_at")] == (
        "timestamp with time zone",
        "YES",
    )
    assert await _revision(migration_engine) == "0042"

    result = _alembic("downgrade", "0041")
    assert result.returncode == 0, f"downgrade 0041 fallo:\n{result.stdout}\n{result.stderr}"
    assert await _revision(migration_engine) == "0041"
    _upgrade("0042")
    assert await _revision(migration_engine) == "0042"
    assert expected <= (await _columns(migration_engine)).keys()


async def test_0042_backfill_solo_drafts_y_preserva_historicos(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("0041")
    casos = [
        ("DRAFT-RETAIL", "DRAFT", "RETAIL", "200", "80", "8", 30, "2026-01-01 00:00:00+00"),
        ("DRAFT-WHOLESALE", "DRAFT", "WHOLESALE", "75", "80", "8", 30, "2026-01-01 00:00:00+00"),
        ("ISSUED", "CONFIRMED", "RETAIL", "200", "80", "8", 30, "2026-01-01 00:00:00+00"),
        ("CANCELLED", "CANCELLED", "RETAIL", "200", "80", "8", 30, "2026-01-01 00:00:00+00"),
        # V2 no tiene estado EXPIRED: es CONFIRMED con la vigencia vencida.
        ("EXPIRED", "CONFIRMED", "RETAIL", "200", "80", "8", 1, "2020-01-01 00:00:00+00"),
        ("DRAFT-NULL", "DRAFT", "RETAIL", "90", None, None, 30, "2026-01-01 00:00:00+00"),
    ]
    ids: dict[str, int] = {}
    for code, status, kind, admin, space, hours, days, created_at in casos:
        ids[code] = await _quotation(
            migration_engine,
            code=code,
            status=status,
            production_type=kind,
            administrative_cost=admin,
            space_per_day=space,
            workday_hours=hours,
            validity_days=days,
            created_at=created_at,
        )

    _upgrade("0042")
    async with migration_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT code, status, production_type, pricing_rules_version,"
                    " administrative_cost_snapshot, space_cost_per_hour_snapshot,"
                    " wholesale_threshold_snapshot FROM v2_quotations ORDER BY code"
                )
            )
        ).all()
    actual = {
        code: (
            status,
            kind,
            int(version),
            Decimal(admin),
            None if space is None else Decimal(space),
            threshold,
        )
        for code, status, kind, version, admin, space, threshold in rows
    }
    assert actual["DRAFT-RETAIL"] == ("DRAFT", "RETAIL", 2, Decimal(0), Decimal(10), None)
    assert actual["DRAFT-WHOLESALE"] == (
        "DRAFT",
        "WHOLESALE",
        2,
        Decimal(75),
        Decimal(10),
        None,
    )
    assert actual["ISSUED"] == ("CONFIRMED", "RETAIL", 1, Decimal(200), None, None)
    assert actual["CANCELLED"] == ("CANCELLED", "RETAIL", 1, Decimal(200), None, None)
    assert actual["EXPIRED"] == ("CONFIRMED", "RETAIL", 1, Decimal(200), None, None)
    assert actual["DRAFT-NULL"] == ("DRAFT", "RETAIL", 2, Decimal(0), None, None)

    # La migracion no puede volver atras si la regla 010P ya ha cambiado
    # borradores retail o congelado valores nuevos.
    result = _alembic("downgrade", "0041")
    assert result.returncode != 0
    assert "datos 010P" in result.stderr + result.stdout
    assert await _revision(migration_engine) == "0042"
