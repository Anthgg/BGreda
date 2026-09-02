"""Documentos de una orden de produccion.

Este servicio emite DOS documentos distintos de la misma orden:

- :meth:`ProductionPdfService.render`: la hoja de taller. Dice de que almacen
  sale el material, que preparado toca y cuantos gramos. Se obtiene con sesion.

- :meth:`ProductionPdfService.render_public`: la constancia de seguimiento. La
  descarga quien escanea el QR sin tener cuenta.

Los dos comparten la maquetacion —son documentos de la misma casa— y ni un
campo del modelo. Que la constancia publica no lleve el almacen no depende de
que la plantilla se acuerde de omitirlo: depende de que
`PublicTrackingDocument` no tiene donde guardarlo (ver `app/documents/production`).

Lo que ninguno de los dos lleva es igual de deliberado: ni precio de venta, ni
margen, ni IGV, ni total del cliente. Ninguna de esas cifras ayuda a esmaltar
una pieza. El documento comercial de la cotizacion es otro y esta fase no lo
toca.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents.common import build_company_doc_info, sanitize_pdf_filename
from app.documents.production import (
    ProductionOrderDocument,
    PublicTrackingData,
    build_production_order_document,
    build_public_tracking_data,
    build_public_tracking_sheet,
)
from app.models.inventory import StockLocation
from app.models.masters import Product
from app.models.production import ProductionOrder
from app.models.quotations import Quotation
from app.models.settings import SINGLETON_ID, CompanySettings
from app.services.document_assets import resolve_company_logo_data_uri
from app.services.document_qr import build_document_qr_data_uri
from app.services.storage import ObjectStorage

#: Raiz del sistema documental. El cargador apunta aqui y no a `production/`
#: porque las plantillas extienden `base_document.html` y usan los componentes
#: compartidos.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

#: Ruta PUBLICA de seguimiento. Es lo que se codifica en el QR desde 009I.1.
#:
#: Antes apuntaba a una ruta interna y eso dejaba el QR sin utilidad para quien
#: no tiene cuenta: escaneaba y acababa en el login. Ahora resuelve contra la
#: superficie publica de solo lectura, y quien SI tiene sesion sigue pudiendo
#: saltar desde ahi a la vista interna.
SCAN_PATH = "/seguimiento"

#: Pie del QR impreso. Dice para que sirve, porque un cuadrado negro sin
#: explicacion no se escanea.
QR_CAPTION = "Escanea para consultar el estado de producción"


def build_qr_data_uri(
    token: str,
    *,
    base_url: str | None = None,
    logo_data_uri: str | None = None,
) -> str:
    """El QR de una orden: una ruta con el token opaco, y nada mas.

    En el QR no van el cliente, el precio, el documento de identidad ni el
    stock: un QR es texto legible por cualquiera que apunte una camara, y lo
    que se imprime en una hoja de taller acaba en una mesa, en una foto y en un
    grupo de mensajeria.

    El logo del taller —si esta configurado— se dibuja centrado. **No forma
    parte del contenido codificado**: no cambia el token, ni la URL, ni la
    entropia. Solo obliga a subir la correccion de errores, porque tapar el
    centro destruye modulos que hay que poder reconstruir. Como dibujarlo lo
    decide `app/services/document_qr.py`, que es el unico sitio del proyecto
    que genera codigos.
    """
    prefix = (base_url or "").rstrip("/")
    payload = f"{prefix}{SCAN_PATH}/{token}"
    return build_document_qr_data_uri(payload, logo_data_uri=logo_data_uri)


class ProductionPdfService:
    """Renderiza los documentos de una orden."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        base_url: str | None = None,
        storage: ObjectStorage | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url
        self._storage = storage
        self._jinja_env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._template = self._jinja_env.get_template("production/production_order.html")
        self._public_template = self._jinja_env.get_template("production/production_public.html")

    @staticmethod
    def render_pdf_from_html(html_content: str) -> bytes:
        import weasyprint

        return weasyprint.HTML(string=html_content).write_pdf()

    async def _company_settings(self) -> CompanySettings | None:
        return (
            await self._session.execute(
                select(CompanySettings).where(CompanySettings.id == SINGLETON_ID)
            )
        ).scalar_one_or_none()

    # -- Hoja de taller (interna) -------------------------------------------
    async def build_document(self, order: ProductionOrder) -> ProductionOrderDocument:
        """Modelo de la hoja de taller, con todo lo operativo ya resuelto."""
        quotation = await self._session.get(Quotation, order.quotation_id)
        location = await self._session.get(StockLocation, order.stock_location_id)
        company = await self._company_settings()
        logo_data_uri = await resolve_company_logo_data_uri(company, self._storage)

        prepared_ids = {
            line.prepared_product_id for line in order.lines if line.prepared_product_id is not None
        }
        prepared: dict[int, tuple[str, str]] = {}
        if prepared_ids:
            rows = await self._session.execute(select(Product).where(Product.id.in_(prepared_ids)))
            prepared = {row.id: (row.name, row.internal_reference) for row in rows.scalars().all()}

        return build_production_order_document(
            order=order,
            company=build_company_doc_info(company, logo_data_uri),
            quotation_code=quotation.code if quotation else None,
            stock_location_name=location.name if location else None,
            prepared_names=prepared,
            qr_data_uri=build_qr_data_uri(
                order.qr_token, base_url=self._base_url, logo_data_uri=logo_data_uri
            ),
            qr_caption=QR_CAPTION,
        )

    async def render(self, order: ProductionOrder) -> tuple[bytes, str]:
        html_content = self._template.render(doc=await self.build_document(order))
        content = await asyncio.to_thread(self.render_pdf_from_html, html_content)
        return content, sanitize_pdf_filename(order.code, "orden-de-produccion")

    # -- Constancia de seguimiento (publica) ---------------------------------
    async def build_public_data(self, order: ProductionOrder) -> PublicTrackingData:
        """Lo unico que sale de aqui sin sesion. Lo consumen la API y el PDF."""
        company = await self._company_settings()
        return self._build_public_data(order, company)

    @staticmethod
    def _build_public_data(
        order: ProductionOrder,
        company: CompanySettings | None,
    ) -> PublicTrackingData:
        nombre = None
        if company is not None:
            nombre = company.trade_name or company.legal_name
        return build_public_tracking_data(order=order, company_name=nombre or "Greda")

    async def render_public(self, order: ProductionOrder) -> tuple[bytes, str]:
        """La constancia publica, compuesta desde el MISMO dato publico.

        Pasa por `build_public_data` a proposito, en vez de leer la orden otra
        vez: si el PDF pudiera mirar la orden entera, la frontera dejaria de
        serlo y bastaria una fila nueva en la plantilla para publicar el
        almacen.
        """
        company = await self._company_settings()
        data = self._build_public_data(order, company)
        logo_data_uri = await resolve_company_logo_data_uri(company, self._storage)
        hoja = build_public_tracking_sheet(data, logo_data_uri=logo_data_uri)
        html_content = self._public_template.render(doc=hoja)
        content = await asyncio.to_thread(self.render_pdf_from_html, html_content)
        return content, sanitize_pdf_filename(order.code, "seguimiento")
