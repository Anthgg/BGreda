"""Modelos de dominio para recetas, versiones y lineas de componentes.

Una receta modela la formula de un producto preparado (PREPARED_MATERIAL).
Cada receta cuenta con historial inmutable de versiones y una unica version
activa (ACTIVE). Las lineas diferencian entre componentes base (BASE),
colorantes (COLORANT) y aditivos (ADDITIVE).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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

from app.core.precision import percentage_numeric
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
