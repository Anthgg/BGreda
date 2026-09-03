"""Fase 009K — el dominio de prototipos, contra PostgreSQL de verdad.

Lo que se prueba aqui no es que los metodos devuelvan lo que dicen: es que
detras de cada bloqueo no quede NADA. Un arranque rechazado que ya descontó
medio material, o una auditoria que afirma una transicion que no ocurrio, son
peores que no bloquear, porque mienten sobre lo que paso.

Por eso cada caso negativo compara una FOTO de la base —movimientos, saldos,
salidas de prototipo y eventos— tomada antes y despues, y la relee desde la
sesion expirada para no creerle a objetos que quedaron en memoria de una
transaccion que se deshizo.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.models.inventory import StockBalance, StockMovement
from app.models.prototypes import Prototype, PrototypeApproval, PrototypeStatus
from tests.db.conftest import (
    OPERATOR_EMAIL,
    OPERATOR_PASSWORD,
    TEST_EMAIL,
    TEST_PASSWORD,
    authenticate,
)
from tests.db.test_masters_api import create_category, create_product
from tests.db.test_production_orders_api import (
    confirmar,
    dar_existencia,
    escenario,
    pagar,
)
from tests.db.test_quotation_builder_api import head

PROTOTYPES = "/api/v1/prototypes"


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
async def _foto(db_session: AsyncSession) -> tuple[int, int, list[Decimal], int]:
    """Todo lo que un bloqueo NO puede haber tocado.

    Se expira la sesion a proposito: despues de una transaccion deshecha, los
    objetos que quedaron en memoria pueden seguir diciendo lo que se intento
    escribir, y creerles convertiria la prueba en una comprobacion de la cache.
    """
    db_session.expire_all()
    movimientos = int(await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0)
    salidas = int(
        await db_session.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(StockMovement.movement_type == "PROTOTYPE_OUT")
        )
        or 0
    )
    saldos = list(
        (await db_session.execute(select(StockBalance.quantity).order_by(StockBalance.id)))
        .scalars()
        .all()
    )
    eventos = int(
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.entity_type == "prototype")
        )
        or 0
    )
    return movimientos, salidas, saldos, eventos


async def _saldo(db_session: AsyncSession, product_id: int, location_id: int) -> Decimal:
    db_session.expire_all()
    valor = await db_session.scalar(
        select(StockBalance.quantity).where(
            StockBalance.product_id == product_id,
            StockBalance.location_id == location_id,
        )
    )
    return Decimal(valor) if valor is not None else Decimal(0)


async def _prototipo(db_session: AsyncSession, prototype_id: int) -> Prototype:
    """Relee desde la base, nunca desde el objeto de la peticion anterior."""
    db_session.expire_all()
    row = await db_session.get(Prototype, prototype_id)
    assert row is not None
    return row


async def _material(
    api: httpx.AsyncClient, csrf: str, *, nombre: str, tipo: str = "RAW_MATERIAL"
) -> dict[str, Any]:
    """Un insumo real del catalogo, del tipo que se pida."""
    categoria = await create_category(api, csrf, f"Prototipos {nombre}")
    respuesta = await create_product(
        api,
        csrf,
        product_category_id=categoria["id"],
        product_type=tipo,
        name=nombre,
        base_uom_code="g",
    )
    assert respuesta.status_code == 201, respuesta.text
    return dict(respuesta.json())


async def crear_prototipo(api: httpx.AsyncClient, csrf: str, **payload: Any) -> httpx.Response:
    cuerpo: dict[str, Any] = {"name": "E2E-009K muestra"}
    cuerpo.update(payload)
    return await api.post(PROTOTYPES, json=cuerpo, headers=head(csrf))


async def _como_operario(api: httpx.AsyncClient) -> str:
    return await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)


async def _como_admin(api: httpx.AsyncClient) -> str:
    return await authenticate(api, email=TEST_EMAIL, password=TEST_PASSWORD)


async def _muestra_lista(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    *,
    suffix: str,
    gramos: str = "30",
    pagada: bool = True,
    existencia: str = "1000",
) -> dict[str, Any]:
    """Una muestra con todo lo necesario para arrancar (o casi).

    `pagada=False` deja la cotizacion confirmada e impagada, que es la unica
    diferencia entre poder fabricar y no poder.
    """
    datos = await escenario(api, csrf, db_session, suffix=suffix)
    confirmada = await confirmar(api, csrf, datos["quotation"])
    if pagada:
        await pagar(api, csrf, confirmada["id"])

    barro = await _material(api, csrf, nombre=f"Arcilla E2E{suffix}")
    await dar_existencia(
        api, csrf, product_id=barro["id"], location_id=datos["location_id"], cantidad=existencia
    )

    creado = await crear_prototipo(
        api,
        csrf,
        name=f"E2E-009K{suffix}",
        quotation_id=confirmada["id"],
        stock_location_id=datos["location_id"],
        materials=[{"product_id": barro["id"], "quantity": gramos}],
    )
    assert creado.status_code == 201, creado.text
    return {
        **datos,
        "confirmada": confirmada,
        "barro": barro,
        "prototipo": creado.json(),
        "gramos": Decimal(gramos),
    }


# ---------------------------------------------------------------------------
# 01 — Muestra suelta
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_01_muestra_sin_cotizacion_se_registra_pero_no_arranca(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Se prototipa antes de que exista pedido, y hasta sin producto.

    Pero sin cotizacion no hay nada que cobrar, y sin cobro no se gasta
    material. No falta un dato: falta el pedido.
    """
    antes = await _foto(db_session)

    creado = await crear_prototipo(api, admin_csrf, name="E2E-009K suelta")
    assert creado.status_code == 201, creado.text
    cuerpo = creado.json()
    assert cuerpo["code"].startswith("PRT-")
    assert cuerpo["status"] == "CREATED"
    assert cuerpo["approval"] == "PENDING"
    assert cuerpo["quotation_id"] is None
    assert cuerpo["product_id"] is None

    codigos = {issue["code"] for issue in cuerpo["readiness"]["issues"]}
    assert "NO_QUOTATION" in codigos

    arranque = await api.post(f"{PROTOTYPES}/{cuerpo['id']}/start", headers=head(admin_csrf))
    assert arranque.status_code == 409, arranque.text
    assert arranque.json()["error"]["code"] == "PROTOTYPE_NOT_READY"

    prototipo = await _prototipo(db_session, cuerpo["id"])
    assert prototipo.status is PrototypeStatus.CREATED
    assert prototipo.started_at is None

    movimientos, salidas, saldos, _eventos = await _foto(db_session)
    assert (movimientos, salidas, saldos) == antes[:3]


# ---------------------------------------------------------------------------
# 02 — Cotizacion impagada
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_02_cotizacion_impagada_no_arranca_la_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El eje de pago es el de la cotizacion (009H.1). No hay uno propio."""
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_unpaid", pagada=False)
    antes = await _foto(db_session)

    arranque = await api.post(
        f"{PROTOTYPES}/{datos['prototipo']['id']}/start", headers=head(admin_csrf)
    )

    assert arranque.status_code == 409, arranque.text
    codigos = {detalle["code"] for detalle in arranque.json()["error"]["details"]}
    assert "QUOTATION_UNPAID" in codigos

    prototipo = await _prototipo(db_session, datos["prototipo"]["id"])
    assert prototipo.status is PrototypeStatus.CREATED
    assert prototipo.started_at is None
    assert await _foto(db_session) == antes


# ---------------------------------------------------------------------------
# 03 — Arranque normal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_03_cobrada_arranca_y_descuenta_lo_justo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Consume EXACTAMENTE lo que alguien eligio, y lo deja atado a la muestra."""
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_ok")
    barro_id = datos["barro"]["id"]
    antes = await _saldo(db_session, barro_id, datos["location_id"])

    arranque = await api.post(
        f"{PROTOTYPES}/{datos['prototipo']['id']}/start", headers=head(admin_csrf)
    )

    assert arranque.status_code == 200, arranque.text
    assert arranque.json()["status"] == "STARTED"
    assert arranque.json()["started_at"] is not None

    assert await _saldo(db_session, barro_id, datos["location_id"]) == antes - datos["gramos"]

    db_session.expire_all()
    movimientos = (
        (
            await db_session.execute(
                select(StockMovement).where(StockMovement.prototype_id == datos["prototipo"]["id"])
            )
        )
        .scalars()
        .all()
    )
    assert len(movimientos) == 1
    movimiento = movimientos[0]
    assert movimiento.movement_type == "PROTOTYPE_OUT"
    assert movimiento.quantity == -datos["gramos"]
    assert movimiento.product_id == barro_id
    assert movimiento.uom_code == "g"


# ---------------------------------------------------------------------------
# 04 — Idempotencia
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_04_arrancar_dos_veces_no_gasta_dos_veces(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_idem")
    prototipo_id = datos["prototipo"]["id"]

    primera = await api.post(f"{PROTOTYPES}/{prototipo_id}/start", headers=head(admin_csrf))
    assert primera.status_code == 200, primera.text
    foto = await _foto(db_session)

    segunda = await api.post(f"{PROTOTYPES}/{prototipo_id}/start", headers=head(admin_csrf))

    assert segunda.status_code == 200, segunda.text
    assert segunda.json()["started_at"] == primera.json()["started_at"]
    assert await _foto(db_session) == foto


# ---------------------------------------------------------------------------
# 05 — Dos arranques a la vez
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_05_dos_arranques_simultaneos_consumen_una_sola_vez(
    api: httpx.AsyncClient, api_app: Any, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_CONCURRENT_START_EXACTLY_ONCE.

    Dos clientes distintos, no dos llamadas seguidas: con un solo cliente httpx
    las peticiones se serializan por el pool y la prueba pasaria sin haber
    probado la concurrencia.
    """
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_conc")
    prototipo_id = datos["prototipo"]["id"]
    barro_id = datos["barro"]["id"]
    antes = await _saldo(db_session, barro_id, datos["location_id"])

    async def arrancar() -> int:
        transporte = httpx.ASGITransport(app=api_app)
        async with httpx.AsyncClient(transport=transporte, base_url="http://testserver") as cliente:
            csrf = await authenticate(cliente, email=TEST_EMAIL, password=TEST_PASSWORD)
            respuesta = await cliente.post(f"{PROTOTYPES}/{prototipo_id}/start", headers=head(csrf))
            return respuesta.status_code

    codigos = await asyncio.gather(arrancar(), arrancar())

    assert all(codigo in (200, 409) for codigo in codigos), codigos
    assert 200 in codigos, "ninguna de las dos llego a arrancar"

    assert await _saldo(db_session, barro_id, datos["location_id"]) == antes - datos["gramos"]

    db_session.expire_all()
    salidas = int(
        await db_session.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(StockMovement.prototype_id == prototipo_id)
        )
        or 0
    )
    assert salidas == 1, "el material se descontó dos veces"


# ---------------------------------------------------------------------------
# 06 — Atomicidad
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_06_si_falta_un_material_no_se_gasta_ninguno(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_ATOMIC_STOCK. Todo o nada, y «nada» significa nada."""
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_atom", existencia="1000")

    escaso = await _material(api, admin_csrf, nombre="Pasta escasa E2E")
    await dar_existencia(
        api, admin_csrf, product_id=escaso["id"], location_id=datos["location_id"], cantidad="5"
    )
    materiales = await api.put(
        f"{PROTOTYPES}/{datos['prototipo']['id']}/materials",
        json={
            "materials": [
                {"product_id": datos["barro"]["id"], "quantity": "50"},
                {"product_id": escaso["id"], "quantity": "200"},
            ]
        },
        headers=head(admin_csrf),
    )
    assert materiales.status_code == 200, materiales.text

    barro_antes = await _saldo(db_session, datos["barro"]["id"], datos["location_id"])
    escaso_antes = await _saldo(db_session, escaso["id"], datos["location_id"])
    foto = await _foto(db_session)

    arranque = await api.post(
        f"{PROTOTYPES}/{datos['prototipo']['id']}/start", headers=head(admin_csrf)
    )

    assert arranque.status_code == 409, arranque.text
    codigos = {detalle["code"] for detalle in arranque.json()["error"]["details"]}
    assert "INSUFFICIENT_STOCK" in codigos

    # El que SI alcanzaba tampoco se toca.
    assert await _saldo(db_session, datos["barro"]["id"], datos["location_id"]) == barro_antes
    assert await _saldo(db_session, escaso["id"], datos["location_id"]) == escaso_antes
    assert await _foto(db_session) == foto

    prototipo = await _prototipo(db_session, datos["prototipo"]["id"])
    assert prototipo.status is PrototypeStatus.CREATED
    assert prototipo.started_at is None


# ---------------------------------------------------------------------------
# 07 — Material preparado
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_07_un_preparado_se_consume_el_y_no_sus_componentes(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_PREPARED_COMPONENT_DOUBLE_CONSUMPTION: 0.

    El preparado ya se pago con su materia prima cuando se preparo. Volver a
    descontar los componentes al usarlo cobraria el mismo barro dos veces.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_prep")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    await pagar(api, admin_csrf, confirmada["id"])

    preparado_id = datos["prepared_product_id"]
    componente_id = datos["receta"]["current_version"]["lines"][0]["component_product_id"]
    await dar_existencia(
        api, admin_csrf, product_id=componente_id, location_id=datos["location_id"], cantidad="900"
    )

    creado = await crear_prototipo(
        api,
        admin_csrf,
        name="E2E-009K preparado",
        quotation_id=confirmada["id"],
        stock_location_id=datos["location_id"],
        materials=[{"product_id": preparado_id, "quantity": "40"}],
    )
    assert creado.status_code == 201, creado.text

    preparado_antes = await _saldo(db_session, preparado_id, datos["location_id"])
    componente_antes = await _saldo(db_session, componente_id, datos["location_id"])

    arranque = await api.post(f"{PROTOTYPES}/{creado.json()['id']}/start", headers=head(admin_csrf))
    assert arranque.status_code == 200, arranque.text

    assert await _saldo(db_session, preparado_id, datos["location_id"]) == preparado_antes - 40
    assert await _saldo(db_session, componente_id, datos["location_id"]) == componente_antes


# ---------------------------------------------------------------------------
# 08 — Completar
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_08_completar_no_vuelve_a_consumir(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_comp")
    prototipo_id = datos["prototipo"]["id"]
    await api.post(f"{PROTOTYPES}/{prototipo_id}/start", headers=head(admin_csrf))
    foto = await _foto(db_session)

    completado = await api.post(f"{PROTOTYPES}/{prototipo_id}/complete", headers=head(admin_csrf))

    assert completado.status_code == 200, completado.text
    assert completado.json()["status"] == "COMPLETED"
    assert completado.json()["completed_at"] is not None
    # La foto incluye eventos de auditoria, que SI crecen al completar.
    movimientos, salidas, saldos, _ = await _foto(db_session)
    assert (movimientos, salidas, saldos) == foto[:3]


# ---------------------------------------------------------------------------
# 09 y 10 — Decidir antes de fabricar
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_09_10_no_se_decide_una_muestra_que_no_existe(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession, decision: str
) -> None:
    """Aprobar algo que nadie fabrico afirmaria que alguien lo vio."""
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix=f"_dec{decision[:3]}")
    prototipo_id = datos["prototipo"]["id"]

    respuesta = await api.post(
        f"{PROTOTYPES}/{prototipo_id}/{decision}", json={}, headers=head(admin_csrf)
    )

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PROTOTYPE_NOT_DECIDABLE"

    prototipo = await _prototipo(db_session, prototipo_id)
    assert prototipo.approval is PrototypeApproval.PENDING
    assert prototipo.decided_at is None


# ---------------------------------------------------------------------------
# 11 y 12 — Quien decide
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_11_el_operario_no_aprueba(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Aprobar condiciona si un pedido entero se fabrica: es administrativo."""
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_operap")
    prototipo_id = datos["prototipo"]["id"]
    await api.post(f"{PROTOTYPES}/{prototipo_id}/start", headers=head(admin_csrf))
    await api.post(f"{PROTOTYPES}/{prototipo_id}/complete", headers=head(admin_csrf))

    operario_csrf = await _como_operario(api)
    respuesta = await api.post(
        f"{PROTOTYPES}/{prototipo_id}/approve", json={}, headers=head(operario_csrf)
    )

    assert respuesta.status_code == 403, respuesta.text
    prototipo = await _prototipo(db_session, prototipo_id)
    assert prototipo.approval is PrototypeApproval.PENDING
    assert prototipo.decided_at is None


@pytest.mark.asyncio
async def test_12_el_administrador_aprueba(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_adminap")
    prototipo_id = datos["prototipo"]["id"]
    await api.post(f"{PROTOTYPES}/{prototipo_id}/start", headers=head(admin_csrf))
    await api.post(f"{PROTOTYPES}/{prototipo_id}/complete", headers=head(admin_csrf))

    respuesta = await api.post(
        f"{PROTOTYPES}/{prototipo_id}/approve",
        json={"note": "Medidas conformes"},
        headers=head(admin_csrf),
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["approval"] == "APPROVED"
    assert respuesta.json()["decided_at"] is not None

    db_session.expire_all()
    eventos = (
        (
            await db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "prototype",
                    AuditEvent.entity_id == str(prototipo_id),
                    AuditEvent.field == "approval",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(eventos) == 1
    assert eventos[0].old_value == "PENDING"
    assert eventos[0].new_value == "APPROVED"


# ---------------------------------------------------------------------------
# 13, 14 y 15 — Anular
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_13_el_operario_no_anula(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_opercan")
    operario_csrf = await _como_operario(api)

    respuesta = await api.post(
        f"{PROTOTYPES}/{datos['prototipo']['id']}/cancel", headers=head(operario_csrf)
    )

    assert respuesta.status_code == 403, respuesta.text
    prototipo = await _prototipo(db_session, datos["prototipo"]["id"])
    assert prototipo.status is PrototypeStatus.CREATED


@pytest.mark.asyncio
async def test_14_el_administrador_anula_una_que_no_gasto_nada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_admincan")
    foto = await _foto(db_session)

    respuesta = await api.post(
        f"{PROTOTYPES}/{datos['prototipo']['id']}/cancel", headers=head(admin_csrf)
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["status"] == "CANCELLED"
    prototipo = await _prototipo(db_session, datos["prototipo"]["id"])
    assert prototipo.cancelled_at is not None
    assert prototipo.approval is PrototypeApproval.PENDING
    movimientos, salidas, saldos, _ = await _foto(db_session)
    assert (movimientos, salidas, saldos) == foto[:3]


@pytest.mark.asyncio
async def test_15_una_muestra_arrancada_no_se_anula(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Anularla no devuelve el barro al saco."""
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_cancstart")
    prototipo_id = datos["prototipo"]["id"]
    await api.post(f"{PROTOTYPES}/{prototipo_id}/start", headers=head(admin_csrf))
    foto = await _foto(db_session)

    respuesta = await api.post(f"{PROTOTYPES}/{prototipo_id}/cancel", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PROTOTYPE_NOT_CANCELLABLE"
    prototipo = await _prototipo(db_session, prototipo_id)
    assert prototipo.status is PrototypeStatus.STARTED
    movimientos, salidas, saldos, _ = await _foto(db_session)
    assert (movimientos, salidas, saldos) == foto[:3]
