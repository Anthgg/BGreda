"""Logica matematica y validaciones del motor de recetas.

Semantica obligatoria:
- La suma de componentes base (BASE) debe ser exactamente 100%.
- Colorantes (COLORANT) y aditivos (ADDITIVE) se agregan encima del 100% base.
- El factor de rendimiento (yield_factor) se calcula como:
    yield_factor = (base_total + additional_total) / base_total
- Los calculos de costo operan con precision Decimal y conversion exacta entre kg y g.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal

from app.core.errors import APIError
from app.models.recipes import RecipeComponentType

BASE_PERCENTAGE_TARGET = Decimal("100.000000")
PERCENTAGE_TOLERANCE = Decimal("0.000100")
MAX_RECIPE_RECURSION_DEPTH = 10


class RecipeError(APIError):
    """Error base para operaciones de recetas."""

    status_code = 422
    code = "RECIPE_ERROR"
    message = "Error en la definicion de la receta"


class RecipeBasePercentageError(RecipeError):
    code = "RECIPE_INVALID_BASE_PERCENTAGE"
    message = "La suma de los componentes base debe ser exactamente 100%"


class RecipeCycleError(RecipeError):
    code = "RECIPE_CYCLE_DETECTED"
    message = "Se ha detectado una referencia circular en las recetas anidadas"


class RecipeTargetProductError(RecipeError):
    code = "RECIPE_INVALID_TARGET_PRODUCT"
    message = "Solo los productos de tipo PREPARED_MATERIAL pueden tener recetas"


class RecipeComponentProductError(RecipeError):
    code = "RECIPE_INVALID_COMPONENT"
    message = "El producto no es un componente valido para la receta"


class RecipeRecursionLimitError(RecipeError):
    code = "RECIPE_RECURSION_LIMIT_EXCEEDED"
    message = "Se ha superado el limite maximo de profundidad en recetas anidadas"


def validate_recipe_percentages(
    lines: Sequence[tuple[RecipeComponentType, Decimal]],
) -> tuple[Decimal, Decimal, Decimal]:
    """Valida y calcula las proporciones de una receta.

    Devuelve:
        (base_total, additional_total, yield_factor)

    Lanza:
        RecipeBasePercentageError si la suma de componentes BASE no es 100% +- tolerancia.
    """
    if not lines:
        raise RecipeBasePercentageError("La receta debe contener al menos un componente base")

    base_total = Decimal(0)
    additional_total = Decimal(0)

    for comp_type, percentage in lines:
        if percentage <= Decimal(0):
            raise RecipeError("El porcentaje de cada componente debe ser mayor a cero")

        if comp_type == RecipeComponentType.BASE:
            base_total += percentage
        elif comp_type in (RecipeComponentType.COLORANT, RecipeComponentType.ADDITIVE):
            additional_total += percentage
        else:
            raise RecipeError(f"Tipo de componente no reconocido: {comp_type}")

    diff = abs(base_total - BASE_PERCENTAGE_TARGET)
    if diff > PERCENTAGE_TOLERANCE:
        raise RecipeBasePercentageError(
            f"La suma de componentes base debe ser exactamente 100% (actual: {base_total:.4f}%)"
        )

    # yield_factor = (base_total + additional_total) / base_total
    # Si base_total es 100 y additional_total es 6 -> 106 / 100 = 1.06
    yield_factor = (base_total + additional_total) / base_total
    return base_total, additional_total, yield_factor


def compute_recipe_fingerprint(
    lines: Sequence[tuple[int, RecipeComponentType, Decimal, int]],
) -> str:
    """Calcula un hash SHA-256 determinista de la formula.

    Cada tupla es: (component_product_id, component_type, percentage, sort_order)
    """
    items = []
    for item in lines:
        c_type = item[1].value if hasattr(item[1], "value") else str(item[1])
        items.append(f"{item[0]}:{c_type}:{item[2]:.6f}:{item[3]}")
    canonical_items = sorted(items)
    raw_payload = "|".join(canonical_items).encode("utf-8")
    return hashlib.sha256(raw_payload).hexdigest()


def normalize_component_unit_cost_to_grams(
    cost: Decimal | None,
    uom_code: str | None,
) -> Decimal:
    """Convierte el costo unitario del maestro a costo por gramo (g).

    Si el producto esta en kg, 1 kg = 1000 g -> costo / 1000.
    Si el producto esta en g, el costo ya es por gramo.
    Si no tiene costo, devuelve 0.
    """
    if cost is None or cost <= Decimal(0):
        return Decimal(0)

    uom = (uom_code or "").strip().lower()
    if uom in ("kg", "kilo", "kilogramo"):
        return cost / Decimal(1000)
    elif uom in ("g", "gr", "gramo", "gramos"):
        return cost
    return cost
