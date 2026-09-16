"""Correccion 010H — la migracion 0036 contra PostgreSQL real.

1. sube y baja: las cuatro tablas aparecen y la vuelta limpia funciona;
2. un proceso por tecnica y linea, garantizado por la base;
3. el downgrade se niega si alguien ya configuro procesos o adicionales, que
   son decisiones de taller que no viven en ningun otro sitio.
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

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
MIGRATION_DB = "greda_migration_0036"
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
    # S603: comando fijo y argumentos que son revisiones literales.
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


async def _escalar(engine: AsyncEngine, sql: str, parametros: dict[str, object]) -> int:
    async with engine.begin() as connection:
        valor = await connection.scalar(text(sql), parametros)
    assert valor is not None
    return int(valor)


async def _tecnica(engine: AsyncEngine, code: str) -> int:
    return await _escalar(
        engine,
        "INSERT INTO v2_techniques"
        " (code, name, default_capacity_per_workday, unit, created_at, updated_at)"
        " VALUES (:code, :code, 50, 'piezas', now(), now()) RETURNING id",
        {"code": code},
    )


async def _cotizacion(engine: AsyncEngine, code: str) -> int:
    return await _escalar(
        engine,
        "INSERT INTO v2_quotations"
        " (code, pricing_engine_version, status, production_type, created_at, updated_at)"
        " VALUES (:code, 'V2', 'DRAFT', 'RETAIL', now(), now()) RETURNING id",
        {"code": code},
    )


async def _linea(engine: AsyncEngine, cotizacion: int) -> int:
    return await _escalar(
        engine,
        "INSERT INTO v2_quotation_products"
        " (v2_quotation_id, sort_order, product_name_snapshot, quantity, created_at, updated_at)"
        " VALUES (:cotizacion, 0, 'Pieza', 10, now(), now()) RETURNING id",
        {"cotizacion": cotizacion},
    )


class TestSubirYBajar:
    async def test_las_tablas_nuevas_aparecen(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0036")
        async with migration_engine.connect() as connection:
            for tabla in (
                "v2_product_techniques",
                "v2_quotation_processes",
                "v2_extras",
                "v2_quotation_extras",
            ):
                assert await connection.scalar(text(f"SELECT to_regclass('{tabla}')")) is not None
            columna = await connection.scalar(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'v2_quotation_labor'"
                    " AND column_name = 'v2_quotation_process_id'"
                )
            )
        assert columna == "v2_quotation_process_id"

    async def test_un_proceso_por_tecnica_y_linea(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0036")
        cotizacion = await _cotizacion(migration_engine, "CTZ-V2-MIG36-UNICO")
        linea = await _linea(migration_engine, cotizacion)
        tecnica = await _tecnica(migration_engine, "MIG36-TORNO")
        insertar = (
            "INSERT INTO v2_quotation_processes"
            " (v2_quotation_id, v2_quotation_product_id, technique_id, quantity)"
            " VALUES (:c, :l, :t, 10)"
        )
        async with migration_engine.begin() as connection:
            await connection.execute(text(insertar), {"c": cotizacion, "l": linea, "t": tecnica})
        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(insertar), {"c": cotizacion, "l": linea, "t": tecnica}
                )

    async def test_la_vuelta_limpia_funciona(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0036")
        resultado = _alembic("downgrade", "0035")
        assert resultado.returncode == 0, resultado.stdout + resultado.stderr

    async def test_se_niega_si_hay_procesos_configurados(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0036")
        cotizacion = await _cotizacion(migration_engine, "CTZ-V2-MIG36-CONFIG")
        linea = await _linea(migration_engine, cotizacion)
        tecnica = await _tecnica(migration_engine, "MIG36-CONFIG")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO v2_quotation_processes"
                    " (v2_quotation_id, v2_quotation_product_id, technique_id, quantity)"
                    " VALUES (:c, :l, :t, 10)"
                ),
                {"c": cotizacion, "l": linea, "t": tecnica},
            )

        resultado = _alembic("downgrade", "0035")

        assert resultado.returncode != 0
        assert "no puede revertirse" in resultado.stdout + resultado.stderr


async def test_toda_la_cadena_deja_una_sola_cabeza(migration_engine: AsyncEngine) -> None:
    _upgrade("head")
    async with migration_engine.connect() as connection:
        cabezas = list(
            (await connection.scalars(text("SELECT version_num FROM alembic_version"))).all()
        )
    assert cabezas == ["0036"]
