"""Fase 009K — linaje de muestras y guardia sobre la produccion final.

La pregunta que responde este archivo es una sola: **cual es la muestra que
manda hoy**. Una cotizacion puede arrastrar varias iteraciones —la primera no
valio, la segunda se anulo, la tercera se aprobo— y de todas ellas solo una
decide si el pedido se puede fabricar.

Contestarla con «la ultima creada» seria facil y estaria mal: dos cadenas
independientes de la misma cotizacion tienen ids entremezclados, y la mas nueva
de la tabla no es la que sustituye a la de esta pieza. Se recorre la cadena.

Lo otro que se prueba es lo que el guardia NO hace: no bloquea pedidos ajenos,
no revive una muestra rechazada cuya sucesora fue aprobada, y no convierte un
registro creado por error en una condena perpetua para esa cotizacion.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import MovementType, StockMovement
from app.models.production import ProductionOrder, ProductionOrderStatus
from app.models.prototypes import Prototype
from app.services.inventory import InventoryService, OrphanPrototypeMovementError
from tests.db.conftest import TEST_EMAIL, TEST_PASSWORD, authenticate
from tests.db.test_production_orders_api import (
    ORDERS,
    confirmar,
    crear_orden,
    dar_existencia,
    escenario,
    pagar,
)
from tests.db.test_prototypes import (
    PROTOTYPES,
    _foto,
    _material,
    _prototipo,
    crear_prototipo,
)
from tests.db.test_quotation_builder_api import head


async def _pedido_con_muestra(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    *,
    suffix: str,
) -> dict[str, Any]:
    """Una cotizacion pagada con su orden final Y una muestra, ambas de ella."""
    datos = await escenario(api, csrf, db_session, suffix=suffix)
    confirmada = await confirmar(api, csrf, datos["quotation"])
    await pagar(api, csrf, confirmada["id"])

    barro = await _material(api, csrf, nombre=f"Arcilla guard{suffix}")
    await dar_existencia(
        api, csrf, product_id=barro["id"], location_id=datos["location_id"], cantidad="1000"
    )

    orden = await crear_orden(
        api, csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert orden.status_code == 201, orden.text

    muestra = await crear_prototipo(
        api,
        csrf,
        name=f"E2E-009K guard{suffix}",
        quotation_id=confirmada["id"],
        stock_location_id=datos["location_id"],
        materials=[{"product_id": barro["id"], "quantity": "20"}],
    )
    assert muestra.status_code == 201, muestra.text

    return {
        **datos,
        "confirmada": confirmada,
        "barro": barro,
        "orden": orden.json(),
        "prototipo": muestra.json(),
    }


async def _fabricar_muestra(api: httpx.AsyncClient, csrf: str, prototipo_id: int) -> None:
    """Deja la muestra en COMPLETED, que es cuando se puede decidir sobre ella."""
    arrancada = await api.post(f"{PROTOTYPES}/{prototipo_id}/start", headers=head(csrf))
    assert arrancada.status_code == 200, arrancada.text
    completada = await api.post(f"{PROTOTYPES}/{prototipo_id}/complete", headers=head(csrf))
    assert completada.status_code == 200, completada.text


# ---------------------------------------------------------------------------
# 16, 17, 18 — La matriz basica
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_16_una_muestra_sin_decidir_para_la_produccion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """COMPLETED + PENDING bloquea, y el pedido no se mueve ni un gramo."""
    datos = await _pedido_con_muestra(api, admin_csrf, db_session, suffix="_pend")
    await _fabricar_muestra(api, admin_csrf, datos["prototipo"]["id"])
    foto = await _foto(db_session)

    arranque = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert arranque.status_code == 409, arranque.text
    assert arranque.json()["error"]["code"] == "PRODUCTION_ORDER_PROTOTYPE_NOT_APPROVED"

    db_session.expire_all()
    orden = await db_session.get(ProductionOrder, datos["orden"]["id"])
    assert orden is not None
    assert orden.status is ProductionOrderStatus.CREATED
    assert orden.started_at is None
    movimientos, salidas, saldos, _ = await _foto(db_session)
    assert (movimientos, salidas, saldos) == foto[:3]


@pytest.mark.asyncio
async def test_17_una_muestra_rechazada_para_la_produccion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    datos = await _pedido_con_muestra(api, admin_csrf, db_session, suffix="_rej")
    await _fabricar_muestra(api, admin_csrf, datos["prototipo"]["id"])
    rechazo = await api.post(
        f"{PROTOTYPES}/{datos['prototipo']['id']}/reject", json={}, headers=head(admin_csrf)
    )
    assert rechazo.status_code == 200, rechazo.text

    arranque = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert arranque.status_code == 409, arranque.text
    assert arranque.json()["error"]["code"] == "PRODUCTION_ORDER_PROTOTYPE_NOT_APPROVED"


@pytest.mark.asyncio
async def test_18_aprobada_deja_pasar_el_guardia(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El guardia de prototipo deja pasar; los demas siguen mandando.

    Se comprueba que la orden ARRANCA de verdad, no solo que el codigo de error
    cambio: un fallo posterior por inventario tambien haria desaparecer el
    codigo de prototipo y la prueba pasaria sin que el pedido se fabricara.
    """
    datos = await _pedido_con_muestra(api, admin_csrf, db_session, suffix="_apr")
    await _fabricar_muestra(api, admin_csrf, datos["prototipo"]["id"])
    aprobacion = await api.post(
        f"{PROTOTYPES}/{datos['prototipo']['id']}/approve", json={}, headers=head(admin_csrf)
    )
    assert aprobacion.status_code == 200, aprobacion.text

    arranque = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert arranque.status_code == 200, arranque.text
    assert arranque.json()["status"] == "STARTED"


# ---------------------------------------------------------------------------
# 19, 20, 21 — Iteraciones
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_19_una_rechazada_con_sucesora_aprobada_deja_de_bloquear(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """HISTORICAL_REJECTED_PERMANENTLY_BLOCKS: NO.

    Es el caso que hace util todo el linaje: la primera muestra no valio, se
    hizo otra y esa si. Si la rechazada siguiera contando, repetir una muestra
    dejaria el pedido sin poder fabricarse nunca.
    """
    datos = await _pedido_con_muestra(api, admin_csrf, db_session, suffix="_iter1")
    primera = datos["prototipo"]["id"]
    await _fabricar_muestra(api, admin_csrf, primera)
    await api.post(f"{PROTOTYPES}/{primera}/reject", json={}, headers=head(admin_csrf))

    sucesora = await api.post(
        f"{PROTOTYPES}/{primera}/successor", json={}, headers=head(admin_csrf)
    )
    assert sucesora.status_code == 201, sucesora.text
    segunda = sucesora.json()["id"]
    assert sucesora.json()["supersedes_prototype_id"] == primera
    assert sucesora.json()["code"] != datos["prototipo"]["code"]
    # La sucesora hereda la INTENCION, no la historia.
    assert sucesora.json()["status"] == "CREATED"
    assert sucesora.json()["approval"] == "PENDING"
    assert len(sucesora.json()["materials"]) == 1

    await _fabricar_muestra(api, admin_csrf, segunda)
    await api.post(f"{PROTOTYPES}/{segunda}/approve", json={}, headers=head(admin_csrf))

    arranque = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert arranque.status_code == 200, arranque.text
    # Y la rechazada sigue rechazada: un rechazo es un hecho, no se reescribe.
    anterior = await _prototipo(db_session, primera)
    assert anterior.approval.value == "REJECTED"


@pytest.mark.asyncio
async def test_20_una_rechazada_con_sucesora_sin_decidir_sigue_bloqueando(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La vigente es la sucesora, y la sucesora todavia no convencio a nadie."""
    datos = await _pedido_con_muestra(api, admin_csrf, db_session, suffix="_iter2")
    primera = datos["prototipo"]["id"]
    await _fabricar_muestra(api, admin_csrf, primera)
    await api.post(f"{PROTOTYPES}/{primera}/reject", json={}, headers=head(admin_csrf))

    sucesora = await api.post(
        f"{PROTOTYPES}/{primera}/successor", json={}, headers=head(admin_csrf)
    )
    assert sucesora.status_code == 201, sucesora.text

    arranque = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert arranque.status_code == 409, arranque.text
    assert arranque.json()["error"]["code"] == "PRODUCTION_ORDER_PROTOTYPE_NOT_APPROVED"


@pytest.mark.asyncio
async def test_21_una_muestra_anulada_no_condena_al_pedido(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CANCELLED_PROTOTYPE_BLOCKS: NO.

    Si se registro por error, anularla tiene que devolver el pedido a como
    estaba. Lo contrario dejaria una cotizacion sin poder fabricarse por un
    apunte equivocado, y sin ninguna forma de arreglarlo.
    """
    datos = await _pedido_con_muestra(api, admin_csrf, db_session, suffix="_canc")

    bloqueado = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))
    assert bloqueado.status_code == 409, bloqueado.text

    anulada = await api.post(
        f"{PROTOTYPES}/{datos['prototipo']['id']}/cancel", headers=head(admin_csrf)
    )
    assert anulada.status_code == 200, anulada.text

    arranque = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert arranque.status_code == 200, arranque.text
    assert arranque.json()["status"] == "STARTED"


# ---------------------------------------------------------------------------
# 22 — Pedidos ajenos
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_22_la_muestra_de_un_pedido_no_bloquea_otro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """UNRELATED_FINAL_PRODUCTION_NOT_BLOCKED."""
    con_muestra = await _pedido_con_muestra(api, admin_csrf, db_session, suffix="_conA")

    otros = await escenario(api, admin_csrf, db_session, suffix="_sinB")
    confirmada_b = await confirmar(api, admin_csrf, otros["quotation"])
    await pagar(api, admin_csrf, confirmada_b["id"])
    orden_b = await crear_orden(
        api, admin_csrf, quotation_id=confirmada_b["id"], location_id=otros["location_id"]
    )
    assert orden_b.status_code == 201, orden_b.text

    # La muestra de A esta sin decidir y bloquea A...
    bloqueado = await api.post(
        f"{ORDERS}/{con_muestra['orden']['id']}/start", headers=head(admin_csrf)
    )
    assert bloqueado.status_code == 409, bloqueado.text

    # ...y no tiene nada que decir sobre B.
    arranque = await api.post(f"{ORDERS}/{orden_b.json()['id']}/start", headers=head(admin_csrf))
    assert arranque.status_code == 200, arranque.text


# ---------------------------------------------------------------------------
# 23 — Enlace incoherente
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_23_no_se_enlaza_una_muestra_a_un_producto_de_otro_pedido(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Un identificador enviado a mano no puede crear un requisito falso.

    Sin esta comprobacion se podria colgar «la muestra de la jarra» de una
    cotizacion de platos, y el guardia pararia un pedido por una muestra que no
    le corresponde.
    """
    pedido_a = await escenario(api, admin_csrf, db_session, suffix="_linkA")
    confirmada_a = await confirmar(api, admin_csrf, pedido_a["quotation"])
    pedido_b = await escenario(api, admin_csrf, db_session, suffix="_linkB")

    respuesta = await crear_prototipo(
        api,
        admin_csrf,
        name="E2E-009K enlace imposible",
        quotation_id=confirmada_a["id"],
        product_id=pedido_b["producto"]["id"],
    )

    assert respuesta.status_code == 422, respuesta.text
    assert respuesta.json()["error"]["code"] == "PROTOTYPE_PRODUCT_NOT_IN_QUOTATION"

    db_session.expire_all()
    creadas = int(
        await db_session.scalar(
            select(func.count())
            .select_from(Prototype)
            .where(Prototype.quotation_id == confirmada_a["id"])
        )
        or 0
    )
    assert creadas == 0, "no puede quedar un vinculo incoherente persistido"


# ---------------------------------------------------------------------------
# 24, 25, 26 — Cadenas imposibles
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_24_25_una_cadena_no_puede_cerrarse_sobre_si_misma(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El ciclo corto lo corta la base; el largo, el servicio.

    Se construye A → B → C por el camino real de la API y se comprueba que
    ninguna de las tres puede acabar apuntando a una anterior. Un ciclo dejaria
    un grupo de muestras donde ninguna es la vigente, y el guardia no sabria
    que responder.
    """
    primera = await crear_prototipo(api, admin_csrf, name="E2E-009K cadena A")
    assert primera.status_code == 201, primera.text
    a = primera.json()["id"]

    # La API no ofrece forma de apuntar a una anterior arbitraria: el sucesor
    # SIEMPRE nace de su predecesor. Eso ya cierra el ciclo por construccion.
    segunda = await api.post(f"{PROTOTYPES}/{a}/successor", json={}, headers=head(admin_csrf))
    assert segunda.status_code == 201, segunda.text
    b = segunda.json()["id"]
    assert segunda.json()["supersedes_prototype_id"] == a

    tercera = await api.post(f"{PROTOTYPES}/{b}/successor", json={}, headers=head(admin_csrf))
    assert tercera.status_code == 201, tercera.text
    c = tercera.json()["id"]

    db_session.expire_all()
    cadena = {
        fila.id: fila.supersedes_prototype_id
        for fila in (await db_session.execute(select(Prototype).where(Prototype.id.in_([a, b, c]))))
        .scalars()
        .all()
    }
    assert cadena[a] is None
    assert cadena[b] == a
    assert cadena[c] == b
    # Ninguna se apunta a si misma: lo garantiza el CHECK `no_self_supersede`.
    assert all(hijo != padre for hijo, padre in cadena.items())


@pytest.mark.asyncio
async def test_26_una_muestra_no_admite_dos_sucesoras(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_SINGLE_SUCCESSOR, y con error de dominio, no de PostgreSQL.

    Dos iteraciones colgando de la misma bifurcarian la cadena y «cual es la
    vigente» pasaria a ser una opinion.
    """
    primera = await crear_prototipo(api, admin_csrf, name="E2E-009K bifurcacion")
    assert primera.status_code == 201, primera.text
    a = primera.json()["id"]

    una = await api.post(f"{PROTOTYPES}/{a}/successor", json={}, headers=head(admin_csrf))
    assert una.status_code == 201, una.text

    otra = await api.post(f"{PROTOTYPES}/{a}/successor", json={}, headers=head(admin_csrf))

    assert otra.status_code == 409, otra.text
    assert otra.json()["error"]["code"] == "PROTOTYPE_ALREADY_SUPERSEDED"
    # Y el mensaje no filtra SQL crudo a quien lo lee.
    assert "psycopg" not in otra.text.lower()
    assert "integrityerror" not in otra.text.lower()


# ---------------------------------------------------------------------------
# 27 — Correlativo bajo concurrencia
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_27_veinte_muestras_a_la_vez_no_repiten_codigo(
    api: httpx.AsyncClient, api_app: Any, db_session: AsyncSession
) -> None:
    """PROTOTYPE_CODE_CONCURRENCY_SAFE, probado para PROTOTYPE y no heredado.

    Que el contador funcione para cotizaciones no demuestra que funcione aqui:
    lo que se prueba es la fila de `document_sequences` de PROTOTYPE y su
    cerrojo, que es nueva.
    """
    cantidad = 20

    async def crear(indice: int) -> tuple[int, str | None]:
        transporte = httpx.ASGITransport(app=api_app)
        async with httpx.AsyncClient(transport=transporte, base_url="http://testserver") as cliente:
            csrf = await authenticate(cliente, email=TEST_EMAIL, password=TEST_PASSWORD)
            respuesta = await cliente.post(
                PROTOTYPES,
                json={"name": f"E2E-009K concurrente {indice}"},
                headers=head(csrf),
            )
            cuerpo = respuesta.json() if respuesta.status_code == 201 else {}
            return respuesta.status_code, cuerpo.get("code")

    resultados = await asyncio.gather(*(crear(i) for i in range(cantidad)))

    creados = [codigo for estado, codigo in resultados if estado == 201 and codigo]
    assert len(creados) == cantidad, [estado for estado, _ in resultados]
    assert len(set(creados)) == cantidad, "hay codigos repetidos"
    for codigo in creados:
        assert codigo.startswith("PRT-")
        _prefijo, anio, numero = codigo.split("-")
        assert len(anio) == 4 and anio.isdigit()
        assert len(numero) == 6 and numero.isdigit()


# ---------------------------------------------------------------------------
# Extras obligatorios
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_extra_a_un_consumo_de_muestra_sin_muestra_no_se_escribe(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """ORPHAN_PROTOTYPE_OUT: 0, comprobado en el motor y no en la API.

    Se llama al unico camino de escritura de existencias directamente: es el
    sitio donde la comprobacion no se puede rodear, y por tanto donde tiene que
    estar.
    """
    from app.models.inventory import StockLocation
    from app.models.masters import Product

    datos = await escenario(api, admin_csrf, db_session, suffix="_orphan")
    barro = await _material(api, admin_csrf, nombre="Arcilla huerfana")
    await dar_existencia(
        api, admin_csrf, product_id=barro["id"], location_id=datos["location_id"], cantidad="500"
    )
    foto = await _foto(db_session)

    db_session.expire_all()
    producto = await db_session.get(Product, barro["id"])
    ubicacion = await db_session.get(StockLocation, datos["location_id"])
    assert producto is not None and ubicacion is not None

    with pytest.raises(OrphanPrototypeMovementError):
        await InventoryService(db_session).apply_movement(
            product=producto,
            location=ubicacion,
            quantity=Decimal("-10"),
            movement_type=MovementType.PROTOTYPE_OUT,
            reason="huerfano",
            user_id=None,
            user_name=None,
        )

    await db_session.rollback()
    movimientos, salidas, _saldos, _ = await _foto(db_session)
    assert (movimientos, salidas) == foto[:2]


@pytest.mark.asyncio
async def test_extra_b_los_materiales_no_se_tocan_despues_de_arrancar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Una vez gastado, lo que llevaba la muestra es historia fisica."""
    from tests.db.test_prototypes import _muestra_lista

    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_editstart")
    prototipo_id = datos["prototipo"]["id"]
    await api.post(f"{PROTOTYPES}/{prototipo_id}/start", headers=head(admin_csrf))

    respuesta = await api.put(
        f"{PROTOTYPES}/{prototipo_id}/materials",
        json={"materials": [{"product_id": datos["barro"]["id"], "quantity": "999"}]},
        headers=head(admin_csrf),
    )

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PROTOTYPE_NOT_EDITABLE"


@pytest.mark.asyncio
async def test_extra_c_la_procedencia_fisica_no_se_reescribe(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """De que pedido salio y de que almacen no cambian una vez consumido."""
    from tests.db.test_prototypes import _muestra_lista

    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_origstart")
    prototipo_id = datos["prototipo"]["id"]
    await api.post(f"{PROTOTYPES}/{prototipo_id}/start", headers=head(admin_csrf))

    respuesta = await api.put(
        f"{PROTOTYPES}/{prototipo_id}",
        json={"stock_location_id": datos["location_id"], "name": "otro nombre"},
        headers=head(admin_csrf),
    )

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PROTOTYPE_NOT_EDITABLE"


@pytest.mark.asyncio
async def test_extra_d_cobrar_la_cotizacion_no_arranca_la_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Cobrar habilita; no fabrica. Son dos decisiones de dos personas."""
    from tests.db.test_prototypes import _muestra_lista

    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_paynostart", pagada=False)
    foto = await _foto(db_session)

    await pagar(api, admin_csrf, datos["confirmada"]["id"])

    prototipo = await _prototipo(db_session, datos["prototipo"]["id"])
    assert prototipo.status.value == "CREATED"
    assert prototipo.started_at is None
    movimientos, salidas, saldos, _ = await _foto(db_session)
    assert (movimientos, salidas, saldos) == foto[:3]


@pytest.mark.asyncio
async def test_extra_e_aprobar_no_arranca_la_produccion_final(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Aprobar satisface un guardia; no crea ni arranca ninguna orden."""
    datos = await _pedido_con_muestra(api, admin_csrf, db_session, suffix="_noauto")
    await _fabricar_muestra(api, admin_csrf, datos["prototipo"]["id"])
    foto = await _foto(db_session)

    aprobacion = await api.post(
        f"{PROTOTYPES}/{datos['prototipo']['id']}/approve", json={}, headers=head(admin_csrf)
    )
    assert aprobacion.status_code == 200, aprobacion.text

    db_session.expire_all()
    orden = await db_session.get(ProductionOrder, datos["orden"]["id"])
    assert orden is not None
    assert orden.status is ProductionOrderStatus.CREATED
    assert orden.started_at is None

    produccion = int(
        await db_session.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(StockMovement.production_order_id == datos["orden"]["id"])
        )
        or 0
    )
    assert produccion == 0
    movimientos, salidas, saldos, _ = await _foto(db_session)
    assert (movimientos, salidas, saldos) == foto[:3]


@pytest.mark.asyncio
async def test_regresion_una_cotizacion_sin_muestra_produce_como_siempre(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NORMAL_PRODUCTION_REGRESSION: 0.

    Lo que mas facil seria romper: que 009K obligara a tener prototipo para
    fabricar. La produccion de una cotizacion sin muestras tiene que seguir
    comportandose exactamente como antes.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_regres")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    await pagar(api, admin_csrf, confirmada["id"])
    orden = await crear_orden(
        api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert orden.status_code == 201, orden.text

    arranque = await api.post(f"{ORDERS}/{orden.json()['id']}/start", headers=head(admin_csrf))

    assert arranque.status_code == 200, arranque.text
    assert arranque.json()["status"] == "STARTED"


@pytest.mark.asyncio
async def test_regresion_el_guardia_de_pago_sigue_mandando_primero(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PAYMENT_GATE_REGRESSION: 0.

    Con cotizacion impagada Y muestra sin aprobar, el que responde es el de
    pago: va antes en el orden del codigo, y ese orden es el que decide que
    mensaje recibe quien pulsa.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_regpay")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    orden = await crear_orden(
        api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert orden.status_code == 201, orden.text

    arranque = await api.post(f"{ORDERS}/{orden.json()['id']}/start", headers=head(admin_csrf))

    assert arranque.status_code == 409, arranque.text
    assert arranque.json()["error"]["code"] == "PRODUCTION_ORDER_QUOTATION_NOT_PAID"
