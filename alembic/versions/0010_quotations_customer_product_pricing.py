"""Fase 005.6 - 005.10: Integracion de cliente, dimensiones tecnicas y precios.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-24

Agrega columnas tecnicas a productos y snapshots completos de cliente, producto y precios
comerciales al cotizador.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Dimensiones tecnicas en products
    op.add_column("products", sa.Column("material", sa.String(length=200), nullable=True))
    op.add_column("products", sa.Column("grammage", sa.Numeric(18, 6), nullable=True))
    op.add_column("products", sa.Column("width", sa.Numeric(18, 6), nullable=True))
    op.add_column("products", sa.Column("height", sa.Numeric(18, 6), nullable=True))
    op.add_column("products", sa.Column("length", sa.Numeric(18, 6), nullable=True))
    op.add_column("products", sa.Column("depth", sa.Numeric(18, 6), nullable=True))

    # 2. Cliente y snapshots en quotations
    op.add_column("quotations", sa.Column("name", sa.String(length=200), nullable=True))
    op.add_column(
        "quotations",
        sa.Column(
            "customer_id",
            sa.Integer(),
            sa.ForeignKey("partners.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index("ix_quotations_customer_id", "quotations", ["customer_id"])

    op.add_column(
        "quotations", sa.Column("customer_name_snapshot", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "quotations",
        sa.Column("customer_trade_name_snapshot", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "quotations",
        sa.Column("customer_document_type_snapshot", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "quotations",
        sa.Column("customer_document_number_snapshot", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "quotations", sa.Column("customer_address_snapshot", sa.String(length=240), nullable=True)
    )
    op.add_column(
        "quotations", sa.Column("customer_ubigeo_snapshot", sa.String(length=120), nullable=True)
    )
    op.add_column(
        "quotations", sa.Column("customer_email_snapshot", sa.String(length=160), nullable=True)
    )
    op.add_column(
        "quotations", sa.Column("customer_phone_snapshot", sa.String(length=64), nullable=True)
    )

    # 3. Snapshots de producto y dimensiones en quotations
    op.add_column(
        "quotations", sa.Column("product_name_snapshot", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "quotations",
        sa.Column("product_internal_reference_snapshot", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "quotations", sa.Column("product_type_snapshot", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "quotations", sa.Column("product_uom_snapshot", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "quotations", sa.Column("product_material_snapshot", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "quotations", sa.Column("product_grammage_snapshot", sa.Numeric(18, 6), nullable=True)
    )
    op.add_column(
        "quotations", sa.Column("product_width_snapshot", sa.Numeric(18, 6), nullable=True)
    )
    op.add_column(
        "quotations", sa.Column("product_height_snapshot", sa.Numeric(18, 6), nullable=True)
    )
    op.add_column(
        "quotations", sa.Column("product_length_snapshot", sa.Numeric(18, 6), nullable=True)
    )
    op.add_column(
        "quotations", sa.Column("product_depth_snapshot", sa.Numeric(18, 6), nullable=True)
    )

    # 4. Costeo interno, ganancia y precios comerciales en quotations
    op.add_column(
        "quotations",
        sa.Column(
            "final_unit_cost",
            sa.Numeric(36, 18),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "final_total_cost",
            sa.Numeric(36, 18),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "markup_percent",
            sa.Numeric(9, 6),
            server_default=sa.text("100"),
            nullable=False,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "target_profit_unit",
            sa.Numeric(36, 18),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "calculated_sale_unit_price",
            sa.Numeric(36, 18),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "suggested_commercial_unit_price",
            sa.Numeric(36, 18),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "commercial_sale_unit_price",
            sa.Numeric(36, 18),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "effective_profit_unit",
            sa.Numeric(36, 18),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "effective_profit_total",
            sa.Numeric(36, 18),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "effective_markup_percent",
            sa.Numeric(9, 6),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "commercial_subtotal",
            sa.Numeric(36, 18),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "commercial_total",
            sa.Numeric(36, 18),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "commercial_unit_price_with_tax",
            sa.Numeric(36, 18),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("quotations", "commercial_unit_price_with_tax")
    op.drop_column("quotations", "commercial_total")
    op.drop_column("quotations", "commercial_subtotal")
    op.drop_column("quotations", "effective_markup_percent")
    op.drop_column("quotations", "effective_profit_total")
    op.drop_column("quotations", "effective_profit_unit")
    op.drop_column("quotations", "commercial_sale_unit_price")
    op.drop_column("quotations", "suggested_commercial_unit_price")
    op.drop_column("quotations", "calculated_sale_unit_price")
    op.drop_column("quotations", "target_profit_unit")
    op.drop_column("quotations", "markup_percent")
    op.drop_column("quotations", "final_total_cost")
    op.drop_column("quotations", "final_unit_cost")

    op.drop_column("quotations", "product_depth_snapshot")
    op.drop_column("quotations", "product_length_snapshot")
    op.drop_column("quotations", "product_height_snapshot")
    op.drop_column("quotations", "product_width_snapshot")
    op.drop_column("quotations", "product_grammage_snapshot")
    op.drop_column("quotations", "product_material_snapshot")
    op.drop_column("quotations", "product_uom_snapshot")
    op.drop_column("quotations", "product_type_snapshot")
    op.drop_column("quotations", "product_internal_reference_snapshot")
    op.drop_column("quotations", "product_name_snapshot")

    op.drop_column("quotations", "customer_phone_snapshot")
    op.drop_column("quotations", "customer_email_snapshot")
    op.drop_column("quotations", "customer_ubigeo_snapshot")
    op.drop_column("quotations", "customer_address_snapshot")
    op.drop_column("quotations", "customer_document_number_snapshot")
    op.drop_column("quotations", "customer_document_type_snapshot")
    op.drop_column("quotations", "customer_trade_name_snapshot")
    op.drop_column("quotations", "customer_name_snapshot")
    op.drop_index("ix_quotations_customer_id", table_name="quotations")
    op.drop_column("quotations", "customer_id")
    op.drop_column("quotations", "name")

    op.drop_column("products", "depth")
    op.drop_column("products", "length")
    op.drop_column("products", "height")
    op.drop_column("products", "width")
    op.drop_column("products", "grammage")
    op.drop_column("products", "material")
