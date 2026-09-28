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


class StockLotOut(BaseModel):
    preparation_id: int
    preparation_code: str
    product_id: int
    location_id: int
    quantity: Decimal
    uom_code: str
    prepared_at: datetime
    solids_g_per_ml: Decimal


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
    preparation_id: int | None = None
    source_preparation_id: int | None = None
    production_order_id: int | None = None
    prototype_id: int | None = None
    v2_quotation_id: int | None = None
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
    preparation_id: int | None = Field(default=None, gt=0)
    quantity: Decimal = Field(description="Delta con signo. Negativo descuenta.")
    reason: Annotated[str, Field(min_length=3, max_length=240)]

    @field_validator("quantity", mode="after")
    @classmethod
    def _not_zero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("Un ajuste de cero no es un movimiento")
        return value


class StockDeliveryCreate(_In):
    """Salida real de producto terminado entregado al cliente."""

    product_id: int = Field(gt=0)
    location_id: int = Field(gt=0)
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    v2_quotation_id: int | None = Field(default=None, gt=0)
    production_order_id: int | None = Field(default=None, gt=0)
    reason: Annotated[str | None, Field(max_length=240)] = None
