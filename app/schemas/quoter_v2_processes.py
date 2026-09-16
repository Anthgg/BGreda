"""Contratos de procesos y adicionales del Cotizador V2 (correccion 010H)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

MAX_QUANTITY = Decimal("9999999999.999999")


# ---------------------------------------------------------------- maestro
class V2ProductTechniquesIn(BaseModel):
    """Los procesos que una pieza del catalogo necesita, en orden."""

    model_config = ConfigDict(extra="forbid")

    technique_ids: list[int] = Field(default_factory=list)


class V2ProductTechniqueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    technique_id: int
    technique_name: str
    sort_order: int
    active: bool


class V2ProductTechniquesOut(BaseModel):
    product_id: int
    items: list[V2ProductTechniqueOut]


# ---------------------------------------------------------- procesos de CTZ
class V2ProcessIn(BaseModel):
    """Un proceso mas para una linea, solo en esta cotizacion."""

    model_config = ConfigDict(extra="forbid")

    v2_quotation_product_id: int = Field(ge=1)
    technique_id: int = Field(ge=1)
    #: Ausente: las piezas de la linea (0 si la tecnica decide sus horas a mano).
    quantity: Decimal | None = Field(default=None, ge=0, le=MAX_QUANTITY)


class V2ProcessQuantityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: Decimal = Field(ge=0, le=MAX_QUANTITY)


class V2ProcessAssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: int = Field(ge=1)


class V2ProcessOut(BaseModel):
    id: int
    v2_quotation_product_id: int
    product_name: str | None
    technique_id: int
    technique_name: str
    technique_unit: str
    technique_active: bool
    standard_capacity: Decimal
    manual_hours: bool
    origin: str
    quantity: Decimal
    quantity_overridden: bool
    #: Horas al rendimiento estandar. `None` cuando la tecnica las decide a mano.
    calculated_hours: Decimal | None
    #: La tarea, cuando ya hay alguien asignado. Sin ella el proceso no cuesta.
    labor_id: int | None
    worker_id: int | None
    worker_name: str | None
    final_hours: Decimal | None
    labor_cost: Decimal | None
    warnings: list[str] = Field(default_factory=list)


class V2ProcessPage(BaseModel):
    items: list[V2ProcessOut]
    warnings: list[str] = Field(default_factory=list)


# ------------------------------------------------------------- adicionales
class V2ExtraIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    unit: str = Field(default="servicio", max_length=32)
    unit_cost: Decimal = Field(default=Decimal(0), ge=0, le=MAX_QUANTITY)
    active: bool = True
    notes: str | None = None


class V2ExtraUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    unit: str | None = Field(default=None, max_length=32)
    unit_cost: Decimal | None = Field(default=None, ge=0, le=MAX_QUANTITY)
    active: bool | None = None
    notes: str | None = None


class V2ExtraOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    unit: str
    unit_cost: Decimal
    active: bool
    notes: str | None
    version: int


class V2ExtraPage(BaseModel):
    items: list[V2ExtraOut]


class V2QuotationExtraIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v2_extra_id: int = Field(ge=1)
    #: Ausente o nulo: el adicional es de todo el pedido, como en el Excel.
    v2_quotation_product_id: int | None = Field(default=None, ge=1)
    quantity: Decimal = Field(default=Decimal(0), ge=0, le=MAX_QUANTITY)
    #: Ausente: el precio del maestro. Presente: esta cotizacion paga otro.
    unit_cost: Decimal | None = Field(default=None, ge=0, le=MAX_QUANTITY)
    description: str | None = None


class V2QuotationExtraUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v2_quotation_product_id: int | None = Field(default=None, ge=1)
    quantity: Decimal | None = Field(default=None, ge=0, le=MAX_QUANTITY)
    unit_cost: Decimal | None = Field(default=None, ge=0, le=MAX_QUANTITY)
    description: str | None = None


class V2QuotationExtraOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    v2_extra_id: int
    v2_quotation_product_id: int | None
    name_snapshot: str
    unit_snapshot: str
    unit_cost_snapshot: Decimal
    unit_cost_is_override: bool
    description: str | None
    quantity: Decimal
    total_cost: Decimal
    sort_order: int
    created_at: datetime


class V2QuotationExtraPage(BaseModel):
    items: list[V2QuotationExtraOut]
    extras_cost_total: Decimal
    warnings: list[str] = Field(default_factory=list)
