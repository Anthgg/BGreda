"""Pruebas compactas de cardinalidad y privacidad comercial multiproducto."""

from decimal import Decimal

import pytest

from app.documents.quotation import build_quotation_pdf_document
from app.models.quotations import (
    Quotation,
    QuotationItem,
    QuotationStatus,
    QuotationWorkflow,
)


@pytest.mark.parametrize("item_count", [1, 2, 5])
def test_pdf_document_supports_one_two_and_five_products(item_count: int) -> None:
    lines = [
        QuotationItem(
            id=index,
            product_id=index,
            sort_order=index,
            quantity=index * 10,
            product_name_snapshot=f"Producto {index}",
            product_internal_reference_snapshot=f"LAB{index:03d}",
            product_type_snapshot="FINISHED_PRODUCT",
            product_uom_snapshot="NIU",
            product_width_snapshot=Decimal("10"),
            product_height_snapshot=Decimal("5"),
            product_length_snapshot=Decimal("10"),
            commercial_sale_unit_price=Decimal("8.50"),
            commercial_subtotal=Decimal(index * 10) * Decimal("8.50"),
            source_fingerprint="a" * 64,
        )
        for index in range(1, item_count + 1)
    ]
    subtotal = sum((line.commercial_subtotal for line in lines), Decimal(0))
    tax = subtotal * Decimal("0.18")
    quotation = Quotation(
        id=99,
        code="CTZ-2026-000099",
        name="Prueba multiproducto",
        status=QuotationStatus.CONFIRMED,
        workflow=QuotationWorkflow.COTIZADOR,
        product_id=None,
        quantity=None,
        commercial_factor_default_snapshot=Decimal(2),
        commercial_factor=Decimal(2),
        commercial_subtotal=subtotal,
        commercial_total=subtotal + tax,
        tax_percentage_snapshot=Decimal(18),
        tax_amount=tax,
        total_with_tax=subtotal + tax,
        currency_code_snapshot="PEN",
        currency_symbol_snapshot="S/",
        source_fingerprint="b" * 64,
        items=lines,
    )

    document = build_quotation_pdf_document(quotation)

    assert len(document.items) == item_count
    assert [item.item_number for item in document.items] == list(range(1, item_count + 1))
    serialized = repr(document)
    assert "final_unit_cost" not in serialized
    assert "markup_percent" not in serialized
    assert "effective_profit" not in serialized
