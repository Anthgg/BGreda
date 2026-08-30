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
    QuotationWorkflow,
    TechniqueFormulaType,
)

Money = Annotated[Decimal, Field(ge=0, max_digits=36, decimal_places=18)]
PositiveFactor = Annotated[Decimal, Field(gt=0, max_digits=36, decimal_places=18)]
PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeStrictInt = Annotated[int, Field(strict=True, ge=0)]
SortOrder = Annotated[int, Field(strict=True, ge=0, le=10000)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


#: Codigo interno de un maestro de costos, aceptado pero IGNORADO.
#:
#: El codigo lo emite el backend: es identidad interna y una secuencia de
#: negocio, no un dato que el usuario invente. Antes se exigia en el payload y
#: el cliente lo escribia a mano, con lo que dos instalaciones podian numerar
#: distinto y editar una tecnica permitia cambiarle el codigo.
#:
#: El campo sigue aceptandose —sin usarse— para no romper a un cliente
#: desplegado que todavia lo envie; `extra="forbid"` lo rechazaria. Puede
#: retirarse cuando ningun cliente lo mande.
LegacyClientCode = Annotated[str | None, Field(max_length=64, deprecated=True)]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TechniqueBase(_Strict):
    #: Aceptado y descartado: lo genera el backend. Ver `LegacyClientCode`.
    code: LegacyClientCode = None
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
    #: Aceptado y descartado: lo genera el backend. Ver `LegacyClientCode`.
    code: LegacyClientCode = None
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
    #: Aceptado y descartado: lo genera el backend. Ver `LegacyClientCode`.
    code: LegacyClientCode = None
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
    name: Annotated[str | None, Field(max_length=200)] = None
    customer_id: PositiveStrictInt | None = None
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
    markup_percent: Annotated[Decimal | None, Field(ge=0, max_digits=36, decimal_places=18)] = None
    commercial_sale_unit_price: Money | None = None

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
    name: str | None = None
    customer_id: int | None = None
    customer_name_snapshot: str | None = None
    customer_trade_name_snapshot: str | None = None
    customer_document_type_snapshot: str | None = None
    customer_document_number_snapshot: str | None = None
    customer_address_snapshot: str | None = None
    customer_ubigeo_snapshot: str | None = None
    customer_email_snapshot: str | None = None
    customer_phone_snapshot: str | None = None
    product_id: int
    product_internal_reference: str
    product_name: str
    product_name_snapshot: str | None = None
    product_internal_reference_snapshot: str | None = None
    product_type_snapshot: str | None = None
    product_uom_snapshot: str | None = None
    product_material_snapshot: str | None = None
    product_grammage_snapshot: Decimal | None = None
    product_width_snapshot: Decimal | None = None
    product_height_snapshot: Decimal | None = None
    product_length_snapshot: Decimal | None = None
    product_depth_snapshot: Decimal | None = None
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
    #: Costos internos puros, ganancia y precios comerciales
    final_unit_cost: Decimal = Decimal(0)
    final_total_cost: Decimal = Decimal(0)
    markup_percent: Decimal = Decimal(100)
    target_profit_unit: Decimal = Decimal(0)
    calculated_sale_unit_price: Decimal = Decimal(0)
    suggested_commercial_unit_price: Decimal = Decimal(0)
    commercial_sale_unit_price: Decimal = Decimal(0)
    effective_profit_unit: Decimal = Decimal(0)
    effective_profit_total: Decimal = Decimal(0)
    effective_markup_percent: Decimal = Decimal(0)
    commercial_subtotal: Decimal = Decimal(0)
    commercial_total: Decimal = Decimal(0)
    commercial_unit_price_with_tax: Decimal = Decimal(0)
    currency_code_snapshot: str = "PEN"
    currency_symbol_snapshot: str = "S/"
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
    #: Fase 009E. Los DOS importes de la cotizacion, resueltos por el backend.
    #:
    #: La via heredada dejaba a cada pantalla elegir entre `commercial_*` y
    #: `calculated_*`/`total_with_tax` con un `||`. Cual es el importe real es
    #: una regla comercial: escrita en cada componente, se escribe distinto.
    #: Los campos anteriores siguen en el contrato; el que se muestra es este.
    subtotal: Decimal = Decimal(0)
    total: Decimal = Decimal(0)
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
    name: str | None = None
    status: QuotationStatus
    workflow: QuotationWorkflow = QuotationWorkflow.LEGACY
    customer_id: int | None = None
    customer_name: str | None = None
    customer_document_number: str | None = None
    product_id: int | None = None
    product_internal_reference: str | None = None
    product_name: str
    quantity: int | None = None
    item_count: int = 1
    calculated_unit_price: Decimal
    calculated_total: Decimal
    final_unit_cost: Decimal = Decimal(0)
    commercial_sale_unit_price: Decimal = Decimal(0)
    commercial_total: Decimal = Decimal(0)
    total_with_tax: Decimal
    #: Fase 009E. EL total de la cotizacion, con IGV, venga del Cotizador o de
    #: la via heredada. Lo resuelve el backend.
    #:
    #: Existe porque el frontend estaba eligiendo entre `commercial_total`,
    #: `total_with_tax` y `calculated_total` con una cascada propia, y esa
    #: cascada es una regla comercial: si dos pantallas la escriben distinto,
    #: el mismo pedido vale dos importes. Los tres campos anteriores se
    #: conservan para no romper contratos, pero el que se muestra es este.
    total: Decimal = Decimal(0)
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
