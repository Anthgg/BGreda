"""Contrato publico de Solo Quema V2 (fase 010K).

Lo que viaja de ida son DECISIONES —cliente, piezas y medidas, horno, modo,
ciclos, separacion, factor, vidriado— y lo que vuelve son consecuencias ya
calculadas: volumen, ocupacion, hornadas, importes. Aceptar una ocupacion o un
precio desde la pantalla seria dejar que ella decidiera lo que se cobra.

Hay dos lecturas y no se mezclan:

- la INTERNA (`V2FiringQuotationOut`): gas, costo real, ganancia y margen, el
  comparador de hornos. Solo la ve administracion;
- la del CLIENTE (`V2FiringQuotationPreviewOut` y el PDF): piezas, servicio,
  subtotal, IGV y total. Sin gas, ocupacion, factor ni ganancia.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.quoter_v2_config import MAX_PIECE_SEPARATION_CM
from app.core.quoter_v2_lifecycle import V2EffectiveStatus
from app.models.firing_quotation_v2 import (
    FIRING_QUOTATION_FACTOR_MAX,
    FIRING_QUOTATION_FACTOR_MIN,
    V2GlazeCostSource,
)
from app.models.quoter_v2 import V2CustomerKind, V2FiringMode, V2QuotationStatus

MAX_DIMENSION = Decimal("10000")
MAX_GRAMS = Decimal("100000000")
MAX_UNIT_COST = Decimal("1000000")
MAX_EXCHANGE_RATE = Decimal("1000")
MAX_LABOR_QUANTITY = Decimal("1000000000")


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    limpio = value.strip()
    return limpio or None


class V2FiringQuotationCreateIn(BaseModel):
    """Abrir un borrador. Todo es opcional: el resto se decide despues."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    customer_id: int | None = Field(default=None, ge=1)
    customer_kind: V2CustomerKind | None = None
    currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    exchange_rate: Decimal | None = Field(default=None, gt=0, le=MAX_EXCHANGE_RATE)
    notes: str | None = Field(default=None, max_length=4000)
    client_notes: str | None = Field(default=None, max_length=4000)

    @field_validator("name", "notes", "client_notes")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class V2FiringQuotationUpdateIn(BaseModel):
    """Cambiar un borrador. Semantica de PATCH: lo ausente se conserva."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)
    client_notes: str | None = Field(default=None, max_length=4000)
    customer_id: int | None = Field(default=None, ge=1)
    customer_kind: V2CustomerKind | None = None
    currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    exchange_rate: Decimal | None = Field(default=None, gt=0, le=MAX_EXCHANGE_RATE)

    #: El horno ELEGIDO. En nulo lo retira.
    kiln_id: int | None = Field(default=None, ge=1)
    firing_mode: V2FiringMode | None = None
    low_fire_enabled: bool | None = None
    high_fire_enabled: bool | None = None
    piece_separation_cm: Decimal | None = Field(default=None, ge=0, le=MAX_PIECE_SEPARATION_CM)
    #: x1,00 a x2,00, cualquier decimal dentro (1,10; 1,17...).
    factor: Decimal | None = Field(
        default=None, ge=FIRING_QUOTATION_FACTOR_MIN, le=FIRING_QUOTATION_FACTOR_MAX
    )

    glaze_enabled: bool | None = None
    glaze_grams: Decimal | None = Field(default=None, ge=0, le=MAX_GRAMS)
    glaze_cost_source: V2GlazeCostSource | None = None
    #: Un esmalte concreto del maestro. En nulo vuelve al activo mas caro.
    glaze_material_id: int | None = Field(default=None, ge=1)
    glaze_manual_cost_per_gram: Decimal | None = Field(default=None, ge=0, le=MAX_UNIT_COST)

    glaze_labor_enabled: bool | None = None
    glaze_labor_worker_id: int | None = Field(default=None, ge=1)
    glaze_labor_technique_id: int | None = Field(default=None, ge=1)
    glaze_labor_quantity: Decimal | None = Field(default=None, ge=0, le=MAX_LABOR_QUANTITY)

    @field_validator("name", "notes", "client_notes")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class V2FiringQuotationLineIn(BaseModel):
    """Una pieza del cliente. Semantica de PATCH al editar."""

    model_config = ConfigDict(extra="forbid")

    product_id: int | None = Field(default=None, ge=1)
    product_name: str | None = Field(default=None, max_length=200)
    quantity: int | None = Field(default=None, ge=0, le=1_000_000)
    length_cm: Decimal | None = Field(default=None, gt=0, le=MAX_DIMENSION)
    width_cm: Decimal | None = Field(default=None, gt=0, le=MAX_DIMENSION)
    height_cm: Decimal | None = Field(default=None, gt=0, le=MAX_DIMENSION)

    @field_validator("product_name")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class V2FiringQuotationLineOut(BaseModel):
    id: int
    sort_order: int
    product_id: int | None
    product_name: str | None
    quantity: int
    length_cm: Decimal | None
    width_cm: Decimal | None
    height_cm: Decimal | None
    #: La separacion de la cabecera, repetida para leer la fila sola.
    separation_cm: Decimal
    unit_volume_cm3: Decimal
    total_volume_cm3: Decimal
    volume_share_percent: Decimal


class V2FiringModeQuoteOut(BaseModel):
    billed_load: Decimal
    commercial: Decimal
    gas: Decimal


class V2FiringQuotationKilnOut(BaseModel):
    """Un horno en el comparador: la misma carga, compartida y exclusiva."""

    kiln_id: int
    name: str
    capacity_cm3: Decimal
    active: bool
    selected: bool
    occupancy_percent: Decimal
    firing_count: int
    batch_loads: list[Decimal]
    #: `None` si al horno le falta la tarifa de algun ciclo encendido.
    shared: V2FiringModeQuoteOut | None
    exclusive: V2FiringModeQuoteOut | None


class V2FiringQuotationSuggestionOut(BaseModel):
    """«El horno grande reduce el costo estimado en S/ X». Nunca se aplica solo."""

    kiln_id: int
    name: str
    commercial: Decimal
    savings: Decimal


class V2FiringQuotationOut(BaseModel):
    """La lectura INTERNA de un servicio de quema."""

    id: int
    code: str
    status: V2QuotationStatus
    effective_status: V2EffectiveStatus
    name: str | None
    notes: str | None
    client_notes: str | None
    customer_id: int | None
    customer_name: str | None
    customer_kind: V2CustomerKind
    currency_code: str | None
    currency_symbol: str | None
    exchange_rate: Decimal | None
    tax_percent: Decimal | None
    rounding_step: Decimal | None
    validity_days: int | None

    kiln_id: int | None
    kiln_name: str | None
    kiln_capacity_cm3: Decimal | None
    firing_mode: V2FiringMode
    low_fire_enabled: bool
    high_fire_enabled: bool
    piece_separation_cm: Decimal
    total_volume_cm3: Decimal
    occupancy_percent: Decimal
    firing_count: int
    billed_load: Decimal
    batch_loads: list[Decimal]
    commercial_rate_low: Decimal | None
    commercial_rate_high: Decimal | None
    gas_cost_low: Decimal | None
    gas_cost_high: Decimal | None
    firing_commercial_total: Decimal
    firing_gas_total: Decimal

    glaze_enabled: bool
    glaze_grams: Decimal
    glaze_cost_source: V2GlazeCostSource
    glaze_material_id: int | None
    glaze_material_name: str | None
    glaze_manual_cost_per_gram: Decimal | None
    glaze_cost_per_gram: Decimal | None
    glaze_material_cost: Decimal
    glaze_labor_enabled: bool
    glaze_labor_worker_id: int | None
    glaze_labor_worker_name: str | None
    glaze_labor_worker_type: str | None
    glaze_labor_technique_id: int | None
    glaze_labor_technique_name: str | None
    glaze_labor_quantity: Decimal
    glaze_labor_hours: Decimal
    glaze_labor_cost: Decimal

    factor: Decimal
    factor_min: Decimal
    factor_max: Decimal
    base_amount: Decimal
    commercial_price: Decimal
    subtotal_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    real_cost_total: Decimal
    estimated_profit: Decimal
    effective_margin_percent: Decimal

    kilns: list[V2FiringQuotationKilnOut]
    suggestion: V2FiringQuotationSuggestionOut | None
    lines: list[V2FiringQuotationLineOut]
    warnings: list[str]

    issued_at: datetime | None
    valid_until: date | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    duplicated_from_id: int | None
    created_at: datetime
    created_by_name: str | None


class V2FiringQuotationListItemOut(BaseModel):
    id: int
    code: str
    status: V2QuotationStatus
    effective_status: V2EffectiveStatus
    name: str | None
    customer_name: str | None
    currency_code: str | None
    total_amount: Decimal
    created_at: datetime
    valid_until: date | None


class V2FiringQuotationPage(BaseModel):
    items: list[V2FiringQuotationListItemOut]
    total: int


class V2FiringQuotationBlockerOut(BaseModel):
    code: str
    line_id: int | None = None


class V2FiringQuotationPreviewLineOut(BaseModel):
    """Una pieza tal como la vera el cliente: sin volumen ni precio por linea."""

    id: int
    product_name: str | None
    quantity: int
    length_cm: Decimal | None
    width_cm: Decimal | None
    height_cm: Decimal | None


class V2FiringQuotationPreviewOut(BaseModel):
    """Lo que se revisa antes de emitir y lo que dira el documento.

    Sin gas, ocupacion, factor, costo ni ganancia (hoja «PDF Quema», A24).
    """

    id: int
    code: str
    status: V2QuotationStatus
    effective_status: V2EffectiveStatus
    can_confirm: bool
    blockers: list[V2FiringQuotationBlockerOut]
    warnings: list[str]
    fingerprint: str
    customer_name: str | None
    name: str | None
    client_notes: str | None
    kiln_name: str | None
    service_label: str
    firing_mode: V2FiringMode
    glaze_enabled: bool
    currency_code: str | None
    currency_symbol: str | None
    exchange_rate: Decimal | None
    tax_percent: Decimal | None
    validity_days: int | None
    valid_until: date | None
    subtotal_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    lines: list[V2FiringQuotationPreviewLineOut]


class V2FiringQuotationConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class V2FiringQuotationCancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class V2FiringQuotationDuplicateOut(BaseModel):
    """El borrador nuevo. `created` en falso: ya existia y se devuelve ese."""

    quotation: V2FiringQuotationOut
    created: bool
    warnings: list[str]
