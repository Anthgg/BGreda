"""Fase 010F — la migracion 0033 se ejecuta de verdad contra PostgreSQL.

Como las de 0015 a 0032: alembic real en un subproceso, sobre una base propia,
la ida y la vuelta.

Leer el fichero no basta. Lo que aqui hay que ver funcionando es:

1. que una cotizacion de 010E llegue al otro lado con todo el bloque economico
   en cero y su quema intacta: se creo antes de que existiera el precio;
2. que los CHECK muerdan: un costo negativo, un suelo por encima del objetivo,
   una linea sin piezas con subtotal;
3. que la GANANCIA si pueda ser negativa —una venta a perdida tiene que poder
   verse— mientras los costos no;
4. que la configuracion comercial de la casa no se duplique;
5. que el downgrade se niegue cuando ya hay un precio comprometido.
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
MIGRATION_DB = "greda_migration_0033"
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


async def _cotizacion_v2(engine: AsyncEngine, codigo: str) -> int:
    async with engine.begin() as connection:
        identificador = await connection.scalar(
            text(
                "INSERT INTO v2_quotations"
                " (code, pricing_engine_version, status, production_type,"
                "  created_at, updated_at)"
                " VALUES (:codigo, 'V2', 'DRAFT', 'RETAIL', now(), now())"
                " RETURNING id"
            ),
            {"codigo": codigo},
        )
    assert identificador is not None
    return int(identificador)


async def _linea(engine: AsyncEngine, cotizacion: int, cantidad: int = 10) -> int:
    async with engine.begin() as connection:
        identificador = await connection.scalar(
            text(
                "INSERT INTO v2_quotation_products"
                " (v2_quotation_id, sort_order, quantity, created_at, updated_at)"
                " VALUES (:cotizacion, 0, :cantidad, now(), now())"
                " RETURNING id"
            ),
            {"cotizacion": cotizacion, "cantidad": cantidad},
        )
    assert identificador is not None
    return int(identificador)


# ---------------------------------------------------------------------------
# 1. Una cotizacion anterior sobrevive
# ---------------------------------------------------------------------------
class TestHistoricos:
    async def test_una_cotizacion_de_010e_nace_sin_precio(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Cero en el bloque economico es la verdad de esa fila.

        Rellenarle un precio por defecto seria inventar que alguien lo acepto.
        """
        _upgrade("0032")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-SIN-PRECIO")
        linea = await _linea(migration_engine, cotizacion)

        _upgrade("0033")

        async with migration_engine.connect() as connection:
            cabecera = (
                await connection.execute(
                    text(
                        "SELECT production_cost_total, real_cost_total, negotiated_price,"
                        "       subtotal_amount, tax_amount, total_amount, estimated_profit"
                        " FROM v2_quotations WHERE id = :id"
                    ),
                    {"id": cotizacion},
                )
            ).one()
            fila = (
                await connection.execute(
                    text(
                        "SELECT direct_cost, allocated_production_cost, unit_price,"
                        "       line_subtotal, line_total"
                        " FROM v2_quotation_products WHERE id = :id"
                    ),
                    {"id": linea},
                )
            ).one()

        assert cabecera.production_cost_total == Decimal(0)
        assert cabecera.real_cost_total == Decimal(0)
        assert cabecera.total_amount == Decimal(0)
        assert cabecera.estimated_profit == Decimal(0)
        assert fila.unit_price == Decimal(0)
        assert fila.line_total == Decimal(0)

    async def test_la_quema_de_010e_llega_intacta(self, migration_engine: AsyncEngine) -> None:
        """0033 no toca ni una columna de la fase anterior."""
        _upgrade("0032")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-QUEMA")
        async with migration_engine.begin() as connection:
            horno = await connection.scalar(
                text(
                    "INSERT INTO kilns (code, name, capacity_volume_cm3, created_at, updated_at)"
                    " VALUES ('KILN-0033', 'Horno', 17000, now(), now()) RETURNING id"
                )
            )
            await connection.execute(
                text(
                    "UPDATE v2_quotations SET kiln_id = :horno, firing_count = 2,"
                    " low_fire_enabled = true, high_fire_enabled = true,"
                    " low_fire_count = 2, high_fire_count = 2,"
                    " firing_commercial_total = 900, firing_gas_total = 210"
                    " WHERE id = :id"
                ),
                {"horno": horno, "id": cotizacion},
            )

        _upgrade("0033")

        async with migration_engine.connect() as connection:
            fila = (
                await connection.execute(
                    text(
                        "SELECT firing_commercial_total, firing_gas_total, firing_difference"
                        " FROM v2_quotations WHERE id = :id"
                    ),
                    {"id": cotizacion},
                )
            ).one()
        assert fila.firing_commercial_total == Decimal(900)
        assert fila.firing_gas_total == Decimal(210)
        assert fila.firing_difference == Decimal(690)


# ---------------------------------------------------------------------------
# 2. Los CHECK muerden
# ---------------------------------------------------------------------------
class TestRestricciones:
    async def test_un_costo_negativo_se_rechaza(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0033")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-NEGATIVO")

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE v2_quotations SET production_cost_total = -1 WHERE id = :id"),
                    {"id": cotizacion},
                )

    async def test_el_suelo_no_puede_superar_al_objetivo(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Si pasara, no existiria ningun factor valido para la cotizacion."""
        _upgrade("0033")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-RANGO")

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE v2_quotations SET price_min = 100, price_target = 50 WHERE id = :id"
                    ),
                    {"id": cotizacion},
                )

    async def test_una_linea_sin_piezas_no_puede_llevar_subtotal(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0033")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-SIN-PIEZAS")
        linea = await _linea(migration_engine, cotizacion, cantidad=0)

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE v2_quotation_products SET line_subtotal = 10 WHERE id = :id"),
                    {"id": linea},
                )

    async def test_un_precio_unitario_negativo_se_rechaza(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0033")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-UNITARIO")
        linea = await _linea(migration_engine, cotizacion)

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE v2_quotation_products SET unit_price = -1 WHERE id = :id"),
                    {"id": linea},
                )

    async def test_la_ganancia_si_puede_ser_negativa(self, migration_engine: AsyncEngine) -> None:
        """Una venta a perdida tiene que poder verse, no esconderse tras un cero."""
        _upgrade("0033")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-PERDIDA")

        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE v2_quotations SET estimated_profit = -500,"
                    " effective_margin_percent = -25, rounding_adjustment = -3"
                    " WHERE id = :id"
                ),
                {"id": cotizacion},
            )

        async with migration_engine.connect() as connection:
            fila = (
                await connection.execute(
                    text(
                        "SELECT estimated_profit, effective_margin_percent, rounding_adjustment"
                        " FROM v2_quotations WHERE id = :id"
                    ),
                    {"id": cotizacion},
                )
            ).one()
        assert fila.estimated_profit == Decimal(-500)
        assert fila.effective_margin_percent == Decimal(-25)
        assert fila.rounding_adjustment == Decimal(-3)


# ---------------------------------------------------------------------------
# 3. Una sola politica fiscal
# ---------------------------------------------------------------------------
async def test_la_configuracion_comercial_de_la_casa_sigue_siendo_la_unica(
    migration_engine: AsyncEngine,
) -> None:
    """El IGV se lee de `commercial_settings`; 0033 no crea un segundo sitio."""
    _upgrade("0033")
    async with migration_engine.connect() as connection:
        columnas = set(
            (
                await connection.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = 'v2_commercial_settings'"
                    )
                )
            ).all()
        )
    assert "tax_percent" not in columnas
    assert "rounding_step" not in columnas


# ---------------------------------------------------------------------------
# 4. La vuelta
# ---------------------------------------------------------------------------
class TestDowngrade:
    async def test_la_vuelta_limpia_funciona(self, migration_engine: AsyncEngine) -> None:
        """Sin precio registrado, 0033 se puede revertir y reaplicar."""
        _upgrade("0033")

        vuelta = _alembic("downgrade", "0032")
        assert vuelta.returncode == 0, f"{vuelta.stdout}\n{vuelta.stderr}"

        async with migration_engine.connect() as connection:
            columnas = set(
                (
                    await connection.scalars(
                        text(
                            "SELECT column_name FROM information_schema.columns"
                            " WHERE table_name = 'v2_quotations'"
                        )
                    )
                ).all()
            )
        assert "production_cost_total" not in columnas
        assert "subtotal_amount" not in columnas
        # Y lo de 0032 sigue en pie: la vuelta es de UNA revision.
        assert "firing_commercial_total" in columnas

        _upgrade("0033")

    async def test_la_vuelta_se_niega_si_hay_una_cotizacion_valorizada(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0033")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-VALORIZADA")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text("UPDATE v2_quotations SET production_cost_total = 2558.43 WHERE id = :id"),
                {"id": cotizacion},
            )

        vuelta = _alembic("downgrade", "0032")

        assert vuelta.returncode != 0
        assert "no puede revertirse" in vuelta.stderr

    async def test_la_vuelta_se_niega_si_solo_hay_un_unitario(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Una linea puede tener precio unitario sin que la cabecera lo tenga.

        Pasa con una linea sin piezas. Mirar solo la cabecera dejaria borrar un
        precio ya comunicado sin avisar.
        """
        _upgrade("0033")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-UNITARIO-SOLO")
        linea = await _linea(migration_engine, cotizacion, cantidad=0)
        async with migration_engine.begin() as connection:
            await connection.execute(
                text("UPDATE v2_quotation_products SET unit_price = 182 WHERE id = :id"),
                {"id": linea},
            )

        vuelta = _alembic("downgrade", "0032")

        assert vuelta.returncode != 0
        assert "no puede revertirse" in vuelta.stderr


# ---------------------------------------------------------------------------
# 5. La cabeza
# ---------------------------------------------------------------------------
async def test_la_cabeza_es_0033(migration_engine: AsyncEngine) -> None:
    _upgrade("head")
    async with migration_engine.connect() as connection:
        cabezas = list(
            (await connection.scalars(text("SELECT version_num FROM alembic_version"))).all()
        )
    assert cabezas == ["0033"]
