"""Fase 010H — el PDF que recibe el cliente de una cotizacion V2.

**Es el MISMO documento que un CTZ y un CPR.** Se arma un `QuotationPdfDocument`
y lo dibuja `QuotationPdfService`: logo, datos de empresa, cabecera, cliente,
tabla, caja de totales, condiciones, cuentas bancarias, pie y paginacion. Lo
unico propio de V2 es de donde salen los datos.

## Solo columnas congeladas

Todo lo economico y todo lo del cliente sale de la fila emitida: unitarios
redondeados, subtotales, IGV, total, moneda, tipo de cambio, vigencia, nombre y
documento del cliente, nombres y medidas de las piezas, condiciones y notas de
pago. No se lee `partners`, ni `products`, ni la configuracion comercial de hoy
para nada de eso. Si manana sube el IGV, cambia un precio, se corrige la
direccion del cliente o se renombra un producto, el PDF de una cotizacion ya
emitida no cambia. Regenerarlo un año despues da la misma informacion.

Lo que SI se lee vivo es la identidad de la casa —logo, razon social, cuentas
bancarias, pie—, exactamente como en los CTZ y CPR: es el membrete de quien
emite, no un termino de la oferta.

## Lista blanca, no lista negra

El ViewModel no tiene un solo campo de costo. No se «ocultan» el costo real, el
gas, las tarifas internas, los factores x2/x3, la ganancia ni el margen: nunca
llegan a la plantilla, porque este constructor no los lee. Una prueba extrae el
texto del PDF y busca esos terminos igualmente.

Aqui NO se calcula dinero. Se formatea lo que el motor de 010F dejo escrito.
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
    format_exchange_rate,
    format_quantity,
)
from app.models.quoter_v2 import V2Quotation, V2QuotationProduct, V2QuotationStatus
from app.services.quotation_pdf import QuotationPdfService

ZERO = Decimal(0)
TITULO = "COTIZACIÓN"
#: Las piezas V2 se cotizan por unidad.
UNIDAD = "und"


class V2QuotationPdfDraftBlockedError(APIError):
    """Un borrador no es un documento: su precio todavia puede cambiar."""

    status_code = 409
    code = "V2_QUOTATION_PDF_DRAFT_BLOCKED"
    message = "Confirme la cotizacion antes de descargar su PDF"


class V2QuotationPdfNotIssuedError(APIError):
    """Una cancelada que nunca llego a emitirse no tiene documento.

    No es un borrador —ya no se puede confirmar—, asi que decirle «confirme
    antes» seria invitar a algo imposible.
    """

    status_code = 409
    code = "V2_QUOTATION_PDF_NOT_ISSUED"
    message = "Esta cotizacion se cancelo sin llegar a emitirse y no tiene documento"


class V2QuotationPdfService:
    """El documento del cliente de una cotizacion V2. Presenta; no calcula."""

    def __init__(self, session: AsyncSession, base: QuotationPdfService) -> None:
        self._session = session
        self._base = base

    async def render(self, quotation: V2Quotation, *, expired: bool) -> tuple[bytes, str]:
        documento = await self.build(quotation, expired=expired)
        html = self._base.render_html(documento)
        pdf = await asyncio.to_thread(self._base.render_pdf_from_html, html)
        return pdf, sanitize_pdf_filename(quotation.code, quotation.customer_name_snapshot)

    async def build(self, quotation: V2Quotation, *, expired: bool) -> QuotationPdfDocument:
        _exigir_emitida(quotation)
        lineas = list(
            (
                await self._session.scalars(
                    select(V2QuotationProduct)
                    .where(V2QuotationProduct.v2_quotation_id == quotation.id)
                    .order_by(V2QuotationProduct.sort_order, V2QuotationProduct.id)
                )
            ).all()
        )
        empresa = await self._base._get_company_settings()
        comercial = await self._base._get_commercial_settings()
        logo = await self._base._resolve_logo_data_uri(empresa)
        return build_v2_pdf_document(
            quotation,
            lineas,
            company=build_company_doc_info(empresa, logo),
            bank_accounts=_build_bank_accounts_doc(comercial),
            document_footer=comercial.document_footer if comercial else None,
            expired=expired,
        )


def _exigir_emitida(quotation: V2Quotation) -> None:
    if quotation.status is V2QuotationStatus.DRAFT:
        raise V2QuotationPdfDraftBlockedError()
    if quotation.issued_at is None:
        raise V2QuotationPdfNotIssuedError()


def build_v2_pdf_document(
    quotation: V2Quotation,
    lines: list[V2QuotationProduct],
    *,
    company: object,
    bank_accounts: Sequence[object],
    document_footer: str | None,
    expired: bool,
) -> QuotationPdfDocument:
    """El ViewModel del PDF, solo desde lo congelado. Funcion pura: se prueba sin base."""
    simbolo = quotation.currency_symbol_snapshot or "S/"
    moneda = quotation.currency_code_snapshot or "PEN"
    porcentaje = quotation.tax_percent_snapshot or ZERO
    cancelada = quotation.status is V2QuotationStatus.CANCELLED
    _exigir_emitida(quotation)
    assert quotation.issued_at is not None

    items = [
        QuotationDocItem(
            item_number=numero,
            product_name=linea.product_name_snapshot or "Producto",
            dimensions_formatted=format_dimensions(
                length=linea.length_cm, width=linea.width_cm, height=linea.height_cm
            ),
            quantity=linea.quantity,
            quantity_formatted=format_quantity(linea.quantity),
            unit_of_measure=UNIDAD,
            # El unitario REDONDEADO: el que el cliente lee y con el que se
            # reconstruyeron subtotal, IGV y total en 010F.
            unit_price_formatted=format_currency(linea.unit_price, simbolo),
            subtotal_formatted=format_currency(linea.line_subtotal, simbolo),
            line_tax_formatted=format_currency(linea.line_tax, simbolo),
            line_total_formatted=format_currency(linea.line_total, simbolo),
            observation=linea.client_observation,
        )
        for numero, linea in enumerate(lines, start=1)
    ]

    vigencia = None
    if quotation.valid_until is not None:
        vigencia = f"Válida hasta el {format_date_display(quotation.valid_until)}"
        if quotation.validity_days_snapshot:
            vigencia += f" ({quotation.validity_days_snapshot} días desde su emisión)"
        vigencia += "."

    generales = "\n".join(
        parte for parte in (quotation.client_notes, quotation.conditions_snapshot) if parte
    )

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
            name=quotation.name,
            status=quotation.status.value,
            is_cancelled=cancelada,
            # Lo cancelado ya dice ANULADA; no se superponen dos distintivos.
            is_expired=expired and not cancelada,
            # En el calendario de Lima: la misma fecha con la que se conto la vigencia.
            emission_date=format_date_display(business_date(quotation.issued_at)),
            validity_date=format_date_display(quotation.valid_until)
            if quotation.valid_until
            else None,
            currency_symbol=simbolo,
            currency_code=moneda,
            prepared_by=nombre_de_actor(quotation.issued_by_name or quotation.created_by_name),
            exchange_rate_text=format_exchange_rate(quotation.exchange_rate_snapshot, moneda),
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
            general_conditions=generales or None,
            payment_notes=quotation.payment_notes_snapshot,
            document_footer=document_footer,
        ),
        bank_accounts=list(bank_accounts),  # type: ignore[arg-type]
        show_line_tax=True,
    )


__all__ = [
    "V2QuotationPdfDraftBlockedError",
    "V2QuotationPdfNotIssuedError",
    "V2QuotationPdfService",
    "build_v2_pdf_document",
]
