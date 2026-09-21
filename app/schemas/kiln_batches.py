"""Contratos de la API de planificacion de hornadas. Fase 010L."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.firings import FiringType
from app.models.kiln_batches import KilnBatchSourceKind, KilnBatchStatus
from app.models.quoter_v2 import V2FiringMode


class KilnBatchCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kiln_id: int = Field(gt=0)
    firing_type: FiringType
    scheduled_date: date
    notes: str | None = Field(default=None, max_length=2000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)


class KilnBatchUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    notes_set: bool = False
    expected_version: int = Field(ge=1)


class KilnBatchCancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class KilnBatchAssignItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: int = Field(gt=0)
    quantity: int = Field(gt=0)


class KilnBatchAssignmentCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    production_order_id: int | None = Field(default=None, gt=0)
    internal_load_id: int | None = Field(default=None, gt=0)
    items: list[KilnBatchAssignItemIn] = Field(min_length=1)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)

    @model_validator(mode="after")
    def _un_origen(self) -> KilnBatchAssignmentCreateIn:
        if (self.production_order_id is None) == (self.internal_load_id is None):
            raise ValueError("Indica una orden o una carga interna, no ambas.")
        return self


class KilnBatchAssignmentReleaseIn(KilnBatchAssignmentCreateIn):
    pass


class KilnBatchMoveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_batch_id: int = Field(gt=0)
    to_batch_id: int = Field(gt=0)
    production_order_id: int | None = Field(default=None, gt=0)
    internal_load_id: int | None = Field(default=None, gt=0)
    items: list[KilnBatchAssignItemIn] = Field(min_length=1)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)

    @model_validator(mode="after")
    def _un_origen(self) -> KilnBatchMoveIn:
        if (self.production_order_id is None) == (self.internal_load_id is None):
            raise ValueError("Indica una orden o una carga interna, no ambas.")
        return self


class KilnBatchAssignmentOut(BaseModel):
    id: int
    batch_id: int
    source_kind: KilnBatchSourceKind
    production_order_id: int | None
    internal_load_id: int | None
    line_id: int
    product_name: str
    quantity: int
    unit_volume_cm3: Decimal
    assigned_volume_cm3: Decimal
    firing_mode: V2FiringMode


class KilnBatchOut(BaseModel):
    id: int
    code: str
    kiln_id: int
    kiln_name_snapshot: str
    firing_type: FiringType
    scheduled_date: date
    status: KilnBatchStatus
    capacity_snapshot_cm3: Decimal
    assigned_volume_cm3: Decimal
    occupancy_percent: Decimal
    available_percent: Decimal
    available_cm3: Decimal
    exclusive: bool
    version: int
    notes: str | None
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    assignments: list[KilnBatchAssignmentOut]


class KilnBatchPage(BaseModel):
    items: list[KilnBatchOut]
    total: int
    limit: int
    offset: int


class KilnBatchMoveOut(BaseModel):
    from_batch: KilnBatchOut
    to_batch: KilnBatchOut


class FiringPlanLineOut(BaseModel):
    line_id: int
    product_name: str
    quantity: int
    unit_volume_cm3: Decimal
    required_low: int | None = None
    assigned_low: int | None = None
    remaining_low: int | None = None
    required_high: int | None = None
    assigned_high: int | None = None
    remaining_high: int | None = None


class FiringPlanOut(BaseModel):
    source_kind: KilnBatchSourceKind
    production_order_id: int | None
    internal_load_id: int | None
    code: str
    origin_code: str | None
    customer_name: str | None
    firing_mode: V2FiringMode
    needs_low: bool
    needs_high: bool
    glaze_required: bool
    open: bool
    lines: list[FiringPlanLineOut]
    batches: list[KilnBatchOut]


class KilnBatchSuggestionOut(BaseModel):
    batch_id: int
    kiln_id: int
    kiln_name: str
    scheduled_date: date
    occupancy_percent: Decimal
    available_percent: Decimal
    available_cm3: Decimal
    covers_all: bool
    covered_percent: Decimal
