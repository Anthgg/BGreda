"""Fase 010J — la migracion 0038 contra PostgreSQL real.

1. sube: las cotizaciones V2 que ya habia quedan EXCLUSIVAS, sin separacion,
   con carga facturada = hornadas y objetivo = maximo: sus numeros no cambian;
2. las cotizaciones nuevas nacen COMPARTIDAS y con 3 cm de separacion;
3. los CHECK nuevos muerden;
4. bajar funciona si nadie uso las reglas nuevas y se niega si alguien las uso;
5. la cadena entera deja una sola cabeza.
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.session import normalize_database_url

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
MIGRATION_DB = "greda_migration_0038"
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


async def _cotizacion_de_010e(engine: AsyncEngine, codigo: str) -> int:
    """Una cotizacion V2 de antes de 0038: horno, dos hornadas enteras, 900 de quema.

    Sembrada en SQL: una prueba de migracion tiene que poder sembrar una base
    que todavia no conoce el codigo de hoy.
    """
    horno = await _escalar(
        engine,
        "INSERT INTO kilns (code, name, capacity_volume_cm3, created_at, updated_at)"
        " VALUES (:codigo, :codigo, 17000, now(), now()) RETURNING id",
        {"codigo": f"K-{codigo}"},
    )
    return await _escalar(
        engine,
        "INSERT INTO v2_quotations"
        " (code, pricing_engine_version, status, production_type, kiln_id,"
        "  kiln_capacity_snapshot, firing_total_volume_cm3, firing_occupancy_percent,"
        "  firing_count, low_fire_enabled, high_fire_enabled, low_fire_count,"
        "  high_fire_count, firing_gas_total, firing_commercial_total,"
        "  commercial_factor, commercial_factor_min_snapshot,"
        "  commercial_factor_max_snapshot, created_at, updated_at)"
        " VALUES (:codigo, 'V2', 'DRAFT', 'RETAIL', :horno, 17000, 27200, 160, 2,"
        "  true, true, 2, 2, 210, 900, 3, 2, 3, now(), now()) RETURNING id",
        {"codigo": codigo, "horno": horno},
    )


async def _fila(engine: AsyncEngine, quotation_id: int) -> dict[str, object]:
    async with engine.connect() as connection:
        fila = (
            await connection.execute(
                text(
                    "SELECT firing_mode, piece_separation_cm_snapshot, firing_billed_load,"
                    " firing_count, commercial_factor_target_snapshot,"
                    " firing_commercial_total, firing_gas_total"
                    " FROM v2_quotations WHERE id = :id"
                ),
                {"id": quotation_id},
            )
        ).one()
    return dict(fila._mapping)


async def test_las_cotizaciones_que_ya_habia_no_cambian_de_numero(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("0037")
    vieja = await _cotizacion_de_010e(migration_engine, "CTZ-V2-OLD-1")
    _upgrade("0038")

    fila = await _fila(migration_engine, vieja)
    assert fila["firing_mode"] == "EXCLUSIVE"
    assert fila["piece_separation_cm_snapshot"] == Decimal(0)
    assert fila["firing_billed_load"] == Decimal(2)
    assert fila["firing_count"] == 2
    assert fila["commercial_factor_target_snapshot"] == Decimal(3)
    assert fila["firing_commercial_total"] == Decimal(900)
    assert fila["firing_gas_total"] == Decimal(210)


async def test_las_nuevas_nacen_compartidas_con_tres_centimetros(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("0038")
    nueva = await _escalar(
        migration_engine,
        "INSERT INTO v2_quotations"
        " (code, pricing_engine_version, status, production_type, created_at, updated_at)"
        " VALUES ('CTZ-V2-NEW-1', 'V2', 'DRAFT', 'RETAIL', now(), now()) RETURNING id",
        {},
    )
    fila = await _fila(migration_engine, nueva)
    assert fila["firing_mode"] == "SHARED"
    assert fila["piece_separation_cm_snapshot"] == Decimal(3)
    async with migration_engine.connect() as connection:
        separacion = await connection.scalar(
            text("SELECT piece_separation_cm FROM v2_commercial_settings")
        )
    # La fila de configuracion la siembra una migracion anterior; si existe, lleva 3.
    assert separacion in (None, Decimal(3))


@pytest.mark.parametrize(
    ("columna", "valor"),
    [
        ("firing_mode", "'HALF'"),
        ("piece_separation_cm_snapshot", "21"),
        ("piece_separation_cm_snapshot", "-1"),
        ("firing_billed_load", "-1"),
        ("commercial_factor_target_snapshot", "1.5"),
    ],
)
async def test_los_check_nuevos_muerden(
    migration_engine: AsyncEngine, columna: str, valor: str
) -> None:
    _upgrade("0038")
    fila = await _escalar(
        migration_engine,
        "INSERT INTO v2_quotations"
        " (code, pricing_engine_version, status, production_type, created_at, updated_at)"
        " VALUES ('CTZ-V2-CK', 'V2', 'DRAFT', 'RETAIL', now(), now()) RETURNING id",
        {},
    )
    with pytest.raises(IntegrityError):
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(f"UPDATE v2_quotations SET {columna} = {valor} WHERE id = :id"),  # noqa: S608
                {"id": fila},
            )


async def test_bajar_sin_reglas_nuevas_conserva_los_numeros(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("0037")
    vieja = await _cotizacion_de_010e(migration_engine, "CTZ-V2-OLD-2")
    _upgrade("0038")
    resultado = _alembic("downgrade", "0037")
    assert resultado.returncode == 0, resultado.stderr
    async with migration_engine.connect() as connection:
        total = await connection.scalar(
            text("SELECT firing_commercial_total FROM v2_quotations WHERE id = :id"),
            {"id": vieja},
        )
        columna = await connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_name = 'v2_quotations' AND column_name = 'firing_mode'"
            )
        )
    assert total == Decimal(900)
    assert columna == 0


async def test_bajar_se_niega_si_alguien_uso_la_quema_compartida(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("0038")
    await _escalar(
        migration_engine,
        "INSERT INTO v2_quotations"
        " (code, pricing_engine_version, status, production_type, created_at, updated_at)"
        " VALUES ('CTZ-V2-SHARED', 'V2', 'DRAFT', 'RETAIL', now(), now()) RETURNING id",
        {},
    )
    resultado = _alembic("downgrade", "0037")
    assert resultado.returncode != 0
    assert "No se puede bajar de 0038" in resultado.stderr + resultado.stdout
    async with migration_engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0038"


async def test_toda_la_cadena_deja_una_sola_cabeza(
    migration_engine: AsyncEngine,
) -> None:
    """La cabeza se la lleva la migracion mas nueva; esa afirmacion vive en 0039."""
    _upgrade("head")
    async with migration_engine.connect() as connection:
        cabezas = list(
            (await connection.scalars(text("SELECT version_num FROM alembic_version"))).all()
        )
    assert len(cabezas) == 1
