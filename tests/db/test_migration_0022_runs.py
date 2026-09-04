"""Fase 009K.1 — la migracion 0022 se ejecuta de verdad contra PostgreSQL.

Aqui se comprueba lo que solo dice la base: que la ida funciona, que lo
historico queda en NULL sin inventarse nada, que el indice unico PARCIAL
permite varias cotizaciones por muestra pero un solo borrador vivo, y que la
vuelta se niega en voz alta cuando habria algo que perder.
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
MIGRATION_DB = "greda_migration_0022"
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
    # S603: comando fijo y argumentos literales de esta prueba.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _upgrade(revision: str) -> str:
    result = _alembic("upgrade", revision)
    assert result.returncode == 0, f"upgrade {revision} fallo:\n{result.stdout}\n{result.stderr}"
    return f"{result.stdout}\n{result.stderr}"


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


async def _scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as connection:
        return await connection.scalar(text(sql), params)


async def _current(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        if await connection.scalar(text("SELECT to_regclass('alembic_version')")) is None:
            return None
        return await connection.scalar(text("SELECT version_num FROM alembic_version"))


async def _prototipo(engine: AsyncEngine, code: str) -> int:
    """Una muestra minima, por SQL directo: aqui no hay servicios, hay esquema."""
    async with engine.begin() as connection:
        return int(
            await connection.scalar(
                text(
                    "INSERT INTO prototypes (code, name, quantity, status, approval, requested_at)"
                    " VALUES (:code, :code, 1, 'CREATED', 'PENDING', now()) RETURNING id"
                ),
                {"code": code},
            )
            or 0
        )


async def _cotizacion(engine: AsyncEngine, code: str, status: str, origen: int | None) -> int:
    async with engine.begin() as connection:
        return int(
            await connection.scalar(
                text(
                    "INSERT INTO quotations (code, name, status, workflow, quantity,"
                    " source_fingerprint, origin_prototype_id)"
                    " VALUES (:code, :code, :status, 'COTIZADOR', 1, :fp, :origen)"
                    " RETURNING id"
                ),
                {"code": code, "status": status, "fp": code.ljust(64, "0")[:64], "origen": origen},
            )
            or 0
        )


# ---------------------------------------------------------------------------
# Ida
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_la_0022_sube_desde_0021(migration_engine: AsyncEngine) -> None:
    _upgrade("0021")
    assert await _current(migration_engine) == "0021"

    salida = _upgrade("0022")
    assert "Running upgrade 0021 -> 0022" in salida
    assert await _current(migration_engine) == "0022"

    # Una sola cabeza: dos dejarian a `alembic upgrade head` sin saber a cual ir.
    heads = _alembic("heads")
    assert len([linea for linea in heads.stdout.splitlines() if linea.strip()]) == 1


@pytest.mark.asyncio
async def test_lo_historico_queda_en_nulo_y_no_se_inventa(migration_engine: AsyncEngine) -> None:
    """BACKFILL: 0. NULL dice «nadie lo declaro», que es la verdad."""
    _upgrade("0021")
    prototipo = await _prototipo(migration_engine, "PRT-HIST-0001")
    async with migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO quotations (code, name, status, workflow, quantity,"
                " source_fingerprint)"
                " VALUES ('CTZ-HIST-1', 'historica', 'CONFIRMED', 'LEGACY', 1, :fp)"
            ),
            {"fp": "h" * 64},
        )
        # Un material historico: producto real cualquiera del catalogo de prueba.
        producto = await connection.scalar(
            text("INSERT INTO product_categories (name) VALUES ('QA 0022') RETURNING id")
        )
        material = await connection.scalar(
            text(
                "INSERT INTO products (internal_reference, name, product_type,"
                " product_category_id, active) VALUES ('QA-0022-1', 'Arcilla QA',"
                " 'RAW_MATERIAL', :cat, true) RETURNING id"
            ),
            {"cat": producto},
        )
        await connection.execute(
            text(
                "INSERT INTO prototype_material_lines (prototype_id, product_id, sort_order,"
                " quantity, uom_code) VALUES (:p, :m, 0, 30, 'g')"
            ),
            {"p": prototipo, "m": material},
        )

    _upgrade("0022")

    assert (
        await _scalar(
            migration_engine,
            "SELECT count(*) FROM quotations WHERE origin_prototype_id IS NOT NULL",
        )
        == 0
    )
    assert (
        await _scalar(
            migration_engine,
            "SELECT count(*) FROM prototypes WHERE technical_specifications IS NOT NULL",
        )
        == 0
    )
    assert (
        await _scalar(
            migration_engine,
            "SELECT count(*) FROM prototype_material_lines WHERE material_role IS NOT NULL",
        )
        == 0
    )


@pytest.mark.asyncio
async def test_el_backend_anterior_sigue_pudiendo_operar(migration_engine: AsyncEngine) -> None:
    """OLD_0021_BACKEND_SCHEMA_COMPATIBLE_WITH_0022.

    El despliegue es DB primero. Entre que la base llega a 0022 y el backend
    009K.1 recibe trafico, la revision que sigue sirviendo es la de 009K, y esa
    lee y escribe `prototype_material_lines.quantity` sin saber nada de las
    columnas nuevas.

    Esta prueba hace exactamente lo que haria ese backend: leer la columna e
    insertar una linea nombrando solo las que conocia. Si algun dia alguien
    renombra `quantity`, esto se cae aqui y no en produccion.
    """
    _upgrade("0022")
    prototipo = await _prototipo(migration_engine, "PRT-0022-COMPAT")

    async with migration_engine.begin() as connection:
        categoria = await connection.scalar(
            text("INSERT INTO product_categories (name) VALUES ('QA compat') RETURNING id")
        )
        material = await connection.scalar(
            text(
                "INSERT INTO products (internal_reference, name, product_type,"
                " product_category_id, active) VALUES ('QA-COMPAT-1', 'Arcilla compat',"
                " 'RAW_MATERIAL', :cat, true) RETURNING id"
            ),
            {"cat": categoria},
        )
        # El INSERT tal cual lo escribia el modelo de 009K.
        await connection.execute(
            text(
                "INSERT INTO prototype_material_lines (prototype_id, product_id, sort_order,"
                " quantity, uom_code, product_name_snapshot,"
                " product_internal_reference_snapshot)"
                " VALUES (:p, :m, 0, 30, 'g', 'Arcilla compat', 'QA-COMPAT-1')"
            ),
            {"p": prototipo, "m": material},
        )

    # Y el SELECT que hacia. Devuelve lo previsto, que es lo que significaba.
    assert await _scalar(
        migration_engine,
        "SELECT quantity FROM prototype_material_lines WHERE prototype_id = :p",
        p=prototipo,
    ) == Decimal(30)

    # Las columnas nuevas nacen nulas: nadie las relleno por el.
    assert (
        await _scalar(
            migration_engine,
            "SELECT count(*) FROM prototype_material_lines"
            " WHERE quantity_actual IS NOT NULL OR material_role IS NOT NULL OR stage IS NOT NULL",
        )
        == 0
    )


# ---------------------------------------------------------------------------
# La regla que justifica el indice parcial
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_muestra_admite_varias_cotizaciones_pero_un_solo_borrador(
    migration_engine: AsyncEngine,
) -> None:
    """PROTOTYPE_MULTIPLE_HISTORICAL_QUOTES / PROTOTYPE_MULTIPLE_ACTIVE_DRAFTS = 0."""
    _upgrade("0022")
    prototipo = await _prototipo(migration_engine, "PRT-0022-0001")

    await _cotizacion(migration_engine, "CTZ-0022-A", "DRAFT", prototipo)

    # Un segundo borrador de la misma muestra no puede existir: es lo que
    # impide que un doble clic deje dos gemelas y nadie sepa cual vale.
    with pytest.raises(IntegrityError):
        await _cotizacion(migration_engine, "CTZ-0022-B", "DRAFT", prototipo)

    # Pero confirmarla libera el hueco: recotizar mas adelante es legitimo.
    async with migration_engine.begin() as connection:
        await connection.execute(
            text("UPDATE quotations SET status = 'CONFIRMED' WHERE code = 'CTZ-0022-A'")
        )
    await _cotizacion(migration_engine, "CTZ-0022-C", "DRAFT", prototipo)

    # Y anular tambien.
    async with migration_engine.begin() as connection:
        await connection.execute(
            text("UPDATE quotations SET status = 'CANCELLED' WHERE code = 'CTZ-0022-C'")
        )
    await _cotizacion(migration_engine, "CTZ-0022-D", "DRAFT", prototipo)

    assert (
        await _scalar(
            migration_engine,
            "SELECT count(*) FROM quotations WHERE origin_prototype_id = :p",
            p=prototipo,
        )
        == 3
    )


@pytest.mark.asyncio
async def test_el_origen_no_se_borra_por_arrastre(migration_engine: AsyncEngine) -> None:
    """FK RESTRICT: borrar la muestra de la que nacio una cotizacion se rechaza."""
    _upgrade("0022")
    prototipo = await _prototipo(migration_engine, "PRT-0022-FK")
    await _cotizacion(migration_engine, "CTZ-0022-FK", "DRAFT", prototipo)

    with pytest.raises(IntegrityError):
        async with migration_engine.begin() as connection:
            await connection.execute(text("DELETE FROM prototypes WHERE id = :p"), {"p": prototipo})


# ---------------------------------------------------------------------------
# La linea comercial
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_un_cargo_no_puede_ser_gratis_ni_huerfano(migration_engine: AsyncEngine) -> None:
    _upgrade("0022")
    prototipo = await _prototipo(migration_engine, "PRT-0022-CARGO")
    cotizacion = await _cotizacion(migration_engine, "CTZ-0022-CARGO", "DRAFT", prototipo)

    async def _cargo(**valores: object) -> None:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO quotation_commercial_lines (quotation_id, kind, description,"
                    " prototype_id, quantity, manual_net_amount)"
                    " VALUES (:q, :kind, :desc, :proto, :qty, :amount)"
                ),
                {"q": cotizacion, **valores},
            )

    # Un cargo de cero no es un cargo: es una linea que nadie revisaria.
    with pytest.raises(IntegrityError):
        await _cargo(kind="PROTOTYPE", desc="Prototipo", proto=prototipo, qty=1, amount=0)

    # Un cargo de prototipo sin prototipo no se puede auditar.
    with pytest.raises(IntegrityError):
        await _cargo(kind="PROTOTYPE", desc="Prototipo", proto=None, qty=1, amount=200)

    # El caso legitimo.
    await _cargo(
        kind="PROTOTYPE", desc="Prototipo PRT-0022-CARGO", proto=prototipo, qty=1, amount=200
    )
    assert await _scalar(migration_engine, "SELECT count(*) FROM quotation_commercial_lines") == 1


# ---------------------------------------------------------------------------
# Vuelta
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_la_vuelta_se_niega_cuando_hay_algo_que_perder(
    migration_engine: AsyncEngine,
) -> None:
    """Misma politica que 0021: fallar en voz alta antes que borrar en silencio."""
    _upgrade("0022")
    prototipo = await _prototipo(migration_engine, "PRT-0022-DOWN")
    await _cotizacion(migration_engine, "CTZ-0022-DOWN", "DRAFT", prototipo)

    fallo = _alembic("downgrade", "0021")
    assert fallo.returncode != 0
    assert "origin" in (fallo.stdout + fallo.stderr) or "0022 no puede revertirse" in (
        fallo.stdout + fallo.stderr
    )
    assert await _current(migration_engine) == "0022"


@pytest.mark.asyncio
async def test_la_vuelta_funciona_cuando_no_hay_nada_nuevo(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("0022")
    resultado = _alembic("downgrade", "0021")
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert await _current(migration_engine) == "0021"
    assert (
        await _scalar(migration_engine, "SELECT to_regclass('quotation_commercial_lines')") is None
    )
