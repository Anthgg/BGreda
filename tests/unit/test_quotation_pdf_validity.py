"""Fase 009G — la vigencia de un documento confirmado no la mueve la configuracion.

El PDF decia «Cotizacion valida por N dias» leyendo
`commercial_settings.quote_validity_days` en el momento de generarse. Ese
numero es configuracion viva: pasarlo de 20 a 30 reescribia la vigencia de
todos los documentos ya entregados, incluidos los firmados.

Estas pruebas fijan las tres respuestas correctas: la confirmada lee su
snapshot, la confirmada antigua sin snapshot no dice nada, y el borrador —que
todavia no ha emitido nada— si mira el ajuste vigente.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.documents.quotation import (
    build_draft_quotation_pdf_document,
    build_quotation_pdf_document,
)
from app.models.masters import Product, ProductType
from app.models.quotations import Quotation, QuotationStatus, QuotationWorkflow
from app.models.settings import CommercialSettings
from app.schemas.quotation_builder import QuotationBuilderItemOut, QuotationBuilderOut


def _producto() -> Product:
    return Product(
        id=1,
        internal_reference="LAB50032",
        name="Shot pisquero",
        product_type=ProductType.FINISHED_PRODUCT,
        base_uom_code="NIU",
    )


def _confirmada(
    *,
    validity_days_snapshot: int | None,
    currency: str = "PEN",
    exchange_rate: Decimal | None = None,
) -> Quotation:
    return Quotation(
        id=349,
        code="CTZ-2026-000347",
        status=QuotationStatus.CONFIRMED,
        quantity=8,
        product_id=1,
        product=_producto(),
        product_name_snapshot="Shot pisquero",
        confirmed_at=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
        created_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
        currency_code_snapshot=currency,
        currency_symbol_snapshot="US$" if currency == "USD" else "S/",
        exchange_rate_snapshot=exchange_rate,
        exchange_rate_source_snapshot="MANUAL" if currency == "USD" else None,
        validity_days_snapshot=validity_days_snapshot,
        commercial_sale_unit_price=Decimal("8.50"),
        commercial_subtotal=Decimal("68.00"),
        tax_percentage_snapshot=Decimal("18.00"),
        tax_amount=Decimal("12.24"),
        commercial_total=Decimal("80.24"),
    )


def _ajuste(dias: int | None) -> CommercialSettings:
    return CommercialSettings(
        id=1, currency_code="PEN", currency_symbol="S/", quote_validity_days=dias
    )


def test_la_confirmada_usa_su_snapshot_y_no_el_ajuste_vigente() -> None:
    """CONFIRMED_PDF_VALIDITY_IMMUTABLE.

    Se confirmo con 20. Que hoy la configuracion diga 30 no puede cambiar lo
    que dice un documento ya emitido.
    """
    doc = build_quotation_pdf_document(
        quotation=_confirmada(validity_days_snapshot=20),
        commercial_settings=_ajuste(30),
    )
    assert doc.document.validity_date == "20 días calendario"
    assert doc.conditions.validity_text == "Cotización válida por 20 días."
    assert "30" not in str(doc.document.validity_date)


def test_el_pdf_confirmado_se_regenera_identico_tras_cambiar_la_configuracion() -> None:
    """CONFIRMED_PDF_REGENERATES_IDENTICALLY, en lo que 009G puede mover.

    Se genera el mismo documento con dos configuraciones distintas. Si algo del
    contrato dependiera del ajuste vivo, las dos salidas diferirian.
    """
    quotation = _confirmada(validity_days_snapshot=20)

    antes = build_quotation_pdf_document(quotation=quotation, commercial_settings=_ajuste(20))
    despues = build_quotation_pdf_document(quotation=quotation, commercial_settings=_ajuste(30))

    assert antes.document == despues.document
    assert antes.conditions.validity_text == despues.conditions.validity_text
    assert antes.totals == despues.totals
    # Y la fecha de emision sigue saliendo de `confirmed_at`, no del reloj.
    assert antes.document.emission_date == "31/08/2026"


def test_una_confirmada_sin_snapshot_no_muestra_vigencia() -> None:
    """Confirmadas anteriores a 009G, en soles.

    No hay registro de cuanto valia el ajuste cuando se confirmaron. Poner el
    de hoy les inventaria una vigencia que nadie acordo, y con aspecto de dato
    real. Callar es la unica respuesta honesta.
    """
    doc = build_quotation_pdf_document(
        quotation=_confirmada(validity_days_snapshot=None),
        commercial_settings=_ajuste(30),
    )
    assert doc.document.validity_date is None
    assert doc.conditions.validity_text is None


def test_una_confirmada_en_dolares_sin_snapshot_tampoco_la_inventa() -> None:
    """Lo mismo en USD: la moneda no cambia la regla."""
    doc = build_quotation_pdf_document(
        quotation=_confirmada(
            validity_days_snapshot=None, currency="USD", exchange_rate=Decimal("3.31")
        ),
        commercial_settings=_ajuste(30),
    )
    assert doc.document.validity_date is None
    assert doc.conditions.validity_text is None
    assert doc.document.currency_code == "USD"


def test_el_documento_no_inventa_una_fecha_de_vencimiento() -> None:
    """El diseño nunca mostro un «valida hasta».

    009G congela el plazo; no estrena contenido contractual. Calcular una fecha
    de vencimiento seria afirmar algo que el documento entregado no decia.
    """
    doc = build_quotation_pdf_document(
        quotation=_confirmada(validity_days_snapshot=20),
        commercial_settings=_ajuste(20),
    )
    assert doc.document.validity_date == "20 días calendario"
    assert "hasta" not in (doc.conditions.validity_text or "").lower()


def _borrador() -> QuotationBuilderOut:
    return QuotationBuilderOut(
        id=None,
        code="BORRADOR",
        workflow=QuotationWorkflow.COTIZADOR,
        status=QuotationStatus.DRAFT,
        name="Borrador",
        customer_id=None,
        items=[
            QuotationBuilderItemOut(
                product_id=1,
                product_internal_reference="LAB50032",
                product_name="Shot pisquero",
                product_type="FINISHED_PRODUCT",
                product_uom="NIU",
                quantity=8,
                commercial_sale_unit_price=Decimal("8.50"),
                commercial_subtotal=Decimal("68.00"),
                commercial_total=Decimal("80.24"),
                tax_percentage_snapshot=Decimal("18"),
                tax_amount=Decimal("12.24"),
                final_unit_cost=Decimal("3.00"),
                final_total_cost=Decimal("24.00"),
                markup_percent=Decimal("100"),
                effective_profit_total=Decimal("44.00"),
                source_fingerprint="fp",
                complete=True,
                sort_order=0,
            )
        ],
        item_count=1,
        commercial_subtotal=Decimal("68.00"),
        tax_percentage_snapshot=Decimal("18"),
        tax_rate_source_snapshot="COMMERCIAL_SETTINGS",
        tax_amount=Decimal("12.24"),
        total_with_tax=Decimal("80.24"),
        currency_code_snapshot="PEN",
        currency_symbol_snapshot="S/",
        complete=True,
        next_step="SUMMARY",
        source_fingerprint="fp",
        created_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 31, 9, 0, tzinfo=UTC),
    )


def test_el_borrador_si_refleja_el_ajuste_vigente() -> None:
    """Un borrador no ha emitido nada.

    Aqui el ajuste vivo es la respuesta correcta: el numero que se ve es el que
    quedara congelado si se confirma hoy.
    """
    doc = build_draft_quotation_pdf_document(
        quotation_out=_borrador(), commercial_settings=_ajuste(30)
    )
    assert doc.document.validity_date == "30 días calendario"
    assert doc.conditions.validity_text == "Cotización válida por 30 días."


def test_el_borrador_sin_vigencia_configurada_no_muestra_nada() -> None:
    doc = build_draft_quotation_pdf_document(
        quotation_out=_borrador(), commercial_settings=_ajuste(None)
    )
    assert doc.document.validity_date is None
    assert doc.conditions.validity_text is None
