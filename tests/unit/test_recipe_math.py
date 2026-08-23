"""Pruebas unitarias para la logica matematica de recetas y rendimientos."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.recipes import (
    RecipeBasePercentageError,
    RecipeError,
    compute_recipe_fingerprint,
    normalize_component_unit_cost_to_grams,
    validate_recipe_percentages,
)
from app.models.recipes import RecipeComponentType
from app.schemas.recipes import RecipeCalculateIn


class TestRecipeMath:
    def test_base_exacta_100_sin_adicionales(self) -> None:
        lines = [
            (RecipeComponentType.BASE, Decimal("60.000000")),
            (RecipeComponentType.BASE, Decimal("40.000000")),
        ]
        base_total, add_total, yield_factor = validate_recipe_percentages(lines)
        assert base_total == Decimal("100.000000")
        assert add_total == Decimal("0.000000")
        assert yield_factor == Decimal("1.000000")

    def test_base_con_colorante_y_aditivo_encima_del_100(self) -> None:
        lines = [
            (RecipeComponentType.BASE, Decimal("50.000000")),
            (RecipeComponentType.BASE, Decimal("50.000000")),
            (RecipeComponentType.COLORANT, Decimal("6.000000")),
            (RecipeComponentType.ADDITIVE, Decimal("2.000000")),
        ]
        base_total, add_total, yield_factor = validate_recipe_percentages(lines)
        assert base_total == Decimal("100.000000")
        assert add_total == Decimal("8.000000")
        assert yield_factor == Decimal("1.080000")

    def test_base_menor_a_100_falla(self) -> None:
        lines = [
            (RecipeComponentType.BASE, Decimal("95.000000")),
            (RecipeComponentType.COLORANT, Decimal("6.000000")),
        ]
        with pytest.raises(RecipeBasePercentageError) as exc_info:
            validate_recipe_percentages(lines)
        assert "La suma de componentes base debe ser exactamente 100%" in str(
            exc_info.value.message
        )

    def test_base_mayor_a_100_falla(self) -> None:
        lines = [
            (RecipeComponentType.BASE, Decimal("105.000000")),
        ]
        with pytest.raises(RecipeBasePercentageError):
            validate_recipe_percentages(lines)

    def test_tolerancia_decimal_de_validacion(self) -> None:
        # 100.00005 esta dentro de la tolerancia de 0.000100
        lines_ok = [(RecipeComponentType.BASE, Decimal("100.000050"))]
        base, _, _ = validate_recipe_percentages(lines_ok)
        assert abs(base - Decimal("100")) < Decimal("0.0001")

        # 100.001 excede la tolerancia
        lines_fail = [(RecipeComponentType.BASE, Decimal("100.001000"))]
        with pytest.raises(RecipeBasePercentageError):
            validate_recipe_percentages(lines_fail)

    def test_porcentaje_cero_o_negativo_falla(self) -> None:
        lines = [
            (RecipeComponentType.BASE, Decimal("100.000000")),
            (RecipeComponentType.COLORANT, Decimal("0.000000")),
        ]
        with pytest.raises(RecipeError):
            validate_recipe_percentages(lines)

    def test_fingerprint_es_deterministico_e_independiente_del_orden(self) -> None:
        lines_a = [
            (10, RecipeComponentType.BASE, Decimal("60.000000"), 0),
            (20, RecipeComponentType.BASE, Decimal("40.000000"), 1),
            (30, RecipeComponentType.COLORANT, Decimal("5.000000"), 2),
        ]
        lines_b = [
            (30, RecipeComponentType.COLORANT, Decimal("5.000000"), 2),
            (20, RecipeComponentType.BASE, Decimal("40.000000"), 1),
            (10, RecipeComponentType.BASE, Decimal("60.000000"), 0),
        ]
        fp_a = compute_recipe_fingerprint(lines_a)
        fp_b = compute_recipe_fingerprint(lines_b)
        assert fp_a == fp_b

        # Un cambio en porcentaje cambia el fingerprint
        lines_c = [
            (10, RecipeComponentType.BASE, Decimal("60.000000"), 0),
            (20, RecipeComponentType.BASE, Decimal("40.000000"), 1),
            (30, RecipeComponentType.COLORANT, Decimal("6.000000"), 2),
        ]
        assert compute_recipe_fingerprint(lines_c) != fp_a

    def test_conversion_exacta_kg_a_g(self) -> None:
        # S/ 25 por kg -> S/ 0.025 por g
        cost_g = normalize_component_unit_cost_to_grams(Decimal("25.000000"), "kg")
        assert cost_g == Decimal("0.025")

        # S/ 0.05 por g -> S/ 0.05 por g
        cost_g2 = normalize_component_unit_cost_to_grams(Decimal("0.050000"), "g")
        assert cost_g2 == Decimal("0.05")

        # None -> Decimal(0)
        assert normalize_component_unit_cost_to_grams(None, "kg") == Decimal(0)

    def test_recipe_calculate_in_target_uom_restriction(self) -> None:
        # Default es "g"
        calc_default = RecipeCalculateIn(target_base_quantity=Decimal("1000.0"))
        assert calc_default.target_uom == "g"

        # Explicit "g" es valido
        calc_g = RecipeCalculateIn(target_base_quantity=Decimal("1000.0"), target_uom="g")
        assert calc_g.target_uom == "g"

        # kg o unit son rechazados con error de validacion
        with pytest.raises(ValidationError):
            RecipeCalculateIn(target_base_quantity=Decimal("1000.0"), target_uom="kg")  # type: ignore[arg-type]

        with pytest.raises(ValidationError):
            RecipeCalculateIn(target_base_quantity=Decimal("1000.0"), target_uom="unit")  # type: ignore[arg-type]
