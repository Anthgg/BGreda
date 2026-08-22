"""Contratos de los maestros: categorias, unidades, productos y terceros."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.masters import parse_decimal, quantize_cost
from app.models.masters import DocumentType, PartnerRole, ProductType, UomDimension

_TAG_PATTERN = re.compile(r"<\s*/?\s*[a-zA-Z]")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _plain_text(value: str) -> str:
    """Rechaza HTML y caracteres de control, igual que la Fase 2."""
    if _TAG_PATTERN.search(value):
        raise ValueError("El texto no admite etiquetas HTML")
    if _CONTROL_CHARS.search(value):
        raise ValueError("El texto no admite caracteres de control")
    return value


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Categorias
# ---------------------------------------------------------------------------
class ProductCategoryOut(_Out):
    id: int
    name: str
    parent_id: int | None
    display_path: str
    active: bool


class ProductCategoryCreate(_In):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    parent_id: int | None = None
    active: bool = True

    @field_validator("name", mode="after")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _plain_text(value)


class ProductCategoryUpdate(_In):
    name: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    parent_id: int | None = None
    active: bool | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        return None if value is None else _plain_text(value)


class PosCategoryOut(_Out):
    id: int
    name: str
    parent_id: int | None
    active: bool


class PosCategoryCreate(_In):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    parent_id: int | None = None
    active: bool = True

    @field_validator("name", mode="after")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _plain_text(value)


# ---------------------------------------------------------------------------
# Unidades
# ---------------------------------------------------------------------------
class UnitOfMeasureOut(_Out):
    code: str
    name: str
    symbol: str
    dimension: UomDimension
    factor_to_base: Decimal
    is_base: bool
    active: bool


class UnitOfMeasureCreate(_In):
    code: Annotated[str, Field(min_length=1, max_length=16)]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    symbol: Annotated[str, Field(min_length=1, max_length=16)]
    dimension: UomDimension
    factor_to_base: Decimal = Decimal(1)
    active: bool = True

    @field_validator("factor_to_base", mode="after")
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("El factor debe ser mayor que cero")
        return value


# ---------------------------------------------------------------------------
# Productos
# ---------------------------------------------------------------------------
class ProductOut(_Out):
    id: int
    internal_reference: str
    name: str
    product_type: ProductType
    product_category_id: int
    product_category_path: str | None = None
    pos_category_id: int | None
    pos_category_name: str | None = None
    base_uom_code: str | None
    purchase_uom_code: str | None
    cost: Decimal | None
    sale_price: Decimal | None
    sale_tax_rate: Decimal | None
    purchase_tax_rate: Decimal | None
    sellable: bool
    purchasable: bool
    available_in_pos: bool
    active: bool
    notes: str | None


class ProductPage(BaseModel):
    items: list[ProductOut]
    total: int
    limit: int
    offset: int


class _ProductFields(_In):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    product_type: ProductType
    product_category_id: int
    pos_category_id: int | None = None
    base_uom_code: Annotated[str | None, Field(max_length=16)] = None
    purchase_uom_code: Annotated[str | None, Field(max_length=16)] = None
    cost: Decimal | None = None
    sale_price: Decimal | None = None
    sale_tax_rate: Decimal | None = None
    purchase_tax_rate: Decimal | None = None
    sellable: bool = False
    purchasable: bool = False
    available_in_pos: bool = False
    active: bool = True
    notes: Annotated[str | None, Field(max_length=2000)] = None

    @field_validator("name", "notes", mode="after")
    @classmethod
    def _check_text(cls, value: str | None) -> str | None:
        return None if value is None else _plain_text(value)

    @field_validator("cost", mode="after")
    @classmethod
    def _quantize_cost(cls, value: Decimal | None) -> Decimal | None:
        """Aplica la escala aprobada del proyecto tambien en el alta manual."""
        if value is None:
            return None
        if value < 0:
            raise ValueError("El costo no puede ser negativo")
        quantized, _ = quantize_cost(value)
        return quantized

    @field_validator("sale_price", mode="after")
    @classmethod
    def _check_price(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("El precio no puede ser negativo")
        return value

    @model_validator(mode="after")
    def _service_may_lack_uom(self) -> _ProductFields:
        if self.product_type is not ProductType.SERVICE and self.base_uom_code is None:
            raise ValueError("Solo un servicio puede quedarse sin unidad de medida")
        return self


class ProductCreate(_ProductFields):
    internal_reference: Annotated[str, Field(min_length=1, max_length=32)]

    @field_validator("internal_reference", mode="after")
    @classmethod
    def _check_reference(cls, value: str) -> str:
        return _plain_text(value)


class ProductUpdate(_ProductFields):
    """La referencia interna no se edita: es la clave de negocio."""


# ---------------------------------------------------------------------------
# Terceros
# ---------------------------------------------------------------------------
class PartnerOut(_Out):
    id: int
    name: str
    role: PartnerRole
    document_type: DocumentType | None
    document_number: str | None
    address: str | None
    reference: str | None
    ubigeo_code: str | None
    district: str | None
    province: str | None
    department: str | None
    country: str | None
    email: str | None
    mobile: str | None
    phone: str | None
    active: bool
    notes: str | None


class PartnerPage(BaseModel):
    items: list[PartnerOut]
    total: int
    limit: int
    offset: int


class PartnerWrite(_In):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    role: PartnerRole
    document_type: DocumentType | None = None
    document_number: Annotated[str | None, Field(max_length=20)] = None
    address: Annotated[str | None, Field(max_length=240)] = None
    reference: Annotated[str | None, Field(max_length=240)] = None
    ubigeo_code: Annotated[str | None, Field(min_length=6, max_length=6)] = None
    email: Annotated[str | None, Field(max_length=160)] = None
    mobile: Annotated[str | None, Field(max_length=32)] = None
    phone: Annotated[str | None, Field(max_length=32)] = None
    active: bool = True
    notes: Annotated[str | None, Field(max_length=2000)] = None

    @field_validator("name", "notes", "address", "reference", mode="after")
    @classmethod
    def _check_text(cls, value: str | None) -> str | None:
        return None if value is None else _plain_text(value)

    @model_validator(mode="after")
    def _document_pair(self) -> PartnerWrite:
        if (self.document_type is None) != (self.document_number is None):
            raise ValueError("El tipo y el numero de documento van juntos")
        return self


class PartnerCreate(PartnerWrite):
    pass


class PartnerUpdate(PartnerWrite):
    pass


def parse_price_text(value: Any) -> Decimal:
    """Precio del maestro, que llega como texto con separador de millares."""
    return parse_decimal(value)
