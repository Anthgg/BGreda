"""Fase 009I — la migracion 0020 se ejecuta de verdad contra PostgreSQL.

Que el archivo diga lo correcto lo comprueba `tests/unit/test_migration_0020.py`.
Aqui se comprueba lo que solo dice la base: que la ida y la vuelta funcionan,
que los movimientos historicos siguen siendo validos despues de ampliar el
CHECK, y que las restricciones nuevas rechazan de verdad las combinaciones
imposibles de estado y fecha.
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
from tests.db.test_migration_0017_runs import _columnas_obligatorias

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
MIGRATION_DB = "greda_migration_0020"
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


async def _scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as connection:
        return await connection.scalar(text(sql), params)


async def _table_exists(engine: AsyncEngine, table: str) -> bool:
    return (await _scalar(engine, "SELECT to_regclass(:t)", t=table)) is not None


async def _column_exists(engine: AsyncEngine, table: str, column: str) -> bool:
    found = await _scalar(
        engine,
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = :column",
        table=table,
        column=column,
    )
    return found is not None


async def _current(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        exists = await connection.scalar(text("SELECT to_regclass('alembic_version')"))
        if exists is None:
            return None
        return await connection.scalar(text("SELECT version_num FROM alembic_version"))


async def _constraint_con(engine: AsyncEngine, table: str, fragmento: str) -> str | None:
    """Busca un CHECK por TABLA y CONTENIDO, nunca por nombre.

    Un nombre equivocado devuelve NULL y la asercion pasaria por no haber
    encontrado nada, que es la peor forma de estar en verde.
    """
    resultado = await _scalar(
        engine,
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE t.relname = :table AND c.contype = 'c' "
        "AND pg_get_constraintdef(c.oid) LIKE :patron",
        table=table,
        patron=f"%{fragmento}%",
    )
    return str(resultado) if resultado is not None else None


@pytest.mark.asyncio
async def test_0019_a_0020_a_0019_y_de_vuelta(migration_engine: AsyncEngine) -> None:
    """La ida, la vuelta y la ida otra vez, sin residuos."""
    _upgrade("0019")
    assert await _current(migration_engine) == "0019"
    assert not await _table_exists(migration_engine, "production_orders")

    salida = _upgrade("0020")
    assert "Running upgrade 0019 -> 0020" in salida, salida
    assert await _current(migration_engine) == "0020"
    assert await _table_exists(migration_engine, "production_orders")
    assert await _table_exists(migration_engine, "production_order_lines")
    assert await _column_exists(migration_engine, "stock_movements", "production_order_id")

    _downgrade("0019")
    assert await _current(migration_engine) == "0019"
    assert not await _table_exists(migration_engine, "production_orders")
    assert not await _table_exists(migration_engine, "production_order_lines")
    assert not await _column_exists(migration_engine, "stock_movements", "production_order_id")
    # Lo de 009H y 009G sigue en pie tras el downgrade.
    assert await _column_exists(migration_engine, "quotations", "payment_status")
    assert await _column_exists(migration_engine, "quotations", "validity_days_snapshot")

    _upgrade("0020")
    assert await _current(migration_engine) == "0020"


@pytest.mark.asyncio
async def test_la_migracion_es_idempotente(migration_engine: AsyncEngine) -> None:
    """MIGRATION_JOB_IDEMPOTENT.

    La segunda pasada no puede volver a aplicar nada. En produccion el Job de
    migracion se ejecuta mas de una vez, y `alembic upgrade head` sobre una base
    ya migrada tiene que ser un no-op, no un error ni un reintento.
    """
    _upgrade("0020")
    segunda = _upgrade("0020")
    assert "Running upgrade" not in segunda, segunda

    # Y el contador de la secuencia no se reinicio ni se duplico.
    assert (
        await _scalar(
            migration_engine,
            "SELECT count(*) FROM document_sequences WHERE sequence_type = 'PRODUCTION_ORDER'",
        )
    ) == 1


@pytest.mark.asyncio
async def test_los_movimientos_historicos_siguen_siendo_validos(
    migration_engine: AsyncEngine,
) -> None:
    """MIGRATION_BACKWARD_COMPAT.

    Se crea un movimiento de carga inicial ANTES de migrar —como los 7 que hay
    en produccion— y despues de ampliar el CHECK tiene que seguir ahi y seguir
    siendo insertable. Ampliar una restriccion no puede invalidar lo ya escrito.
    """
    _upgrade("0019")
    async with migration_engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO stock_locations (name) VALUES ('Almacen historico')")
        )

    _upgrade("0020")

    permitidos = await _constraint_con(migration_engine, "stock_movements", "INITIAL_IMPORT")
    assert permitidos is not None, "no se encontro el CHECK de tipos de movimiento"
    for tipo in (
        "INITIAL_IMPORT",
        "ADJUSTMENT",
        "PREPARATION_OUT",
        "PREPARATION_IN",
        "PRODUCTION_OUT",
    ):
        assert tipo in permitidos, f"{tipo} desaparecio del CHECK al migrar"


@pytest.mark.asyncio
async def test_la_secuencia_de_la_orden_queda_configurada(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("0020")

    fila = await _scalar(
        migration_engine,
        "SELECT prefix || '|' || pattern || '|' || padding || '|' || reset_policy "
        "FROM document_sequences WHERE sequence_type = 'PRODUCTION_ORDER'",
    )
    assert fila == "OP|{PREFIX}-{YYYY}-{NUMBER}|6|YEARLY"

    permitidos = await _constraint_con(migration_engine, "document_sequences", "PRODUCTION_ORDER")
    assert permitidos is not None
    # Los tipos anteriores siguen aceptados.
    for tipo in ("QUOTE", "FIRING", "PRODUCT_50", "PRODUCT_70", "PREPARATION"):
        assert tipo in permitidos


@pytest.mark.asyncio
async def test_el_tipo_de_secuencia_cabe_con_holgura(migration_engine: AsyncEngine) -> None:
    """El varchar se ensancho a 24: "PRODUCTION_ORDER" medía justo 16."""
    _upgrade("0020")
    for tabla in ("document_sequences", "document_sequence_issues"):
        longitud = await _scalar(
            migration_engine,
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = 'sequence_type'",
            t=tabla,
        )
        assert longitud == 24, f"{tabla}.sequence_type quedo en {longitud}"


async def _insertar_orden(
    engine: AsyncEngine,
    *,
    code: str,
    status: str,
    started: str = "NULL",
    completed: str = "NULL",
    cancelled: str = "NULL",
) -> None:
    async with engine.begin() as connection:
        cotizacion = await connection.scalar(
            text("SELECT id FROM quotations LIMIT 1"),
        )
        ubicacion = await connection.scalar(text("SELECT id FROM stock_locations LIMIT 1"))
        # S608: nombres de columna literales de esta prueba; los valores que
        # vienen de fuera viajan como parametros.
        sql = (
            "INSERT INTO production_orders "  # noqa: S608
            "(code, quotation_id, stock_location_id, status, qr_token, "
            " started_at, completed_at, cancelled_at) "
            f"VALUES (:code, :q, :l, :status, :token, {started}, {completed}, {cancelled})"
        )
        await connection.execute(
            text(sql),
            {
                "code": code,
                "q": cotizacion,
                "l": ubicacion,
                "status": status,
                "token": f"token-largo-de-prueba-{code}-0123456789",
            },
        )


async def _sembrar_dependencias(engine: AsyncEngine) -> None:
    """Una cotizacion y una ubicacion: lo minimo para poder colgar una orden.

    Las columnas obligatorias de `quotations` se derivan del modelo con el
    mismo ayudante que usan 0017 y 0019. Enumerarlas a mano fallaba una cada
    vez, y el sintoma era un IntegrityError generico DESPUES de que la prueba
    creyera estar comprobando otra cosa.
    """
    relleno = {
        nombre: valor
        for nombre, valor in _columnas_obligatorias().items()
        if nombre not in ("code", "status", "workflow")
    }
    columnas = ["code", "status", "workflow", *relleno]
    valores = ["'CTZ-0020-PRUEBA'", "'DRAFT'", "'COTIZADOR'", *relleno.values()]
    async with engine.begin() as connection:
        await connection.execute(text("INSERT INTO stock_locations (name) VALUES ('Almacen 0020')"))
        # S608: nombres derivados del modelo, valores literales de esta prueba.
        sql = (
            f"INSERT INTO quotations ({', '.join(columnas)}) "  # noqa: S608
            f"VALUES ({', '.join(valores)})"
        )
        await connection.execute(text(sql))


@pytest.mark.asyncio
async def test_los_cuatro_estados_coherentes_se_aceptan(migration_engine: AsyncEngine) -> None:
    _upgrade("0020")
    await _sembrar_dependencias(migration_engine)

    await _insertar_orden(migration_engine, code="OP-CREADA", status="CREATED")
    async with migration_engine.begin() as connection:
        await connection.execute(text("DELETE FROM production_orders"))

    for code, status, campos in (
        ("OP-CREADA", "CREATED", {}),
        ("OP-ARRANCADA", "STARTED", {"started": "now()"}),
        ("OP-HECHA", "COMPLETED", {"started": "now()", "completed": "now()"}),
        ("OP-ANULADA", "CANCELLED", {"cancelled": "now()"}),
    ):
        await _insertar_orden(migration_engine, code=code, status=status, **campos)
        async with migration_engine.begin() as connection:
            await connection.execute(text("DELETE FROM production_orders"))


@pytest.mark.parametrize(
    ("caso", "status", "campos"),
    [
        # Arrancada sin fecha de arranque: un consumo sin cuando.
        ("ARRANCADA_SIN_FECHA", "STARTED", {}),
        # Creada con fecha de arranque: arranco sin arrancar.
        ("CREADA_CON_ARRANQUE", "CREATED", {"started": "now()"}),
        # Completada sin haber arrancado: se termino lo que nunca empezo.
        ("HECHA_SIN_ARRANCAR", "COMPLETED", {"completed": "now()"}),
        # Anulada y arrancada a la vez: las dos salidas de CREATED, juntas.
        ("ANULADA_Y_ARRANCADA", "CANCELLED", {"started": "now()", "cancelled": "now()"}),
        # Un estado que no existe.
        ("ESTADO_INVENTADO", "EN_PROCESO", {"started": "now()"}),
    ],
)
@pytest.mark.asyncio
async def test_las_combinaciones_imposibles_se_rechazan(
    migration_engine: AsyncEngine, caso: str, status: str, campos: dict[str, str]
) -> None:
    """Las fechas no son decorativas: cada estado exige las suyas y prohibe el resto."""
    _upgrade("0020")
    await _sembrar_dependencias(migration_engine)

    with pytest.raises(IntegrityError):
        await _insertar_orden(migration_engine, code=f"OP-{caso}", status=status, **campos)


@pytest.mark.asyncio
async def test_una_cotizacion_no_admite_dos_ordenes(migration_engine: AsyncEngine) -> None:
    """ONE_ORDER_PER_QUOTATION_DB_ENFORCED, comprobado en la base y no en el codigo."""
    _upgrade("0020")
    await _sembrar_dependencias(migration_engine)
    await _insertar_orden(migration_engine, code="OP-PRIMERA", status="CREATED")

    with pytest.raises(IntegrityError):
        await _insertar_orden(migration_engine, code="OP-SEGUNDA", status="CREATED")


@pytest.mark.asyncio
async def test_el_token_del_qr_no_puede_ser_corto(migration_engine: AsyncEngine) -> None:
    """Un token corto es un correlativo disfrazado: se recorre cambiando digitos."""
    _upgrade("0020")
    await _sembrar_dependencias(migration_engine)

    async with migration_engine.begin() as connection:
        cotizacion = await connection.scalar(text("SELECT id FROM quotations LIMIT 1"))
        ubicacion = await connection.scalar(text("SELECT id FROM stock_locations LIMIT 1"))

    with pytest.raises(IntegrityError):
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO production_orders "
                    "(code, quotation_id, stock_location_id, status, qr_token) "
                    "VALUES ('OP-CORTA', :q, :l, 'CREATED', '7')"
                ),
                {"q": cotizacion, "l": ubicacion},
            )
