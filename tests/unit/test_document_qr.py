"""Fase 009I.1 — el generador de codigos QR.

Un QR mal generado no falla: se imprime igual y deja de leerse en el taller,
donde nadie tiene forma de saber por que. Por eso las decisiones que deciden si
el codigo se lee —borde silencioso, correccion de errores, cuanto tapa el
logo— se fijan aqui por escrito y no se dejan a la vista de quien haga el
siguiente cambio.

El tamano impreso NO se comprueba aqui: eso vive en el sistema documental y lo
vigila `test_document_system.py`, medido sobre un PDF renderizado de verdad.
"""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET

import pytest
import segno

from app.services.document_qr import (
    ERROR_WITH_LOGO,
    ERROR_WITHOUT_LOGO,
    LOGO_PADDING_MODULES,
    LOGO_WIDTH_RATIO,
    QUIET_ZONE_MODULES,
    build_document_qr_data_uri,
    build_document_qr_svg,
)

PAYLOAD = "https://app.ejemplo.pe/seguimiento/" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t"
LOGO = "data:image/png;base64,iVBORw0KGgo="


def _raiz(svg: str) -> ET.Element:
    # El SVG lo acaba de generar la funcion bajo prueba en esta misma linea: no
    # es entrada de nadie. La regla que avisa de XML no confiable es correcta en
    # general y aqui no aplica.
    return ET.fromstring(svg)  # noqa: S314


def _lado(svg: str) -> float:
    caja = _raiz(svg).get("viewBox")
    assert caja is not None
    return float(caja.split()[2])


def test_el_borde_silencioso_es_el_de_la_norma() -> None:
    """Cuatro modulos por lado, no dos.

    Hasta 009I.1 se imprimian dos. Con fondo blanco alrededor la mayoria de los
    lectores lo perdonan, pero «la mayoria» no sirve para un papel que acaba
    fotografiado de lado sobre una mesa.
    """
    assert QUIET_ZONE_MODULES == 4

    modulos = len(segno.make(PAYLOAD, error=ERROR_WITHOUT_LOGO).matrix)
    assert _lado(build_document_qr_svg(PAYLOAD)) == modulos + 2 * QUIET_ZONE_MODULES


def test_nada_se_dibuja_encima_del_borde_silencioso() -> None:
    """Ni el logo, ni su placa, ni un borde. El borde tiene que quedar limpio."""
    svg = build_document_qr_svg(PAYLOAD, logo_data_uri=LOGO)
    lado = _lado(svg)
    raiz = _raiz(svg)

    for etiqueta in ("{http://www.w3.org/2000/svg}rect", "{http://www.w3.org/2000/svg}image"):
        for nodo in raiz.iter(etiqueta):
            x, y = float(nodo.get("x", 0)), float(nodo.get("y", 0))
            ancho, alto = float(nodo.get("width", 0)), float(nodo.get("height", 0))
            # El fondo blanco cubre la hoja entera a proposito: ES el borde.
            if ancho == lado and alto == lado:
                continue
            assert x >= QUIET_ZONE_MODULES
            assert y >= QUIET_ZONE_MODULES
            assert x + ancho <= lado - QUIET_ZONE_MODULES
            assert y + alto <= lado - QUIET_ZONE_MODULES


def test_la_correccion_alta_solo_se_paga_cuando_hay_logo() -> None:
    """Subirla gratis haria el codigo MAS denso y por tanto menos legible.

    Con correccion alta caben menos datos por version, asi que el mismo texto
    necesita mas modulos en los mismos milimetros. Se paga cuando hace falta
    —tapar el centro destruye modulos que hay que reconstruir— y no antes.
    """
    assert ERROR_WITHOUT_LOGO == "m"
    assert ERROR_WITH_LOGO == "h"

    sin_logo = len(segno.make(PAYLOAD, error=ERROR_WITHOUT_LOGO).matrix)
    con_logo = len(segno.make(PAYLOAD, error=ERROR_WITH_LOGO).matrix)
    assert con_logo > sin_logo, "la correccion alta deberia añadir modulos"

    assert _lado(build_document_qr_svg(PAYLOAD)) < _lado(
        build_document_qr_svg(PAYLOAD, logo_data_uri=LOGO)
    )


def test_el_logo_ocupa_lo_que_un_qr_puede_perder() -> None:
    """QR_SECURITY_UNCHANGED_BY_BRANDING.

    La correccion alta recupera hasta un 30 % del simbolo. El logo mas su placa
    tapan alrededor de un 5 %, asi que sobra holgura. Este margen se consume sin
    avisar en cuanto alguien agranda el logo «para que se vea mejor», y el
    codigo deja de leerse en el taller sin que nadie lo relacione.
    """
    assert 0.15 <= LOGO_WIDTH_RATIO <= 0.20

    svg = build_document_qr_svg(PAYLOAD, logo_data_uri=LOGO)
    lado = _lado(svg)
    placa = lado * LOGO_WIDTH_RATIO + 2 * LOGO_PADDING_MODULES
    assert (placa / lado) ** 2 < 0.15, "la placa tapa demasiado simbolo"


def test_el_logo_tiene_una_placa_limpia_detras() -> None:
    """Sin ella, un modulo negro pegado al logo se lee como parte del dibujo."""
    svg = build_document_qr_svg(PAYLOAD, logo_data_uri=LOGO)
    raiz = _raiz(svg)
    lado = _lado(svg)

    imagen = next(raiz.iter("{http://www.w3.org/2000/svg}image"))
    ancho_logo = float(imagen.get("width", 0))

    placas = [
        r
        for r in raiz.iter("{http://www.w3.org/2000/svg}rect")
        if r.get("fill") == "#ffffff" and float(r.get("width", 0)) != lado
    ]
    assert len(placas) == 1, "deberia haber exactamente una placa de proteccion"
    assert float(placas[0].get("width", 0)) > ancho_logo


def test_el_logo_no_cambia_ni_una_letra_de_lo_codificado() -> None:
    """QR_TOKEN_UNCHANGED_BY_BRANDING.

    El branding es dibujo. Ni el token, ni la URL, ni la entropia dependen de
    que el taller tenga logo configurado: la misma orden apunta al mismo sitio
    con logo y sin el.
    """
    sin_logo = build_document_qr_svg(PAYLOAD)
    con_logo = build_document_qr_svg(PAYLOAD, logo_data_uri=LOGO)

    # Se comprueba contra la libreria: los modulos de datos son los que segno
    # produce para ESE texto y esa correccion, sin que el logo intervenga.
    for svg, error in ((sin_logo, ERROR_WITHOUT_LOGO), (con_logo, ERROR_WITH_LOGO)):
        matriz = segno.make(PAYLOAD, error=error).matrix
        oscuros = sum(sum(fila) for fila in matriz)
        dibujados = sum(
            float(r.get("width", 0))
            for r in _raiz(svg).iter("{http://www.w3.org/2000/svg}rect")
            if r.get("fill") is None
        )
        assert dibujados == oscuros


def test_el_logo_no_se_deforma() -> None:
    """Un logo apaisado no se estira para llenar el cuadrado del centro."""
    imagen = next(
        _raiz(build_document_qr_svg(PAYLOAD, logo_data_uri=LOGO)).iter(
            "{http://www.w3.org/2000/svg}image"
        )
    )
    assert imagen.get("preserveAspectRatio") == "xMidYMid meet"
    assert imagen.get("href") == LOGO


def test_sin_logo_no_se_dibuja_ninguna_placa() -> None:
    svg = build_document_qr_svg(PAYLOAD)
    assert "<image" not in svg
    lado = _lado(svg)
    blancos = [
        r for r in _raiz(svg).iter("{http://www.w3.org/2000/svg}rect") if r.get("fill") == "#ffffff"
    ]
    assert len(blancos) == 1
    assert float(blancos[0].get("width", 0)) == lado


@pytest.mark.parametrize("logo", [None, LOGO])
def test_el_data_uri_es_autosuficiente(logo: str | None) -> None:
    """WeasyPrint no descarga nada: el documento tiene que traerlo todo dentro."""
    uri = build_document_qr_data_uri(PAYLOAD, logo_data_uri=logo)
    assert uri.startswith("data:image/svg+xml;base64,")

    svg = base64.b64decode(uri.split(",", 1)[1]).decode("utf-8")
    assert svg.startswith("<svg")
    # Ninguna referencia externa: ni http, ni file, ni una ruta del disco.
    assert not re.search(r'(?:href|src)\s*=\s*"(?!data:)', svg)


def test_los_modulos_seguidos_se_agrupan() -> None:
    """Un rectangulo por modulo daria un SVG que viaja en CADA documento.

    No es una micro-optimizacion: la fila superior de un QR son tres bloques
    largos, y dibujarla modulo a modulo multiplica el tamano del fichero por el
    ancho del simbolo.
    """
    svg = build_document_qr_svg(PAYLOAD)
    matriz = segno.make(PAYLOAD, error=ERROR_WITHOUT_LOGO).matrix
    oscuros = sum(sum(fila) for fila in matriz)
    rectangulos = [
        r for r in _raiz(svg).iter("{http://www.w3.org/2000/svg}rect") if r.get("fill") is None
    ]
    assert len(rectangulos) < oscuros
