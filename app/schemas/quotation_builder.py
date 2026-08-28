"""Contratos del nuevo Cotizador multiproducto de Fase 005.11."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.quotations import QuotationStatus, QuotationWorkflow
from app.schemas.quotations import (
    AdditionalSelectionIn,
    OtherCostSelectionIn,
    TechniqueSelectionIn,
)

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
SortOrder = Annotated[int, Field(strict=True, ge=0, le=10_000)]
Money = Annotated[Decimal, Field(ge=0, max_digits=36, decimal_places=18)]
Dimension = Annotated[Decimal, Field(gt=0, le=100_000, max_digits=18, decimal_places=6)]
Markup = Annotated[Decimal, Field(ge=0, le=100_000, max_digits=36, decimal_places=18)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProductDimensionCompletionIn(_Strict):
    """Dimensiones efectivas para ESTA linea de cotizacion.

    Fase 009B: un valor aqui SIEMPRE gana sobre el maestro del producto para
    calcular y persistir la dimension efectiva de la linea — nunca se
    escribe de vuelta en products.* (el maestro es de solo lectura desde el
    Cotizador). Antes de esta fase, este contrato solo aceptaba completar
    campos ausentes en el maestro (y el guardado SI lo escribia en el
    maestro compartido); ver dimensions_overridden en QuotationBuilderItemIn
    para la bandera que distingue "usa el estandar" de "personalizado para
    esta cotizacion".
    """

    width: Dimension | None = None
    height: Dimension | None = None
    length: Dimension | None = None
    depth: Dimension | None = None


class QuotationBuilderItemIn(_Strict):
    id: PositiveInt | None = None
    product_id: PositiveInt
    quantity: PositiveInt | None = None
    dimensions: ProductDimensionCompletionIn = Field(default_factory=ProductDimensionCompletionIn)
    #: Intencion explicita del usuario (paso Piezas: "Usar medidas estandar"
    #: vs "Personalizar medidas"), independiente de si `dimensions` coincide
    #: numericamente con el maestro. Persistida en production_snapshot (no
    #: amerita columna propia); impulsa el badge "Medidas personalizadas" y
    #: sobrevive a que el maestro cambie despues (nunca se re-infiere
    #: comparando contra el maestro en vivo).
    dimensions_overridden: bool = False
    recipe_id: PositiveInt | None = None
    recipe_version_id: PositiveInt | None = None
    firing_line_id: PositiveInt | None = None
    materials_applied: Money | None = None
    material_grams_per_piece: Annotated[
        Decimal | None, Field(gt=0, le=1_000_000, max_digits=18, decimal_places=6)
    ] = None
    low_kiln_id: PositiveInt | None = None
    high_kiln_id: PositiveInt | None = None
    factor_kiln_id: PositiveInt | None = None
    techniques: list[TechniqueSelectionIn] = Field(default_factory=list, max_length=100)
    additionals: list[AdditionalSelectionIn] = Field(default_factory=list, max_length=100)
    days_adjustment: Annotated[int, Field(strict=True, ge=-10_000, le=10_000)] = 0
    waiting_days: Annotated[int, Field(strict=True, ge=0, le=10_000)] = 0
    other_costs: list[OtherCostSelectionIn] | None = Field(default=None, max_length=100)
    markup_percent: Markup = Decimal(100)
    commercial_sale_unit_price: Money | None = None
    sort_order: SortOrder = 0

    @model_validator(mode="after")
    def recipe_pair(self) -> QuotationBuilderItemIn:
        if self.recipe_version_id is not None and self.recipe_id is None:
            raise ValueError("recipe_id es obligatorio cuando se indica recipe_version_id")
        return self


class QuotationBuilderDraftIn(_Strict):
    name: Annotated[str | None, Field(max_length=200)] = None
    customer_id: PositiveInt | None = None
    kiln_id: PositiveInt | None = None
    items: list[QuotationBuilderItemIn] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_products(self) -> QuotationBuilderDraftIn:
        product_ids = [item.product_id for item in self.items]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("Un producto no puede repetirse en la misma cotizacion")
        return self


class QuotationBuilderCreateIn(QuotationBuilderDraftIn):
    pass


class QuotationBuilderUpdateIn(QuotationBuilderDraftIn):
    expected_updated_at: datetime


class QuotationBuilderConfirmIn(_Strict):
    expected_updated_at: datetime


class QuotationBuilderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    product_id: int
    product_internal_reference: str
    product_name: str
    product_type: str
    product_uom: str | None = None
    product_material: str | None = None
    product_grammage: Decimal | None = None
    #: Medidas EFECTIVAS de esta linea (personalizadas o heredadas del maestro).
    width: Decimal | None = None
    height: Decimal | None = None
    length: Decimal | None = None
    depth: Decimal | None = None
    #: Medidas ESTANDAR del maestro vigente, para que la UI pueda mostrar el
    #: contraste y restaurarlas al volver a "usar medidas estandar" sin tener
    #: que consultar el producto por separado (Fase 009B).
    standard_width: Decimal | None = None
    standard_height: Decimal | None = None
    standard_length: Decimal | None = None
    standard_depth: Decimal | None = None
    editable_dimensions: list[str] = Field(default_factory=list)
    dimensions_overridden: bool = False
    quantity: int | None = None
    recipe_id: int | None = None
    recipe_version_id: int | None = None
    recipe_version_fingerprint_snapshot: str | None = None
    recipe_auto_selected: bool = False
    materials_applied_input: Decimal | None = None
    material_grams_per_piece: Decimal | None = None
    firing_id: int | None = None
    firing_line_id: int | None = None
    firing_code_snapshot: str | None = None
    kiln_id: int | None = None
    low_kiln_id: int | None = None
    high_kiln_id: int | None = None
    factor_kiln_id: int | None = None
    production_snapshot: dict[str, Any] = Field(default_factory=dict)
    techniques: list[dict[str, Any]] = Field(default_factory=list)
    additionals: list[dict[str, Any]] = Field(default_factory=list)
    other_costs: list[dict[str, Any]] = Field(default_factory=list)
    materials_calculated: Decimal = Decimal(0)
    materials_applied: Decimal = Decimal(0)
    firing_cost: Decimal = Decimal(0)
    labor_cost: Decimal = Decimal(0)
    calculated_days: int = 0
    days_adjustment: int = 0
    waiting_days: int = 0
    total_days: int = 0
    space_cost: Decimal = Decimal(0)
    final_unit_cost: Decimal = Decimal(0)
    final_total_cost: Decimal = Decimal(0)
    markup_percent: Decimal = Decimal(100)
    calculated_sale_unit_price: Decimal = Decimal(0)
    suggested_commercial_unit_price: Decimal = Decimal(0)
    commercial_sale_unit_price_input: Decimal | None = None
    commercial_sale_unit_price: Decimal = Decimal(0)
    effective_profit_unit: Decimal = Decimal(0)
    effective_profit_total: Decimal = Decimal(0)
    effective_markup_percent: Decimal = Decimal(0)
    commercial_subtotal: Decimal = Decimal(0)
    commercial_unit_price_with_tax: Decimal = Decimal(0)
    commercial_total: Decimal = Decimal(0)
    tax_percentage_snapshot: Decimal = Decimal(0)
    tax_rate_source_snapshot: str = "COMMERCIAL_SETTINGS"
    tax_amount: Decimal = Decimal(0)
    source_fingerprint: str
    warnings: list[str] = Field(default_factory=list)
    complete: bool = False
    sort_order: int = 0


class QuotationBuilderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    code: str | None = None
    workflow: QuotationWorkflow = QuotationWorkflow.COTIZADOR
    status: QuotationStatus = QuotationStatus.DRAFT
    name: str | None = None
    customer_id: int | None = None
    customer_name_snapshot: str | None = None
    kiln_id: int | None = None
    kiln_snapshot: dict[str, Any] = Field(default_factory=dict)
    production_summary: dict[str, Any] = Field(default_factory=dict)
    items: list[QuotationBuilderItemOut] = Field(default_factory=list)
    item_count: int = 0
    commercial_subtotal: Decimal = Decimal(0)
    tax_percentage_snapshot: Decimal = Decimal(0)
    tax_rate_source_snapshot: str = "COMMERCIAL_SETTINGS"
    tax_amount: Decimal = Decimal(0)
    total_with_tax: Decimal = Decimal(0)
    currency_code_snapshot: str = "PEN"
    currency_symbol_snapshot: str = "S/"
    warnings: list[str] = Field(default_factory=list)
    complete: bool = False
    next_step: str = "GENERAL_DATA"
    source_fingerprint: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None
