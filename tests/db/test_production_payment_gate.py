"""Fase 009H.1 — no se gasta material de una cotizacion que no consta cobrada.

Hasta aqui cobrar y fabricar eran ejes independientes, y lo eran a proposito:
atarlos habria parado el taller por una gestion administrativa. Lo que 009H.1
cambia no es esa idea sino DONDE esta la linea.

    preparar la orden  →  sin cobro      correlativo, hoja, QR, disponibilidad
    arrancarla         →  exige PAID     es el unico punto que gasta material

Lo que se comprueba en el caso negativo no es el 409. Es que detras del 409 no
quede NADA: ni saldo tocado, ni movimiento, ni `started_at`, ni un evento de
auditoria que afirme una transicion que no ocurrio. Un bloqueo que deja rastro
de exito es peor que no bloquear, porque miente sobre lo que paso.

**El nulo tambien bloquea.** El eje admite tres valores y el nulo significa «no
consta» —lo que hay en todo lo anterior a 009H—, no «pagada». En produccion son
17 de las 19 confirmadas: dejarlo pasar habria vuelto la regla inoperante el
mismo dia que se escribio.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.models.inventory import StockBalance, StockMovement
from app.models.production import ProductionOrder, ProductionOrderStatus
from app.models.quotations import Quotation
from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate
from tests.db.test_production_orders_api import (
    ORDERS,
    confirmar,
    crear_orden,
    escenario,
    pagar,
)
from tests.db.test_quotation_builder_api import head

BUILDER = "/api/v1/quotation-builder"


async def _foto(db_session: AsyncSession) -> tuple[int, list[Decimal], int, int]:
    """Todo lo que un arranque bloqueado NO puede haber tocado."""
    db_session.expire_all()
    movimientos = int(await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0)
    saldos = list(
        (await db_session.execute(select(StockBalance.quantity).order_by(StockBalance.id)))
        .scalars()
        .all()
    )
    salidas = int(
        await db_session.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(StockMovement.movement_type == "PRODUCTION_OUT")
        )
        or 0
    )
    eventos = int(
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.entity_type == "production_order")
        )
        or 0
    )
    return movimientos, saldos, salidas, eventos


async def _orden_impagada(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, suffix: str
) -> dict[str, Any]:
    """Cotizacion CONFIRMED + UNPAID con su orden ya creada y lista de material."""
    datos = await escenario(api, csrf, db_session, suffix=suffix)
    confirmada = await confirmar(api, csrf, datos["quotation"])
    assert confirmada["payment_status"] == "UNPAID"
    creada = await crear_orden(
        api, csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert creada.status_code == 201, creada.text
    return {**datos, "confirmada": confirmada, "orden": creada.json()}


async def _como_operario(api: httpx.AsyncClient) -> str:
    """Cambia la sesion del cliente a OPERATOR y devuelve su token CSRF."""
    return await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)


# ---------------------------------------------------------------------------
# Lo que SI se puede hacer sin haber cobrado
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sin_cobrar_la_orden_se_prepara_entera(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """UNPAID_ORDER_CAN_BE_CREATED.

    El taller puede tenerlo todo listo mientras administracion cobra: el
    correlativo, la hoja impresa con su QR y la disponibilidad de material
    calculada. Nada de eso gasta un gramo.
    """
    datos = await _orden_impagada(api, admin_csrf, db_session, "_prep")
    orden_id = datos["orden"]["id"]

    leida = await api.get(f"{ORDERS}/{orden_id}")
    assert leida.status_code == 200, leida.text
    assert leida.json()["status"] == "CREATED"
    # La disponibilidad sigue siendo informativa: mide MATERIAL, no cobro.
    assert leida.json()["readiness"]["ready"] is True
    assert leida.json()["quotation_payment_status"] == "UNPAID"

    documento = await api.get(f"{ORDERS}/{orden_id}/document")
    assert documento.status_code == 200, documento.text
    assert documento.content.startswith(b"%PDF-")


# ---------------------------------------------------------------------------
# Lo que NO
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sin_cobrar_no_arranca_y_no_queda_rastro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """UNPAID_ORDER_CAN_START: NO, y las cuatro mutaciones a cero.

    Se mira la base entera antes y despues: movimientos, saldos, salidas de
    produccion y eventos de la orden. Comprobar solo el codigo de respuesta
    dejaria pasar un bloqueo que descuenta primero y falla despues.
    """
    datos = await _orden_impagada(api, admin_csrf, db_session, "_block")
    orden_id = datos["orden"]["id"]
    antes = await _foto(db_session)

    respuesta = await api.post(f"{ORDERS}/{orden_id}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_QUOTATION_NOT_PAID"

    db_session.expire_all()
    orden = await db_session.get(ProductionOrder, orden_id)
    assert orden is not None
    assert orden.status is ProductionOrderStatus.CREATED
    assert orden.started_at is None
    assert await _foto(db_session) == antes


@pytest.mark.asyncio
async def test_sin_registro_de_pago_tampoco_arranca(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El nulo bloquea igual que UNPAID, y es el caso que mas hay.

    «No consta» no es «pagada». En produccion 17 de las 19 confirmadas estan
    asi, todas anteriores a 009H: dejarlas arrancar habria vuelto la regla
    inoperante el mismo dia. La salida es registrar el cobro, no una excepcion.
    """
    datos = await _orden_impagada(api, admin_csrf, db_session, "_nulo")
    await db_session.execute(
        Quotation.__table__.update()
        .where(Quotation.__table__.c.id == datos["confirmada"]["id"])
        .values(payment_status=None, paid_at=None)
    )
    await db_session.commit()
    antes = await _foto(db_session)

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_QUOTATION_NOT_PAID"
    assert await _foto(db_session) == antes


@pytest.mark.asyncio
async def test_un_arranque_bloqueado_por_pago_no_finge_una_transicion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """UNPAID_BLOCKED_START_FAKE_TRANSITION_AUDIT: 0.

    La auditoria no puede decir CREATED -> STARTED de algo que nunca arranco.
    Quien lea ese registro dentro de un ano no tiene forma de distinguir un
    apunte falso de uno verdadero.
    """
    datos = await _orden_impagada(api, admin_csrf, db_session, "_audit")
    orden_id = datos["orden"]["id"]

    antes = (
        (
            await db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "production_order",
                    AuditEvent.entity_id == str(orden_id),
                )
            )
        )
        .scalars()
        .all()
    )

    respuesta = await api.post(f"{ORDERS}/{orden_id}/start", headers=head(admin_csrf))
    assert respuesta.status_code == 409, respuesta.text

    db_session.expire_all()
    despues = (
        (
            await db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "production_order",
                    AuditEvent.entity_id == str(orden_id),
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(despues) == len(antes)
    for evento in despues:
        campos = evento.event_metadata or {}
        assert campos.get("status") != "STARTED", "hay un apunte de un arranque que no ocurrio"


# ---------------------------------------------------------------------------
# Cobrar habilita; no produce
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_marcar_pagada_no_arranca_la_produccion_sola(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MARK_PAID_AUTO_STARTS_PRODUCTION: NO. MARK_PAID_STOCK_MUTATION: 0.

    Cobrar habilita el arranque; no lo ejecuta. Son dos decisiones de dos
    personas distintas —administracion cobra, el taller fabrica— y juntarlas
    haria que registrar un cobro gastase material sin que nadie lo pidiera.
    """
    datos = await _orden_impagada(api, admin_csrf, db_session, "_habilita")
    antes = await _foto(db_session)

    await pagar(api, admin_csrf, datos["confirmada"]["id"])

    db_session.expire_all()
    orden = await db_session.get(ProductionOrder, datos["orden"]["id"])
    assert orden is not None
    assert orden.status is ProductionOrderStatus.CREATED
    assert orden.started_at is None
    assert await _foto(db_session) == antes


@pytest.mark.asyncio
async def test_preparar_antes_de_cobrar_y_arrancar_despues(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CREATE_BEFORE_PAYMENT_START_AFTER_PAYMENT_FLOW.

    El recorrido entero del negocio en una sola prueba: se prepara sin cobrar,
    se intenta arrancar y no se puede, se cobra, la orden sigue donde estaba, y
    entonces arranca y consume EXACTAMENTE una vez.
    """
    datos = await _orden_impagada(api, admin_csrf, db_session, "_flujo")
    orden_id = datos["orden"]["id"]

    bloqueado = await api.post(f"{ORDERS}/{orden_id}/start", headers=head(admin_csrf))
    assert bloqueado.status_code == 409, bloqueado.text

    await pagar(api, admin_csrf, datos["confirmada"]["id"])

    db_session.expire_all()
    orden = await db_session.get(ProductionOrder, orden_id)
    assert orden is not None
    assert orden.status is ProductionOrderStatus.CREATED, "cobrar no mueve la orden"

    movimientos_antes, _saldos, salidas_antes, _eventos = await _foto(db_session)
    arrancada = await api.post(f"{ORDERS}/{orden_id}/start", headers=head(admin_csrf))

    assert arrancada.status_code == 200, arrancada.text
    assert arrancada.json()["status"] == "STARTED"
    assert arrancada.json()["started_at"] is not None

    movimientos, _s, salidas, _e = await _foto(db_session)
    assert movimientos > movimientos_antes
    assert salidas == salidas_antes + 1, "una salida de produccion, ni cero ni dos"


@pytest.mark.asyncio
async def test_arrancar_dos_veces_tras_cobrar_sigue_consumiendo_una_vez(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCTION_START_IDEMPOTENCY, que el guardia de pago no puede romper.

    El guardia va antes del atajo de idempotencia en el orden del codigo, pero
    despues de la comprobacion de estado: una orden ya STARTED sale por el
    atajo sin volver a mirar el cobro. Si lo mirara, cambiar el pago DESPUES de
    arrancar rompería una orden que ya consumio, y eso es incoherente.
    """
    datos = await _orden_impagada(api, admin_csrf, db_session, "_idem")
    orden_id = datos["orden"]["id"]
    await pagar(api, admin_csrf, datos["confirmada"]["id"])

    primera = await api.post(f"{ORDERS}/{orden_id}/start", headers=head(admin_csrf))
    assert primera.status_code == 200, primera.text
    foto = await _foto(db_session)

    segunda = await api.post(f"{ORDERS}/{orden_id}/start", headers=head(admin_csrf))

    assert segunda.status_code == 200, segunda.text
    assert segunda.json()["status"] == "STARTED"
    assert segunda.json()["started_at"] == primera.json()["started_at"]
    assert await _foto(db_session) == foto


# ---------------------------------------------------------------------------
# Nadie se salta la regla
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_administrador_tampoco_se_salta_el_cobro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """ADMIN_BYPASSES_PAYMENT_GATE: NO.

    Tener todo el permiso del mundo no cambia el estado del negocio. Es la
    diferencia entre las dos preguntas: RBAC responde «¿puede esta persona
    arrancar?» y esto responde «¿puede arrancar ESTA orden ahora?».
    """
    datos = await _orden_impagada(api, admin_csrf, db_session, "_admin")

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_QUOTATION_NOT_PAID"


@pytest.mark.asyncio
async def test_el_operario_tampoco(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """OPERATOR_BYPASSES_PAYMENT_GATE: NO.

    El montaje va como administrador y la accion que se juzga, como operario:
    los dos fixtures autentican el MISMO cliente, asi que pedirlos juntos
    dejaria la sesion en manos del orden de resolucion.
    """
    datos = await _orden_impagada(api, admin_csrf, db_session, "_oper")
    antes = await _foto(db_session)

    operario_csrf = await _como_operario(api)
    respuesta = await api.post(
        f"{ORDERS}/{datos['orden']['id']}/start", headers=head(operario_csrf)
    )

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_QUOTATION_NOT_PAID"
    assert await _foto(db_session) == antes


@pytest.mark.asyncio
async def test_el_operario_arranca_en_cuanto_esta_cobrada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """OPERATOR_PAID_START_ALLOWED. La matriz de 009J no se toca.

    El taller ejecuta. Lo que 009H.1 anade no es un permiso menos sino una
    condicion del negocio, y en cuanto se cumple el operario arranca igual que
    antes.
    """
    datos = await _orden_impagada(api, admin_csrf, db_session, "_operok")
    await pagar(api, admin_csrf, datos["confirmada"]["id"])

    operario_csrf = await _como_operario(api)
    respuesta = await api.post(
        f"{ORDERS}/{datos['orden']['id']}/start", headers=head(operario_csrf)
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["status"] == "STARTED"


@pytest.mark.asyncio
async def test_sin_permiso_es_401_y_no_409(
    api: httpx.AsyncClient,
    api_app: Any,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Falta de permiso y falta de cobro son problemas distintos y se dicen distinto.

    Confundirlos manda a quien lo recibe al sitio equivocado: un 403 por una
    factura impagada hace buscar a un administrador que no puede arreglarlo
    dando permisos.

    Con la matriz de 009J no existe ningun rol que NO pueda arrancar —ADMIN y
    OPERATOR pueden—, asi que el unico caso real de «sin permiso» es no tener
    sesion. Se comprueba ese, y que la cotizacion este PAGADA para que el
    rechazo sea inequivocamente por identidad y no por cobro.
    """
    datos = await _orden_impagada(api, admin_csrf, db_session, "_anon")
    await pagar(api, admin_csrf, datos["confirmada"]["id"])
    antes = await _foto(db_session)

    transporte = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transporte, base_url="http://testserver") as anonimo:
        respuesta = await anonimo.post(
            f"{ORDERS}/{datos['orden']['id']}/start",
            json={},
            headers={"Content-Type": "application/json"},
        )

    assert respuesta.status_code in (401, 403), respuesta.text
    assert respuesta.json()["error"]["code"] != "PRODUCTION_ORDER_QUOTATION_NOT_PAID"
    assert await _foto(db_session) == antes
