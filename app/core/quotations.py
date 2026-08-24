"""Calculo puro y exacto del cotizador integral de Fase 005.

Las formulas se reconstruyen de ``Propuesta para cotizar.xlsx``. Este modulo
no conoce HTTP ni SQLAlchemy: recibe snapshots y devuelve importes ``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

ZERO = Decimal(0)


class QuotationCalculationError(ValueError):
    """Una entrada no permite calcular una cotizacion valida."""


class TechniqueFormulaType(StrEnum):
    ONE_FACTOR = "ONE_FACTOR"
    TWO_FACTORS = "TWO_FACTORS"


class AdditionalFormulaType(StrEnum):
    PIECE_QUANTITY = "PIECE_QUANTITY"
    SIMPLE_QUANTITY = "SIMPLE_QUANTITY"
    PIECE_X_ADDITIONAL = "PIECE_X_ADDITIONAL"


class OtherCostCalculationType(StrEnum):
    PER_DAY = "PER_DAY"
    FIXED = "FIXED"
    PER_PIECE = "PER_PIECE"


@dataclass(frozen=True, slots=True)
class TechniqueInput:
    reference_id: int
    name: str
    unit_price: Decimal
    formula_type: TechniqueFormulaType
    factor_1: Decimal
    factor_2: Decimal | None
    quantity: int
    applied_cost: Decimal | None = None
    applied_days: int | None = None


@dataclass(frozen=True, slots=True)
class TechniqueResult:
    source: TechniqueInput
    proposed_cost: Decimal
    applied_cost: Decimal
    proposed_days: int
    applied_days: int


@dataclass(frozen=True, slots=True)
class AdditionalInput:
    reference_id: int
    name: str
    unit_price: Decimal
    formula_type: AdditionalFormulaType
    factor_1: Decimal | None
    total_quantity: int
    additional_quantity: Decimal | None = None
    applied_cost: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AdditionalResult:
    source: AdditionalInput
    proposed_cost: Decimal
    applied_cost: Decimal


@dataclass(frozen=True, slots=True)
class OtherCostInput:
    reference_id: int
    name: str
    unit_price: Decimal
    calculation_type: OtherCostCalculationType


@dataclass(frozen=True, slots=True)
class OtherCostResult:
    source: OtherCostInput
    proposed_cost: Decimal
    applied_cost: Decimal


@dataclass(frozen=True, slots=True)
class QuotationInput:
    quantity: int
    materials_calculated: Decimal
    materials_applied: Decimal
    firing_cost: Decimal
    techniques: tuple[TechniqueInput, ...]
    additionals: tuple[AdditionalInput, ...]
    days_adjustment: int
    waiting_days: int
    other_costs: tuple[OtherCostInput, ...]
    commercial_factor: Decimal
    #: Porcentaje de IGV, no fraccion: 18 significa 18 %. Cero deja el
    #: importe con impuesto igual al neto.
    tax_percentage: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class QuotationResult:
    techniques: tuple[TechniqueResult, ...]
    additionals: tuple[AdditionalResult, ...]
    other_costs: tuple[OtherCostResult, ...]
    materials_calculated: Decimal
    materials_applied: Decimal
    firing_cost: Decimal
    labor_cost: Decimal
    calculated_days: int
    days_adjustment: int
    waiting_days: int
    total_days: int
    space_cost: Decimal
    commercial_factor: Decimal
    base_commercial_cost: Decimal
    #: Precio neto. Es el de la hoja de calculo del negocio y el que manda:
    #: el impuesto se anade encima, nunca se disuelve dentro.
    calculated_total: Decimal
    calculated_unit_price: Decimal
    tax_percentage: Decimal
    tax_amount: Decimal
    total_with_tax: Decimal
    unit_price_with_tax: Decimal


def _require_non_negative(value: Decimal, label: str) -> None:
    if value < ZERO:
        raise QuotationCalculationError(f"{label} no puede ser negativo")


def ceil_units(quantity: Decimal | int, factor: Decimal) -> int:
    """Division entera superior sin pasar por ``float``."""
    if Decimal(quantity) < ZERO:
        raise QuotationCalculationError("La cantidad no puede ser negativa")
    if factor <= ZERO:
        raise QuotationCalculationError("El factor debe ser mayor que cero")
    return int((Decimal(quantity) / factor).to_integral_value(rounding=ROUND_CEILING))


def calculate_technique(item: TechniqueInput) -> TechniqueResult:
    if item.quantity <= 0:
        raise QuotationCalculationError("La cantidad de tecnica debe ser mayor que cero")
    _require_non_negative(item.unit_price, "El precio de la tecnica")

    units_a = ceil_units(item.quantity, item.factor_1)
    units_b = 0
    if item.formula_type is TechniqueFormulaType.TWO_FACTORS:
        if item.factor_2 is None:
            raise QuotationCalculationError("La tecnica de dos factores requiere factor 2")
        units_b = ceil_units(item.quantity, item.factor_2)
    elif item.factor_2 is not None:
        raise QuotationCalculationError("La tecnica de un factor no admite factor 2")

    proposed_days = units_a + units_b
    proposed_cost = item.unit_price * Decimal(proposed_days)
    applied_cost = item.applied_cost if item.applied_cost is not None else proposed_cost
    applied_days = item.applied_days if item.applied_days is not None else proposed_days
    _require_non_negative(applied_cost, "El costo aplicado de la tecnica")
    if applied_days < 0:
        raise QuotationCalculationError("Los dias aplicados no pueden ser negativos")
    return TechniqueResult(item, proposed_cost, applied_cost, proposed_days, applied_days)


def calculate_additional(item: AdditionalInput) -> AdditionalResult:
    if item.total_quantity <= 0:
        raise QuotationCalculationError("La cantidad total debe ser mayor que cero")
    _require_non_negative(item.unit_price, "El precio del adicional")

    if item.formula_type is AdditionalFormulaType.SIMPLE_QUANTITY:
        if item.additional_quantity is None or item.additional_quantity < ZERO:
            raise QuotationCalculationError("El adicional simple requiere una cantidad no negativa")
        proposed_cost = item.unit_price * item.additional_quantity
    else:
        if item.factor_1 is None:
            raise QuotationCalculationError("El adicional por piezas requiere factor 1")
        piece_units = ceil_units(item.total_quantity, item.factor_1)
        multiplier = Decimal(piece_units)
        if item.formula_type is AdditionalFormulaType.PIECE_X_ADDITIONAL:
            if item.additional_quantity is None or item.additional_quantity < ZERO:
                raise QuotationCalculationError(
                    "El adicional por piezas y cantidad requiere una cantidad no negativa"
                )
            multiplier *= item.additional_quantity
        proposed_cost = item.unit_price * multiplier

    applied_cost = item.applied_cost if item.applied_cost is not None else proposed_cost
    _require_non_negative(applied_cost, "El costo aplicado del adicional")
    return AdditionalResult(item, proposed_cost, applied_cost)


def calculate_other_cost(
    item: OtherCostInput, *, quantity: int, total_days: int
) -> OtherCostResult:
    _require_non_negative(item.unit_price, "El valor de otros gastos")
    if item.calculation_type is OtherCostCalculationType.PER_DAY:
        proposed = item.unit_price * Decimal(total_days)
    elif item.calculation_type is OtherCostCalculationType.PER_PIECE:
        proposed = item.unit_price * Decimal(quantity)
    else:
        proposed = item.unit_price
    return OtherCostResult(item, proposed, proposed)


def calculate_quotation(payload: QuotationInput) -> QuotationResult:
    if payload.quantity <= 0:
        raise QuotationCalculationError("La cantidad debe ser un entero mayor que cero")
    _require_non_negative(payload.materials_calculated, "El costo calculado de materiales")
    _require_non_negative(payload.materials_applied, "El costo aplicado de materiales")
    _require_non_negative(payload.firing_cost, "El costo de quema")
    if payload.waiting_days < 0:
        raise QuotationCalculationError("Los dias de espera no pueden ser negativos")
    if payload.commercial_factor <= ZERO:
        raise QuotationCalculationError("El factor comercial debe ser mayor que cero")

    techniques = tuple(calculate_technique(item) for item in payload.techniques)
    additionals = tuple(calculate_additional(item) for item in payload.additionals)
    labor_cost = sum((item.applied_cost for item in techniques), ZERO) + sum(
        (item.applied_cost for item in additionals), ZERO
    )
    calculated_days = sum(item.applied_days for item in techniques)
    total_days = calculated_days + payload.days_adjustment + payload.waiting_days
    if total_days < 0:
        raise QuotationCalculationError("El total de dias no puede ser negativo")

    other_costs = tuple(
        calculate_other_cost(item, quantity=payload.quantity, total_days=total_days)
        for item in payload.other_costs
    )
    space_cost = sum((item.applied_cost for item in other_costs), ZERO)
    base_commercial_cost = payload.materials_applied + payload.firing_cost + labor_cost
    calculated_total = base_commercial_cost * payload.commercial_factor + space_cost
    calculated_unit_price = calculated_total / Decimal(payload.quantity)

    # El IGV se suma sobre el precio neto: la cotizacion se emite sin impuesto y
    # el documento que se entrega muestra las dos cifras. Por eso no entra en la
    # formula comercial ni se multiplica por el factor.
    if payload.tax_percentage < ZERO:
        raise QuotationCalculationError("El porcentaje de IGV no puede ser negativo")
    tax_amount = calculated_total * payload.tax_percentage / Decimal(100)
    total_with_tax = calculated_total + tax_amount
    unit_price_with_tax = total_with_tax / Decimal(payload.quantity)

    return QuotationResult(
        techniques=techniques,
        additionals=additionals,
        other_costs=other_costs,
        materials_calculated=payload.materials_calculated,
        materials_applied=payload.materials_applied,
        firing_cost=payload.firing_cost,
        labor_cost=labor_cost,
        calculated_days=calculated_days,
        days_adjustment=payload.days_adjustment,
        waiting_days=payload.waiting_days,
        total_days=total_days,
        space_cost=space_cost,
        commercial_factor=payload.commercial_factor,
        base_commercial_cost=base_commercial_cost,
        calculated_total=calculated_total,
        calculated_unit_price=calculated_unit_price,
        tax_percentage=payload.tax_percentage,
        tax_amount=tax_amount,
        total_with_tax=total_with_tax,
        unit_price_with_tax=unit_price_with_tax,
    )
