"""Fase 010I, bloque A — la orden de produccion de una cotizacion V2, contra PostgreSQL.

Lo que se fija aqui:

- una cotizacion V2 enviada a produccion crea su orden, en INICIO (CREATED), sin
  tocar un gramo de inventario y sin tocar la cotizacion;
- una cotizacion V2 tiene como mucho UNA orden: pedirla otra vez, con otra
  clave o con cinco peticiones a la vez, devuelve la misma;
- sin «Enviar a produccion» no hay orden: no nace un segundo camino a la fabrica;
- la orden se presenta con SU origen, no como una cotizacion Legacy;
- el operador del taller puede crearla, igual que una orden Legacy.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockMovement
from app.models.production import ProductionOrder
from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate
from tests.db.test_quoter_v2_lifecycle_api import cotizacion_completa, emitir, h

V2 = "/api/v1/quotations-v2"
ORDERS = "/api/v1/production-orders"
LOCATIONS = "/api/v1/inventory/locations"


# ---------------------------------------------------------------------------
# Apoyo
# ---------------------------------------------------------------------------
async def enviada_a_produccion(api: httpx.AsyncClient, csrf: str) -> dict[str, Any]:
    """Una cotizacion V2 completa, emitida y enviada a produccion, y un almacen."""
    datos = await cotizacion_completa(api, csrf)
    await emitir(api, csrf, datos["id"])
    enviada = await api.post(f"{V2}/{datos['id']}/send-to-production", headers=h(csrf))
    assert enviada.status_code in (200, 201), enviada.text
    almacen = await api.post(LOCATIONS, json={"name": f"Taller V2 {datos['id']}"}, headers=h(csrf))
    assert almacen.status_code == 201, almacen.text
    return {**datos, "location_id": int(almacen.json()["id"])}


async def otra_enviada_a_produccion(
    api: httpx.AsyncClient, csrf: str, primera: dict[str, Any]
) -> dict[str, Any]:
    """Una SEGUNDA cotizacion V2 enviada a produccion, con los maestros de la primera.

    `cotizacion_completa` siembra los maestros con nombres fijos, asi que no se
    puede llamar dos veces en la misma prueba: la segunda choca con la categoria
    que dejo la primera. Aqui se reutilizan pasta, trabajador, tecnica y cliente.
    """
    creada = await api.post(
        V2,
        json={"name": "Segundo pedido 010I", "customer_id": primera["customer_id"]},
        headers=h(csrf),
    )
    assert creada.status_code == 201, creada.text
    qid = int(creada.json()["id"])
    linea = await api.post(
        f"{V2}/{qid}/products",
        json={
            "product_name": "Taza de encargo",
            "quantity": 20,
            "length_cm": "9",
            "width_cm": "9",
            "height_cm": "10",
            "body_material_id": primera["pasta_id"],
            "body_unit_weight": "300",
        },
        headers=h(csrf),
    )
    assert linea.status_code == 201, linea.text
    proceso = await api.post(
        f"{V2}/{qid}/processes",
        json={
            "v2_quotation_product_id": int(linea.json()["id"]),
            "technique_id": primera["technique_id"],
        },
        headers=h(csrf),
    )
    assert proceso.status_code == 201, proceso.text
    asignada = await api.post(
        f"{V2}/{qid}/processes/{int(proceso.json()['id'])}/assign",
        json={"worker_id": primera["worker_id"]},
        headers=h(csrf),
    )
    assert asignada.status_code == 200, asignada.text
    plan = await api.put(f"{V2}/{qid}/planning", json={"effective_work_days": 2}, headers=h(csrf))
    assert plan.status_code == 200, plan.text
    await emitir(api, csrf, qid)
    enviada = await api.post(f"{V2}/{qid}/send-to-production", headers=h(csrf))
    assert enviada.status_code in (200, 201), enviada.text
    return {"id": qid, "location_id": primera["location_id"]}


async def crear_orden(
    api: httpx.AsyncClient,
    csrf: str,
    *,
    v2_quotation_id: int,
    location_id: int,
    idempotency_key: str | None = None,
) -> httpx.Response:
    cuerpo: dict[str, Any] = {"v2_quotation_id": v2_quotation_id, "stock_location_id": location_id}
    if idempotency_key is not None:
        cuerpo["idempotency_key"] = idempotency_key
    return await api.post(ORDERS, json=cuerpo, headers=h(csrf))


async def movimientos(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(StockMovement)) or 0)


async def ordenes_de(db: AsyncSession, orden_id: int) -> int:
    orden = await db.get(ProductionOrder, orden_id)
    assert orden is not None
    return int(
        await db.scalar(
            select(func.count())
            .select_from(ProductionOrder)
            .where(ProductionOrder.v2_handoff_id == orden.v2_handoff_id)
        )
        or 0
    )


# ---------------------------------------------------------------------------
# Crear
# ---------------------------------------------------------------------------
class TestCrearOrdenV2:
    async def test_crea_la_orden_en_inicio_con_su_origen_y_sin_tocar_inventario(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await enviada_a_produccion(api, admin_csrf)
        cotizacion = (await api.get(f"{V2}/{datos['id']}")).json()
        antes = await movimientos(db_session)

        r = await crear_orden(
            api, admin_csrf, v2_quotation_id=datos["id"], location_id=datos["location_id"]
        )

        assert r.status_code == 201, r.text
        orden = r.json()
        assert orden["status"] == "CREATED"
        assert orden["origin_type"] == "V2_QUOTATION"
        assert orden["v2_quotation_id"] == datos["id"]
        assert orden["v2_quotation_code"] == cotizacion["code"]
        assert orden["quotation_customer_name"] == cotizacion["customer_name"]
        # Ni un campo de los otros dos origenes: no se presenta como Legacy.
        for campo in ("quotation_id", "quotation_code", "prototype_id", "prototype_code"):
            assert orden[campo] is None, campo
        assert orden["stock_location_id"] == datos["location_id"]
        assert orden["started_at"] is None
        assert orden["lines"] == []
        # Arrancar una orden V2 no descuenta nada, asi que no hay nada que la bloquee.
        assert orden["readiness"] == {"ready": True, "issues": []}
        assert len(orden["qr_token"]) >= 32
        assert await movimientos(db_session) == antes

    async def test_crearla_no_cambia_la_cotizacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Ni la cabecera ni el precio de la V2 cambian en un solo campo."""
        datos = await enviada_a_produccion(api, admin_csrf)
        cabecera = (await api.get(f"{V2}/{datos['id']}")).json()
        precio = (await api.get(f"{V2}/{datos['id']}/pricing")).json()

        r = await crear_orden(
            api, admin_csrf, v2_quotation_id=datos["id"], location_id=datos["location_id"]
        )
        assert r.status_code == 201, r.text

        assert (await api.get(f"{V2}/{datos['id']}")).json() == cabecera
        assert (await api.get(f"{V2}/{datos['id']}/pricing")).json() == precio


# ---------------------------------------------------------------------------
# Una cotizacion V2, una orden
# ---------------------------------------------------------------------------
class TestUnaSolaOrden:
    async def test_pedirla_otra_vez_devuelve_la_misma(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await enviada_a_produccion(api, admin_csrf)
        primera = await crear_orden(
            api, admin_csrf, v2_quotation_id=datos["id"], location_id=datos["location_id"]
        )
        segunda = await crear_orden(
            api, admin_csrf, v2_quotation_id=datos["id"], location_id=datos["location_id"]
        )

        assert primera.status_code == 201, primera.text
        # 200 y no 201: el cliente sabe que no acaba de crear nada.
        assert segunda.status_code == 200, segunda.text
        assert segunda.json()["id"] == primera.json()["id"]
        assert await ordenes_de(db_session, primera.json()["id"]) == 1

    async def test_con_otra_clave_sigue_siendo_la_misma(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """La unicidad del ORIGEN manda sobre la clave de idempotencia."""
        datos = await enviada_a_produccion(api, admin_csrf)
        primera = await crear_orden(
            api,
            admin_csrf,
            v2_quotation_id=datos["id"],
            location_id=datos["location_id"],
            idempotency_key="clave-uno-010i",
        )
        segunda = await crear_orden(
            api,
            admin_csrf,
            v2_quotation_id=datos["id"],
            location_id=datos["location_id"],
            idempotency_key="clave-dos-010i",
        )

        assert primera.status_code == 201, primera.text
        assert segunda.status_code == 200, segunda.text
        assert segunda.json()["id"] == primera.json()["id"]
        assert await ordenes_de(db_session, primera.json()["id"]) == 1

    async def test_cinco_peticiones_a_la_vez_dejan_una_sola_orden(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """A la vez de verdad: en serie solo probaria que la segunda lee la primera."""
        datos = await enviada_a_produccion(api, admin_csrf)
        respuestas = await asyncio.gather(
            *(
                crear_orden(
                    api,
                    admin_csrf,
                    v2_quotation_id=datos["id"],
                    location_id=datos["location_id"],
                    idempotency_key=f"clave-concurrente-{indice}",
                )
                for indice in range(5)
            )
        )

        assert all(r.status_code in (200, 201) for r in respuestas), [r.text for r in respuestas]
        assert sorted(r.status_code for r in respuestas) == [200, 200, 200, 200, 201]
        assert len({r.json()["id"] for r in respuestas}) == 1
        assert await ordenes_de(db_session, respuestas[0].json()["id"]) == 1

    async def test_la_clave_de_otra_orden_no_devuelve_una_orden_ajena(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        primera = await enviada_a_produccion(api, admin_csrf)
        otra = await otra_enviada_a_produccion(api, admin_csrf, primera)
        creada = await crear_orden(
            api,
            admin_csrf,
            v2_quotation_id=primera["id"],
            location_id=primera["location_id"],
            idempotency_key="clave-reutilizada-010i",
        )
        assert creada.status_code == 201, creada.text

        r = await crear_orden(
            api,
            admin_csrf,
            v2_quotation_id=otra["id"],
            location_id=otra["location_id"],
            idempotency_key="clave-reutilizada-010i",
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_ORDER_IDEMPOTENCY_KEY_REUSED"


# ---------------------------------------------------------------------------
# Lo que no puede originar una orden
# ---------------------------------------------------------------------------
class TestRechazos:
    async def test_sin_enviar_a_produccion_no_hay_orden(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """No se crea el puente de 010H por la puerta de atras."""
        datos = await cotizacion_completa(api, admin_csrf)
        await emitir(api, admin_csrf, datos["id"])
        almacen = await api.post(
            LOCATIONS, json={"name": "Taller sin enviar"}, headers=h(admin_csrf)
        )

        r = await crear_orden(
            api, admin_csrf, v2_quotation_id=datos["id"], location_id=int(almacen.json()["id"])
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_ORDER_V2_NOT_SENT"
        assert (
            int(await db_session.scalar(select(func.count()).select_from(ProductionOrder)) or 0)
            == 0
        )

    async def test_una_cotizacion_v2_que_no_existe(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        almacen = await api.post(LOCATIONS, json={"name": "Taller fantasma"}, headers=h(admin_csrf))
        r = await crear_orden(
            api, admin_csrf, v2_quotation_id=999_999, location_id=int(almacen.json()["id"])
        )
        assert r.status_code == 404, r.text

    async def test_un_almacen_que_no_existe(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        datos = await enviada_a_produccion(api, admin_csrf)
        r = await crear_orden(api, admin_csrf, v2_quotation_id=datos["id"], location_id=999_999)
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_ORDER_LOCATION_INVALID"

    async def test_dos_origenes_a_la_vez_es_un_422(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        r = await api.post(
            ORDERS,
            json={"v2_quotation_id": 1, "quotation_id": 1, "stock_location_id": 1},
            headers=h(admin_csrf),
        )
        assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Arrancar
# ---------------------------------------------------------------------------
class TestArrancarOrdenV2:
    async def test_arranca_sin_preguntar_por_un_cobro_legacy_y_sin_tocar_inventario(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Hallazgo de Copilot en el bloque A: antes caia en el guardia Legacy.

        Preguntaba por el cobro de una cotizacion Legacy que una orden V2 no
        tiene, y rechazaba SIEMPRE con «no pagada».
        """
        datos = await enviada_a_produccion(api, admin_csrf)
        orden = (
            await crear_orden(
                api, admin_csrf, v2_quotation_id=datos["id"], location_id=datos["location_id"]
            )
        ).json()
        antes = await movimientos(db_session)

        r = await api.post(f"{ORDERS}/{orden['id']}/start", headers=h(admin_csrf))

        assert r.status_code == 200, r.text
        arrancada = r.json()
        assert arrancada["status"] == "STARTED"
        assert arrancada["started_at"] is not None
        assert await movimientos(db_session) == antes

    async def test_arrancar_dos_veces_no_cambia_nada(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await enviada_a_produccion(api, admin_csrf)
        orden = (
            await crear_orden(
                api, admin_csrf, v2_quotation_id=datos["id"], location_id=datos["location_id"]
            )
        ).json()
        primera = await api.post(f"{ORDERS}/{orden['id']}/start", headers=h(admin_csrf))
        antes = await movimientos(db_session)

        segunda = await api.post(f"{ORDERS}/{orden['id']}/start", headers=h(admin_csrf))

        assert segunda.status_code == 200, segunda.text
        # `started_at` sigue diciendo cuando se arranco DE VERDAD.
        assert segunda.json()["started_at"] == primera.json()["started_at"]
        assert await movimientos(db_session) == antes

    async def test_el_operador_arranca_la_orden(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await enviada_a_produccion(api, admin_csrf)
        orden = (
            await crear_orden(
                api, admin_csrf, v2_quotation_id=datos["id"], location_id=datos["location_id"]
            )
        ).json()
        operario = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)

        r = await api.post(f"{ORDERS}/{orden['id']}/start", headers=h(operario))

        assert r.status_code == 200, r.text
        assert r.json()["status"] == "STARTED"


# ---------------------------------------------------------------------------
# Permisos y lectura
# ---------------------------------------------------------------------------
class TestPermisosYLectura:
    async def test_el_operador_crea_la_orden_como_una_legacy(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Crear la orden es de taller (WorkshopUserDep); enviar a produccion no."""
        datos = await enviada_a_produccion(api, admin_csrf)
        operario = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)

        r = await crear_orden(
            api, operario, v2_quotation_id=datos["id"], location_id=datos["location_id"]
        )

        assert r.status_code == 201, r.text
        assert r.json()["origin_type"] == "V2_QUOTATION"

    async def test_la_lista_filtra_por_cotizacion_v2_y_no_la_confunde_con_legacy(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await enviada_a_produccion(api, admin_csrf)
        creada = (
            await crear_orden(
                api, admin_csrf, v2_quotation_id=datos["id"], location_id=datos["location_id"]
            )
        ).json()

        por_v2 = (await api.get(ORDERS, params={"v2_quotation_id": datos["id"]})).json()
        assert [fila["id"] for fila in por_v2["items"]] == [creada["id"]]
        assert por_v2["items"][0]["origin_type"] == "V2_QUOTATION"
        assert por_v2["items"][0]["v2_quotation_code"] == creada["v2_quotation_code"]

        # El mismo numero como cotizacion LEGACY no encuentra la orden V2.
        por_legacy = (await api.get(ORDERS, params={"quotation": datos["id"]})).json()
        assert creada["id"] not in [fila["id"] for fila in por_legacy["items"]]

    async def test_la_ficha_se_lee_con_su_origen(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await enviada_a_produccion(api, admin_csrf)
        creada = (
            await crear_orden(
                api, admin_csrf, v2_quotation_id=datos["id"], location_id=datos["location_id"]
            )
        ).json()

        ficha = (await api.get(f"{ORDERS}/{creada['id']}")).json()
        assert ficha["origin_type"] == "V2_QUOTATION"
        assert ficha["v2_quotation_id"] == datos["id"]
        assert ficha["quotation_id"] is None
