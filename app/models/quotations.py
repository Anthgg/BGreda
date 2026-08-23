"""Maestros de costos, cotizaciones y snapshots economicos de Fase 005."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import calculation_numeric, quantity_numeric
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType


class TechniqueFormulaType(StrEnum):
    ONE_FACTOR = "ONE_FACTOR"
    TWO_FACTORS = "TWO_FACTORS"


class AdditionalFormulaType(StrEnum):
    PIECE_QUANTITY = "PIECE_QUANTITY"
    SIMPLE_QUANTITY = "SIMPLE_QUANTITY"
    PIECE_X_ADDITIONAL = "PIECE_X_ADDITIONAL"


class OtherCostCalculationType(StrEnum):
    PER_DAY = "PER_DAY"
    FIXED = "FIXED"
    PER_PIECE = "PER_PIECE"


class QuotationStatus(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class Technique(Base, TimestampMixin):
    __tablename__ = "techniques"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    formula_type: Mapped[TechniqueFormulaType] = mapped_column(
        StrEnumType(TechniqueFormulaType, 24), nullable=False
    )
    factor_1: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    factor_2: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("factor_1 > 0", name="factor_1_positive"),
        CheckConstraint(
            "(formula_type = 'ONE_FACTOR' AND factor_2 IS NULL) OR "
            "(formula_type = 'TWO_FACTORS' AND factor_2 > 0)",
            name="formula_factors_valid",
        ),
        Index(
            "uq_techniques_active_name",
            text("lower(btrim(name))"),
            unique=True,
            postgresql_where=text("active"),
        ),
    )


class Additional(Base, TimestampMixin):
    __tablename__ = "additionals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    formula_type: Mapped[AdditionalFormulaType] = mapped_column(
        StrEnumType(AdditionalFormulaType, 32), nullable=False
    )
    factor_1: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint(
            "NOT active OR formula_type = 'SIMPLE_QUANTITY' OR factor_1 > 0",
            name="active_formula_factor_valid",
        ),
        CheckConstraint(
            "formula_type <> 'SIMPLE_QUANTITY' OR factor_1 IS NULL",
            name="simple_quantity_has_no_factor",
        ),
        Index(
            "uq_additionals_active_name",
            text("lower(btrim(name))"),
            unique=True,
            postgresql_where=text("active"),
        ),
    )


class OtherCost(Base, TimestampMixin):
    __tablename__ = "other_costs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    calculation_type: Mapped[OtherCostCalculationType] = mapped_column(
        StrEnumType(OtherCostCalculationType, 24), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        Index(
            "uq_other_costs_active_name",
            text("lower(btrim(name))"),
            unique=True,
            postgresql_where=text("active"),
        ),
    )


class Quotation(Base, TimestampMixin):
    __tablename__ = "quotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[QuotationStatus] = mapped_column(
        StrEnumType(QuotationStatus, 16),
        nullable=False,
        default=QuotationStatus.DRAFT,
        index=True,
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe_id: Mapped[int | None] = mapped_column(
        ForeignKey("recipes.id", ondelete="RESTRICT"), index=True
    )
    recipe_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("recipe_versions.id", ondelete="RESTRICT"), index=True
    )
    recipe_version_fingerprint_snapshot: Mapped[str | None] = mapped_column(String(64))
    firing_id: Mapped[int | None] = mapped_column(
        ForeignKey("firings.id", ondelete="RESTRICT"), index=True
    )
    firing_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("firing_lines.id", ondelete="RESTRICT"), index=True
    )
    firing_code_snapshot: Mapped[str | None] = mapped_column(String(64))
    firing_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    materials_calculated: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    materials_applied: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    firing_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    labor_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    calculated_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    days_adjustment: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    waiting_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    space_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    commercial_factor_default_snapshot: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False
    )
    commercial_factor: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    current_sale_price_snapshot: Mapped[Decimal | None] = mapped_column(calculation_numeric())
    base_commercial_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    calculated_total: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    calculated_unit_price: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    calculation_warnings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("materials_calculated >= 0", name="materials_calculated_non_negative"),
        CheckConstraint("materials_applied >= 0", name="materials_applied_non_negative"),
        CheckConstraint("firing_cost >= 0", name="firing_cost_non_negative"),
        CheckConstraint("labor_cost >= 0", name="labor_cost_non_negative"),
        CheckConstraint("waiting_days >= 0", name="waiting_days_non_negative"),
        CheckConstraint("total_days >= 0", name="total_days_non_negative"),
        CheckConstraint("space_cost >= 0", name="space_cost_non_negative"),
        CheckConstraint("commercial_factor > 0", name="commercial_factor_positive"),
        CheckConstraint("status IN ('DRAFT', 'CONFIRMED', 'CANCELLED')", name="status_allowed"),
        Index("ix_quotations_created_at", "created_at"),
    )

    techniques: Mapped[list[QuotationTechnique]] = relationship(
        "QuotationTechnique",
        back_populates="quotation",
        cascade="all, delete-orphan",
        order_by=lambda: (QuotationTechnique.sort_order.asc(), QuotationTechnique.id.asc()),
    )
    additionals: Mapped[list[QuotationAdditional]] = relationship(
        "QuotationAdditional",
        back_populates="quotation",
        cascade="all, delete-orphan",
        order_by=lambda: (QuotationAdditional.sort_order.asc(), QuotationAdditional.id.asc()),
    )
    other_costs: Mapped[list[QuotationOtherCost]] = relationship(
        "QuotationOtherCost",
        back_populates="quotation",
        cascade="all, delete-orphan",
        order_by=lambda: (QuotationOtherCost.sort_order.asc(), QuotationOtherCost.id.asc()),
    )


class QuotationTechnique(Base, TimestampMixin):
    __tablename__ = "quotation_techniques"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    technique_id: Mapped[int] = mapped_column(
        ForeignKey("techniques.id", ondelete="RESTRICT"), nullable=False
    )
    name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_price_snapshot: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    formula_type_snapshot: Mapped[TechniqueFormulaType] = mapped_column(
        StrEnumType(TechniqueFormulaType, 24), nullable=False
    )
    factor_1_snapshot: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    factor_2_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    source_updated_at_snapshot: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    proposed_cost: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    applied_cost: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    proposed_days: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_days: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    factors_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    applied_cost_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    applied_days_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("proposed_cost >= 0 AND applied_cost >= 0", name="costs_non_negative"),
        CheckConstraint("proposed_days >= 0 AND applied_days >= 0", name="days_non_negative"),
    )
    quotation: Mapped[Quotation] = relationship("Quotation", back_populates="techniques")


class QuotationAdditional(Base, TimestampMixin):
    __tablename__ = "quotation_additionals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    additional_id: Mapped[int] = mapped_column(
        ForeignKey("additionals.id", ondelete="RESTRICT"), nullable=False
    )
    name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_price_snapshot: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    formula_type_snapshot: Mapped[AdditionalFormulaType] = mapped_column(
        StrEnumType(AdditionalFormulaType, 32), nullable=False
    )
    factor_1_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    source_updated_at_snapshot: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    additional_quantity: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    proposed_cost: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    applied_cost: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    unit_price_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    factor_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    applied_cost_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("proposed_cost >= 0 AND applied_cost >= 0", name="costs_non_negative"),
    )
    quotation: Mapped[Quotation] = relationship("Quotation", back_populates="additionals")


class QuotationOtherCost(Base, TimestampMixin):
    __tablename__ = "quotation_other_costs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    other_cost_id: Mapped[int] = mapped_column(
        ForeignKey("other_costs.id", ondelete="RESTRICT"), nullable=False
    )
    name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_price_snapshot: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    calculation_type_snapshot: Mapped[OtherCostCalculationType] = mapped_column(
        StrEnumType(OtherCostCalculationType, 24), nullable=False
    )
    source_updated_at_snapshot: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    proposed_cost: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    applied_cost: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    unit_price_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("proposed_cost >= 0 AND applied_cost >= 0", name="costs_non_negative"),
    )
    quotation: Mapped[Quotation] = relationship("Quotation", back_populates="other_costs")


class QuotationProductPriceUpdate(Base):
    __tablename__ = "quotation_product_price_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    old_price: Mapped[Decimal | None] = mapped_column(calculation_numeric())
    new_price: Mapped[Decimal] = mapped_column(calculation_numeric(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("quotation_id", name="uq_price_update_quotation"),
        CheckConstraint("new_price >= 0", name="new_price_non_negative"),
    )
