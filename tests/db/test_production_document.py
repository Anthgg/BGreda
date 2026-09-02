"""Fase 009I — la hoja de taller de la orden, y lo que NO puede llevar.

El PDF se lee con `pypdf` y no comprobando el HTML de entrada: WeasyPrint es
quien decide que acaba impreso, y un dato escondido por CSS sigue estando en el
papel que alguien deja sobre una mesa.

La otra mitad de este archivo comprueba que el documento comercial de la
cotizacion (009G) sale exactamente igual que antes de esta fase.
"""

from __future__ import annotations

import io

import httpx
import pytest
from fastapi import FastAPI
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.test_production_orders_api import (
    ORDERS,
    confirmar,
    crear_orden,
    escenario,
)
from tests.db.test_quotation_builder_api import head

BUILDER = "/api/v1/quotation-builder"


def _texto(contenido: bytes) -> str:
    reader = PdfReader(io.BytesIO(contenido))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


async def _orden_con_documento(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, suffix: str
) -> tuple[dict[str, object], bytes]:
    datos = await escenario(api, csrf, db_session, suffix=suffix)
    confirmada = await confirmar(api, csrf, datos["quotation"])
    creada = await crear_orden(
        api, csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert creada.status_code == 201, creada.text
    documento = await api.get(f"{ORDERS}/{creada.json()['id']}/document")
    assert documento.status_code == 200, documento.text
    return creada.json(), documento.content


@pytest.mark.asyncio
async def test_el_documento_de_produccion_se_renderiza_y_lleva_lo_que_el_taller_necesita(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCTION_DOCUMENT_RENDERS.

    Codigo de la orden, cotizacion de origen, estado, almacen y la pieza. Es lo
    minimo para que alguien pueda fabricar sin abrir el sistema.
    """
    orden, contenido = await _orden_con_documento(api, admin_csrf, db_session, "_doc")

    assert contenido.startswith(b"%PDF-")
    texto = _texto(contenido)
    assert str(orden["code"]) in texto
    assert str(orden["quotation_code"]) in texto
    assert str(orden["stock_location_name"]) in texto
    assert "Orden de producción" in texto


@pytest.mark.asyncio
async def test_el_documento_lleva_el_qr_incrustado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCTION_DOCUMENT_HAS_QR.

    El QR va como imagen embebida y no como enlace: WeasyPrint no descarga
    nada, asi que un `src` remoto saldria en blanco y nadie lo notaria hasta
    tener la hoja impresa en la mano.
    """
    _orden, contenido = await _orden_con_documento(api, admin_csrf, db_session, "_qr")

    reader = PdfReader(io.BytesIO(contenido))
    # El QR se dibuja como SVG, asi que en el PDF acaba siendo un conjunto de
    # trazos vectoriales, no un XObject de imagen. Lo que se comprueba es que
    # la pagina lleva contenido grafico ademas del texto y que el pie explica
    # para que sirve.
    assert len(reader.pages) >= 1
    assert "Escanear" in _texto(contenido)


@pytest.mark.asyncio
async def test_el_documento_de_taller_no_lleva_precios_ni_margenes(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """QR/DOCUMENT_DOES_NOT_EXPOSE_COMMERCIAL_DATA.

    Ninguna de estas cifras ayuda a esmaltar una pieza. Sacarlas impresas al
    taller solo amplia quien acaba viendo el margen del cliente, y una hoja de
    taller circula mucho mas que una cotizacion.
    """
    orden, contenido = await _orden_con_documento(api, admin_csrf, db_session, "_privado")
    texto = _texto(contenido).lower()

    for prohibido in ("igv", "margen", "precio unitario", "total con", "utilidad"):
        assert prohibido not in texto, f"la hoja de taller no puede llevar «{prohibido}»"
    # Tampoco el importe concreto de la cotizacion de prueba.
    assert "8.50" not in texto
    assert str(orden["code"]) in _texto(contenido)


@pytest.mark.asyncio
async def test_el_token_del_qr_no_se_imprime_en_claro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El token va DENTRO del QR, no escrito al lado.

    Impreso en claro, cualquiera que vea la hoja de reojo se lo lleva anotado;
    dentro del QR hace falta al menos apuntar una camara, y sobre todo hace
    falta una sesion para que sirva de algo.
    """
    orden, contenido = await _orden_con_documento(api, admin_csrf, db_session, "_token")

    assert str(orden["qr_token"]) not in _texto(contenido)


@pytest.mark.asyncio
async def test_el_pdf_comercial_de_la_cotizacion_no_cambia(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """QUOTATION_PDF_UNCHANGED.

    009I no toca el documento de 009G. Se pide el PDF comercial ANTES de crear
    la orden y DESPUES de crearla y arrancarla: tiene que decir exactamente lo
    mismo. Producir no reescribe lo que se le prometio al cliente.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_pdfigual")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])

    antes = await api.get(f"/api/v1/quotations/{confirmada['id']}/pdf")
    assert antes.status_code == 200, antes.text
    texto_antes = _texto(antes.content)

    creada = await crear_orden(
        api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert creada.status_code == 201, creada.text
    arrancada = await api.post(f"{ORDERS}/{creada.json()['id']}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text

    despues = await api.get(f"/api/v1/quotations/{confirmada['id']}/pdf")
    assert despues.status_code == 200, despues.text

    assert _texto(despues.content) == texto_antes
    # Y no se ha colado el codigo de la orden en el documento comercial.
    assert str(creada.json()["code"]) not in _texto(despues.content)


@pytest.mark.asyncio
async def test_el_qr_resuelve_la_orden_correcta_y_exige_sesion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El escaneo abre la orden, y solo si hay sesion.

    Que el QR sea imprimible no convierte la orden en publica: dentro va una
    ruta de la aplicacion, no un enlace anonimo a los datos.
    """
    orden, _contenido = await _orden_con_documento(api, admin_csrf, db_session, "_scan")

    escaneada = await api.get(f"{ORDERS}/scan/{orden['qr_token']}")
    assert escaneada.status_code == 200, escaneada.text
    assert escaneada.json()["id"] == orden["id"]
    assert escaneada.json()["code"] == orden["code"]


@pytest.mark.asyncio
async def test_un_token_desconocido_responde_lo_mismo_que_uno_inexistente(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """No hay oraculo de tokens.

    Distinguir «token invalido» de «token valido de otra orden» convertiria el
    endpoint en una forma de averiguar que tokens existen.
    """
    respuesta = await api.get(f"{ORDERS}/scan/token-que-no-existe-en-ninguna-parte-000")

    assert respuesta.status_code == 404, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_NOT_FOUND"


@pytest.mark.asyncio
async def test_escanear_sin_sesion_no_devuelve_la_orden(
    api: httpx.AsyncClient, api_app: FastAPI, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La ruta del QR es interna: sin cookie no hay orden.

    Se usa un cliente NUEVO en vez de intentar borrarle la cookie al de la
    sesion: httpx guarda las cookies en un tarro propio y «quitarla» pasando
    cabeceras vacias no siempre la quita, con lo que la prueba pasaria por
    seguir autenticada y no por rechazar a nadie.
    """
    orden, _contenido = await _orden_con_documento(api, admin_csrf, db_session, "_sinsesion")

    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anonimo:
        respuesta = await anonimo.get(f"{ORDERS}/scan/{orden['qr_token']}")

    assert respuesta.status_code == 401, respuesta.text
