"""Fase 009K.4 — la migracion 0027 se ejecuta de verdad contra PostgreSQL.

Como las de 0015 a 0026: alembic real en un subproceso, sobre una base propia,
la ida y la vuelta.

Esta revision es la mas delicada de toda la fase 009K, porque **relaja dos NOT
NULL**. Lo que aqui importa:

1. que las once muestras y las cuatro ordenes que ya existen **no se toquen**.
   Ni backfill de `prototype_id`, ni ordenes retroactivas;
2. que lo que se pierde en NOT NULL se recupere en el CHECK: una orden sigue
   teniendo exactamente un origen, y ahora lo dice la base;
3. que el UNIQUE de `prototype_id` **muerda**. Es lo unico que impide que dos
   cobros simultaneos de la misma cotizacion de prototipo creen dos ordenes,
   cada una dispuesta a gastar el barro entero;
4. que el downgrade **se niegue** cuando ya hay ordenes de muestra, en vez de
   fallar a medias contra el NOT NULL con un mensaje que no explica nada.
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
MIGRATION_DB = "greda_migration_0027"
REPO_ROOT = Path(__file__).parents[2]

#: Los nombres reales que deja la convencion del proyecto.
CK_ORIGEN = "ck_production_orders_exactly_one_origin"
FK_PROTOTIPO = "fk_production_orders_prototype_id_prototypes"
UQ_PROTOTIPO = "uq_production_orders_prototype_id"

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


# ---------------------------------------------------------------------------
# Semillas. Lo minimo que exige cada tabla, sin pasar por los modelos: una
# prueba de migracion tiene que poder sembrar una base que todavia no conoce
# el codigo de hoy.
# ---------------------------------------------------------------------------
async def _almacen(engine: AsyncEngine, nombre: str) -> int:
    async with engine.begin() as connection:
        valor = await connection.scalar(
            text(
                "INSERT INTO stock_locations (name, active, created_at, updated_at)"
                " VALUES (:nombre, true, now(), now()) RETURNING id"
            ),
            {"nombre": nombre},
        )
    assert valor is not None
    return int(valor)


async def _muestra(engine: AsyncEngine, codigo: str) -> int:
    async with engine.begin() as connection:
        valor = await connection.scalar(
            text(
                "INSERT INTO prototypes"
                " (code, name, quantity, status, approval, requested_at, created_at, updated_at)"
                " VALUES (:codigo, :codigo, 1, 'CREATED', 'PENDING', now(), now(), now())"
                " RETURNING id"
            ),
            {"codigo": codigo},
        )
    assert valor is not None
    return int(valor)


#: Alta de una orden. Las dos columnas de origen se nombran SIEMPRE y viajan
#: como parametros: componer la lista de columnas segun los argumentos daria
#: una consulta distinta en cada llamada, y una consulta que se arma sola es la
#: que nadie revisa.
_INSERT_ORDEN = text(
    "INSERT INTO production_orders"
    " (code, quotation_id, prototype_id, stock_location_id, status, qr_token,"
    "  created_at, updated_at)"
    " VALUES (:codigo, :quotation_id, :prototype_id, :almacen, 'CREATED', :token,"
    "  now(), now())"
)

#: La misma, para una base que todavia no ha llegado a 0027 y no tiene la
#: columna del segundo origen.
_INSERT_ORDEN_0026 = text(
    "INSERT INTO production_orders"
    " (code, quotation_id, stock_location_id, status, qr_token, created_at, updated_at)"
    " VALUES (:codigo, :quotation_id, :almacen, 'CREATED', :token, now(), now())"
)


async def _cotizacion(engine: AsyncEngine, codigo: str) -> int:
    async with engine.begin() as connection:
        valor = await connection.scalar(
            text(
                "INSERT INTO quotations"
                " (code, status, workflow, source_fingerprint,"
                "  commercial_factor_default_snapshot, commercial_factor,"
                "  created_at, updated_at)"
                " VALUES (:codigo, 'CONFIRMED', 'LEGACY', :huella, 3, 3, now(), now())"
                " RETURNING id"
            ),
            {"codigo": codigo, "huella": "0" * 64},
        )
    assert valor is not None
    return int(valor)


# ---------------------------------------------------------------------------
# Pruebas
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_0026_a_0027_abre_el_segundo_origen(migration_engine: AsyncEngine) -> None:
    """MIGRATION_0026_TO_0027: PASS.

    Aparece `prototype_id` con su clave ajena y su UNIQUE; los dos NOT NULL de
    la rama de cotizacion se aflojan; y el CHECK que sustituye a lo que se
    pierde queda en pie.
    """
    _upgrade("0026")
    assert await _current(migration_engine) == "0026"
    assert await _columna(migration_engine, "production_orders", "prototype_id") is None
    assert await _columna(migration_engine, "production_orders", "quotation_id") == "NO"
    assert await _columna(migration_engine, "production_order_lines", "quotation_item_id") == "NO"

    _upgrade("0027")
    assert await _current(migration_engine) == "0027"
    assert await _columna(migration_engine, "production_orders", "prototype_id") == "YES"
    assert await _columna(migration_engine, "production_orders", "quotation_id") == "YES"
    assert await _columna(migration_engine, "production_order_lines", "quotation_item_id") == "YES"
    # El almacen NO se relaja: una orden que no sabe de donde sale su material
    # no es una orden.
    assert await _columna(migration_engine, "production_orders", "stock_location_id") == "NO"

    for nombre in (CK_ORIGEN, FK_PROTOTIPO, UQ_PROTOTIPO):
        assert await _restriccion(migration_engine, nombre) == nombre, nombre


@pytest.mark.asyncio
async def test_0027_no_toca_ni_una_orden_ni_una_muestra(migration_engine: AsyncEngine) -> None:
    """MIGRATION_0027_UPDATE_COUNT: 0. LEGACY_PRODUCTION_ORDER_BACKFILL: NO.

    Se siembran una orden y una muestra ANTES de migrar. Es la unica forma de
    distinguir «no rellene nada» de «no habia nada que rellenar»: la muestra
    tiene que seguir sin orden, y la orden tiene que seguir con su cotizacion.
    """
    _upgrade("0026")
    almacen = await _almacen(migration_engine, "Almacén histórico 0027")
    cotizacion = await _cotizacion(migration_engine, "CTZ-HIST-0027")
    await _muestra(migration_engine, "PRT-HIST-0027")
    async with migration_engine.begin() as connection:
        await connection.execute(
            _INSERT_ORDEN_0026,
            {
                "codigo": "OP-HIST-0027",
                "quotation_id": cotizacion,
                "almacen": almacen,
                "token": "t" * 43,
            },
        )

    _upgrade("0027")
    async with migration_engine.connect() as connection:
        fila = (
            await connection.execute(
                text(
                    "SELECT quotation_id, prototype_id FROM production_orders"
                    " WHERE code = 'OP-HIST-0027'"
                )
            )
        ).one()
        huerfanas = await connection.scalar(
            text(
                "SELECT count(*) FROM prototypes p WHERE NOT EXISTS ("
                " SELECT 1 FROM production_orders o WHERE o.prototype_id = p.id)"
            )
        )
    assert fila == (cotizacion, None), fila
    assert huerfanas == 1, "la muestra histórica sigue sin orden, que es lo correcto"


@pytest.mark.asyncio
async def test_una_orden_sigue_teniendo_exactamente_un_origen(
    migration_engine: AsyncEngine,
) -> None:
    """El CHECK muerde por los dos lados. Sin origen no sabria que fabricar;
    con los dos tendria dos modelos de material contradictorios."""
    _upgrade("0027")
    almacen = await _almacen(migration_engine, "Almacén origen 0027")
    cotizacion = await _cotizacion(migration_engine, "CTZ-XOR-0027")
    muestra = await _muestra(migration_engine, "PRT-XOR-0027")

    async with migration_engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                _INSERT_ORDEN,
                {
                    "codigo": "OP-SIN-0027",
                    "quotation_id": None,
                    "prototype_id": None,
                    "almacen": almacen,
                    "token": "a" * 43,
                },
            )

    async with migration_engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                _INSERT_ORDEN,
                {
                    "codigo": "OP-DOS-0027",
                    "quotation_id": cotizacion,
                    "prototype_id": muestra,
                    "almacen": almacen,
                    "token": "b" * 43,
                },
            )

    # Y los dos casos legitimos entran sin protestar.
    async with migration_engine.begin() as connection:
        await connection.execute(
            _INSERT_ORDEN,
            {
                "codigo": "OP-CTZ-0027",
                "quotation_id": cotizacion,
                "prototype_id": None,
                "almacen": almacen,
                "token": "c" * 43,
            },
        )
        await connection.execute(
            _INSERT_ORDEN,
            {
                "codigo": "OP-PRT-0027",
                "quotation_id": None,
                "prototype_id": muestra,
                "almacen": almacen,
                "token": "d" * 43,
            },
        )


@pytest.mark.asyncio
async def test_una_muestra_no_admite_dos_ordenes(migration_engine: AsyncEngine) -> None:
    """El UNIQUE es lo unico que para dos cobros simultaneos.

    Comprobarlo en el servicio no basta: las dos peticiones pasan la lectura
    previa antes de que ninguna haya insertado, y la segunda orden estaria
    dispuesta a gastar el barro entero por segunda vez.
    """
    _upgrade("0027")
    almacen = await _almacen(migration_engine, "Almacén único 0027")
    muestra = await _muestra(migration_engine, "PRT-UNICA-0027")

    async with migration_engine.begin() as connection:
        await connection.execute(
            _INSERT_ORDEN,
            {
                "codigo": "OP-UNA-0027",
                "quotation_id": None,
                "prototype_id": muestra,
                "almacen": almacen,
                "token": "e" * 43,
            },
        )

    async with migration_engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                _INSERT_ORDEN,
                {
                    "codigo": "OP-OTRA-0027",
                    "quotation_id": None,
                    "prototype_id": muestra,
                    "almacen": almacen,
                    "token": "f" * 43,
                },
            )


@pytest.mark.asyncio
async def test_la_vuelta_a_0026_se_niega_si_hay_ordenes_de_muestra(
    migration_engine: AsyncEngine,
) -> None:
    """DOWNGRADE_WITH_PROTOTYPE_ORDERS_REJECTED: PASS.

    Sin la guarda, el downgrade fallaria igual —contra el NOT NULL de
    `quotation_id`— pero a mitad de camino y con un mensaje que no explica por
    que. Se aborta antes, diciendo cuantas ordenes lo impiden.
    """
    _upgrade("0027")
    almacen = await _almacen(migration_engine, "Almacén bajada 0027")
    muestra = await _muestra(migration_engine, "PRT-DOWN-0027")
    async with migration_engine.begin() as connection:
        await connection.execute(
            _INSERT_ORDEN,
            {
                "codigo": "OP-DOWN-0027",
                "quotation_id": None,
                "prototype_id": muestra,
                "almacen": almacen,
                "token": "g" * 43,
            },
        )

    resultado = _alembic("downgrade", "0026")
    assert resultado.returncode != 0, resultado.stdout
    assert "0027 downgrade bloqueado" in resultado.stderr, resultado.stderr
    # Y la base se queda donde estaba, entera.
    assert await _current(migration_engine) == "0027"
    async with migration_engine.connect() as connection:
        vive = await connection.scalar(
            text("SELECT count(*) FROM production_orders WHERE code = 'OP-DOWN-0027'")
        )
    assert vive == 1


@pytest.mark.asyncio
async def test_la_vuelta_a_0026_pasa_cuando_no_hay_ordenes_de_muestra(
    migration_engine: AsyncEngine,
) -> None:
    """El camino de vuelta existe de verdad, y no se lleva por delante nada."""
    _upgrade("0027")
    almacen = await _almacen(migration_engine, "Almacén vuelta 0027")
    cotizacion = await _cotizacion(migration_engine, "CTZ-VUELTA-0027")
    async with migration_engine.begin() as connection:
        await connection.execute(
            _INSERT_ORDEN,
            {
                "codigo": "OP-VUELTA-0027",
                "quotation_id": cotizacion,
                "prototype_id": None,
                "almacen": almacen,
                "token": "h" * 43,
            },
        )

    _downgrade("0026")
    assert await _current(migration_engine) == "0026"
    assert await _columna(migration_engine, "production_orders", "prototype_id") is None
    assert await _columna(migration_engine, "production_orders", "quotation_id") == "NO"
    assert await _columna(migration_engine, "production_order_lines", "quotation_item_id") == "NO"
    for nombre in (CK_ORIGEN, FK_PROTOTIPO, UQ_PROTOTIPO):
        assert await _restriccion(migration_engine, nombre) is None, nombre

    async with migration_engine.connect() as connection:
        vive = await connection.scalar(
            text("SELECT count(*) FROM production_orders WHERE code = 'OP-VUELTA-0027'")
        )
    assert vive == 1


@pytest.mark.asyncio
async def test_subir_hasta_la_cabeza_conserva_lo_de_0027_y_deja_una_sola(
    migration_engine: AsyncEngine,
) -> None:
    """Llegar al final de la cadena no deshace lo que anadio 0027.

    Antes esta prueba fijaba que la cabeza ERA 0027. Esa afirmacion acompana
    siempre a la ULTIMA revision y se retira de la anterior —vive ahora en
    `tests/unit/test_migration_0028.py`—; dejarla aqui obligaba a reescribir la
    prueba en cada fase y, mientras tanto, no comprobaba nada de 0027.

    Lo que si importa de 0027 al llegar a la cabeza es que sus artefactos
    sobrevivan: la columna del segundo origen y las tres restricciones que
    garantizan que una orden tiene exactamente un origen y que una muestra no
    genera dos.
    """
    _upgrade("head")

    heads = _alembic("heads")
    assert heads.returncode == 0, heads.stderr
    assert heads.stdout.count("(head)") == 1, heads.stdout

    assert await _columna(migration_engine, "production_orders", "prototype_id") == "YES"
    for nombre in (CK_ORIGEN, FK_PROTOTIPO, UQ_PROTOTIPO):
        assert await _restriccion(migration_engine, nombre) == nombre, nombre
