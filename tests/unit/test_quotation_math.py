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


@pytest.mark.parametrize(
    ("input_val", "expected"),
    [
        (Decimal("8.00"), Decimal("8.00")),
        (Decimal("8.10"), Decimal("8.00")),
        (Decimal("8.24"), Decimal("8.00")),
        (Decimal("8.25"), Decimal("8.50")),
        (Decimal("8.30"), Decimal("8.50")),
        (Decimal("8.49"), Decimal("8.50")),
        (Decimal("8.50"), Decimal("8.50")),
        (Decimal("8.60"), Decimal("8.50")),
        (Decimal("8.74"), Decimal("8.50")),
        (Decimal("8.75"), Decimal("9.00")),
        (Decimal("8.90"), Decimal("9.00")),
        (Decimal("9.00"), Decimal("9.00")),
    ],
)
def test_round_to_commercial_half_exact_cases(input_val: Decimal, expected: Decimal) -> None:
    from app.core.quotations import round_to_commercial_half

    assert round_to_commercial_half(input_val) == expected


def test_costing_markup_and_effective_profit_calculations() -> None:
    # 10 piezas, costo de materiales = 100, quema = 50, mano de obra = 0, espacio = 50
    # Total costo interno = 200 -> Costo unitario = 20
    # Markup deseado = 50%
    # Ganancia objetivo unitaria = 20 * 50% = 10
    # Precio calculado = 20 + 10 = 30 -> redondeo sugerido = 30.00
    # Si usuario fija precio manual comercial en 35.00:
    # Ganancia efectiva unit = 35 - 20 = 15
    # Ganancia efectiva total = 15 * 10 = 150
    # Markup efectivo = 15 / 20 * 100 = 75%
    # Subtotal comercial = 35 * 10 = 350
    # Con IGV 18% = 350 * 1.18 = 413
    result = calculate_quotation(
        QuotationInput(
            quantity=10,
            materials_calculated=Decimal("100"),
            materials_applied=Decimal("100"),
            firing_cost=Decimal("50"),
            techniques=(),
            additionals=(),
            days_adjustment=0,
            waiting_days=0,
            other_costs=(OtherCostInput(1, "Fijo", Decimal("50"), OtherCostCalculationType.FIXED),),
            markup_percent=Decimal("50"),
            manual_commercial_unit_price=Decimal("35.00"),
            tax_percentage=Decimal("18"),
        )
    )

    assert result.final_total_cost == Decimal("200")
    assert result.final_unit_cost == Decimal("20")
    assert result.markup_percent == Decimal("50")
    assert result.target_profit_unit == Decimal("10")
    assert result.calculated_sale_unit_price == Decimal("30")
    assert result.suggested_commercial_unit_price == Decimal("30.00")
    assert result.commercial_sale_unit_price == Decimal("35.00")
    assert result.effective_profit_unit == Decimal("15.00")
    assert result.effective_profit_total == Decimal("150.00")
    assert result.effective_markup_percent == Decimal("75")
    assert result.commercial_subtotal == Decimal("350.00")
    assert result.commercial_total == Decimal("413.00")
    assert result.commercial_unit_price_with_tax == Decimal("41.30")


def test_control_case_validated_20_pieces() -> None:
    """Caso manual validado CTZ-2026-000010:

    20 piezas, costo total S/ 3886.24 (costo unitario S/ 194.312),
    markup 100%, precio calculado S/ 388.624 -> precio comercial S/ 388.50,
    subtotal S/ 7770.00, IGV 18% S/ 1398.60, total S/ 9168.60,
    precio unitario con IGV S/ 458.43.
    """
    result = calculate_quotation(
        QuotationInput(
            quantity=20,
            materials_calculated=Decimal("0"),
            materials_applied=Decimal("0"),
            firing_cost=Decimal("3886.24"),
            techniques=(),
            additionals=(),
            days_adjustment=0,
            waiting_days=0,
            other_costs=(),
            markup_percent=Decimal("100"),
            tax_percentage=Decimal("18"),
        )
    )

    assert result.final_total_cost == Decimal("3886.24")
    assert result.final_unit_cost.quantize(Decimal("0.01")) == Decimal("194.31")
    assert result.markup_percent == Decimal("100")
    assert result.suggested_commercial_unit_price == Decimal("388.50")
    assert result.commercial_sale_unit_price == Decimal("388.50")
    assert result.commercial_subtotal == Decimal("7770.00")
    assert result.commercial_total - result.commercial_subtotal == Decimal("1398.60")
    assert result.commercial_total == Decimal("9168.60")
    assert result.commercial_unit_price_with_tax == Decimal("458.43")
