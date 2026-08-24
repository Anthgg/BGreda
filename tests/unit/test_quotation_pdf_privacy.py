"""Pruebas de privacidad comercial: garantiza que costos internos o markup no se filtren al PDF."""

from __future__ import annotations

from decimal import Decimal

from app.documents.quotation import build_quotation_pdf_document
from app.models.masters import Product, ProductType
from app.models.quotations import Quotation, QuotationStatus
from app.services.quotation_pdf import QuotationPdfService


def test_internal_costs_and_markups_are_strictly_omitted_from_pdf() -> None:
    """Valida exhaustivamente que costos de materiales, mano de obra, quema, markup y utilidades

    queden completamente excluidos del modelo de documento, del HTML y del PDF.
    """
    dummy_product = Product(
        id=99,
        internal_reference="PRD-CONFIDENTIAL",
        name="Florero Artesanal",
        product_type=ProductType.FINISHED_PRODUCT,
        base_uom_code="NIU",
    )

    # Valores internos confidenciales
    internal_unit_cost = Decimal("4.173829")
    internal_total_cost = Decimal("4173.829000")
    markup_pct = Decimal("115.827338")
    target_profit_unit = Decimal("4.834000")
    effective_profit_unit = Decimal("4.826171")
    effective_profit_total = Decimal("4826.171000")
    calculated_sale_price = Decimal("8.347658")
    suggested_commercial_price = Decimal("8.500000")
    materials_cost = Decimal("1.250000")
    labor_cost = Decimal("2.000000")
    firing_cost = Decimal("0.923829")
    space_cost = Decimal("0.500000")
    commercial_factor = Decimal("2.000000")
    source_fingerprint_secret = "secret_fingerprint_hash_987654321"

    # Precio comercial oficial que SI debe aparecer
    official_commercial_sale_unit_price = Decimal("9.00")
    official_subtotal = Decimal("9000.00")
    official_tax_amount = Decimal("1620.00")
    official_total = Decimal("10620.00")

    quotation = Quotation(
        id=77,
        code="CTZ-000777",
        name="Cotizacion Privada",
        status=QuotationStatus.CONFIRMED,
        quantity=1000,
        customer_name_snapshot="Cliente Auditor SAC",
        product_id=99,
        product=dummy_product,
        product_name_snapshot="Florero Artesanal Granate",
        product_internal_reference_snapshot="FLOR-001",
        product_material_snapshot="Greda roja",
        product_grammage_snapshot=Decimal("450.00"),
        product_width_snapshot=Decimal("15.00"),
        product_height_snapshot=Decimal("25.00"),
        # Datos internos confidenciales
        materials_calculated=materials_cost,
        materials_applied=materials_cost,
        labor_cost=labor_cost,
        firing_cost=firing_cost,
        space_cost=space_cost,
        commercial_factor=commercial_factor,
        commercial_factor_default_snapshot=commercial_factor,
        final_unit_cost=internal_unit_cost,
        final_total_cost=internal_total_cost,
        markup_percent=markup_pct,
        target_profit_unit=target_profit_unit,
        calculated_sale_unit_price=calculated_sale_price,
        suggested_commercial_unit_price=suggested_commercial_price,
        effective_profit_unit=effective_profit_unit,
        effective_profit_total=effective_profit_total,
        effective_markup_percent=markup_pct,
        source_fingerprint=source_fingerprint_secret,
        # Datos comerciales oficiales
        commercial_sale_unit_price=official_commercial_sale_unit_price,
        commercial_subtotal=official_subtotal,
        tax_percentage_snapshot=Decimal("18.00"),
        tax_amount=official_tax_amount,
        commercial_total=official_total,
    )

    doc = build_quotation_pdf_document(quotation=quotation)
    service = QuotationPdfService(session=None)  # type: ignore[arg-type]
    html = service.render_html(doc)

    # 1. El único precio unitario en el documento debe ser el precio comercial oficial
    assert "S/ 9.00" in html
    assert "S/ 9,000.00" in html
    assert "S/ 1,620.00" in html
    assert "S/ 10,620.00" in html

    # 2. Palabras clave prohibidas
    forbidden_terms = [
        "costo de material",
        "costo de quema",
        "costo de producci",
        "mano de obra",
        "costo unitario",
        "costo total",
        "markup",
        "ganancia",
        "utilidad",
        "precio calculado",
        "precio sugerido",
        "margen",
        "fingerprint",
        "recipe_id",
        "firing_id",
        "recipe_version_id",
    ]
    html_lower = html.lower()
    for term in forbidden_terms:
        assert term not in html_lower, f"Término '{term}' encontrado en el HTML comercial"

    # 3. Cifras confidenciales exactas
    forbidden_values = [
        str(internal_unit_cost),
        "4.17",
        str(internal_total_cost),
        "4173",
        str(markup_pct),
        "115.82",
        str(target_profit_unit),
        str(effective_profit_unit),
        str(effective_profit_total),
        "4826",
        str(calculated_sale_price),
        "8.34",
        str(suggested_commercial_price),
        "8.50",
        str(materials_cost),
        str(labor_cost),
        str(firing_cost),
        source_fingerprint_secret,
    ]
    for val in forbidden_values:
        assert val not in html, f"Cifra confidencial '{val}' encontrada en el HTML comercial"
