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
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.pricing_engine import PricingEngineVersion
from app.models.quoter_v2 import (
    V2CustomerKind,
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
    #: Por menor / por mayor. Lo decide la persona; el sistema jamas lo cambia,
    #: ni siquiera si la cantidad parece de por mayor. Ausente = lo que diga la
    #: configuracion.
    production_type: V2ProductionType | None = None
    #: A quien se cotiza, a efectos de tarifa de horno. Explicito: no se deduce
    #: del nombre del cliente.
    customer_kind: V2CustomerKind | None = None
    #: Fase 010B. Lo que el alta no diga, lo pone la configuracion. Lo que diga,
    #: manda y queda congelado en la cotizacion.
    currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    #: Solo tiene sentido en moneda extranjera. MANUAL, como en Legacy.
    exchange_rate: Decimal | None = Field(default=None, gt=0)
    #: Nunca por debajo del minimo vigente; el servicio lo comprueba contra la
    #: configuracion y la base lo vuelve a exigir.
    commercial_factor: Decimal | None = Field(default=None, ge=2)
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

    #: Fase 010B. La copia congelada de la configuracion. Viaja en la respuesta
    #: para que la pantalla muestre con que numeros se armo ESTA cotizacion, y
    #: no los que la configuracion tenga hoy.
    customer_kind: V2CustomerKind | None
    tax_percent: Decimal | None
    currency_code: str | None
    currency_symbol: str | None
    exchange_rate: Decimal | None
    validity_days: int | None
    workday_hours: Decimal | None
    space_service_cost_per_day: Decimal | None
    administrative_cost: Decimal | None
    commercial_factor: Decimal | None
    commercial_factor_min: Decimal | None
    commercial_factor_max: Decimal | None
    low_fire_enabled: bool | None
    high_fire_enabled: bool | None
    settings_version: int | None

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
