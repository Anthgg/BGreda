"""Contratos y esquemas de validacion para recetas, versiones y calculos."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

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
    target_uom: Annotated[str, Field(max_length=16)] = "g"


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
