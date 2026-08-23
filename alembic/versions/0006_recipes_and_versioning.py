"""Fase 3.5: recetas, versiones, lineas de componentes y rendimiento.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-22

Crea las tablas para el motor de recetas:
- recipes: cabecera vinculada 1:1 a un producto preparado (PREPARED_MATERIAL).
- recipe_versions: historial inmutable de formulas con versionamiento e indice
  parcial unico para garantizar como maximo una version ACTIVE.
- recipe_lines: componentes clasificados (BASE, COLORANT, ADDITIVE) con porcentaje decimal.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLES = (
    "recipes",
    "recipe_versions",
    "recipe_lines",
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


def upgrade() -> None:
    # 1. recipes
    op.create_table(
        "recipes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("current_version_id", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["public.products.id"],
            name="fk_recipes_product_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_recipes"),
        sa.UniqueConstraint("product_id", name="uq_recipes_product_id"),
    )
    op.create_index("ix_recipes_active", "recipes", ["active"])
    op.create_index("ix_recipes_product_id", "recipes", ["product_id"])

    # 2. recipe_versions
    op.create_table(
        "recipe_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recipe_id", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("yield_factor", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("base_total", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "additional_total",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.CheckConstraint("yield_factor > 0", name="chk_recipe_version_yield_positive"),
        sa.CheckConstraint("version_number > 0", name="chk_recipe_version_number_positive"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'ARCHIVED')",
            name="chk_recipe_version_status",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["public.recipes.id"],
            name="fk_recipe_versions_recipe_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_recipe_versions"),
        sa.UniqueConstraint("recipe_id", "version_number", name="uq_recipe_version_number"),
    )
    op.create_index("ix_recipe_versions_recipe_id", "recipe_versions", ["recipe_id"])
    op.create_index("ix_recipe_versions_status", "recipe_versions", ["status"])
    op.create_index("ix_recipe_versions_fingerprint", "recipe_versions", ["fingerprint"])
    op.create_index(
        "ix_recipe_versions_single_active",
        "recipe_versions",
        ["recipe_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    # Add fk_recipes_current_version_id to recipes
    op.create_foreign_key(
        "fk_recipes_current_version_id",
        "recipes",
        "recipe_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
        use_alter=True,
    )
    op.create_index("ix_recipes_current_version_id", "recipes", ["current_version_id"])

    # 3. recipe_lines
    op.create_table(
        "recipe_lines",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recipe_version_id", sa.Integer(), nullable=False),
        sa.Column("component_product_id", sa.Integer(), nullable=False),
        sa.Column("component_type", sa.String(length=32), nullable=False),
        sa.Column("percentage", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("percentage > 0", name="chk_recipe_line_percentage_positive"),
        sa.CheckConstraint(
            "component_type IN ('BASE', 'COLORANT', 'ADDITIVE')",
            name="chk_recipe_line_component_type",
        ),
        sa.ForeignKeyConstraint(
            ["component_product_id"],
            ["public.products.id"],
            name="fk_recipe_lines_component_product_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_version_id"],
            ["public.recipe_versions.id"],
            name="fk_recipe_lines_recipe_version_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_recipe_lines"),
    )
    op.create_index("ix_recipe_lines_recipe_version_id", "recipe_lines", ["recipe_version_id"])
    op.create_index(
        "ix_recipe_lines_component_product_id", "recipe_lines", ["component_product_id"]
    )

    # Seguridad RLS & Revokes (sin FORCE ROW LEVEL SECURITY, siguiendo 0005)
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;")
        for role in ("anon", "authenticated"):
            op.execute(_REVOKE_SQL.format(table=table, role=role))


def downgrade() -> None:
    op.drop_constraint("fk_recipes_current_version_id", "recipes", type_="foreignkey")
    op.drop_table("recipe_lines")
    op.drop_table("recipe_versions")
    op.drop_table("recipes")
