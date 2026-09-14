"""Fase 010H — la migracion 0034 se ejecuta de verdad contra PostgreSQL.

Lo que hay que ver funcionando:

1. que un borrador de 010G llega intacto y sigue siendo borrador, sin fecha de
   emision inventada;
2. que el CHECK de ciclo de vida muerde: una emitida sin vencimiento, un
   borrador con fecha de emision, una cancelada sin fecha de cancelacion;
3. que el indice parcial admite un solo borrador abierto por origen, y otro en
   cuanto el primero deja de ser borrador;
4. que el puente a produccion es unico por cotizacion;
5. que el upgrade se niega si ya hay no-borradores y el downgrade si ya hay
   algo emitido; y que la vuelta limpia funciona sin nada comprometido.
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
MIGRATION_DB = "greda_migration_0034"
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


async def _cotizacion_v2(engine: AsyncEngine, codigo: str) -> int:
    async with engine.begin() as connection:
        identificador = await connection.scalar(
            text(
                "INSERT INTO v2_quotations"
                " (code, pricing_engine_version, status, production_type,"
                "  created_at, updated_at)"
                " VALUES (:codigo, 'V2', 'DRAFT', 'RETAIL', now(), now())"
                " RETURNING id"
            ),
            {"codigo": codigo},
        )
    assert identificador is not None
    return int(identificador)


EMITIR = (
    "UPDATE v2_quotations SET status = 'CONFIRMED', issued_at = now(),"
    " valid_until = current_date + 20, expires_at = now() + interval '21 days',"
    " commercial_fingerprint = repeat('a', 64) WHERE id = :id"
)


async def _falla(engine: AsyncEngine, sql: str, parametros: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(text(sql), parametros)


class TestHistoricos:
    async def test_un_borrador_de_010g_sigue_siendo_borrador(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0033")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-010G")
        _upgrade("0034")
        async with migration_engine.connect() as connection:
            fila = (
                await connection.execute(
                    text(
                        "SELECT status, issued_at, valid_until, expires_at, cancelled_at,"
                        " duplicated_from_id FROM v2_quotations WHERE id = :id"
                    ),
                    {"id": cotizacion},
                )
            ).one()
        assert fila.status == "DRAFT"
        assert fila.issued_at is None and fila.valid_until is None and fila.expires_at is None
        assert fila.cancelled_at is None and fila.duplicated_from_id is None

    async def test_el_upgrade_se_niega_con_no_borradores(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0033")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-A-MANO")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text("UPDATE v2_quotations SET status = 'CONFIRMED' WHERE id = :id"),
                {"id": cotizacion},
            )
        resultado = _alembic("upgrade", "0034")
        assert resultado.returncode != 0
        assert "no puede aplicarse" in resultado.stdout + resultado.stderr


class TestCheckDeCicloDeVida:
    async def test_emitir_bien_pasa(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0034")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-BIEN")
        async with migration_engine.begin() as connection:
            await connection.execute(text(EMITIR), {"id": cotizacion})

    async def test_emitida_sin_vencimiento_no(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0034")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-SIN-VENCE")
        await _falla(
            migration_engine,
            "UPDATE v2_quotations SET status = 'CONFIRMED', issued_at = now(),"
            " valid_until = current_date, commercial_fingerprint = repeat('a', 64)"
            " WHERE id = :id",
            {"id": cotizacion},
        )

    async def test_borrador_con_emision_no(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0034")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-BORR-EMI")
        await _falla(
            migration_engine,
            "UPDATE v2_quotations SET issued_at = now() WHERE id = :id",
            {"id": cotizacion},
        )

    async def test_cancelada_sin_fecha_no(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0034")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-CANC")
        await _falla(
            migration_engine,
            "UPDATE v2_quotations SET status = 'CANCELLED' WHERE id = :id",
            {"id": cotizacion},
        )

    async def test_vence_antes_de_emitirse_no(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0034")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-AL-REVES")
        await _falla(
            migration_engine,
            "UPDATE v2_quotations SET status = 'CONFIRMED', issued_at = now(),"
            " valid_until = current_date, expires_at = now() - interval '1 day',"
            " commercial_fingerprint = repeat('a', 64) WHERE id = :id",
            {"id": cotizacion},
        )


class TestUnicidad:
    async def test_un_solo_borrador_abierto_por_origen(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0034")
        origen = await _cotizacion_v2(migration_engine, "CTZ-V2-ORIGEN")
        primera = await _cotizacion_v2(migration_engine, "CTZ-V2-DUP-1")
        segunda = await _cotizacion_v2(migration_engine, "CTZ-V2-DUP-2")
        asignar = "UPDATE v2_quotations SET duplicated_from_id = :o WHERE id = :id"
        async with migration_engine.begin() as connection:
            await connection.execute(text(asignar), {"o": origen, "id": primera})
        await _falla(migration_engine, asignar, {"o": origen, "id": segunda})
        # Emitida la primera, ya se puede abrir otra.
        async with migration_engine.begin() as connection:
            await connection.execute(text(EMITIR), {"id": primera})
            await connection.execute(text(asignar), {"o": origen, "id": segunda})

    async def test_un_solo_puente_por_cotizacion(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0034")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-PUENTE")
        insertar = (
            "INSERT INTO v2_production_handoffs (v2_quotation_id, commercial_fingerprint)"
            " VALUES (:id, repeat('b', 64))"
        )
        async with migration_engine.begin() as connection:
            await connection.execute(text(insertar), {"id": cotizacion})
        await _falla(migration_engine, insertar, {"id": cotizacion})


class TestDowngrade:
    async def test_vuelta_limpia_sin_nada_comprometido(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0034")
        await _cotizacion_v2(migration_engine, "CTZ-V2-LIMPIA")
        resultado = _alembic("downgrade", "0033")
        assert resultado.returncode == 0, resultado.stdout + resultado.stderr
        _upgrade("0034")

    async def test_se_niega_con_una_emitida(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0034")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-EMITIDA")
        async with migration_engine.begin() as connection:
            await connection.execute(text(EMITIR), {"id": cotizacion})
        resultado = _alembic("downgrade", "0033")
        assert resultado.returncode != 0
        assert "no puede revertirse" in resultado.stdout + resultado.stderr


async def test_0034_se_aplica_y_se_sella(migration_engine: AsyncEngine) -> None:
    """0034 se aplica y queda sellada. La cabeza es 0035 desde la correccion de mano de obra."""
    _upgrade("0034")
    async with migration_engine.connect() as connection:
        cabezas = list(
            (await connection.scalars(text("SELECT version_num FROM alembic_version"))).all()
        )
    assert cabezas == ["0034"]
