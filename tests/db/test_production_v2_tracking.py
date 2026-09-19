"""Fase 010I, bloque C — estados, cierre y seguimiento de una orden V2, contra PostgreSQL.

Lo que se fija:

- decision D3: FINALIZAR no es «cero consumos, no»; es «cada clase de material
  inventariable que la cotizacion planifico tiene al menos un consumo real».
  Caso A: pide pasta y no hay consumo -> no finaliza. Caso B: no pide nada
  inventariable -> finaliza sin consumos;
- decision D4: la quema real es una nota estructurada del seguimiento —horno,
  tipo y cuando—, no una tabla de quemas por orden;
- el seguimiento reune estados, consumos, notas y quemas, en orden y con autor;
- las notas no se editan, no se duplican por un doble clic y no tocan stock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.firings import Kiln
from app.models.inventory import StockMovement
from app.models.masters import Product, ProductType
from app.models.production import ProductionOrderNote
from app.models.quoter_v2 import V2QuotationProduct
from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate
from tests.db.test_production_v2_consumption import consumir, orden_con_existencia
from tests.db.test_production_v2_origin import ORDERS
from tests.db.test_quoter_v2_lifecycle_api import h

KILNS = "/api/v1/kilns"


# ---------------------------------------------------------------------------
# Apoyo
# ---------------------------------------------------------------------------
async def arrancar(api: httpx.AsyncClient, csrf: str, order_id: int) -> None:
    r = await api.post(f"{ORDERS}/{order_id}/start", headers=h(csrf))
    assert r.status_code == 200, r.text


async def finalizar(api: httpx.AsyncClient, csrf: str, order_id: int) -> httpx.Response:
    return await api.post(f"{ORDERS}/{order_id}/complete", headers=h(csrf))


async def horno(api: httpx.AsyncClient, csrf: str, nombre: str = "Horno grande") -> int:
    r = await api.post(
        KILNS, json={"name": nombre, "capacity_volume_cm3": "90000"}, headers=h(csrf)
    )
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def anotar(
    api: httpx.AsyncClient, csrf: str, order_id: int, *, key: str, **datos: Any
) -> httpx.Response:
    """Como la pantalla: la fecha va siempre, propuesta en «ahora»."""
    datos.setdefault("occurred_at", datetime.now(UTC).isoformat())
    return await api.post(
        f"{ORDERS}/{order_id}/notes",
        json={"idempotency_key": key, **datos},
        headers=h(csrf),
    )


async def pide_esmalte(db: AsyncSession, quotation_id: int, material_id: int) -> None:
    """La cotizacion ya emitida pasa a pedir esmalte en todas sus piezas.

    Se escribe directo en la base: el ciclo de vida V2 no deja cambiar una
    cotizacion emitida, y lo que se prueba aqui es la lectura que hace el cierre.
    """
    await db.execute(
        update(V2QuotationProduct)
        .where(V2QuotationProduct.v2_quotation_id == quotation_id)
        .values(requires_glaze=True, glaze_material_id=material_id)
    )
    await db.commit()


async def pendientes(api: httpx.AsyncClient, order_id: int) -> list[str]:
    r = await api.get(f"{ORDERS}/{order_id}")
    assert r.status_code == 200, r.text
    return list(r.json()["pending_consumption_kinds"])


# ---------------------------------------------------------------------------
# D3 — finalizar con material real
# ---------------------------------------------------------------------------
class TestFinalizarConMaterialReal:
    async def test_caso_a_pide_pasta_y_sin_consumo_no_finaliza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        await arrancar(api, admin_csrf, datos["order_id"])
        assert await pendientes(api, datos["order_id"]) == ["BODY"]

        r = await finalizar(api, admin_csrf, datos["order_id"])

        assert r.status_code == 409, r.text
        error = r.json()["error"]
        assert error["code"] == "PRODUCTION_ORDER_CONSUMPTION_MISSING"
        assert error["details"] == [{"kind": "BODY"}]
        orden = await api.get(f"{ORDERS}/{datos['order_id']}")
        assert orden.json()["status"] == "STARTED"
        assert orden.json()["completed_at"] is None

    async def test_caso_a_con_el_consumo_real_ya_finaliza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        await arrancar(api, admin_csrf, datos["order_id"])
        r = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="120",
            key="pasta-para-cerrar",
        )
        assert r.status_code == 201, r.text
        assert await pendientes(api, datos["order_id"]) == []

        cerrada = await finalizar(api, admin_csrf, datos["order_id"])

        assert cerrada.status_code == 200, cerrada.text
        assert cerrada.json()["status"] == "COMPLETED"
        assert cerrada.json()["pending_consumption_kinds"] == []

    async def test_caso_b_sin_material_inventariable_finaliza_sin_consumos(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """La cotizacion planifico un material que NO se inventaria.

        La V2 exige pasta para confirmar, asi que el caso real de «no requiere
        material inventariable» es que ese material sea un servicio: un
        modelado encargado fuera, una pieza que el cliente trae. Exigir su
        consumo seria exigir un movimiento de stock que no puede existir.
        """
        datos = await orden_con_existencia(api, admin_csrf, existencia="0")
        await db_session.execute(
            update(Product)
            .where(Product.id == datos["pasta_id"])
            .values(product_type=ProductType.SERVICE)
        )
        await db_session.commit()
        movs_antes = int(
            await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0
        )
        await arrancar(api, admin_csrf, datos["order_id"])
        assert await pendientes(api, datos["order_id"]) == []

        cerrada = await finalizar(api, admin_csrf, datos["order_id"])

        assert cerrada.status_code == 200, cerrada.text
        assert cerrada.json()["status"] == "COMPLETED"
        consumos = await api.get(f"{ORDERS}/{datos['order_id']}/consumptions")
        assert consumos.json()["total"] == 0
        db_session.expire_all()
        assert (
            int(await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0)
            == movs_antes
        )

    async def test_si_pide_esmalte_hace_falta_tambien_un_consumo_de_esmalte(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        await pide_esmalte(db_session, datos["id"], datos["pasta_id"])
        await arrancar(api, admin_csrf, datos["order_id"])
        assert await pendientes(api, datos["order_id"]) == ["BODY", "GLAZE"]

        pasta = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="100",
            key="solo-la-pasta",
        )
        assert pasta.status_code == 201, pasta.text
        r = await finalizar(api, admin_csrf, datos["order_id"])
        assert r.status_code == 409, r.text
        assert r.json()["error"]["details"] == [{"kind": "GLAZE"}]

        # Otro material que el cotizado vale: se exige la CLASE, no el producto.
        esmalte = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="15",
            key="y-el-esmalte",
            kind="GLAZE",
        )
        assert esmalte.status_code == 201, esmalte.text
        cerrada = await finalizar(api, admin_csrf, datos["order_id"])
        assert cerrada.status_code == 200, cerrada.text

    async def test_un_consumo_de_otra_clase_no_cubre_la_pasta(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        await arrancar(api, admin_csrf, datos["order_id"])
        otro = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="5",
            key="un-aditivo-cualquiera",
            kind="OTHER",
        )
        assert otro.status_code == 201, otro.text

        r = await finalizar(api, admin_csrf, datos["order_id"])

        assert r.status_code == 409, r.text
        assert r.json()["error"]["details"] == [{"kind": "BODY"}]

    async def test_una_orden_en_inicio_sigue_sin_poder_finalizar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El orden de las reglas no cambia: primero el estado, despues el material."""
        datos = await orden_con_existencia(api, admin_csrf)

        r = await finalizar(api, admin_csrf, datos["order_id"])

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_ORDER_NOT_COMPLETABLE"

    async def test_finalizar_dos_veces_no_falla_ni_vuelve_a_exigir(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="50",
            key="pasta-de-la-doble",
        )
        await arrancar(api, admin_csrf, datos["order_id"])
        primera = await finalizar(api, admin_csrf, datos["order_id"])
        segunda = await finalizar(api, admin_csrf, datos["order_id"])

        assert primera.status_code == 200, primera.text
        assert segunda.status_code == 200, segunda.text
        assert segunda.json()["completed_at"] == primera.json()["completed_at"]


# ---------------------------------------------------------------------------
# D4 — notas y quemas
# ---------------------------------------------------------------------------
class TestNotasYQuemas:
    async def test_una_nota_en_inicio(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        datos = await orden_con_existencia(api, admin_csrf)

        r = await anotar(
            api,
            admin_csrf,
            datos["order_id"],
            key="nota-inicio-010i",
            kind="NOTE",
            body="  El cliente pide el borde mas grueso  ",
        )

        assert r.status_code == 201, r.text
        nota = r.json()
        assert nota["kind"] == "NOTE"
        assert nota["body"] == "El cliente pide el borde mas grueso"
        assert nota["kiln_id"] is None and nota["firing_type"] is None
        assert nota["created_by_name"]

    async def test_una_quema_guarda_horno_tipo_y_cuando(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        await arrancar(api, admin_csrf, datos["order_id"])
        kiln_id = await horno(api, admin_csrf, "Horno del patio")
        # Entre la creacion de la orden y ahora: la quema se anota despues de
        # ocurrir, pero no puede ser anterior a la orden.
        creada = datetime.fromisoformat(
            (await api.get(f"{ORDERS}/{datos['order_id']}")).json()["created_at"]
        )
        datos_ahora = datetime.now(UTC)
        cuando = creada + (datos_ahora - creada) / 2

        r = await anotar(
            api,
            admin_csrf,
            datos["order_id"],
            key="quema-alta-010i",
            kind="FIRING_NOTE",
            kiln_id=kiln_id,
            firing_type="HIGH",
            occurred_at=cuando.isoformat(),
            body="Hornada compartida con otras dos ordenes",
        )

        assert r.status_code == 201, r.text
        quema = r.json()
        assert quema["kind"] == "FIRING_NOTE"
        assert quema["kiln_id"] == kiln_id
        assert quema["kiln_name"] == "Horno del patio"
        assert quema["firing_type"] == "HIGH"
        assert datetime.fromisoformat(quema["occurred_at"]) == cuando
        assert datetime.fromisoformat(quema["created_at"]) >= datos_ahora - timedelta(minutes=1)

        # El nombre queda copiado: renombrar el horno no reescribe la historia.
        await db_session.execute(update(Kiln).where(Kiln.id == kiln_id).values(name="Otro"))
        await db_session.commit()
        linea = await api.get(f"{ORDERS}/{datos['order_id']}/timeline")
        quemas = [e for e in linea.json()["items"] if e["type"] == "FIRING_NOTE"]
        assert quemas[0]["note"]["kiln_name"] == "Horno del patio"

    async def test_una_quema_no_mueve_inventario(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        await arrancar(api, admin_csrf, datos["order_id"])
        kiln_id = await horno(api, admin_csrf)
        antes = int(await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0)

        r = await anotar(
            api,
            admin_csrf,
            datos["order_id"],
            key="quema-sin-stock",
            kind="FIRING_NOTE",
            kiln_id=kiln_id,
            firing_type="LOW",
        )

        assert r.status_code == 201, r.text
        db_session.expire_all()
        despues = int(await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0)
        assert despues == antes

    async def test_no_hay_quema_antes_de_arrancar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        kiln_id = await horno(api, admin_csrf)

        r = await anotar(
            api,
            admin_csrf,
            datos["order_id"],
            key="quema-prematura",
            kind="FIRING_NOTE",
            kiln_id=kiln_id,
            firing_type="LOW",
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_NOTE_NOT_ALLOWED"

    async def test_una_orden_finalizada_admite_notas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="10",
            key="pasta-y-despues-nota",
        )
        await arrancar(api, admin_csrf, datos["order_id"])
        assert (await finalizar(api, admin_csrf, datos["order_id"])).status_code == 200

        r = await anotar(
            api,
            admin_csrf,
            datos["order_id"],
            key="nota-tras-cerrar",
            kind="NOTE",
            body="Entregado en tienda",
        )

        assert r.status_code == 201, r.text

    async def test_una_orden_anulada_no_admite_notas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        assert (
            await api.post(f"{ORDERS}/{datos['order_id']}/cancel", headers=h(admin_csrf))
        ).status_code == 200

        r = await anotar(
            api, admin_csrf, datos["order_id"], key="nota-anulada", kind="NOTE", body="Tarde"
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_NOTE_NOT_ALLOWED"

    async def test_el_reintento_devuelve_la_misma_nota(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        payload = {
            "kind": "NOTE",
            "body": "Revisar asas",
            "occurred_at": datetime.now(UTC).isoformat(),
        }

        primera = await anotar(api, admin_csrf, datos["order_id"], key="nota-doble-clic", **payload)
        segunda = await anotar(api, admin_csrf, datos["order_id"], key="nota-doble-clic", **payload)

        assert primera.status_code == 201, primera.text
        assert segunda.status_code == 200, segunda.text
        assert segunda.json()["id"] == primera.json()["id"]
        db_session.expire_all()
        total = await db_session.scalar(
            select(func.count())
            .select_from(ProductionOrderNote)
            .where(ProductionOrderNote.idempotency_key == "nota-doble-clic")
        )
        assert total == 1

    async def test_la_misma_clave_con_otro_texto_es_un_409(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        await anotar(api, admin_csrf, datos["order_id"], key="nota-reusada", kind="NOTE", body="A")

        r = await anotar(
            api, admin_csrf, datos["order_id"], key="nota-reusada", kind="NOTE", body="B"
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_NOTE_KEY_REUSED"

    async def test_la_misma_clave_con_otra_fecha_es_un_409(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Hallazgo de Copilot en el bloque C: la fecha tambien distingue una nota."""
        datos = await orden_con_existencia(api, admin_csrf)
        ahora = datetime.now(UTC)
        primera = await anotar(
            api,
            admin_csrf,
            datos["order_id"],
            key="nota-fechada",
            kind="NOTE",
            body="Secado",
            occurred_at=ahora.isoformat(),
        )
        assert primera.status_code == 201, primera.text

        r = await anotar(
            api,
            admin_csrf,
            datos["order_id"],
            key="nota-fechada",
            kind="NOTE",
            body="Secado",
            occurred_at=(ahora + timedelta(seconds=30)).isoformat(),
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_NOTE_KEY_REUSED"

    async def test_la_fecha_es_obligatoria(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        datos = await orden_con_existencia(api, admin_csrf)

        r = await api.post(
            f"{ORDERS}/{datos['order_id']}/notes",
            json={"idempotency_key": "nota-sin-fecha", "kind": "NOTE", "body": "Sin fecha"},
            headers=h(admin_csrf),
        )

        assert r.status_code == 422, r.text

    async def test_un_horno_inactivo_o_inexistente(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        await arrancar(api, admin_csrf, datos["order_id"])
        kiln_id = await horno(api, admin_csrf)
        await db_session.execute(update(Kiln).where(Kiln.id == kiln_id).values(active=False))
        await db_session.commit()

        for key, kid in (("horno-apagado", kiln_id), ("horno-fantasma", 999_999)):
            r = await anotar(
                api,
                admin_csrf,
                datos["order_id"],
                key=key,
                kind="FIRING_NOTE",
                kiln_id=kid,
                firing_type="LOW",
            )
            assert r.status_code == 422, r.text
            assert r.json()["error"]["code"] == "PRODUCTION_NOTE_KILN_INVALID"

    async def test_una_fecha_del_futuro_o_de_antes_de_la_orden(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        ahora = datetime.now(UTC)

        for key, cuando in (
            ("nota-del-futuro", ahora + timedelta(days=1)),
            ("nota-del-pasado", ahora - timedelta(days=30)),
        ):
            r = await anotar(
                api,
                admin_csrf,
                datos["order_id"],
                key=key,
                kind="NOTE",
                body="Fecha imposible",
                occurred_at=cuando.isoformat(),
            )
            assert r.status_code == 422, r.text
            assert r.json()["error"]["code"] == "PRODUCTION_NOTE_OCCURRED_AT_INVALID"

    async def test_una_nota_y_una_quema_no_se_hacen_pasar_la_una_por_la_otra(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        casos = (
            {"kind": "NOTE", "body": "   "},
            {"kind": "NOTE", "body": "Con horno", "kiln_id": 1},
            {"kind": "FIRING_NOTE", "firing_type": "LOW"},
            {"kind": "FIRING_NOTE", "kiln_id": 1},
            {"kind": "FIRING_NOTE", "kiln_id": 1, "firing_type": "MEDIUM"},
        )
        for i, caso in enumerate(casos):
            r = await anotar(api, admin_csrf, datos["order_id"], key=f"nota-invalida-{i}", **caso)
            assert r.status_code == 422, (caso, r.text)

    async def test_la_base_tambien_lo_impide(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """El CHECK no depende del esquema de la API: una quema sin horno no entra."""
        datos = await orden_con_existencia(api, admin_csrf)

        with pytest.raises(IntegrityError) as error:
            await db_session.execute(
                text(
                    "INSERT INTO production_order_notes"
                    " (production_order_id, kind, body, occurred_at, idempotency_key)"
                    " VALUES (:oid, 'FIRING_NOTE', 'sin horno', now(), 'clave-invalida-bd')"
                ),
                {"oid": datos["order_id"]},
            )
        await db_session.rollback()
        assert "ck_production_order_notes_kind_fields_consistent" in str(error.value)

    async def test_una_orden_legacy_no_admite_notas(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        from tests.db.test_production_orders_api import confirmada_y_pagada, escenario
        from tests.db.test_production_orders_api import crear_orden as crear_orden_legacy

        datos = await escenario(api, admin_csrf, db_session, suffix="_nota_legacy")
        confirmada = await confirmada_y_pagada(api, admin_csrf, datos["quotation"])
        orden = await crear_orden_legacy(
            api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
        )
        assert orden.status_code == 201, orden.text

        r = await anotar(
            api, admin_csrf, int(orden.json()["id"]), key="nota-legacy", kind="NOTE", body="x"
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_ORDER_NOT_V2"


# ---------------------------------------------------------------------------
# El seguimiento
# ---------------------------------------------------------------------------
class TestSeguimiento:
    async def test_reune_estados_consumos_notas_y_quemas_en_orden(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        oid = datos["order_id"]
        await anotar(api, admin_csrf, oid, key="seguimiento-nota-1", kind="NOTE", body="Arrancamos")
        await arrancar(api, admin_csrf, oid)
        consumo = await consumir(
            api, admin_csrf, oid, product_id=datos["pasta_id"], quantity="80", key="seguim-pasta"
        )
        assert consumo.status_code == 201, consumo.text
        kiln_id = await horno(api, admin_csrf)
        quema = await anotar(
            api,
            admin_csrf,
            oid,
            key="seguimiento-quema",
            kind="FIRING_NOTE",
            kiln_id=kiln_id,
            firing_type="LOW",
        )
        assert quema.status_code == 201, quema.text
        assert (await finalizar(api, admin_csrf, oid)).status_code == 200

        r = await api.get(f"{ORDERS}/{oid}/timeline")

        assert r.status_code == 200, r.text
        items = r.json()["items"]
        resumen = [(e["type"], e["status"]) for e in items]
        assert resumen == [
            ("STATUS", "CREATED"),
            ("NOTE", None),
            ("STATUS", "STARTED"),
            ("CONSUMPTION", None),
            ("FIRING_NOTE", None),
            ("STATUS", "COMPLETED"),
        ]
        instantes = [datetime.fromisoformat(e["occurred_at"]) for e in items]
        assert instantes == sorted(instantes)
        assert all(e["actor_name"] for e in items)
        # Solo viaja el detalle de su tipo, y el consumo sigue sin ensenar costos.
        consumo_evt = items[3]
        assert consumo_evt["note"] is None
        assert consumo_evt["consumption"]["quantity"] in ("80", "80.000000000000")
        assert not any("cost" in k for k in consumo_evt["consumption"])

    async def test_una_orden_anulada_muestra_su_anulacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        assert (
            await api.post(f"{ORDERS}/{datos['order_id']}/cancel", headers=h(admin_csrf))
        ).status_code == 200

        r = await api.get(f"{ORDERS}/{datos['order_id']}/timeline")

        assert [e["status"] for e in r.json()["items"]] == ["CREATED", "CANCELLED"]

    async def test_una_orden_que_no_existe(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        r = await api.get(f"{ORDERS}/999999/timeline")
        assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Permisos
# ---------------------------------------------------------------------------
class TestPermisos:
    async def test_el_operador_anota_quema_y_finaliza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        kiln_id = await horno(api, admin_csrf)
        oid = datos["order_id"]
        operador = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)

        await arrancar(api, operador, oid)
        consumo = await consumir(
            api, operador, oid, product_id=datos["pasta_id"], quantity="30", key="operador-pasta"
        )
        assert consumo.status_code == 201, consumo.text
        nota = await anotar(api, operador, oid, key="operador-nota", kind="NOTE", body="Listo")
        quema = await anotar(
            api,
            operador,
            oid,
            key="operador-quema",
            kind="FIRING_NOTE",
            kiln_id=kiln_id,
            firing_type="HIGH",
        )
        cerrada = await finalizar(api, operador, oid)

        assert nota.status_code == 201, nota.text
        assert quema.status_code == 201, quema.text
        assert cerrada.status_code == 200, cerrada.text
        linea = await api.get(f"{ORDERS}/{oid}/timeline")
        assert linea.status_code == 200, linea.text
