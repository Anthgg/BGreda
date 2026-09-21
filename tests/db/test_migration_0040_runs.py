"""Fase 010L — la migracion 0040 contra PostgreSQL real.

Lo que se prueba aqui es lo que la base garantiza SOLA, sin servicio delante:

1. sube: talonarios HOR y CI, tablas y el trigger del volumen;
2. una orden V2 que ya existia sobrevive al CHECK de cuatro origenes, y una
   fila con dos origenes se rechaza;
3. la capacidad: el contador sigue a lo activo en alta, cambio de cantidad,
   liberacion y borrado, y ninguna escritura directa pasa del 100 %;
4. cantidades y volumenes no positivos se rechazan —un delta negativo abriria
   hueco—;
5. la linea de una carga interna no se cuelga de otra carga, y una asignacion
   no puede tener dos padres;
6. bajar funciona sin datos y se niega con datos;
7. la cadena deja una sola cabeza, y es 0040.
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
MIGRATION_DB = "greda_migration_0040"
REPO_ROOT = Path(__file__).parents[2]

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL no definida: se omiten las pruebas con base de datos",
)

QR = "q" * 43
HUELLA = "a" * 64


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


async def _escalar(engine: AsyncEngine, sql: str, parametros: dict[str, object]) -> object:
    async with engine.begin() as connection:
        return await connection.scalar(text(sql), parametros)


async def _id(engine: AsyncEngine, sql: str, parametros: dict[str, object] | None = None) -> int:
    valor = await _escalar(engine, sql, parametros or {})
    assert valor is not None
    return int(str(valor))


async def _hornada(engine: AsyncEngine, codigo: str, capacidad: str = "1000") -> int:
    horno = await _id(
        engine,
        "INSERT INTO kilns (code, name, capacity_volume_cm3, firing_days_per_batch, active)"
        " VALUES (:c, 'Horno prueba', :cap, 3, true) RETURNING id",
        {"c": f"K-{codigo}", "cap": Decimal(capacidad)},
    )
    return await _id(
        engine,
        "INSERT INTO kiln_batches (code, kiln_id, firing_type, scheduled_date,"
        " kiln_name_snapshot, capacity_snapshot_cm3)"
        " VALUES (:c, :k, 'LOW', current_date, 'Horno prueba', :cap) RETURNING id",
        {"c": codigo, "k": horno, "cap": Decimal(capacidad)},
    )


async def _carga(engine: AsyncEngine, codigo: str) -> tuple[int, int]:
    carga = await _id(
        engine,
        "INSERT INTO internal_loads (code, name, low_fire_required, high_fire_required)"
        " VALUES (:c, 'Carga prueba', true, false) RETURNING id",
        {"c": codigo},
    )
    linea = await _id(
        engine,
        "INSERT INTO internal_load_lines (load_id, sort_order, name, quantity, length_cm,"
        " width_cm, height_cm, unit_volume_cm3, total_volume_cm3)"
        " VALUES (:l, 1, 'Taza', 20, 5, 5, 4, 100, 2000) RETURNING id",
        {"l": carga},
    )
    return carga, linea


async def _asignar(
    engine: AsyncEngine,
    hornada: int,
    carga: int,
    linea: int,
    cantidad: int,
    unitario: str = "100",
) -> int:
    return await _id(
        engine,
        "INSERT INTO kiln_batch_assignments (batch_id, source_kind, internal_load_id,"
        " internal_load_line_id, quantity, unit_volume_snapshot_cm3, assigned_volume_cm3,"
        " firing_mode, product_name_snapshot)"
        " VALUES (:b, 'INTERNAL', :c, :l, :q, :u, :v, 'SHARED', 'Taza') RETURNING id",
        {
            "b": hornada,
            "c": carga,
            "l": linea,
            "q": cantidad,
            "u": Decimal(unitario),
            "v": Decimal(cantidad) * Decimal(unitario),
        },
    )


async def _asignado(engine: AsyncEngine, hornada: int) -> Decimal:
    valor = await _escalar(
        engine, "SELECT assigned_volume_cm3 FROM kiln_batches WHERE id = :b", {"b": hornada}
    )
    return Decimal(str(valor))


async def test_sube_con_talonarios_tablas_y_trigger(migration_engine: AsyncEngine) -> None:
    _upgrade("0040")
    async with migration_engine.connect() as connection:
        prefijos = dict(
            (
                await connection.execute(
                    text(
                        "SELECT sequence_type, prefix FROM document_sequences"
                        " WHERE sequence_type IN ('KILN_BATCH', 'INTERNAL_LOAD')"
                    )
                )
            ).all()
        )
        trigger = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_kiln_batch_assignments_volume'"
            )
        )
    assert prefijos == {"KILN_BATCH": "HOR", "INTERNAL_LOAD": "CI"}
    assert trigger == 1


async def test_una_orden_v2_existente_sobrevive_al_cuarto_origen(
    migration_engine: AsyncEngine,
) -> None:
    """Ni una fila se toca: la orden de antes cumple su rama tal como esta."""
    _upgrade("0039")
    almacen = await _id(
        migration_engine, "INSERT INTO stock_locations (name) VALUES ('Taller') RETURNING id"
    )
    cotizacion = await _id(
        migration_engine,
        "INSERT INTO v2_quotations (code) VALUES ('CTZ-V2-X') RETURNING id",
    )
    puente = await _id(
        migration_engine,
        "INSERT INTO v2_production_handoffs (v2_quotation_id, commercial_fingerprint)"
        " VALUES (:q, :h) RETURNING id",
        {"q": cotizacion, "h": HUELLA},
    )
    orden = await _id(
        migration_engine,
        "INSERT INTO production_orders (code, stock_location_id, qr_token, v2_handoff_id)"
        " VALUES ('OP-ANTES', :a, :qr, :p) RETURNING id",
        {"a": almacen, "qr": QR, "p": puente},
    )

    _upgrade("0040")

    async with migration_engine.connect() as connection:
        fila = (
            await connection.execute(
                text(
                    "SELECT v2_handoff_id, v2_firing_handoff_id FROM production_orders"
                    " WHERE id = :o"
                ),
                {"o": orden},
            )
        ).one()
    assert fila == (puente, None)

    # Y una orden con DOS origenes —V2 y Solo Quema a la vez— no entra.
    servicio = await _id(
        migration_engine,
        "INSERT INTO v2_firing_quotations (code, status, created_at, updated_at)"
        " VALUES ('Q-V2-X', 'DRAFT', now(), now()) RETURNING id",
    )
    puente_sq = await _id(
        migration_engine,
        "INSERT INTO v2_firing_production_handoffs (v2_firing_quotation_id,"
        " commercial_fingerprint) VALUES (:s, :h) RETURNING id",
        {"s": servicio, "h": HUELLA},
    )
    otra_cotizacion = await _id(
        migration_engine,
        "INSERT INTO v2_quotations (code) VALUES ('CTZ-V2-Y') RETURNING id",
    )
    otro_puente = await _id(
        migration_engine,
        "INSERT INTO v2_production_handoffs (v2_quotation_id, commercial_fingerprint)"
        " VALUES (:q, :h) RETURNING id",
        {"q": otra_cotizacion, "h": HUELLA},
    )
    with pytest.raises(IntegrityError):
        await _id(
            migration_engine,
            "INSERT INTO production_orders (code, stock_location_id, qr_token,"
            " v2_handoff_id, v2_firing_handoff_id)"
            " VALUES ('OP-DOBLE', :a, :qr, :p, :s) RETURNING id",
            {"a": almacen, "qr": "r" * 43, "p": otro_puente, "s": puente_sq},
        )


async def test_el_contador_sigue_a_lo_activo(migration_engine: AsyncEngine) -> None:
    """Alta, cambio de cantidad, liberacion y borrado, todo por SQL directo."""
    _upgrade("0040")
    hornada = await _hornada(migration_engine, "HOR-CONT")
    carga, linea = await _carga(migration_engine, "CI-CONT")

    asignacion = await _asignar(migration_engine, hornada, carga, linea, 6)
    assert await _asignado(migration_engine, hornada) == Decimal(600)

    async with migration_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE kiln_batch_assignments SET quantity = 8, assigned_volume_cm3 = 800"
                " WHERE id = :a"
            ),
            {"a": asignacion},
        )
    assert await _asignado(migration_engine, hornada) == Decimal(800)

    async with migration_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE kiln_batch_assignments SET status = 'RELEASED', released_at = now()"
                " WHERE id = :a"
            ),
            {"a": asignacion},
        )
    assert await _asignado(migration_engine, hornada) == Decimal(0)

    # Borrar una LIBERADA no resta dos veces; borrar una ACTIVA si resta.
    otra = await _asignar(migration_engine, hornada, carga, linea, 3)
    assert await _asignado(migration_engine, hornada) == Decimal(300)
    async with migration_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM kiln_batch_assignments WHERE id = :a"), {"a": asignacion}
        )
    assert await _asignado(migration_engine, hornada) == Decimal(300)
    async with migration_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM kiln_batch_assignments WHERE id = :a"), {"a": otra}
        )
    assert await _asignado(migration_engine, hornada) == Decimal(0)


async def test_ninguna_escritura_directa_pasa_del_cien_por_cien(
    migration_engine: AsyncEngine,
) -> None:
    """Saltandose el servicio: la base sigue sin dejar pasar de la capacidad."""
    _upgrade("0040")
    hornada = await _hornada(migration_engine, "HOR-TOPE")
    carga, linea = await _carga(migration_engine, "CI-TOPE")

    await _asignar(migration_engine, hornada, carga, linea, 10)  # justo el 100 %
    assert await _asignado(migration_engine, hornada) == Decimal(1000)

    otra_carga, otra_linea = await _carga(migration_engine, "CI-TOPE-2")
    with pytest.raises(IntegrityError):
        await _asignar(migration_engine, hornada, otra_carga, otra_linea, 1)
    assert await _asignado(migration_engine, hornada) == Decimal(1000)


@pytest.mark.parametrize(
    ("cantidad", "unitario", "volumen"),
    [(0, "100", "0"), (-1, "100", "-100"), (1, "0", "0"), (2, "100", "150")],
)
async def test_cantidades_y_volumenes_no_positivos_o_incoherentes_se_rechazan(
    migration_engine: AsyncEngine, cantidad: int, unitario: str, volumen: str
) -> None:
    """Un delta negativo restaria del contador y abriria hueco para pasarse."""
    _upgrade("0040")
    hornada = await _hornada(migration_engine, "HOR-NEG")
    carga, linea = await _carga(migration_engine, "CI-NEG")
    with pytest.raises(IntegrityError):
        await _id(
            migration_engine,
            "INSERT INTO kiln_batch_assignments (batch_id, source_kind, internal_load_id,"
            " internal_load_line_id, quantity, unit_volume_snapshot_cm3, assigned_volume_cm3,"
            " firing_mode, product_name_snapshot)"
            " VALUES (:b, 'INTERNAL', :c, :l, :q, :u, :v, 'SHARED', 'Taza') RETURNING id",
            {
                "b": hornada,
                "c": carga,
                "l": linea,
                "q": cantidad,
                "u": Decimal(unitario),
                "v": Decimal(volumen),
            },
        )


async def test_la_linea_de_una_carga_no_se_cuelga_de_otra(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("0040")
    hornada = await _hornada(migration_engine, "HOR-FK")
    carga_a, _ = await _carga(migration_engine, "CI-A")
    _, linea_b = await _carga(migration_engine, "CI-B")
    with pytest.raises(IntegrityError):
        await _asignar(migration_engine, hornada, carga_a, linea_b, 1)


async def test_una_asignacion_no_tiene_dos_padres(migration_engine: AsyncEngine) -> None:
    _upgrade("0040")
    hornada = await _hornada(migration_engine, "HOR-2P")
    carga, linea = await _carga(migration_engine, "CI-2P")
    almacen = await _id(
        migration_engine, "INSERT INTO stock_locations (name) VALUES ('Taller') RETURNING id"
    )
    cotizacion = await _id(
        migration_engine,
        "INSERT INTO v2_quotations (code) VALUES ('CTZ-2P') RETURNING id",
    )
    puente = await _id(
        migration_engine,
        "INSERT INTO v2_production_handoffs (v2_quotation_id, commercial_fingerprint)"
        " VALUES (:q, :h) RETURNING id",
        {"q": cotizacion, "h": HUELLA},
    )
    orden = await _id(
        migration_engine,
        "INSERT INTO production_orders (code, stock_location_id, qr_token, v2_handoff_id)"
        " VALUES ('OP-2P', :a, :qr, :p) RETURNING id",
        {"a": almacen, "qr": QR, "p": puente},
    )
    with pytest.raises(IntegrityError):
        await _id(
            migration_engine,
            "INSERT INTO kiln_batch_assignments (batch_id, source_kind, production_order_id,"
            " internal_load_id, internal_load_line_id, quantity, unit_volume_snapshot_cm3,"
            " assigned_volume_cm3, firing_mode, product_name_snapshot)"
            " VALUES (:b, 'INTERNAL', :o, :c, :l, 1, 100, 100, 'SHARED', 'Taza') RETURNING id",
            {"b": hornada, "o": orden, "c": carga, "l": linea},
        )


async def test_bajar_sin_datos_funciona_y_con_datos_se_niega(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("0040")
    limpio = _alembic("downgrade", "0039")
    assert limpio.returncode == 0, limpio.stderr
    _upgrade("0040")
    await _hornada(migration_engine, "HOR-BAJA")
    resultado = _alembic("downgrade", "0039")
    assert resultado.returncode != 0
    assert "No se puede bajar de 0040" in resultado.stderr + resultado.stdout
    async with migration_engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0040"


async def test_toda_la_cadena_deja_una_sola_cabeza_y_es_0040(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("head")
    async with migration_engine.connect() as connection:
        cabezas = list(
            (await connection.scalars(text("SELECT version_num FROM alembic_version"))).all()
        )
    assert cabezas == ["0040"]
