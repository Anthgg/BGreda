"""Hoja de taller de una orden de produccion.

Es un documento operativo, no comercial. Dice que fabricar, cuanto, de que
medida, con que material preparado y de que almacen sale, y lleva el QR de la
orden para poder abrirla desde el taller sin teclear el codigo.

Lo que NO lleva es igual de deliberado: ni precio de venta, ni margen, ni IGV,
ni total del cliente. Ninguna de esas cifras ayuda a esmaltar una pieza, y
sacarlas impresas al taller solo amplia quien acaba viendo el margen del
cliente. El PDF comercial de la cotizacion sigue siendo otro documento y esta
fase no lo toca.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import segno
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents.quotation import sanitize_pdf_filename
from app.models.inventory import StockLocation
from app.models.masters import Product
from app.models.production import ProductionOrder
from app.models.quotations import Quotation
from app.models.settings import SINGLETON_ID, CompanySettings

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "production"

#: Ruta interna que resuelve el token. Es lo que se codifica en el QR: una ruta
#: de la aplicacion, no un enlace publico ni un volcado de datos.
SCAN_PATH = "/produccion/scan"


def build_qr_data_uri(token: str, *, base_url: str | None = None) -> str:
    """Dibuja el QR del token como SVG embebido.

    Se codifica una RUTA con el token opaco y nada mas. En el QR no van el
    cliente, el precio, el documento de identidad ni el stock: un QR es texto
    legible por cualquiera que apunte una camara, y lo que se imprime en una
    hoja de taller acaba en una mesa, en una foto y en un grupo de mensajeria.

    El SVG se incrusta como `data:` porque WeasyPrint no descarga nada: el
    documento tiene que ser autosuficiente.
    """
    prefix = (base_url or "").rstrip("/")
    payload = f"{prefix}{SCAN_PATH}/{token}"
    qr = segno.make(payload, error="m")
    svg = qr.svg_inline(scale=4, border=2)
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


class ProductionPdfService:
    """Renderiza la hoja de taller de una orden."""

    def __init__(self, session: AsyncSession, *, base_url: str | None = None) -> None:
        self._session = session
        self._base_url = base_url
        self._jinja_env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._template = self._jinja_env.get_template("production_order.html")

    @staticmethod
    def render_pdf_from_html(html_content: str) -> bytes:
        import weasyprint

        return weasyprint.HTML(string=html_content).write_pdf()

    async def build_context(self, order: ProductionOrder) -> dict[str, object]:
        quotation = await self._session.get(Quotation, order.quotation_id)
        location = await self._session.get(StockLocation, order.stock_location_id)
        company = (
            await self._session.execute(
                select(CompanySettings).where(CompanySettings.id == SINGLETON_ID)
            )
        ).scalar_one_or_none()

        prepared_ids = {
            line.prepared_product_id for line in order.lines if line.prepared_product_id is not None
        }
        prepared: dict[int, Product] = {}
        if prepared_ids:
            rows = await self._session.execute(select(Product).where(Product.id.in_(prepared_ids)))
            prepared = {row.id: row for row in rows.scalars().all()}

        return {
            "order": order,
            "quotation": quotation,
            "location": location,
            "company_name": (company.legal_name if company else None) or "Greda",
            "prepared": prepared,
            "qr_data_uri": build_qr_data_uri(order.qr_token, base_url=self._base_url),
        }

    async def render(self, order: ProductionOrder) -> tuple[bytes, str]:
        html_content = self._template.render(**await self.build_context(order))
        content = await asyncio.to_thread(self.render_pdf_from_html, html_content)
        return content, sanitize_pdf_filename(order.code, "orden-de-produccion")
