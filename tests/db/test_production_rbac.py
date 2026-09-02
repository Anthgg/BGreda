"""Fase 009J — quien puede mover el taller, y quien no.

Antes de esta fase la afirmacion «un OPERATOR no puede consumir inventario»
descansaba solo en leer `AdminUserDep` en el codigo: el fixture `operator_csrf`
se usaba en nueve modulos y en ninguno tocaba produccion. Una frontera de
seguridad sin una sola prueba ejecutada.

La regla que se fija aqui es la que el negocio decidio: **el taller ejecuta, la
administracion decide.** Quien esta en el taller prepara receta, ajusta
existencia y lleva una orden de principio a fin. Anular no: anular deshace un
compromiso de fabricacion y ademas ocupa para siempre la cotizacion de origen,
que no admite una segunda orden.

Lo que se comprueba en los casos negativos no es solo el 403. Es que detras del
403 no quede NADA: ni estado cambiado, ni movimiento, ni saldo tocado, ni un
evento de auditoria que afirme una transicion que no ocurrio.
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
from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate
from tests.db.test_production_orders_api import (
    ORDERS,
    confirmar,
    crear_orden,
    escenario,
)
from tests.db.test_production_start import _preparada
from tests.db.test_quotation_builder_api import head

INVENTARIO = "/api/v1/inventory"
PREPARACIONES = "/api/v1/recipe-preparations"


async def como_operario(api: httpx.AsyncClient) -> str:
    """Cambia la sesion del cliente a OPERATOR y devuelve su token CSRF.

    Se hace explicito y a mitad de la prueba en vez de pedir el fixture
    `operator_csrf` junto al de admin: los dos autentican EL MISMO cliente, asi
    que pedirlos juntos deja la sesion en manos del orden de resolucion de
    fixtures. El montaje va como administrador y la accion que se juzga, como
    operario; de esta forma se ve cual es cual al leerlo.
    """
    return await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)


async def _foto(db_session: AsyncSession) -> tuple[int, list[Decimal], int]:
    """Movimientos, saldos y eventos de auditoria: todo lo que un 403 no puede tocar."""
    db_session.expire_all()
    movimientos = int(await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0)
    saldos = list(
        (await db_session.execute(select(StockBalance.quantity).order_by(StockBalance.id)))
        .scalars()
        .all()
    )
    eventos = int(
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.entity_type == "production_order")
        )
        or 0
    )
    return movimientos, saldos, eventos


# ---------------------------------------------------------------------------
# Lo que el taller SI puede hacer
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_operario_lleva_la_orden_de_principio_a_fin(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """OPERATOR_REQUIRED_PRODUCTION_ACTIONS.

    Crear, arrancar y completar. Es la cadena entera de ejecucion, y si le
    faltara un solo eslabon el taller tendria que ir a buscar a un
    administrador a mitad de la fabricacion.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_rbac_op")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])

    operator_csrf = await como_operario(api)
    creada = await crear_orden(
        api, operator_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert creada.status_code == 201, creada.text
    orden_id = creada.json()["id"]

    arrancada = await api.post(f"{ORDERS}/{orden_id}/start", headers=head(operator_csrf))
    assert arrancada.status_code == 200, arrancada.text
    assert arrancada.json()["status"] == "STARTED"

    completada = await api.post(f"{ORDERS}/{orden_id}/complete", headers=head(operator_csrf))
    assert completada.status_code == 200, completada.text
    assert completada.json()["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_el_operario_prepara_receta_y_ajusta_existencia(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Sin estos dos, arrancar una orden no sirve de nada.

    El material que consume la produccion sale de una preparacion, y la
    preparacion sale de materia prima que alguien carga. Dar `start` sin dar
    estos dos dejaria al taller pudiendo gastar barniz y sin poder fabricarlo.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_rbac_prep")

    operator_csrf = await como_operario(api)
    ajuste = await api.post(
        f"{INVENTARIO}/adjustments",
        json={
            "product_id": datos["prepared_product_id"],
            "location_id": datos["location_id"],
            "quantity": "100",
            "reason": "Carga del taller",
        },
        headers=head(operator_csrf),
    )
    assert ajuste.status_code == 201, ajuste.text

    preparacion = await api.post(
        PREPARACIONES,
        json={
            "recipe_version_id": datos["receta"]["current_version"]["id"],
            "location_id": datos["location_id"],
            "total_dry_weight_g": "100",
            "water_amount_ml": "50",
            "final_yield_ml": "120",
            "idempotency_key": "rbac-operario-prepara-001",
        },
        headers=head(operator_csrf),
    )
    assert preparacion.status_code in (200, 201), preparacion.text


@pytest.mark.asyncio
async def test_el_operario_lee_la_orden_y_su_hoja_de_taller(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Leer y escanear el QR: sin esto no puede ni saber que fabricar."""
    datos = await _preparada(api, admin_csrf, db_session, suffix="_rbac_lee")
    orden = datos["orden"]

    await como_operario(api)
    for ruta in (ORDERS, f"{ORDERS}/{orden['id']}", f"{ORDERS}/{orden['id']}/document"):
        respuesta = await api.get(ruta)
        assert respuesta.status_code == 200, f"{ruta} -> {respuesta.text}"

    escaneada = await api.get(f"{ORDERS}/scan/{orden['qr_token']}")
    assert escaneada.status_code == 200, escaneada.text


# ---------------------------------------------------------------------------
# Lo que el taller NO puede hacer
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_operario_no_puede_anular_una_orden(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CANCEL sigue siendo de ADMIN, y el 403 no deja rastro.

    Anular no es ejecucion sino deshacer un compromiso ya tomado, y ademas deja
    la cotizacion de origen ocupada para siempre: su `quotation_id` es UNICO y
    no admite una segunda orden. Es una decision administrativa.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_rbac_anula")
    orden_id = datos["orden"]["id"]
    antes = await _foto(db_session)

    operator_csrf = await como_operario(api)
    respuesta = await api.post(f"{ORDERS}/{orden_id}/cancel", headers=head(operator_csrf))

    assert respuesta.status_code == 403, respuesta.text
    assert respuesta.json()["error"]["code"] == "AUTH_INSUFFICIENT_ROLE"

    db_session.expire_all()
    orden = await db_session.get(ProductionOrder, orden_id)
    assert orden is not None
    assert orden.status is ProductionOrderStatus.CREATED, "el estado no puede haber cambiado"
    assert orden.cancelled_at is None
    assert await _foto(db_session) == antes, "un 403 no puede dejar rastro de ninguna clase"


@pytest.mark.asyncio
async def test_el_operario_no_crea_almacenes(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Abrir un almacen nuevo es decision administrativa, no de taller."""
    operator_csrf = await como_operario(api)
    respuesta = await api.post(
        f"{INVENTARIO}/locations",
        json={"name": "Almacen del operario"},
        headers=head(operator_csrf),
    )
    assert respuesta.status_code == 403, respuesta.text


@pytest.mark.asyncio
async def test_el_operario_no_toca_lo_comercial(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La frontera que 009J NO mueve.

    Ejecutar la fabricacion no es decidir el precio. Confirmar una cotizacion
    la vuelve inmutable y compromete un importe con un cliente; eso sigue
    siendo de quien lleva la parte comercial.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_rbac_comercial")
    borrador = datos["quotation"]

    operator_csrf = await como_operario(api)
    confirmar_op = await api.post(
        f"/api/v1/quotation-builder/{borrador['id']}/confirm",
        json={"expected_updated_at": borrador["updated_at"]},
        headers=head(operator_csrf),
    )
    assert confirmar_op.status_code == 403, confirmar_op.text

    db_session.expire_all()
    from app.models.quotations import Quotation, QuotationStatus

    fila = await db_session.get(Quotation, borrador["id"])
    assert fila is not None
    assert fila.status is QuotationStatus.DRAFT, "un 403 no puede confirmar nada"


# ---------------------------------------------------------------------------
# Sin sesion no se mueve nada
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sin_sesion_ninguna_mutacion_de_produccion_pasa(
    api: httpx.AsyncClient, api_app: Any, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DEFAULT_DENY.

    Se usa un cliente NUEVO en vez de intentar borrarle la cookie al de la
    sesion: httpx guarda las cookies en un tarro propio y «quitarla» pasando
    cabeceras vacias no siempre la quita, con lo que la prueba pasaria por
    seguir autenticada y no por rechazar a nadie.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_rbac_anon")
    orden_id = datos["orden"]["id"]
    antes = await _foto(db_session)

    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anonimo:
        for ruta in ("start", "complete", "cancel"):
            respuesta = await anonimo.post(f"{ORDERS}/{orden_id}/{ruta}")
            assert respuesta.status_code in (401, 403), f"{ruta} -> {respuesta.text}"

    db_session.expire_all()
    orden = await db_session.get(ProductionOrder, orden_id)
    assert orden is not None
    assert orden.status is ProductionOrderStatus.CREATED
    assert await _foto(db_session) == antes


# ---------------------------------------------------------------------------
# El administrador conserva todo
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_administrador_conserva_las_cuatro_transiciones(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """ADMIN_REQUIRED_PRODUCTION_ACTIONS.

    Ampliar lo que puede el taller no puede haberle quitado nada a quien ya lo
    podia todo.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_rbac_admin")
    anulada = await api.post(f"{ORDERS}/{datos['orden']['id']}/cancel", headers=head(admin_csrf))
    assert anulada.status_code == 200, anulada.text
    assert anulada.json()["status"] == "CANCELLED"

    otros = await _preparada(api, admin_csrf, db_session, suffix="_rbac_admin2")
    arrancada = await api.post(f"{ORDERS}/{otros['orden']['id']}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text
    completada = await api.post(
        f"{ORDERS}/{otros['orden']['id']}/complete", headers=head(admin_csrf)
    )
    assert completada.status_code == 200, completada.text
