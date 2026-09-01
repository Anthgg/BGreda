"""Fase 009H — la migracion 0019 se ejecuta de verdad contra PostgreSQL.

Lo que de verdad se prueba aqui no son las dos columnas nuevas sino el hueco:
que una fila creada ANTES de la migracion siga diciendo «no se» y no «impaga».
`ADD COLUMN ... DEFAULT` en una sola sentencia habria convertido 347
cotizaciones en impagas sin que nadie lo pidiera, y el dato inventado no se
distinguiria despues de uno real.
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
MIGRATION_DB = "greda_migration_0019"
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
    # S603: comando fijo y argumentos literales de esta prueba.
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


COLUMNAS_PROPIAS = ("code", "status", "workflow")


def _insert_sql(extra: dict[str, str] | None = None) -> str:
    relleno = {
        nombre: valor
        for nombre, valor in _columnas_obligatorias().items()
        if nombre not in COLUMNAS_PROPIAS
    }
    columnas = [*COLUMNAS_PROPIAS, *relleno, *(extra or {})]
    valores = [":code", ":status", "'COTIZADOR'", *relleno.values(), *(extra or {}).values()]
    # S608: nombres derivados del modelo, valores literales de esta prueba.
    return (
        f"INSERT INTO quotations ({', '.join(columnas)}) "  # noqa: S608
        f"VALUES ({', '.join(valores)})"
    )


async def _insertar(
    engine: AsyncEngine, *, code: str, status: str = "DRAFT", extra: dict[str, str] | None = None
) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(_insert_sql(extra)), {"code": code, "status": status})


@pytest.mark.asyncio
async def test_0018_a_0019_a_0018_y_de_vuelta(migration_engine: AsyncEngine) -> None:
    """La ida, la vuelta y la ida otra vez, sin residuos."""
    _upgrade("0018")
    assert await _current(migration_engine) == "0018"
    assert not await _column_exists(migration_engine, "quotations", "payment_status")

    _upgrade("0019")
    assert await _current(migration_engine) == "0019"
    assert await _column_exists(migration_engine, "quotations", "payment_status")
    assert await _column_exists(migration_engine, "quotations", "paid_at")

    # `status` no se toca: pagar no es un cuarto estado comercial. Se busca por
    # tabla y contenido, no por un nombre de restriccion que puede variar con
    # la convencion: un nombre equivocado devuelve NULL y la prueba pasaria por
    # no encontrar nada.
    permitidos = await _scalar(
        migration_engine,
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE t.relname = 'quotations' AND c.contype = 'c' "
        "AND pg_get_constraintdef(c.oid) LIKE '%DRAFT%'",
    )
    assert permitidos is not None, "no se encontro el CHECK del estado comercial"
    assert "DRAFT" in str(permitidos) and "CONFIRMED" in str(permitidos)
    assert "PAID" not in str(permitidos), "pagar no puede haberse colado en `status`"

    _downgrade("0018")
    assert await _current(migration_engine) == "0018"
    assert not await _column_exists(migration_engine, "quotations", "payment_status")
    assert not await _column_exists(migration_engine, "quotations", "paid_at")
    # Lo de 009G sigue en pie tras el downgrade.
    assert await _column_exists(migration_engine, "quotations", "validity_days_snapshot")

    _upgrade("0019")
    assert await _current(migration_engine) == "0019"


@pytest.mark.asyncio
async def test_una_fila_anterior_a_0019_no_queda_como_impaga(
    migration_engine: AsyncEngine,
) -> None:
    """MIGRATION_DOES_NOT_INVENT_HISTORICAL_PAYMENT_STATE.

    La prueba central de 009H. La fila se crea ANTES de la migracion, cuando el
    sistema no sabia nada de pagos, y despues de migrar tiene que seguir
    diciendo «no se»: NULL, no UNPAID.

    Si algun dia alguien junta el ADD COLUMN y el DEFAULT en una sola sentencia,
    esta prueba se pone roja y explica por que.
    """
    _upgrade("0018")
    await _insertar(migration_engine, code="CTZ-HISTORICA", status="CONFIRMED")

    _upgrade("0019")

    fila = await _scalar(
        migration_engine,
        "SELECT payment_status FROM quotations WHERE code = 'CTZ-HISTORICA'",
    )
    assert fila is None, "una cotizacion anterior a 009H no puede declararse impaga"
    assert (
        await _scalar(
            migration_engine, "SELECT paid_at FROM quotations WHERE code = 'CTZ-HISTORICA'"
        )
    ) is None


@pytest.mark.asyncio
async def test_una_fila_nueva_nace_impaga(migration_engine: AsyncEngine) -> None:
    """NEW_QUOTATION_DEFAULTS_UNPAID.

    La contraparte: lo creado DESPUES si entra al flujo nuevo, y de una
    cotizacion nueva si se sabe que aun no se ha cobrado.
    """
    _upgrade("0019")
    await _insertar(migration_engine, code="CTZ-NUEVA")

    assert (
        await _scalar(
            migration_engine, "SELECT payment_status FROM quotations WHERE code = 'CTZ-NUEVA'"
        )
    ) == "UNPAID"
    assert (
        await _scalar(migration_engine, "SELECT paid_at FROM quotations WHERE code = 'CTZ-NUEVA'")
    ) is None


@pytest.mark.asyncio
async def test_los_tres_estados_coherentes_se_aceptan(migration_engine: AsyncEngine) -> None:
    _upgrade("0019")

    await _insertar(
        migration_engine,
        code="CTZ-SIN-REGISTRO",
        extra={"payment_status": "NULL", "paid_at": "NULL"},
    )
    await _insertar(
        migration_engine, code="CTZ-IMPAGA", extra={"payment_status": "'UNPAID'", "paid_at": "NULL"}
    )
    await _insertar(
        migration_engine,
        code="CTZ-PAGADA",
        extra={"payment_status": "'PAID'", "paid_at": "now()"},
    )

    assert await _scalar(migration_engine, "SELECT count(*) FROM quotations") == 3


@pytest.mark.parametrize(
    ("caso", "estado", "fecha"),
    [
        # Pagada sin fecha: un cobro sin cuando.
        ("PAGADA_SIN_FECHA", "'PAID'", "NULL"),
        # Impaga con fecha de cobro: una fecha de algo que no ocurrio.
        ("IMPAGA_CON_FECHA", "'UNPAID'", "now()"),
        # Sin registro pero con fecha: lo mismo, por otra puerta.
        ("SIN_REGISTRO_CON_FECHA", "NULL", "now()"),
        # Un estado que no existe.
        ("ESTADO_INVENTADO", "'PAGADA'", "now()"),
    ],
)
@pytest.mark.asyncio
async def test_las_combinaciones_incoherentes_se_rechazan(
    migration_engine: AsyncEngine, caso: str, estado: str, fecha: str
) -> None:
    _upgrade("0019")

    with pytest.raises(IntegrityError):
        await _insertar(
            migration_engine,
            code=f"CTZ-{caso}",
            extra={"payment_status": estado, "paid_at": fecha},
        )
