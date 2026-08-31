"""Fase 009G — lo que de verdad queda impreso en el PDF.

Las otras pruebas miran el modelo de vista y el HTML. Eso deja fuera al ultimo
eslabon: WeasyPrint es quien decide que se imprime, y un texto puede quedar
recortado, superpuesto o fuera de pagina sin que el HTML lo delate.

Aqui se renderiza el PDF de verdad y se lee su texto. Es la unica prueba que
sabria decir que la tasa esta en el HTML pero no llego al papel.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.documents.quotation import build_quotation_pdf_document
from app.models.masters import Product, ProductType
from app.models.quotations import Quotation, QuotationItem, QuotationStatus
from app.models.settings import CommercialSettings, CompanySettings
from app.services.quotation_pdf import QuotationPdfService

pypdf = pytest.importorskip("pypdf", reason="pypdf es dependencia de desarrollo")


def _texto_del_pdf(pdf_bytes: bytes) -> str:
    """Texto de TODAS las paginas, con los espacios normalizados.

    Sin normalizar, el salto de linea que el maquetador mete a mitad de una
    frase hace fallar una comparacion que deberia pasar: «Tipo de cambio:\n1
    USD» y «Tipo de cambio: 1 USD» dicen lo mismo en el papel.
    """
    lector = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    crudo = " ".join(pagina.extract_text() or "" for pagina in lector.pages)
    return " ".join(crudo.split())


def _cotizacion(
    *,
    currency: str,
    rate: Decimal | None,
    validity_days: int | None,
) -> Quotation:
    producto = Product(
        id=1,
        internal_reference="LAB50032",
        name="Shot pisquero",
        product_type=ProductType.FINISHED_PRODUCT,
        base_uom_code="NIU",
    )
    quotation = Quotation(
        id=349,
        code="CTZ-2026-000347",
        name="009F caso canonico PEN-USD",
        status=QuotationStatus.CONFIRMED,
        quantity=8,
        product_id=1,
        product=producto,
        product_name_snapshot="Shot pisquero",
        customer_name_snapshot="ANA MARIA CISNEROS VELARDE DE BUTRICH",
        customer_document_type_snapshot="DNI",
        customer_document_number_snapshot="7799614",
        confirmed_at=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
        currency_code_snapshot=currency,
        currency_symbol_snapshot="US$" if currency == "USD" else "S/",
        exchange_rate_snapshot=rate,
        exchange_rate_source_snapshot="MANUAL" if rate is not None else None,
        validity_days_snapshot=validity_days,
        commercial_sale_unit_price=Decimal("8.50"),
        commercial_subtotal=Decimal("68.00"),
        tax_percentage_snapshot=Decimal("18.00"),
        tax_amount=Decimal("12.24"),
        commercial_total=Decimal("80.24"),
    )
    quotation.items = [
        QuotationItem(
            quotation_id=349,
            product_id=1,
            product_name_snapshot="Shot pisquero",
            product_internal_reference_snapshot="LAB50032",
            product_width_snapshot=Decimal("6"),
            product_height_snapshot=Decimal("8"),
            product_length_snapshot=Decimal("6"),
            quantity=8,
            sort_order=0,
            commercial_sale_unit_price=Decimal("8.50"),
            commercial_subtotal=Decimal("68.00"),
        )
    ]
    return quotation


def _empresa() -> CompanySettings:
    return CompanySettings(id=1, legal_name="GREDA CERAMICA S.A.C.", tax_id="20123456789")


def _renderizar(quotation: Quotation, dias_configurados: int | None = 30) -> str:
    servicio = QuotationPdfService(session=None)  # type: ignore[arg-type]
    documento = build_quotation_pdf_document(
        quotation=quotation,
        company_settings=_empresa(),
        commercial_settings=CommercialSettings(
            id=1,
            currency_code="PEN",
            currency_symbol="S/",
            quote_validity_days=dias_configurados,
        ),
    )
    try:
        pdf = servicio.render_pdf_from_html(servicio.render_html(documento))
    except (OSError, ImportError) as exc:  # pragma: no cover
        pytest.skip(f"WeasyPrint sin librerias nativas: {exc}")
    assert pdf.startswith(b"%PDF")
    return _texto_del_pdf(pdf)


def test_el_pdf_impreso_en_dolares_dice_la_tasa_y_la_vigencia_congelada() -> None:
    """PDF_TEXT_EXTRACTION sobre el binario, no sobre su HTML de entrada."""
    texto = _renderizar(
        _cotizacion(currency="USD", rate=Decimal("3.310000"), validity_days=20),
        dias_configurados=30,
    )

    assert "CTZ-2026-000347" in texto
    assert "ANA MARIA CISNEROS VELARDE DE BUTRICH" in texto
    assert "Shot pisquero" in texto
    assert "USD" in texto
    assert "US$" in texto
    assert "Tipo de cambio: 1 USD = S/ 3.31" in texto
    assert "31/08/2026" in texto
    # La vigencia impresa es la congelada, no el 30 que dice la configuracion.
    assert "20 días calendario" in texto
    assert "Cotización válida por 20 días." in texto
    assert "30 días" not in texto
    # Y los importes son los que calculo el backend.
    assert "US$ 68.00" in texto
    assert "US$ 80.24" in texto


def test_el_pdf_impreso_en_soles_no_menciona_ninguna_tasa() -> None:
    texto = _renderizar(_cotizacion(currency="PEN", rate=None, validity_days=20))

    assert "S/ 80.24" in texto
    assert "Tipo de cambio" not in texto
    assert "Moneda: PEN" in texto
    # Buscar «USD» a secas no vale: el nombre de esta cotizacion es «009F caso
    # canonico PEN-USD», y una asercion asi fallaria por el titulo, no por la
    # moneda. Lo que no puede aparecer es la moneda de emision en dolares.
    assert "Moneda: USD" not in texto
    assert "US$" not in texto


def test_el_pdf_de_una_confirmada_sin_vigencia_no_la_menciona() -> None:
    """Confirmadas anteriores a 009G: el papel calla en vez de suponer."""
    texto = _renderizar(
        _cotizacion(currency="PEN", rate=None, validity_days=None), dias_configurados=30
    )

    assert "CTZ-2026-000347" in texto
    assert "Vigencia" not in texto
    assert "días calendario" not in texto
    assert "Cotización válida por" not in texto


def test_la_tasa_se_imprime_una_sola_vez() -> None:
    """PDF_ONE_EXCHANGE_RATE_PER_DOCUMENT, contado sobre el papel."""
    texto = _renderizar(_cotizacion(currency="USD", rate=Decimal("3.310000"), validity_days=20))

    assert texto.count("1 USD = S/ 3.31") == 1


def test_el_pdf_no_filtra_costos_internos() -> None:
    """PDF_NO_INTERNAL_COST_LEAK, sobre el texto realmente impreso.

    El cliente recibe este archivo. Un costo tecnico impreso ahi no se puede
    retirar despues.
    """
    texto = _renderizar(
        _cotizacion(currency="USD", rate=Decimal("3.310000"), validity_days=20)
    ).lower()

    for filtracion in (
        "technical_cost",
        "firing_cost",
        "factored_cost",
        "fixed_cost",
        "commercial_base",
        "labor_cost",
        "costo tecnico",
        "costo de quema",
        "margen",
    ):
        assert filtracion not in texto, f"el PDF filtra {filtracion}"


def test_el_pdf_no_ensena_codigos_internos() -> None:
    """PDF_RAW_DOMAIN_CODES_VISIBLE = 0."""
    texto = _renderizar(_cotizacion(currency="USD", rate=Decimal("3.310000"), validity_days=20))

    for codigo in (
        "EXCHANGE_RATE_REQUIRED",
        "PRODUCTION_DIMENSIONS_REQUIRED",
        "COMMERCIAL_SETTINGS",
        "MANUAL",
        "None",
        "null",
    ):
        assert codigo not in texto, f"el PDF ensena el codigo {codigo}"
