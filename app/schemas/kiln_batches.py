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


# ---------------------------------------------------------------------------
# Fase 010M: layout fisico del horno
# ---------------------------------------------------------------------------

class KilnBatchLayoutPlacementInput(BaseModel):
    """Un placement individual en el PUT del layout."""

    model_config = ConfigDict(extra="forbid")

    batch_assignment_id: int = Field(gt=0)
    group_index: int = Field(default=0, ge=0)
    unit_index: int | None = Field(default=None, ge=0)
    quantity: int = Field(gt=0)
    level_index: int = Field(ge=0)
    x_cm: Decimal = Field(ge=0)
    y_cm: Decimal = Field(ge=0)
    rotation_degrees: int = Field(default=0)

    @model_validator(mode="after")
    def _rotation_only_0_or_90(self) -> KilnBatchLayoutPlacementInput:
        if self.rotation_degrees not in (0, 90):
            raise ValueError("rotation_degrees debe ser 0 o 90")
        return self


class KilnBatchLayoutLevelInput(BaseModel):
    """Un nivel del horno en el PUT del layout."""

    model_config = ConfigDict(extra="forbid")

    level_index: int = Field(ge=0)
    name: str | None = Field(default=None, max_length=120)
    z_cm: Decimal = Field(ge=0)
    usable_height_cm: Decimal = Field(gt=0)
    plate_label: str | None = Field(default=None, max_length=100)
    plate_thickness_cm: Decimal | None = Field(default=None, ge=0)


class KilnBatchLayoutUpdate(BaseModel):
    """Cuerpo del PUT /kiln-batches/{batch_id}/layout.

    `expected_version = 0` indica creacion inicial (no existe layout todavia).
    Cualquier otro valor debe coincidir con el `version` actual del layout.
    """

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)
    levels: list[KilnBatchLayoutLevelInput] = Field(default_factory=list)
    placements: list[KilnBatchLayoutPlacementInput] = Field(default_factory=list)


class KilnBatchLayoutPlacementOut(BaseModel):
    """Un placement devuelto por el GET del layout."""

    id: int
    batch_assignment_id: int
    group_index: int
    unit_index: int | None
    quantity: int
    level_index: int
    x_cm: Decimal
    y_cm: Decimal
    rotation_degrees: int
    piece_length_cm_snapshot: Decimal
    piece_width_cm_snapshot: Decimal
    piece_height_cm_snapshot: Decimal
    separation_cm_snapshot: Decimal


class KilnBatchLayoutLevelOut(BaseModel):
    """Un nivel devuelto por el GET del layout."""

    id: int
    level_index: int
    name: str | None
    z_cm: Decimal
    usable_height_cm: Decimal
    plate_label: str | None
    plate_thickness_cm: Decimal | None


class KilnBatchLayoutOut(BaseModel):
    """Respuesta completa del GET y PUT del layout.

    Solo incluye informacion operacional. Nunca precios, IGV, factor ni margen.
    """

    batch_id: int
    layout_id: int
    version: int
    kiln_width_cm_snapshot: Decimal
    kiln_depth_cm_snapshot: Decimal
    kiln_height_cm_snapshot: Decimal
    placed_quantity: int
    pending_quantity: int
    invalid_quantity: int = 0
    levels: list[KilnBatchLayoutLevelOut]
    placements: list[KilnBatchLayoutPlacementOut]
    updated_at: datetime


# ---------------------------------------------------------------------------
# Fase 010M - M3: Sugerencia de acomodo físico (Auto-packing)
# ---------------------------------------------------------------------------

class KilnBatchLayoutSuggestIn(BaseModel):
    """Cuerpo opcional del POST /kiln-batches/{batch_id}/layout/suggest."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=0)
    levels: list[KilnBatchLayoutLevelInput] | None = None


class SuggestedPlacementOut(BaseModel):
    """Un placement sugerido por el motor de auto-packing."""

    batch_assignment_id: int
    group_index: int
    unit_index: int | None
    quantity: int = 1
    level_index: int
    x_cm: Decimal
    y_cm: Decimal
    rotation_degrees: int
    piece_length_cm_snapshot: Decimal
    piece_width_cm_snapshot: Decimal
    piece_height_cm_snapshot: Decimal
    separation_cm_snapshot: Decimal


class UnplacedPieceOut(BaseModel):
    """Una pieza/unidad que no pudo ser ubicada en la sugerencia."""

    batch_assignment_id: int
    unit_index: int | None
    quantity: int = 1
    reason: str


class KilnBatchLayoutSuggestionOut(BaseModel):
    """Respuesta completa del POST /kiln-batches/{batch_id}/layout/suggest.

    Solo incluye información operacional. Nunca precios, IGV, factor ni margen.
    """

    batch_id: int
    base_version: int
    total_pending: int
    suggested_count: int
    unplaced_count: int
    levels_used: list[int]
    suggested_placements: list[SuggestedPlacementOut]
    unplaced_pieces: list[UnplacedPieceOut]


