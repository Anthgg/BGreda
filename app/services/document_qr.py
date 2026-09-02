"""El generador de codigos QR de los documentos. Hay uno y solo uno.

Un QR impreso es una llave: lo que lleva dentro da acceso a algo. Por eso no se
genera en tres sitios con tres criterios distintos —uno con borde silencioso
corto, otro con otra correccion de errores— sino aqui, donde las decisiones que
afectan a si el codigo se lee o no estan escritas una vez y medidas una vez.

El modulo no sabe que es una orden de produccion. Recibe un texto y, si el
taller tiene logo configurado, una imagen. Que ese texto sea una ruta de
seguimiento lo decide quien llama.

**El logo no cambia el contenido del codigo.** Ni el token, ni la URL, ni la
entropia. Cambia como se dibuja y, por eso mismo, cambia cuanta redundancia
hace falta: tapar el centro obliga a subir la correccion de errores.
"""

from __future__ import annotations

import base64

import segno

#: Borde silencioso, en modulos. La norma pide 4 y aqui se respetan los 4.
#:
#: Hasta 009I.1 se imprimian 2. Con un fondo blanco alrededor la mayoria de los
#: lectores lo perdonan, pero «la mayoria» no es un criterio para un papel que
#: acaba fotografiado de lado sobre una mesa de taller.
QUIET_ZONE_MODULES = 4

#: Ancho del logo respecto al ancho TOTAL del codigo, borde incluido.
#:
#: 18 %, dentro del margen de 15-20 % que tolera un QR con imagen central. El
#: area tapada es el cuadrado de esa fraccion —alrededor de un 3 %— y la
#: correccion alta recupera hasta un 30 %, asi que sobra holgura. Subirlo «para
#: que se vea mejor el logo» consume esa holgura sin avisar.
LOGO_WIDTH_RATIO = 0.18

#: Margen limpio alrededor del logo, en modulos. Impide que un modulo negro
#: quede pegado al borde de la imagen y se lea como parte de ella.
LOGO_PADDING_MODULES = 1

#: Correccion de errores. Alta SOLO cuando hay logo, porque tapar el centro
#: destruye modulos y hay que poder reconstruirlos. Sin logo se mantiene la
#: media de siempre: subirla gratis haria el codigo mas denso —mas modulos en
#: los mismos milimetros— y por tanto MENOS legible impreso, que es lo contrario
#: de lo que se busca.
ERROR_WITH_LOGO = "h"
ERROR_WITHOUT_LOGO = "m"


def _modules_svg(matrix: tuple[bytearray, ...], offset: int) -> str:
    """Dibuja los modulos oscuros agrupando los seguidos de cada fila.

    Un rectangulo por modulo daria un SVG varias veces mas grande, y ese SVG
    viaja embebido en cada PDF.
    """
    piezas: list[str] = []
    for y, fila in enumerate(matrix):
        x = 0
        ancho = len(fila)
        while x < ancho:
            if not fila[x]:
                x += 1
                continue
            inicio = x
            while x < ancho and fila[x]:
                x += 1
            piezas.append(
                f'<rect x="{inicio + offset}" y="{y + offset}" width="{x - inicio}" height="1"/>'
            )
    return "".join(piezas)


def build_document_qr_svg(payload: str, *, logo_data_uri: str | None = None) -> str:
    """Devuelve el SVG del codigo, en unidades de modulo.

    El `viewBox` va en modulos y no en milimetros a proposito: el tamano
    impreso lo decide el sistema documental con CSS, en un unico sitio, y aqui
    no hay que saber en que documento acabara.
    """
    qr = segno.make(payload, error=ERROR_WITH_LOGO if logo_data_uri else ERROR_WITHOUT_LOGO)
    matrix = qr.matrix
    lado = len(matrix) + 2 * QUIET_ZONE_MODULES

    partes = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {lado} {lado}" '
        f'shape-rendering="crispEdges">',
        # El fondo blanco es el borde silencioso: sin el, el QR heredaria el
        # color del papel o de la caja que lo contenga.
        f'<rect width="{lado}" height="{lado}" fill="#ffffff"/>',
        f'<g fill="#000000">{_modules_svg(matrix, QUIET_ZONE_MODULES)}</g>',
    ]

    if logo_data_uri:
        logo = lado * LOGO_WIDTH_RATIO
        placa = logo + 2 * LOGO_PADDING_MODULES
        origen_placa = (lado - placa) / 2
        origen_logo = (lado - logo) / 2
        partes.append(
            f'<rect x="{origen_placa:.3f}" y="{origen_placa:.3f}" '
            f'width="{placa:.3f}" height="{placa:.3f}" rx="0.6" fill="#ffffff"/>'
        )
        # `meet` conserva la proporcion: un logo apaisado no se deforma para
        # llenar el cuadrado.
        partes.append(
            f'<image x="{origen_logo:.3f}" y="{origen_logo:.3f}" '
            f'width="{logo:.3f}" height="{logo:.3f}" '
            f'preserveAspectRatio="xMidYMid meet" href="{logo_data_uri}"/>'
        )

    partes.append("</svg>")
    return "".join(partes)


def build_document_qr_data_uri(payload: str, *, logo_data_uri: str | None = None) -> str:
    """El SVG del codigo, embebido como `data:`.

    WeasyPrint no descarga nada: el documento tiene que ser autosuficiente.
    """
    svg = build_document_qr_svg(payload, logo_data_uri=logo_data_uri)
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"
