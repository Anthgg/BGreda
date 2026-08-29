"""Modelos de hornos, tarifas, factores de ocupacion y hojas de quema.

## De donde sale la regla

La fuente funcional es la hoja «Costo de quema» del libro *Propuesta para
cotizar*. Conviene dejar escrito lo que ese documento decide, porque no es
evidente:

- El **reparto** del costo de un horno entre las piezas es proporcional al
  volumen: cada linea absorbe ``volumen_linea / volumen_total`` de la tarifa.
  El denominador es el volumen de **toda la hoja**, no el de una sesion.
- La **ocupacion** que determina el factor es la de *cada linea contra la
  capacidad del horno elegido* (``volumen_linea / capacidad``), no la del lote.
  Esa es la economia del modelo: quien ocupa poco horno paga un multiplicador
  alto (hasta x3), porque la quema se paga entera aunque vaya media vacia.
- El **factor** depende del horno *y* del tramo de ocupacion: cada horno tiene
  su propia curva. Por eso vive en una tabla y no en un ``if``.

Una pieza pasa normalmente por dos quemas —una baja (bizcocho) y una alta
(vidriado)— que pueden ocurrir en hornos distintos. De ahi que una linea
apunte a dos sesiones y no a una.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import money_numeric, quantity_numeric
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType

if TYPE_CHECKING:
    from app.models.masters import Product

#: Factor de ocupacion y participaciones. Con tres enteros basta (el maximo es
#: 3.0), pero la escala fina evita perder precision al multiplicar.
FACTOR_PRECISION = 10
FACTOR_SCALE = 6


def volume_numeric() -> Numeric[Decimal]:
    """Columna de volumen en cm3.

    Comparte escala con las cantidades fisicas: un volumen es una cantidad, y
    redondearlo antes de repartir el costo falsearia el reparto.
    """
    return quantity_numeric()


def factor_numeric() -> Numeric[Decimal]:
    """Columna para factores y porcentajes de ocupacion."""
    return Numeric(FACTOR_PRECISION, FACTOR_SCALE, asdecimal=True)


class FiringType(StrEnum):
    """Tipo de quema.

    Conjunto cerrado por definicion del negocio: un horno se enciende en
    temperatura baja o alta. Se modela como enum con ``CHECK`` —igual que
    ``RecipeStatus``— y no como tabla de catalogo, porque no hay ningun caso en
    el que el taller anada un tercer valor sin cambiar tambien las tarifas y la
    interfaz.
    """

    LOW = "LOW"
    HIGH = "HIGH"


class FiringStatus(StrEnum):
    """Ciclo de vida de una hoja de quema."""

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class Kiln(Base, TimestampMixin):
    """Horno del taller con su capacidad util."""

    __tablename__ = "kilns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Codigo interno generado por el backend (KILN-001...). Nunca por el cliente.
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    capacity_volume_cm3: Mapped[Decimal] = mapped_column(volume_numeric(), nullable=False)
    #: Dias que el horno queda ocupado por CADA hornada (Fase 009C).
    #:
    #: Vive aqui, y no como constante, porque la duracion es una propiedad
    #: fisica del horno: el pequeno tarda 3 dias y el grande 4. Ponerlo en
    #: codigo obligaria a desplegar para dar de alta un horno, y a deducir el
    #: tamano de un umbral de capacidad que el negocio nunca definio.
    firing_days_per_batch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"), index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("capacity_volume_cm3 > 0", name="capacity_positive"),
        CheckConstraint("firing_days_per_batch >= 1", name="firing_days_per_batch_positive"),
    )

    rates: Mapped[list[KilnRate]] = relationship(
        "KilnRate",
        back_populates="kiln",
        cascade="all, delete-orphan",
        order_by=lambda: (KilnRate.firing_type.asc(), KilnRate.valid_from.desc()),
    )
    occupancy_factors: Mapped[list[KilnOccupancyFactor]] = relationship(
        "KilnOccupancyFactor",
        back_populates="kiln",
        cascade="all, delete-orphan",
        order_by=lambda: (KilnOccupancyFactor.min_percentage.asc(),),
    )


class KilnRate(Base, TimestampMixin):
    """Tarifa de una quema en un horno, con vigencia.

    No se sobrescribe: cambiar una tarifa cierra la vigente (``valid_to``) y
    abre una nueva. Asi una quema confirmada en el pasado puede seguir
    explicandose con la tarifa que realmente se le aplico.
    """

    __tablename__ = "kiln_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kiln_id: Mapped[int] = mapped_column(
        ForeignKey("kilns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    firing_type: Mapped[FiringType] = mapped_column(StrEnumType(FiringType, 16), nullable=False)
    rate: Mapped[Decimal] = mapped_column(money_numeric(), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: Nulo mientras la tarifa esta vigente.
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        CheckConstraint("rate >= 0", name="rate_not_negative"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="validity_ordered"),
        # Como maximo una tarifa abierta por horno y tipo de quema. El indice
        # parcial lo impone la base de datos: dos peticiones concurrentes no
        # pueden dejar dos tarifas vigentes.
        Index(
            "ix_kiln_rates_single_open",
            "kiln_id",
            "firing_type",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
    )

    kiln: Mapped[Kiln] = relationship("Kiln", back_populates="rates")


class KilnOccupancyFactor(Base, TimestampMixin):
    """Tramo de ocupacion y su multiplicador, por horno.

    Es la tabla T15:V25 del documento funcional. Vive en base de datos y no en
    codigo porque el negocio la ajusta sin desplegar.
    """

    __tablename__ = "kiln_occupancy_factors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kiln_id: Mapped[int] = mapped_column(
        ForeignKey("kilns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Tramo cerrado en porcentaje entero: (1,10), (11,20)... (91,100).
    min_percentage: Mapped[int] = mapped_column(Integer, nullable=False)
    max_percentage: Mapped[int] = mapped_column(Integer, nullable=False)
    factor: Mapped[Decimal] = mapped_column(factor_numeric(), nullable=False)

    __table_args__ = (
        UniqueConstraint("kiln_id", "min_percentage", name="uq_kiln_occupancy_factors_kiln_id"),
        CheckConstraint("min_percentage >= 1 AND max_percentage <= 100", name="range_within_100"),
        CheckConstraint("max_percentage >= min_percentage", name="range_ordered"),
        CheckConstraint("factor > 0", name="factor_positive"),
    )

    kiln: Mapped[Kiln] = relationship("Kiln", back_populates="occupancy_factors")


class Firing(Base, TimestampMixin):
    """Hoja de quema: sesiones de horno, piezas y costo repartido."""

    __tablename__ = "firings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Correlativo emitido por ``SequenceService`` (tipo FIRING). Nunca por el cliente.
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[FiringStatus] = mapped_column(
        StrEnumType(FiringStatus, 16),
        nullable=False,
        default=FiringStatus.DRAFT,
        index=True,
    )
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    firing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    total_volume_cm3: Mapped[Decimal] = mapped_column(
        volume_numeric(), nullable=False, default=Decimal(0), server_default=text("0")
    )
    #: Ocupacion fisica de la sesion mas cargada, en porcentaje. Informativa: el
    #: factor no se calcula con ella, sino linea a linea.
    occupancy_percentage: Mapped[Decimal] = mapped_column(
        factor_numeric(), nullable=False, default=Decimal(0), server_default=text("0")
    )
    #: Factor efectivo de la hoja: ``total_cost / subtotal``. Es un resumen
    #: ponderado, no un dato de entrada: cada linea tiene el suyo.
    occupancy_factor: Mapped[Decimal] = mapped_column(
        factor_numeric(), nullable=False, default=Decimal(1), server_default=text("1")
    )
    #: Costo repartido antes de aplicar los factores.
    subtotal: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, default=Decimal(0), server_default=text("0")
    )
    total_cost: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, default=Decimal(0), server_default=text("0")
    )

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('DRAFT', 'CONFIRMED', 'CANCELLED')", name="status_allowed"),
        Index("ix_firings_firing_date", "firing_date"),
    )

    sessions: Mapped[list[FiringKilnSession]] = relationship(
        "FiringKilnSession",
        back_populates="firing",
        cascade="all, delete-orphan",
        order_by=lambda: (FiringKilnSession.sort_order.asc(), FiringKilnSession.id.asc()),
    )
    lines: Mapped[list[FiringLine]] = relationship(
        "FiringLine",
        back_populates="firing",
        cascade="all, delete-orphan",
        order_by=lambda: (FiringLine.sort_order.asc(), FiringLine.id.asc()),
    )


class FiringKilnSession(Base, TimestampMixin):
    """Uso de un horno a una temperatura dentro de una hoja de quema.

    Guarda copia de la tarifa y de la capacidad vigentes. Cambiar el maestro
    manana no reescribe lo que costo ayer.
    """

    __tablename__ = "firing_kiln_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    firing_id: Mapped[int] = mapped_column(
        ForeignKey("firings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kiln_id: Mapped[int] = mapped_column(
        ForeignKey("kilns.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    firing_type: Mapped[FiringType] = mapped_column(StrEnumType(FiringType, 16), nullable=False)
    rate_snapshot: Mapped[Decimal] = mapped_column(money_numeric(), nullable=False)
    capacity_snapshot: Mapped[Decimal] = mapped_column(volume_numeric(), nullable=False)
    #: Costo repartido de esta sesion antes de factores. Coincide con la tarifa
    #: cuando todas las lineas de la hoja participan en ella.
    subtotal: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, default=Decimal(0), server_default=text("0")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "firing_id", "kiln_id", "firing_type", name="uq_firing_kiln_sessions_firing_id"
        ),
        CheckConstraint("rate_snapshot >= 0", name="rate_not_negative"),
        CheckConstraint("capacity_snapshot > 0", name="capacity_positive"),
    )

    firing: Mapped[Firing] = relationship("Firing", back_populates="sessions")
    kiln: Mapped[Kiln] = relationship("Kiln", lazy="joined")


class FiringLine(Base, TimestampMixin):
    """Pieza —o grupo de piezas iguales— dentro de una hoja de quema."""

    __tablename__ = "firing_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    firing_id: Mapped[int] = mapped_column(
        ForeignKey("firings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Producto de catalogo cuando existe. El documento funcional tambien
    #: registra piezas descritas a mano, asi que se admite nulo con descripcion.
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    length_cm: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    width_cm: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    height_cm: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    unit_volume_cm3: Mapped[Decimal] = mapped_column(volume_numeric(), nullable=False)
    total_volume_cm3: Mapped[Decimal] = mapped_column(volume_numeric(), nullable=False)

    #: Sesiones que queman esta pieza. Se modelan explicitamente porque el
    #: documento permite que la quema baja y la alta ocurran en hornos distintos.
    low_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("firing_kiln_sessions.id", ondelete="SET NULL"), nullable=True
    )
    high_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("firing_kiln_sessions.id", ondelete="SET NULL"), nullable=True
    )
    #: Horno cuya capacidad decide el tramo de ocupacion de esta linea.
    factor_kiln_id: Mapped[int | None] = mapped_column(
        ForeignKey("kilns.id", ondelete="RESTRICT"), nullable=True
    )

    occupancy_percentage: Mapped[Decimal] = mapped_column(
        factor_numeric(), nullable=False, default=Decimal(0), server_default=text("0")
    )
    #: Tramo comercial en decenas (10, 20... 100). No es la ocupacion fisica.
    occupancy_bracket: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default=text("10")
    )
    occupancy_factor: Mapped[Decimal] = mapped_column(
        factor_numeric(), nullable=False, default=Decimal(1), server_default=text("1")
    )
    #: Reparto proporcional antes del factor.
    base_cost: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, default=Decimal(0), server_default=text("0")
    )
    #: Costo final de la linea: ``base_cost * occupancy_factor``.
    allocated_cost: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, default=Decimal(0), server_default=text("0")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "length_cm > 0 AND width_cm > 0 AND height_cm > 0", name="dimensions_positive"
        ),
        CheckConstraint("occupancy_bracket BETWEEN 10 AND 100", name="bracket_in_tens"),
        CheckConstraint("occupancy_bracket % 10 = 0", name="bracket_multiple_of_ten"),
    )

    firing: Mapped[Firing] = relationship("Firing", back_populates="lines")
    product: Mapped[Product | None] = relationship("Product", lazy="joined")
