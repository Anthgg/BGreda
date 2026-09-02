"""Fase 009I.1 — el seguimiento publico, ejercido como lo ejerce un desconocido.

Todo lo de aqui se hace con un cliente SIN sesion, creado aparte del `api`
autenticado. Es la unica forma de probar una superficie publica: con el cliente
de siempre, cualquier prueba pasaria por seguir autenticada y no por estar
permitido.

Lo que se comprueba tiene dos mitades, y la segunda es la que importa:

1. Que funciona: escanear el QR sin cuenta devuelve el estado, y el token
   desaparece de la URL a cambio de una cookie.
2. Que no filtra: en el JSON, en el PDF y en el HTML no aparece ni el almacen,
   ni el material preparado, ni los gramos, ni la cotizacion, ni un
   identificador, ni el propio token. Y que no hay forma de escribir nada.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tracking import TRACKING_COOKIE_NAME, TRACKING_COOKIE_PATH
from app.models.audit import AuditEvent
from app.models.inventory import StockMovement
from app.models.production import ProductionOrder, ProductionOrderStatus
from tests.db.conftest import TEST_EMAIL, TEST_PASSWORD, authenticate
from tests.db.test_production_orders_api import (
    ORDERS,
    confirmar,
    crear_orden,
    escenario,
)
from tests.db.test_quotation_builder_api import head

TRACKING = "/api/v1/tracking/production-orders"


def _texto(contenido: bytes) -> str:
    reader = PdfReader(io.BytesIO(contenido))
    crudo = "\n".join(page.extract_text() or "" for page in reader.pages)
    return " ".join(crudo.split())


@pytest.fixture
async def anonimo(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Un navegador sin cuenta. Nunca inicia sesion en toda la prueba."""
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as cliente:
        yield cliente


async def _orden_arrancada(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, suffix: str
) -> dict[str, object]:
    """Una orden real, ya en marcha, con su token y sus dos piezas."""
    datos = await escenario(api, csrf, db_session, suffix=suffix)
    confirmada = await confirmar(api, csrf, datos["quotation"])
    creada = await crear_orden(
        api, csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert creada.status_code == 201, creada.text
    arrancada = await api.post(f"{ORDERS}/{creada.json()['id']}/start", headers=head(csrf))
    assert arrancada.status_code == 200, arrancada.text
    return dict(arrancada.json())


# ---------------------------------------------------------------------------
# Que funciona sin cuenta
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_escanear_sin_cuenta_devuelve_el_estado(
    api: httpx.AsyncClient,
    anonimo: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """QR_PUBLIC_WITHOUT_LOGIN / QR_PUBLIC_TOKEN_RESOLVES.

    Es la razon de ser de la subfase. Hasta 009J esta misma peticion respondia
    401 y quien escaneaba acababa en el login.
    """
    orden = await _orden_arrancada(api, admin_csrf, db_session, "_pub")

    respuesta = await anonimo.get(f"{TRACKING}/scan/{orden['qr_token']}")

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["order_code"] == orden["code"]
    assert cuerpo["status"] == "STARTED"
    assert cuerpo["created_at"] and cuerpo["started_at"]
    assert cuerpo["completed_at"] is None


@pytest.mark.asyncio
async def test_el_token_se_cambia_por_una_cookie_acotada(
    api: httpx.AsyncClient,
    anonimo: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """QR_TOKEN_VISIBLE_AFTER_REDIRECT: NO.

    Despues de resolver, la aplicacion consulta con la cookie y el token no
    vuelve a aparecer en ninguna direccion. La cookie ademas esta acotada a la
    superficie de seguimiento: no viaja con las peticiones internas, asi que no
    puede confundirse con una sesion.
    """
    orden = await _orden_arrancada(api, admin_csrf, db_session, "_cookie")

    escaneo = await anonimo.get(f"{TRACKING}/scan/{orden['qr_token']}")
    assert escaneo.status_code == 200

    galleta = escaneo.headers["set-cookie"]
    assert TRACKING_COOKIE_NAME in galleta
    assert "HttpOnly" in galleta
    assert f"Path={TRACKING_COOKIE_PATH}" in galleta

    # Y con la cookie ya puesta, la consulta no necesita el token.
    actual = await anonimo.get(f"{TRACKING}/current")
    assert actual.status_code == 200, actual.text
    assert actual.json()["order_code"] == orden["code"]


@pytest.mark.asyncio
async def test_sin_contexto_no_hay_seguimiento(anonimo: httpx.AsyncClient) -> None:
    """Entrar directamente a la vista publica no ensena la orden de nadie."""
    respuesta = await anonimo.get(f"{TRACKING}/current")

    assert respuesta.status_code == 404, respuesta.text
    assert respuesta.json()["error"]["code"] == "TRACKING_NOT_FOUND"


# ---------------------------------------------------------------------------
# Que no filtra
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_json_publico_no_lleva_nada_interno(
    api: httpx.AsyncClient,
    anonimo: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """QR_PUBLIC_SENSITIVE_DATA_LEAK: 0 / QR_PUBLIC_INTERNAL_IDS_VISIBLE: 0.

    Se busca sobre el JSON CRUDO, no sobre las claves: un identificador metido
    dentro de una cadena tambien es un identificador filtrado.
    """
    orden = await _orden_arrancada(api, admin_csrf, db_session, "_fuga")

    crudo = (await anonimo.get(f"{TRACKING}/scan/{orden['qr_token']}")).text

    for prohibido in (
        "quotation",
        "stock_location",
        "prepared_product",
        "required_material",
        "recipe",
        "readiness",
        "qr_token",
        "idempotency",
        "created_by",
        "technical_cost",
        "commercial",
        "markup",
    ):
        assert prohibido not in crudo, f"«{prohibido}» no puede salir sin sesión"

    assert str(orden["qr_token"]) not in crudo
    assert '"id"' not in crudo


@pytest.mark.asyncio
async def test_la_constancia_publica_no_es_la_hoja_de_taller(
    api: httpx.AsyncClient,
    anonimo: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """QR_PUBLIC_DOCUMENT_ACCESS + PUBLIC_DOCUMENT_CONTENT_SANITIZED.

    El documento publico existe, se abre sin cuenta y es OTRO documento: lleva
    el codigo y el estado, y no lleva el almacen, el preparado ni los gramos
    que si lleva la hoja que se imprime para fabricar.
    """
    orden = await _orden_arrancada(api, admin_csrf, db_session, "_docpub")
    await anonimo.get(f"{TRACKING}/scan/{orden['qr_token']}")

    publico = await anonimo.get(f"{TRACKING}/current/document")
    assert publico.status_code == 200, publico.text
    assert publico.content.startswith(b"%PDF-")

    texto = _texto(publico.content)
    assert str(orden["code"]) in texto
    assert "Seguimiento de producción" in texto

    interno = await api.get(f"{ORDERS}/{orden['id']}/document")
    assert interno.status_code == 200
    texto_interno = _texto(interno.content)

    # Lo que la hoja de taller SI dice y la constancia publica NO.
    for solo_interno in (
        str(orden["quotation_code"]),
        str(orden["stock_location_name"]),
        "Material preparado",
        "Almacén de salida",
    ):
        assert solo_interno in texto_interno, "la hoja de taller cambió de contenido"
        assert solo_interno not in texto, f"«{solo_interno}» no puede salir sin sesión"

    assert str(orden["qr_token"]) not in texto


@pytest.mark.asyncio
async def test_la_hoja_de_taller_sigue_exigiendo_sesion(
    api: httpx.AsyncClient,
    anonimo: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Abrir el seguimiento publico no abre el documento interno.

    Es el error que haria inutil todo lo demas: sanitizar la constancia y
    dejar la hoja de taller a un GET de distancia.
    """
    orden = await _orden_arrancada(api, admin_csrf, db_session, "_interna")
    await anonimo.get(f"{TRACKING}/scan/{orden['qr_token']}")

    respuesta = await anonimo.get(f"{ORDERS}/{orden['id']}/document")

    assert respuesta.status_code == 401, respuesta.text


# ---------------------------------------------------------------------------
# Que no se puede escribir
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("accion", ["start", "complete", "cancel"])
async def test_sin_cuenta_no_se_mueve_una_orden(
    api: httpx.AsyncClient,
    anonimo: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    accion: str,
) -> None:
    """QR_PUBLIC_MUTATION_ENDPOINTS: 0, comprobado contra la base.

    Tener el QR en la mano no es tener permiso. Y detras del rechazo no puede
    quedar nada: ni estado cambiado, ni movimiento, ni auditoria.
    """
    orden = await _orden_arrancada(api, admin_csrf, db_session, f"_mut{accion[:3]}")

    db_session.expire_all()
    movimientos_antes = int(
        await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0
    )
    eventos_antes = int(
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.entity_type == "production_order")
        )
        or 0
    )

    await anonimo.get(f"{TRACKING}/scan/{orden['qr_token']}")
    respuesta = await anonimo.post(f"{ORDERS}/{orden['id']}/{accion}")

    assert respuesta.status_code in (401, 403), respuesta.text

    db_session.expire_all()
    estado = await db_session.scalar(
        select(ProductionOrder.status).where(ProductionOrder.id == orden["id"])
    )
    assert estado is ProductionOrderStatus.STARTED
    assert (
        int(await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0)
        == movimientos_antes
    )
    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.entity_type == "production_order")
            )
            or 0
        )
        == eventos_antes
    )


@pytest.mark.asyncio
async def test_la_superficie_publica_no_acepta_escrituras(
    api: httpx.AsyncClient,
    anonimo: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Ni siquiera con el contexto puesto: bajo /tracking no hay verbos que escriban."""
    orden = await _orden_arrancada(api, admin_csrf, db_session, "_verbos")
    await anonimo.get(f"{TRACKING}/scan/{orden['qr_token']}")

    for metodo, ruta in (
        ("POST", f"{TRACKING}/current"),
        ("PUT", f"{TRACKING}/current"),
        ("PATCH", f"{TRACKING}/current"),
        ("DELETE", f"{TRACKING}/current"),
        ("POST", f"{TRACKING}/scan/{orden['qr_token']}"),
    ):
        respuesta = await anonimo.request(metodo, ruta)
        assert respuesta.status_code in (403, 405), f"{metodo} {ruta} -> {respuesta.status_code}"


# ---------------------------------------------------------------------------
# Tokens que no valen
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token",
    [
        "token-que-no-existe-en-ninguna-parte-000000",
        "corto",
        "x" * 500,
        "../../../etc/passwd",
        "' OR 1=1 --",
    ],
)
async def test_un_token_invalido_responde_siempre_lo_mismo_y_nunca_un_500(
    anonimo: httpx.AsyncClient, token: str
) -> None:
    """QR_PUBLIC_INVALID_TOKEN_NO_500.

    Uno corto, uno larguisimo, uno con recorrido de rutas y uno con comillas.
    Todos tienen que responder el MISMO 404: cualquier diferencia —un 422 aqui,
    un 500 alla— le dice a quien prueba tokens por donde seguir.
    """
    respuesta = await anonimo.get(f"{TRACKING}/scan/{token}")

    assert respuesta.status_code == 404, respuesta.text
    assert respuesta.json()["error"]["code"] == "TRACKING_NOT_FOUND"


# ---------------------------------------------------------------------------
# El puente hacia dentro
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_puente_a_la_vista_interna_exige_sesion(
    api: httpx.AsyncClient,
    anonimo: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """QR_AUTHENTICATED_INTERNAL_FLOW.

    El identificador interno es lo unico que el seguimiento no da a cualquiera:
    sin el, la superficie publica seria un traductor de tokens a identificadores.
    Con sesion si, porque quien trabaja aqui tiene que poder pasar del papel a
    la pantalla sin teclear un codigo.
    """
    orden = await _orden_arrancada(api, admin_csrf, db_session, "_puente")

    await anonimo.get(f"{TRACKING}/scan/{orden['qr_token']}")
    sin_sesion = await anonimo.get(f"{TRACKING}/current/internal-link")
    assert sin_sesion.status_code == 401, sin_sesion.text

    # El mismo navegador, ahora con sesion.
    await authenticate(anonimo, email=TEST_EMAIL, password=TEST_PASSWORD)
    con_sesion = await anonimo.get(f"{TRACKING}/current/internal-link")

    assert con_sesion.status_code == 200, con_sesion.text
    assert con_sesion.json() == {"production_order_id": orden["id"]}
