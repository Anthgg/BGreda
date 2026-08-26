"""Pruebas unitarias de previsualización comercial PDF para borradores DRAFT (Fase 006B)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.documents.quotation import (
    build_draft_quotation_pdf_document,
)
from app.models.masters import DocumentType, Partner, PartnerRole
from app.models.quotations import QuotationStatus, QuotationWorkflow
from app.models.settings import CommercialSettings, CompanySettings
from app.schemas.quotation_builder import (
    QuotationBuilderItemOut,
    QuotationBuilderOut,
)
from app.services.quotation_pdf import QuotationPdfService


def test_build_draft_document_model_multiproduct() -> None:
    customer = Partner(
        id=10,
        name="RESTAURANTE EL FUEGO S.A.C.",
        role=PartnerRole.CLIENT,
        document_type=DocumentType.RUC,
        document_number="20601234567",
        address="Av. Mariscal La Mar 1234",
        district="Miraflores",
        province="Lima",
        department="Lima",
        email="contacto@elfuego.pe",
        phone="999888777",
    )

    items = [
        QuotationBuilderItemOut(
            product_id=101,
            product_internal_reference="LAB50001",
            product_name="Jarra Cerámica 1L",
            product_type="FINISHED_PRODUCT",
            product_uom="NIU",
            product_material="Greda Roja",
            product_grammage=Decimal("850.0"),
            width=Decimal("12.0"),
            height=Decimal("18.0"),
            length=Decimal("12.0"),
            depth=None,
            quantity=20,
            commercial_sale_unit_price=Decimal("388.50"),
            commercial_subtotal=Decimal("7770.00"),
            commercial_total=Decimal("9168.60"),
            tax_percentage_snapshot=Decimal("18"),
            tax_amount=Decimal("1398.60"),
            final_unit_cost=Decimal("150.00"),
            final_total_cost=Decimal("3000.00"),
            markup_percent=Decimal("159.00"),
            effective_profit_total=Decimal("4770.00"),
            source_fingerprint="fp1",
            complete=True,
            sort_order=0,
        ),
        QuotationBuilderItemOut(
            product_id=102,
            product_internal_reference="LAB50032",
            product_name="Shot Pisquero Artesanal",
            product_type="FINISHED_PRODUCT",
            product_uom="NIU",
            product_material="Greda Blanca",
            product_grammage=Decimal("90.0"),
            width=Decimal("5.0"),
            height=Decimal("7.0"),
            length=Decimal("5.0"),
            depth=None,
            quantity=50,
            commercial_sale_unit_price=Decimal("25.00"),
            commercial_subtotal=Decimal("1250.00"),
            commercial_total=Decimal("1475.00"),
            tax_percentage_snapshot=Decimal("18"),
            tax_amount=Decimal("225.00"),
            final_unit_cost=Decimal("8.50"),
            final_total_cost=Decimal("425.00"),
            markup_percent=Decimal("194.12"),
            effective_profit_total=Decimal("825.00"),
            source_fingerprint="fp2",
            complete=True,
            sort_order=1,
        ),
    ]

    quotation_out = QuotationBuilderOut(
        id=None,
        code="BORRADOR",
        workflow=QuotationWorkflow.COTIZADOR,
        status=QuotationStatus.DRAFT,
        name="Lote Inauguración Miraflores",
        customer_id=customer.id,
        customer_name_snapshot=customer.name,
        items=items,
        item_count=2,
        commercial_subtotal=Decimal("9020.00"),
        tax_percentage_snapshot=Decimal("18"),
        tax_rate_source_snapshot="COMMERCIAL_SETTINGS",
        tax_amount=Decimal("1623.60"),
        total_with_tax=Decimal("10643.60"),
        currency_code_snapshot="PEN",
        currency_symbol_snapshot="S/",
        complete=True,
        next_step="SUMMARY",
        source_fingerprint="full_fp",
        created_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 25, 12, 30, tzinfo=UTC),
    )

    company = CompanySettings(
        legal_name="GREDA CERÁMICA S.A.C.",
        trade_name="Taller Greda",
        tax_id="20123456789",
        address_line1="Av. Artesanos 456",
        district="Barranco",
        province="Lima",
        department="Lima",
    )
    commercial = CommercialSettings(
        quote_validity_days=15,
        currency_code="PEN",
        currency_symbol="S/",
        general_conditions="Precios incluyen IGV según ley.",
    )

    doc = build_draft_quotation_pdf_document(
        quotation_out=quotation_out,
        customer=customer,
        company_settings=company,
        commercial_settings=commercial,
    )

    assert doc.document.title == "COTIZACIÓN"
    assert doc.document.code == "BORRADOR"
    assert doc.document.status == "DRAFT"
    assert doc.document.is_cancelled is False
    assert doc.customer.display_name == "RESTAURANTE EL FUEGO S.A.C."
    assert doc.customer.document_label == "RUC: 20601234567"
    assert "Miraflores" in (doc.customer.address or "")

    assert len(doc.items) == 2
    assert doc.items[0].product_name == "Jarra Cerámica 1L"
    assert doc.items[0].product_reference == "LAB50001"
    assert doc.items[0].quantity_formatted == "20"
    assert doc.items[0].unit_price_formatted == "S/ 388.50"
    assert doc.items[0].subtotal_formatted == "S/ 7,770.00"
    assert "Ancho: 12 cm" in (doc.items[0].dimensions_formatted or "")

    assert doc.items[1].product_name == "Shot Pisquero Artesanal"
    assert doc.items[1].product_reference == "LAB50032"
    assert doc.items[1].quantity_formatted == "50"
    assert doc.items[1].unit_price_formatted == "S/ 25.00"
    assert doc.items[1].subtotal_formatted == "S/ 1,250.00"

    assert doc.totals.subtotal_formatted == "S/ 9,020.00"
    assert doc.totals.tax_label == "IGV (18%)"
    assert doc.totals.tax_amount_formatted == "S/ 1,623.60"
    assert doc.totals.total_formatted == "S/ 10,643.60"


def test_draft_pdf_rendering_and_privacy() -> None:
    items = [
        QuotationBuilderItemOut(
            product_id=1,
            product_internal_reference="JARRA-01",
            product_name="Jarra Especial",
            product_type="FINISHED_PRODUCT",
            product_uom="NIU",
            quantity=10,
            commercial_sale_unit_price=Decimal("100.00"),
            commercial_subtotal=Decimal("1000.00"),
            commercial_total=Decimal("1180.00"),
            tax_percentage_snapshot=Decimal("18"),
            tax_amount=Decimal("180.00"),
            final_unit_cost=Decimal("45.50"),
            final_total_cost=Decimal("455.00"),
            markup_percent=Decimal("119.78"),
            effective_profit_total=Decimal("545.00"),
            source_fingerprint="fp",
            complete=True,
        )
    ]
    quotation_out = QuotationBuilderOut(
        id=None,
        code="BORRADOR",
        status=QuotationStatus.DRAFT,
        name="Pedido Especial",
        items=items,
        item_count=1,
        commercial_subtotal=Decimal("1000.00"),
        tax_percentage_snapshot=Decimal("18"),
        tax_amount=Decimal("180.00"),
        total_with_tax=Decimal("1180.00"),
        source_fingerprint="fp",
    )

    doc = build_draft_quotation_pdf_document(quotation_out=quotation_out)
    service = QuotationPdfService(None)  # type: ignore[arg-type]
    html = service.render_html(doc)

    # Verificaciones en el HTML renderizado
    assert "Jarra Especial" in html
    assert "S/ 1,000.00" in html
    assert "S/ 1,180.00" in html

    forbidden_strings = [
        "45.50",
        "455.00",
        "119.78",
        "545.00",
        "costo unitario",
        "costo de quema",
        "mano de obra",
        "markup",
        "margen interno",
        "ganancia",
        "profit",
    ]
    html_lower = html.lower()
    for forbidden in forbidden_strings:
        assert forbidden not in html_lower, f"Fuga de información interna: '{forbidden}'"


def test_draft_pdf_multipage_generation() -> None:
    items = []
    for i in range(1, 21):
        items.append(
            QuotationBuilderItemOut(
                product_id=i,
                product_internal_reference=f"REF-{i:03d}",
                product_name=f"Pieza Cerámica Artesanal Modelo {i:03d}",
                product_type="FINISHED_PRODUCT",
                product_uom="NIU",
                quantity=10 * i,
                commercial_sale_unit_price=Decimal("50.00"),
                commercial_subtotal=Decimal(500 * i),
                commercial_total=Decimal(590 * i),
                tax_percentage_snapshot=Decimal("18"),
                tax_amount=Decimal(90 * i),
                source_fingerprint=f"fp_{i}",
                complete=True,
                sort_order=i,
            )
        )

    subtotal = sum((item.commercial_subtotal for item in items), Decimal(0))
    tax = sum((item.tax_amount for item in items), Decimal(0))
    total = subtotal + tax

    quotation_out = QuotationBuilderOut(
        id=None,
        code="BORRADOR",
        status=QuotationStatus.DRAFT,
        name="Catálogo Completo Restaurante",
        items=items,
        item_count=len(items),
        commercial_subtotal=subtotal,
        tax_percentage_snapshot=Decimal("18"),
        tax_amount=tax,
        total_with_tax=total,
        source_fingerprint="fp_multi",
    )

    doc = build_draft_quotation_pdf_document(quotation_out=quotation_out)
    service = QuotationPdfService(None)  # type: ignore[arg-type]
    html = service.render_html(doc)

    assert "REF-020" in html
    assert "TOTAL" in html
    assert len(doc.items) == 20
