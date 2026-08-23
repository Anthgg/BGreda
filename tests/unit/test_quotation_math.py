from decimal import Decimal

import pytest

from app.core.quotations import (
    AdditionalFormulaType,
    AdditionalInput,
    OtherCostCalculationType,
    OtherCostInput,
    QuotationCalculationError,
    QuotationInput,
    TechniqueFormulaType,
    TechniqueInput,
    calculate_additional,
    calculate_quotation,
    calculate_technique,
    ceil_units,
)


def test_ceil_respects_boundaries_without_float() -> None:
    assert ceil_units(50, Decimal("50")) == 1
    assert ceil_units(51, Decimal("50")) == 2
    assert ceil_units(1, Decimal("0.1")) == 10


def test_technique_formulas_and_applied_values_are_kept_separate() -> None:
    one = calculate_technique(
        TechniqueInput(
            1,
            "A mano",
            Decimal("110"),
            TechniqueFormulaType.ONE_FACTOR,
            Decimal("15"),
            None,
            16,
            applied_cost=Decimal("175.25"),
            applied_days=4,
        )
    )
    two = calculate_technique(
        TechniqueInput(
            2,
            "Torno",
            Decimal("220"),
            TechniqueFormulaType.TWO_FACTORS,
            Decimal("50"),
            Decimal("100"),
            101,
        )
    )

    assert (one.proposed_days, one.proposed_cost) == (2, Decimal("220"))
    assert (one.applied_days, one.applied_cost) == (4, Decimal("175.25"))
    assert (two.proposed_days, two.proposed_cost) == (5, Decimal("1100"))


@pytest.mark.parametrize(
    ("formula", "factor", "additional_quantity", "expected"),
    [
        (AdditionalFormulaType.PIECE_QUANTITY, Decimal("50"), None, Decimal("220")),
        (AdditionalFormulaType.SIMPLE_QUANTITY, None, Decimal("2.5"), Decimal("275")),
        (
            AdditionalFormulaType.PIECE_X_ADDITIONAL,
            Decimal("50"),
            Decimal("1.5"),
            Decimal("330"),
        ),
    ],
)
def test_additional_formulas(
    formula: AdditionalFormulaType,
    factor: Decimal | None,
    additional_quantity: Decimal | None,
    expected: Decimal,
) -> None:
    result = calculate_additional(
        AdditionalInput(
            1,
            "Adicional",
            Decimal("110"),
            formula,
            factor,
            51,
            additional_quantity,
        )
    )

    assert result.proposed_cost == expected
    assert result.applied_cost == expected


def test_excel_reference_case_is_reproduced_exactly() -> None:
    techniques = (
        TechniqueInput(
            1, "A mano", Decimal("110"), TechniqueFormulaType.ONE_FACTOR, Decimal("15"), None, 10
        ),
        TechniqueInput(
            2,
            "Piezas en torno facil",
            Decimal("220"),
            TechniqueFormulaType.TWO_FACTORS,
            Decimal("50"),
            Decimal("100"),
            2,
        ),
        TechniqueInput(
            3,
            "Piezas en torno dificil",
            Decimal("220"),
            TechniqueFormulaType.TWO_FACTORS,
            Decimal("25"),
            Decimal("100"),
            1,
        ),
        TechniqueInput(
            4, "Colada", Decimal("220"), TechniqueFormulaType.ONE_FACTOR, Decimal("100"), None, 3
        ),
    )
    additionals = (
        AdditionalInput(
            1,
            "Armado de asa",
            Decimal("110"),
            AdditionalFormulaType.PIECE_QUANTITY,
            Decimal("50"),
            19,
        ),
        AdditionalInput(
            2,
            "Numero de moldes",
            Decimal("250"),
            AdditionalFormulaType.SIMPLE_QUANTITY,
            None,
            19,
            Decimal("0.5"),
        ),
        AdditionalInput(
            3,
            "Vidriado por inmersion",
            Decimal("110"),
            AdditionalFormulaType.PIECE_X_ADDITIONAL,
            Decimal("50"),
            19,
            Decimal("1"),
        ),
        AdditionalInput(
            4,
            "Vidriado por aspersion",
            Decimal("110"),
            AdditionalFormulaType.PIECE_X_ADDITIONAL,
            Decimal("50"),
            19,
            Decimal("1"),
        ),
        AdditionalInput(
            5,
            "Vidriado a mano alzada",
            Decimal("110"),
            AdditionalFormulaType.PIECE_X_ADDITIONAL,
            Decimal("50"),
            19,
            Decimal("1"),
        ),
        AdditionalInput(
            6,
            "Con ilustracion",
            Decimal("110"),
            AdditionalFormulaType.PIECE_QUANTITY,
            Decimal("50"),
            19,
        ),
    )
    other_costs = (
        OtherCostInput(1, "Alquiler", Decimal("110"), OtherCostCalculationType.PER_DAY),
        OtherCostInput(2, "Servicios", Decimal("10"), OtherCostCalculationType.PER_DAY),
        OtherCostInput(3, "Administrativo", Decimal("200"), OtherCostCalculationType.FIXED),
        OtherCostInput(4, "Factor", Decimal("3"), OtherCostCalculationType.PER_PIECE),
    )
    result = calculate_quotation(
        QuotationInput(
            quantity=19,
            materials_calculated=Decimal("11.58"),
            materials_applied=Decimal("11.58"),
            firing_cost=Decimal("1249.6608996539792"),
            techniques=techniques,
            additionals=additionals,
            days_adjustment=-5,
            waiting_days=3,
            other_costs=other_costs,
            commercial_factor=Decimal("2"),
        )
    )

    assert result.labor_cost == Decimal("1885")
    assert result.calculated_days == 6
    assert result.total_days == 4
    assert result.space_cost == Decimal("737")
    assert result.base_commercial_cost == Decimal("3146.2408996539792")
    assert result.calculated_total == Decimal("7029.4817993079584")
    assert result.calculated_unit_price == Decimal("369.9727262793662315789473684")


def test_space_cost_is_not_multiplied_by_commercial_factor() -> None:
    result = calculate_quotation(
        QuotationInput(
            quantity=1,
            materials_calculated=Decimal("10"),
            materials_applied=Decimal("10"),
            firing_cost=Decimal("0"),
            techniques=(),
            additionals=(),
            days_adjustment=0,
            waiting_days=0,
            other_costs=(OtherCostInput(1, "Fijo", Decimal("5"), OtherCostCalculationType.FIXED),),
            commercial_factor=Decimal("2"),
        )
    )
    assert result.calculated_total == Decimal("25")


def test_negative_total_days_is_rejected() -> None:
    with pytest.raises(QuotationCalculationError, match="total de dias"):
        calculate_quotation(
            QuotationInput(
                quantity=1,
                materials_calculated=Decimal(0),
                materials_applied=Decimal(0),
                firing_cost=Decimal(0),
                techniques=(),
                additionals=(),
                days_adjustment=-1,
                waiting_days=0,
                other_costs=(),
                commercial_factor=Decimal(1),
            )
        )
