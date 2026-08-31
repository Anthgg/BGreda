"""Fase 009G — la migracion 0018 se ejecuta de verdad contra PostgreSQL.

Como 0015, 0016 y 0017: alembic real en un subproceso contra una base propia,
la ida y la vuelta, y el esquema comprobado con SELECT e INSERT reales.

Lo que se comprueba aqui, mas alla de que la columna aparezca, es que el CHECK
de rango rechaza lo que la configuracion tambien rechazaria. Un plazo imposible
guardado en el snapshot no rompe nada el dia que se escribe: rompe cuando
alguien regenera el PDF de una cotizacion firmada y encuentra una vigencia que
la pantalla de configuracion nunca habria dejado teclear.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.session import normalize_database_url
from tests.db.test_migration_0017_runs import _columnas_obligatorias

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
#: Base propia, distinta de las de las migraciones anteriores, para que puedan
#: correr a la vez sin pisarse.
MIGRATION_DB = "greda_migration_0018"
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


async def _current(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        exists = await connection.scalar(text("SELECT to_regclass('alembic_version')"))
        if exists is None:
            return None
        return await connection.scalar(text("SELECT version_num FROM alembic_version"))


#: Lo que esta prueba SI decide. El resto de columnas obligatorias se rellena
#: solo, derivado del modelo, para que una columna nueva no rompa el arnes.
COLUMNAS_PROPIAS = (
    "code",
    "status",
    "workflow",
    "currency_code_snapshot",
    "validity_days_snapshot",
)


def _insert_sql() -> str:
    relleno = {
        nombre: valor
        for nombre, valor in _columnas_obligatorias().items()
        if nombre not in COLUMNAS_PROPIAS
    }
    columnas = [*COLUMNAS_PROPIAS, *relleno]
    valores = [":code", "'DRAFT'", "'COTIZADOR'", "'PEN'", ":dias", *relleno.values()]
    # S608: los nombres salen del modelo y los valores son literales de esta
    # prueba. No hay entrada de usuario.
    return (
        f"INSERT INTO quotations ({', '.join(columnas)}) "  # noqa: S608
        f"VALUES ({', '.join(valores)})"
    )


NUEVA_COTIZACION = _insert_sql()


async def _insertar(engine: AsyncEngine, *, code: str, dias: int | None) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(NUEVA_COTIZACION), {"code": code, "dias": dias})


@pytest.mark.asyncio
async def test_0017_a_0018_a_0017_y_de_vuelta(migration_engine: AsyncEngine) -> None:
    """La ida, la vuelta y la ida otra vez. Sin residuos entre medias."""
    _upgrade("0017")
    assert await _current(migration_engine) == "0017"
    assert not await _column_exists(migration_engine, "quotations", "validity_days_snapshot")

    # ---- 0017 -> 0018 -------------------------------------------------
    _upgrade("0018")
    assert await _current(migration_engine) == "0018"
    assert await _column_exists(migration_engine, "quotations", "validity_days_snapshot")

    # Nullable a proposito: es lo que distingue «no se capturo» de un plazo.
    nullable = await _scalar(
        migration_engine,
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'quotations' AND column_name = 'validity_days_snapshot'",
    )
    assert nullable == "YES"

    # Y sin default: un default convertiria en dato lo que deberia ser un hueco.
    por_omision = await _scalar(
        migration_engine,
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'quotations' AND column_name = 'validity_days_snapshot'",
    )
    assert por_omision is None

    # ---- 0018 -> 0017 -> 0018 -----------------------------------------
    _downgrade("0017")
    assert await _current(migration_engine) == "0017"
    assert not await _column_exists(migration_engine, "quotations", "validity_days_snapshot")
    # Lo que congelo 009F sigue en pie: bajar 0018 no se lo lleva por delante.
    assert await _column_exists(migration_engine, "quotations", "exchange_rate_snapshot")
    assert await _column_exists(migration_engine, "quotations", "currency_code_snapshot")

    _upgrade("0018")
    assert await _current(migration_engine) == "0018"


@pytest.mark.asyncio
async def test_el_hueco_y_los_plazos_reales_se_aceptan(migration_engine: AsyncEngine) -> None:
    """NULL es un estado legitimo, no un fallo de captura.

    Las confirmadas anteriores a 009G quedan asi, y el CHECK tiene que
    admitirlas tal cual: no hay rastro de su vigencia y no se les va a inventar.
    """
    _upgrade("0018")

    await _insertar(migration_engine, code="CTZ-SIN-VIGENCIA", dias=None)
    await _insertar(migration_engine, code="CTZ-VEINTE", dias=20)
    await _insertar(migration_engine, code="CTZ-LIMITE", dias=3650)

    guardado = await _scalar(
        migration_engine,
        "SELECT validity_days_snapshot FROM quotations WHERE code = 'CTZ-VEINTE'",
    )
    assert guardado == 20

    hueco = await _scalar(
        migration_engine,
        "SELECT validity_days_snapshot FROM quotations WHERE code = 'CTZ-SIN-VIGENCIA'",
    )
    assert hueco is None


@pytest.mark.parametrize(
    ("caso", "dias"),
    [
        # Cero no es «sin vigencia», es una vigencia de cero dias: un documento
        # que nace vencido. Para «sin vigencia» ya existe NULL.
        ("CERO", 0),
        ("NEGATIVO", -1),
        # El tope es el mismo de `commercial_settings`: la copia no puede ser
        # mas permisiva que el ajuste del que se copia.
        ("PASADO_EL_TOPE", 3651),
    ],
)
@pytest.mark.asyncio
async def test_los_plazos_imposibles_se_rechazan(
    migration_engine: AsyncEngine, caso: str, dias: int
) -> None:
    _upgrade("0018")

    with pytest.raises(IntegrityError) as error:
        await _insertar(migration_engine, code=f"CTZ-{caso}", dias=dias)
    assert "validity_days_snapshot" in str(error.value)
