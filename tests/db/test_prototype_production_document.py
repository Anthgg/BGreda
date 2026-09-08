"""Fase 009K.4 — la hoja de taller de una orden nacida de una muestra.

El renderizador se escribio suponiendo que toda orden venia de una cotizacion.
Desde esta fase hay ordenes cuyo `quotation_id` es nulo, y la hoja tiene que
salir igual: sin huecos, sin una CTZ inventada y sin un segundo documento
paralelo que haya que mantener dos veces.

Se lee el PDF con `pypdf` y no el HTML de entrada, por la misma razon que en
009I: WeasyPrint es quien decide que acaba impreso, y un dato escondido por CSS
sigue estando en el papel que alguien deja encima de una mesa.
"""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.test_production_orders_api import crear_ubicacion
from tests.db.test_prototype_production_order import ORDENES, _cpr_confirmada
from tests.db.test_prototype_quotations import cobrar
from tests.db.test_quotation_builder_api import head


def _texto(contenido: bytes) -> str:
    reader = PdfReader(io.BytesIO(contenido))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _seguido(contenido: bytes) -> str:
    """El mismo texto con los espacios normalizados.

    Hace falta para comparar frases y etiquetas: el maquetador parte una linea
    donde le cabe, y buscar la frase seguida falla por una razon que no tiene
    nada que ver con lo que se quiere comprobar.
    """
    return " ".join(_texto(contenido).split())


async def _hoja(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, sufijo: str
) -> tuple[dict[str, Any], dict[str, Any], str, bytes]:
    """Cobra una cotizacion de prototipo y descarga la hoja de su orden."""
    escenario = await _cpr_confirmada(api, csrf, db_session, sufijo)
    nombre_almacen = f"Almacen hoja{sufijo}"
    almacen = await crear_ubicacion(api, csrf, nombre_almacen)
    pagada = await cobrar(api, csrf, escenario["documento"]["id"], stock_location_id=almacen)
    assert pagada.status_code == 200, pagada.text

    documento = await api.get(
        f"{ORDENES}/{pagada.json()['production_order_id']}/document", headers=head(csrf)
    )
    assert documento.status_code == 200, documento.text
    return escenario, pagada.json(), nombre_almacen, documento.content


@pytest.mark.asyncio
async def test_la_hoja_de_una_orden_de_muestra_se_imprime(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B18 + B19. PROTOTYPE_PRODUCTION_ORDER_PDF.

    Que salga un PDF no basta: tiene que llevar el codigo de la orden, que es
    lo que hace que la hoja se pueda emparejar con lo que hay en el sistema.
    """
    _escenario, cobro, _almacen, contenido = await _hoja(api, admin_csrf, db_session, "_k4_pdf")

    assert contenido.startswith(b"%PDF-")
    detalle = (
        await api.get(f"{ORDENES}/{cobro['production_order_id']}", headers=head(admin_csrf))
    ).json()
    assert str(detalle["code"]) in _texto(contenido)


@pytest.mark.asyncio
async def test_la_hoja_dice_de_donde_viene_la_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B20. La procedencia se imprime como es: CPR y PRT, no una CTZ falsa.

    Es lo que permite a quien tiene la hoja en la mano saber que esta
    fabricando una muestra y de que encargo salio. Rellenar «Cotización
    origen» con el codigo de la cotizacion de prototipo habria sido mas facil
    y habria mentido: una CPR no es una CTZ.
    """
    escenario, cobro, _almacen, contenido = await _hoja(
        api, admin_csrf, db_session, "_k4_pdf_origen"
    )
    texto = _seguido(contenido)
    # Las etiquetas de los hechos se imprimen en versalitas: se comparan sin
    # distinguir mayusculas para no atar la prueba a una regla de CSS.
    etiquetas = texto.lower()

    assert str(escenario["documento"]["code"]) in texto, "falta la cotización de prototipo"
    assert str(cobro["prototype_code"]) in texto, "falta la muestra"
    assert "origen cpr-" in etiquetas
    assert "muestra prt-" in etiquetas
    assert "cotización origen" not in etiquetas, "esta orden no nace de una cotización de producto"


@pytest.mark.asyncio
async def test_la_hoja_lleva_lo_que_hace_falta_para_fabricar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La pieza, su tamano, cuantas, de que almacen sale y con que material.

    Los materiales salen de las lineas del prototipo —lo que alguien eligio a
    mano— y no de una receta: una muestra no tiene. La hoja no puede decir
    «Sin receta» donde hay barro elegido.
    """
    escenario, _cobro, almacen, contenido = await _hoja(
        api, admin_csrf, db_session, "_k4_pdf_taller"
    )
    texto = _seguido(contenido)

    assert "Taza personalizada" in texto, "la pieza"
    assert almacen in texto, "el almacén del que sale el material"
    assert str(escenario["caso"]["_pasta"]["name"]) in texto, "el material elegido"
    assert "1.25 kg" in texto, "cuánto material, en su unidad"
    # Las medidas acordadas viajan congeladas en la linea de la orden.
    assert "A 15" in texto and "H 20" in texto and "L 15" in texto
    assert "sin receta" not in texto.lower(), "una muestra no tiene receta, y no le falta ninguna"
    assert "creada" in texto.lower(), "la fecha de alta"


@pytest.mark.asyncio
async def test_la_hoja_de_una_muestra_no_lleva_ni_un_precio(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B21. PROTOTYPE_PDF_INTERNAL_PRICING_LEAK: NO.

    Una cotizacion de prototipo lleva dias de diseno, de artista y de moldeo, y
    el precio que se le cobro al cliente. Nada de eso ayuda a hacer la pieza, y
    una hoja de taller circula muchisimo mas que una cotizacion: acaba en una
    mesa, en una foto y en un grupo de mensajeria.
    """
    _escenario, _cobro, _almacen, contenido = await _hoja(
        api, admin_csrf, db_session, "_k4_pdf_privado"
    )
    texto = _seguido(contenido).lower()

    for prohibido in (
        "igv",
        "margen",
        "utilidad",
        "precio",
        "diseño",
        "artista",
        "moldeo",
        "factor",
    ):
        assert prohibido not in texto, f"la hoja de taller no puede llevar «{prohibido}»"
    # Ni las cifras del caso de referencia: 240 de diseno, 200 de artista, 450
    # de base, 81 de impuesto y 531 de total.
    for importe in ("240.00", "200.00", "450.00", "81.00", "531.00"):
        assert importe not in texto, f"la hoja de taller no puede llevar «{importe}»"


@pytest.mark.asyncio
async def test_la_hoja_de_una_muestra_reutiliza_el_qr_de_siempre(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_PRODUCTION_ORDER_QR.

    El QR no es un mecanismo nuevo: es el mismo de 009I.1, con el mismo token
    opaco y la misma leyenda. Y el token sigue sin imprimirse en claro.
    """
    _escenario, cobro, _almacen, contenido = await _hoja(api, admin_csrf, db_session, "_k4_pdf_qr")

    assert "Escanea para consultar el estado de producción" in _seguido(contenido)
    detalle = (
        await api.get(f"{ORDENES}/{cobro['production_order_id']}", headers=head(admin_csrf))
    ).json()
    assert str(detalle["qr_token"]) not in _texto(contenido)


@pytest.mark.asyncio
async def test_el_seguimiento_publico_de_una_muestra_no_dice_que_es_una_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La frontera publica no se amplia por esta fase.

    Quien escanea sin sesion ve el codigo de la orden, en que punto va y que
    pieza es. Ni el almacen, ni el material, ni de que cotizacion salio: el
    modelo publico no tiene donde guardarlos, y esta fase no le anadio sitio.
    """
    escenario, cobro, almacen, _contenido = await _hoja(
        api, admin_csrf, db_session, "_k4_pdf_publico"
    )
    detalle = (
        await api.get(f"{ORDENES}/{cobro['production_order_id']}", headers=head(admin_csrf))
    ).json()

    publico = await api.get(f"/api/v1/tracking/production-orders/scan/{detalle['qr_token']}")
    assert publico.status_code == 200, publico.text
    cuerpo = publico.json()

    assert cuerpo["order_code"] == detalle["code"]
    crudo = publico.text
    assert almacen not in crudo
    assert str(escenario["documento"]["code"]) not in crudo
    assert str(cobro["prototype_code"]) not in crudo
