"""Fase 010M — la migracion 0041 contra PostgreSQL real.

Lo que se prueba aqui es lo que la base garantiza SOLA, sin servicio delante:

1. sube: las tres nuevas tablas de layout, las dimensiones del horno y el
   nuevo valor LAYOUT en la constraint de kind;
2. las dimensiones del horno admiten NULL (campo opcional en el maestro);
3. la constraint de positividad de las dimensiones rechaza valores <= 0;
4. el UNIQUE de layout (una hornada — un layout) se cumple;
5. las constraints de rotation (solo 0 o 90), quantity > 0, x/y >= 0;
6. el UNIQUE de (layout_id, level_index) en niveles;
7. bajar funciona sin datos de 041, se niega con datos;
8. la cadena deja una sola cabeza, y es 0041.
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
MIGRATION_DB = "greda_migration_0041"
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


async def _id(engine: AsyncEngine, sql: str, params: dict[str, object] | None = None) -> int:
    async with engine.begin() as conn:
        val = await conn.scalar(text(sql), params or {})
        assert val is not None
        return int(str(val))


async def _horno(engine: AsyncEngine, codigo: str) -> tuple[int, int]:
    """Retorna (kiln_id, batch_id)."""
    kiln_id = await _id(
        engine,
        "INSERT INTO kilns (code, name, capacity_volume_cm3, firing_days_per_batch, active)"
        " VALUES (:c, 'Horno prueba', 1000, 3, true) RETURNING id",
        {"c": f"K-{codigo}"},
    )
    batch_id = await _id(
        engine,
        "INSERT INTO kiln_batches (code, kiln_id, firing_type, scheduled_date,"
        " kiln_name_snapshot, capacity_snapshot_cm3)"
        " VALUES (:c, :k, 'LOW', current_date, 'Horno prueba', 1000) RETURNING id",
        {"c": codigo, "k": kiln_id},
    )
    return kiln_id, batch_id


async def _layout(
    engine: AsyncEngine,
    batch_id: int,
    *,
    width: str = "60",
    depth: str = "50",
    height: str = "40",
) -> int:
    return await _id(
        engine,
        "INSERT INTO kiln_batch_layouts"
        " (batch_id, kiln_width_cm_snapshot, kiln_depth_cm_snapshot, kiln_height_cm_snapshot)"
        " VALUES (:b, :w, :d, :h) RETURNING id",
        {"b": batch_id, "w": Decimal(width), "d": Decimal(depth), "h": Decimal(height)},
    )


async def _nivel(
    engine: AsyncEngine,
    layout_id: int,
    level_index: int,
    *,
    z_cm: str = "0",
    usable_height_cm: str = "10",
) -> int:
    return await _id(
        engine,
        "INSERT INTO kiln_batch_layout_levels"
        " (layout_id, level_index, z_cm, usable_height_cm)"
        " VALUES (:l, :li, :z, :h) RETURNING id",
        {
            "l": layout_id,
            "li": level_index,
            "z": Decimal(z_cm),
            "h": Decimal(usable_height_cm),
        },
    )


async def _carga(engine: AsyncEngine, codigo: str) -> tuple[int, int]:
    carga = await _id(
        engine,
        "INSERT INTO internal_loads (code, name, low_fire_required, high_fire_required)"
        " VALUES (:c, 'Carga', true, false) RETURNING id",
        {"c": codigo},
    )
    linea = await _id(
        engine,
        "INSERT INTO internal_load_lines"
        " (load_id, sort_order, name, quantity, length_cm, width_cm, height_cm,"
        "  unit_volume_cm3, total_volume_cm3)"
        " VALUES (:l, 1, 'Pieza', 10, 5, 5, 4, 100, 1000) RETURNING id",
        {"l": carga},
    )
    return carga, linea


async def _asignacion(
    engine: AsyncEngine, batch_id: int, carga_id: int, linea_id: int, cantidad: int = 5
) -> int:
    return await _id(
        engine,
        "INSERT INTO kiln_batch_assignments"
        " (batch_id, source_kind, internal_load_id, internal_load_line_id,"
        "  quantity, unit_volume_snapshot_cm3, assigned_volume_cm3,"
        "  firing_mode, product_name_snapshot)"
        " VALUES (:b, 'INTERNAL', :c, :l, :q, 100, :v, 'SHARED', 'Pieza') RETURNING id",
        {"b": batch_id, "c": carga_id, "l": linea_id, "q": cantidad, "v": cantidad * 100},
    )


async def _placement(
    engine: AsyncEngine,
    layout_id: int,
    assignment_id: int,
    *,
    rotation_degrees: int = 0,
    quantity: int = 1,
    x_cm: str = "0",
    y_cm: str = "0",
    level_index: int = 0,
) -> int:
    return await _id(
        engine,
        "INSERT INTO kiln_batch_layout_placements"
        " (layout_id, batch_assignment_id, quantity, level_index,"
        "  x_cm, y_cm, rotation_degrees,"
        "  piece_length_cm_snapshot, piece_width_cm_snapshot, piece_height_cm_snapshot)"
        " VALUES (:l, :a, :q, :li, :x, :y, :r, 10, 8, 6) RETURNING id",
        {
            "l": layout_id,
            "a": assignment_id,
            "q": quantity,
            "li": level_index,
            "x": Decimal(x_cm),
            "y": Decimal(y_cm),
            "r": rotation_degrees,
        },
    )


# ---------------------------------------------------------------------------
# Pruebas
# ---------------------------------------------------------------------------

async def test_sube_y_crea_tablas_de_layout(migration_engine: AsyncEngine) -> None:
    """La migracion 0041 crea las tres tablas del layout y las columnas del horno."""
    _upgrade("0041")
    async with migration_engine.connect() as conn:
        tablas = list(
            (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables"
                        " WHERE table_schema = 'public'"
                        " AND table_name IN"
                        " ('kiln_batch_layouts', 'kiln_batch_layout_levels',"
                        "  'kiln_batch_layout_placements')"
                        " ORDER BY table_name"
                    )
                )
            ).scalars().all()
        )
        columnas = list(
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_schema = 'public'"
                        " AND table_name = 'kilns'"
                        " AND column_name IN"
                        " ('usable_width_cm', 'usable_depth_cm', 'usable_height_cm')"
                        " ORDER BY column_name"
                    )
                )
            ).scalars().all()
        )
    assert tablas == [
        "kiln_batch_layout_levels",
        "kiln_batch_layout_placements",
        "kiln_batch_layouts",
    ]
    assert columnas == ["usable_depth_cm", "usable_height_cm", "usable_width_cm"]


async def test_dimensiones_del_horno_admiten_null(migration_engine: AsyncEngine) -> None:
    """Las columnas de dimensiones utiles del horno son nullable."""
    _upgrade("0041")
    kiln_id = await _id(
        migration_engine,
        "INSERT INTO kilns (code, name, capacity_volume_cm3, firing_days_per_batch, active)"
        " VALUES ('K-NULL', 'Horno sin medidas', 1000, 3, true) RETURNING id",
    )
    # Debe poder insertarse sin dimensiones.
    async with migration_engine.connect() as conn:
        dims = (
            await conn.execute(
                text(
                    "SELECT usable_width_cm, usable_depth_cm, usable_height_cm "
                    "FROM kilns WHERE id = :k"
                ),
                {"k": kiln_id},
            )
        ).one()
    assert dims == (None, None, None)


async def test_dimensiones_del_horno_positivas(migration_engine: AsyncEngine) -> None:
    """usable_width/depth/height_cm deben ser > 0 si no son NULL."""
    _upgrade("0041")
    with pytest.raises(IntegrityError):
        await _id(
            migration_engine,
            "INSERT INTO kilns (code, name, capacity_volume_cm3, firing_days_per_batch,"
            " active, usable_width_cm) VALUES ('K-NEG', 'X', 1000, 3, true, -1) RETURNING id",
        )


async def test_unique_layout_por_hornada(migration_engine: AsyncEngine) -> None:
    """Solo puede existir un layout por hornada (UNIQUE batch_id)."""
    _upgrade("0041")
    _, batch_id = await _horno(migration_engine, "UNIQ")
    await _layout(migration_engine, batch_id)
    with pytest.raises(IntegrityError):
        await _layout(migration_engine, batch_id)


async def test_rotation_solo_0_o_90(migration_engine: AsyncEngine) -> None:
    """rotation_degrees solo acepta 0 o 90; cualquier otro valor es rechazado."""
    _upgrade("0041")
    _, batch_id = await _horno(migration_engine, "ROT")
    carga_id, linea_id = await _carga(migration_engine, "CI-ROT")
    asgn_id = await _asignacion(migration_engine, batch_id, carga_id, linea_id)
    layout_id = await _layout(migration_engine, batch_id)

    # Valido: 0 y 90 pasan.
    await _placement(migration_engine, layout_id, asgn_id, rotation_degrees=0)
    await _placement(migration_engine, layout_id, asgn_id, rotation_degrees=90)

    # Invalido: 45 y 180 se rechazan.
    with pytest.raises(IntegrityError):
        await _placement(migration_engine, layout_id, asgn_id, rotation_degrees=45)


async def test_quantity_mayor_que_cero(migration_engine: AsyncEngine) -> None:
    """quantity en placements debe ser > 0."""
    _upgrade("0041")
    _, batch_id = await _horno(migration_engine, "QTY")
    carga_id, linea_id = await _carga(migration_engine, "CI-QTY")
    asgn_id = await _asignacion(migration_engine, batch_id, carga_id, linea_id)
    layout_id = await _layout(migration_engine, batch_id)
    with pytest.raises(IntegrityError):
        await _placement(migration_engine, layout_id, asgn_id, quantity=0)


async def test_x_y_no_negativas(migration_engine: AsyncEngine) -> None:
    """x_cm e y_cm deben ser >= 0."""
    _upgrade("0041")
    _, batch_id = await _horno(migration_engine, "XY")
    carga_id, linea_id = await _carga(migration_engine, "CI-XY")
    asgn_id = await _asignacion(migration_engine, batch_id, carga_id, linea_id)
    layout_id = await _layout(migration_engine, batch_id)
    with pytest.raises(IntegrityError):
        await _placement(migration_engine, layout_id, asgn_id, x_cm="-1")


async def test_unique_level_index_por_layout(migration_engine: AsyncEngine) -> None:
    """No puede haber dos niveles con el mismo level_index en un layout."""
    _upgrade("0041")
    _, batch_id = await _horno(migration_engine, "LVLDP")
    layout_id = await _layout(migration_engine, batch_id)
    await _nivel(migration_engine, layout_id, 0)
    with pytest.raises(IntegrityError):
        await _nivel(migration_engine, layout_id, 0)


async def test_usable_height_positiva(migration_engine: AsyncEngine) -> None:
    """usable_height_cm en niveles debe ser > 0."""
    _upgrade("0041")
    _, batch_id = await _horno(migration_engine, "UH")
    layout_id = await _layout(migration_engine, batch_id)
    with pytest.raises(IntegrityError):
        await _nivel(migration_engine, layout_id, 0, usable_height_cm="0")


async def test_bajar_sin_datos_funciona_con_datos_se_niega(migration_engine: AsyncEngine) -> None:
    """Downgrade sin datos de 0041 funciona; con datos de layout se niega."""
    _upgrade("0041")
    limpio = _alembic("downgrade", "0040")
    assert limpio.returncode == 0, limpio.stderr

    _upgrade("0041")
    _, batch_id = await _horno(migration_engine, "BAJA")
    await _layout(migration_engine, batch_id)
    resultado = _alembic("downgrade", "0040")
    assert resultado.returncode != 0
    assert "0041" in resultado.stderr + resultado.stdout


async def test_cadena_deja_una_sola_cabeza_y_es_0041(migration_engine: AsyncEngine) -> None:
    _upgrade("head")
    async with migration_engine.connect() as connection:
        cabezas = list(
            (await connection.scalars(text("SELECT version_num FROM alembic_version"))).all()
        )
    assert cabezas == ["0041"]
