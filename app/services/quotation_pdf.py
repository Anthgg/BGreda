"""Servicio de generacion de documentos PDF comerciales de cotizaciones."""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import APIError
from app.documents.quotation import (
    QuotationPdfDocument,
    build_draft_quotation_pdf_document,
    build_quotation_pdf_document,
    sanitize_pdf_filename,
)
from app.models.quotations import Quotation, QuotationStatus
from app.models.settings import SINGLETON_ID, CommercialSettings, CompanySettings
from app.services.quotations import QuotationNotFoundError
from app.services.storage import ObjectStorage, sniff_content_type

if TYPE_CHECKING:
    from app.models.masters import Partner
    from app.schemas.quotation_builder import QuotationBuilderOut

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "quotations"


class QuotationPdfDraftBlockedError(APIError):
    status_code = 409
    code = "QUOTATION_DRAFT_PDF_BLOCKED"
    message = "No se puede emitir el documento comercial de una cotización en borrador"


class QuotationPdfService:
    """Autoridad responsable de renderizar documentos PDF comerciales."""

    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectStorage | None = None,
    ) -> None:
        self._session = session
        self._storage = storage
        self._jinja_env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._template = self._jinja_env.get_template("quotation.html")

    async def _get_company_settings(self) -> CompanySettings | None:
        stmt = select(CompanySettings).where(CompanySettings.id == SINGLETON_ID)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _get_commercial_settings(self) -> CommercialSettings | None:
        stmt = (
            select(CommercialSettings)
            .where(CommercialSettings.id == SINGLETON_ID)
            .options(selectinload(CommercialSettings.bank_accounts))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _resolve_logo_data_uri(self, company_settings: CompanySettings | None) -> str | None:
        """Descarga de forma segura el logo y lo convierte en Data URI en memoria."""
        if not company_settings or not company_settings.logo_object_path or not self._storage:
            return None

        try:
            logo_bytes = await self._storage.download(company_settings.logo_object_path)
            if not logo_bytes:
                return None
            content_type = (
                sniff_content_type(logo_bytes) or company_settings.logo_content_type or "image/png"
            )
            b64_data = base64.b64encode(logo_bytes).decode("ascii")
            return f"data:{content_type};base64,{b64_data}"
        except Exception as exc:
            # La falla en Storage/Logo no debe impedir la generacion del PDF comercial
            logger.warning("No se pudo cargar el logo para el PDF comercial: %s", exc)
            return None

    def build_document_model(
        self,
        quotation: Quotation,
        company_settings: CompanySettings | None,
        commercial_settings: CommercialSettings | None,
        logo_data_uri: str | None = None,
    ) -> QuotationPdfDocument:
        """Construye el ViewModel segregado del documento."""
        return build_quotation_pdf_document(
            quotation=quotation,
            company_settings=company_settings,
            commercial_settings=commercial_settings,
            logo_data_uri=logo_data_uri,
        )

    def build_draft_document_model(
        self,
        quotation_out: QuotationBuilderOut,
        customer: Partner | None = None,
        company_settings: CompanySettings | None = None,
        commercial_settings: CommercialSettings | None = None,
        logo_data_uri: str | None = None,
    ) -> QuotationPdfDocument:
        """Construye el ViewModel segregado del documento de previsualizacion DRAFT."""
        return build_draft_quotation_pdf_document(
            quotation_out=quotation_out,
            customer=customer,
            company_settings=company_settings,
            commercial_settings=commercial_settings,
            logo_data_uri=logo_data_uri,
        )

    def render_html(self, document_model: QuotationPdfDocument) -> str:
        """Renderiza la plantilla HTML a partir del ViewModel."""
        return self._template.render(doc=document_model)

    def render_pdf_from_html(self, html_content: str) -> bytes:
        """Genera los bytes del binario PDF a partir de HTML/CSS con WeasyPrint."""
        import weasyprint

        return weasyprint.HTML(string=html_content).write_pdf()

    async def render_draft_pdf(
        self,
        quotation_out: QuotationBuilderOut,
        customer: Partner | None = None,
    ) -> tuple[bytes, str]:
        """Renderiza la previsualizacion efimera del PDF comercial para borradores DRAFT."""
        company_settings = await self._get_company_settings()
        commercial_settings = await self._get_commercial_settings()
        logo_data_uri = await self._resolve_logo_data_uri(company_settings)

        doc_model = self.build_draft_document_model(
            quotation_out=quotation_out,
            customer=customer,
            company_settings=company_settings,
            commercial_settings=commercial_settings,
            logo_data_uri=logo_data_uri,
        )

        html_content = self.render_html(doc_model)
        pdf_bytes = await asyncio.to_thread(self.render_pdf_from_html, html_content)

        customer_name = customer.name if customer else quotation_out.customer_name_snapshot
        code_label = quotation_out.code or "BORRADOR"
        filename = sanitize_pdf_filename(code_label, customer_name)

        return pdf_bytes, filename

    async def get_quotation_pdf(self, quotation_id: int) -> tuple[bytes, str]:
        """Obtiene y renderiza el PDF comercial oficial de la cotizacion."""
        stmt = (
            select(Quotation)
            .where(Quotation.id == quotation_id)
            .options(
                selectinload(Quotation.product),
                selectinload(Quotation.items),
            )
        )
        quotation = (await self._session.execute(stmt)).scalar_one_or_none()
        if quotation is None:
            raise QuotationNotFoundError()

        # REGLA: Solo CONFIRMED y CANCELLED tienen documento comercial
        if quotation.status is QuotationStatus.DRAFT:
            raise QuotationPdfDraftBlockedError()

        company_settings = await self._get_company_settings()
        commercial_settings = await self._get_commercial_settings()
        logo_data_uri = await self._resolve_logo_data_uri(company_settings)

        doc_model = self.build_document_model(
            quotation=quotation,
            company_settings=company_settings,
            commercial_settings=commercial_settings,
            logo_data_uri=logo_data_uri,
        )

        html_content = self.render_html(doc_model)
        pdf_bytes = await asyncio.to_thread(self.render_pdf_from_html, html_content)

        customer_name = quotation.customer_name_snapshot or quotation.customer_trade_name_snapshot
        filename = sanitize_pdf_filename(quotation.code, customer_name)

        return pdf_bytes, filename
