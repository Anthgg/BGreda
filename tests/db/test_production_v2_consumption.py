"""Fase 010I, bloque B — el consumo real de una orden V2, contra PostgreSQL.

A diferencia de 010H, aqui SI se mueve inventario, y solo por una accion
explicita: registrar un consumo. Lo que se fija:

- el consumo descuenta exactamente lo pedido y deja UN movimiento PRODUCTION_OUT
  atado a la orden;
- si no alcanza, no descuenta nada: ni saldo negativo, ni movimiento, ni consumo;
- el doble clic, el reintento de red y cinco peticiones con la misma clave
  descuentan UNA vez;
- dos consumos a la vez de un saldo que solo da para uno: gana uno;
- el conflicto de clave dentro del SAVEPOINT deshace tambien el movimiento;
- solo se consume en INICIO o EN PROCESO, y solo en ordenes V2;
- una orden con consumos no se anula (D1) y anular no devuelve nada;
- la cotizacion V2 no cambia.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockBalance, StockMovement
from app.models.production import ProductionConsumption, ProductionConsumptionKind
from app.schemas.auth import AuthenticatedUser
from app.schemas.production import ProductionConsumptionCreateIn
from app.services.production import ProductionOrderService
from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate
from tests.db.test_production_v2_origin import (
    ORDERS,
    crear_orden,
    enviada_a_produccion,
    otra_enviada_a_produccion,
)
from tests.db.test_quoter_v2_lifecycle_api import h

V2 = "/api/v1/quotations-v2"
ADJUSTMENTS = "/api/v1/inventory/adjustments"
LOCATIONS = "/api/v1/inventory/locations"


# ---------------------------------------------------------------------------
# Apoyo
# ---------------------------------------------------------------------------
async def orden_con_existencia(
    api: httpx.AsyncClient, csrf: str, *, existencia: str = "1000"
) -> dict[str, Any]:
    """Una orden V2 en INICIO y la pasta de su cotizacion con existencia conocida."""
    datos = await enviada_a_produccion(api, csrf)
    orden = await crear_orden(
        api, csrf, v2_quotation_id=datos["id"], location_id=datos["location_id"]
    )
    assert orden.status_code == 201, orden.text
    if existencia != "0":
        ajuste = await api.post(
            ADJUSTMENTS,
            json={
                "product_id": datos["pasta_id"],
                "location_id": datos["location_id"],
                "quantity": existencia,
                "reason": "Existencia inicial de la prueba",
            },
            headers=h(csrf),
        )
        assert ajuste.status_code == 201, ajuste.text
    return {**datos, "order_id": int(orden.json()["id"])}


async def consumir(
    api: httpx.AsyncClient,
    csrf: str,
    order_id: int,
    *,
    product_id: int,
    quantity: str,
    key: str,
    kind: str = "BODY",
    **extra: Any,
) -> httpx.Response:
    return await api.post(
        f"{ORDERS}/{order_id}/consumptions",
        json={
            "product_id": product_id,
            "quantity": quantity,
            "kind": kind,
            "idempotency_key": key,
            **extra,
        },
        headers=h(csrf),
    )


async def saldo(db: AsyncSession, product_id: int, location_id: int) -> Decimal:
    db.expire_all()
    valor = await db.scalar(
        select(StockBalance.quantity).where(
            StockBalance.product_id == product_id, StockBalance.location_id == location_id
        )
    )
    return Decimal(valor) if valor is not None else Decimal(0)


async def movimientos(db: AsyncSession) -> int:
    db.expire_all()
    return int(await db.scalar(select(func.count()).select_from(StockMovement)) or 0)


async def consumos(db: AsyncSession) -> int:
    db.expire_all()
    return int(await db.scalar(select(func.count()).select_from(ProductionConsumption)) or 0)


# ---------------------------------------------------------------------------
# El consumo feliz
# ---------------------------------------------------------------------------
class TestConsumoReal:
    async def test_descuenta_exactamente_y_deja_un_movimiento_de_produccion(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        pasta, almacen = datos["pasta_id"], datos["location_id"]
        antes = await saldo(db_session, pasta, almacen)
        movs = await movimientos(db_session)

        r = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=pasta,
            quantity="200",
            key="consumo-feliz-010i",
            v2_quotation_product_id=datos["lines"][0],
            note="Pasta para la primera tanda",
        )

        assert r.status_code == 201, r.text
        consumo = r.json()
        assert antes == Decimal(1000)
        assert await saldo(db_session, pasta, almacen) == Decimal(800)
        assert await movimientos(db_session) == movs + 1

        assert consumo["kind"] == "BODY"
        assert Decimal(consumo["quantity"]) == Decimal(200)
        assert consumo["uom_code"] == "g"
        assert Decimal(consumo["balance_after"]) == Decimal(800)
        assert consumo["v2_quotation_product_id"] == datos["lines"][0]
        assert consumo["stock_location_id"] == almacen
        assert consumo["note"] == "Pasta para la primera tanda"
        assert consumo["created_by_name"]
        # Papel de taller: ni un importe.
        assert "unit_cost_snapshot" not in consumo
        assert not [campo for campo in consumo if "cost" in campo or "price" in campo]

        movimiento = await db_session.get(StockMovement, consumo["stock_movement_id"])
        assert movimiento is not None
        assert movimiento.movement_type.value == "PRODUCTION_OUT"
        assert movimiento.quantity == Decimal(-200)
        assert movimiento.balance_after == Decimal(800)
        assert movimiento.production_order_id == datos["order_id"]
        assert movimiento.created_by_name == consumo["created_by_name"]

    async def test_el_consumo_de_la_orden_entera_no_exige_pieza(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Un esmalte para todas las piezas no es de ninguna en concreto."""
        datos = await orden_con_existencia(api, admin_csrf)
        r = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="50",
            key="consumo-orden-010i",
            kind="OTHER",
        )
        assert r.status_code == 201, r.text
        assert r.json()["v2_quotation_product_id"] is None

    async def test_la_lista_devuelve_los_consumos_en_orden(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        for indice, cantidad in enumerate(("100", "150")):
            r = await consumir(
                api,
                admin_csrf,
                datos["order_id"],
                product_id=datos["pasta_id"],
                quantity=cantidad,
                key=f"consumo-lista-{indice}-010i",
            )
            assert r.status_code == 201, r.text

        lista = (await api.get(f"{ORDERS}/{datos['order_id']}/consumptions")).json()
        assert lista["total"] == 2
        assert [Decimal(fila["quantity"]) for fila in lista["items"]] == [
            Decimal(100),
            Decimal(150),
        ]
        assert [Decimal(fila["balance_after"]) for fila in lista["items"]] == [
            Decimal(900),
            Decimal(750),
        ]

    async def test_consumir_no_cambia_la_cotizacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        cabecera = (await api.get(f"{V2}/{datos['id']}")).json()
        precio = (await api.get(f"{V2}/{datos['id']}/pricing")).json()

        r = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="300",
            key="consumo-cotizacion-010i",
        )
        assert r.status_code == 201, r.text

        assert (await api.get(f"{V2}/{datos['id']}")).json() == cabecera
        assert (await api.get(f"{V2}/{datos['id']}/pricing")).json() == precio


# ---------------------------------------------------------------------------
# Lo que no alcanza no se descuenta
# ---------------------------------------------------------------------------
class TestExistenciaInsuficiente:
    async def test_pedir_mas_de_lo_que_hay_no_descuenta_nada(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf, existencia="100")
        pasta, almacen = datos["pasta_id"], datos["location_id"]
        movs = await movimientos(db_session)

        r = await consumir(
            api, admin_csrf, datos["order_id"], product_id=pasta, quantity="200", key="no-alcanza"
        )

        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "NEGATIVE_STOCK_NOT_ALLOWED"
        assert await saldo(db_session, pasta, almacen) == Decimal(100)
        assert await movimientos(db_session) == movs
        assert await consumos(db_session) == 0

    async def test_sin_saldo_previo_es_un_error_de_negocio_y_no_un_500(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Hallazgo MF3 del plan: el saldo inexistente arranca en 0, el consumo lo
        dejaria negativo y se rechaza ANTES de insertar ningun saldo."""
        datos = await orden_con_existencia(api, admin_csrf, existencia="0")

        r = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="10",
            key="sin-saldo-previo",
        )

        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "NEGATIVE_STOCK_NOT_ALLOWED"
        db_session.expire_all()
        saldos = await db_session.scalar(
            select(func.count())
            .select_from(StockBalance)
            .where(StockBalance.product_id == datos["pasta_id"])
        )
        assert saldos == 0, "no puede quedar un saldo creado a medias"
        assert await consumos(db_session) == 0


# ---------------------------------------------------------------------------
# Una clave, un descuento
# ---------------------------------------------------------------------------
class TestIdempotencia:
    async def test_el_reintento_devuelve_el_mismo_consumo_sin_descontar_otra_vez(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        pasta, almacen = datos["pasta_id"], datos["location_id"]
        primera = await consumir(
            api, admin_csrf, datos["order_id"], product_id=pasta, quantity="200", key="reintento"
        )
        movs = await movimientos(db_session)

        segunda = await consumir(
            api, admin_csrf, datos["order_id"], product_id=pasta, quantity="200", key="reintento"
        )

        assert primera.status_code == 201, primera.text
        assert segunda.status_code == 200, segunda.text
        assert segunda.json()["id"] == primera.json()["id"]
        assert await saldo(db_session, pasta, almacen) == Decimal(800)
        assert await movimientos(db_session) == movs
        assert await consumos(db_session) == 1

    async def test_la_misma_clave_pidiendo_otra_cantidad_es_un_409(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        pasta, almacen = datos["pasta_id"], datos["location_id"]
        await consumir(
            api, admin_csrf, datos["order_id"], product_id=pasta, quantity="200", key="otra-cosa"
        )

        r = await consumir(
            api, admin_csrf, datos["order_id"], product_id=pasta, quantity="999", key="otra-cosa"
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_CONSUMPTION_KEY_REUSED"
        assert await saldo(db_session, pasta, almacen) == Decimal(800)

    async def test_un_reintento_sin_almacen_no_valida_un_consumo_de_otro_almacen(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """«Sin almacen» es el de la orden: no vale como comodin para cualquier otro."""
        datos = await orden_con_existencia(api, admin_csrf)
        otro = await api.post(LOCATIONS, json={"name": "Almacen secundario"}, headers=h(admin_csrf))
        otro_id = int(otro.json()["id"])
        await api.post(
            ADJUSTMENTS,
            json={
                "product_id": datos["pasta_id"],
                "location_id": otro_id,
                "quantity": "500",
                "reason": "Existencia en el secundario",
            },
            headers=h(admin_csrf),
        )
        primera = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="100",
            key="almacen-explicito",
            stock_location_id=otro_id,
        )
        assert primera.status_code == 201, primera.text

        r = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="100",
            key="almacen-explicito",
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_CONSUMPTION_KEY_REUSED"

    async def test_cinco_peticiones_con_la_misma_clave_descuentan_una_vez(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        pasta, almacen = datos["pasta_id"], datos["location_id"]
        movs = await movimientos(db_session)

        respuestas = await asyncio.gather(
            *(
                consumir(
                    api,
                    admin_csrf,
                    datos["order_id"],
                    product_id=pasta,
                    quantity="200",
                    key="misma-clave-a-la-vez",
                )
                for _ in range(5)
            )
        )

        assert sorted(r.status_code for r in respuestas) == [200, 200, 200, 200, 201]
        assert len({r.json()["id"] for r in respuestas}) == 1
        assert await saldo(db_session, pasta, almacen) == Decimal(800)
        assert await movimientos(db_session) == movs + 1
        assert await consumos(db_session) == 1

    async def test_el_conflicto_dentro_del_savepoint_deshace_tambien_el_movimiento(
        self,
        api: httpx.AsyncClient,
        admin_csrf: str,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hallazgo MF6 del plan, forzado.

        Con el bloqueo consultivo esta rama no deberia alcanzarse nunca, asi que
        se fuerza: la primera busqueda por clave «no encuentra» el consumo que si
        existe. El servicio mueve stock y el INSERT choca con el UNIQUE. Si el
        SAVEPOINT envolviera solo el INSERT, el movimiento quedaria y el saldo
        bajaria dos veces.
        """
        datos = await orden_con_existencia(api, admin_csrf)
        pasta, almacen = datos["pasta_id"], datos["location_id"]
        ganador = await consumir(
            api, admin_csrf, datos["order_id"], product_id=pasta, quantity="200", key="savepoint"
        )
        assert ganador.status_code == 201, ganador.text
        movs = await movimientos(db_session)

        servicio = ProductionOrderService(db_session)
        original = servicio._consumption_by_key
        llamadas = {"n": 0}

        async def ciega_la_primera_vez(clave: str) -> ProductionConsumption | None:
            llamadas["n"] += 1
            if llamadas["n"] == 1:
                return None
            return await original(clave)

        monkeypatch.setattr(servicio, "_consumption_by_key", ciega_la_primera_vez)
        usuario = AuthenticatedUser.model_validate(
            {
                "id": "11111111-2222-3333-4444-555555555555",
                "email": "prueba@empresa.com",
                "display_name": "Prueba",
                "role": "ADMIN",
            }
        )

        consumo, nuevo = await servicio.record_consumption(
            datos["order_id"],
            ProductionConsumptionCreateIn(
                product_id=pasta,
                quantity=Decimal(200),
                kind=ProductionConsumptionKind.BODY,
                idempotency_key="savepoint",
            ),
            user=usuario,
        )
        await db_session.commit()

        assert nuevo is False
        assert consumo.id == ganador.json()["id"]
        assert llamadas["n"] == 2, "tiene que haber pasado por la rama del IntegrityError"
        assert await saldo(db_session, pasta, almacen) == Decimal(800)
        assert await movimientos(db_session) == movs


# ---------------------------------------------------------------------------
# Concurrencia sobre el mismo saldo
# ---------------------------------------------------------------------------
class TestConcurrencia:
    async def test_dos_consumos_de_700_sobre_1000_solo_uno_gana(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf, existencia="1000")
        pasta, almacen = datos["pasta_id"], datos["location_id"]
        movs = await movimientos(db_session)

        a, b = await asyncio.gather(
            consumir(
                api,
                admin_csrf,
                datos["order_id"],
                product_id=pasta,
                quantity="700",
                key="carrera-a",
            ),
            consumir(
                api,
                admin_csrf,
                datos["order_id"],
                product_id=pasta,
                quantity="700",
                key="carrera-b",
            ),
        )

        assert sorted((a.status_code, b.status_code)) == [201, 422], (a.text, b.text)
        perdedora = a if a.status_code == 422 else b
        assert perdedora.json()["error"]["code"] == "NEGATIVE_STOCK_NOT_ALLOWED"
        assert await saldo(db_session, pasta, almacen) == Decimal(300)
        assert await movimientos(db_session) == movs + 1
        assert await consumos(db_session) == 1

    async def test_dos_ordenes_distintas_tampoco_pueden_llevarse_las_dos(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Dos ordenes no comparten el bloqueo de la orden: lo que las serializa es el
        bloqueo del SALDO."""
        datos = await orden_con_existencia(api, admin_csrf, existencia="1000")
        otra = await otra_enviada_a_produccion(api, admin_csrf, datos)
        otra_orden = await crear_orden(
            api, admin_csrf, v2_quotation_id=otra["id"], location_id=datos["location_id"]
        )
        pasta, almacen = datos["pasta_id"], datos["location_id"]

        a, b = await asyncio.gather(
            consumir(
                api,
                admin_csrf,
                datos["order_id"],
                product_id=pasta,
                quantity="700",
                key="orden-a-010i",
            ),
            consumir(
                api,
                admin_csrf,
                int(otra_orden.json()["id"]),
                product_id=pasta,
                quantity="700",
                key="orden-b-010i",
            ),
        )

        assert sorted((a.status_code, b.status_code)) == [201, 422], (a.text, b.text)
        assert await saldo(db_session, pasta, almacen) == Decimal(300)


# ---------------------------------------------------------------------------
# Cuando se puede consumir
# ---------------------------------------------------------------------------
class TestCuandoSePuedeConsumir:
    async def test_se_consume_en_inicio_y_en_proceso(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        en_inicio = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="10",
            key="en-inicio",
        )
        arrancada = await api.post(f"{ORDERS}/{datos['order_id']}/start", headers=h(admin_csrf))
        en_proceso = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="10",
            key="en-proceso",
        )

        assert en_inicio.status_code == 201, en_inicio.text
        assert arrancada.status_code == 200, arrancada.text
        assert en_proceso.status_code == 201, en_proceso.text

    async def test_una_orden_finalizada_no_admite_consumos(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="10",
            key="antes-de-cerrar",
        )
        await api.post(f"{ORDERS}/{datos['order_id']}/start", headers=h(admin_csrf))
        cerrada = await api.post(f"{ORDERS}/{datos['order_id']}/complete", headers=h(admin_csrf))
        assert cerrada.status_code == 200, cerrada.text

        r = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="10",
            key="tras-cerrar",
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_ORDER_NOT_CONSUMABLE"

    async def test_una_orden_anulada_no_admite_consumos(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        anulada = await api.post(f"{ORDERS}/{datos['order_id']}/cancel", headers=h(admin_csrf))
        assert anulada.status_code == 200, anulada.text

        r = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="10",
            key="tras-anular",
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_ORDER_NOT_CONSUMABLE"

    async def test_una_orden_legacy_no_registra_consumos_a_mano(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Una Legacy ya descuenta su receta al arrancar: esto la descontaria dos veces."""
        from tests.db.test_production_orders_api import (
            confirmada_y_pagada,
            escenario,
        )
        from tests.db.test_production_orders_api import (
            crear_orden as crear_orden_legacy,
        )

        datos = await escenario(api, admin_csrf, db_session, suffix="_consumo_legacy")
        confirmada = await confirmada_y_pagada(api, admin_csrf, datos["quotation"])
        orden = await crear_orden_legacy(
            api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
        )
        assert orden.status_code == 201, orden.text

        # El origen se comprueba ANTES que el material: sea cual sea, una Legacy
        # no llega a descontar nada.
        r = await consumir(
            api,
            admin_csrf,
            int(orden.json()["id"]),
            product_id=1,
            quantity="1",
            key="legacy-a-mano",
        )

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_CONSUMPTION_ORDER_NOT_V2"


# ---------------------------------------------------------------------------
# Lo que no vale
# ---------------------------------------------------------------------------
class TestRechazos:
    async def test_un_material_desactivado(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        await db_session.execute(
            text("UPDATE products SET active = false WHERE id = :id"), {"id": datos["pasta_id"]}
        )
        await db_session.commit()

        r = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="10",
            key="inactivo",
        )

        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_CONSUMPTION_MATERIAL_INVALID"

    async def test_un_material_que_no_existe(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        r = await consumir(
            api, admin_csrf, datos["order_id"], product_id=999_999, quantity="10", key="no-existe"
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_CONSUMPTION_MATERIAL_INVALID"

    async def test_un_almacen_que_no_existe(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        r = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="10",
            key="almacen-falso",
            stock_location_id=999_999,
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_ORDER_LOCATION_INVALID"

    async def test_una_pieza_de_otra_cotizacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        otra = await otra_enviada_a_produccion(api, admin_csrf, datos)
        pieza_ajena = (await api.get(f"{V2}/{otra['id']}/products")).json()["items"][0]["id"]

        r = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="10",
            key="pieza-ajena",
            v2_quotation_product_id=pieza_ajena,
        )

        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_CONSUMPTION_LINE_INVALID"

    @pytest.mark.parametrize("cantidad", ["0", "-5"])
    async def test_una_cantidad_que_no_es_positiva(
        self, api: httpx.AsyncClient, admin_csrf: str, cantidad: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        r = await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity=cantidad,
            key=f"cantidad-{cantidad}",
        )
        assert r.status_code == 422, r.text

    async def test_la_clave_de_idempotencia_es_obligatoria(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        r = await api.post(
            f"{ORDERS}/{datos['order_id']}/consumptions",
            json={"product_id": datos["pasta_id"], "quantity": "10", "kind": "BODY"},
            headers=h(admin_csrf),
        )
        assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Anular una orden que ya gasto material (D1)
# ---------------------------------------------------------------------------
class TestAnularConConsumos:
    async def test_no_se_anula_y_no_devuelve_nada(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        pasta, almacen = datos["pasta_id"], datos["location_id"]
        await consumir(
            api,
            admin_csrf,
            datos["order_id"],
            product_id=pasta,
            quantity="200",
            key="antes-de-anular",
        )
        movs = await movimientos(db_session)

        r = await api.post(f"{ORDERS}/{datos['order_id']}/cancel", headers=h(admin_csrf))

        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PRODUCTION_ORDER_HAS_CONSUMPTIONS"
        assert (await api.get(f"{ORDERS}/{datos['order_id']}")).json()["status"] == "CREATED"
        # Ni devolucion automatica ni movimiento nuevo.
        assert await saldo(db_session, pasta, almacen) == Decimal(800)
        assert await movimientos(db_session) == movs

    async def test_consumir_y_anular_a_la_vez_nunca_terminan_los_dos(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """La garantia de D1 bajo carrera, no solo en serie.

        Consumir y anular bloquean la MISMA fila de la orden. Gane quien gane, el
        otro ve el resultado: o se consumio y no se anula, o se anulo y no se
        consume. Nunca una orden anulada con material descontado.
        """
        datos = await orden_con_existencia(api, admin_csrf)
        pasta, almacen = datos["pasta_id"], datos["location_id"]

        consumo, anulacion = await asyncio.gather(
            consumir(
                api,
                admin_csrf,
                datos["order_id"],
                product_id=pasta,
                quantity="200",
                key="carrera-anular",
            ),
            api.post(f"{ORDERS}/{datos['order_id']}/cancel", headers=h(admin_csrf)),
        )

        estado = (await api.get(f"{ORDERS}/{datos['order_id']}")).json()["status"]
        if consumo.status_code == 201:
            assert anulacion.status_code == 409, anulacion.text
            assert anulacion.json()["error"]["code"] == "PRODUCTION_ORDER_HAS_CONSUMPTIONS"
            assert estado == "CREATED"
            assert await saldo(db_session, pasta, almacen) == Decimal(800)
        else:
            assert anulacion.status_code == 200, anulacion.text
            assert consumo.status_code == 409, consumo.text
            assert consumo.json()["error"]["code"] == "PRODUCTION_ORDER_NOT_CONSUMABLE"
            assert estado == "CANCELLED"
            assert await saldo(db_session, pasta, almacen) == Decimal(1000)
            assert await consumos(db_session) == 0

    async def test_sin_consumos_se_sigue_pudiendo_anular(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_con_existencia(api, admin_csrf)
        r = await api.post(f"{ORDERS}/{datos['order_id']}/cancel", headers=h(admin_csrf))
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "CANCELLED"


# ---------------------------------------------------------------------------
# Permisos
# ---------------------------------------------------------------------------
class TestPermisos:
    async def test_el_operador_registra_consumos(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Consumir es de taller, igual que ajustar existencia (`WorkshopUserDep`)."""
        datos = await orden_con_existencia(api, admin_csrf)
        operario = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)

        r = await consumir(
            api,
            operario,
            datos["order_id"],
            product_id=datos["pasta_id"],
            quantity="50",
            key="operario",
        )

        assert r.status_code == 201, r.text
        assert await saldo(db_session, datos["pasta_id"], datos["location_id"]) == Decimal(950)
