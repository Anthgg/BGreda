"""Contrato publico del Cotizador V2.

Schemas **propios**. No se reexporta ni un campo de `app.schemas.quotations` ni
de `app.schemas.quotation_builder`, y no por gusto de duplicar: el contrato de
Legacy arrastra decenas de campos de costeo cuyo significado depende del motor
viejo. Compartirlo obligaria a que cada cambio de V2 pasara por la pantalla de
Legacy y al reves.

En 010A el contrato es deliberadamente corto: identidad, estado y las dos
decisiones de cabecera. Todo lo economico —materiales, quema, mano de obra,
factor comercial, IGV, moneda— entra con su fase y su snapshot.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.pricing_engine import PricingEngineVersion
from app.models.quoter_v2 import (
    DEFAULT_V2_PRODUCTION_TYPE,
    V2ProductionType,
    V2QuotationStatus,
)


def _blank_to_none(value: str | None) -> str | None:
    """Un campo vacio es ausencia, no una cadena vacia guardada para siempre."""
    if value is None:
        return None
    limpio = value.strip()
    return limpio or None


class V2QuotationCreateIn(BaseModel):
    """Alta de un borrador V2."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    customer_id: int | None = Field(default=None, ge=1)
    #: Por menor / por mayor. Lo decide la persona; el sistema jamas lo cambia.
    production_type: V2ProductionType = DEFAULT_V2_PRODUCTION_TYPE
    #: Acotado aunque la columna sea TEXT: un campo libre sin tope es un campo
    #: por el que cabe cualquier cosa, y 4000 caracteres son mas que de sobra
    #: para una nota interna.
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("name", "notes")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class V2QuotationOut(BaseModel):
    """Una cotizacion V2 tal como la ve el frontend."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    #: Siempre `V2`. Viaja en la respuesta a proposito: el cliente no tiene que
    #: adivinar el motor mirando el prefijo del codigo.
    pricing_engine_version: PricingEngineVersion
    status: V2QuotationStatus
    production_type: V2ProductionType
    customer_id: int | None
    customer_name: str | None
    name: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class V2QuotationListItemOut(BaseModel):
    """Fila del listado. Sin desglose: nadie necesita el detalle para elegir."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    pricing_engine_version: PricingEngineVersion
    status: V2QuotationStatus
    production_type: V2ProductionType
    customer_name: str | None
    name: str | None
    created_at: datetime


class V2QuotationPage(BaseModel):
    items: list[V2QuotationListItemOut]
    total: int
