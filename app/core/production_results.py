"""Reglas puras para cerrar una línea de producción."""

from decimal import Decimal


def result_matches_started_quantity(
    *,
    started_quantity: Decimal,
    good_quantity: Decimal,
    scrap_quantity: Decimal,
    scrap_reason: str | None,
    require_scrap_reason: bool = True,
) -> bool:
    """El resultado da cuenta de cada unidad iniciada y explica toda merma."""
    if min(started_quantity, good_quantity, scrap_quantity) < 0:
        return False
    if good_quantity + scrap_quantity != started_quantity:
        return False
    return not require_scrap_reason or scrap_quantity == 0 or bool((scrap_reason or "").strip())
