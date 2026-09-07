"""Fase 009K.2 — la migracion 0025 se ejecuta de verdad contra PostgreSQL.

Como las de 0015, 0016 y 0017: alembic real en un subproceso, sobre una base
propia, la ida y la vuelta.

Lo que aqui importa no son las cinco columnas —anadir columnas anulables no
sorprende a nadie— sino las dos cosas que si podrian salir mal sin que se note:

1. que la migracion **no toque ni una fila**. Un backfill que atribuyera los
   documentos viejos al administrador actual seria mentir en el historial, y
   una vez escrito ya no se distingue de la verdad;
2. que las columnas queden **anulables**. Si nacieran obligatorias, la propia
   migracion fallaria en produccion —hay cotizaciones antiguas sin actor— o,
   peor, forzaria a rellenarlas con algo.
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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.session import normalize_database_url

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
#: Base propia, para que pueda correr a la vez que las otras pruebas de migracion.
MIGRATION_DB = "greda_migration_0025"
REPO_ROOT = Path(__file__).parents[2]

#: Las cinco columnas que anade la 0025, por tabla.
COLUMNAS_NUEVAS = {
    "quotations": ("created_by_name", "confirmed_by_id", "confirmed_by_name"),
    "prototype_quotations": ("confirmed_by", "confirmed_by_name"),
}

#: Lo que 0025 NO debe anadir. La decision de la fase fue que `display_name`
#: siga siendo la unica autoridad del nombre visible y que el correo siga
#: viviendo solo en Supabase: tres fuentes de verdad para lo mismo es como se
#: empieza a no saber cual vale.
COLUMNAS_PROHIBIDAS = ("first_name", "last_name", "email")

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


async def _columna(engine: AsyncEngine, tabla: str, columna: str) -> str | None:
    """Devuelve `is_nullable` de la columna, o `None` si no existe."""
    async with engine.connect() as connection:
        return await connection.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = :tabla AND column_name = :columna"
            ),
            {"tabla": tabla, "columna": columna},
        )


async def _current(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        existe = await connection.scalar(text("SELECT to_regclass('alembic_version')"))
        if existe is None:
            return None
        return await connection.scalar(text("SELECT version_num FROM alembic_version"))


@pytest.mark.asyncio
async def test_0024_a_0025_anade_las_cinco_columnas_anulables(
    migration_engine: AsyncEngine,
) -> None:
    """MIGRATION_0024_TO_0025: PASS."""
    _upgrade("0024")
    assert await _current(migration_engine) == "0024"
    for tabla, columnas in COLUMNAS_NUEVAS.items():
        for columna in columnas:
            assert await _columna(migration_engine, tabla, columna) is None, (tabla, columna)

    _upgrade("0025")
    assert await _current(migration_engine) == "0025"
    for tabla, columnas in COLUMNAS_NUEVAS.items():
        for columna in columnas:
            # Anulables: hay cotizaciones anteriores a esta fase sin actor, y
            # una columna obligatoria las dejaria fuera o forzaria a inventarlo.
            assert await _columna(migration_engine, tabla, columna) == "YES", (tabla, columna)


@pytest.mark.asyncio
async def test_0025_no_toca_ni_una_fila_existente(migration_engine: AsyncEngine) -> None:
    """MIGRATION_0025_HISTORICAL_UPDATE_COUNT: 0.

    Se siembra una cotizacion ANTES de migrar y se comprueba despues que sus
    columnas de actor siguen vacias. Es la unica forma de distinguir «no hice
    backfill» de «no habia nada que rellenar».
    """
    _upgrade("0024")
    async with migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO quotations (code, status, workflow, source_fingerprint, "
                " commercial_factor_default_snapshot, commercial_factor) "
                "VALUES ('CTZ-HIST-0025', 'DRAFT', 'LEGACY', :huella, 1, 1)"
            ),
            {"huella": "0" * 64},
        )

    _upgrade("0025")
    async with migration_engine.connect() as connection:
        fila = (
            await connection.execute(
                text(
                    "SELECT created_by_name, confirmed_by_id, confirmed_by_name "
                    "FROM quotations WHERE code = 'CTZ-HIST-0025'"
                )
            )
        ).one()
    assert fila == (None, None, None), fila


@pytest.mark.asyncio
async def test_0025_no_anade_nombre_apellido_ni_correo_al_perfil(
    migration_engine: AsyncEngine,
) -> None:
    """PROFILE_FIRST_LAST_ADDED: NO. PROFILE_EMAIL_ADDED: NO.

    `display_name` sigue siendo la unica autoridad del nombre visible, y el
    correo sigue viviendo solo en Supabase Auth.
    """
    _upgrade("0025")
    for columna in COLUMNAS_PROHIBIDAS:
        assert await _columna(migration_engine, "profiles", columna) is None, columna


@pytest.mark.asyncio
async def test_la_vuelta_a_0024_retira_las_columnas_y_deja_los_datos(
    migration_engine: AsyncEngine,
) -> None:
    """El downgrade no puede llevarse por delante la cotizacion."""
    _upgrade("0025")
    async with migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO quotations (code, status, workflow, source_fingerprint, "
                " commercial_factor_default_snapshot, commercial_factor, created_by_name) "
                "VALUES ('CTZ-DOWN-0025', 'DRAFT', 'LEGACY', :huella, 1, 1, 'Ana Perez')"
            ),
            {"huella": "0" * 64},
        )

    _downgrade("0024")
    assert await _current(migration_engine) == "0024"
    for tabla, columnas in COLUMNAS_NUEVAS.items():
        for columna in columnas:
            assert await _columna(migration_engine, tabla, columna) is None, (tabla, columna)

    async with migration_engine.connect() as connection:
        vive = await connection.scalar(
            text("SELECT count(*) FROM quotations WHERE code = 'CTZ-DOWN-0025'")
        )
    assert vive == 1


@pytest.mark.asyncio
async def test_subir_hasta_la_cabeza_pasa_por_0025(migration_engine: AsyncEngine) -> None:
    """Subir del todo NO se detiene en 0025: pasa por ella y sigue.

    Que la cabeza sea la ultima revision lo afirma la prueba de ESA revision
    —hoy `test_migration_0026_runs`—, y por eso aqui no se nombra ninguna:
    fijarla obligaria a reescribir este archivo en cada fase, y una prueba que
    hay que reescribir cada vez deja de comprobar nada.
    """
    _upgrade("head")
    assert await _current(migration_engine) != "0025", "0025 dejo de ser la cabeza"
    for tabla, columnas in COLUMNAS_NUEVAS.items():
        for columna in columnas:
            assert await _columna(migration_engine, tabla, columna) == "YES", (tabla, columna)
    heads = _alembic("heads")
    assert heads.returncode == 0, heads.stderr
    assert heads.stdout.count("(head)") == 1, heads.stdout
