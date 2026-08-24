"""Contratos de la configuracion de empresa y comercial.

Todos los modelos de entrada declaran ``extra="forbid"``: un campo desconocido
se rechaza en vez de ignorarse en silencio. Sin eso, una peticion podria
intentar escribir columnas que el endpoint no expone (mass assignment).
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.settings import MAX_TAX_PERCENT, MAX_VALIDITY_DAYS

# ---------------------------------------------------------------------------
# Texto seguro
# ---------------------------------------------------------------------------
#: Detecta una apertura de etiqueta ("<p", "</div"). No pretende ser un parser
#: de HTML: la defensa real es el escapado en la salida. Es una barrera
#: adicional para que un script jamas llegue siquiera a almacenarse.
_TAG_PATTERN = re.compile(r"<\s*/?\s*[a-zA-Z]")
#: Caracteres de control salvo tabulador, salto de linea y retorno de carro.
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_text(value: str | None) -> str | None:
    """Normaliza un texto libre y rechaza marcado HTML."""
    if value is None:
        return None
    if _CONTROL_PATTERN.search(value):
        raise ValueError("El texto contiene caracteres de control no permitidos")
    if _TAG_PATTERN.search(value):
        raise ValueError(
            "El texto no admite etiquetas HTML. Estos campos se almacenan y "
            "se muestran como texto plano."
        )
    cleaned = value.strip()
    return cleaned or None


PlainText = Annotated[str | None, Field(max_length=4000)]
ShortText = Annotated[str | None, Field(max_length=200)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Empresa
# ---------------------------------------------------------------------------
class CompanySettingsBase(_StrictModel):
    legal_name: ShortText = None
    trade_name: ShortText = None
    #: RUC peruano: 11 digitos. Se admite vacio mientras no se configure.
    tax_id: Annotated[str | None, Field(max_length=20)] = None

    address_line1: ShortText = None
    address_line2: ShortText = None
    #: Codigo oficial INEI. Los nombres se resuelven y canonizan en el servidor.
    ubigeo_code: Annotated[str | None, Field(pattern=r"^\d{6}$")] = None
    district: Annotated[str | None, Field(max_length=120)] = None
    province: Annotated[str | None, Field(max_length=120)] = None
    department: Annotated[str | None, Field(max_length=120)] = None
    country: Annotated[str | None, Field(max_length=120)] = None
    postal_code: Annotated[str | None, Field(max_length=20)] = None

    phone: Annotated[str | None, Field(max_length=40)] = None
    mobile: Annotated[str | None, Field(max_length=40)] = None
    email: EmailStr | None = None
    website: ShortText = None
    contact_name: Annotated[str | None, Field(max_length=160)] = None
    contact_role: Annotated[str | None, Field(max_length=120)] = None

    @field_validator(
        "legal_name",
        "trade_name",
        "address_line1",
        "address_line2",
        "district",
        "province",
        "department",
        "country",
        "postal_code",
        "phone",
        "mobile",
        "website",
        "contact_name",
        "contact_role",
        mode="after",
    )
    @classmethod
    def _plain_text(cls, value: str | None) -> str | None:
        return _clean_text(value)

    @field_validator("tax_id", mode="after")
    @classmethod
    def _validate_tax_id(cls, value: str | None) -> str | None:
        cleaned = _clean_text(value)
        if cleaned is None:
            return None
        if not cleaned.isdigit():
            raise ValueError("El RUC solo admite digitos")
        if len(cleaned) != 11:
            raise ValueError("El RUC peruano tiene 11 digitos")
        return cleaned

    @field_validator("website", mode="after")
    @classmethod
    def _validate_website(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("El sitio web debe empezar por http:// o https://")
        return value


class CompanySettingsUpdate(CompanySettingsBase):
    """Cuerpo de ``PUT /settings/company``.

    ``version`` implementa el bloqueo optimista: es la version que el cliente
    leyo. Si entre tanto otro administrador guardo, la escritura se rechaza.
    """

    version: Annotated[int, Field(ge=1)]


class LogoInfo(BaseModel):
    """Metadatos del logo. El binario se sirve por un endpoint aparte."""

    content_type: str
    size_bytes: int
    #: Ruta relativa que el frontend debe pedir al backend, nunca a Storage.
    url: str


class CompanySettingsOut(CompanySettingsBase):
    version: int
    updated_at: datetime
    logo: LogoInfo | None = None


# ---------------------------------------------------------------------------
# Cuentas bancarias
# ---------------------------------------------------------------------------
class BankAccountBase(_StrictModel):
    bank_name: Annotated[str | None, Field(max_length=160)] = None
    account_holder: ShortText = None
    account_number: Annotated[str | None, Field(max_length=64)] = None
    cci: Annotated[str | None, Field(max_length=32)] = None
    notes: PlainText = None

    @field_validator("bank_name", "account_holder", "account_number", "notes", mode="after")
    @classmethod
    def _plain_text(cls, value: str | None) -> str | None:
        return _clean_text(value)

    @field_validator("cci", mode="after")
    @classmethod
    def _validate_cci(cls, value: str | None) -> str | None:
        cleaned = _clean_text(value)
        if cleaned is None:
            return None
        compact = cleaned.replace(" ", "").replace("-", "")
        if not compact.isdigit():
            raise ValueError("El CCI solo admite digitos")
        if len(compact) != 20:
            raise ValueError("El CCI peruano tiene 20 digitos")
        return compact


class BankAccountOut(BankAccountBase):
    id: int
    is_primary: bool


# ---------------------------------------------------------------------------
# Comercial
# ---------------------------------------------------------------------------
class CommercialSettingsBase(_StrictModel):
    #: Codigo ISO 4217 en mayusculas. No se precarga ningun valor.
    currency_code: Annotated[str | None, Field(min_length=3, max_length=3)] = None
    currency_symbol: Annotated[str | None, Field(max_length=8)] = None

    #: Porcentaje, no fraccion: 18 significa 18 %. Se persiste como NUMERIC.
    tax_percent: Annotated[
        Decimal | None,
        Field(ge=0, le=MAX_TAX_PERCENT, max_digits=9, decimal_places=6),
    ] = None

    #: Fuente configurable del factor 2 reconstruido del Excel de Fase 005.
    default_quotation_factor: Annotated[
        Decimal,
        Field(gt=0, max_digits=9, decimal_places=6),
    ] = Decimal(2)

    quote_validity_days: Annotated[int | None, Field(ge=1, le=MAX_VALIDITY_DAYS)] = None

    general_conditions: PlainText = None
    payment_notes: PlainText = None
    document_footer: PlainText = None

    @field_validator(
        "currency_symbol",
        "general_conditions",
        "payment_notes",
        "document_footer",
        mode="after",
    )
    @classmethod
    def _plain_text(cls, value: str | None) -> str | None:
        return _clean_text(value)

    @field_validator("currency_code", mode="after")
    @classmethod
    def _validate_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = value.strip().upper()
        if not code.isalpha() or len(code) != 3:
            raise ValueError(
                "La moneda debe ser un codigo ISO 4217 de tres letras, por ejemplo PEN"
            )
        return code


class CommercialSettingsUpdate(CommercialSettingsBase):
    version: Annotated[int, Field(ge=1)]
    #: Cuenta bancaria principal. La Fase 2 gestiona una sola; el modelo de
    #: datos ya admite varias.
    bank_account: BankAccountBase | None = None


class CommercialSettingsOut(CommercialSettingsBase):
    version: int
    updated_at: datetime
    bank_accounts: list[BankAccountOut] = Field(default_factory=list)
