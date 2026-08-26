"""Fase 005.11: Cotizador multiproducto y estimacion productiva.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-24

La cabecera ``quotations`` se conserva para no romper el flujo historico. Las
lineas comerciales y productivas pasan a ``quotation_items``. Las cotizaciones
existentes se copian como una linea LEGACY; sus columnas originales no se
eliminan en esta transicion.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CALC = sa.Numeric(precision=36, scale=18)
PCT = sa.Numeric(precision=9, scale=6)
QUANTITY = sa.Numeric(precision=18, scale=6)

_REVOKE_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
        REVOKE ALL ON TABLE public.quotation_items FROM {role};
    END IF;
END
$$
"""


def upgrade() -> None:
    op.add_column(
        "quotations",
        sa.Column(
            "workflow",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'LEGACY'"),
        ),
    )
    op.create_check_constraint(
        "workflow_allowed",
        "quotations",
        "workflow IN ('LEGACY', 'COTIZADOR')",
    )
    op.create_index("ix_quotations_workflow", "quotations", ["workflow"])
    op.alter_column("quotations", "product_id", existing_type=sa.Integer(), nullable=True)
    op.alter_column("quotations", "quantity", existing_type=sa.Integer(), nullable=True)

    op.create_table(
        "quotation_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "quotation_id",
            sa.Integer(),
            sa.ForeignKey("quotations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("product_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("product_internal_reference_snapshot", sa.String(length=64), nullable=False),
        sa.Column("product_type_snapshot", sa.String(length=32), nullable=False),
        sa.Column("product_uom_snapshot", sa.String(length=32), nullable=True),
        sa.Column("product_material_snapshot", sa.String(length=200), nullable=True),
        sa.Column("product_grammage_snapshot", QUANTITY, nullable=True),
        sa.Column("product_width_snapshot", QUANTITY, nullable=True),
        sa.Column("product_height_snapshot", QUANTITY, nullable=True),
        sa.Column("product_length_snapshot", QUANTITY, nullable=True),
        sa.Column("product_depth_snapshot", QUANTITY, nullable=True),
        sa.Column(
            "recipe_id",
            sa.Integer(),
            sa.ForeignKey("recipes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "recipe_version_id",
            sa.Integer(),
            sa.ForeignKey("recipe_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("recipe_version_fingerprint_snapshot", sa.String(length=64), nullable=True),
        sa.Column("material_grams_per_piece", QUANTITY, nullable=True),
        sa.Column(
            "kiln_id",
            sa.Integer(),
            sa.ForeignKey("kilns.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "kiln_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "production_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "techniques_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "additionals_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "other_costs_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("materials_calculated", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column("materials_applied", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column("firing_cost", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column("labor_cost", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column("calculated_days", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("days_adjustment", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("waiting_days", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_days", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("space_cost", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column("final_unit_cost", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column("final_total_cost", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column("markup_percent", PCT, nullable=False, server_default=sa.text("100")),
        sa.Column("calculated_sale_unit_price", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "suggested_commercial_unit_price",
            CALC,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("commercial_sale_unit_price", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column("effective_profit_unit", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column("effective_profit_total", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column("effective_markup_percent", PCT, nullable=False, server_default=sa.text("0")),
        sa.Column("commercial_subtotal", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column("tax_percentage_snapshot", PCT, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "tax_rate_source_snapshot",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'COMMERCIAL_SETTINGS'"),
        ),
        sa.Column("tax_amount", CALC, nullable=False, server_default=sa.text("0")),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "calculation_warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("quotation_id", "sort_order", name="uq_quotation_items_sort_order"),
        sa.CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positive"),
        sa.CheckConstraint("materials_calculated >= 0", name="materials_calculated_non_negative"),
        sa.CheckConstraint("materials_applied >= 0", name="materials_applied_non_negative"),
        sa.CheckConstraint("firing_cost >= 0", name="firing_cost_non_negative"),
        sa.CheckConstraint("labor_cost >= 0", name="labor_cost_non_negative"),
        sa.CheckConstraint("waiting_days >= 0", name="waiting_days_non_negative"),
        sa.CheckConstraint("total_days >= 0", name="total_days_non_negative"),
        sa.CheckConstraint("space_cost >= 0", name="space_cost_non_negative"),
        sa.CheckConstraint("markup_percent >= 0", name="markup_non_negative"),
    )
    op.create_index("ix_quotation_items_quotation_id", "quotation_items", ["quotation_id"])
    op.create_index("ix_quotation_items_product_id", "quotation_items", ["product_id"])
    op.create_index("ix_quotation_items_recipe_id", "quotation_items", ["recipe_id"])
    op.create_index(
        "ix_quotation_items_recipe_version_id", "quotation_items", ["recipe_version_id"]
    )
    op.create_index("ix_quotation_items_kiln_id", "quotation_items", ["kiln_id"])
    op.create_index(
        "ix_quotation_items_quotation_product",
        "quotation_items",
        ["quotation_id", "product_id"],
    )

    # Transicion sin perdida: cada cotizacion de una pieza gana una linea. La
    # cabecera legacy y sus tablas de detalle se conservan por compatibilidad.
    op.execute(
        """
        INSERT INTO quotation_items (
            quotation_id, product_id, sort_order, quantity,
            product_name_snapshot, product_internal_reference_snapshot,
            product_type_snapshot, product_uom_snapshot, product_material_snapshot,
            product_grammage_snapshot, product_width_snapshot, product_height_snapshot,
            product_length_snapshot, product_depth_snapshot,
            recipe_id, recipe_version_id, recipe_version_fingerprint_snapshot,
            material_grams_per_piece, kiln_id, production_snapshot,
            materials_calculated, materials_applied, firing_cost, labor_cost,
            calculated_days, days_adjustment, waiting_days, total_days, space_cost,
            final_unit_cost, final_total_cost, markup_percent,
            calculated_sale_unit_price, suggested_commercial_unit_price,
            commercial_sale_unit_price, effective_profit_unit, effective_profit_total,
            effective_markup_percent, commercial_subtotal,
            tax_percentage_snapshot, tax_rate_source_snapshot, tax_amount,
            source_fingerprint,
            calculation_warnings, created_at, updated_at
        )
        SELECT
            q.id, q.product_id, 0, q.quantity,
            COALESCE(q.product_name_snapshot, p.name),
            COALESCE(q.product_internal_reference_snapshot, p.internal_reference),
            COALESCE(q.product_type_snapshot, p.product_type),
            q.product_uom_snapshot, q.product_material_snapshot,
            q.product_grammage_snapshot, q.product_width_snapshot, q.product_height_snapshot,
            q.product_length_snapshot, q.product_depth_snapshot,
            q.recipe_id, q.recipe_version_id, q.recipe_version_fingerprint_snapshot,
            q.material_grams_per_piece,
            fl.factor_kiln_id,
            COALESCE(q.firing_snapshot, '{}'::jsonb),
            q.materials_calculated, q.materials_applied, q.firing_cost, q.labor_cost,
            q.calculated_days, q.days_adjustment, q.waiting_days, q.total_days, q.space_cost,
            q.final_unit_cost, q.final_total_cost, q.markup_percent,
            q.calculated_sale_unit_price, q.suggested_commercial_unit_price,
            q.commercial_sale_unit_price, q.effective_profit_unit, q.effective_profit_total,
            q.effective_markup_percent, q.commercial_subtotal,
            q.tax_percentage_snapshot, q.tax_rate_source_snapshot, q.tax_amount,
            q.source_fingerprint,
            q.calculation_warnings, q.created_at, q.updated_at
        FROM quotations q
        JOIN products p ON p.id = q.product_id
        LEFT JOIN firing_lines fl ON fl.id = q.firing_line_id
        WHERE q.product_id IS NOT NULL
        """
    )

    op.execute("ALTER TABLE public.quotation_items ENABLE ROW LEVEL SECURITY;")
    for role in ("anon", "authenticated"):
        op.execute(_REVOKE_SQL.format(role=role))


def downgrade() -> None:
    # Un downgrade con datos creados por el nuevo flujo perderia N lineas por
    # cabecera. Fallar de forma explicita es mas seguro que destruirlas.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM quotations WHERE workflow = 'COTIZADOR') THEN
                RAISE EXCEPTION '0012 downgrade bloqueado: existen cotizaciones COTIZADOR';
            END IF;
        END
        $$
        """
    )
    op.drop_table("quotation_items")
    op.alter_column("quotations", "quantity", existing_type=sa.Integer(), nullable=False)
    op.alter_column("quotations", "product_id", existing_type=sa.Integer(), nullable=False)
    op.drop_index("ix_quotations_workflow", table_name="quotations")
    op.drop_constraint("workflow_allowed", "quotations", type_="check")
    op.drop_column("quotations", "workflow")
