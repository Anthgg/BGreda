"""Contratos y esquemas de validacion para recetas, versiones y calculos."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.recipes import RecipeComponentType, RecipeStatus

_TAG_PATTERN = re.compile(r"<\s*/?\s*[a-zA-Z]")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _plain_text(value: str) -> str:
    """Rechaza HTML y caracteres de control."""
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
# Lineas de receta
# ---------------------------------------------------------------------------
class RecipeLineIn(_In):
    component_product_id: int
    component_type: RecipeComponentType
    percentage: Annotated[Decimal, Field(gt=Decimal(0), le=Decimal(500))]
    sort_order: int = 0


class RecipeLineOut(_Out):
    id: int
    recipe_version_id: int
    component_product_id: int
    component_internal_reference: str
    component_name: str
    component_type: RecipeComponentType
    percentage: Decimal
    sort_order: int
    component_cost: Decimal | None = None
    component_uom: str | None = None


# ---------------------------------------------------------------------------
# Versiones de receta
# ---------------------------------------------------------------------------
class RecipeVersionIn(_In):
    lines: list[RecipeLineIn]
    notes: Annotated[str | None, Field(max_length=1000)] = None

    @field_validator("notes")
    @classmethod
    def _validate_notes(cls, value: str | None) -> str | None:
        return _plain_text(value) if value else None


class RecipeVersionOut(_Out):
    id: int
    recipe_id: int
    version_number: int
    status: RecipeStatus
    yield_factor: Decimal
    base_total: Decimal
    additional_total: Decimal
    fingerprint: str
    notes: str | None = None
    created_by_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    lines: list[RecipeLineOut] = []


# ---------------------------------------------------------------------------
# Recetas Cabecera
# ---------------------------------------------------------------------------
class RecipeCreate(_In):
    product_id: int
    name: Annotated[str, Field(min_length=1, max_length=200)]
    lines: list[RecipeLineIn]
    notes: Annotated[str | None, Field(max_length=1000)] = None
    active: bool = True
    activate_immediately: bool = True

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _plain_text(value)

    @field_validator("notes")
    @classmethod
    def _validate_notes(cls, value: str | None) -> str | None:
        return _plain_text(value) if value else None


class RecipeUpdate(_In):
    name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    active: bool | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        return _plain_text(value) if value else None


class RecipeOut(_Out):
    id: int
    product_id: int
    product_internal_reference: str
    product_name: str
    product_category_path: str | None = None
    name: str
    active: bool
    current_version_id: int | None = None
    current_version: RecipeVersionOut | None = None
    versions: list[RecipeVersionOut] = []
    versions_count: int = 0
    created_at: datetime
    updated_at: datetime


class RecipePage(BaseModel):
    items: list[RecipeOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Calculador / Simulador
# ---------------------------------------------------------------------------
class RecipeCalculateIn(_In):
    recipe_version_id: int | None = None
    recipe_id: int | None = None
    lines: list[RecipeLineIn] | None = None
    target_base_quantity: Annotated[Decimal, Field(gt=Decimal(0))] = Decimal("1000.000000")
    target_uom: Literal["g"] = "g"


class CalculatedComponentLineOut(_Out):
    component_product_id: int
    component_internal_reference: str
    component_name: str
    component_type: RecipeComponentType
    percentage: Decimal
    required_quantity: Decimal
    uom: str
    unit_cost_in_grams: Decimal
    component_cost: Decimal


class RecipeCalculateOut(_Out):
    target_base_quantity: Decimal
    target_uom: str
    yield_factor: Decimal
    real_output_quantity: Decimal
    base_cost: Decimal
    colorant_cost: Decimal
    additive_cost: Decimal
    total_material_cost: Decimal
    cost_per_real_unit: Decimal
    components: list[CalculatedComponentLineOut]


# ---------------------------------------------------------------------------
# Importacion de recetas (Staging Preview & Resolution)
# ---------------------------------------------------------------------------
class RecipeStagingLineOut(_Out):
    row_id: int
    source_row: int
    component_name_raw: str
    component_product_id: int | None = None
    component_reference: str | None = None
    component_product_name: str | None = None
    component_type: RecipeComponentType | None = None
    suggested_component_type: RecipeComponentType | None = None
    classification_role: str = "BASE"  # BASE, ADDITIONAL, UNKNOWN
    classification_source: str = (
        "SOURCE_STRUCTURE"  # SOURCE_STRUCTURE, HUMAN_RESOLUTION, UNRESOLVED, SUGGESTED
    )
    cumulative_percentage: Decimal = Decimal(0)
    source_percentage: Decimal
    final_percentage: Decimal
    percentage: Decimal
    resolution_source: str = "SOURCE"  # SOURCE, HUMAN, SUGGESTED, UNRESOLVED
    status: str = "READY"  # READY, REVIEW_REQUIRED, RESOLVED, SKIPPED, ERROR
    action: str = "CREATE"  # CREATE, SKIP
    requires_review: bool = False
    quantity_raw: Decimal | None = None
    uom_raw: str | None = None
    warnings: list[str] = []
    errors: list[str] = []


class RecipeStagingGroupOut(_Out):
    target_product_id: int
    target_internal_reference: str
    target_product_name: str
    recipe_name: str
    target_quantity: Decimal | None = None
    target_uom: str | None = None
    base_total: Decimal
    additional_total: Decimal
    yield_factor: Decimal
    estimated_cost_per_gram: Decimal
    is_valid: bool
    has_structural_base_boundary: bool = True
    status: str = "READY"  # READY, REVIEW_REQUIRED, ERROR
    warnings: list[str] = []
    errors: list[str] = []
    lines: list[RecipeStagingLineOut] = []


class RecipeImportPreviewOut(_Out):
    batch_id: int
    recipes_detected: int
    lines_detected: int
    ready_count: int
    review_required_count: int
    error_count: int
    recipes: list[RecipeStagingGroupOut] = []


class RecipeRowResolutionIn(_In):
    row_id: int
    component_type: RecipeComponentType | None = None
    percentage: Decimal | None = None
    action: str = "RESOLVE"  # RESOLVE, SKIP


# ---------------------------------------------------------------------------
# Preparaciones (Fase 009D)
# ---------------------------------------------------------------------------
class RecipePreparationIn(_In):
    """Peticion para registrar una preparacion fisica.

    No lleva `code`: lo emite el backend. Tampoco lleva la concentracion ni el
    costo: se calculan aqui, porque son autoridad del dominio.
    """

    recipe_version_id: int
    location_id: int
    total_dry_weight_g: Annotated[Decimal, Field(gt=Decimal(0))]
    water_amount_ml: Annotated[Decimal, Field(ge=Decimal(0))] = Decimal(0)
    #: Rendimiento REAL medido. No se deriva de peso seco + agua: los solidos
    #: ocupan volumen y el agua se absorbe, asi que la suma no es el volumen.
    final_yield_ml: Annotated[Decimal, Field(gt=Decimal(0))]
    #: Clave que impide ejecutar dos veces la misma preparacion fisica.
    idempotency_key: Annotated[str, Field(min_length=8, max_length=64)]


class RecipePreparationLineOut(_Out):
    id: int
    component_product_id: int
    component_internal_reference: str
    component_name: str
    quantity_g: Decimal
    #: Costo por gramo en el momento de preparar. Congelado.
    unit_cost_snapshot: Decimal
    line_cost: Decimal


class RecipePreparationOut(_Out):
    id: int
    code: str
    recipe_version_id: int
    prepared_product_id: int
    prepared_product_internal_reference: str
    prepared_product_name: str
    location_id: int
    total_dry_weight_g: Decimal
    water_amount_ml: Decimal
    final_yield_ml: Decimal
    solids_g_per_ml: Decimal
    batch_total_cost: Decimal
    unit_cost_per_ml: Decimal
    status: str
    prepared_at: datetime
    lines: list[RecipePreparationLineOut] = []


class RecipePreparationPage(_Out):
    items: list[RecipePreparationOut]
    total: int
    limit: int
    offset: int


class GlazeSelectionIn(_In):
    """Un esmalte concreto (un lote preparado) y su participacion en la pieza."""

    preparation_id: int
    #: Peso relativo del reparto. Dos esmaltes a 1 y 1 se llevan mitad y mitad;
    #: a 70 y 30 se llevan ese reparto. NO es un porcentaje del peso de la
    #: pieza: el porcentaje total ya lo fija la configuracion comercial.
    share: Annotated[Decimal, Field(gt=Decimal(0))] = Decimal(1)


class GlazeEstimateIn(_In):
    """Estimacion de esmalte para cotizar. No toca inventario.

    No lleva el porcentaje: es autoridad del backend y sale de
    ``commercial_settings.estimated_glaze_percent``. Si el cliente pudiera
    enviarlo, dos cotizaciones del mismo dia podrian estimar distinto sin que
    nadie lo hubiera decidido.
    """

    piece_weight_g: Annotated[Decimal, Field(gt=Decimal(0))]
    quantity: Annotated[int, Field(gt=0)]
    glazes: list[GlazeSelectionIn] = Field(default_factory=list, max_length=20)


class GlazeAllocationOut(_Out):
    preparation_id: int
    preparation_code: str
    prepared_product_id: int
    prepared_product_internal_reference: str
    prepared_product_name: str
    share: Decimal
    #: Gramos de solidos que le tocan a este esmalte del total estimado.
    grams: Decimal
    solids_g_per_ml: Decimal
    #: Los mismos gramos expresados en mililitros de preparado, con la
    #: concentracion de ESTE lote. Nunca con densidad 1.
    millilitres: Decimal
    unit_cost_per_ml: Decimal
    estimated_cost: Decimal


class GlazeEstimateOut(_Out):
    """Cuanto esmalte se estima y como se reparte. Es una simulacion."""

    estimated_glaze_percent: Decimal
    piece_weight_g: Decimal
    quantity: int
    grams_per_piece: Decimal
    total_estimated_grams: Decimal
    allocations: list[GlazeAllocationOut] = []
    total_estimated_cost: Decimal


class UnitConversionIn(_In):
    """Conversion g <-> ml apoyada en una preparacion concreta."""

    preparation_id: int
    value: Annotated[Decimal, Field(ge=Decimal(0))]
    from_unit: Literal["g", "ml"]


class UnitConversionOut(_Out):
    preparation_id: int
    solids_g_per_ml: Decimal
    value: Decimal
    from_unit: str
    converted: Decimal
    to_unit: str
