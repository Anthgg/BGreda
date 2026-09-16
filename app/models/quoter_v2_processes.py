"""Procesos de una pieza y adicionales de una cotizacion (correccion 010H).

## Por que el proceso no es la tarea

Una taza necesita torno, asa y acabado **antes** de que nadie decida quien los
hace. Eso es el proceso: la tecnica, las piezas afectadas y de donde salio la
decision. La tarea —`V2QuotationLabor`, sin cambios— aparece cuando se asigna a
una persona, y entonces si congela jornal, jornada y tarifa.

Separarlos es lo que permite que la pantalla muestre «Torno · 20 piezas · 3.2 h»
con el trabajador todavia vacio, sin inventarse una tarifa de nadie y sin que el
reparto de costos, la duplicacion o el PDF tengan que aprender a vivir con
tareas a medio llenar.

## Por que el maestro cuelga del producto de CATALOGO

Porque es lo unico que se repite entre cotizaciones. Una pieza a medida, que no
esta en el catalogo, elige sus procesos en su propia cotizacion y ahi se quedan:
nadie va a volver a cotizar exactamente esa pieza.

## Adicionales

El empaque especial no es una tecnica ni un material: no tiene rendimiento ni se
consume del inventario. Es un costo que alguien decide, con su cantidad y su
precio, como en la hoja «Cotizador V2» del Excel aprobado. Vive aparte para que
nadie lo cobre dos veces.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import money_numeric, quantity_numeric, unit_cost_numeric
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType

if TYPE_CHECKING:  # pragma: no cover
    from app.models.quoter_v2_labor import V2Technique


class V2ProcessOrigin(StrEnum):
    """De donde salio el proceso."""

    #: Lo pedia la pieza del catalogo.
    PRODUCT = "PRODUCT"
    #: Lo anadio quien cotiza, solo para esta cotizacion.
    MANUAL = "MANUAL"


class V2ProductTechnique(Base, TimestampMixin):
    """Una tecnica que una pieza del catalogo necesita."""

    __tablename__ = "v2_product_techniques"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    technique_id: Mapped[int] = mapped_column(
        ForeignKey("v2_techniques.id", ondelete="RESTRICT"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        UniqueConstraint("product_id", "technique_id", name="uq_v2_product_techniques_pair"),
        Index("ix_v2_product_techniques_product", "product_id"),
    )


class V2QuotationProcess(Base, TimestampMixin):
    """Un proceso que esta cotizacion le hace a una de sus lineas."""

    __tablename__ = "v2_quotation_processes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    v2_quotation_id: Mapped[int] = mapped_column(
        ForeignKey("v2_quotations.id", ondelete="CASCADE"), nullable=False
    )
    #: Un proceso es de una pieza. Lo que apoya al pedido entero es personal
    #: adicional, y eso es una tarea, no un proceso.
    v2_quotation_product_id: Mapped[int] = mapped_column(
        ForeignKey("v2_quotation_products.id", ondelete="CASCADE"), nullable=False
    )
    technique_id: Mapped[int] = mapped_column(
        ForeignKey("v2_techniques.id", ondelete="RESTRICT"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    origin: Mapped[V2ProcessOrigin] = mapped_column(
        StrEnumType(V2ProcessOrigin, 16), nullable=False, server_default=text("'PRODUCT'")
    )
    quantity: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    #: Piezas escritas a mano. Cambiar la cantidad del producto ya no las pisa:
    #: si alguien dijo que el logo va en 5 de las 20 tazas, sigue en 5.
    quantity_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: Quitado en ESTA cotizacion. La fila se queda para que la proxima
    #: regeneracion no lo devuelva como si nadie lo hubiera decidido.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    technique: Mapped[V2Technique] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_v2_quotation_processes_quantity"),
        CheckConstraint("origin IN ('PRODUCT', 'MANUAL')", name="ck_v2_quotation_processes_origin"),
        UniqueConstraint(
            "v2_quotation_product_id", "technique_id", name="uq_v2_quotation_processes_pair"
        ),
        Index("ix_v2_quotation_processes_quotation", "v2_quotation_id"),
    )

    @property
    def is_active(self) -> bool:
        return self.removed_at is None


class V2Extra(Base, TimestampMixin):
    """Un concepto adicional del maestro: empaque especial, molde, sello..."""

    __tablename__ = "v2_extras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'servicio'"))
    #: Lo que suele costar. La cotizacion puede pagar otro precio y lo dice.
    unit_cost: Mapped[Decimal] = mapped_column(
        unit_cost_numeric(), nullable=False, server_default=text("0")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    notes: Mapped[str | None] = mapped_column(Text())
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __table_args__ = (
        CheckConstraint("unit_cost >= 0", name="ck_v2_extras_unit_cost"),
        UniqueConstraint("name", name="uq_v2_extras_name"),
    )


class V2QuotationExtra(Base, TimestampMixin):
    """Un adicional de una cotizacion, ya congelado."""

    __tablename__ = "v2_quotation_extras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    v2_quotation_id: Mapped[int] = mapped_column(
        ForeignKey("v2_quotations.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL: es del pedido entero, como en el Excel.
    v2_quotation_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("v2_quotation_products.id", ondelete="CASCADE")
    )
    v2_extra_id: Mapped[int] = mapped_column(
        ForeignKey("v2_extras.id", ondelete="RESTRICT"), nullable=False
    )
    name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_snapshot: Mapped[str] = mapped_column(String(32), nullable=False)
    unit_cost_snapshot: Mapped[Decimal] = mapped_column(unit_cost_numeric(), nullable=False)
    unit_cost_is_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    description: Mapped[str | None] = mapped_column(Text())
    quantity: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    total_cost: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, server_default=text("0")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_v2_quotation_extras_quantity"),
        CheckConstraint("unit_cost_snapshot >= 0", name="ck_v2_quotation_extras_unit_cost"),
        Index("ix_v2_quotation_extras_quotation", "v2_quotation_id"),
    )
