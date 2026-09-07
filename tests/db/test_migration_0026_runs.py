"""Fase 009K.3 — la migracion 0026 se ejecuta de verdad contra PostgreSQL.

Como las de 0015 a 0025: alembic real en un subproceso, sobre una base propia,
la ida y la vuelta.

Anadir cuatro columnas anulables no sorprende a nadie. Lo que aqui importa son
las tres cosas que si podrian salir mal sin que se note:

1. que la migracion **no toque ni una fila**. Escribir hoy una bandera de
   factor sobre cotizaciones ya emitidas seria reinterpretar precios que
   alguien firmo, y una vez escrita ya no se distingue de una eleccion;
2. que los CHECK de factor **sigan en pie**. Lo que cambia en 009K.3 es que se
   puede no aplicar el factor, no que el factor pueda valer cero. Si 0026 los
   relajara, un cero entraria en la base y `price_line` respondria 422 al
   releer el documento;
3. que el CHECK del modo de horno **muerda**. Sin el, un modo invalido no
   fallaria: el codigo elegiria en silencio la rama `TOGETHER`.
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
MIGRATION_DB = "greda_migration_0026"
REPO_ROOT = Path(__file__).parents[2]

#: Las cuatro columnas que anade la 0026, por tabla. Cuatro, no cinco.
COLUMNAS_NUEVAS = {
    "quotations": ("production_factor_enabled", "kiln_mode"),
    "commercial_settings": ("production_factor_enabled_default", "kiln_mode_default"),
}

#: Los CHECK que existian antes y tienen que seguir existiendo despues.
#:
#: El de `quotations` lleva el prefijo repetido desde 0008: la convencion de
#: `alembic/env.py` se aplica sobre un nombre que YA lo traia. Es historico y
#: esta asi en produccion; se escribe tal cual porque el proposito de esta
#: prueba es encontrar la restriccion real, no la que deberia llamarse.
CHECKS_INTACTOS = (
    "ck_commercial_settings_production_factor_default_positive",
    "ck_quotations_ck_quotations_commercial_factor_positive",
)

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


async def _restriccion(engine: AsyncEngine, nombre: str) -> str | None:
    async with engine.connect() as connection:
        return await connection.scalar(
            text("SELECT conname FROM pg_constraint WHERE conname = :nombre"),
            {"nombre": nombre},
        )


async def _current(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        existe = await connection.scalar(text("SELECT to_regclass('alembic_version')"))
        if existe is None:
            return None
        return await connection.scalar(text("SELECT version_num FROM alembic_version"))


#: Cotizacion minima con factor tres. Las dos columnas de 009K.3 se nombran
#: siempre y viajan como parametros: componer la lista de columnas a partir de
#: los argumentos daria una consulta distinta en cada llamada, y una consulta
#: que se arma sola es la que nadie revisa.
_INSERT_COTIZACION = text(
    "INSERT INTO quotations (code, status, workflow, source_fingerprint,"
    " commercial_factor_default_snapshot, commercial_factor,"
    " kiln_mode, production_factor_enabled)"
    " VALUES (:codigo, 'DRAFT', 'LEGACY', :huella, 3, 3, :kiln_mode, :factor_enabled)"
)

#: La misma, para una base que todavia no ha llegado a 0026.
_INSERT_COTIZACION_0025 = text(
    "INSERT INTO quotations (code, status, workflow, source_fingerprint,"
    " commercial_factor_default_snapshot, commercial_factor)"
    " VALUES (:codigo, 'DRAFT', 'LEGACY', :huella, 3, 3)"
)


async def _sembrar_cotizacion(
    engine: AsyncEngine,
    codigo: str,
    *,
    kiln_mode: str | None = None,
    production_factor_enabled: bool | None = None,
    antes_de_0026: bool = False,
) -> None:
    async with engine.begin() as connection:
        if antes_de_0026:
            await connection.execute(
                _INSERT_COTIZACION_0025, {"codigo": codigo, "huella": "0" * 64}
            )
            return
        await connection.execute(
            _INSERT_COTIZACION,
            {
                "codigo": codigo,
                "huella": "0" * 64,
                "kiln_mode": kiln_mode,
                "factor_enabled": production_factor_enabled,
            },
        )


@pytest.mark.asyncio
async def test_0025_a_0026_anade_cuatro_columnas_anulables(
    migration_engine: AsyncEngine,
) -> None:
    """MIGRATION_0025_TO_0026: PASS. EXPECTED_0026_NEW_COLUMNS: 4."""
    _upgrade("0025")
    assert await _current(migration_engine) == "0025"
    for tabla, columnas in COLUMNAS_NUEVAS.items():
        for columna in columnas:
            assert await _columna(migration_engine, tabla, columna) is None, (tabla, columna)

    _upgrade("0026")
    assert await _current(migration_engine) == "0026"
    for tabla, columnas in COLUMNAS_NUEVAS.items():
        for columna in columnas:
            # Anulables: NULL tiene lectura conocida —`TOGETHER`, y el factor
            # que la propia cotizacion guardo— y obligatorias exigirian
            # inventarle un valor a toda la historia.
            assert await _columna(migration_engine, tabla, columna) == "YES", (tabla, columna)


@pytest.mark.asyncio
async def test_0026_no_toca_ni_una_cotizacion_existente(migration_engine: AsyncEngine) -> None:
    """MIGRATION_0026_UPDATE_COUNT: 0.

    Se siembra una cotizacion con factor tres ANTES de migrar y se comprueba
    despues que sus dos columnas nuevas siguen vacias. Es la unica forma de
    distinguir «no hice backfill» de «no habia nada que rellenar».

    Rellenar aqui `production_factor_enabled = true` porque el factor guardado
    es tres pareceria un favor. Seria escribir una decision que nadie tomo
    sobre un documento que alguien firmo, y ademas quitaria del codigo el
    unico sitio donde esa lectura se puede cambiar de opinion.
    """
    _upgrade("0025")
    await _sembrar_cotizacion(migration_engine, "CTZ-HIST-0026", antes_de_0026=True)

    _upgrade("0026")
    async with migration_engine.connect() as connection:
        fila = (
            await connection.execute(
                text(
                    "SELECT production_factor_enabled, kiln_mode, commercial_factor"
                    " FROM quotations WHERE code = 'CTZ-HIST-0026'"
                )
            )
        ).one()
    assert fila == (None, None, 3), fila


@pytest.mark.asyncio
async def test_0026_no_relaja_ningun_check_de_factor(migration_engine: AsyncEngine) -> None:
    """Cero sigue prohibido: apagado se dice con una bandera, no con un valor."""
    _upgrade("0026")

    for nombre in CHECKS_INTACTOS:
        assert await _restriccion(migration_engine, nombre) == nombre, nombre

    await _sembrar_cotizacion(migration_engine, "CTZ-CERO-0026", kiln_mode="TOGETHER")
    async with migration_engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                text("UPDATE quotations SET commercial_factor = 0 WHERE code = 'CTZ-CERO-0026'")
            )


@pytest.mark.asyncio
async def test_el_modo_de_horno_solo_admite_los_dos_conocidos(
    migration_engine: AsyncEngine,
) -> None:
    """Sin este CHECK un modo invalido elegiria TOGETHER en silencio."""
    _upgrade("0026")

    await _sembrar_cotizacion(migration_engine, "CTZ-JUNTO-0026", kiln_mode="TOGETHER")
    await _sembrar_cotizacion(migration_engine, "CTZ-SEPARADO-0026", kiln_mode="PER_PRODUCT")
    await _sembrar_cotizacion(migration_engine, "CTZ-NULO-0026")

    with pytest.raises(IntegrityError):
        await _sembrar_cotizacion(migration_engine, "CTZ-RARO-0026", kiln_mode="POR_PRODUCTO")

    async with migration_engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                text("UPDATE commercial_settings SET kiln_mode_default = 'CUALQUIERA'")
            )


@pytest.mark.asyncio
async def test_la_vuelta_a_0025_retira_las_columnas_y_deja_los_datos(
    migration_engine: AsyncEngine,
) -> None:
    """El downgrade no puede llevarse por delante la cotizacion."""
    _upgrade("0026")
    await _sembrar_cotizacion(
        migration_engine,
        "CTZ-DOWN-0026",
        kiln_mode="PER_PRODUCT",
        production_factor_enabled=True,
    )

    _downgrade("0025")
    assert await _current(migration_engine) == "0025"
    for tabla, columnas in COLUMNAS_NUEVAS.items():
        for columna in columnas:
            assert await _columna(migration_engine, tabla, columna) is None, (tabla, columna)

    async with migration_engine.connect() as connection:
        vive = await connection.scalar(
            text("SELECT count(*) FROM quotations WHERE code = 'CTZ-DOWN-0026'")
        )
    assert vive == 1


@pytest.mark.asyncio
async def test_subir_hasta_la_cabeza_pasa_por_0026_y_deja_una_sola(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("head")
    assert await _current(migration_engine) == "0026"
    heads = _alembic("heads")
    assert heads.returncode == 0, heads.stderr
    assert heads.stdout.count("(head)") == 1, heads.stdout
