"""La 0043 W1 contra PostgreSQL: snapshots distintos y origen histórico MANUAL."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.session import normalize_database_url

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
MIGRATION_DB = "greda_test_migration_0043"
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


async def _id(engine: AsyncEngine, sql: str, params: dict[str, object]) -> int:
    async with engine.begin() as conn:
        value = await conn.scalar(text(sql), params)
    assert value is not None
    return int(value)


async def _quotation(engine: AsyncEngine, code: str, status: str, production_type: str) -> int:
    issued_at = datetime(2026, 1, 1, tzinfo=UTC) if status == "CONFIRMED" else None
    return await _id(
        engine,
        "INSERT INTO v2_quotations"
        " (code, status, production_type, issued_at, valid_until, expires_at,"
        " commercial_fingerprint)"
        " VALUES (:code, :status, :production_type, :issued_at, :valid_until,"
        " :expires_at, :fingerprint) RETURNING id",
        {
            "code": code,
            "status": status,
            "production_type": production_type,
            "issued_at": issued_at,
            "valid_until": date(2026, 1, 31) if issued_at is not None else None,
            "expires_at": (issued_at + timedelta(days=30) if issued_at is not None else None),
            "fingerprint": "a" * 64 if issued_at is not None else None,
        },
    )


async def _worker(engine: AsyncEngine, name: str, kind: str, rate: str) -> int:
    return await _id(
        engine,
        "INSERT INTO v2_workers (name, worker_type, daily_rate)"
        " VALUES (:name, :kind, :rate) RETURNING id",
        {"name": name, "kind": kind, "rate": Decimal(rate)},
    )


async def _technique(engine: AsyncEngine) -> int:
    return await _id(
        engine,
        "INSERT INTO v2_techniques (code, name, default_capacity_per_workday)"
        " VALUES ('MIG-0043', 'Tecnica migracion', 50) RETURNING id",
        {},
    )


async def _task(
    engine: AsyncEngine,
    quotation_id: int,
    worker_id: int,
    technique_id: int,
    *,
    worker_type: str,
    daily_rate: str,
    workday_hours: str,
) -> int:
    return await _id(
        engine,
        "INSERT INTO v2_quotation_labor"
        " (v2_quotation_id, worker_id, worker_name_snapshot, worker_type_snapshot,"
        "  daily_rate_snapshot, workday_hours_snapshot, hourly_rate_snapshot, technique_id,"
        "  technique_name_snapshot, technique_unit_snapshot, standard_capacity_snapshot,"
        "  quantity, calculated_hours, final_hours, labor_cost)"
        " VALUES (:quotation_id, :worker_id, 'Snapshot', :worker_type, :daily_rate,"
        "  :workday_hours, 15, :technique_id, 'Tecnica snapshot', 'piezas', 50,"
        "  10, 2, 2, 0) RETURNING id",
        {
            "quotation_id": quotation_id,
            "worker_id": worker_id,
            "worker_type": worker_type,
            "daily_rate": Decimal(daily_rate),
            "workday_hours": Decimal(workday_hours),
            "technique_id": technique_id,
        },
    )


async def _revision(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        value = await conn.scalar(text("SELECT version_num FROM alembic_version"))
    assert value is not None
    return str(value)


async def _snapshots(
    engine: AsyncEngine, quotation_id: int
) -> list[tuple[int, str, Decimal, Decimal | None]]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT worker_id, worker_type_snapshot, daily_rate_snapshot,"
                    " workday_hours_snapshot FROM v2_quotation_workers"
                    " WHERE v2_quotation_id = :id ORDER BY worker_id"
                ),
                {"id": quotation_id},
            )
        ).all()
    return [
        (int(worker), str(kind), Decimal(rate), None if hours is None else Decimal(hours))
        for worker, kind, rate, hours in rows
    ]


async def test_0043_esquema_upgrade_downgrade_upgrade(migration_engine: AsyncEngine) -> None:
    _upgrade("0043")
    async with migration_engine.connect() as conn:
        columns = {
            (table, column)
            for table, column in (
                await conn.execute(
                    text(
                        "SELECT table_name, column_name FROM information_schema.columns"
                        " WHERE table_schema = 'public' AND table_name IN"
                        " ('v2_quotation_workers', 'v2_quotation_labor')"
                    )
                )
            ).all()
        }
        heads = list((await conn.scalars(text("SELECT version_num FROM alembic_version"))).all())
        unique = await conn.scalar(
            text(
                "SELECT count(*) FROM pg_constraint WHERE conname ="
                " 'uq_v2_quotation_workers_worker' AND contype = 'u'"
            )
        )
    assert {
        ("v2_quotation_workers", "v2_quotation_id"),
        ("v2_quotation_workers", "worker_id"),
        ("v2_quotation_workers", "worker_type_snapshot"),
        ("v2_quotation_workers", "daily_rate_snapshot"),
        ("v2_quotation_workers", "workday_hours_snapshot"),
        ("v2_quotation_labor", "assignment_origin"),
    } <= columns
    assert heads == ["0043"]
    assert unique == 1

    result = _alembic("downgrade", "0042")
    assert result.returncode == 0, f"downgrade 0042 fallo:\n{result.stdout}\n{result.stderr}"
    assert await _revision(migration_engine) == "0042"
    _upgrade("0043")
    assert await _revision(migration_engine) == "0043"


async def test_0043_backfill_unico_solo_drafts_y_roundtrip_preserva_snapshots(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("0042")
    draft = await _quotation(migration_engine, "MIG-0043-DRAFT", "DRAFT", "WHOLESALE")
    confirmed = await _quotation(migration_engine, "MIG-0043-CONFIRMED", "CONFIRMED", "RETAIL")
    externo = await _worker(migration_engine, "Externo maestro cambiado", "EXTERNAL", "999")
    interno = await _worker(migration_engine, "Interno", "INTERNAL", "0")
    tecnica = await _technique(migration_engine)

    # Dos tareas del mismo trabajador: prevalece la foto de la tarea más reciente.
    await _task(
        migration_engine,
        draft,
        externo,
        tecnica,
        worker_type="EXTERNAL",
        daily_rate="120",
        workday_hours="6",
    )
    await _task(
        migration_engine,
        draft,
        externo,
        tecnica,
        worker_type="EXTERNAL",
        daily_rate="150",
        workday_hours="8",
    )
    await _task(
        migration_engine,
        draft,
        interno,
        tecnica,
        worker_type="INTERNAL",
        daily_rate="0",
        workday_hours="8",
    )
    await _task(
        migration_engine,
        confirmed,
        externo,
        tecnica,
        worker_type="EXTERNAL",
        daily_rate="999",
        workday_hours="7",
    )

    _upgrade("0043")
    expected = [
        (externo, "EXTERNAL", Decimal(150), Decimal(8)),
        (interno, "INTERNAL", Decimal(0), Decimal(8)),
    ]
    assert await _snapshots(migration_engine, draft) == expected
    assert await _snapshots(migration_engine, confirmed) == []
    async with migration_engine.connect() as conn:
        origins = list(
            (
                await conn.scalars(
                    text("SELECT assignment_origin FROM v2_quotation_labor ORDER BY id")
                )
            ).all()
        )
        total = await conn.scalar(
            text("SELECT count(*) FROM v2_quotation_workers WHERE v2_quotation_id = :id"),
            {"id": draft},
        )
    assert origins == ["MANUAL", "MANUAL", "MANUAL", "MANUAL"]
    assert total == 2

    with pytest.raises(IntegrityError):
        async with migration_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO v2_quotation_workers"
                    " (v2_quotation_id, worker_id, worker_type_snapshot,"
                    "  daily_rate_snapshot, workday_hours_snapshot)"
                    " VALUES (:quotation_id, :worker_id, 'EXTERNAL', 1, 8)"
                ),
                {"quotation_id": draft, "worker_id": externo},
            )

    result = _alembic("downgrade", "0042")
    assert result.returncode == 0, f"downgrade 0042 fallo:\n{result.stdout}\n{result.stderr}"
    _upgrade("0043")
    assert await _snapshots(migration_engine, draft) == expected
    assert await _snapshots(migration_engine, confirmed) == []
