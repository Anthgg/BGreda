"""Fase 009I.1 — el sistema documental compartido.

Desde esta subfase los documentos oficiales de Greda comparten hoja,
tipografia, cabecera, tablas, firmas y pie, y NO comparten una sola regla de
negocio. Las dos mitades de esa frase necesitan prueba:

- que de verdad comparten: si la cotizacion se descolgara del sistema, cambiar
  la identidad de la empresa dejaria de cambiarla en todos los documentos y
  nadie se enteraria hasta imprimir.
- que de verdad NO comparten: si la orden de produccion heredara de la
  cotizacion, retocar el IGV movería la hoja de taller.

Se comprueba sobre el HTML renderizado y no comparando ficheros: lo que importa
es lo que llega al papel.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.documents.common import CompanyDocInfo
from app.documents.production import (
    build_production_order_document,
    build_public_tracking_data,
    build_public_tracking_sheet,
)
from app.documents.quotation import build_quotation_pdf_document
from app.models.masters import Product, ProductType
from app.models.production import ProductionOrder, ProductionOrderLine, ProductionOrderStatus
from app.models.quotations import Quotation, QuotationStatus
from app.services.production_pdf import QR_CAPTION, ProductionPdfService
from app.services.quotation_pdf import QuotationPdfService

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"

#: Marcas del sistema compartido. Si un documento deja de traerlas, es que dejo
#: de usar el sistema, aunque siga viendose parecido ese dia.
MARCAS_COMPARTIDAS = (
    "--doc-ink:",  # los tokens de color
    "--doc-font:",  # la tipografia
    "size: A4 portrait",  # la hoja
    "counter(pages)",  # la paginacion
    "doc-header",  # la cabecera
    "doc-table",  # la tabla base
)


def _empresa() -> CompanyDocInfo:
    return CompanyDocInfo(
        legal_name="LABERINTO S.A.C.",
        trade_name="LABERINTO S.A.C.",
        tax_id="20601234567",
        address="Av. Ejemplo 123",
    )


def _orden() -> ProductionOrder:
    orden = ProductionOrder(
        id=2,
        code="OP-2026-000002",
        quotation_id=349,
        stock_location_id=1,
        status=ProductionOrderStatus.COMPLETED,
        qr_token="t" * 43,
        started_at=datetime(2026, 9, 2, 6, 7, tzinfo=UTC),
        completed_at=datetime(2026, 9, 2, 6, 11, tzinfo=UTC),
    )
    orden.created_at = datetime(2026, 9, 2, 6, 6, tzinfo=UTC)
    orden.lines = [
        ProductionOrderLine(
            id=1,
            production_order_id=2,
            quotation_item_id=1,
            sort_order=1,
            product_id=1,
            product_name_snapshot="Jarras",
            product_internal_reference_snapshot="LAB50021",
            quantity=12,
            width_snapshot=Decimal("10.5"),
            recipe_id=1,
            prepared_product_id=7,
            required_material_quantity=Decimal("1500.00"),
            required_material_uom="g",
        )
    ]
    return orden


def _html_orden() -> str:
    servicio = ProductionPdfService.__new__(ProductionPdfService)
    ProductionPdfService.__init__(servicio, session=None)  # type: ignore[arg-type]
    documento = build_production_order_document(
        order=_orden(),
        company=_empresa(),
        quotation_code="CTZ-2026-000349",
        stock_location_name="Almacén principal",
        prepared_names={7: ("BARNIZ BASE 57", "LAB70005")},
        qr_data_uri="data:image/svg+xml;base64,AAAA",
        qr_caption=QR_CAPTION,
    )
    return servicio._template.render(doc=documento)


def _html_publico(logo_data_uri: str | None = None) -> str:
    servicio = ProductionPdfService.__new__(ProductionPdfService)
    ProductionPdfService.__init__(servicio, session=None)  # type: ignore[arg-type]
    hoja = build_public_tracking_sheet(
        build_public_tracking_data(order=_orden(), company_name="LABERINTO S.A.C."),
        logo_data_uri=logo_data_uri,
    )
    return servicio._public_template.render(doc=hoja)


def _html_cotizacion() -> str:
    producto = Product(
        id=1,
        internal_reference="LAB50032",
        name="Shot pisquero",
        product_type=ProductType.FINISHED_PRODUCT,
        base_uom_code="NIU",
    )
    cotizacion = Quotation(
        id=1,
        code="CTZ-2026-000349",
        status=QuotationStatus.CONFIRMED,
        quantity=8,
        product_id=1,
        product=producto,
        product_name_snapshot="Shot pisquero",
        commercial_sale_unit_price=Decimal("9.00"),
        commercial_subtotal=Decimal("72.00"),
        tax_percentage_snapshot=Decimal("18.00"),
        tax_amount=Decimal("12.96"),
        commercial_total=Decimal("84.96"),
    )
    servicio = QuotationPdfService(session=None)  # type: ignore[arg-type]
    return servicio.render_html(build_quotation_pdf_document(quotation=cotizacion))


TODOS = {
    "cotización": _html_cotizacion,
    "orden de producción": _html_orden,
    "seguimiento público": _html_publico,
}


@pytest.mark.parametrize("nombre", sorted(TODOS))
@pytest.mark.parametrize("marca", MARCAS_COMPARTIDAS)
def test_todos_los_documentos_traen_el_sistema_compartido(nombre: str, marca: str) -> None:
    """GLOBAL_PDF_DESIGN_SYSTEM / SHARED_DOCUMENT_CSS / SHARED_HEADER."""
    assert marca in TODOS[nombre](), f"«{nombre}» no está usando el sistema documental"


@pytest.mark.parametrize("nombre", sorted(TODOS))
def test_todos_extienden_la_misma_base(nombre: str) -> None:
    """GLOBAL_DOCUMENT_BASE.

    Ninguno hereda de otro documento. La cadena `production_order extends
    quotation` es justo la que este sistema existe para impedir.
    """
    plantillas = {
        "cotización": TEMPLATES / "quotations" / "quotation.html",
        "orden de producción": TEMPLATES / "production" / "production_order.html",
        "seguimiento público": TEMPLATES / "production" / "production_public.html",
    }
    fuente = plantillas[nombre].read_text(encoding="utf-8")
    assert '{% extends "base_document.html" %}' in fuente
    assert 'extends "quotations/' not in fuente
    assert 'extends "production/' not in fuente


def test_la_constancia_publica_usa_el_logo_configurado_sin_exponer_otros_datos() -> None:
    html = _html_publico("data:image/png;base64,CONFIGURADO")

    assert 'src="data:image/png;base64,CONFIGURADO"' in html
    assert "RUC:" not in html
    assert "Dirección:" not in html


def test_lo_comercial_no_se_cuela_en_los_documentos_de_produccion() -> None:
    """DOCUMENT_SPECIFIC_CONTENT_ISOLATED.

    Compartir la maquetacion no puede acabar compartiendo el precio. Ninguno de
    los dos documentos de produccion tiene por que saber que existe un IGV.
    """
    for nombre in ("orden de producción", "seguimiento público"):
        html = TODOS[nombre]().lower()
        for prohibido in ("igv", "subtotal", "p. unitario", "datos bancarios", "cci"):
            # Con limites de palabra: «cci» suelto tambien esta dentro de
            # «producción», y una prueba que falla por eso deja de leerse.
            assert not re.search(rf"{re.escape(prohibido)}", html), (
                f"«{prohibido}» no pinta nada en «{nombre}»"
            )


def test_lo_operativo_no_se_cuela_en_la_cotizacion() -> None:
    """El camino contrario: la cotizacion no habla del taller."""
    html = _html_cotizacion().lower()
    for prohibido in ("material preparado", "almacén de salida", "fabricó", "conformidad"):
        assert prohibido not in html


def test_el_css_compartido_no_lleva_vocabulario_comercial() -> None:
    """El fichero entero se incrusta en TODOS los documentos, comentarios incluidos.

    Una explicacion escrita aqui con la palabra «margen» acaba dentro del PDF
    que recibe el cliente, y la prueba de privacidad comercial la encuentra
    ahi. Antes que descubrirlo por ese camino, se dice aqui.
    """
    css = (TEMPLATES / "styles" / "document_system.css").read_text(encoding="utf-8").lower()
    for prohibido in ("margen", "markup", "costo", "ganancia", "utilidad", "precio"):
        assert prohibido not in css


def _sin_comentarios(fuente: str) -> str:
    """Quita comentarios CSS y Jinja: lo que queda son reglas y marcado."""
    sin_css = re.sub(r"/\*.*?\*/", " ", fuente, flags=re.DOTALL)
    return re.sub(r"\{#.*?#\}", " ", sin_css, flags=re.DOTALL)


def test_el_sistema_no_sabe_de_negocio() -> None:
    """El CSS y la base son de la empresa, no de un documento.

    Se miran las REGLAS, no los comentarios: los comentarios explican a que
    documento sirve cada cosa y tienen que poder nombrarlos. Lo que no puede
    aparecer es una clase o una cadena que solo tenga sentido para uno, porque
    entonces el sistema ha empezado a resolver el problema de un documento en
    el sitio donde afecta a todos.
    """
    fuentes = [
        (TEMPLATES / "styles" / "document_system.css"),
        (TEMPLATES / "base_document.html"),
    ]
    for ruta in fuentes:
        reglas = _sin_comentarios(ruta.read_text(encoding="utf-8")).lower()
        for prohibido in (
            "quotation",
            "production",
            "tracking",
            "igv",
            "cotiz",
            "almac",
            "receta",
        ):
            assert prohibido not in reglas, f"«{prohibido}» no puede vivir en {ruta.name}"


def test_el_qr_impreso_no_puede_encoger() -> None:
    """PRODUCTION_PDF_QR_SCANNABLE, dicho como un minimo y no como una opinion.

    Se midio sobre el PDF renderizado, decodificando el codigo con OpenCV a
    varias resoluciones: a 26 mm decodifica desde 120 ppp; a 110 ppp ya no. Una
    impresora de oficina trabaja a 300-600 ppp y la foto de un A4 con un movil
    a distancia de lectura queda muy por encima de 120, asi que 26 mm sobra.

    Pero sobra por poco, y el tamano vive en el CSS compartido, donde alguien
    puede ajustarlo pensando solo en como queda la cabecera. Esta prueba fija el
    suelo. Si hace falta bajarlo, hay que volver a medir con un PDF de verdad,
    no estimarlo mirando la pantalla.
    """
    css = (TEMPLATES / "styles" / "document_system.css").read_text(encoding="utf-8")
    medidas = re.findall(r"\.doc-qr img \{[^}]*?width:\s*(\d+(?:\.\d+)?)mm", css, re.DOTALL)
    assert medidas, "el bloque de QR dejo de fijar su tamano impreso"
    assert float(medidas[0]) >= 24, "por debajo de 24 mm el QR deja de leerse impreso"
