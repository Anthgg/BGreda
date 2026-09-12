"""Fase 010E — la migracion 0032 se ejecuta de verdad contra PostgreSQL.

Como las de 0015 a 0031: alembic real en un subproceso, sobre una base propia,
la ida y la vuelta.

Leer el fichero no basta. Lo que aqui hay que ver funcionando es:

1. que el factor por ocupacion de Legacy llegue al otro lado **intacto**, con
   su curva entera. Es la mitad de la formula con la que cobra el Cotizador
   historico, y 010E lo que hace es dejar de usarlo, no borrarlo;
2. que los CHECK muerdan: una quema alta apagada con hornadas, unas hornadas
   sin horno, una medida en cero. Ninguno deberia poder escribirse;
3. que la diferencia de quema la calcule la BASE y no se pueda escribir;
4. que una cotizacion de 010D sobreviva con sus columnas nuevas en cero y su
   horno en NULL, que es la verdad: se creo antes de que existiera la quema;
5. que el downgrade se niegue cuando ya hay un horno elegido o piezas medidas.
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
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.session import normalize_database_url

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
MIGRATION_DB = "greda_migration_0032"
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


async def _horno(engine: AsyncEngine, code: str, capacidad: str = "17000") -> int:
    async with engine.begin() as connection:
        identificador = await connection.scalar(
            text(
                "INSERT INTO kilns (code, name, capacity_volume_cm3, created_at, updated_at)"
                " VALUES (:code, :code, :capacidad, now(), now())"
                " RETURNING id"
            ),
            {"code": code, "capacidad": Decimal(capacidad)},
        )
    assert identificador is not None
    return int(identificador)


async def _linea(engine: AsyncEngine, cotizacion: int) -> int:
    async with engine.begin() as connection:
        identificador = await connection.scalar(
            text(
                "INSERT INTO v2_quotation_products"
                " (v2_quotation_id, sort_order, quantity, created_at, updated_at)"
                " VALUES (:cotizacion, 0, 10, now(), now())"
                " RETURNING id"
            ),
            {"cotizacion": cotizacion},
        )
    assert identificador is not None
    return int(identificador)


# ---------------------------------------------------------------------------
# 1. El motor de quema de Legacy no se toca
# ---------------------------------------------------------------------------
class TestNoContamina:
    async def test_el_factor_por_ocupacion_de_legacy_llega_intacto(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Es la mitad de la formula con la que cobra el Cotizador historico.

        010E deja de usarlo en V2. Borrarlo dejaria los precios historicos sin
        poder explicarse y la hoja de quema real sin factor que resolver.
        """
        _upgrade("0031")
        horno = await _horno(migration_engine, "KILN-LEGACY")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO kiln_occupancy_factors"
                    " (kiln_id, min_percentage, max_percentage, factor, created_at, updated_at)"
                    " VALUES (:horno, 1, 10, 3, now(), now())"
                ),
                {"horno": horno},
            )

        _upgrade("0032")

        async with migration_engine.connect() as connection:
            fila = (
                await connection.execute(
                    text(
                        "SELECT min_percentage, max_percentage, factor"
                        " FROM kiln_occupancy_factors WHERE kiln_id = :horno"
                    ),
                    {"horno": horno},
                )
            ).one()
        assert fila.factor == Decimal(3)
        assert (fila.min_percentage, fila.max_percentage) == (1, 10)

    async def test_las_tarifas_de_horno_de_legacy_llegan_intactas(
        self, migration_engine: AsyncEngine
    ) -> None:
        """`kiln_rates` es de Legacy; V2 tiene su propia `v2_kiln_rates`."""
        _upgrade("0031")
        horno = await _horno(migration_engine, "KILN-RATES")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO kiln_rates"
                    " (kiln_id, firing_type, rate, valid_from, created_at, updated_at)"
                    " VALUES (:horno, 'LOW', 195, current_date, now(), now())"
                ),
                {"horno": horno},
            )

        _upgrade("0032")

        async with migration_engine.connect() as connection:
            tarifa = await connection.scalar(
                text("SELECT rate FROM kiln_rates WHERE kiln_id = :horno"), {"horno": horno}
            )
        assert tarifa == Decimal(195)


# ---------------------------------------------------------------------------
# 2. Una cotizacion anterior sobrevive
# ---------------------------------------------------------------------------
class TestHistoricos:
    async def test_una_cotizacion_de_010d_nace_sin_horno_y_sin_quema(
        self, migration_engine: AsyncEngine
    ) -> None:
        """NULL en el horno y cero en los importes es la verdad de esa fila.

        Rellenarle un horno por defecto seria inventar que alguien lo eligio.
        """
        _upgrade("0031")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-ANTERIOR")
        linea = await _linea(migration_engine, cotizacion)

        _upgrade("0032")

        async with migration_engine.connect() as connection:
            cabecera = (
                await connection.execute(
                    text(
                        "SELECT kiln_id, kiln_capacity_snapshot, firing_count,"
                        "       firing_gas_total, firing_commercial_total, firing_difference"
                        " FROM v2_quotations WHERE id = :id"
                    ),
                    {"id": cotizacion},
                )
            ).one()
            fila = (
                await connection.execute(
                    text(
                        "SELECT length_cm, width_cm, height_cm, total_volume_cm3,"
                        "       firing_commercial_cost"
                        " FROM v2_quotation_products WHERE id = :id"
                    ),
                    {"id": linea},
                )
            ).one()

        assert cabecera.kiln_id is None
        assert cabecera.kiln_capacity_snapshot is None
        assert cabecera.firing_count == 0
        assert cabecera.firing_gas_total == Decimal(0)
        assert cabecera.firing_difference == Decimal(0)
        assert (fila.length_cm, fila.width_cm, fila.height_cm) == (None, None, None)
        assert fila.total_volume_cm3 == Decimal(0)
        assert fila.firing_commercial_cost == Decimal(0)


# ---------------------------------------------------------------------------
# 3. Los CHECK muerden
# ---------------------------------------------------------------------------
class TestRestricciones:
    async def test_una_quema_apagada_no_puede_tener_hornadas(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0032")
        horno = await _horno(migration_engine, "KILN-APAGADA")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-APAGADA")

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE v2_quotations SET kiln_id = :horno, firing_count = 2,"
                        " high_fire_enabled = false, high_fire_count = 2 WHERE id = :id"
                    ),
                    {"horno": horno, "id": cotizacion},
                )

    async def test_sin_horno_no_puede_haber_importe(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0032")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-SIN-HORNO")

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE v2_quotations SET firing_commercial_total = 900 WHERE id = :id"),
                    {"id": cotizacion},
                )

    async def test_no_se_pueden_pedir_mas_hornadas_de_baja_que_de_carga(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Baja y alta van sobre la MISMA carga: no hay encendidos de mas."""
        _upgrade("0032")
        horno = await _horno(migration_engine, "KILN-EXCESO")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-EXCESO")

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE v2_quotations SET kiln_id = :horno, firing_count = 1,"
                        " low_fire_count = 3 WHERE id = :id"
                    ),
                    {"horno": horno, "id": cotizacion},
                )

    async def test_una_medida_en_cero_se_rechaza(self, migration_engine: AsyncEngine) -> None:
        """NULL es «sin medir»; cero seria una pieza plana que no existe."""
        _upgrade("0032")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-MEDIDA")
        linea = await _linea(migration_engine, cotizacion)

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE v2_quotation_products SET height_cm = 0 WHERE id = :id"),
                    {"id": linea},
                )

    async def test_una_capacidad_congelada_en_cero_se_rechaza(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0032")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-CAPACIDAD")

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE v2_quotations SET kiln_capacity_snapshot = 0 WHERE id = :id"),
                    {"id": cotizacion},
                )

    async def test_sin_ningun_encendido_no_puede_haber_importe(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Apagar las DOS quemas deja los dos conteos en cero.

        Los CHECK de baja y de alta miran cada uno lo suyo, y por separado
        dejan pasar unos totales que sobrevivieran a apagar ambas.
        """
        _upgrade("0032")
        horno = await _horno(migration_engine, "KILN-NADA")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-NADA")

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE v2_quotations SET kiln_id = :horno, firing_count = 2,"
                        " low_fire_count = 0, high_fire_count = 0,"
                        " firing_commercial_total = 900 WHERE id = :id"
                    ),
                    {"horno": horno, "id": cotizacion},
                )

    async def test_sin_horno_tampoco_puede_haber_ocupacion(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0032")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-OCUPACION")

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE v2_quotations SET firing_occupancy_percent = 50 WHERE id = :id"),
                    {"id": cotizacion},
                )

    async def test_la_clave_foranea_del_horno_tiene_indice(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Sin el, dar de baja un horno recorre y bloquea las cotizaciones."""
        _upgrade("0032")
        async with migration_engine.connect() as connection:
            indices = set(
                (
                    await connection.scalars(
                        text("SELECT indexname FROM pg_indexes WHERE tablename = 'v2_quotations'")
                    )
                ).all()
            )
        assert "ix_v2_quotations_kiln_id" in indices

    async def test_la_participacion_de_una_linea_no_pasa_de_cien(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0032")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-SHARE")
        linea = await _linea(migration_engine, cotizacion)

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE v2_quotation_products"
                        " SET firing_volume_share_percent = 101 WHERE id = :id"
                    ),
                    {"id": linea},
                )


# ---------------------------------------------------------------------------
# 4. La diferencia la calcula la base
# ---------------------------------------------------------------------------
class TestDiferenciaGenerada:
    async def test_la_base_calcula_la_diferencia(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0032")
        horno = await _horno(migration_engine, "KILN-DIF")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-DIF")

        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE v2_quotations SET kiln_id = :horno, firing_count = 2,"
                    # Encendidas explicitamente: una fila insertada a pelo las
                    # tiene en NULL —nacio antes de 010B— y el CHECK de «quema
                    # apagada no cuesta nada» rechazaria las hornadas.
                    " low_fire_enabled = true, high_fire_enabled = true,"
                    " low_fire_count = 2, high_fire_count = 2,"
                    " firing_commercial_total = 900, firing_gas_total = 210"
                    " WHERE id = :id"
                ),
                {"horno": horno, "id": cotizacion},
            )

        async with migration_engine.connect() as connection:
            diferencia = await connection.scalar(
                text("SELECT firing_difference FROM v2_quotations WHERE id = :id"),
                {"id": cotizacion},
            )
        assert diferencia == Decimal(690)

    async def test_la_diferencia_no_se_puede_escribir_a_mano(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Una columna generada no puede contradecir a sus dos sumandos."""
        _upgrade("0032")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-DIF-MANO")

        with pytest.raises(DBAPIError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE v2_quotations SET firing_difference = 1 WHERE id = :id"),
                    {"id": cotizacion},
                )


# ---------------------------------------------------------------------------
# 5. La vuelta
# ---------------------------------------------------------------------------
class TestDowngrade:
    async def test_la_vuelta_limpia_funciona(self, migration_engine: AsyncEngine) -> None:
        """Sin quema registrada, 0032 se puede revertir y reaplicar."""
        _upgrade("0032")

        vuelta = _alembic("downgrade", "0031")
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
        assert "kiln_id" not in columnas
        assert "firing_difference" not in columnas
        # Y lo de 0031 sigue en pie: la vuelta es de UNA revision.
        assert "illustration_enabled" in columnas

        _upgrade("0032")

    async def test_la_vuelta_se_niega_si_hay_horno_elegido(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0032")
        horno = await _horno(migration_engine, "KILN-VUELTA")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-VUELTA")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text("UPDATE v2_quotations SET kiln_id = :horno WHERE id = :id"),
                {"horno": horno, "id": cotizacion},
            )

        vuelta = _alembic("downgrade", "0031")

        assert vuelta.returncode != 0
        assert "no puede revertirse" in vuelta.stderr

    async def test_la_vuelta_se_niega_si_solo_hay_medidas(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Sin horno todavia, pero con las piezas medidas a mano.

        Esas medidas no se derivan de nada que quede en la base: revertir sin
        mirarlas las borraria sin forma de recuperarlas.
        """
        _upgrade("0032")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-MEDIDAS")
        linea = await _linea(migration_engine, cotizacion)
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE v2_quotation_products"
                    " SET length_cm = 18, width_cm = 12, height_cm = 3 WHERE id = :id"
                ),
                {"id": linea},
            )

        vuelta = _alembic("downgrade", "0031")

        assert vuelta.returncode != 0
        assert "no puede revertirse" in vuelta.stderr

    async def test_la_vuelta_deja_intacto_el_factor_de_legacy(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0032")
        horno = await _horno(migration_engine, "KILN-VUELTA-LEGACY")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO kiln_occupancy_factors"
                    " (kiln_id, min_percentage, max_percentage, factor, created_at, updated_at)"
                    " VALUES (:horno, 91, 100, 1, now(), now())"
                ),
                {"horno": horno},
            )

        vuelta = _alembic("downgrade", "0031")
        assert vuelta.returncode == 0, f"{vuelta.stdout}\n{vuelta.stderr}"

        async with migration_engine.connect() as connection:
            cuantos = await connection.scalar(
                text("SELECT count(*) FROM kiln_occupancy_factors WHERE kiln_id = :horno"),
                {"horno": horno},
            )
        assert cuantos == 1

        _upgrade("0032")


# ---------------------------------------------------------------------------
# 6. La cabeza
# ---------------------------------------------------------------------------
async def test_la_cabeza_es_0032(migration_engine: AsyncEngine) -> None:
    _upgrade("head")
    async with migration_engine.connect() as connection:
        cabezas = list(
            (await connection.scalars(text("SELECT version_num FROM alembic_version"))).all()
        )
    assert cabezas == ["0032"]
