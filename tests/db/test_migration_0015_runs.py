"""Fase 009D — la 0015 ejecutada como MIGRACION real sobre PostgreSQL.

## Por que hacia falta este archivo

El resto de las pruebas con base de datos crean el esquema con
`Base.metadata.create_all`. Eso demuestra que los MODELOS son coherentes, pero
no ejecuta ni una linea de la migracion: un `upgrade` roto —una constraint con
otro nombre, un `DO $$` mal escrito, un downgrade que olvida una columna— no
saldria hasta produccion.

Aqui se crea una base de datos aparte, se ejecuta `alembic upgrade 0014`, luego
`0015`, se comprueba el resultado contra el catalogo del sistema, se baja a
0014 y se vuelve a subir.

Alembic corre en un SUBPROCESO con `DATABASE_URL` apuntando a esa base: es su
propio `env.py` el que se usa, el mismo que correra contra Supabase, y no hace
falta un driver sincrono que no esta entre las dependencias del proyecto.
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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.session import normalize_database_url

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
#: Base de datos propia, para no pisar la que usan las demas pruebas.
MIGRATION_DB = "greda_migration_0015"
REPO_ROOT = Path(__file__).parents[2]

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL no definida: se omiten las pruebas con base de datos",
)


def _url_for(database: str) -> str:
    parts = urlsplit(normalize_database_url(TEST_DATABASE_URL))
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Ejecuta alembic contra la base de migracion, con su propio env.py."""
    environment = {**os.environ, "DATABASE_URL": _url_for(MIGRATION_DB)}
    # S603: el comando es fijo (el propio interprete y alembic) y los
    # argumentos son revisiones literales de esta prueba. No hay entrada de
    # usuario en juego.
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
    """Base de datos limpia para migrar, creada y destruida por la prueba.

    `AUTOCOMMIT` es obligatorio: PostgreSQL no admite CREATE/DROP DATABASE
    dentro de una transaccion.
    """
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
            # Sin cerrar el motor, la conexion abierta impide el DROP DATABASE.
            await engine.dispose()

        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}"'))
    finally:
        await admin.dispose()


async def _scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as connection:
        return await connection.scalar(text(sql), params)


async def _table_exists(engine: AsyncEngine, name: str) -> bool:
    return bool(
        await _scalar(
            engine,
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :t)",
            t=name,
        )
    )


async def _column_exists(engine: AsyncEngine, table: str, column: str) -> bool:
    return bool(
        await _scalar(
            engine,
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c)",
            t=table,
            c=column,
        )
    )


async def _check_clause(engine: AsyncEngine, name: str) -> str | None:
    value = await _scalar(
        engine,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n",
        n=name,
    )
    return str(value) if value is not None else None


async def _current(engine: AsyncEngine) -> str | None:
    value = await _scalar(engine, "SELECT version_num FROM alembic_version")
    return str(value) if value is not None else None


async def _uom(engine: AsyncEngine, code: str) -> tuple[str, Decimal] | None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT dimension, factor_to_base FROM units_of_measure WHERE code = :c"),
                {"c": code},
            )
        ).first()
    return (str(row[0]), Decimal(str(row[1]))) if row else None


async def _uom_snapshot(engine: AsyncEngine) -> dict[str, tuple[object, ...]]:
    """Retrato completo de las unidades existentes, para comparar antes/despues.

    Comparar solo dimension y factor dejaria pasar un cambio silencioso de
    nombre, simbolo, `is_base` o `active`.
    """
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT code, name, symbol, dimension, factor_to_base, is_base, active "
                    "FROM units_of_measure ORDER BY code"
                )
            )
        ).all()
    return {str(row[0]): tuple(row[1:]) for row in rows}


@pytest.mark.asyncio
async def test_0014_a_0015_a_0014_y_de_vuelta(migration_engine: AsyncEngine) -> None:
    """ALEMBIC_0014_TO_0015 + ALEMBIC_0015_TO_0014_TO_0015."""
    _upgrade("0014")
    assert await _current(migration_engine) == "0014"
    assert not await _table_exists(migration_engine, "recipe_preparations")
    unidades_antes = await _uom_snapshot(migration_engine)

    _upgrade("0015")
    assert await _current(migration_engine) == "0015"

    # ---- Unidades ------------------------------------------------------
    assert await _uom(migration_engine, "ml") == ("VOLUME", Decimal(1))
    assert await _uom(migration_engine, "l") == ("VOLUME", Decimal(1000))
    # EXISTING_UOMS_UNCHANGED: cada unidad previa, entera y sin tocar.
    unidades_despues = await _uom_snapshot(migration_engine)
    for code, antes in unidades_antes.items():
        assert code in unidades_despues, f"la 0015 borro la unidad {code}"
        assert unidades_despues[code] == antes, f"la 0015 altero la unidad {code}"
    # Y las unicas altas son las dos de volumen.
    assert set(unidades_despues) - set(unidades_antes) == {"ml", "l"}

    # ---- Tablas y columnas nuevas ---------------------------------------
    assert await _table_exists(migration_engine, "recipe_preparations")
    assert await _table_exists(migration_engine, "recipe_preparation_lines")
    assert await _column_exists(migration_engine, "stock_movements", "preparation_id")
    assert await _column_exists(migration_engine, "commercial_settings", "estimated_glaze_percent")

    # ---- Constraints que protegen el dominio ----------------------------
    for name, fragment in (
        ("ck_recipe_preparations_dry_weight_positive", "total_dry_weight_g >"),
        ("ck_recipe_preparations_water_not_negative", "water_amount_ml >="),
        ("ck_recipe_preparations_yield_positive", "final_yield_ml >"),
        ("ck_recipe_preparations_concentration_positive", "solids_g_per_ml >"),
        ("ck_recipe_preparations_cost_not_negative", "batch_total_cost >="),
        ("ck_recipe_preparations_unit_cost_not_negative", "unit_cost_per_ml >="),
    ):
        clause = await _check_clause(migration_engine, name)
        assert clause is not None, f"falta la constraint {name}"
        assert fragment in clause, f"{name}: {clause}"

    assert (
        await _scalar(
            migration_engine,
            "SELECT COUNT(*) FROM pg_constraint "
            "WHERE conname = 'uq_recipe_preparations_idempotency_key' AND contype = 'u'",
        )
        == 1
    )

    movimientos = await _check_clause(migration_engine, "ck_stock_movements_movement_type_allowed")
    assert movimientos is not None
    assert "PREPARATION_OUT" in movimientos and "PREPARATION_IN" in movimientos

    # ---- Downgrade -------------------------------------------------------
    _downgrade("0014")
    assert await _current(migration_engine) == "0014"
    assert not await _table_exists(migration_engine, "recipe_preparations")
    assert not await _table_exists(migration_engine, "recipe_preparation_lines")
    assert not await _column_exists(migration_engine, "stock_movements", "preparation_id")
    assert not await _column_exists(
        migration_engine, "commercial_settings", "estimated_glaze_percent"
    )
    unidades = await _check_clause(migration_engine, "ck_units_of_measure_dimension_allowed")
    assert unidades is not None and "VOLUME" not in unidades
    assert await _uom(migration_engine, "ml") is None
    assert await _uom(migration_engine, "l") is None
    assert (
        await _scalar(
            migration_engine,
            "SELECT COUNT(*) FROM document_sequences WHERE sequence_type = 'PREPARATION'",
        )
        == 0
    )

    # ---- Y vuelve a subir -------------------------------------------------
    _upgrade("0015")
    assert await _current(migration_engine) == "0015"
    assert await _table_exists(migration_engine, "recipe_preparations")
    assert await _uom(migration_engine, "ml") == ("VOLUME", Decimal(1))


@pytest.mark.asyncio
async def test_la_migracion_siembra_la_secuencia_de_preparacion(
    migration_engine: AsyncEngine,
) -> None:
    """MIGRATION_SEEDS_PREPARATION_SEQUENCE.

    La fila la crea la MIGRACION, no el fixture de pruebas: en esta base no ha
    corrido ningun conftest.
    """
    _upgrade("0015")
    async with migration_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT prefix, pattern, padding, reset_policy, current_value, active "
                    "FROM document_sequences WHERE sequence_type = 'PREPARATION'"
                )
            )
        ).first()
    assert row is not None, "la 0015 no sembro la secuencia PREPARATION"
    assert row[0] == "PREP"
    # El patron canonico del sistema, el mismo de CTZ y HR.
    assert row[1] == "{PREFIX}-{YYYY}-{NUMBER}"
    assert row[2] == 6
    assert row[3] == "YEARLY"
    assert row[4] == 0
    assert row[5] is True


@pytest.mark.asyncio
async def test_el_porcentaje_de_esmalte_llega_como_quince(
    migration_engine: AsyncEngine,
) -> None:
    """ESTIMATED_GLAZE_PERCENT: 15, no 0.15."""
    _upgrade("0015")
    # La fila singleton ya existe: la siembra una migracion anterior. Lo que se
    # comprueba es que la 0015 le puso el valor por omision, no que se pueda
    # crear otra.
    value = await _scalar(
        migration_engine, "SELECT estimated_glaze_percent FROM commercial_settings LIMIT 1"
    )
    assert value is not None, "no hay fila de commercial_settings que comprobar"
    assert Decimal(str(value)) == Decimal(15)

    clause = await _check_clause(
        migration_engine, "ck_commercial_settings_estimated_glaze_percent_range"
    )
    assert clause is not None
    assert "estimated_glaze_percent >" in clause and "<=" in clause


async def _assert_no_partial_schema(engine: AsyncEngine) -> None:
    """Nada de la 0015 puede quedar aplicado tras un upgrade abortado.

    Que `alembic_version` siga en 0014 no basta: alembic podria haber dejado
    objetos creados si el DDL no fuera transaccional. En PostgreSQL lo es, pero
    eso hay que COMPROBARLO, no suponerlo — y aqui importa especialmente,
    porque el CHECK de unidades se cambia ANTES de la guarda que aborta.
    """
    assert await _current(engine) == "0014"

    assert not await _table_exists(engine, "recipe_preparations")
    assert not await _table_exists(engine, "recipe_preparation_lines")
    assert not await _column_exists(engine, "stock_movements", "preparation_id")
    assert not await _column_exists(engine, "commercial_settings", "estimated_glaze_percent")

    # La secuencia del lote no debe haberse sembrado.
    assert (
        await _scalar(
            engine,
            "SELECT COUNT(*) FROM document_sequences WHERE sequence_type = 'PREPARATION'",
        )
        == 0
    )

    # El CHECK de unidades sigue siendo el de 0014: sin VOLUME.
    unidades = await _check_clause(engine, "ck_units_of_measure_dimension_allowed")
    assert unidades is not None, "el CHECK de unidades desaparecio"
    assert "VOLUME" not in unidades, f"quedo aplicado a medias: {unidades}"

    # Y los tipos de movimiento tampoco se ampliaron.
    movimientos = await _check_clause(engine, "ck_stock_movements_movement_type_allowed")
    assert movimientos is not None
    assert "PREPARATION_OUT" not in movimientos and "PREPARATION_IN" not in movimientos


async def _insert_uom(engine: AsyncEngine, code: str, dimension: str, factor: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO units_of_measure "
                "(code, name, symbol, dimension, factor_to_base, is_base, active) "
                "VALUES (:c, :c, :c, :d, :f, false, true)"
            ),
            {"c": code, "d": dimension, "f": factor},
        )


@pytest.mark.asyncio
async def test_ml_existente_con_definicion_incorrecta_aborta(
    migration_engine: AsyncEngine,
) -> None:
    """CONFLICTING_ML.

    `ON CONFLICT DO NOTHING` habria dado SUCCESS dejando la base con un `ml`
    que no es un mililitro, y a partir de ahi cada conversion g <-> ml daria un
    numero equivocado sin que nadie se entere. Se prefiere abortar.
    """
    _upgrade("0014")
    # En 0014 el CHECK aun no admite VOLUME, asi que una unidad incompatible
    # plausible es justamente una de otra dimension.
    await _insert_uom(migration_engine, "ml", "MASS", "1")

    result = _alembic("upgrade", "0015")
    assert result.returncode != 0, "la migracion deberia haber abortado"
    assert "definicion distinta" in (result.stdout + result.stderr)
    # FAILED_MIGRATION_TRANSACTIONALITY + NO_PARTIAL_SCHEMA_AFTER_FAILURE.
    await _assert_no_partial_schema(migration_engine)


@pytest.mark.asyncio
async def test_l_existente_con_factor_incorrecto_aborta(migration_engine: AsyncEngine) -> None:
    """CONFLICTING_L: un litro que no vale 1000 ml no es un litro."""
    _upgrade("0014")
    await _insert_uom(migration_engine, "l", "MASS", "999")

    result = _alembic("upgrade", "0015")
    assert result.returncode != 0, "la migracion deberia haber abortado"
    assert "definicion distinta" in (result.stdout + result.stderr)
    await _assert_no_partial_schema(migration_engine)
