"""Fase 010K — el documento del cliente de Solo Quema V2.

Es el MISMO papel que un CTZ: membrete, cabecera, cliente, tabla, totales,
condiciones y pie los dibuja `QuotationPdfService`. Lo propio de Solo Quema es
lo que se imprime y lo que NO:

- se imprimen las piezas con su cantidad y sus medidas, el horno, el servicio
  («Quema baja + alta») y la modalidad (compartida o exclusiva), y al pie
  subtotal, IGV y total (hoja «PDF Quema»);
- **no se imprime precio por pieza**: el documento cobra UN servicio, y la hoja
  no le pone precio a cada fila. Por eso `show_line_amounts=False`;
- **no se imprimen ocupacion, hornadas, gas, costo real, factor ni ganancia**
  («PDF Quema», A24). No se ocultan: este constructor no los lee.

Todo sale de lo CONGELADO al emitir. Lo unico vivo es la identidad de la casa
—logo, razon social, cuentas, pie—, como en los demas documentos.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import nombre_de_actor
from app.core.errors import APIError
from app.core.quoter_v2_lifecycle import business_date
from app.documents.common import (
    build_company_doc_info,
    format_date_display,
    sanitize_pdf_filename,
)
from app.documents.quotation import (
    CommercialDocConditions,
    CustomerDocInfo,
    DocumentHeaderInfo,
    QuotationDocItem,
    QuotationDocTotals,
    QuotationPdfDocument,
    _build_bank_accounts_doc,
    format_currency,
    format_dimensions,
    format_quantity,
)
from app.models.firing_quotation_v2 import V2FiringQuotation, V2FiringQuotationLine
from app.models.quoter_v2 import V2FiringMode, V2QuotationStatus
from app.services.firing_quotation_v2 import service_label
from app.services.quotation_pdf import QuotationPdfService

ZERO = Decimal(0)
TITULO = "COTIZACIÓN DE QUEMA"
UNIDAD = "NIU"

MODO_TEXTO = {
    V2FiringMode.SHARED: "Quema compartida",
    V2FiringMode.EXCLUSIVE: "Quema exclusiva (hornadas completas)",
}


class V2FiringQuotationPdfDraftBlockedError(APIError):
    status_code = 409
    code = "V2_FQ_PDF_DRAFT_BLOCKED"
    message = "Emita la cotizacion de quema antes de descargar su PDF"


class V2FiringQuotationPdfNotIssuedError(APIError):
    status_code = 409
    code = "V2_FQ_PDF_NOT_ISSUED"
    message = "Esta cotizacion se anulo sin llegar a emitirse y no tiene documento"


def _exigir_emitida(quotation: V2FiringQuotation) -> None:
    if quotation.status is V2QuotationStatus.DRAFT:
        raise V2FiringQuotationPdfDraftBlockedError()
    if quotation.issued_at is None:
        raise V2FiringQuotationPdfNotIssuedError()


def build_firing_quotation_pdf_document(
    quotation: V2FiringQuotation,
    lines: list[V2FiringQuotationLine],
    *,
    company: object,
    bank_accounts: Sequence[object],
    document_footer: str | None,
    expired: bool,
) -> QuotationPdfDocument:
    """El ViewModel del documento. Funcion pura: se prueba sin base de datos."""
    _exigir_emitida(quotation)
    assert quotation.issued_at is not None
    simbolo = quotation.currency_symbol_snapshot or "S/"
    moneda = quotation.currency_code_snapshot or "PEN"
    porcentaje = quotation.tax_percent_snapshot or ZERO
    cancelada = quotation.status is V2QuotationStatus.CANCELLED

    items = [
        QuotationDocItem(
            item_number=numero,
            product_name=linea.product_name_snapshot or "Pieza",
            dimensions_formatted=format_dimensions(
                length=linea.length_cm, width=linea.width_cm, height=linea.height_cm
            ),
            quantity=linea.quantity,
            quantity_formatted=format_quantity(linea.quantity),
            unit_of_measure=UNIDAD,
        )
        for numero, linea in enumerate(lines, start=1)
    ]

    vigencia = None
    if quotation.valid_until is not None:
        vigencia = f"Válida hasta el {format_date_display(quotation.valid_until)}"
        if quotation.validity_days_snapshot:
            vigencia += f" ({quotation.validity_days_snapshot} días desde su emisión)"
        vigencia += "."

    # Lo que describe el servicio contratado. El horno y la modalidad son
    # terminos comerciales: una exclusiva reserva hornadas completas y eso es
    # lo que respalda su precio.
    servicio = [
        f"Servicio: {service_label(quotation.low_fire_enabled, quotation.high_fire_enabled)}.",
        f"Modalidad: {MODO_TEXTO[quotation.firing_mode]}.",
    ]
    if quotation.kiln_name_snapshot:
        servicio.append(f"Horno: {quotation.kiln_name_snapshot}.")
    if quotation.glaze_enabled:
        servicio.append("Incluye vidriado.")
    if quotation.client_notes:
        servicio.append(quotation.client_notes)

    return QuotationPdfDocument(
        company=company,  # type: ignore[arg-type]
        customer=CustomerDocInfo(
            name=quotation.customer_name_snapshot or "Cliente",
            document_type=quotation.customer_document_type_snapshot,
            document_number=quotation.customer_document_number_snapshot,
            address=quotation.customer_address_snapshot,
            email=quotation.customer_email_snapshot,
            phone=quotation.customer_phone_snapshot,
        ),
        document=DocumentHeaderInfo(
            title=TITULO,
            code=quotation.code,
            # El nombre es interno, para reconocerla en el listado.
            name=None,
            status=quotation.status.value,
            is_cancelled=cancelada,
            is_expired=expired and not cancelada,
            emission_date=format_date_display(business_date(quotation.issued_at)),
            validity_date=(
                format_date_display(quotation.valid_until) if quotation.valid_until else None
            ),
            currency_symbol=simbolo,
            currency_code=moneda,
            prepared_by=nombre_de_actor(quotation.issued_by_name or quotation.created_by_name),
        ),
        items=items,
        totals=QuotationDocTotals(
            subtotal_formatted=format_currency(quotation.subtotal_amount, simbolo),
            tax_percentage=porcentaje,
            tax_label=f"IGV ({format(porcentaje.normalize(), 'f')}%)",
            tax_amount_formatted=format_currency(quotation.tax_amount, simbolo),
            total_formatted=format_currency(quotation.total_amount, simbolo),
        ),
        conditions=CommercialDocConditions(
            validity_text=vigencia,
            general_conditions="\n".join(servicio),
            document_footer=document_footer,
        ),
        bank_accounts=list(bank_accounts),  # type: ignore[arg-type]
        # El precio es del SERVICIO, no de cada pieza.
        show_line_amounts=False,
    )


class V2FiringQuotationPdfService:
    """El documento de Solo Quema. Presenta; no calcula."""

    def __init__(self, session: AsyncSession, base: QuotationPdfService) -> None:
        self._session = session
        self._base = base

    async def render(self, quotation: V2FiringQuotation, *, expired: bool) -> tuple[bytes, str]:
        documento = await self.build(quotation, expired=expired)
        html = self._base.render_html(documento)
        pdf = await asyncio.to_thread(self._base.render_pdf_from_html, html)
        return pdf, sanitize_pdf_filename(quotation.code, quotation.customer_name_snapshot)

    async def build(self, quotation: V2FiringQuotation, *, expired: bool) -> QuotationPdfDocument:
        _exigir_emitida(quotation)
        lineas = list(
            (
                await self._session.scalars(
                    select(V2FiringQuotationLine)
                    .where(V2FiringQuotationLine.v2_firing_quotation_id == quotation.id)
                    .order_by(V2FiringQuotationLine.sort_order, V2FiringQuotationLine.id)
                )
            ).all()
        )
        empresa = await self._base._get_company_settings()
        comercial = await self._base._get_commercial_settings()
        logo = await self._base._resolve_logo_data_uri(empresa)
        return build_firing_quotation_pdf_document(
            quotation,
            lineas,
            company=build_company_doc_info(empresa, logo),
            bank_accounts=_build_bank_accounts_doc(comercial),
            document_footer=comercial.document_footer if comercial else None,
            expired=expired,
        )


__all__ = [
    "V2FiringQuotationPdfDraftBlockedError",
    "V2FiringQuotationPdfNotIssuedError",
    "V2FiringQuotationPdfService",
    "build_firing_quotation_pdf_document",
]
