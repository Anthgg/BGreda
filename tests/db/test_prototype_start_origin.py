"""Fase 009K.1.1 — arrancar una muestra nacida de una cotizacion de prototipo.

Este archivo existe por un defecto que llego a produccion. `evaluate_readiness`
aprendio en 009K.1.1 que una muestra puede nacer de un CPR pagado y respondia
`ready: true`. La restriccion `ck_prototypes_started_requires_origin`, escrita
en 0021 cuando el unico origen era una cotizacion de producto, seguia exigiendo
`quotation_id IS NOT NULL`. Arrancar moria con `CheckViolationError` y salia por
la API como un 500 que no explicaba nada.

Ninguna de las 1535 pruebas lo vio porque **ninguna llamaba a START sobre una
muestra de CPR**. La que mas cerca estaba se paraba en la readiness y decia,
literalmente, «puede faltar almacen o existencia: eso es otra cosa». Aqui se
llega hasta el final: se arranca de verdad, y se mira el barro.

La base de pruebas se crea desde los modelos con `create_all`, no corriendo
migraciones. Por eso hay ademas una prueba que compara la expresion del modelo
con la que escribe la 0024: si una de las dos se mueve sin la otra, produccion
y pruebas volverian a discrepar, que es exactamente como empezo esto.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import MovementType, StockMovement
from app.models.prototypes import (
    STARTED_REQUIRES_ORIGIN,
    Prototype,
    PrototypeMaterialLine,
)
from app.services.inventory import InventoryService
from tests.db.test_production_orders_api import crear_ubicacion, dar_existencia
from tests.db.test_prototype_quotations import (
    COTIZADOR,
    _caso_referencia,
    _payload,
)
from tests.db.test_quotation_builder_api import head

PROTOTYPES = "/api/v1/prototypes"


#: La 0024 de verdad, leida del disco al importar y no dentro de una prueba:
#: tocar el disco desde una corrutina bloquearia el bucle de eventos.
#:
#: Se carga por ruta porque el nombre del modulo empieza por un digito y no es
#: un identificador valido de Python.
_RUTA_0024 = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0024_prototype_start_origin.py"
)


def _cargar_migracion() -> Any:
    spec = importlib.util.spec_from_file_location("migracion_0024", _RUTA_0024)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


MIGRACION_0024 = _cargar_migracion()
_FUENTE_0024 = _RUTA_0024.read_text(encoding="utf-8")


def _guarda_del_downgrade() -> str:
    """El bloque `DO $$ ... $$` REAL de la 0024, recortado de su fuente.

    Se extrae en vez de copiarse para que la prueba no acabe comprobando una
    replica que puede quedarse atras cuando la migracion cambie.
    """
    inicio = _FUENTE_0024.index("DO $$")
    fin = _FUENTE_0024.index("$$", _FUENTE_0024.index("END", inicio)) + 2
    return _FUENTE_0024[inicio:fin]


GUARDA_DOWNGRADE = _guarda_del_downgrade()


async def _movimientos(db_session: AsyncSession, prototype_id: int) -> list[StockMovement]:
    db_session.expire_all()
    return list(
        (
            await db_session.execute(
                select(StockMovement)
                .where(StockMovement.prototype_id == prototype_id)
                .order_by(StockMovement.id)
            )
        )
        .scalars()
        .all()
    )


async def _consumos(db_session: AsyncSession, prototype_id: int) -> list[Any]:
    db_session.expire_all()
    return list(
        (
            await db_session.execute(
                select(PrototypeMaterialLine.quantity_actual).where(
                    PrototypeMaterialLine.prototype_id == prototype_id
                )
            )
        )
        .scalars()
        .all()
    )


async def _muestra_de_cpr(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    sufijo: str,
    *,
    existencia: str = "1000",
    con_almacen: bool = True,
) -> dict[str, Any]:
    """Una muestra nacida al cobrar un CPR, lista para arrancar.

    Es el camino real: se emite, se cobra —y al cobrar nace la muestra—, el
    taller le asigna almacen y hay material en ese almacen. `quotation_id`
    queda nulo a proposito: ese es justo el caso que la base rechazaba.
    """
    caso = await _caso_referencia(api, csrf, db_session, sufijo)
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(csrf))
    assert creada.status_code == 201, creada.text
    confirmada = await api.post(f"{COTIZADOR}/{creada.json()['id']}/confirm", headers=head(csrf))
    assert confirmada.status_code == 200, confirmada.text
    pagada = await api.post(f"{COTIZADOR}/{confirmada.json()['id']}/mark-paid", headers=head(csrf))
    assert pagada.status_code == 200, pagada.text

    muestra_id = int(pagada.json()["prototype_id"])
    almacen = await crear_ubicacion(api, csrf, f"Almacen CPR{sufijo}")
    await dar_existencia(
        api, csrf, product_id=caso["_pasta"]["id"], location_id=almacen, cantidad=existencia
    )
    if con_almacen:
        puesto = await api.put(
            f"{PROTOTYPES}/{muestra_id}",
            json={"stock_location_id": almacen},
            headers=head(csrf),
        )
        assert puesto.status_code == 200, puesto.text

    db_session.expire_all()
    fila = await db_session.get(Prototype, muestra_id)
    assert fila is not None
    # La premisa del defecto, comprobada y no supuesta.
    assert fila.prototype_quotation_id is not None
    assert fila.quotation_id is None

    return {
        "prototype_id": muestra_id,
        "cotizacion": pagada.json(),
        "location_id": almacen,
        "pasta": caso["_pasta"],
    }


# ---------------------------------------------------------------------------
# A — el defecto, al derecho
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_muestra_de_cpr_pagado_arranca_y_gasta_el_barro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CPR_ORIGIN_START: PASS.

    Esta es la regresion exacta de produccion: `readiness` decia que si y la
    base decia que no. Ahora dicen lo mismo, y el material sale de verdad.
    """
    datos = await _muestra_de_cpr(api, admin_csrf, db_session, "_cprstart")

    detalle = await api.get(f"{PROTOTYPES}/{datos['prototype_id']}", headers=head(admin_csrf))
    assert detalle.status_code == 200, detalle.text
    assert detalle.json()["readiness"]["ready"] is True, detalle.json()["readiness"]

    arrancada = await api.post(
        f"{PROTOTYPES}/{datos['prototype_id']}/start", headers=head(admin_csrf)
    )
    assert arrancada.status_code == 200, arrancada.text
    assert arrancada.json()["status"] == "STARTED"

    movimientos = await _movimientos(db_session, datos["prototype_id"])
    assert len(movimientos) == 1
    assert movimientos[0].movement_type is MovementType.PROTOTYPE_OUT
    # 1.25 kg de pasta por muestra, una muestra. Sale del catalogo, no de aqui.
    assert movimientos[0].quantity == -Decimal("1.25")
    assert movimientos[0].quantity < 0

    consumos = await _consumos(db_session, datos["prototype_id"])
    assert consumos and all(valor is not None for valor in consumos)


# ---------------------------------------------------------------------------
# B — atomicidad
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_si_algo_revienta_a_mitad_del_arranque_no_queda_medio_consumo(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """START_ATOMIC: PASS.

    No basta con que falle: tiene que fallar sin dejar rastro. Se rompe el
    apunte de inventario a proposito, que es el punto donde un fallo podria
    dejar barro descontado y la muestra sin arrancar.
    """
    datos = await _muestra_de_cpr(api, admin_csrf, db_session, "_atomico")

    async def revienta(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("fallo inyectado a mitad del consumo")

    monkeypatch.setattr(InventoryService, "apply_movement", revienta)

    # Segun donde atrape la excepcion la aplicacion, esto sale como un 500 o
    # sube hasta aqui. Lo que se prueba no es la forma del fallo sino que la
    # transaccion no dejo nada escrito.
    try:
        respuesta = await api.post(
            f"{PROTOTYPES}/{datos['prototype_id']}/start", headers=head(admin_csrf)
        )
        assert respuesta.status_code >= 500, respuesta.text
    except RuntimeError:
        pass

    monkeypatch.undo()

    assert await _movimientos(db_session, datos["prototype_id"]) == []
    assert all(valor is None for valor in await _consumos(db_session, datos["prototype_id"]))
    db_session.expire_all()
    fila = await db_session.get(Prototype, datos["prototype_id"])
    assert fila is not None
    assert fila.status.value == "CREATED"
    assert fila.started_at is None


@pytest.mark.asyncio
async def test_sin_existencia_suficiente_no_se_descuenta_nada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La otra cara de la atomicidad, sin inyectar nada: falta barro."""
    datos = await _muestra_de_cpr(api, admin_csrf, db_session, "_pocobarro", existencia="0.5")

    respuesta = await api.post(
        f"{PROTOTYPES}/{datos['prototype_id']}/start", headers=head(admin_csrf)
    )
    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PROTOTYPE_NOT_READY"
    assert await _movimientos(db_session, datos["prototype_id"]) == []
    assert all(valor is None for valor in await _consumos(db_session, datos["prototype_id"]))


# ---------------------------------------------------------------------------
# C — idempotencia
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_arrancar_dos_veces_no_gasta_el_barro_dos_veces(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_START_IDEMPOTENT: PASS, por la via CPR."""
    datos = await _muestra_de_cpr(api, admin_csrf, db_session, "_idem")

    primera = await api.post(
        f"{PROTOTYPES}/{datos['prototype_id']}/start", headers=head(admin_csrf)
    )
    assert primera.status_code == 200, primera.text
    tras_primera = await _movimientos(db_session, datos["prototype_id"])
    consumo_primero = await _consumos(db_session, datos["prototype_id"])

    segunda = await api.post(
        f"{PROTOTYPES}/{datos['prototype_id']}/start", headers=head(admin_csrf)
    )
    assert segunda.status_code == 200, segunda.text

    assert len(await _movimientos(db_session, datos["prototype_id"])) == len(tras_primera) == 1
    assert await _consumos(db_session, datos["prototype_id"]) == consumo_primero


# ---------------------------------------------------------------------------
# D — la via de siempre no se toca
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_la_muestra_de_una_cotizacion_de_producto_sigue_arrancando(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """LEGACY_PROTOTYPE_START_REGRESSION: PASS.

    Ampliar el origen no puede aflojar ni endurecer el camino de 009K.
    """
    from tests.db.test_prototypes import _muestra_lista

    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_legacy0024")
    muestra_id = int(datos["prototipo"]["id"])

    db_session.expire_all()
    fila = await db_session.get(Prototype, muestra_id)
    assert fila is not None
    assert fila.quotation_id is not None
    assert fila.prototype_quotation_id is None

    arrancada = await api.post(f"{PROTOTYPES}/{muestra_id}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text
    movimientos = await _movimientos(db_session, muestra_id)
    assert len(movimientos) == 1
    assert movimientos[0].movement_type is MovementType.PROTOTYPE_OUT


# ---------------------------------------------------------------------------
# E — sin origen, se rechaza; y se rechaza BIEN
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_muestra_sin_origen_se_rechaza_con_un_error_de_dominio(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NO_ORIGIN_START_BLOCKED: PASS, y con 409, no con un 500.

    Un 500 aqui volveria a ser el mismo problema con otro disfraz: la base
    diciendo que no despues de que el servicio dijera que si.
    """
    from tests.db.test_prototypes import crear_prototipo

    creado = await crear_prototipo(api, admin_csrf, name="Sin origen 0024", quantity=1)
    assert creado.status_code == 201, creado.text
    muestra_id = int(creado.json()["id"])

    respuesta = await api.post(f"{PROTOTYPES}/{muestra_id}/start", headers=head(admin_csrf))
    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PROTOTYPE_NOT_READY"
    codigos = {d["code"] for d in respuesta.json()["error"]["details"]}
    assert "NO_QUOTATION" in codigos
    assert await _movimientos(db_session, muestra_id) == []


# ---------------------------------------------------------------------------
# F — la restriccion, contra PostgreSQL de verdad
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_la_restriccion_acepta_el_origen_cpr_y_sigue_exigiendo_almacen(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONSTRAINT_ACCEPTS_CPR_ORIGIN: PASS.

    Se habla con la tabla, no con el servicio: una fila arrancada cuyo unico
    origen es un CPR debe entrar, y la misma fila sin almacen debe seguir
    rebotando. Ampliar el origen no podia aflojar el resto del CHECK.
    """
    datos = await _muestra_de_cpr(api, admin_csrf, db_session, "_check")
    muestra_id = datos["prototype_id"]

    await db_session.execute(
        text("UPDATE prototypes SET status = 'STARTED', started_at = now() WHERE id = :id"),
        {"id": muestra_id},
    )
    await db_session.commit()

    db_session.expire_all()
    fila = await db_session.get(Prototype, muestra_id)
    assert fila is not None
    assert fila.quotation_id is None
    assert fila.prototype_quotation_id is not None
    assert fila.stock_location_id is not None

    # Y sin almacen la misma fila no pasa.
    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.execute(
            text("UPDATE prototypes SET stock_location_id = NULL WHERE id = :id"),
            {"id": muestra_id},
        )
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_el_modelo_y_la_migracion_0024_dicen_exactamente_lo_mismo(
    db_session: AsyncSession,
) -> None:
    """MODEL_MIGRATION_PARITY: PASS.

    La base de pruebas se crea con `create_all`, asi que sin esta comparacion
    la 0024 podria escribir una expresion y el modelo otra, y nadie se
    enteraria hasta produccion. Que es como empezo este defecto.
    """
    assert MIGRACION_0024.revision == "0024"
    assert MIGRACION_0024.down_revision == "0023"
    assert MIGRACION_0024.ORIGEN_AMPLIADO == STARTED_REQUIRES_ORIGIN

    # Y lo que PostgreSQL guarda de verdad menciona los dos origenes.
    definicion = (
        await db_session.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = 'ck_prototypes_started_requires_origin'"
            )
        )
    ).scalar_one()
    # Se busca la disyuncion entera, no cada mitad: «quotation_id IS NOT NULL»
    # es subcadena de «prototype_quotation_id IS NOT NULL», asi que buscarlas
    # por separado pasaria incluso con la restriccion vieja.
    assert "(quotation_id IS NOT NULL) OR (prototype_quotation_id IS NOT NULL)" in definicion
    assert "(stock_location_id IS NOT NULL)" in definicion


# ---------------------------------------------------------------------------
# G — el downgrade avisa en vez de romperse
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_volver_a_0023_con_muestras_de_cpr_arrancadas_aborta_diciendo_por_que(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DOWNGRADE_GUARD: PASS.

    Se ejecuta la guarda REAL de la 0024, leida del fichero, para que la prueba
    no acabe comprobando una copia suya. Sin ella, volver a 0023 dejaria filas
    que la propia restriccion prohibe y PostgreSQL se quejaria de algo que no
    explica nada.
    """
    datos = await _muestra_de_cpr(api, admin_csrf, db_session, "_downgrade")
    arrancada = await api.post(
        f"{PROTOTYPES}/{datos['prototype_id']}/start", headers=head(admin_csrf)
    )
    assert arrancada.status_code == 200, arrancada.text

    bloque = GUARDA_DOWNGRADE

    with pytest.raises(DBAPIError) as fallo:
        await db_session.execute(text(bloque))
    assert "0024 downgrade bloqueado" in str(fallo.value)
    await db_session.rollback()

    # Y con la tabla limpia de ese caso, la guarda deja pasar.
    await db_session.execute(
        text("UPDATE prototypes SET status = 'CREATED', started_at = NULL WHERE id = :id"),
        {"id": datos["prototype_id"]},
    )
    await db_session.commit()
    await db_session.execute(text(bloque))
    await db_session.commit()


@pytest.mark.asyncio
async def test_el_conteo_de_movimientos_no_se_mueve_por_mirar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Leer la muestra y su readiness no gasta nada. Se comprueba porque
    `evaluate_readiness` toma cerrojos sobre los saldos cuando se le pide."""
    datos = await _muestra_de_cpr(api, admin_csrf, db_session, "_mirar")
    antes = await db_session.scalar(select(func.count()).select_from(StockMovement))

    for _ in range(3):
        detalle = await api.get(f"{PROTOTYPES}/{datos['prototype_id']}", headers=head(admin_csrf))
        assert detalle.status_code == 200

    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(StockMovement)) == antes
