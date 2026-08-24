"""Fase 005: cotizaciones y maestros de costos reconstruidos del Excel.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-23

Los seeds provienen de ``Propuesta para cotizar.xlsx``, hoja ``Cotizador``:

- tecnicas: ``L5:P9``;
- adicionales: ``L16:P22``;
- otros gastos: ``L27:N31``;
- factor comercial: ``D10``.

``Con ilustracion`` tiene la celda de factor vacia en la tabla maestra
(``O22``), pero el propio libro si lo declara en el caso aplicado: ``E24``
usa 50, igual que el resto de adicionales por piezas. No son dos valores en
conflicto sino una celda del maestro sin rellenar, asi que se siembra con ese
50 y se anota de donde sale. La reproduccion del caso completo del Excel
—1885 de mano de obra y 7029.4818 de total— lo confirma.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CALC = sa.Numeric(precision=36, scale=18)
PCT = sa.Numeric(precision=9, scale=6)
QUANTITY = sa.Numeric(precision=18, scale=6)

NEW_TABLES = (
    "techniques",
    "additionals",
    "other_costs",
    "quotations",
    "quotation_techniques",
    "quotation_additionals",
    "quotation_other_costs",
    "quotation_product_price_updates",
)

_REVOKE_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
        REVOKE ALL ON TABLE public.{table} FROM {role};
    END IF;
END
$$
"""

SEED_TECHNIQUES = (
    (1, "TEC-001", "Piezas en torno facil", "220", "TWO_FACTORS", "50", "100"),
    (2, "TEC-002", "Piezas en torno dificil", "220", "TWO_FACTORS", "25", "100"),
    (3, "TEC-003", "Colada", "220", "ONE_FACTOR", "100", None),
    (4, "TEC-004", "A mano", "110", "ONE_FACTOR", "15", None),
)

SEED_ADDITIONALS = (
    (1, "ADI-001", "Armado de asa", "110", "PIECE_QUANTITY", "50", True, None),
    (2, "ADI-002", "Numero de moldes", "250", "SIMPLE_QUANTITY", None, True, None),
    (
        3,
        "ADI-003",
        "Vidriado por immersion",
        "110",
        "PIECE_X_ADDITIONAL",
        "50",
        True,
        None,
    ),
    (
        4,
        "ADI-004",
        "Vidriado por aspersion",
        "110",
        "PIECE_X_ADDITIONAL",
        "50",
        True,
        None,
    ),
    (
        5,
        "ADI-005",
        "Vidriado a mano alzada",
        "110",
        "PIECE_X_ADDITIONAL",
        "50",
        True,
        None,
    ),
    (
        6,
        "ADI-006",
        "Con ilustracion",
        "110",
        "PIECE_QUANTITY",
        "50",
        True,
        "Factor tomado de E24 del caso aplicado: O22 del maestro esta vacia.",
    ),
)

SEED_OTHER_COSTS = (
    (1, "OTH-001", "Alquiler del espacio x dia", "110", "PER_DAY"),
    (2, "OTH-002", "Costo de servicios x dia", "10", "PER_DAY"),
    (3, "OTH-003", "Costo administrativo", "200", "FIXED"),
    (4, "OTH-004", "Factor", "3", "PER_PIECE"),
)


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.add_column(
        "commercial_settings",
        sa.Column(
            "default_quotation_factor",
            sa.Numeric(precision=9, scale=6),
            server_default=sa.text("2"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_commercial_settings_default_quotation_factor_positive",
        "commercial_settings",
        "default_quotation_factor > 0",
    )

    op.create_table(
        "techniques",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("unit_price", CALC, nullable=False),
        sa.Column("formula_type", sa.String(length=24), nullable=False),
        sa.Column("factor_1", QUANTITY, nullable=False),
        sa.Column("factor_2", QUANTITY, nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("unit_price >= 0", name="ck_techniques_unit_price_non_negative"),
        sa.CheckConstraint("factor_1 > 0", name="ck_techniques_factor_1_positive"),
        sa.CheckConstraint(
            "(formula_type = 'ONE_FACTOR' AND factor_2 IS NULL) OR "
            "(formula_type = 'TWO_FACTORS' AND factor_2 > 0)",
            name="ck_techniques_formula_factors_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_techniques"),
        sa.UniqueConstraint("code", name="uq_techniques_code"),
    )
    op.create_index(
        "uq_techniques_active_name",
        "techniques",
        [sa.text("lower(btrim(name))")],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    op.create_table(
        "additionals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("unit_price", CALC, nullable=False),
        sa.Column("formula_type", sa.String(length=32), nullable=False),
        sa.Column("factor_1", QUANTITY, nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("unit_price >= 0", name="ck_additionals_unit_price_non_negative"),
        sa.CheckConstraint(
            "NOT active OR formula_type = 'SIMPLE_QUANTITY' OR factor_1 > 0",
            name="ck_additionals_active_formula_factor_valid",
        ),
        sa.CheckConstraint(
            "formula_type <> 'SIMPLE_QUANTITY' OR factor_1 IS NULL",
            name="ck_additionals_simple_quantity_has_no_factor",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_additionals"),
        sa.UniqueConstraint("code", name="uq_additionals_code"),
    )
    op.create_index(
        "uq_additionals_active_name",
        "additionals",
        [sa.text("lower(btrim(name))")],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    op.create_table(
        "other_costs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("unit_price", CALC, nullable=False),
        sa.Column("calculation_type", sa.String(length=24), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("unit_price >= 0", name="ck_other_costs_unit_price_non_negative"),
        sa.PrimaryKeyConstraint("id", name="pk_other_costs"),
        sa.UniqueConstraint("code", name="uq_other_costs_code"),
    )
    op.create_index(
        "uq_other_costs_active_name",
        "other_costs",
        [sa.text("lower(btrim(name))")],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    op.create_table(
        "quotations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("recipe_id", sa.Integer(), nullable=True),
        sa.Column("recipe_version_id", sa.Integer(), nullable=True),
        sa.Column("recipe_version_fingerprint_snapshot", sa.String(length=64), nullable=True),
        sa.Column("firing_id", sa.Integer(), nullable=True),
        sa.Column("firing_line_id", sa.Integer(), nullable=True),
        sa.Column("firing_code_snapshot", sa.String(length=64), nullable=True),
        sa.Column(
            "firing_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("materials_calculated", CALC, server_default=sa.text("0"), nullable=False),
        sa.Column("materials_applied", CALC, server_default=sa.text("0"), nullable=False),
        sa.Column("firing_cost", CALC, server_default=sa.text("0"), nullable=False),
        sa.Column("labor_cost", CALC, server_default=sa.text("0"), nullable=False),
        sa.Column("calculated_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("days_adjustment", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("waiting_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("total_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("space_cost", CALC, server_default=sa.text("0"), nullable=False),
        sa.Column("commercial_factor_default_snapshot", CALC, nullable=False),
        sa.Column("commercial_factor", CALC, nullable=False),
        sa.Column("current_sale_price_snapshot", CALC, nullable=True),
        sa.Column("base_commercial_cost", CALC, server_default=sa.text("0"), nullable=False),
        sa.Column("calculated_total", CALC, server_default=sa.text("0"), nullable=False),
        sa.Column("calculated_unit_price", CALC, server_default=sa.text("0"), nullable=False),
        # Gramos de receta por pieza. Nulo significa «todavia no se ha
        # indicado»: no hay valor por omision, porque suponer uno convertiria un
        # dato ausente en un costo de materiales creible pero falso.
        sa.Column(
            "material_grams_per_piece",
            sa.Numeric(precision=18, scale=6),
            nullable=True,
        ),
        # El IGV se anade sobre el precio neto; el neto no lo incluye nunca.
        sa.Column("tax_percentage_snapshot", PCT, server_default=sa.text("0"), nullable=False),
        sa.Column(
            "tax_rate_source_snapshot",
            sa.String(length=32),
            server_default=sa.text("'COMMERCIAL_SETTINGS'"),
            nullable=False,
        ),
        sa.Column("tax_amount", CALC, server_default=sa.text("0"), nullable=False),
        sa.Column("total_with_tax", CALC, server_default=sa.text("0"), nullable=False),
        sa.Column("unit_price_with_tax", CALC, server_default=sa.text("0"), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "calculation_warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("quantity > 0", name="ck_quotations_quantity_positive"),
        sa.CheckConstraint(
            "material_grams_per_piece IS NULL OR material_grams_per_piece > 0",
            name="ck_quotations_material_grams_positive",
        ),
        sa.CheckConstraint(
            "tax_percentage_snapshot >= 0", name="ck_quotations_tax_percentage_not_negative"
        ),
        sa.CheckConstraint(
            "tax_rate_source_snapshot IN ('PRODUCT', 'COMMERCIAL_SETTINGS')",
            name="ck_quotations_tax_rate_source_allowed",
        ),
        sa.CheckConstraint(
            "materials_calculated >= 0", name="ck_quotations_materials_calculated_non_negative"
        ),
        sa.CheckConstraint(
            "materials_applied >= 0", name="ck_quotations_materials_applied_non_negative"
        ),
        sa.CheckConstraint("firing_cost >= 0", name="ck_quotations_firing_cost_non_negative"),
        sa.CheckConstraint("labor_cost >= 0", name="ck_quotations_labor_cost_non_negative"),
        sa.CheckConstraint("waiting_days >= 0", name="ck_quotations_waiting_days_non_negative"),
        sa.CheckConstraint("total_days >= 0", name="ck_quotations_total_days_non_negative"),
        sa.CheckConstraint("space_cost >= 0", name="ck_quotations_space_cost_non_negative"),
        sa.CheckConstraint(
            "commercial_factor > 0", name="ck_quotations_commercial_factor_positive"
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'CONFIRMED', 'CANCELLED')",
            name="ck_quotations_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_quotations_product_id_products",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipes.id"],
            name="fk_quotations_recipe_id_recipes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_version_id"],
            ["recipe_versions.id"],
            name="fk_quotations_recipe_version_id_recipe_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["firing_id"],
            ["firings.id"],
            name="fk_quotations_firing_id_firings",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["firing_line_id"],
            ["firing_lines.id"],
            name="fk_quotations_firing_line_id_firing_lines",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quotations"),
        sa.UniqueConstraint("code", name="uq_quotations_code"),
    )
    for column in (
        "status",
        "product_id",
        "recipe_id",
        "recipe_version_id",
        "firing_id",
        "firing_line_id",
    ):
        op.create_index(f"ix_quotations_{column}", "quotations", [column])
    op.create_index("ix_quotations_created_at", "quotations", ["created_at"])

    _create_quotation_lines()
    _seed_cost_masters()

    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;")
        for role in ("anon", "authenticated"):
            op.execute(_REVOKE_SQL.format(table=table, role=role))


def _create_quotation_lines() -> None:
    op.create_table(
        "quotation_techniques",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("quotation_id", sa.Integer(), nullable=False),
        sa.Column("technique_id", sa.Integer(), nullable=False),
        sa.Column("name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("unit_price_snapshot", CALC, nullable=False),
        sa.Column("formula_type_snapshot", sa.String(length=24), nullable=False),
        sa.Column("factor_1_snapshot", QUANTITY, nullable=False),
        sa.Column("factor_2_snapshot", QUANTITY, nullable=True),
        sa.Column("source_updated_at_snapshot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("proposed_cost", CALC, nullable=False),
        sa.Column("applied_cost", CALC, nullable=False),
        sa.Column("proposed_days", sa.Integer(), nullable=False),
        sa.Column("applied_days", sa.Integer(), nullable=False),
        sa.Column(
            "unit_price_overridden", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "factors_overridden", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "applied_cost_overridden", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "applied_days_overridden", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("quantity > 0", name="ck_quotation_techniques_quantity_positive"),
        sa.CheckConstraint(
            "proposed_cost >= 0 AND applied_cost >= 0",
            name="ck_quotation_techniques_costs_non_negative",
        ),
        sa.CheckConstraint(
            "proposed_days >= 0 AND applied_days >= 0",
            name="ck_quotation_techniques_days_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["quotation_id"],
            ["quotations.id"],
            name="fk_quotation_techniques_quotation_id_quotations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["technique_id"],
            ["techniques.id"],
            name="fk_quotation_techniques_technique_id_techniques",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quotation_techniques"),
    )
    op.create_index(
        "ix_quotation_techniques_quotation_id", "quotation_techniques", ["quotation_id"]
    )

    op.create_table(
        "quotation_additionals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("quotation_id", sa.Integer(), nullable=False),
        sa.Column("additional_id", sa.Integer(), nullable=False),
        sa.Column("name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("unit_price_snapshot", CALC, nullable=False),
        sa.Column("formula_type_snapshot", sa.String(length=32), nullable=False),
        sa.Column("factor_1_snapshot", QUANTITY, nullable=True),
        sa.Column("source_updated_at_snapshot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("additional_quantity", QUANTITY, nullable=True),
        sa.Column("proposed_cost", CALC, nullable=False),
        sa.Column("applied_cost", CALC, nullable=False),
        sa.Column(
            "unit_price_overridden", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "factor_overridden", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "applied_cost_overridden", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "proposed_cost >= 0 AND applied_cost >= 0",
            name="ck_quotation_additionals_costs_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["quotation_id"],
            ["quotations.id"],
            name="fk_quotation_additionals_quotation_id_quotations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["additional_id"],
            ["additionals.id"],
            name="fk_quotation_additionals_additional_id_additionals",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quotation_additionals"),
    )
    op.create_index(
        "ix_quotation_additionals_quotation_id", "quotation_additionals", ["quotation_id"]
    )

    op.create_table(
        "quotation_other_costs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("quotation_id", sa.Integer(), nullable=False),
        sa.Column("other_cost_id", sa.Integer(), nullable=False),
        sa.Column("name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("unit_price_snapshot", CALC, nullable=False),
        sa.Column("calculation_type_snapshot", sa.String(length=24), nullable=False),
        sa.Column("source_updated_at_snapshot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proposed_cost", CALC, nullable=False),
        sa.Column("applied_cost", CALC, nullable=False),
        sa.Column(
            "unit_price_overridden", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "proposed_cost >= 0 AND applied_cost >= 0",
            name="ck_quotation_other_costs_costs_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["quotation_id"],
            ["quotations.id"],
            name="fk_quotation_other_costs_quotation_id_quotations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["other_cost_id"],
            ["other_costs.id"],
            name="fk_quotation_other_costs_other_cost_id_other_costs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quotation_other_costs"),
    )
    op.create_index(
        "ix_quotation_other_costs_quotation_id", "quotation_other_costs", ["quotation_id"]
    )

    op.create_table(
        "quotation_product_price_updates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("quotation_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("old_price", CALC, nullable=True),
        sa.Column("new_price", CALC, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "new_price >= 0", name="ck_quotation_product_price_updates_new_price_non_negative"
        ),
        sa.ForeignKeyConstraint(
            ["quotation_id"],
            ["quotations.id"],
            name="fk_quotation_product_price_updates_quotation_id_quotations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_quotation_product_price_updates_product_id_products",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quotation_product_price_updates"),
        sa.UniqueConstraint("quotation_id", name="uq_price_update_quotation"),
    )
    op.create_index(
        "ix_quotation_product_price_updates_quotation_id",
        "quotation_product_price_updates",
        ["quotation_id"],
    )
    op.create_index(
        "ix_quotation_product_price_updates_product_id",
        "quotation_product_price_updates",
        ["product_id"],
    )


def _seed_cost_masters() -> None:
    techniques = sa.table(
        "techniques",
        sa.column("id", sa.Integer),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("unit_price", sa.Numeric),
        sa.column("formula_type", sa.String),
        sa.column("factor_1", sa.Numeric),
        sa.column("factor_2", sa.Numeric),
        sa.column("active", sa.Boolean),
    )
    additionals = sa.table(
        "additionals",
        sa.column("id", sa.Integer),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("unit_price", sa.Numeric),
        sa.column("formula_type", sa.String),
        sa.column("factor_1", sa.Numeric),
        sa.column("active", sa.Boolean),
        sa.column("notes", sa.Text),
    )
    other_costs = sa.table(
        "other_costs",
        sa.column("id", sa.Integer),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("unit_price", sa.Numeric),
        sa.column("calculation_type", sa.String),
        sa.column("active", sa.Boolean),
    )
    op.bulk_insert(
        techniques,
        [
            {
                "id": row_id,
                "code": code,
                "name": name,
                "unit_price": price,
                "formula_type": formula,
                "factor_1": factor_1,
                "factor_2": factor_2,
                "active": True,
            }
            for row_id, code, name, price, formula, factor_1, factor_2 in SEED_TECHNIQUES
        ],
    )
    op.bulk_insert(
        additionals,
        [
            {
                "id": row_id,
                "code": code,
                "name": name,
                "unit_price": price,
                "formula_type": formula,
                "factor_1": factor,
                "active": active,
                "notes": notes,
            }
            for row_id, code, name, price, formula, factor, active, notes in SEED_ADDITIONALS
        ],
    )
    op.bulk_insert(
        other_costs,
        [
            {
                "id": row_id,
                "code": code,
                "name": name,
                "unit_price": price,
                "calculation_type": calculation,
                "active": True,
            }
            for row_id, code, name, price, calculation in SEED_OTHER_COSTS
        ],
    )
    op.execute(
        "SELECT setval(pg_get_serial_sequence('public.techniques', 'id'), "
        "(SELECT MAX(id) FROM public.techniques))"
    )
    op.execute(
        "SELECT setval(pg_get_serial_sequence('public.additionals', 'id'), "
        "(SELECT MAX(id) FROM public.additionals))"
    )
    op.execute(
        "SELECT setval(pg_get_serial_sequence('public.other_costs', 'id'), "
        "(SELECT MAX(id) FROM public.other_costs))"
    )


def downgrade() -> None:
    op.drop_table("quotation_product_price_updates")
    op.drop_table("quotation_other_costs")
    op.drop_table("quotation_additionals")
    op.drop_table("quotation_techniques")
    op.drop_table("quotations")
    op.drop_table("other_costs")
    op.drop_table("additionals")
    op.drop_table("techniques")
    op.drop_constraint(
        "ck_commercial_settings_default_quotation_factor_positive",
        "commercial_settings",
        type_="check",
    )
    op.drop_column("commercial_settings", "default_quotation_factor")
