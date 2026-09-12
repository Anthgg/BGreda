"""Fase 010D — la migracion 0031 se ejecuta de verdad contra PostgreSQL.

Como las de 0015 a 0030: alembic real en un subproceso, sobre una base propia,
la ida y la vuelta.

Leer el fichero no basta. Lo que aqui hay que ver funcionando es:

1. que el catalogo de tecnicas de Legacy llegue al otro lado **sin un solo
   cambio**, con sus precios intactos. Es lo que cobra el Cotizador historico;
2. que los CHECK muerdan: rendimiento cero, jornada de veinticinco horas,
   tarifa negativa. Ninguno de los tres deberia poder escribirse;
3. que la ilustracion apagada no pueda costar nada;
4. que una cotizacion de 010C sobreviva con sus columnas nuevas en NULL;
5. que el downgrade se niegue cuando ya hay trabajo registrado.
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
MIGRATION_DB = "greda_migration_0031"
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


async def _trabajador(engine: AsyncEngine, nombre: str, *, jornal: str = "120") -> int:
    async with engine.begin() as connection:
        identificador = await connection.scalar(
            text(
                "INSERT INTO v2_workers"
                " (name, worker_type, daily_rate, created_at, updated_at)"
                " VALUES (:nombre, 'INTERNAL', :jornal, now(), now())"
                " RETURNING id"
            ),
            {"nombre": nombre, "jornal": Decimal(jornal)},
        )
    assert identificador is not None
    return int(identificador)


async def _tecnica(engine: AsyncEngine, code: str, *, capacidad: str = "50") -> int:
    async with engine.begin() as connection:
        identificador = await connection.scalar(
            text(
                "INSERT INTO v2_techniques"
                " (code, name, default_capacity_per_workday, unit, created_at, updated_at)"
                " VALUES (:code, :code, :capacidad, 'piezas', now(), now())"
                " RETURNING id"
            ),
            {"code": code, "capacidad": Decimal(capacidad)},
        )
    assert identificador is not None
    return int(identificador)


async def _tarea(
    engine: AsyncEngine, cotizacion: int, worker: int, tecnica: int, *, horas: str = "8"
) -> int:
    async with engine.begin() as connection:
        identificador = await connection.scalar(
            text(
                "INSERT INTO v2_quotation_labor"
                " (v2_quotation_id, worker_id, worker_name_snapshot, worker_type_snapshot,"
                "  daily_rate_snapshot, workday_hours_snapshot, hourly_rate_snapshot,"
                "  technique_id, technique_name_snapshot, technique_unit_snapshot,"
                "  standard_capacity_snapshot, quantity, calculated_hours, final_hours,"
                "  labor_cost, created_at, updated_at)"
                " VALUES (:cotizacion, :worker, 'Quien sea', 'INTERNAL',"
                "  120, 8, 15, :tecnica, 'Lo que sea', 'piezas', 50, 50, :horas, :horas,"
                "  120, now(), now())"
                " RETURNING id"
            ),
            {
                "cotizacion": cotizacion,
                "worker": worker,
                "tecnica": tecnica,
                "horas": Decimal(horas),
            },
        )
    assert identificador is not None
    return int(identificador)


# ---------------------------------------------------------------------------
# 1. El catalogo de Legacy no se toca
# ---------------------------------------------------------------------------
class TestNoContamina:
    async def test_las_tecnicas_de_legacy_llegan_intactas(
        self, migration_engine: AsyncEngine
    ) -> None:
        """`techniques.unit_price` es lo que cobra el Cotizador historico."""
        _upgrade("0030")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO techniques"
                    " (code, name, unit_price, formula_type, factor_1, active,"
                    "  created_at, updated_at)"
                    " VALUES ('LEG-TORNO', 'Torno Legacy', 110, 'ONE_FACTOR', 1, true,"
                    "  now(), now())"
                )
            )

        _upgrade("0031")

        async with migration_engine.connect() as connection:
            fila = (
                await connection.execute(
                    text(
                        "SELECT unit_price, formula_type, factor_1 FROM techniques"
                        " WHERE code = 'LEG-TORNO'"
                    )
                )
            ).one()
            nuevas = await connection.scalar(text("SELECT count(*) FROM v2_techniques"))
        assert fila.unit_price == 110, "0031 cambio el precio de una tecnica de Legacy"
        assert fila.formula_type == "ONE_FACTOR"
        # Y el catalogo de V2 nace vacio: las tecnicas las escribe el taller.
        assert nuevas == 0

    async def test_una_cotizacion_de_010c_sobrevive_sin_ilustracion(
        self, migration_engine: AsyncEngine
    ) -> None:
        """NULL ahi es la verdad: nacio antes de que la ilustracion existiera."""
        _upgrade("0030")
        identificador = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000020")

        _upgrade("0031")

        async with migration_engine.connect() as connection:
            fila = (
                await connection.execute(
                    text(
                        "SELECT status, illustration_enabled, illustration_daily_rate_snapshot,"
                        "       illustration_hours, effective_work_days"
                        "  FROM v2_quotations WHERE id = :id"
                    ),
                    {"id": identificador},
                )
            ).one()
        assert fila.status == "DRAFT"
        # El estado nace con valor —no hay ilustracion, y eso es cierto—;
        # el snapshot nace en NULL, que es «no se congelo nada».
        assert fila.illustration_enabled is False
        assert fila.illustration_daily_rate_snapshot is None
        assert fila.illustration_hours == 0
        assert fila.effective_work_days is None

    async def test_no_se_siembra_ninguna_tecnica_de_ejemplo(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Sembrar «torno, asas, vidriado» seria inventar rendimientos."""
        _upgrade("0031")
        async with migration_engine.connect() as connection:
            tecnicas = await connection.scalar(text("SELECT count(*) FROM v2_techniques"))
            trabajadores = await connection.scalar(text("SELECT count(*) FROM v2_workers"))
        assert tecnicas == 0
        assert trabajadores == 0


# ---------------------------------------------------------------------------
# 2. Lo que la base no deja escribir mal
# ---------------------------------------------------------------------------
class TestRestricciones:
    async def test_un_rendimiento_de_cero_no_cabe(self, migration_engine: AsyncEngine) -> None:
        """Seria una division por cero en la formula de horas."""
        _upgrade("0031")
        with pytest.raises(IntegrityError):
            await _tecnica(migration_engine, "cero", capacidad="0")

    async def test_una_jornada_de_veinticinco_horas_no_cabe(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0031")
        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO v2_workers"
                        " (name, worker_type, daily_rate, workday_hours, created_at, updated_at)"
                        " VALUES ('Imposible', 'INTERNAL', 120, 25, now(), now())"
                    )
                )

    async def test_una_tarifa_negativa_no_cabe(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0031")
        with pytest.raises(IntegrityError):
            await _trabajador(migration_engine, "Negativo", jornal="-1")

    async def test_la_jornada_nula_del_trabajador_si_cabe(
        self, migration_engine: AsyncEngine
    ) -> None:
        """NULL significa «la jornada del taller», y es el caso normal."""
        _upgrade("0031")
        identificador = await _trabajador(migration_engine, "Con la del taller")
        async with migration_engine.connect() as connection:
            horas = await connection.scalar(
                text("SELECT workday_hours FROM v2_workers WHERE id = :id"),
                {"id": identificador},
            )
        assert horas is None

    async def test_la_ilustracion_apagada_no_puede_costar_nada(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Cobrar lo que alguien apago es el error que este CHECK impide."""
        _upgrade("0031")
        identificador = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000021")
        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE v2_quotations"
                        "   SET illustration_enabled = false, illustration_cost = 165"
                        " WHERE id = :id"
                    ),
                    {"id": identificador},
                )

    async def test_con_la_ilustracion_encendida_el_costo_si_cabe(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0031")
        identificador = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000022")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE v2_quotations"
                    "   SET illustration_enabled = true, illustration_quantity = 75,"
                    "       illustration_hours = 12, illustration_cost = 165"
                    " WHERE id = :id"
                ),
                {"id": identificador},
            )
        async with migration_engine.connect() as connection:
            costo = await connection.scalar(
                text("SELECT illustration_cost FROM v2_quotations WHERE id = :id"),
                {"id": identificador},
            )
        assert costo == Decimal(165)

    async def test_borrar_la_cotizacion_se_lleva_sus_tareas(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0031")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000023")
        worker = await _trabajador(migration_engine, "Arrastrado")
        tecnica = await _tecnica(migration_engine, "arrastre")
        await _tarea(migration_engine, cotizacion, worker, tecnica)

        async with migration_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM v2_quotations WHERE id = :id"), {"id": cotizacion}
            )

        async with migration_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM v2_quotation_labor")) == 0
            # La persona sigue en el maestro: es del taller, no de la cotizacion.
            assert await connection.scalar(text("SELECT count(*) FROM v2_workers")) == 1

    async def test_un_trabajador_con_tareas_no_se_puede_borrar(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Borrarlo dejaria una cotizacion emitida sin poder explicarse."""
        _upgrade("0031")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000024")
        worker = await _trabajador(migration_engine, "Con historia")
        tecnica = await _tecnica(migration_engine, "historia")
        await _tarea(migration_engine, cotizacion, worker, tecnica)

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM v2_workers WHERE id = :id"), {"id": worker}
                )

    async def test_dos_tecnicas_no_comparten_codigo(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0031")
        await _tecnica(migration_engine, "repetida")
        with pytest.raises(IntegrityError):
            await _tecnica(migration_engine, "repetida")


# ---------------------------------------------------------------------------
# 3. La vuelta
# ---------------------------------------------------------------------------
class TestDowngrade:
    async def test_revertir_en_vacio_deja_el_esquema_de_0030(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0031")
        await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000025")

        resultado = _alembic("downgrade", "0030")
        assert resultado.returncode == 0, f"{resultado.stdout}\n{resultado.stderr}"

        assert await _tabla(migration_engine, "v2_quotation_labor") is None
        assert await _tabla(migration_engine, "v2_workers") is None
        assert await _tabla(migration_engine, "v2_techniques") is None
        # Y lo de 010C sigue en pie, con su cotizacion dentro.
        assert await _tabla(migration_engine, "v2_material_costs") is not None
        async with migration_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM v2_quotations")) == 1

    async def test_revertir_se_niega_si_ya_hay_tareas(self, migration_engine: AsyncEngine) -> None:
        _upgrade("0031")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000026")
        worker = await _trabajador(migration_engine, "Irreversible")
        tecnica = await _tecnica(migration_engine, "irreversible")
        await _tarea(migration_engine, cotizacion, worker, tecnica)

        resultado = _alembic("downgrade", "0030")

        assert resultado.returncode != 0
        assert (
            "no puede revertirse" in resultado.stderr or "no puede revertirse" in resultado.stdout
        )
        assert await _tabla(migration_engine, "v2_quotation_labor") is not None

    async def test_revertir_se_niega_si_solo_hay_maestros(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Un catalogo escrito a mano no vuelve reaplicando la revision."""
        _upgrade("0031")
        await _tecnica(migration_engine, "solo-catalogo")

        resultado = _alembic("downgrade", "0030")

        assert resultado.returncode != 0
        assert await _tabla(migration_engine, "v2_techniques") is not None

    async def test_revertir_se_niega_si_solo_hay_dias_decididos(
        self, migration_engine: AsyncEngine
    ) -> None:
        """El hueco que encontro la auditoria: ni tareas, ni maestros, ni
        ilustracion, y aun asi hay algo que no se recupera.

        Los dias efectivos son una DECISION de quien planifica. El sistema solo
        sugiere un minimo, asi que borrar la columna perderia el dato sin forma
        de recalcularlo.
        """
        _upgrade("0031")
        identificador = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000028")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text("UPDATE v2_quotations SET effective_work_days = 5 WHERE id = :id"),
                {"id": identificador},
            )

        resultado = _alembic("downgrade", "0030")

        assert resultado.returncode != 0
        async with migration_engine.connect() as connection:
            dias = await connection.scalar(
                text("SELECT effective_work_days FROM v2_quotations WHERE id = :id"),
                {"id": identificador},
            )
        assert dias == 5

    async def test_revertir_se_niega_si_hay_ilustracion_congelada(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0031")
        identificador = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000027")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE v2_quotations"
                    "   SET illustration_enabled = true, illustration_quantity = 50,"
                    "       illustration_daily_rate_snapshot = 110,"
                    "       illustration_hours = 8, illustration_cost = 110"
                    " WHERE id = :id"
                ),
                {"id": identificador},
            )

        resultado = _alembic("downgrade", "0030")

        assert resultado.returncode != 0
        async with migration_engine.connect() as connection:
            costo = await connection.scalar(
                text("SELECT illustration_cost FROM v2_quotations WHERE id = :id"),
                {"id": identificador},
            )
        assert costo == Decimal(110)


# ---------------------------------------------------------------------------
# 4. La cadena entera
# ---------------------------------------------------------------------------
async def test_subir_hasta_la_cabeza_deja_una_sola_y_con_las_tres_tablas(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("head")

    heads = _alembic("heads")
    assert heads.returncode == 0, heads.stderr
    assert heads.stdout.count("(head)") == 1, heads.stdout

    for tabla in ("v2_workers", "v2_techniques", "v2_quotation_labor"):
        assert await _tabla(migration_engine, tabla) is not None, tabla
