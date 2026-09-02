"""Inventario: ubicaciones, saldos y movimientos.

El saldo no se puede mover "magicamente": cada cambio de ``stock_balances``
nace de una fila en ``stock_movements``. El balance es una vista materializada
del historial, no una celda editable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.precision import stock_quantity_numeric
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType


class MovementType(StrEnum):
    """Origen del movimiento.

    ``INITIAL_IMPORT`` es la carga del maestro; existe como tipo propio para
    que el primer saldo tenga la misma trazabilidad que cualquier ajuste
    posterior y se distinga de una correccion manual.
    """

    INITIAL_IMPORT = "INITIAL_IMPORT"
    ADJUSTMENT = "ADJUSTMENT"
    IN = "IN"
    OUT = "OUT"
    #: Fase 009D. Preparar una receta es una transformacion fisica: consume
    #: materia prima y produce material preparado. Se distinguen de IN/OUT
    #: genericos para poder auditar la transformacion como un hecho propio, y
    #: llevan `preparation_id` para saber a que lote pertenecen.
    PREPARATION_OUT = "PREPARATION_OUT"
    PREPARATION_IN = "PREPARATION_IN"
    #: Fase 009I. Consumo de material preparado al arrancar una orden de
    #: produccion. Tipo propio y no `OUT` generico: una salida de produccion se
    #: audita distinto de una merma o de un traslado, y mezclarlas obligaria a
    #: adivinar despues cual fue cual.
    PRODUCTION_OUT = "PRODUCTION_OUT"


class StockLocation(Base, TimestampMixin):
    """Lugar fisico donde se guarda existencia."""

    __tablename__ = "stock_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),)


class StockBalance(Base, TimestampMixin):
    """Saldo vigente de un producto en una ubicacion.

    La cantidad se expresa siempre en la unidad base del producto. El check de
    no negatividad es la politica por defecto de Fase 3: una operacion normal
    no puede dejar existencia negativa.
    """

    __tablename__ = "stock_balances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    location_id: Mapped[int] = mapped_column(
        ForeignKey("stock_locations.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(
        stock_quantity_numeric(), nullable=False, server_default=text("0")
    )

    __table_args__ = (
        UniqueConstraint("product_id", "location_id", name="uq_stock_balances_product_id"),
        CheckConstraint("quantity >= 0", name="quantity_not_negative"),
        Index("ix_stock_balances_location", "location_id"),
    )


class StockMovement(Base):
    """Evidencia inmutable de un cambio de existencia.

    ``quantity`` lleva signo: positivo suma, negativo resta. Se guarda tambien
    el saldo resultante para poder auditar la secuencia sin recalcular toda la
    historia.
    """

    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    location_id: Mapped[int] = mapped_column(
        ForeignKey("stock_locations.id", ondelete="RESTRICT"), nullable=False
    )
    movement_type: Mapped[MovementType] = mapped_column(
        StrEnumType(MovementType, 24), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(stock_quantity_numeric(), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(stock_quantity_numeric(), nullable=False)
    uom_code: Mapped[str] = mapped_column(
        ForeignKey("units_of_measure.code", ondelete="RESTRICT"), nullable=False
    )

    #: Lote de preparacion que origino el movimiento (Fase 009D). Nulo en
    #: compras, ajustes y cargas iniciales.
    preparation_id: Mapped[int | None] = mapped_column(
        ForeignKey("recipe_preparations.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: Orden de produccion que origino el movimiento (Fase 009I). Nula en
    #: compras, ajustes, cargas iniciales y preparaciones.
    production_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("production_orders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batches.id", ondelete="RESTRICT")
    )
    reason: Mapped[str | None] = mapped_column(String(240))

    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by_name: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("quantity <> 0", name="quantity_not_zero"),
        CheckConstraint("balance_after >= 0", name="balance_after_not_negative"),
        CheckConstraint(
            "movement_type IN ('INITIAL_IMPORT', 'ADJUSTMENT', 'IN', 'OUT', "
            "'PREPARATION_OUT', 'PREPARATION_IN', 'PRODUCTION_OUT')",
            name="movement_type_allowed",
        ),
        Index("ix_stock_movements_product_created", "product_id", "created_at"),
        Index("ix_stock_movements_batch", "import_batch_id"),
    )
