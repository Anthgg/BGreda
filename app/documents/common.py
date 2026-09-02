"""Piezas comunes a TODOS los documentos oficiales.

Aqui solo entra lo que es de la empresa, no de un documento: quien emite, como
se escribe una fecha, como se llama el fichero que se descarga. La regla para
anadir algo a este modulo es la misma que para el CSS compartido: si dejara de
tener sentido cuando desaparezca un tipo de documento, no es comun.

Lo que NO va aqui, y no debe acabar aqui: importes, IGV, almacenes, recetas,
estados de fabricacion. Un modelo generico con ochenta campos opcionales
volveria a mezclar lo que 009I.1 separa; cada documento conserva su propio
modelo tipado.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from app.models.settings import CompanySettings


@dataclass(slots=True)
class CompanyDocInfo:
    """Quien emite el documento. Identica en la cotizacion y en el taller."""

    legal_name: str | None = None
    trade_name: str | None = None
    tax_id: str | None = None  # RUC
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    logo_data_uri: str | None = None

    @property
    def display_name(self) -> str:
        return self.trade_name or self.legal_name or "EMPRESA"


@dataclass(slots=True)
class DocFact:
    """Un par etiqueta/valor ya formateado, para la rejilla de informacion."""

    label: str
    value: str


def build_company_doc_info(
    company_settings: CompanySettings | None,
    logo_data_uri: str | None = None,
) -> CompanyDocInfo:
    """Arma la identidad de la empresa desde la configuracion."""
    address_parts = []
    if company_settings:
        if company_settings.address_line1:
            address_parts.append(company_settings.address_line1)
        if company_settings.address_line2:
            address_parts.append(company_settings.address_line2)
        loc = [
            p
            for p in (
                company_settings.district,
                company_settings.province,
                company_settings.department,
            )
            if p
        ]
        if loc:
            address_parts.append(", ".join(loc))

    return CompanyDocInfo(
        legal_name=company_settings.legal_name if company_settings else None,
        trade_name=company_settings.trade_name if company_settings else None,
        tax_id=company_settings.tax_id if company_settings else None,
        address=" - ".join(address_parts) if address_parts else None,
        phone=(company_settings.phone or company_settings.mobile) if company_settings else None,
        email=company_settings.email if company_settings else None,
        website=company_settings.website if company_settings else None,
        logo_data_uri=logo_data_uri,
    )


def format_date_display(dt: date | datetime | None) -> str:
    """Fecha en DD/MM/AAAA."""
    if dt is None:
        return "-"
    return dt.strftime("%d/%m/%Y")


def format_datetime_display(dt: datetime | None) -> str | None:
    """Fecha y hora en DD/MM/AAAA HH:MM. Nulo cuando el hecho no ha ocurrido.

    Devuelve None y no una raya: quien lo pinta decide como se ve un hecho que
    todavia no paso, y en una linea de tiempo no se ve igual que en una tabla.
    """
    if dt is None:
        return None
    return dt.strftime("%d/%m/%Y %H:%M")


def sanitize_pdf_filename(code: str, subject: str | None = None) -> str:
    """Nombre de fichero seguro para una descarga.

    El codigo y el sujeto vienen de la base y acaban en una cabecera
    `Content-Disposition`: sin limpiarlos, un nombre con comillas o saltos de
    linea permitiria inyectar cabeceras.
    """
    clean_code = re.sub(r"[^A-Za-z0-9_-]", "", code.strip()) or "DOCUMENTO"
    if subject and subject.strip():
        clean_name = re.sub(r"[^A-Za-z0-9_-]", "_", subject.strip())
        clean_name = re.sub(r"_+", "_", clean_name).strip("_")
        if clean_name:
            return f"{clean_code}_{clean_name[:40]}.pdf"
    return f"{clean_code}.pdf"
