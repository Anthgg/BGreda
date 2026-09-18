"""Fase 010I — la migracion 0037 contra PostgreSQL real.

1. sube: aparece el tercer origen y las ordenes que ya habia no se tocan;
2. el CHECK de origen muerde por los tres lados y admite los tres casos legitimos;
3. una cotizacion V2 no admite dos ordenes, lo garantiza la base;
4. bajar se niega si hay ordenes V2, y cuando no las hay deja las de muestra
   intactas: 0037 no deshace lo que es de 0027;
5. la cadena entera deja una sola cabeza, y es 0037.
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
MIGRATION_DB = "greda_migration_0037"
REPO_ROOT = Path(__file__).parents[2]

CK_ORIGEN = "ck_production_orders_exactly_one_origin"
FK_PUENTE = "fk_production_orders_v2_handoff_id_v2_production_handoffs"
UQ_PUENTE = "uq_production_orders_v2_handoff_id"

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


async def _escalar(engine: AsyncEngine, sql: str, parametros: dict[str, object]) -> int:
    async with engine.begin() as connection:
        valor = await connection.scalar(text(sql), parametros)
    assert valor is not None
    return int(valor)


async def _restriccion(engine: AsyncEngine, nombre: str) -> str | None:
    async with engine.connect() as connection:
        return await connection.scalar(
            text("SELECT conname FROM pg_constraint WHERE conname = :nombre"),
            {"nombre": nombre},
        )


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


# ---------------------------------------------------------------------------
# Semillas, en SQL y sin modelos: una prueba de migracion tiene que poder
# sembrar una base que todavia no conoce el codigo de hoy.
# ---------------------------------------------------------------------------
async def _almacen(engine: AsyncEngine, nombre: str) -> int:
    return await _escalar(
        engine,
        "INSERT INTO stock_locations (name, active, created_at, updated_at)"
        " VALUES (:nombre, true, now(), now()) RETURNING id",
        {"nombre": nombre},
    )


async def _muestra(engine: AsyncEngine, codigo: str) -> int:
    return await _escalar(
        engine,
        "INSERT INTO prototypes"
        " (code, name, quantity, status, approval, requested_at, created_at, updated_at)"
        " VALUES (:codigo, :codigo, 1, 'CREATED', 'PENDING', now(), now(), now())"
        " RETURNING id",
        {"codigo": codigo},
    )


async def _cotizacion_legacy(engine: AsyncEngine, codigo: str) -> int:
    return await _escalar(
        engine,
        "INSERT INTO quotations"
        " (code, status, workflow, source_fingerprint,"
        "  commercial_factor_default_snapshot, commercial_factor,"
        "  created_at, updated_at)"
        " VALUES (:codigo, 'CONFIRMED', 'LEGACY', :huella, 3, 3, now(), now())"
        " RETURNING id",
        {"codigo": codigo, "huella": "0" * 64},
    )


async def _puente(engine: AsyncEngine, codigo: str) -> int:
    """Una cotizacion V2 y su puente a produccion.

    La cotizacion va en borrador a proposito: la tabla del puente no mira el
    estado de la cotizacion, y aqui solo interesa la forma de la base.
    """
    cotizacion = await _escalar(
        engine,
        "INSERT INTO v2_quotations"
        " (code, pricing_engine_version, status, production_type, created_at, updated_at)"
        " VALUES (:codigo, 'V2', 'DRAFT', 'RETAIL', now(), now()) RETURNING id",
        {"codigo": codigo},
    )
    return await _escalar(
        engine,
        "INSERT INTO v2_production_handoffs"
        " (v2_quotation_id, commercial_fingerprint, created_at, updated_at)"
        " VALUES (:cotizacion, :huella, now(), now()) RETURNING id",
        {"cotizacion": cotizacion, "huella": "a" * 64},
    )


#: Las TRES columnas de origen se nombran siempre y viajan como parametros.
_INSERT_ORDEN = text(
    "INSERT INTO production_orders"
    " (code, quotation_id, prototype_id, v2_handoff_id, stock_location_id, status,"
    "  qr_token, created_at, updated_at)"
    " VALUES (:codigo, :quotation_id, :prototype_id, :v2_handoff_id, :almacen,"
    "  'CREATED', :token, now(), now())"
)

#: La misma para una base en 0036, que no tiene el tercer origen.
_INSERT_ORDEN_0036 = text(
    "INSERT INTO production_orders"
    " (code, quotation_id, prototype_id, stock_location_id, status, qr_token,"
    "  created_at, updated_at)"
    " VALUES (:codigo, :quotation_id, :prototype_id, :almacen, 'CREATED', :token,"
    "  now(), now())"
)


async def _orden(
    engine: AsyncEngine,
    codigo: str,
    *,
    almacen: int,
    quotation_id: int | None = None,
    prototype_id: int | None = None,
    v2_handoff_id: int | None = None,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            _INSERT_ORDEN,
            {
                "codigo": codigo,
                "quotation_id": quotation_id,
                "prototype_id": prototype_id,
                "v2_handoff_id": v2_handoff_id,
                "almacen": almacen,
                "token": (codigo + "-" + "t" * 43)[:64],
            },
        )


# ---------------------------------------------------------------------------
# Pruebas
# ---------------------------------------------------------------------------
async def test_0036_a_0037_abre_el_tercer_origen(migration_engine: AsyncEngine) -> None:
    _upgrade("0036")
    assert await _columna(migration_engine, "production_orders", "v2_handoff_id") is None

    _upgrade("0037")
    assert await _columna(migration_engine, "production_orders", "v2_handoff_id") == "YES"
    for nombre in (CK_ORIGEN, FK_PUENTE, UQ_PUENTE):
        assert await _restriccion(migration_engine, nombre) == nombre, nombre


async def test_0037_no_toca_las_ordenes_que_ya_habia(migration_engine: AsyncEngine) -> None:
    """Una orden Legacy y una de muestra, sembradas en 0036, salen de 0037 igual."""
    _upgrade("0036")
    almacen = await _almacen(migration_engine, "Almacen antes de 0037")
    cotizacion = await _cotizacion_legacy(migration_engine, "CTZ-ANTES-0037")
    muestra = await _muestra(migration_engine, "PRT-ANTES-0037")
    async with migration_engine.begin() as connection:
        for codigo, quotation_id, prototype_id in (
            ("OP-CTZ-ANTES", cotizacion, None),
            ("OP-PRT-ANTES", None, muestra),
        ):
            await connection.execute(
                _INSERT_ORDEN_0036,
                {
                    "codigo": codigo,
                    "quotation_id": quotation_id,
                    "prototype_id": prototype_id,
                    "almacen": almacen,
                    "token": (codigo + "-" + "t" * 43)[:64],
                },
            )

    _upgrade("0037")

    async with migration_engine.connect() as connection:
        filas = (
            await connection.execute(
                text(
                    "SELECT code, quotation_id, prototype_id, v2_handoff_id, status"
                    " FROM production_orders ORDER BY code"
                )
            )
        ).all()
    assert [tuple(fila) for fila in filas] == [
        ("OP-CTZ-ANTES", cotizacion, None, None, "CREATED"),
        ("OP-PRT-ANTES", None, muestra, None, "CREATED"),
    ]


async def test_una_orden_tiene_exactamente_uno_de_los_tres_origenes(
    migration_engine: AsyncEngine,
) -> None:
    """El CHECK muerde sin origen y con dos, y deja pasar cada origen solo."""
    _upgrade("0037")
    almacen = await _almacen(migration_engine, "Almacen origen 0037")
    cotizacion = await _cotizacion_legacy(migration_engine, "CTZ-XOR-0037")
    muestra = await _muestra(migration_engine, "PRT-XOR-0037")
    puente = await _puente(migration_engine, "CTZV2-XOR-0037")

    prohibidas = (
        ("OP-SIN", {}),
        ("OP-CTZ-V2", {"quotation_id": cotizacion, "v2_handoff_id": puente}),
        ("OP-PRT-V2", {"prototype_id": muestra, "v2_handoff_id": puente}),
        ("OP-TRES", {"quotation_id": cotizacion, "prototype_id": muestra, "v2_handoff_id": puente}),
    )
    for codigo, origenes in prohibidas:
        with pytest.raises(IntegrityError):
            await _orden(migration_engine, codigo, almacen=almacen, **origenes)

    await _orden(migration_engine, "OP-SOLO-CTZ", almacen=almacen, quotation_id=cotizacion)
    await _orden(migration_engine, "OP-SOLO-PRT", almacen=almacen, prototype_id=muestra)
    await _orden(migration_engine, "OP-SOLO-V2", almacen=almacen, v2_handoff_id=puente)


async def test_una_cotizacion_v2_no_admite_dos_ordenes(migration_engine: AsyncEngine) -> None:
    """Lo impide la base: el servicio solo no bastaria frente a dos peticiones a la vez."""
    _upgrade("0037")
    almacen = await _almacen(migration_engine, "Almacen unica 0037")
    puente = await _puente(migration_engine, "CTZV2-UNICA-0037")

    await _orden(migration_engine, "OP-V2-1", almacen=almacen, v2_handoff_id=puente)
    with pytest.raises(IntegrityError):
        await _orden(migration_engine, "OP-V2-2", almacen=almacen, v2_handoff_id=puente)


async def test_un_puente_con_orden_no_se_puede_borrar(migration_engine: AsyncEngine) -> None:
    """RESTRICT: el puente de una orden no desaparece por debajo."""
    _upgrade("0037")
    almacen = await _almacen(migration_engine, "Almacen restrict 0037")
    puente = await _puente(migration_engine, "CTZV2-RESTRICT-0037")
    await _orden(migration_engine, "OP-V2-RESTRICT", almacen=almacen, v2_handoff_id=puente)

    async with migration_engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                text("DELETE FROM v2_production_handoffs WHERE id = :id"), {"id": puente}
            )


async def test_la_vuelta_a_0036_se_niega_si_hay_ordenes_v2(migration_engine: AsyncEngine) -> None:
    _upgrade("0037")
    almacen = await _almacen(migration_engine, "Almacen bajada 0037")
    puente = await _puente(migration_engine, "CTZV2-BAJADA-0037")
    await _orden(migration_engine, "OP-V2-BAJADA", almacen=almacen, v2_handoff_id=puente)

    resultado = _alembic("downgrade", "0036")

    assert resultado.returncode != 0
    assert "0037 downgrade bloqueado" in resultado.stdout + resultado.stderr
    # Y no se ha tocado nada: la columna y su orden siguen ahi.
    assert await _columna(migration_engine, "production_orders", "v2_handoff_id") == "YES"


async def test_la_vuelta_a_0036_conserva_las_ordenes_de_muestra(
    migration_engine: AsyncEngine,
) -> None:
    """Sin ordenes V2 se puede bajar, y las de muestra sobreviven.

    Es la prueba de que 0037 NO devuelve `quotation_id` a NOT NULL: si lo
    hiciera, esta orden de muestra —sin cotizacion— haria fallar la bajada.
    """
    _upgrade("0037")
    almacen = await _almacen(migration_engine, "Almacen muestra 0037")
    muestra = await _muestra(migration_engine, "PRT-BAJADA-0037")
    await _orden(migration_engine, "OP-PRT-BAJADA", almacen=almacen, prototype_id=muestra)

    resultado = _alembic("downgrade", "0036")
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr

    assert await _columna(migration_engine, "production_orders", "v2_handoff_id") is None
    assert await _columna(migration_engine, "production_orders", "quotation_id") == "YES"
    for nombre in (FK_PUENTE, UQ_PUENTE):
        assert await _restriccion(migration_engine, nombre) is None, nombre
    # El CHECK vuelve a ser el de dos ramas y sigue en su sitio.
    assert await _restriccion(migration_engine, CK_ORIGEN) == CK_ORIGEN
    async with migration_engine.connect() as connection:
        muestras = await connection.scalar(
            text("SELECT count(*) FROM production_orders WHERE prototype_id = :id"),
            {"id": muestra},
        )
    assert muestras == 1


async def test_los_check_del_consumo_no_llevan_doble_prefijo(
    migration_engine: AsyncEngine,
) -> None:
    """Hallazgo de la revision del bloque B: comprobarlo en la base, no en el texto.

    0036 cayo en la trampa de 0024: sus CHECK se llaman
    `ck_v2_quotation_processes_ck_v2_quotation_processes_...` en la base real. Aqui
    se fija que los de la tabla nueva salen con UN solo prefijo.
    """
    _upgrade("0037")
    async with migration_engine.connect() as connection:
        nombres = set(
            (
                await connection.scalars(
                    text(
                        "SELECT conname FROM pg_constraint"
                        " WHERE conrelid = 'production_consumptions'::regclass"
                    )
                )
            ).all()
        )
    for esperado in (
        "ck_production_consumptions_quantity_positive",
        "ck_production_consumptions_kind_allowed",
        "ck_production_consumptions_uom_not_blank",
        "ck_production_consumptions_unit_cost_non_negative",
        "ck_production_consumptions_idempotency_key_long_enough",
        "uq_production_consumptions_stock_movement_id",
        "uq_production_consumptions_idempotency_key",
        "pk_production_consumptions",
    ):
        assert esperado in nombres, (esperado, sorted(nombres))
    # El patron exacto de la trampa, y no «ck_ aparece dos veces»: eso tambien
    # lo cumplen FK legitimas como `...stock_location_id_stock_locations`.
    doble = "ck_production_consumptions_ck_"
    assert not [n for n in nombres if n.startswith(doble)], sorted(nombres)


async def test_los_check_de_las_notas_no_llevan_doble_prefijo(
    migration_engine: AsyncEngine,
) -> None:
    """Bloque C: la tabla de notas y quemas, con la misma comprobacion."""
    _upgrade("0037")
    async with migration_engine.connect() as connection:
        nombres = set(
            (
                await connection.scalars(
                    text(
                        "SELECT conname FROM pg_constraint"
                        " WHERE conrelid = 'production_order_notes'::regclass"
                    )
                )
            ).all()
        )
    for esperado in (
        "ck_production_order_notes_kind_allowed",
        "ck_production_order_notes_firing_type_allowed",
        "ck_production_order_notes_kind_fields_consistent",
        "ck_production_order_notes_body_length",
        "ck_production_order_notes_idempotency_key_long_enough",
        "uq_production_order_notes_idempotency_key",
        "pk_production_order_notes",
    ):
        assert esperado in nombres, (esperado, sorted(nombres))
    assert not [n for n in nombres if n.startswith("ck_production_order_notes_ck_")]


async def test_los_check_de_las_comunicaciones_no_llevan_doble_prefijo(
    migration_engine: AsyncEngine,
) -> None:
    """Bloque D: la tabla de comunicaciones, con la misma comprobacion."""
    _upgrade("0037")
    async with migration_engine.connect() as connection:
        nombres = set(
            (
                await connection.scalars(
                    text(
                        "SELECT conname FROM pg_constraint"
                        " WHERE conrelid = 'production_order_communications'::regclass"
                    )
                )
            ).all()
        )
    for esperado in (
        "ck_production_order_communications_channel_allowed",
        "ck_production_order_communications_message_valid",
        "ck_production_order_communications_idempotency_key_long_enough",
        "uq_production_order_communications_idempotency_key",
        "pk_production_order_communications",
    ):
        assert esperado in nombres, (esperado, sorted(nombres))
    assert not [n for n in nombres if n.startswith("ck_production_order_communications_ck_")]


async def test_las_comunicaciones_de_la_base_migrada_se_comportan(
    migration_engine: AsyncEngine,
) -> None:
    """Hallazgo de Copilot en el bloque D: no basta con los NOMBRES.

    El esquema de las demas pruebas sale del modelo (`create_all`); este sale de
    la migracion. Aqui se comprueba que la tabla MIGRADA rechaza de verdad lo
    que dice rechazar —canal ajeno, mensaje en blanco o largo, clave corta,
    clave repetida— y que una orden con avisos no se puede borrar.
    """
    _upgrade("0037")
    almacen = await _almacen(migration_engine, "Almacen avisos 0037")
    puente = await _puente(migration_engine, "CTZV2-AVISOS-0037")
    await _orden(migration_engine, "OP-V2-AVISOS", almacen=almacen, v2_handoff_id=puente)
    async with migration_engine.connect() as connection:
        orden = await connection.scalar(
            text("SELECT id FROM production_orders WHERE code = 'OP-V2-AVISOS'")
        )

    insertar = text(
        "INSERT INTO production_order_communications"
        " (production_order_id, channel, message, sent_at, idempotency_key)"
        " VALUES (:orden, :canal, :mensaje, now(), :clave)"
    )
    valido = {"orden": orden, "canal": "WHATSAPP", "mensaje": "Hola", "clave": "aviso-migrado"}
    async with migration_engine.begin() as connection:
        await connection.execute(insertar, valido)

    for cambio, restriccion in (
        ({"canal": "EMAIL", "clave": "aviso-canal-x"}, "channel_allowed"),
        ({"mensaje": "   ", "clave": "aviso-blanco-x"}, "message_valid"),
        ({"mensaje": "x" * 2001, "clave": "aviso-largo-x"}, "message_valid"),
        ({"clave": "corta"}, "idempotency_key_long_enough"),
        ({}, "uq_production_order_communications_idempotency_key"),
    ):
        with pytest.raises(IntegrityError) as error:
            async with migration_engine.begin() as connection:
                await connection.execute(insertar, {**valido, **cambio})
        assert restriccion in str(error.value), (cambio, str(error.value)[:300])

    # RESTRICT: una orden con avisos registrados no desaparece por debajo.
    with pytest.raises(IntegrityError) as error:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM production_orders WHERE id = :id"), {"id": orden}
            )
    assert "production_order_communications" in str(error.value)

    async with migration_engine.connect() as connection:
        indices = set(
            (
                await connection.scalars(
                    text(
                        "SELECT indexname FROM pg_indexes"
                        " WHERE tablename = 'production_order_communications'"
                    )
                )
            ).all()
        )
    assert "ix_production_order_communications_production_order_id" in indices, indices


async def test_toda_la_cadena_deja_una_sola_cabeza_y_es_0037(
    migration_engine: AsyncEngine,
) -> None:
    _upgrade("head")
    async with migration_engine.connect() as connection:
        cabezas = list(
            (await connection.scalars(text("SELECT version_num FROM alembic_version"))).all()
        )
    assert cabezas == ["0037"]
