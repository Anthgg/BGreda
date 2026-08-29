"""Modelos de dominio para recetas, versiones y lineas de componentes.

Una receta modela la formula de un producto preparado (PREPARED_MATERIAL).
Cada receta cuenta con historial inmutable de versiones y una unica version
activa (ACTIVE). Las lineas diferencian entre componentes base (BASE),
colorantes (COLORANT) y aditivos (ADDITIVE).
"""

from __future__ import annotations

import uuid
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import (
    money_numeric,
    percentage_numeric,
    quantity_numeric,
    unit_cost_numeric,
)
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType

if TYPE_CHECKING:
    from app.models.masters import Product


class RecipeComponentType(StrEnum):
    """Clasificacion funcional de una linea de receta."""

    BASE = "BASE"
    COLORANT = "COLORANT"
    ADDITIVE = "ADDITIVE"


class RecipeStatus(StrEnum):
    """Estado del ciclo de vida de una version de receta."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class Recipe(Base, TimestampMixin):
    """Cabecera de receta vinculada a un producto preparado."""

    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        index=True,
    )
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "recipe_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_recipes_current_version_id",
        ),
        nullable=True,
        index=True,
    )

    # Relaciones
    product: Mapped[Product] = relationship("Product", foreign_keys=[product_id], lazy="joined")
    current_version: Mapped[RecipeVersion | None] = relationship(
        "RecipeVersion",
        foreign_keys=[current_version_id],
        post_update=True,
    )
    versions: Mapped[list[RecipeVersion]] = relationship(
        "RecipeVersion",
        foreign_keys="RecipeVersion.recipe_id",
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by=lambda: (RecipeVersion.version_number.desc(),),
    )


class RecipeVersion(Base, TimestampMixin):
    """Version especifica e inmutable de la formula de una receta."""

    __tablename__ = "recipe_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RecipeStatus] = mapped_column(
        StrEnumType(RecipeStatus, 32),
        nullable=False,
        default=RecipeStatus.DRAFT,
        index=True,
    )
    yield_factor: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    base_total: Mapped[Decimal] = mapped_column(percentage_numeric(), nullable=False)
    additional_total: Mapped[Decimal] = mapped_column(
        percentage_numeric(),
        nullable=False,
        default=Decimal(0),
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("recipe_id", "version_number", name="uq_recipe_version_number"),
        CheckConstraint("yield_factor > 0", name="chk_recipe_version_yield_positive"),
        CheckConstraint("version_number > 0", name="chk_recipe_version_number_positive"),
        Index(
            "ix_recipe_versions_single_active",
            "recipe_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    # Relaciones
    recipe: Mapped[Recipe] = relationship(
        "Recipe",
        foreign_keys=[recipe_id],
        back_populates="versions",
    )
    lines: Mapped[list[RecipeLine]] = relationship(
        "RecipeLine",
        back_populates="version",
        cascade="all, delete-orphan",
        order_by=lambda: (RecipeLine.sort_order.asc(), RecipeLine.id.asc()),
    )


class RecipeLine(Base):
    """Componente individual de una version de receta."""

    __tablename__ = "recipe_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipe_version_id: Mapped[int] = mapped_column(
        ForeignKey("recipe_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    component_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    component_type: Mapped[RecipeComponentType] = mapped_column(
        StrEnumType(RecipeComponentType, 32),
        nullable=False,
    )
    percentage: Mapped[Decimal] = mapped_column(percentage_numeric(), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("percentage > 0", name="chk_recipe_line_percentage_positive"),
    )

    # Relaciones
    version: Mapped[RecipeVersion] = relationship("RecipeVersion", back_populates="lines")
    component_product: Mapped[Product] = relationship(
        "Product",
        foreign_keys=[component_product_id],
        lazy="joined",
    )


class PreparationStatus(StrEnum):
    """Estado de un lote preparado.

    Solo existe ``COMPLETED`` por ahora: una preparacion se registra cuando ya
    ocurrio fisicamente, en una sola transaccion. El enum existe para que
    anadir una reversion en el futuro no obligue a cambiar el esquema.
    """

    COMPLETED = "COMPLETED"


class RecipePreparation(Base, TimestampMixin):
    """Lote realmente preparado a partir de una version de receta.

    ## Por que el peso absoluto vive aqui y no en la receta

    Una receta es una FORMULACION: proporciones que no dependen de cuanto se
    fabrique. El peso concreto pertenece al acto de fabricar, asi que
    ``total_dry_weight_g`` es del lote. Las cantidades de cada componente se
    derivan al preparar, aplicando los porcentajes de la version.

    ## Por que se guardan agua y rendimiento por separado

    ``final_yield_ml`` es el rendimiento REAL medido, no una suma. Anadir
    200 g de solidos y 800 ml de agua no da necesariamente 1000 ml: los solidos
    ocupan volumen y el agua se absorbe. Derivar uno del otro inventaria un
    dato que solo se conoce midiendo.

    ## Por que el costo se congela

    ``batch_total_cost`` es el costo de los ingredientes en el momento de
    prepararlos. Si manana sube el precio del caolin, este lote sigue habiendo
    costado lo que costo: recalcularlo falsearia el historico.
    """

    __tablename__ = "recipe_preparations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Correlativo emitido por ``SequenceService`` (tipo PREPARATION). Nunca
    #: por el cliente.
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    recipe_version_id: Mapped[int] = mapped_column(
        ForeignKey("recipe_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Producto PREPARED_MATERIAL cuyo stock aumenta.
    prepared_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    location_id: Mapped[int] = mapped_column(
        ForeignKey("stock_locations.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    total_dry_weight_g: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    water_amount_ml: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    final_yield_ml: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    #: ``total_dry_weight_g / final_yield_ml``. Es el puente g <-> ml de ESTE
    #: lote; no existe una densidad universal que sirva para todos.
    solids_g_per_ml: Mapped[Decimal] = mapped_column(unit_cost_numeric(), nullable=False)

    batch_total_cost: Mapped[Decimal] = mapped_column(money_numeric(), nullable=False)
    unit_cost_per_ml: Mapped[Decimal] = mapped_column(unit_cost_numeric(), nullable=False)

    #: Clave que impide ejecutar dos veces la misma preparacion fisica.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[PreparationStatus] = mapped_column(
        StrEnumType(PreparationStatus, 16),
        nullable=False,
        default=PreparationStatus.COMPLETED,
        index=True,
    )

    prepared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    __table_args__ = (
        CheckConstraint("total_dry_weight_g > 0", name="dry_weight_positive"),
        CheckConstraint("water_amount_ml >= 0", name="water_not_negative"),
        CheckConstraint("final_yield_ml > 0", name="yield_positive"),
        CheckConstraint("solids_g_per_ml > 0", name="concentration_positive"),
        CheckConstraint("batch_total_cost >= 0", name="cost_not_negative"),
        CheckConstraint("unit_cost_per_ml >= 0", name="unit_cost_not_negative"),
        CheckConstraint("status IN ('COMPLETED')", name="status_allowed"),
    )

    version: Mapped[RecipeVersion] = relationship("RecipeVersion", lazy="joined")
    prepared_product: Mapped[Product] = relationship(
        "Product", foreign_keys=[prepared_product_id], lazy="joined"
    )
    lines: Mapped[list[RecipePreparationLine]] = relationship(
        "RecipePreparationLine",
        back_populates="preparation",
        cascade="all, delete-orphan",
        order_by=lambda: RecipePreparationLine.id.asc(),
        lazy="selectin",
    )


class RecipePreparationLine(Base):
    """Ingrediente consumido por un lote, con su costo congelado.

    ``unit_cost_snapshot`` es el costo por gramo que tenia el insumo cuando se
    preparo. Guardarlo aqui es lo que permite explicar el costo del lote anos
    despues sin depender del maestro vigente.
    """

    __tablename__ = "recipe_preparation_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    preparation_id: Mapped[int] = mapped_column(
        ForeignKey("recipe_preparations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    component_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    quantity_g: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    unit_cost_snapshot: Mapped[Decimal] = mapped_column(unit_cost_numeric(), nullable=False)
    line_cost: Mapped[Decimal] = mapped_column(money_numeric(), nullable=False)

    __table_args__ = (
        CheckConstraint("quantity_g > 0", name="quantity_positive"),
        CheckConstraint("unit_cost_snapshot >= 0", name="unit_cost_not_negative"),
        CheckConstraint("line_cost >= 0", name="line_cost_not_negative"),
    )

    preparation: Mapped[RecipePreparation] = relationship(
        "RecipePreparation", back_populates="lines"
    )
    component_product: Mapped[Product] = relationship(
        "Product", foreign_keys=[component_product_id], lazy="joined"
    )
