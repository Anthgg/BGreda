"""Fase 009G — el PDF de una cotizacion en dolares dice con que tasa se calculo.

El documento mostraba «Moneda: USD (US$)» y nada mas. Un cliente que recibia
esa cotizacion no tenia forma de reproducir el precio, y meses despues nadie
podia explicar de donde salio la cifra sin buscar en la base.

La tasa sale del snapshot congelado de la cotizacion, no de la configuracion
actual: es el numero que produjo ESTOS importes. Y en soles no se muestra
ninguna, porque no hubo conversion que contar.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.documents.quotation import (
    build_quotation_pdf_document,
    format_exchange_rate,
)
from app.models.masters import Product, ProductType
from app.models.quotations import Quotation, QuotationStatus
from app.services.quotation_pdf import QuotationPdfService


@pytest.mark.parametrize(
    ("tasa", "moneda", "esperado"),
    [
        # La columna guarda escala 6. El documento no ensena los ceros de la
        # escala, que no dicen nada y ocupan sitio.
        (Decimal("3.310000"), "USD", "1 USD = S/ 3.31"),
        # Pero nunca baja de dos decimales: «S/ 4» se lee como un numero
        # redondeado, y esta tasa es exacta.
        (Decimal("4.000000"), "USD", "1 USD = S/ 4.00"),
        # Y no recorta precision real.
        (Decimal("3.756789"), "USD", "1 USD = S/ 3.756789"),
        # En soles no hubo conversion.
        (None, "PEN", None),
        # Una tasa guardada en una cotizacion en soles seria un dato corrupto:
        # tampoco se ensena.
        (Decimal("3.31"), "PEN", None),
        # USD sin tasa es una incoherencia historica. No se rellena con la de
        # hoy: eso explicaria el precio con un numero que no lo produjo.
        (None, "USD", None),
    ],
)
def test_la_tasa_se_lee_como_una_frase(
    tasa: Decimal | None, moneda: str, esperado: str | None
) -> None:
    assert format_exchange_rate(tasa, moneda) == esperado


def _confirmada(*, currency: str, rate: Decimal | None) -> Quotation:
    return Quotation(
        id=349,
        code="CTZ-2026-000347",
        status=QuotationStatus.CONFIRMED,
        quantity=8,
        product_id=1,
        product=Product(
            id=1,
            internal_reference="LAB50032",
            name="Shot pisquero",
            product_type=ProductType.FINISHED_PRODUCT,
            base_uom_code="NIU",
        ),
        product_name_snapshot="Shot pisquero",
        confirmed_at=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
        currency_code_snapshot=currency,
        currency_symbol_snapshot="US$" if currency == "USD" else "S/",
        exchange_rate_snapshot=rate,
        exchange_rate_source_snapshot="MANUAL" if rate is not None else None,
        validity_days_snapshot=20,
        commercial_sale_unit_price=Decimal("8.50"),
        commercial_subtotal=Decimal("68.00"),
        tax_percentage_snapshot=Decimal("18.00"),
        tax_amount=Decimal("12.24"),
        commercial_total=Decimal("80.24"),
    )


def test_el_documento_en_dolares_lleva_la_tasa_del_snapshot() -> None:
    """PDF_USD_RATE_VISIBLE y PDF_USD_USES_RATE_SNAPSHOT."""
    doc = build_quotation_pdf_document(
        quotation=_confirmada(currency="USD", rate=Decimal("3.310000"))
    )
    assert doc.document.exchange_rate_text == "1 USD = S/ 3.31"
    assert doc.document.currency_code == "USD"
    assert doc.document.currency_symbol == "US$"


def test_el_documento_en_soles_no_lleva_tasa() -> None:
    """PDF_PEN_RATE_HIDDEN."""
    doc = build_quotation_pdf_document(quotation=_confirmada(currency="PEN", rate=None))
    assert doc.document.exchange_rate_text is None


def test_la_plantilla_dibuja_la_fila_solo_en_dolares() -> None:
    """La regla vista donde importa: en el HTML que WeasyPrint convierte.

    Comprobarlo en el modelo de vista no basta. La fila podria quedarse sin
    dibujar en la plantilla y el modelo seguiria pasando la prueba.
    """
    service = QuotationPdfService(session=None)  # type: ignore[arg-type]

    dolares = service.render_html(
        build_quotation_pdf_document(
            quotation=_confirmada(currency="USD", rate=Decimal("3.310000"))
        )
    )
    assert "Tipo de cambio:" in dolares
    assert "1 USD = S/ 3.31" in dolares
    assert "US$ 80.24" in dolares

    soles = service.render_html(
        build_quotation_pdf_document(quotation=_confirmada(currency="PEN", rate=None))
    )
    assert "Tipo de cambio" not in soles
    assert "None" not in soles


def test_una_confirmada_en_dolares_sin_tasa_no_usa_la_configuracion() -> None:
    """NO FALLBACK HISTORICO FALSO.

    Es una incoherencia historica, y se trata como tal: el documento omite la
    tasa en vez de inventar una que no produjo estos importes.
    """
    service = QuotationPdfService(session=None)  # type: ignore[arg-type]
    html = service.render_html(
        build_quotation_pdf_document(quotation=_confirmada(currency="USD", rate=None))
    )
    assert "Tipo de cambio" not in html
    # Y la moneda si se sigue diciendo: el cliente tiene que saber en que se
    # le cobra, aunque falte la tasa.
    assert "USD" in html


def test_el_documento_no_ensena_una_sola_tasa_por_linea() -> None:
    """PDF_ONE_EXCHANGE_RATE_PER_DOCUMENT.

    La tasa es de la cotizacion entera. Repetirla por linea abriria la puerta a
    que dos lineas mostraran tasas distintas y a un total que nadie cuadra.
    """
    service = QuotationPdfService(session=None)  # type: ignore[arg-type]
    html = service.render_html(
        build_quotation_pdf_document(
            quotation=_confirmada(currency="USD", rate=Decimal("3.310000"))
        )
    )
    assert html.count("1 USD = S/ 3.31") == 1
