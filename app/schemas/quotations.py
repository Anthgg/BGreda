"""Contratos HTTP del cotizador integral de Fase 005."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.quotations import (
    AdditionalFormulaType,
    OtherCostCalculationType,
    QuotationStatus,
    TechniqueFormulaType,
)

Money = Annotated[Decimal, Field(ge=0, max_digits=36, decimal_places=18)]
PositiveFactor = Annotated[Decimal, Field(gt=0, max_digits=36, decimal_places=18)]
PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeStrictInt = Annotated[int, Field(strict=True, ge=0)]
SortOrder = Annotated[int, Field(strict=True, ge=0, le=10000)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TechniqueBase(_Strict):
    code: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    unit_price: Money
    formula_type: TechniqueFormulaType
    factor_1: PositiveFactor
    factor_2: PositiveFactor | None = None
    active: bool = True
    notes: Annotated[str | None, Field(max_length=2000)] = None

    @model_validator(mode="after")
    def _formula_factors(self) -> TechniqueBase:
        if self.formula_type is TechniqueFormulaType.ONE_FACTOR and self.factor_2 is not None:
            raise ValueError("Una tecnica ONE_FACTOR no admite factor_2")
        if self.formula_type is TechniqueFormulaType.TWO_FACTORS and self.factor_2 is None:
            raise ValueError("Una tecnica TWO_FACTORS requiere factor_2")
        return self


class TechniqueCreate(TechniqueBase):
    pass


class TechniqueUpdate(TechniqueBase):
    pass


class TechniqueOut(_Out):
    id: int
    code: str
    name: str
    unit_price: Decimal
    formula_type: TechniqueFormulaType
    factor_1: Decimal
    factor_2: Decimal | None
    active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class TechniquePage(_Out):
    items: list[TechniqueOut]
    total: int
    limit: int
    offset: int


class AdditionalBase(_Strict):
    code: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    unit_price: Money
    formula_type: AdditionalFormulaType
    factor_1: PositiveFactor | None = None
    active: bool = True
    notes: Annotated[str | None, Field(max_length=2000)] = None

    @model_validator(mode="after")
    def _formula_factor(self) -> AdditionalBase:
        if self.formula_type is AdditionalFormulaType.SIMPLE_QUANTITY:
            if self.factor_1 is not None:
                raise ValueError("SIMPLE_QUANTITY no admite factor_1")
        elif self.active and self.factor_1 is None:
            raise ValueError("El adicional activo por piezas requiere factor_1")
        return self


class AdditionalCreate(AdditionalBase):
    pass


class AdditionalUpdate(AdditionalBase):
    pass


class AdditionalOut(_Out):
    id: int
    code: str
    name: str
    unit_price: Decimal
    formula_type: AdditionalFormulaType
    factor_1: Decimal | None
    active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class AdditionalPage(_Out):
    items: list[AdditionalOut]
    total: int
    limit: int
    offset: int


class OtherCostBase(_Strict):
    code: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    unit_price: Money
    calculation_type: OtherCostCalculationType
    active: bool = True
    notes: Annotated[str | None, Field(max_length=2000)] = None


class OtherCostCreate(OtherCostBase):
    pass


class OtherCostUpdate(OtherCostBase):
    pass


class OtherCostOut(_Out):
    id: int
    code: str
    name: str
    unit_price: Decimal
    calculation_type: OtherCostCalculationType
    active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class OtherCostPage(_Out):
    items: list[OtherCostOut]
    total: int
    limit: int
    offset: int


class TechniqueSelectionIn(_Strict):
    technique_id: PositiveStrictInt
    quantity: PositiveStrictInt
    unit_price: Money | None = None
    factor_1: PositiveFactor | None = None
    factor_2: PositiveFactor | None = None
    applied_cost: Money | None = None
    applied_days: NonNegativeStrictInt | None = None
    sort_order: SortOrder = 0


class AdditionalSelectionIn(_Strict):
    additional_id: PositiveStrictInt
    additional_quantity: Money | None = None
    unit_price: Money | None = None
    factor_1: PositiveFactor | None = None
    applied_cost: Money | None = None
    sort_order: SortOrder = 0


class OtherCostSelectionIn(_Strict):
    other_cost_id: PositiveStrictInt
    unit_price: Money | None = None
    sort_order: SortOrder = 0


class QuotationCalculateIn(_Strict):
    product_id: PositiveStrictInt
    quantity: PositiveStrictInt
    recipe_id: PositiveStrictInt | None = None
    recipe_version_id: PositiveStrictInt | None = None
    firing_line_id: PositiveStrictInt | None = None
    materials_applied: Money | None = None
    #: Gramos de receta por pieza. El costo de materiales se calcula sobre
    #: `gramos_por_pieza x cantidad`.
    #:
    #: **No hay valor por omision.** Ausente significa «todavia no indicado»:
    #: el calculo devuelve cero y avisa, y si hay receta seleccionada hay que
    #: informarlo antes de poder confirmar.
    material_grams_per_piece: Annotated[
        Decimal | None, Field(gt=Decimal(0), le=Decimal(1_000_000))
    ] = None
    techniques: list[TechniqueSelectionIn] = Field(default_factory=list, max_length=100)
    additionals: list[AdditionalSelectionIn] = Field(default_factory=list, max_length=100)
    days_adjustment: Annotated[int, Field(strict=True, ge=-10000, le=10000)] = 0
    waiting_days: NonNegativeStrictInt = 0
    other_costs: list[OtherCostSelectionIn] | None = Field(default=None, max_length=100)
    commercial_factor: PositiveFactor | None = None

    @model_validator(mode="after")
    def _recipe_pair(self) -> QuotationCalculateIn:
        if self.recipe_version_id is not None and self.recipe_id is None:
            raise ValueError("recipe_id es obligatorio cuando se indica recipe_version_id")
        return self


class QuotationCreateIn(QuotationCalculateIn):
    pass


class QuotationUpdateIn(QuotationCalculateIn):
    expected_source_fingerprint: Annotated[str, Field(min_length=64, max_length=64)]
    accept_source_changes: bool = False


class QuotationConfirmIn(_Strict):
    accept_source_changes: bool = False


class TechniqueCalculationOut(_Out):
    id: int | None = None
    technique_id: int
    name_snapshot: str
    unit_price_snapshot: Decimal
    formula_type_snapshot: TechniqueFormulaType
    factor_1_snapshot: Decimal
    factor_2_snapshot: Decimal | None
    quantity: int
    proposed_cost: Decimal
    applied_cost: Decimal
    proposed_days: int
    applied_days: int
    adjusted: bool
    sort_order: int


class AdditionalCalculationOut(_Out):
    id: int | None = None
    additional_id: int
    name_snapshot: str
    unit_price_snapshot: Decimal
    formula_type_snapshot: AdditionalFormulaType
    factor_1_snapshot: Decimal | None
    additional_quantity: Decimal | None
    proposed_cost: Decimal
    applied_cost: Decimal
    adjusted: bool
    formula_explanation: str
    sort_order: int


class OtherCostCalculationOut(_Out):
    id: int | None = None
    other_cost_id: int
    name_snapshot: str
    unit_price_snapshot: Decimal
    calculation_type_snapshot: OtherCostCalculationType
    proposed_cost: Decimal
    applied_cost: Decimal
    adjusted: bool
    sort_order: int


class QuotationCalculateOut(_Out):
    product_id: int
    product_internal_reference: str
    product_name: str
    quantity: int
    recipe_id: int | None
    recipe_version_id: int | None
    recipe_version_fingerprint_snapshot: str | None
    firing_id: int | None
    firing_line_id: int | None
    firing_code_snapshot: str | None
    firing_snapshot: dict[str, object]
    materials_calculated: Decimal
    materials_applied: Decimal
    firing_cost: Decimal
    labor_cost: Decimal
    calculated_days: int
    days_adjustment: int
    waiting_days: int
    total_days: int
    space_cost: Decimal
    commercial_factor_default_snapshot: Decimal
    commercial_factor: Decimal
    current_sale_price_snapshot: Decimal | None
    base_commercial_cost: Decimal
    calculated_total: Decimal
    calculated_unit_price: Decimal
    #: Componentes de la receta sin costo unitario. Suman cero y abaratan el
    #: material sin que nada lo diga, asi que se nombran.
    materials_without_cost: list[str] = []
    #: Nulo mientras no se indique: no existe un valor por omision.
    material_grams_per_piece: Decimal | None
    #: Gramos totales de receta: `gramos_por_pieza x cantidad`.
    material_total_grams: Decimal | None
    #: IGV aplicado, en porcentaje. El total y el unitario de arriba son netos.
    tax_percentage: Decimal
    #: PRODUCT si el producto define su propia tasa; COMMERCIAL_SETTINGS si
    #: se hereda de la configuracion.
    tax_rate_source: Literal["PRODUCT", "COMMERCIAL_SETTINGS"]
    tax_amount: Decimal
    total_with_tax: Decimal
    unit_price_with_tax: Decimal
    source_fingerprint: str
    warnings: list[str]
    #: La regla del IGV si esta definida: la cotizacion se emite sin impuesto y
    #: el documento entregado muestra el neto y el total con IGV.
    igv_rule_source: Literal["FOUND"] = "FOUND"
    #: Ninguna fuente define descuento; sigue sin implementarse.
    discount_rule_source: Literal["NOT_FOUND"] = "NOT_FOUND"
    techniques: list[TechniqueCalculationOut]
    additionals: list[AdditionalCalculationOut]
    other_costs: list[OtherCostCalculationOut]


class QuotationOut(QuotationCalculateOut):
    id: int
    code: str
    status: QuotationStatus
    created_by_id: str | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class QuotationSummaryOut(_Out):
    id: int
    code: str
    status: QuotationStatus
    product_id: int
    product_internal_reference: str
    product_name: str
    quantity: int
    calculated_unit_price: Decimal
    calculated_total: Decimal
    total_with_tax: Decimal
    created_at: datetime


class QuotationPage(_Out):
    items: list[QuotationSummaryOut]
    total: int
    limit: int
    offset: int


class ProductPriceUpdateOut(_Out):
    quotation_id: int
    product_id: int
    old_price: Decimal | None
    new_price: Decimal
    updated_at: datetime
