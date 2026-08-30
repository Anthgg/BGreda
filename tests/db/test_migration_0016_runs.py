"""Fase 009E — la migracion 0016 se ejecuta de verdad contra PostgreSQL.

Como en 0015, no basta con que la base de test se cree desde los modelos: eso
no ejecuta ni una linea de la migracion. Aqui se corre alembic en un
subproceso contra una base propia, 0015 -> 0016 y la vuelta, y se comprueba el
esquema resultante con SELECT reales.

El backfill se comprueba con un SELECT, no confiando en `server_default`: son
dos mecanismos distintos y solo uno demuestra que la fila que YA existia quedo
con el valor correcto.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.session import normalize_database_url

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
#: Base propia, distinta de la de 0015, para que ambas puedan correr a la vez.
MIGRATION_DB = "greda_migration_0016"
REPO_ROOT = Path(__file__).parents[2]

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL no definida: se omiten las pruebas con base de datos",
)


def _url_for(database: str) -> str:
    parts = urlsplit(normalize_database_url(TEST_DATABASE_URL))
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "DATABASE_URL": _url_for(MIGRATION_DB)}
    # S603: comando fijo (el propio interprete y alembic) y argumentos que son
    # revisiones literales de esta prueba. No hay entrada de usuario.
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


def _downgrade(revision: str) -> None:
    result = _alembic("downgrade", revision)
    assert result.returncode == 0, f"downgrade {revision} fallo:\n{result.stdout}\n{result.stderr}"


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


async def _scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as connection:
        return await connection.scalar(text(sql), params)


async def _column_exists(engine: AsyncEngine, table: str, column: str) -> bool:
    found = await _scalar(
        engine,
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = :column",
        table=table,
        column=column,
    )
    return found is not None


async def _check_clause(engine: AsyncEngine, name: str) -> str | None:
    clause = await _scalar(
        engine,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name",
        name=name,
    )
    return str(clause) if clause is not None else None


async def _current(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        exists = await connection.scalar(text("SELECT to_regclass('alembic_version')"))
        if exists is None:
            return None
        return await connection.scalar(text("SELECT version_num FROM alembic_version"))


async def _settings_row(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT * FROM commercial_settings WHERE id = 1"))
        row = result.mappings().one()
    return dict(row)


@pytest.mark.asyncio
async def test_0015_a_0016_a_0015_y_de_vuelta(migration_engine: AsyncEngine) -> None:
    """La ida, la vuelta y la ida otra vez. Sin residuos entre medias."""
    _upgrade("0015")
    assert await _current(migration_engine) == "0015"
    assert not await _column_exists(
        migration_engine, "commercial_settings", "production_factor_default"
    )
    antes = await _settings_row(migration_engine)

    # ---- 0015 -> 0016 -------------------------------------------------
    _upgrade("0016")
    assert await _current(migration_engine) == "0016"
    assert await _column_exists(
        migration_engine, "commercial_settings", "production_factor_default"
    )
    assert await _column_exists(migration_engine, "commercial_settings", "rounding_step")

    # ---- Backfill comprobado con SELECT, no con server_default --------
    despues = await _settings_row(migration_engine)
    assert Decimal(str(despues["production_factor_default"])) == Decimal(3)
    assert Decimal(str(despues["rounding_step"])) == Decimal("0.50")

    # ---- El resto de la fila queda intacto -----------------------------
    for campo, valor in antes.items():
        assert despues[campo] == valor, f"0016 no debe tocar {campo}"

    # ---- Los CHECK existen de verdad ----------------------------------
    factor_check = await _check_clause(
        migration_engine, "ck_commercial_settings_production_factor_default_positive"
    )
    assert factor_check is not None and "> 0" in factor_check
    paso_check = await _check_clause(
        migration_engine, "ck_commercial_settings_rounding_step_allowed"
    )
    assert paso_check is not None
    assert "0.50" in paso_check and "1.00" in paso_check

    # ---- 0016 -> 0015 -> 0016 -----------------------------------------
    _downgrade("0015")
    assert await _current(migration_engine) == "0015"
    assert not await _column_exists(
        migration_engine, "commercial_settings", "production_factor_default"
    )
    assert not await _column_exists(migration_engine, "commercial_settings", "rounding_step")

    _upgrade("0016")
    assert await _current(migration_engine) == "0016"
    final = await _settings_row(migration_engine)
    assert Decimal(str(final["production_factor_default"])) == Decimal(3)
    assert Decimal(str(final["rounding_step"])) == Decimal("0.50")


@pytest.mark.asyncio
async def test_el_check_del_paso_de_redondeo_rechaza_un_tercer_valor(
    migration_engine: AsyncEngine,
) -> None:
    """El CHECK vive en la base, no solo en Pydantic.

    Es el unico sitio por el que no se puede colar un 0,25: un paso distinto
    produciria precios que no son multiplos de nada.
    """
    _upgrade("0016")

    async with migration_engine.begin() as connection:
        with pytest.raises(Exception, match="rounding_step_allowed"):
            await connection.execute(
                text("UPDATE commercial_settings SET rounding_step = 0.25 WHERE id = 1")
            )

    # Y el factor no admite cero ni negativos.
    async with migration_engine.begin() as connection:
        with pytest.raises(Exception, match="production_factor_default_positive"):
            await connection.execute(
                text("UPDATE commercial_settings SET production_factor_default = 0 WHERE id = 1")
            )


@pytest.mark.asyncio
async def test_0016_no_deja_una_sola_cabeza_rota(migration_engine: AsyncEngine) -> None:
    """`alembic upgrade head` tiene que llegar sola a 0016."""
    _upgrade("head")
    assert await _current(migration_engine) == "0016"
    heads = _alembic("heads")
    assert heads.returncode == 0, heads.stderr
    assert heads.stdout.count("(head)") == 1, heads.stdout
