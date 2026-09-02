"""Activos configurados que comparten los documentos oficiales."""

from __future__ import annotations

import base64
import logging

from app.models.settings import CompanySettings
from app.services.storage import ObjectStorage, sniff_content_type

logger = logging.getLogger(__name__)


async def resolve_company_logo_data_uri(
    company_settings: CompanySettings | None,
    storage: ObjectStorage | None,
) -> str | None:
    """Descarga el logo configurado y lo embebe sin volver frágil el PDF.

    Cotización, orden de producción y constancia pública usan esta misma
    resolución. Un fallo del almacenamiento omite el logo, pero nunca impide
    emitir un documento.
    """
    if not company_settings or not company_settings.logo_object_path or not storage:
        return None

    try:
        logo_bytes = await storage.download(company_settings.logo_object_path)
        if not logo_bytes:
            return None
        content_type = (
            sniff_content_type(logo_bytes) or company_settings.logo_content_type or "image/png"
        )
        encoded = base64.b64encode(logo_bytes).decode("ascii")
        return f"data:{content_type};base64,{encoded}"
    except Exception as exc:
        logger.warning("No se pudo cargar el logo configurado para el PDF: %s", exc)
        return None
