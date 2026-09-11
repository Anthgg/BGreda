"""Fase 010C — la migracion 0030 se ejecuta de verdad contra PostgreSQL.

Como las de 0015 a 0029: alembic real en un subproceso, sobre una base propia,
la ida y la vuelta.

Leer el fichero no basta en esta fase. Lo que aqui hay que ver funcionando es:

1. que la columna GENERADA calcule el costo del ejemplo aprobado —100 kg por
   S/100 mas S/30 de transporte dan S/0,0013 el gramo— y que se recalcule sola
   cuando alguien edita la compra. Una expresion mal escrita pasa cualquier
   prueba de texto y falla aqui;
2. que `products.cost` siga exactamente igual al otro lado. Es lo que cobra el
   Cotizador historico;
3. que el CHECK del esmalte apagado muerda de verdad;
4. que el downgrade se niegue cuando ya hay lineas o valorizaciones.
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
MIGRATION_DB = "greda_migration_0030"
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


async def _producto(engine: AsyncEngine, sku: str, *, cost: Decimal | None = None) -> int:
    """Un material del maestro, con el `cost` que lee el Cotizador historico."""
    async with engine.begin() as connection:
        categoria = await connection.scalar(
            text("SELECT id FROM product_categories ORDER BY id LIMIT 1")
        )
        if categoria is None:
            categoria = await connection.scalar(
                text(
                    "INSERT INTO product_categories"
                    " (name, display_path, active, created_at, updated_at)"
                    " VALUES ('Materiales 0030', 'Materiales 0030', true, now(), now())"
                    " RETURNING id"
                )
            )
        unidad = await connection.scalar(
            text("SELECT code FROM units_of_measure ORDER BY code = 'g' DESC, code LIMIT 1")
        )
        producto = await connection.scalar(
            text(
                "INSERT INTO products"
                " (internal_reference, name, product_type, product_category_id,"
                "  base_uom_code, cost, active, created_at, updated_at)"
                " VALUES (:sku, :sku, 'RAW_MATERIAL', :categoria, :unidad, :cost,"
                "  true, now(), now())"
                " RETURNING id"
            ),
            {"sku": sku, "categoria": categoria, "unidad": unidad, "cost": cost},
        )
    assert producto is not None
    return int(producto)


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


async def _valorizar(
    engine: AsyncEngine,
    producto: int,
    *,
    cantidad: str,
    compra: str,
    transporte: str = "0",
    override: str | None = None,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v2_material_costs"
                " (product_id, material_kind, origin, purchase_quantity, purchase_cost,"
                "  transport_cost, costing_override_per_unit, created_at, updated_at)"
                " VALUES (:producto, 'BODY', 'PURCHASE', :cantidad, :compra,"
                "  :transporte, :override, now(), now())"
            ),
            {
                "producto": producto,
                "cantidad": Decimal(cantidad),
                "compra": Decimal(compra),
                "transporte": Decimal(transporte),
                "override": Decimal(override) if override is not None else None,
            },
        )


async def _costo(engine: AsyncEngine, producto: int) -> Decimal | None:
    async with engine.connect() as connection:
        return await connection.scalar(
            text("SELECT effective_cost_per_unit FROM v2_material_costs WHERE product_id = :p"),
            {"p": producto},
        )


# ---------------------------------------------------------------------------
# 1. El costo se deriva, y se deriva bien
# ---------------------------------------------------------------------------
class TestCostoGenerado:
    async def test_el_costo_por_gramo_del_ejemplo_aprobado(
        self, migration_engine: AsyncEngine
    ) -> None:
        """100 kg por S/100 mas S/30 de transporte son S/0,0013 el gramo.

        Sin el transporte darian 0,001: un 23 % menos, en cada gramo de cada
        pieza de cada cotizacion, sin que nada avisara.
        """
        _upgrade("0030")
        producto = await _producto(migration_engine, "PASTA-0030-A")

        await _valorizar(
            migration_engine, producto, cantidad="100000", compra="100", transporte="30"
        )

        assert await _costo(migration_engine, producto) == Decimal("0.0013")

    async def test_editar_la_compra_recalcula_el_costo_sin_que_nadie_lo_pida(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Esto es lo que compra la columna generada.

        Guardado como numero corriente, un UPDATE por cualquier otra via
        —un script, una consola, una fase futura— dejaria el costo mintiendo.
        """
        _upgrade("0030")
        producto = await _producto(migration_engine, "PASTA-0030-B")
        await _valorizar(
            migration_engine, producto, cantidad="100000", compra="100", transporte="30"
        )

        async with migration_engine.begin() as connection:
            await connection.execute(
                text("UPDATE v2_material_costs SET transport_cost = 70 WHERE product_id = :p"),
                {"p": producto},
            )

        assert await _costo(migration_engine, producto) == Decimal("0.0017")

    async def test_el_costo_generado_no_se_puede_escribir_a_mano(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Si se pudiera, no seria una derivacion: seria un segundo dato."""
        _upgrade("0030")
        producto = await _producto(migration_engine, "PASTA-0030-C")
        await _valorizar(migration_engine, producto, cantidad="1000", compra="10")

        with pytest.raises(Exception, match=r"generated|generada|GENERATED"):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE v2_material_costs SET effective_cost_per_unit = 99"
                        " WHERE product_id = :p"
                    ),
                    {"p": producto},
                )

    async def test_un_material_regalado_se_valoriza_por_lo_que_vale(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Adquirirlo costo cero. Regalar tambien el precio de venta es otra cosa."""
        _upgrade("0030")
        producto = await _producto(migration_engine, "PASTA-0030-D")

        await _valorizar(
            migration_engine,
            producto,
            cantidad="100000",
            compra="0",
            override="0.0012",
        )

        assert await _costo(migration_engine, producto) == Decimal("0.0012")

    async def test_valorizar_en_cero_es_una_decision_y_se_respeta(
        self, migration_engine: AsyncEngine
    ) -> None:
        """`COALESCE` distingue «no hay decision» de «la decision es cero»."""
        _upgrade("0030")
        producto = await _producto(migration_engine, "PASTA-0030-E")

        await _valorizar(migration_engine, producto, cantidad="1000", compra="10", override="0")

        assert await _costo(migration_engine, producto) == Decimal(0)

    async def test_un_material_no_admite_dos_valorizaciones(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0030")
        producto = await _producto(migration_engine, "PASTA-0030-F")
        await _valorizar(migration_engine, producto, cantidad="1000", compra="10")

        with pytest.raises(IntegrityError):
            await _valorizar(migration_engine, producto, cantidad="2000", compra="20")

    async def test_una_cantidad_de_cero_no_llega_a_la_tabla(
        self, migration_engine: AsyncEngine
    ) -> None:
        """El CHECK la para antes; `NULLIF` solo cubre lo que se cuele por otra via."""
        _upgrade("0030")
        producto = await _producto(migration_engine, "PASTA-0030-G")

        with pytest.raises(IntegrityError):
            await _valorizar(migration_engine, producto, cantidad="0", compra="10")


# ---------------------------------------------------------------------------
# 2. Nada de lo que ya existia cambia
# ---------------------------------------------------------------------------
class TestNoContamina:
    async def test_el_costo_del_maestro_llega_intacto_al_otro_lado(
        self, migration_engine: AsyncEngine
    ) -> None:
        """`products.cost` es lo que lee el costeo Legacy. 0030 no lo mira."""
        _upgrade("0029")
        producto = await _producto(migration_engine, "PASTA-0030-H", cost=Decimal("7.500000"))

        _upgrade("0030")

        async with migration_engine.connect() as connection:
            costo = await connection.scalar(
                text("SELECT cost FROM products WHERE id = :p"), {"p": producto}
            )
        assert costo == Decimal("7.500000")

    async def test_valorizar_para_v2_no_toca_el_costo_del_maestro(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Son dos costos distintos a proposito, y el de Legacy manda en Legacy."""
        _upgrade("0030")
        producto = await _producto(migration_engine, "PASTA-0030-I", cost=Decimal("7.500000"))

        await _valorizar(
            migration_engine, producto, cantidad="100000", compra="100", transporte="30"
        )

        async with migration_engine.connect() as connection:
            costo = await connection.scalar(
                text("SELECT cost FROM products WHERE id = :p"), {"p": producto}
            )
        assert costo == Decimal("7.500000")

    async def test_0030_no_deja_un_solo_movimiento_de_inventario(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Cotizar no consume material. El consumo es de produccion, en 010I."""
        _upgrade("0029")
        async with migration_engine.connect() as connection:
            antes = await connection.scalar(text("SELECT count(*) FROM stock_movements"))

        _upgrade("0030")

        async with migration_engine.connect() as connection:
            despues = await connection.scalar(text("SELECT count(*) FROM stock_movements"))
        assert antes == despues

    async def test_una_cotizacion_de_010b_sobrevive_sin_lineas(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Cero lineas es la verdad: nacio antes de que existieran."""
        _upgrade("0029")
        identificador = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000010")

        _upgrade("0030")

        async with migration_engine.connect() as connection:
            lineas = await connection.scalar(
                text("SELECT count(*) FROM v2_quotation_products WHERE v2_quotation_id = :q"),
                {"q": identificador},
            )
            estado = await connection.scalar(
                text("SELECT status FROM v2_quotations WHERE id = :q"), {"q": identificador}
            )
        assert lineas == 0
        assert estado == "DRAFT"


# ---------------------------------------------------------------------------
# 3. Lo que la base no deja escribir mal
# ---------------------------------------------------------------------------
class TestRestricciones:
    async def test_un_esmalte_apagado_no_puede_costar_nada(
        self, migration_engine: AsyncEngine
    ) -> None:
        """El CHECK existe porque el error no seria visible: un importe de mas."""
        _upgrade("0030")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000011")

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO v2_quotation_products"
                        " (v2_quotation_id, quantity, requires_glaze, glaze_cost,"
                        "  created_at, updated_at)"
                        " VALUES (:q, 10, false, 300, now(), now())"
                    ),
                    {"q": cotizacion},
                )

    async def test_con_el_esmalte_encendido_el_costo_si_cabe(
        self, migration_engine: AsyncEngine
    ) -> None:
        """La otra mitad del CHECK: apagado prohibe, encendido no estorba."""
        _upgrade("0030")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000012")

        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO v2_quotation_products"
                    " (v2_quotation_id, quantity, requires_glaze, glaze_total_weight,"
                    "  glaze_volume_ml, glaze_cost, created_at, updated_at)"
                    " VALUES (:q, 20, true, 1500, 1500, 300, now(), now())"
                ),
                {"q": cotizacion},
            )

        async with migration_engine.connect() as connection:
            costo = await connection.scalar(
                text("SELECT glaze_cost FROM v2_quotation_products WHERE v2_quotation_id = :q"),
                {"q": cotizacion},
            )
        assert costo == Decimal(300)

    async def test_borrar_la_cotizacion_se_lleva_sus_lineas(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0030")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000013")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO v2_quotation_products"
                    " (v2_quotation_id, quantity, created_at, updated_at)"
                    " VALUES (:q, 5, now(), now())"
                ),
                {"q": cotizacion},
            )

        async with migration_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM v2_quotations WHERE id = :q"), {"q": cotizacion}
            )

        async with migration_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM v2_quotation_products")) == 0

    async def test_un_material_usado_en_una_linea_no_se_puede_borrar(
        self, migration_engine: AsyncEngine
    ) -> None:
        """RESTRICT: la linea guarda el nombre, pero el vinculo tambien vale."""
        _upgrade("0030")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000014")
        producto = await _producto(migration_engine, "PASTA-0030-J")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO v2_quotation_products"
                    " (v2_quotation_id, quantity, body_material_id, created_at, updated_at)"
                    " VALUES (:q, 5, :p, now(), now())"
                ),
                {"q": cotizacion, "p": producto},
            )

        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM products WHERE id = :p"), {"p": producto}
                )


# ---------------------------------------------------------------------------
# 4. La vuelta
# ---------------------------------------------------------------------------
class TestDowngrade:
    async def test_revertir_en_vacio_deja_el_esquema_de_0029(
        self, migration_engine: AsyncEngine
    ) -> None:
        _upgrade("0030")
        await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000015")

        resultado = _alembic("downgrade", "0029")
        assert resultado.returncode == 0, f"{resultado.stdout}\n{resultado.stderr}"

        assert await _tabla(migration_engine, "v2_material_costs") is None
        assert await _tabla(migration_engine, "v2_quotation_products") is None
        # Y lo de 010B sigue en pie.
        assert await _tabla(migration_engine, "v2_commercial_settings") is not None
        async with migration_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM v2_quotations")) == 1

    async def test_revertir_se_niega_si_ya_hay_lineas(self, migration_engine: AsyncEngine) -> None:
        """Una linea guarda el material con el que se calculo un precio."""
        _upgrade("0030")
        cotizacion = await _cotizacion_v2(migration_engine, "CTZ-V2-2026-000016")
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO v2_quotation_products"
                    " (v2_quotation_id, quantity, created_at, updated_at)"
                    " VALUES (:q, 20, now(), now())"
                ),
                {"q": cotizacion},
            )

        resultado = _alembic("downgrade", "0029")

        assert resultado.returncode != 0
        assert (
            "no puede revertirse" in resultado.stderr or "no puede revertirse" in resultado.stdout
        )
        assert await _tabla(migration_engine, "v2_quotation_products") is not None

    async def test_revertir_se_niega_si_ya_hay_materiales_valorizados(
        self, migration_engine: AsyncEngine
    ) -> None:
        """Es parametrizacion escrita a mano: no vuelve sola con un upgrade."""
        _upgrade("0030")
        producto = await _producto(migration_engine, "PASTA-0030-K")
        await _valorizar(
            migration_engine, producto, cantidad="100000", compra="100", transporte="30"
        )

        resultado = _alembic("downgrade", "0029")

        assert resultado.returncode != 0
        assert await _tabla(migration_engine, "v2_material_costs") is not None
        assert await _costo(migration_engine, producto) == Decimal("0.0013")


# ---------------------------------------------------------------------------
# 5. La cadena entera
# ---------------------------------------------------------------------------
async def test_subir_hasta_la_cabeza_deja_una_sola_y_con_las_dos_tablas(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("head")

    heads = _alembic("heads")
    assert heads.returncode == 0, heads.stderr
    assert heads.stdout.count("(head)") == 1, heads.stdout

    assert await _tabla(migration_engine, "v2_material_costs") is not None
    assert await _tabla(migration_engine, "v2_quotation_products") is not None
