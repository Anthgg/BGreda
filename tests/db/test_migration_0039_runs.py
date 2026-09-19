"""Fase 010K — la migracion 0039 contra PostgreSQL real.

1. sube: talonario Q-V2, factor x1,00 por defecto y las tablas de Solo Quema;
2. el CHECK del factor muerde fuera de [1; 2] y el de coherencia de estados tambien;
3. el CHECK de 0038 con nombre recortado queda con su nombre corto;
4. bajar funciona sin datos y se niega si hay servicios de Solo Quema;
5. la cadena entera deja una sola cabeza, y es 0039.
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
MIGRATION_DB = "greda_migration_0039"
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


async def _servicio(engine: AsyncEngine, codigo: str, factor: str = "1") -> int:
    return await _escalar(
        engine,
        "INSERT INTO v2_firing_quotations (code, status, factor, created_at, updated_at)"
        " VALUES (:codigo, 'DRAFT', :factor, now(), now()) RETURNING id",
        {"codigo": codigo, "factor": Decimal(factor)},
    )


async def test_sube_con_su_talonario_y_su_factor(migration_engine: AsyncEngine) -> None:
    _upgrade("0039")
    async with migration_engine.connect() as connection:
        talonario = (
            await connection.execute(
                text(
                    "SELECT prefix, pattern, padding FROM document_sequences"
                    " WHERE sequence_type = 'FIRING_V2'"
                )
            )
        ).one()
        restricciones = set(
            (
                await connection.scalars(
                    text(
                        "SELECT conname FROM pg_constraint"
                        " WHERE conrelid = 'v2_quotation_products'::regclass"
                    )
                )
            ).all()
        )
    assert tuple(talonario) == ("Q-V2", "{PREFIX}-{YYYY}-{NUMBER}", 6)
    assert "ck_v2_quotation_products_line_illustration_qty_non_negative" in restricciones
    assert "ck_v2_quotation_products_line_illustration_quantity_non_f4ba" not in restricciones
    servicio = await _servicio(migration_engine, "Q-V2-TEST-1")
    async with migration_engine.connect() as connection:
        fila = (
            await connection.execute(
                text(
                    "SELECT factor, firing_mode, piece_separation_cm, customer_kind,"
                    " low_fire_enabled, high_fire_enabled, glaze_enabled"
                    " FROM v2_firing_quotations WHERE id = :id"
                ),
                {"id": servicio},
            )
        ).one()
    assert tuple(fila) == (Decimal(1), "SHARED", Decimal(3), "EXTERNAL", True, True, False)


@pytest.mark.parametrize(
    ("factor", "valido"),
    [("1", True), ("1.17", True), ("2", True), ("0.99", False), ("2.01", False)],
)
async def test_el_factor_se_limita_en_la_base(
    migration_engine: AsyncEngine, factor: str, valido: bool
) -> None:
    _upgrade("0039")
    if valido:
        assert await _servicio(migration_engine, f"Q-F-{factor}", factor) > 0
    else:
        with pytest.raises(IntegrityError):
            await _servicio(migration_engine, f"Q-F-{factor}", factor)


async def test_una_emitida_sin_huella_no_entra(migration_engine: AsyncEngine) -> None:
    _upgrade("0039")
    with pytest.raises(IntegrityError):
        await _escalar(
            migration_engine,
            "INSERT INTO v2_firing_quotations (code, status, issued_at, created_at, updated_at)"
            " VALUES ('Q-V2-SIN-HUELLA', 'CONFIRMED', now(), now(), now()) RETURNING id",
            {},
        )


async def test_bajar_sin_datos_funciona_y_con_datos_se_niega(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("0039")
    limpio = _alembic("downgrade", "0038")
    assert limpio.returncode == 0, limpio.stderr
    _upgrade("0039")
    await _servicio(migration_engine, "Q-V2-TEST-2")
    resultado = _alembic("downgrade", "0038")
    assert resultado.returncode != 0
    assert "No se puede bajar de 0039" in resultado.stderr + resultado.stdout
    async with migration_engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0039"


async def test_toda_la_cadena_deja_una_sola_cabeza_y_es_0039(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("head")
    async with migration_engine.connect() as connection:
        cabezas = list(
            (await connection.scalars(text("SELECT version_num FROM alembic_version"))).all()
        )
    assert cabezas == ["0039"]
