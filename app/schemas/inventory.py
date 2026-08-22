"""Contratos de inventario: ubicaciones, saldos y movimientos."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.inventory import MovementType


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StockLocationOut(_Out):
    id: int
    name: str
    active: bool


class StockLocationCreate(_In):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    active: bool = True


class StockBalanceOut(BaseModel):
    product_id: int
    internal_reference: str
    product_name: str
    location_id: int
    location_name: str
    uom_code: str | None
    quantity: Decimal


class StockBalancePage(BaseModel):
    items: list[StockBalanceOut]
    total: int
    limit: int
    offset: int


class StockMovementOut(BaseModel):
    id: int
    product_id: int
    internal_reference: str
    product_name: str
    location_id: int
    location_name: str
    movement_type: MovementType
    quantity: Decimal
    balance_after: Decimal
    uom_code: str
    reason: str | None
    import_batch_id: int | None
    created_by: uuid.UUID | None
    created_by_name: str | None
    created_at: datetime


class StockMovementPage(BaseModel):
    items: list[StockMovementOut]
    total: int
    limit: int
    offset: int


class StockAdjustmentCreate(_In):
    """Ajuste manual de existencia.

    No existe un endpoint para escribir el saldo directamente: se declara el
    delta y el backend genera el movimiento que lo respalda.
    """

    product_id: int
    location_id: int
    quantity: Decimal = Field(description="Delta con signo. Negativo descuenta.")
    reason: Annotated[str, Field(min_length=3, max_length=240)]

    @field_validator("quantity", mode="after")
    @classmethod
    def _not_zero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("Un ajuste de cero no es un movimiento")
        return value
