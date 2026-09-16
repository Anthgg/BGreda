"""Correccion 010H — la migracion 0035 contra PostgreSQL real.

1. lo ya cotizado queda habilitado —un borrador no encalla— y no se inventa
   ninguna capacidad para quien no tiene historial;
2. un par trabajador-tecnica no se repite;
3. el downgrade vuelve limpio si solo hay backfill y se niega si alguien ya
   configuro capacidades a mano.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.session import normalize_database_url

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
MIGRATION_DB = "greda_migration_0035"
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


async def _trabajador(engine: AsyncEngine, nombre: str) -> int:
    return await _escalar(
        engine,
        "INSERT INTO v2_workers (name, worker_type, daily_rate, created_at, updated_at)"
        " VALUES (:nombre, 'INTERNAL', 120, now(), now()) RETURNING id",
        {"nombre": nombre},
    )


async def _tecnica(engine: AsyncEngine, code: str) -> int:
    return await _escalar(
        engine,
        "INSERT INTO v2_techniques"
        " (code, name, default_capacity_per_workday, unit, created_at, updated_at)"
        " VALUES (:code, :code, 50, 'piezas', now(), now()) RETURNING id",
        {"code": code},
    )


async def _tarea(engine: AsyncEngine, worker: int, tecnica: int) -> None:
    cotizacion = await _escalar(
        engine,
        "INSERT INTO v2_quotations"
        " (code, pricing_engine_version, status, production_type, created_at, updated_at)"
        " VALUES (:code, 'V2', 'DRAFT', 'RETAIL', now(), now()) RETURNING id",
        {"code": f"CTZ-V2-MIG35-{uuid.uuid4().hex[:8]}"},
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v2_quotation_labor"
                " (v2_quotation_id, worker_id, worker_name_snapshot, worker_type_snapshot,"
                "  daily_rate_snapshot, workday_hours_snapshot, hourly_rate_snapshot,"
                "  technique_id, technique_name_snapshot, technique_unit_snapshot,"
                "  standard_capacity_snapshot, quantity, calculated_hours, final_hours,"
                "  labor_cost, created_at, updated_at)"
                " VALUES (:cotizacion, :worker, 'Quien sea', 'INTERNAL', 120, 8, 15,"
                "  :tecnica, 'Lo que sea', 'piezas', 50, 50, 8, 8, 120, now(), now())"
            ),
            {"cotizacion": cotizacion, "worker": worker, "tecnica": tecnica},
        )


class TestBackfill:
    async def test_lo_ya_cotizado_queda_habilitado_y_nada_mas(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0034")
        tornero = await _trabajador(migration_engine, "Tornero viejo")
        sin_historial = await _trabajador(migration_engine, "Nuevo")
        torno = await _tecnica(migration_engine, "MIG35-TORNO")
        await _tecnica(migration_engine, "MIG35-ASAS")
        await _tarea(migration_engine, tornero, torno)
        await _tarea(migration_engine, tornero, torno)

        _upgrade("0035")

        async with migration_engine.connect() as connection:
            filas = (
                await connection.execute(
                    text("SELECT worker_id, technique_id, active FROM v2_worker_techniques")
                )
            ).all()
            manuales = await connection.scalar(
                text("SELECT count(*) FROM v2_techniques WHERE manual_hours")
            )
        assert [(f.worker_id, f.technique_id, f.active) for f in filas] == [(tornero, torno, True)]
        assert sin_historial not in [f.worker_id for f in filas]
        assert manuales == 0

    async def test_un_par_no_se_repite(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0035")
        worker = await _trabajador(migration_engine, "Unico")
        tecnica = await _tecnica(migration_engine, "MIG35-UNICO")
        insertar = "INSERT INTO v2_worker_techniques (worker_id, technique_id) VALUES (:w, :t)"
        async with migration_engine.begin() as connection:
            await connection.execute(text(insertar), {"w": worker, "t": tecnica})
        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(text(insertar), {"w": worker, "t": tecnica})


class TestDowngrade:
    async def test_vuelta_limpia_con_solo_el_backfill(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0034")
        worker = await _trabajador(migration_engine, "Vuelta")
        tecnica = await _tecnica(migration_engine, "MIG35-VUELTA")
        await _tarea(migration_engine, worker, tecnica)
        _upgrade("0035")
        resultado = _alembic("downgrade", "0034")
        assert resultado.returncode == 0, resultado.stdout + resultado.stderr

    async def test_se_niega_con_capacidades_configuradas(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0035")
        worker = await _trabajador(migration_engine, "Configurado")
        tecnica = await _tecnica(migration_engine, "MIG35-CONFIG")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO v2_worker_techniques (worker_id, technique_id) VALUES (:w, :t)"),
                {"w": worker, "t": tecnica},
            )
        resultado = _alembic("downgrade", "0034")
        assert resultado.returncode != 0
        assert "no puede revertirse" in resultado.stdout + resultado.stderr


async def test_0035_se_aplica_dentro_de_la_cadena(migration_engine: AsyncEngine) -> None:
    """Subir hasta la cabeza deja UNA sola version sellada y 0035 ya aplicada.

    La cabeza se la lleva la migracion mas nueva —hoy 0036—, asi que fijar aqui
    un numero concreto convertiria esta prueba en una alarma que suena cada vez
    que alguien anade una migracion. Lo que 0035 tiene que garantizar es que su
    tabla existe despues de subir del todo.
    """
    _upgrade("head")
    async with migration_engine.connect() as connection:
        cabezas = list(
            (await connection.scalars(text("SELECT version_num FROM alembic_version"))).all()
        )
        existe = await connection.scalar(text("SELECT to_regclass('v2_worker_techniques')"))
    assert len(cabezas) == 1
    assert existe is not None
