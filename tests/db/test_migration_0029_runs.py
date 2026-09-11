"""Fase 010B — la migracion 0029 se ejecuta de verdad contra PostgreSQL.

Como las de 0015 a 0028: alembic real en un subproceso, sobre una base propia,
la ida y la vuelta.

Lo que aqui importa:

1. que la configuracion de la empresa —el IGV, la moneda, el redondeo— llegue
   al otro lado **sin un solo cambio**. Es la fuente canonica y 0029 no la
   toca;
2. que las tarifas de horno de Legacy tampoco cambien, y que las de V2 nazcan
   en su propia tabla;
3. que una cotizacion V2 de 010A sobreviva con sus snapshots en NULL, que es
   la verdad: nacio antes de que la configuracion existiera;
4. que el downgrade se niegue cuando ya hay cotizaciones con su copia
   congelada.
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
MIGRATION_DB = "greda_migration_0029"
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


async def _tabla(engine: AsyncEngine, nombre: str) -> str | None:
    async with engine.connect() as connection:
        return await connection.scalar(text("SELECT to_regclass(:nombre)"), {"nombre": nombre})


async def _cotizacion_v2(engine: AsyncEngine, codigo: str) -> None:
    """Una cotizacion V2 como las que dejo 010A: sin snapshot."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v2_quotations"
                " (code, pricing_engine_version, status, production_type,"
                "  created_at, updated_at)"
                " VALUES (:codigo, 'V2', 'DRAFT', 'RETAIL', now(), now())"
            ),
            {"codigo": codigo},
        )


# ---------------------------------------------------------------------------
# 1. La configuracion canonica no se toca
# ---------------------------------------------------------------------------
class TestPoliticaCanonica:
    async def test_la_configuracion_de_la_empresa_sobrevive_intacta(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0028")
        # La fila unica ya existe: la siembra el esquema base. Se EDITA, que es
        # ademas lo que hace una instalacion real antes de llegar a 0029.
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE commercial_settings"
                    "   SET tax_percent = 18, currency_code = 'PEN', rounding_step = 0.50"
                    " WHERE id = 1"
                )
            )

        _upgrade("0029")

        async with migration_engine.connect() as connection:
            fila = (
                await connection.execute(
                    text(
                        "SELECT tax_percent, currency_code, rounding_step, version"
                        "  FROM commercial_settings WHERE id = 1"
                    )
                )
            ).one()
        assert fila.tax_percent == 18
        assert fila.currency_code == "PEN"
        assert fila.rounding_step == Decimal("0.50")
        assert fila.version == 1

    async def test_la_configuracion_v2_no_tiene_columna_de_igv(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Un segundo IGV seria una segunda verdad."""
        _upgrade("0029")
        async with migration_engine.connect() as connection:
            columnas = set(
                await connection.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = 'v2_commercial_settings'"
                    )
                )
            )
        assert not columnas & {"tax_percent", "currency_code", "currency_symbol", "rounding_step"}

    async def test_la_fila_unica_nace_con_los_valores_aprobados(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0029")
        async with migration_engine.connect() as connection:
            fila = (
                await connection.execute(
                    text(
                        "SELECT workday_hours, space_service_cost_per_day,"
                        "       administrative_cost_per_quote, commercial_factor_default,"
                        "       commercial_factor_min, quotation_validity_days,"
                        "       illustration_daily_rate, illustration_pieces_per_workday"
                        "  FROM v2_commercial_settings WHERE id = 1"
                    )
                )
            ).one()
        assert fila.workday_hours == 8
        assert fila.space_service_cost_per_day == 140
        assert fila.administrative_cost_per_quote == 200
        assert fila.commercial_factor_default == 3
        assert fila.commercial_factor_min == 2
        assert fila.quotation_validity_days == 20
        assert fila.illustration_daily_rate == 110
        assert fila.illustration_pieces_per_workday == 50

    async def test_sembrar_la_fila_unica_es_idempotente(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0029")
        _upgrade("head")
        async with migration_engine.connect() as connection:
            total = await connection.scalar(text("SELECT count(*) FROM v2_commercial_settings"))
        assert total == 1


# ---------------------------------------------------------------------------
# 2. Las tarifas no se cruzan
# ---------------------------------------------------------------------------
class TestTarifas:
    async def test_las_tarifas_de_horno_de_legacy_no_se_tocan(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0028")
        async with migration_engine.begin() as connection:
            kiln_id = await connection.scalar(
                text(
                    "INSERT INTO kilns (code, name, capacity_volume_cm3, firing_days_per_batch,"
                    "  active, created_at, updated_at)"
                    " VALUES ('KILN-0029A', 'Chico', 17000, 3, true, now(), now())"
                    " RETURNING id"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO kiln_rates (kiln_id, firing_type, rate, valid_from,"
                    "  created_at, updated_at)"
                    " VALUES (:kiln, 'LOW', 120, CURRENT_DATE, now(), now())"
                ),
                {"kiln": kiln_id},
            )

        _upgrade("0029")

        # Se pregunta por LA tarifa sembrada, no por «la unica»: el esquema
        # base puede traer hornos y tarifas propios, y asumir que no los trae
        # convertiria esta prueba en una sobre el esquema, no sobre 0029.
        async with migration_engine.connect() as connection:
            fila = (
                await connection.execute(
                    text(
                        "SELECT firing_type, rate FROM kiln_rates"
                        " WHERE kiln_id = :kiln AND firing_type = 'LOW'"
                    ),
                    {"kiln": kiln_id},
                )
            ).one()
            nuevas = await connection.scalar(text("SELECT count(*) FROM v2_kiln_rates"))
        assert fila.rate == 120, "0029 cambio una tarifa de Legacy"
        assert nuevas == 0, "las tarifas V2 no se inventan: las pone una persona"

    async def test_v2_no_admite_dos_filas_para_el_mismo_horno_y_tipo(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Dos tarifas abiertas serian dos precios para la misma quema."""
        _upgrade("0029")
        async with migration_engine.begin() as connection:
            kiln_id = await connection.scalar(
                text(
                    "INSERT INTO kilns (code, name, capacity_volume_cm3, firing_days_per_batch,"
                    "  active, created_at, updated_at)"
                    " VALUES ('KILN-0029B', 'Grande', 200000, 4, true, now(), now())"
                    " RETURNING id"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO v2_kiln_rates (kiln_id, firing_type, created_at, updated_at)"
                    " VALUES (:kiln, 'LOW', now(), now())"
                ),
                {"kiln": kiln_id},
            )

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO v2_kiln_rates (kiln_id, firing_type, created_at, updated_at)"
                        " VALUES (:kiln, 'LOW', now(), now())"
                    ),
                    {"kiln": kiln_id},
                )


# ---------------------------------------------------------------------------
# 3. Las cotizaciones de 010A sobreviven
# ---------------------------------------------------------------------------
class TestCotizacionesPrevias:
    async def test_una_cotizacion_de_010a_sobrevive_sin_snapshot(
        self, migration_engine: AsyncEngine
    ) -> None:
        """NULL ahi es la verdad: nacio antes de que existiera la configuracion."""
        _upgrade("0028")
        await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000001")

        _upgrade("0029")

        async with migration_engine.connect() as connection:
            fila = (
                await connection.execute(
                    text(
                        "SELECT code, status, production_type, settings_captured_at,"
                        "       space_service_cost_per_day_snapshot"
                        "  FROM v2_quotations WHERE code = 'CTZ-V2-2026-000001'"
                    )
                )
            ).one()
        assert fila.status == "DRAFT"
        assert fila.production_type == "RETAIL"
        assert fila.settings_captured_at is None
        assert fila.space_service_cost_per_day_snapshot is None

    async def test_el_suelo_del_factor_muerde_en_la_base(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0029")
        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO v2_quotations"
                        " (code, pricing_engine_version, status, production_type,"
                        "  commercial_factor, created_at, updated_at)"
                        " VALUES ('X-1', 'V2', 'DRAFT', 'RETAIL', 1.5, now(), now())"
                    )
                )

    async def test_en_moneda_base_no_cabe_un_tipo_de_cambio(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Un 1 ahi seria una cifra inventada que alguien acabaria multiplicando."""
        _upgrade("0029")
        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO v2_quotations"
                        " (code, pricing_engine_version, status, production_type,"
                        "  currency_code_snapshot, exchange_rate_snapshot,"
                        "  created_at, updated_at)"
                        " VALUES ('X-2', 'V2', 'DRAFT', 'RETAIL', 'PEN', 1, now(), now())"
                    )
                )


# ---------------------------------------------------------------------------
# 4. La vuelta
# ---------------------------------------------------------------------------
class TestDowngrade:
    async def test_revertir_sin_snapshots_deja_el_esquema_anterior(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0029")
        await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000002")

        resultado = _alembic("downgrade", "0028")
        assert resultado.returncode == 0, f"{resultado.stdout}\n{resultado.stderr}"

        assert await _tabla(migration_engine, "v2_commercial_settings") is None
        assert await _tabla(migration_engine, "v2_kiln_rates") is None
        # Y la cotizacion sigue ahi.
        async with migration_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM v2_quotations")) == 1

    async def test_revertir_se_niega_si_alguna_cotizacion_ya_congelo(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Sin sus numeros, una cotizacion emitida deja de poder explicarse."""
        _upgrade("0029")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO v2_quotations"
                    " (code, pricing_engine_version, status, production_type,"
                    "  space_service_cost_per_day_snapshot, settings_captured_at,"
                    "  created_at, updated_at)"
                    " VALUES ('CTZ-V2-2026-000003', 'V2', 'CONFIRMED', 'RETAIL',"
                    "  140, now(), now(), now())"
                )
            )

        resultado = _alembic("downgrade", "0028")

        assert resultado.returncode != 0
        assert (
            "no puede revertirse" in resultado.stderr or "no puede revertirse" in resultado.stdout
        )
        assert await _tabla(migration_engine, "v2_commercial_settings") is not None
