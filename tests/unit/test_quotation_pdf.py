"""Pruebas unitarias del motor de documentos comerciales PDF de cotizaciones."""

from __future__ import annotations

from decimal import Decimal

from app.documents.quotation import (
    build_quotation_pdf_document,
    format_currency,
    format_dimensions,
    format_quantity,
    format_tax_label,
    sanitize_pdf_filename,
)
from app.models.masters import Product, ProductType
from app.models.quotations import Quotation, QuotationStatus
from app.models.settings import CommercialSettings, CompanySettings
from app.services.quotation_pdf import QuotationPdfService


def test_format_currency_standard_and_custom_symbols() -> None:
    assert format_currency(Decimal("8.50"), "S/") == "S/ 8.50"
    assert format_currency(Decimal("1250.75"), "S/") == "S/ 1,250.75"
    assert format_currency(Decimal("0"), "S/") == "S/ 0.00"
    assert format_currency(None, "S/") == "S/ 0.00"
    assert format_currency(Decimal("100"), "US$") == "US$ 100.00"
    assert format_currency(Decimal("999999.99"), "$") == "$ 999,999.99"


def test_format_quantity() -> None:
    assert format_quantity(1000) == "1,000"
    assert format_quantity(5) == "5"
    assert format_quantity(Decimal("350.000000")) == "350"
    assert format_quantity(Decimal("12.5")) == "12.5"
    assert format_quantity(None) == "0"


def test_format_tax_label_dynamic() -> None:
    assert format_tax_label(Decimal("18")) == "IGV (18%)"
    assert format_tax_label(Decimal("18.00")) == "IGV (18%)"
    assert format_tax_label(Decimal("10")) == "IGV (10%)"
    assert format_tax_label(Decimal("0")) == "IGV (0%)"
    assert format_tax_label(Decimal("18.5")) == "IGV (18.5%)"
    assert format_tax_label(None) == "IGV (0%)"


def test_format_dimensions_omits_null_or_zero() -> None:
    # All present
    dims = format_dimensions(
        width=Decimal("10.5"),
        height=Decimal("15"),
        length=Decimal("20"),
        depth=Decimal("5"),
    )
    assert dims == "Ancho: 10.5 cm | Alto: 15 cm | Largo: 20 cm | Profundidad: 5 cm"

    # Only width and height
    dims_partial = format_dimensions(
        width=Decimal("12"),
        height=Decimal("25"),
        length=None,
        depth=Decimal(0),
    )
    assert dims_partial == "Ancho: 12 cm | Alto: 25 cm"
    assert "Largo" not in str(dims_partial)
    assert "Profundidad" not in str(dims_partial)

    # None present
    assert format_dimensions() is None


def test_sanitize_pdf_filename() -> None:
    assert sanitize_pdf_filename("CTZ-000123") == "CTZ-000123.pdf"
    assert (
        sanitize_pdf_filename("CTZ-000123", "Restaurante El Fuego")
        == "CTZ-000123_Restaurante_El_Fuego.pdf"
    )
    # Header injection prevention
    assert (
        sanitize_pdf_filename("CTZ-001\r\nSet-Cookie: evil=1", "Client\n\r\"")
        == "CTZ-001Set-Cookieevil1_Client.pdf"
    )
    # Path traversal prevention
    assert sanitize_pdf_filename("../../../etc/passwd", "../client") == "etcpasswd_client.pdf"


def test_build_document_model_uses_snapshots() -> None:
    dummy_product = Product(
        id=1,
        internal_reference="PRD-ACTIVE",
        name="Nombre Producto Maestro",
        product_type=ProductType.FINISHED_PRODUCT,
        base_uom_code="NIU",
    )
    quotation = Quotation(
        id=10,
        code="CTZ-000042",
        name="Pedido Restaurante Mayo",
        status=QuotationStatus.CONFIRMED,
        quantity=500,
        customer_name_snapshot="Cliente Snapshot SAC",
        customer_trade_name_snapshot="Comercial Snapshot",
        customer_document_type_snapshot="RUC",
        customer_document_number_snapshot="20123456789",
        customer_address_snapshot="Av. Las Flores 123",
        customer_email_snapshot="pedidos@clientesnapshot.pe",
        customer_phone_snapshot="987654321",
        product_id=1,
        product=dummy_product,
        product_name_snapshot="Taza Cerámica 350ml Snapshot",
        product_internal_reference_snapshot="PRD-SNAP-01",
        product_uom_snapshot="NIU",
        product_material_snapshot="Greda refractaria",
        product_grammage_snapshot=Decimal("320.00"),
        product_width_snapshot=Decimal("8.50"),
        product_height_snapshot=Decimal("11.00"),
        product_length_snapshot=None,
        product_depth_snapshot=None,
        commercial_sale_unit_price=Decimal("12.00"),
        commercial_subtotal=Decimal("6000.00"),
        tax_percentage_snapshot=Decimal("18.00"),
        tax_amount=Decimal("1080.00"),
        commercial_total=Decimal("7080.00"),
        commercial_unit_price_with_tax=Decimal("14.16"),
    )

    company = CompanySettings(
        id=1,
        legal_name="Cerámica Greda SAC",
        trade_name="Taller Greda",
        tax_id="20555555555",
        address_line1="Jr. Los Alfareros 456",
        district="Lurín",
        province="Lima",
        department="Lima",
        phone="01-4321098",
        email="ventas@greda.pe",
        website="www.greda.pe",
    )

    commercial = CommercialSettings(
        id=1,
        currency_symbol="S/",
        currency_code="PEN",
        quote_validity_days=15,
        general_conditions="Plazo de entrega: 10 días hábiles.",
        payment_notes="50% adelanto y 50% contra entrega.",
        document_footer="Gracias por confiar en nuestra alfarería.",
    )

    doc = build_quotation_pdf_document(
        quotation=quotation,
        company_settings=company,
        commercial_settings=commercial,
        logo_data_uri="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
    )

    assert doc.document.code == "CTZ-000042"
    assert doc.document.name == "Pedido Restaurante Mayo"
    assert doc.document.is_cancelled is False
    assert doc.company.display_name == "Taller Greda"
    assert doc.customer.display_name == "Cliente Snapshot SAC"
    assert doc.customer.document_label == "RUC: 20123456789"
    assert len(doc.items) == 1
    assert doc.items[0].product_name == "Taza Cerámica 350ml Snapshot"
    assert doc.items[0].material == "Greda refractaria"
    assert doc.items[0].grammage_formatted == "320 g"
    assert doc.items[0].dimensions_formatted == "Ancho: 8.5 cm | Alto: 11 cm"
    assert doc.items[0].unit_price_formatted == "S/ 12.00"
    assert doc.items[0].subtotal_formatted == "S/ 6,000.00"
    assert doc.totals.subtotal_formatted == "S/ 6,000.00"
    assert doc.totals.tax_label == "IGV (18%)"
    assert doc.totals.tax_amount_formatted == "S/ 1,080.00"
    assert doc.totals.total_formatted == "S/ 7,080.00"


def test_cancelled_quotation_sets_cancelled_badge_and_watermark() -> None:
    dummy_product = Product(
        id=1,
        internal_reference="PRD-1",
        name="Plato",
        product_type=ProductType.FINISHED_PRODUCT,
        base_uom_code="NIU",
    )
    quotation = Quotation(
        id=20,
        code="CTZ-000099",
        status=QuotationStatus.CANCELLED,
        quantity=100,
        product_id=1,
        product=dummy_product,
        product_name_snapshot="Plato Cerámica",
        commercial_sale_unit_price=Decimal("15.00"),
        commercial_subtotal=Decimal("1500.00"),
        tax_percentage_snapshot=Decimal("18.00"),
        tax_amount=Decimal("270.00"),
        commercial_total=Decimal("1770.00"),
    )

    doc = build_quotation_pdf_document(quotation=quotation)
    assert doc.document.is_cancelled is True

    service = QuotationPdfService(session=None)  # type: ignore[arg-type]
    html = service.render_html(doc)
    assert "watermark" in html
    assert "ANULADA" in html
    assert "COTIZACIÓN ANULADA" in html


def test_missing_optional_fields_render_cleanly() -> None:
    dummy_product = Product(
        id=1,
        internal_reference="PRD-1",
        name="Maceta",
        product_type=ProductType.FINISHED_PRODUCT,
        base_uom_code="NIU",
    )
    quotation = Quotation(
        id=30,
        code="CTZ-000100",
        status=QuotationStatus.CONFIRMED,
        quantity=10,
        product_id=1,
        product=dummy_product,
        commercial_sale_unit_price=Decimal("50.00"),
        commercial_subtotal=Decimal("500.00"),
        tax_percentage_snapshot=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        commercial_total=Decimal("500.00"),
    )

    # Empty company & commercial settings
    doc = build_quotation_pdf_document(
        quotation=quotation,
        company_settings=None,
        commercial_settings=None,
        logo_data_uri=None,
    )

    service = QuotationPdfService(session=None)  # type: ignore[arg-type]
    html = service.render_html(doc)
    assert "None" not in html
    assert "null" not in html
    assert "IGV (0%)" in html
    assert "S/ 500.00" in html

    # PDF generation must succeed
    pdf_bytes = service.render_pdf_from_html(html)
    assert pdf_bytes.startswith(b"%PDF")
