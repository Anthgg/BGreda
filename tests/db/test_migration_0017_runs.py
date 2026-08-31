"""Fase 009F — la migracion 0017 se ejecuta de verdad contra PostgreSQL.

Como en 0015 y 0016: alembic real en un subproceso contra una base propia, la
ida y la vuelta, y el esquema comprobado con SELECT e INSERT reales.

Lo que de verdad importa aqui no son las dos columnas nuevas sino el CHECK de
coherencia. Una tasa de cambio guardada donde no toca no rompe nada el dia que
se escribe: rompe meses despues, cuando alguien reabre una cotizacion en soles
y encuentra una conversion que nunca ocurrio. Por eso los diez casos se prueban
con INSERT contra la base, no con Pydantic.
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
#: Base propia, distinta de las de 0015 y 0016, para que puedan correr a la vez.
MIGRATION_DB = "greda_migration_0017"
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
    # S603: comando fijo (el propio interprete y alembic) y argumentos que son
    # revisiones literales de esta prueba. No hay entrada de usuario.
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


async def _scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as connection:
        return await connection.scalar(text(sql), params)


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


#: Lo minimo que exige `quotations` para aceptar una fila. Se enumera porque
#: `INSERT ... DEFAULT VALUES` no serviria: hay columnas NOT NULL sin default.
NUEVA_COTIZACION = (
    "INSERT INTO quotations (code, status, workflow, commercial_factor, "
    "commercial_factor_default_snapshot, "
    "currency_code_snapshot, exchange_rate_snapshot, exchange_rate_source_snapshot) "
    "VALUES (:code, 'DRAFT', 'COTIZADOR', 1, 1, :currency, :rate, :source)"
)


async def _insertar(
    engine: AsyncEngine,
    *,
    code: str,
    currency: str,
    rate: str | None,
    source: str | None,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(NUEVA_COTIZACION),
            {"code": code, "currency": currency, "rate": rate, "source": source},
        )


@pytest.mark.asyncio
async def test_0016_a_0017_a_0016_y_de_vuelta(migration_engine: AsyncEngine) -> None:
    """La ida, la vuelta y la ida otra vez. Sin residuos entre medias."""
    _upgrade("0016")
    assert await _current(migration_engine) == "0016"
    assert not await _column_exists(migration_engine, "quotations", "exchange_rate_snapshot")

    # ---- 0016 -> 0017 -------------------------------------------------
    _upgrade("0017")
    assert await _current(migration_engine) == "0017"
    assert await _column_exists(migration_engine, "quotations", "exchange_rate_snapshot")
    assert await _column_exists(migration_engine, "quotations", "exchange_rate_source_snapshot")

    # Las columnas que 0017 NO debe tocar siguen ahi y siguen siendo NOT NULL.
    for columna in ("currency_code_snapshot", "currency_symbol_snapshot"):
        nullable = await _scalar(
            migration_engine,
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'quotations' AND column_name = :column",
            column=columna,
        )
        assert nullable == "NO", f"0017 no debe tocar {columna}"

    # La escala importa: una tasa truncada a dos decimales convierte mal.
    escala = await _scalar(
        migration_engine,
        "SELECT numeric_scale FROM information_schema.columns "
        "WHERE table_name = 'quotations' AND column_name = 'exchange_rate_snapshot'",
    )
    assert escala == 6

    # ---- 0017 -> 0016 -> 0017 -----------------------------------------
    _downgrade("0016")
    assert await _current(migration_engine) == "0016"
    assert not await _column_exists(migration_engine, "quotations", "exchange_rate_snapshot")
    assert not await _column_exists(migration_engine, "quotations", "exchange_rate_source_snapshot")
    # Y la moneda, que es de 0015, sigue en pie despues del downgrade.
    assert await _column_exists(migration_engine, "quotations", "currency_code_snapshot")

    _upgrade("0017")
    assert await _current(migration_engine) == "0017"


@pytest.mark.asyncio
async def test_los_dos_estados_validos_se_aceptan(migration_engine: AsyncEngine) -> None:
    """PEN sin tasa y USD con tasa manual: los unicos dos con sentido."""
    _upgrade("0017")

    await _insertar(migration_engine, code="CTZ-PEN-1", currency="PEN", rate=None, source=None)
    await _insertar(
        migration_engine, code="CTZ-USD-1", currency="USD", rate="3.75", source="MANUAL"
    )

    filas = await _scalar(migration_engine, "SELECT count(*) FROM quotations")
    assert filas == 2

    # La tasa vuelve con su escala intacta, no redondeada a dos decimales.
    guardada = await _scalar(
        migration_engine,
        "SELECT exchange_rate_snapshot FROM quotations WHERE code = 'CTZ-USD-1'",
    )
    assert str(guardada) == "3.750000"


@pytest.mark.parametrize(
    ("caso", "currency", "rate", "source"),
    [
        # PEN no admite conversion: guardar una tasa ahi describe algo que no
        # paso, y el dia que alguien la lea creera que hubo cambio de moneda.
        ("PEN_CON_TASA", "PEN", "1", None),
        ("PEN_CON_TASA_UNO_Y_FUENTE", "PEN", "1", "MANUAL"),
        ("PEN_CON_FUENTE", "PEN", None, "MANUAL"),
        # USD sin tasa no se puede convertir; aceptarlo dejaria una cotizacion
        # en dolares cuyo precio nadie puede reproducir.
        ("USD_SIN_TASA", "USD", None, "MANUAL"),
        ("USD_TASA_CERO", "USD", "0", "MANUAL"),
        ("USD_TASA_NEGATIVA", "USD", "-3.75", "MANUAL"),
        ("USD_SIN_FUENTE", "USD", "3.75", None),
        # En 009F la unica fuente es MANUAL. Un 'API' guardado hoy seria una
        # procedencia inventada para un valor que tecleo una persona.
        ("USD_FUENTE_NO_AUTORIZADA", "USD", "3.75", "API"),
        # 009F autoriza dos monedas. Sin el CHECK explicito, el de coherencia
        # dejaria pasar cualquier moneda que trajera tasa.
        ("EUR_CON_TASA", "EUR", "4.10", "MANUAL"),
        ("EUR_SIN_TASA", "EUR", None, None),
    ],
)
@pytest.mark.asyncio
async def test_la_base_rechaza_los_estados_incoherentes(
    migration_engine: AsyncEngine,
    caso: str,
    currency: str,
    rate: str | None,
    source: str | None,
) -> None:
    """El contrato vive en el esquema, no solo en Pydantic.

    Es el unico sitio por el que no se puede colar una fila incoherente: ni por
    un script de mantenimiento, ni por una importacion, ni por un endpoint que
    alguien anada mañana sin acordarse de validar.
    """
    _upgrade("0017")

    with pytest.raises(Exception) as fallo:
        await _insertar(
            migration_engine, code=f"CTZ-{caso}", currency=currency, rate=rate, source=source
        )
    mensaje = str(fallo.value)
    assert "currency_supported" in mensaje or "exchange_rate" in mensaje, mensaje

    # Y no quedo nada a medias.
    assert await _scalar(migration_engine, "SELECT count(*) FROM quotations") == 0


@pytest.mark.asyncio
async def test_las_cotizaciones_historicas_siguen_siendo_validas(
    migration_engine: AsyncEngine,
) -> None:
    """Una fila PEN escrita ANTES de 0017 tiene que sobrevivir la migracion.

    Es el caso real de produccion: 346 cotizaciones en soles, ninguna con tasa.
    Si el CHECK de coherencia las rechazara, la migracion fallaria al crearlo
    en vez de dejar datos que ya no cumplen su propio contrato.
    """
    _upgrade("0016")
    async with migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO quotations (code, status, workflow, commercial_factor, "
                "commercial_factor_default_snapshot, currency_code_snapshot) "
                "VALUES ('CTZ-VIEJA', 'CONFIRMED', 'LEGACY', 1, 1, 'PEN')"
            )
        )

    _upgrade("0017")

    fila = await _scalar(
        migration_engine,
        "SELECT exchange_rate_snapshot FROM quotations WHERE code = 'CTZ-VIEJA'",
    )
    assert fila is None
    fuente = await _scalar(
        migration_engine,
        "SELECT exchange_rate_source_snapshot FROM quotations WHERE code = 'CTZ-VIEJA'",
    )
    assert fuente is None
    # Y sigue siendo PEN: nunca se reinterpreta un historico como dolares.
    assert (
        await _scalar(
            migration_engine,
            "SELECT currency_code_snapshot FROM quotations WHERE code = 'CTZ-VIEJA'",
        )
        == "PEN"
    )


@pytest.mark.asyncio
async def test_0017_no_deja_una_sola_cabeza_rota(migration_engine: AsyncEngine) -> None:
    """`alembic upgrade head` tiene que llegar sola a 0017."""
    _upgrade("head")
    assert await _current(migration_engine) == "0017"
    heads = _alembic("heads")
    assert heads.returncode == 0, heads.stderr
    assert heads.stdout.count("(head)") == 1, heads.stdout
    assert "0017" in heads.stdout
