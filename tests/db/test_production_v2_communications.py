"""Fase 010I, bloque D — comunicaciones con el cliente, contra PostgreSQL.

Decision D2: el taller REGISTRA que aviso al cliente; el sistema no envia nada.
Lo que se fija:

- ADMIN y OPERATOR registran; sin sesion o sin CSRF, nada;
- el autor es quien tiene la sesion, y el formulario no puede decir otro;
- el mensaje guardado es el final, tal cual; vacio o de otro canal, no entra;
- aparece UNA vez en el seguimiento, como comunicacion y no como nota;
- no cambia estado, inventario ni cotizacion;
- vale tambien con la orden FINALIZADA o anulada;
- el doble clic no duplica, y dos avisos distintos son dos.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockBalance, StockMovement
from app.models.production import ProductionOrderCommunication
from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate
from tests.db.test_production_v2_consumption import consumir, orden_con_existencia
from tests.db.test_production_v2_origin import ORDERS
from tests.db.test_quoter_v2_lifecycle_api import h

V2 = "/api/v1/quotations-v2"

TEXTO_A = "Hola, sus piezas entraron hoy a quema. Le avisamos al sacarlas."


# ---------------------------------------------------------------------------
# Apoyo
# ---------------------------------------------------------------------------
async def registrar(
    api: httpx.AsyncClient,
    csrf: str | None,
    order_id: int,
    *,
    key: str,
    message: str = TEXTO_A,
    **extra: Any,
) -> httpx.Response:
    payload = {
        "channel": "WHATSAPP",
        "message": message,
        "sent_at": datetime.now(UTC).isoformat(),
        "idempotency_key": key,
        **extra,
    }
    return await api.post(
        f"{ORDERS}/{order_id}/communications",
        json=payload,
        headers=h(csrf) if csrf else {},
    )


async def avisos(db: AsyncSession, order_id: int) -> int:
    db.expire_all()
    return int(
        await db.scalar(
            select(func.count())
            .select_from(ProductionOrderCommunication)
            .where(ProductionOrderCommunication.production_order_id == order_id)
        )
        or 0
    )


async def foto_inventario(db: AsyncSession) -> tuple[int, list[str]]:
    db.expire_all()
    movimientos = int(await db.scalar(select(func.count()).select_from(StockMovement)) or 0)
    saldos = [
        format(q, "f")
        for q in (await db.scalars(select(StockBalance.quantity).order_by(StockBalance.id))).all()
    ]
    return movimientos, saldos


def de_la_timeline(respuesta: httpx.Response) -> list[dict[str, Any]]:
    assert respuesta.status_code == 200, respuesta.text
    return [e for e in respuesta.json()["items"] if e["type"] == "COMMUNICATION"]


# ---------------------------------------------------------------------------
# Registrar
# ---------------------------------------------------------------------------
class TestRegistrar:
    async def test_el_admin_registra_y_se_guarda_el_texto_final(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        cuando = datetime.now(UTC)
        # Retocado a mano a partir de una plantilla: se guarda ESTO, con sus
        # saltos de linea, y solo se recortan los extremos.
        final = "Hola Ana:\nsus 20 tazas ya estan en quema.\nSaludos, Greda"

        r = await registrar(
            api,
            admin_csrf,
            datos["order_id"],
            key="aviso-admin-010i",
            message=f"  {final}  ",
            sent_at=cuando.isoformat(),
        )

        assert r.status_code == 201, r.text
        aviso = r.json()
        assert aviso["channel"] == "WHATSAPP"
        assert aviso["message"] == final
        assert datetime.fromisoformat(aviso["sent_at"]) == cuando
        assert aviso["sent_by_name"]

    async def test_el_operador_registra(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        operario = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)

        r = await registrar(api, operario, datos["order_id"], key="aviso-operario")

        assert r.status_code == 201, r.text

    async def test_el_autor_es_el_de_la_sesion(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        operario = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
        yo = (await api.get("/api/v1/auth/me")).json()["user"]

        r = await registrar(api, operario, datos["order_id"], key="aviso-autor-real")

        assert r.status_code == 201, r.text
        db_session.expire_all()
        fila = await db_session.get(ProductionOrderCommunication, r.json()["id"])
        assert fila is not None
        assert str(fila.sent_by) == yo["id"]
        assert fila.sent_by_name == yo["display_name"] == r.json()["sent_by_name"]

    async def test_el_formulario_no_puede_atribuirselo_a_otro(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)

        for campo, valor in (
            ("sent_by", "00000000-0000-0000-0000-000000000001"),
            ("sent_by_name", "Juan"),
        ):
            r = await registrar(
                api,
                admin_csrf,
                datos["order_id"],
                key=f"aviso-suplantado-{campo}",
                **{campo: valor},
            )
            assert r.status_code == 422, r.text
        assert await avisos(db_session, datos["order_id"]) == 0


# ---------------------------------------------------------------------------
# Permisos
# ---------------------------------------------------------------------------
class TestPermisos:
    async def test_sin_sesion_no_se_registra(
        self,
        api: httpx.AsyncClient,
        api_app: Any,
        admin_csrf: str,
        db_session: AsyncSession,
    ) -> None:
        """Cliente NUEVO, como en `test_production_rbac`: borrarle la cookie al
        de la sesion no siempre la borra."""
        datos = await orden_con_existencia(api, admin_csrf)
        transport = httpx.ASGITransport(app=api_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anonimo:
            r = await registrar(anonimo, None, datos["order_id"], key="aviso-anonimo")

        assert r.status_code in (401, 403), r.text
        assert await avisos(db_session, datos["order_id"]) == 0

    async def test_sin_csrf_es_403_y_no_deja_nada(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)

        r = await registrar(api, None, datos["order_id"], key="aviso-sin-csrf")

        assert r.status_code == 403, r.text
        assert await avisos(db_session, datos["order_id"]) == 0


# ---------------------------------------------------------------------------
# Lo que no vale
# ---------------------------------------------------------------------------
class TestRechazos:
    async def test_un_mensaje_vacio(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        for i, texto in enumerate(("", "   ", "\n\t ")):
            r = await registrar(
                api, admin_csrf, datos["order_id"], key=f"aviso-vacio-{i}", message=texto
            )
            assert r.status_code == 422, r.text
        assert await avisos(db_session, datos["order_id"]) == 0

    async def test_un_mensaje_demasiado_largo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        r = await registrar(
            api, admin_csrf, datos["order_id"], key="aviso-largo", message="x" * 2001
        )
        assert r.status_code == 422, r.text

    async def test_un_canal_que_no_existe(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        for canal in ("EMAIL", "whatsapp", "SMS"):
            r = await registrar(
                api, admin_csrf, datos["order_id"], key=f"aviso-canal-{canal}", channel=canal
            )
            assert r.status_code == 422, (canal, r.text)

    async def test_la_fecha_es_obligatoria_y_con_zona(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        sin_fecha = await api.post(
            f"{ORDERS}/{datos['order_id']}/communications",
            json={"channel": "WHATSAPP", "message": "Hola", "idempotency_key": "aviso-sin-fecha"},
            headers=h(admin_csrf),
        )
        sin_zona = await registrar(
            api,
            admin_csrf,
            datos["order_id"],
            key="aviso-sin-zona",
            sent_at=datetime.now(UTC).replace(tzinfo=None).isoformat(),
        )
        assert sin_fecha.status_code == 422, sin_fecha.text
        assert sin_zona.status_code == 422, sin_zona.text

    async def test_una_fecha_del_futuro_o_de_antes_de_la_orden(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La misma ventana que las notas del seguimiento (bloque C)."""
        datos = await orden_con_existencia(api, admin_csrf)
        ahora = datetime.now(UTC)
        for key, cuando in (
            ("aviso-del-futuro", ahora + timedelta(hours=2)),
            ("aviso-del-pasado", ahora - timedelta(days=10)),
        ):
            r = await registrar(
                api, admin_csrf, datos["order_id"], key=key, sent_at=cuando.isoformat()
            )
            assert r.status_code == 422, r.text
            assert r.json()["error"]["code"] == "PRODUCTION_COMMUNICATION_SENT_AT_INVALID"

    async def test_una_orden_que_no_existe(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        r = await registrar(api, admin_csrf, 999_999, key="aviso-sin-orden")
        assert r.status_code == 404, r.text

    async def test_una_orden_legacy(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        from tests.db.test_production_orders_api import confirmada_y_pagada, escenario
        from tests.db.test_production_orders_api import crear_orden as crear_orden_legacy

        datos = await escenario(api, admin_csrf, db_session, suffix="_aviso_legacy")
        confirmada = await confirmada_y_pagada(api, admin_csrf, datos["quotation"])
        orden = await crear_orden_legacy(
            api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
        )
        assert orden.status_code == 201, orden.text

        r = await registrar(api, admin_csrf, int(orden.json()["id"]), key="aviso-legacy")

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_ORDER_NOT_V2"

    async def test_la_base_tambien_rechaza_un_mensaje_en_blanco(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        with pytest.raises(IntegrityError) as error:
            await db_session.execute(
                text(
                    "INSERT INTO production_order_communications"
                    " (production_order_id, channel, message, sent_at, idempotency_key)"
                    " VALUES (:oid, 'WHATSAPP', '   ', now(), 'aviso-blanco-bd')"
                ),
                {"oid": datos["order_id"]},
            )
        await db_session.rollback()
        assert "ck_production_order_communications_message_valid" in str(error.value)


# ---------------------------------------------------------------------------
# Estados de la orden
# ---------------------------------------------------------------------------
class TestEstados:
    async def test_se_registra_en_inicio_en_proceso_finalizada_y_anulada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """«Puede recoger» va con la orden FINALIZADA; «se anulo», con la anulada."""
        viva = await orden_con_existencia(api, admin_csrf)
        oid = viva["order_id"]
        assert (await registrar(api, admin_csrf, oid, key="aviso-en-inicio")).status_code == 201
        assert (await api.post(f"{ORDERS}/{oid}/start", headers=h(admin_csrf))).status_code == 200
        assert (await registrar(api, admin_csrf, oid, key="aviso-en-proceso")).status_code == 201
        await consumir(
            api, admin_csrf, oid, product_id=viva["pasta_id"], quantity="10", key="aviso-pasta"
        )
        assert (
            await api.post(f"{ORDERS}/{oid}/complete", headers=h(admin_csrf))
        ).status_code == 200
        r = await registrar(
            api, admin_csrf, oid, key="aviso-finalizada", message="Ya puede pasar a recoger."
        )
        assert r.status_code == 201, r.text

        from tests.db.test_production_v2_origin import crear_orden, otra_enviada_a_produccion

        otra = await otra_enviada_a_produccion(api, admin_csrf, viva)
        creada = await crear_orden(
            api, admin_csrf, v2_quotation_id=otra["id"], location_id=otra["location_id"]
        )
        assert creada.status_code == 201, creada.text
        anulada_id = int(creada.json()["id"])
        assert (
            await api.post(f"{ORDERS}/{anulada_id}/cancel", headers=h(admin_csrf))
        ).status_code == 200
        r = await registrar(
            api, admin_csrf, anulada_id, key="aviso-anulada", message="Su pedido fue anulado."
        )
        assert r.status_code == 201, r.text

    async def test_registrar_no_cambia_el_estado(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        antes = (await api.get(f"{ORDERS}/{datos['order_id']}")).json()

        r = await registrar(
            api,
            admin_csrf,
            datos["order_id"],
            key="aviso-sin-estado",
            message="Pedido finalizado, puede recoger.",
        )

        assert r.status_code == 201, r.text
        despues = (await api.get(f"{ORDERS}/{datos['order_id']}")).json()
        for campo in ("status", "started_at", "completed_at", "cancelled_at"):
            assert despues[campo] == antes[campo], campo
        assert despues["status"] == "CREATED"


# ---------------------------------------------------------------------------
# Lo que NO toca
# ---------------------------------------------------------------------------
class TestSinEfectos:
    async def test_no_toca_inventario_ni_cotizacion(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        cotizacion_antes = (await api.get(f"{V2}/{datos['id']}")).json()
        inventario_antes = await foto_inventario(db_session)

        r = await registrar(api, admin_csrf, datos["order_id"], key="aviso-sin-efectos")

        assert r.status_code == 201, r.text
        assert await foto_inventario(db_session) == inventario_antes
        assert (await api.get(f"{V2}/{datos['id']}")).json() == cotizacion_antes


# ---------------------------------------------------------------------------
# Seguimiento
# ---------------------------------------------------------------------------
class TestSeguimiento:
    async def test_aparece_una_vez_como_comunicacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        cuando = datetime.now(UTC)
        r = await registrar(
            api, admin_csrf, datos["order_id"], key="aviso-timeline", sent_at=cuando.isoformat()
        )
        assert r.status_code == 201, r.text

        linea = await api.get(f"{ORDERS}/{datos['order_id']}/timeline")

        [evento] = de_la_timeline(linea)
        assert evento["note"] is None and evento["consumption"] is None
        assert evento["status"] is None
        assert evento["communication"]["channel"] == "WHATSAPP"
        assert evento["communication"]["message"] == TEXTO_A
        assert datetime.fromisoformat(evento["occurred_at"]) == cuando
        assert evento["actor_name"] == evento["communication"]["sent_by_name"]
        # Ni se cuela como nota ni duplica nada.
        tipos = [e["type"] for e in linea.json()["items"]]
        assert tipos == ["STATUS", "COMMUNICATION"]

    async def test_se_ordena_entre_los_demas_hechos_y_solo_lleva_su_detalle(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Hallazgo de Copilot en el bloque D: el aviso en medio de todo lo demas.

        Se registra DESPUES que todo, pero con la fecha en que se aviso, que cae
        entre el arranque y el consumo: el seguimiento lo coloca por cuando
        ocurrio, no por cuando se anoto.
        """
        datos = await orden_con_existencia(api, admin_csrf)
        oid = datos["order_id"]
        assert (await api.post(f"{ORDERS}/{oid}/start", headers=h(admin_csrf))).status_code == 200
        # Un segundo de margen a cada lado, no cincuenta milisegundos: el
        # arranque y el aviso se fechan con el reloj de la aplicacion, y el
        # consumo con `now()` de PostgreSQL. En local la base corre en Docker
        # y su reloj puede ir decenas de milisegundos desfasado; con 50 ms la
        # prueba fallaba de vez en cuando sin que el orden estuviera mal.
        await asyncio.sleep(1)
        entre = datetime.now(UTC)
        await asyncio.sleep(1)
        consumo = await consumir(
            api, admin_csrf, oid, product_id=datos["pasta_id"], quantity="10", key="aviso-orden-c"
        )
        assert consumo.status_code == 201, consumo.text
        nota = await api.post(
            f"{ORDERS}/{oid}/notes",
            json={
                "kind": "NOTE",
                "body": "Revisar asas",
                "occurred_at": datetime.now(UTC).isoformat(),
                "idempotency_key": "aviso-orden-nota",
            },
            headers=h(admin_csrf),
        )
        assert nota.status_code == 201, nota.text
        aviso = await registrar(
            api, admin_csrf, oid, key="aviso-orden-tarde", sent_at=entre.isoformat()
        )
        assert aviso.status_code == 201, aviso.text

        items = (await api.get(f"{ORDERS}/{oid}/timeline")).json()["items"]

        assert [(e["type"], e["status"]) for e in items] == [
            ("STATUS", "CREATED"),
            ("STATUS", "STARTED"),
            ("COMMUNICATION", None),
            ("CONSUMPTION", None),
            ("NOTE", None),
        ]
        detalle_de = {
            "STATUS": [],
            "CONSUMPTION": ["consumption"],
            "NOTE": ["note"],
            "COMMUNICATION": ["communication"],
        }
        for e in items:
            presentes = [k for k in ("consumption", "note", "communication") if e[k] is not None]
            assert presentes == detalle_de[e["type"]], e

    def test_a_igual_instante_el_aviso_va_despues_de_los_demas(self) -> None:
        """Desempate por rango: estado, consumo, quema, nota y, al final, el aviso."""
        from app.models.production import (
            ProductionCommunicationChannel,
            ProductionNoteKind,
            ProductionOrderStatus,
        )
        from app.schemas.production import (
            ProductionCommunicationOut,
            ProductionNoteOut,
            ProductionTimelineEventOut,
            ProductionTimelineEventType,
        )
        from app.services.production import _timeline_key

        t = datetime.now(UTC)
        aviso = ProductionTimelineEventOut(
            type=ProductionTimelineEventType.COMMUNICATION,
            occurred_at=t,
            actor_name="A",
            communication=ProductionCommunicationOut(
                id=1,
                production_order_id=1,
                channel=ProductionCommunicationChannel.WHATSAPP,
                message="Hola",
                sent_at=t,
                sent_by_name="A",
                created_at=t,
            ),
        )
        nota = ProductionTimelineEventOut(
            type=ProductionTimelineEventType.NOTE,
            occurred_at=t,
            actor_name="A",
            note=ProductionNoteOut(
                id=99,
                production_order_id=1,
                kind=ProductionNoteKind.NOTE,
                body="x",
                kiln_id=None,
                kiln_name=None,
                firing_type=None,
                occurred_at=t,
                created_by_name="A",
                created_at=t,
            ),
        )
        estado = ProductionTimelineEventOut(
            type=ProductionTimelineEventType.STATUS,
            occurred_at=t,
            actor_name="A",
            status=ProductionOrderStatus.STARTED,
        )

        ordenados = sorted([aviso, nota, estado], key=_timeline_key)

        assert [e.type for e in ordenados] == [
            ProductionTimelineEventType.STATUS,
            ProductionTimelineEventType.NOTE,
            ProductionTimelineEventType.COMMUNICATION,
        ]


# ---------------------------------------------------------------------------
# Idempotencia y concurrencia
# ---------------------------------------------------------------------------
class TestIdempotencia:
    async def test_el_doble_clic_no_duplica(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        sent_at = datetime.now(UTC).isoformat()

        primera = await registrar(
            api, admin_csrf, datos["order_id"], key="aviso-doble", sent_at=sent_at
        )
        segunda = await registrar(
            api, admin_csrf, datos["order_id"], key="aviso-doble", sent_at=sent_at
        )

        assert primera.status_code == 201, primera.text
        assert segunda.status_code == 200, segunda.text
        assert segunda.json()["id"] == primera.json()["id"]
        assert await avisos(db_session, datos["order_id"]) == 1
        assert len(de_la_timeline(await api.get(f"{ORDERS}/{datos['order_id']}/timeline"))) == 1

    async def test_la_misma_clave_con_otra_fecha_u_otra_orden_es_un_409(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Hallazgo de Copilot en el bloque D: todo lo que distingue un aviso cuenta.

        El canal no se puede variar desde la API —hoy solo existe WHATSAPP y
        cualquier otro es 422 antes de llegar al servicio—; su parte de la
        comparacion se prueba en la siguiente, sobre el predicado.
        """
        from tests.db.test_production_v2_origin import crear_orden, otra_enviada_a_produccion

        datos = await orden_con_existencia(api, admin_csrf)
        otra = await otra_enviada_a_produccion(api, admin_csrf, datos)
        creada = await crear_orden(
            api, admin_csrf, v2_quotation_id=otra["id"], location_id=otra["location_id"]
        )
        assert creada.status_code == 201, creada.text
        otra_orden_id = int(creada.json()["id"])
        sent_at = datetime.now(UTC)
        primera = await registrar(
            api, admin_csrf, datos["order_id"], key="aviso-clave-fija", sent_at=sent_at.isoformat()
        )
        assert primera.status_code == 201, primera.text

        otra_fecha = await registrar(
            api,
            admin_csrf,
            datos["order_id"],
            key="aviso-clave-fija",
            sent_at=(sent_at - timedelta(seconds=1)).isoformat(),
        )
        otra_orden = await registrar(
            api, admin_csrf, otra_orden_id, key="aviso-clave-fija", sent_at=sent_at.isoformat()
        )

        for r in (otra_fecha, otra_orden):
            assert r.status_code == 409, r.text
            assert r.json()["error"]["code"] == "PRODUCTION_COMMUNICATION_KEY_REUSED"
        assert await avisos(db_session, datos["order_id"]) == 1
        assert await avisos(db_session, otra_orden_id) == 0

    def test_la_comparacion_del_reintento_incluye_el_canal(self) -> None:
        """Sin un segundo canal en el enum, se prueba el predicado con uno forzado."""
        from app.models.production import (
            ProductionCommunicationChannel,
            ProductionOrderCommunication,
        )
        from app.schemas.production import ProductionCommunicationCreateIn
        from app.services.production import ProductionOrderService

        cuando = datetime.now(UTC)
        guardado = ProductionOrderCommunication(
            production_order_id=7,
            channel=ProductionCommunicationChannel.WHATSAPP,
            message="Hola",
            sent_at=cuando,
            idempotency_key="aviso-predicado",
        )
        igual = ProductionCommunicationCreateIn(
            channel=ProductionCommunicationChannel.WHATSAPP,
            message="Hola",
            sent_at=cuando,
            idempotency_key="aviso-predicado",
        )
        otro_canal = igual.model_copy(update={"channel": "SMS"})

        assert ProductionOrderService._same_communication(guardado, order_id=7, data=igual)
        assert not ProductionOrderService._same_communication(guardado, order_id=7, data=otro_canal)
        assert not ProductionOrderService._same_communication(guardado, order_id=8, data=igual)

    async def test_la_misma_clave_con_otro_texto_es_un_409(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        sent_at = datetime.now(UTC).isoformat()
        await registrar(api, admin_csrf, datos["order_id"], key="aviso-reusado", sent_at=sent_at)

        r = await registrar(
            api,
            admin_csrf,
            datos["order_id"],
            key="aviso-reusado",
            sent_at=sent_at,
            message="Otro texto",
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_COMMUNICATION_KEY_REUSED"
        assert await avisos(db_session, datos["order_id"]) == 1

    async def test_cinco_peticiones_con_la_misma_clave_dejan_una(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        sent_at = datetime.now(UTC).isoformat()

        respuestas = await asyncio.gather(
            *(
                registrar(api, admin_csrf, datos["order_id"], key="aviso-rafaga", sent_at=sent_at)
                for _ in range(5)
            )
        )

        codigos = sorted(r.status_code for r in respuestas)
        assert codigos == [200, 200, 200, 200, 201], [r.text for r in respuestas]
        assert len({r.json()["id"] for r in respuestas}) == 1
        assert await avisos(db_session, datos["order_id"]) == 1

    async def test_dos_avisos_distintos_son_dos(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Con el MISMO texto incluso: dos claves son dos avisos deliberados."""
        datos = await orden_con_existencia(api, admin_csrf)

        respuestas = await asyncio.gather(
            registrar(api, admin_csrf, datos["order_id"], key="aviso-uno-de-dos"),
            registrar(api, admin_csrf, datos["order_id"], key="aviso-dos-de-dos"),
            registrar(
                api, admin_csrf, datos["order_id"], key="aviso-tres-de-tres", message="Otro aviso"
            ),
        )

        assert [r.status_code for r in respuestas] == [201, 201, 201]
        assert await avisos(db_session, datos["order_id"]) == 3
        assert len(de_la_timeline(await api.get(f"{ORDERS}/{datos['order_id']}/timeline"))) == 3
