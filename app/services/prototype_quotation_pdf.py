"""Render del PDF de una cotizacion de prototipo.

Se apoya en `QuotationPdfService` en vez de montar un segundo motor: de ahi
salen el logo, los datos de empresa, las condiciones, las cuentas bancarias y
el paso de HTML a PDF. Lo unico propio es armar el ViewModel y elegir otra
plantilla.

Un documento CONFIRMADO se dibuja con lo que congelo. Regenerar el PDF de una
cotizacion firmada leyendo la configuracion de hoy daria un papel distinto del
que se entrego, y el cliente conserva el suyo.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.documents.common import format_date_display, sanitize_pdf_filename
from app.documents.prototype_quotation import (
    PrototypeDocDeadline,
    PrototypeDocSpecs,
    PrototypeQuotationPdfDocument,
    build_prototype_dimensions,
    build_prototype_line,
    build_prototype_totals,
)
from app.documents.quotation import CustomerDocInfo, DocumentHeaderInfo
from app.models.masters import Partner
from app.models.prototype_quotations import PrototypeQuotation, PrototypeQuotationStatus
from app.services.quotation_pdf import TEMPLATES_DIR, QuotationPdfService

ZERO = Decimal(0)
TITULO = "COTIZACIÓN DE PROTOTIPO"
CONCEPTO = "Desarrollo de prototipo"


class PrototypeQuotationPdfDraftBlockedError(APIError):
    """Un borrador no es un documento: todavia no tiene numero ni precio firme.

    Dejar descargarlo invitaria a enviarle al cliente un papel que puede cambiar
    al dia siguiente y que no se puede referenciar por codigo.
    """

    status_code = 409
    code = "PROTOTYPE_QUOTATION_PDF_DRAFT_BLOCKED"
    message = "Emita la cotizacion de prototipo antes de generar su PDF"


class PrototypeQuotationPdfService:
    """El documento del cliente. Presenta; no calcula."""

    def __init__(self, session: AsyncSession, base: QuotationPdfService) -> None:
        self._session = session
        self._base = base
        self._jinja = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._template = self._jinja.get_template("prototype_quotations/prototype_quotation.html")

    async def render(self, fila: PrototypeQuotation) -> tuple[bytes, str]:
        if fila.status is PrototypeQuotationStatus.DRAFT:
            raise PrototypeQuotationPdfDraftBlockedError()

        cliente = await self._session.get(Partner, fila.customer_id) if fila.customer_id else None

        empresa = await self._base._get_company_settings()
        comercial = await self._base._get_commercial_settings()
        logo = await self._base._resolve_logo_data_uri(empresa)

        documento = self._build_document(fila, cliente, empresa, comercial, logo)
        html = self._template.render(doc=documento)
        pdf = await asyncio.to_thread(self._base.render_pdf_from_html, html)
        return pdf, sanitize_pdf_filename(fila.code or "PROTOTIPO", fila.customer_name_snapshot)

    def _build_document(
        self,
        fila: PrototypeQuotation,
        cliente: Partner | None,
        empresa: object,
        comercial: object,
        logo: str | None,
    ) -> PrototypeQuotationPdfDocument:
        from app.documents.common import build_company_doc_info
        from app.documents.quotation import _build_bank_accounts_doc, _build_conditions_doc

        simbolo = fila.currency_symbol_snapshot or "S/"
        # Los tres numeros salen CONGELADOS de la fila. No se recalculan aqui:
        # el escalon comercial se aplico una sola vez al emitir.
        neto = fila.commercial_net_total or ZERO
        impuesto = fila.commercial_tax_total or ZERO
        total = fila.commercial_gross_total or ZERO

        return PrototypeQuotationPdfDocument(
            company=build_company_doc_info(empresa, logo),  # type: ignore[arg-type]
            customer=_cliente_doc(cliente, fila.customer_name_snapshot),
            document=DocumentHeaderInfo(
                title=TITULO,
                code=fila.code or "",
                name=fila.description,
                status=fila.status.value,
                is_cancelled=fila.status is PrototypeQuotationStatus.CANCELLED,
                emission_date=format_date_display(fila.confirmed_at or fila.created_at),
                currency_symbol=simbolo,
                currency_code=fila.currency_code_snapshot or "PEN",
            ),
            lines=[build_prototype_line(CONCEPTO, fila.quantity, neto, simbolo)],
            specs=PrototypeDocSpecs(
                dimensions=build_prototype_dimensions(
                    fila.width_cm, fila.length_cm, fila.height_cm, fila.depth_cm
                ),
                **_specs_tecnicas(fila.technical_specifications),
            ),
            deadline=(
                PrototypeDocDeadline(
                    estimated_days=format(fila.estimated_days.normalize(), "f"),
                    target_date=(
                        format_date_display(fila.target_date) if fila.target_date else None
                    ),
                )
                if fila.estimated_days is not None
                else None
            ),
            totals=build_prototype_totals(
                neto, impuesto, total, fila.tax_percent_snapshot or ZERO, simbolo
            ),
            # Sin vigencia congelada: una cotizacion de prototipo no la
            # declara todavia. Pasar la de configuracion aqui pondria en el
            # papel una fecha que nadie acordo.
            conditions=_build_conditions_doc(comercial, None),  # type: ignore[arg-type]
            bank_accounts=_build_bank_accounts_doc(comercial),  # type: ignore[arg-type]
        )


def _cliente_doc(cliente: Partner | None, nombre: str | None) -> CustomerDocInfo:
    if cliente is None:
        return CustomerDocInfo(name=nombre or "Cliente")
    return CustomerDocInfo(
        name=cliente.name,
        document_type=cliente.document_type.value if cliente.document_type else None,
        document_number=cliente.document_number,
        address=cliente.address,
        email=cliente.email,
        phone=cliente.phone or cliente.mobile,
    )


def _specs_tecnicas(ficha: dict[str, object] | None) -> dict[str, str | None]:
    """Lo comercial de la ficha, y solo eso.

    Del cuaderno del taller sale mucho mas —responsable, prioridad, peso de
    pasta, notas internas—, pero al cliente le importa el acabado y el color
    porque forman parte de lo acordado. El resto se queda dentro.
    """
    if not ficha:
        return {"finish": None, "color": None, "technique": None}
    return {
        "finish": _texto(ficha.get("finish")),
        "color": _texto(ficha.get("color")),
        "technique": _texto(ficha.get("technique")),
    }


def _texto(valor: object) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None
