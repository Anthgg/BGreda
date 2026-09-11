"""Fase 010A — la migracion 0028 se ejecuta de verdad contra PostgreSQL.

Como las de 0015 a 0027: alembic real en un subproceso, sobre una base propia,
la ida y la vuelta.

Lo que aqui importa, por orden de gravedad:

1. que las cotizaciones que ya existian **no cambien**. Ni un total, ni un
   snapshot, ni un estado. Lo unico que reciben es el sello `LEGACY`, que no es
   un dato nuevo: es escribir lo que siempre fue verdad;
2. que los dos CHECK de motor muerdan. Son la frontera entera: sin ellos, el
   aislamiento depende de que nadie escriba el INSERT equivocado;
3. que el downgrade **se niegue** cuando ya hay cotizaciones V2, en vez de
   borrar documentos con correlativo emitido.
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
#: Base propia, para que pueda correr a la vez que las otras de migracion.
MIGRATION_DB = "greda_migration_0028"
REPO_ROOT = Path(__file__).parents[2]

CK_LEGACY = "ck_quotations_engine_is_legacy"
CK_V2 = "ck_v2_quotations_engine_is_v2"

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL no definida: se omiten las pruebas con base de datos",
)


def _url_for(database: str) -> str:
    parts = urlsplit(normalize_database_url(TEST_DATABASE_URL))
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "DATABASE_URL": _url_for(MIGRATION_DB)}
    # S603: comando fijo y argumentos que son revisiones literales de esta
    # prueba. No hay entrada de usuario.
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


# ---------------------------------------------------------------------------
# Semillas. Una cotizacion Legacy como las que ya hay en produccion, sembrada
# sin pasar por los modelos: una prueba de migracion tiene que poder sembrar
# una base que todavia no conoce el codigo de hoy.
# ---------------------------------------------------------------------------
async def _cotizacion_legacy(engine: AsyncEngine, codigo: str) -> dict[str, object]:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO quotations"
                " (code, status, workflow, source_fingerprint,"
                "  commercial_factor_default_snapshot, commercial_factor,"
                "  calculated_total, commercial_total, created_at, updated_at)"
                " VALUES (:codigo, 'CONFIRMED', 'COTIZADOR', :huella, 3, 2.5,"
                "  1234.5, 1456.71, now(), now())"
            ),
            {"codigo": codigo, "huella": "0" * 64},
        )
    return await _leer_cotizacion(engine, codigo)


async def _leer_cotizacion(engine: AsyncEngine, codigo: str) -> dict[str, object]:
    async with engine.connect() as connection:
        fila = (
            (
                await connection.execute(
                    text(
                        "SELECT status, workflow, commercial_factor, calculated_total,"
                        "       commercial_total, updated_at"
                        "  FROM quotations WHERE code = :codigo"
                    ),
                    {"codigo": codigo},
                )
            )
            .mappings()
            .one()
        )
    return dict(fila)


async def _restriccion(engine: AsyncEngine, nombre: str) -> str | None:
    async with engine.connect() as connection:
        return await connection.scalar(
            text("SELECT conname FROM pg_constraint WHERE conname = :nombre"),
            {"nombre": nombre},
        )


async def _tabla(engine: AsyncEngine, nombre: str) -> str | None:
    async with engine.connect() as connection:
        return await connection.scalar(text("SELECT to_regclass(:nombre)"), {"nombre": nombre})


# ---------------------------------------------------------------------------
# 1. Los historicos no se tocan
# ---------------------------------------------------------------------------
class TestHistoricos:
    async def test_una_cotizacion_anterior_sobrevive_intacta(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0027")
        antes = await _cotizacion_legacy(migration_engine, "CTZ-2026-000001")

        _upgrade("0028")

        despues = await _leer_cotizacion(migration_engine, "CTZ-2026-000001")
        assert despues == antes, "la migracion cambio datos de una cotizacion historica"

    async def test_la_cotizacion_anterior_queda_sellada_como_legacy(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0027")
        await _cotizacion_legacy(migration_engine, "CTZ-2026-000002")

        _upgrade("0028")

        async with migration_engine.connect() as connection:
            motores = list(
                await connection.scalars(text("SELECT pricing_engine_version FROM quotations"))
            )
        assert motores == ["LEGACY"]

    async def test_la_tabla_v2_nace_vacia(self, migration_engine: AsyncEngine) -> None:
        """Nada se migra: una cotizacion Legacy no se convierte en una V2."""
        _upgrade("0027")
        await _cotizacion_legacy(migration_engine, "CTZ-2026-000003")

        _upgrade("0028")

        async with migration_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM v2_quotations")) == 0


# ---------------------------------------------------------------------------
# 2. La frontera existe en la base
# ---------------------------------------------------------------------------
class TestFrontera:
    async def test_los_dos_checks_de_motor_quedan_creados(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0028")
        assert await _restriccion(migration_engine, CK_LEGACY) == CK_LEGACY
        assert await _restriccion(migration_engine, CK_V2) == CK_V2

    async def test_la_tabla_legacy_rechaza_una_fila_v2(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0028")
        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO quotations"
                        " (code, status, pricing_engine_version, source_fingerprint,"
                        "  commercial_factor_default_snapshot, commercial_factor,"
                        "  created_at, updated_at)"
                        " VALUES ('X-1', 'DRAFT', 'V2', :huella, 3, 3, now(), now())"
                    ),
                    {"huella": "0" * 64},
                )

    async def test_la_tabla_v2_rechaza_una_fila_legacy(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0028")
        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO v2_quotations"
                        " (code, pricing_engine_version, status, production_type,"
                        "  created_at, updated_at)"
                        " VALUES ('X-2', 'LEGACY', 'DRAFT', 'RETAIL', now(), now())"
                    )
                )

    async def test_el_talonario_v2_queda_sembrado(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0028")
        async with migration_engine.connect() as connection:
            fila = (
                await connection.execute(
                    text(
                        "SELECT prefix, padding, reset_policy, current_value, active"
                        "  FROM document_sequences WHERE sequence_type = 'QUOTE_V2'"
                    )
                )
            ).one()
        assert fila.prefix == "CTZ-V2"
        assert fila.current_value == 0
        assert fila.active is True

    async def test_sembrar_el_talonario_es_idempotente(self, migration_engine: AsyncEngine) -> None:
        """Volver a aplicar la revision no duplica la fila del talonario."""
        _upgrade("0028")
        _upgrade("head")
        async with migration_engine.connect() as connection:
            total = await connection.scalar(
                text("SELECT count(*) FROM document_sequences WHERE sequence_type = 'QUOTE_V2'")
            )
        assert total == 1


# ---------------------------------------------------------------------------
# 3. La vuelta
# ---------------------------------------------------------------------------
class TestDowngrade:
    async def test_revertir_sin_cotizaciones_v2_deja_el_esquema_anterior(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0028")
        await _cotizacion_legacy(migration_engine, "CTZ-2026-000004")
        antes = await _leer_cotizacion(migration_engine, "CTZ-2026-000004")

        resultado = _alembic("downgrade", "0027")
        assert resultado.returncode == 0, f"{resultado.stdout}\n{resultado.stderr}"

        assert await _tabla(migration_engine, "v2_quotations") is None
        assert await _restriccion(migration_engine, CK_LEGACY) is None
        # Y la cotizacion historica sigue exactamente igual que antes.
        assert await _leer_cotizacion(migration_engine, "CTZ-2026-000004") == antes

    async def test_revertir_se_niega_si_hay_cotizaciones_v2(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Un documento con correlativo emitido no se borra por revertir."""
        _upgrade("0028")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO v2_quotations"
                    " (code, pricing_engine_version, status, production_type,"
                    "  created_at, updated_at)"
                    " VALUES ('CTZ-V2-2026-000001', 'V2', 'DRAFT', 'RETAIL', now(), now())"
                )
            )

        resultado = _alembic("downgrade", "0027")

        assert resultado.returncode != 0
        assert (
            "no puede revertirse" in resultado.stderr or "no puede revertirse" in resultado.stdout
        )
        # Y la tabla sigue ahi, con su documento dentro.
        assert await _tabla(migration_engine, "v2_quotations") is not None
        async with migration_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM v2_quotations")) == 1

    async def test_revertir_se_niega_si_ya_se_gasto_un_correlativo(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Aunque la cotizacion se haya borrado, el numero sigue gastado.

        `document_sequence_issues` es el registro inmutable de que ese
        correlativo se entrego. Revertir lo dejaria intacto y una reaplicacion
        posterior sembraria el contador en cero contra numeros que ya existen:
        el primer alta chocaria con el UNIQUE.
        """
        _upgrade("0028")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO document_sequence_issues"
                    " (sequence_type, period_key, number, formatted_value, issued_at)"
                    " VALUES ('QUOTE_V2', '2026', 1, 'CTZ-V2-2026-000001', now())"
                )
            )

        resultado = _alembic("downgrade", "0027")

        assert resultado.returncode != 0
        assert await _tabla(migration_engine, "v2_quotations") is not None
        async with migration_engine.connect() as connection:
            pendientes = await connection.scalar(
                text(
                    "SELECT count(*) FROM document_sequence_issues WHERE sequence_type = 'QUOTE_V2'"
                )
            )
        assert pendientes == 1, "el registro de correlativos emitidos no se toca"
