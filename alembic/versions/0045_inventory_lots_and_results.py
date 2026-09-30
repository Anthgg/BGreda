"""Fase 010P W2 — lotes, resultados, terminado y entregas.

Revision ID: 0045
Revises: 0044
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict

import sqlalchemy as sa

from alembic import op

from app.core.lot_reconciliation import LotReconciliationReport, load_lot_reconciliation

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MOVEMENT_TYPES = (
    "'INITIAL_IMPORT', 'ADJUSTMENT', 'IN', 'OUT', 'PREPARATION_OUT', "
    "'PREPARATION_IN', 'PRODUCTION_OUT', 'PROTOTYPE_OUT', 'PRODUCTION_IN', 'DELIVERY_OUT'"
)
_MOVEMENT_TYPES_BEFORE = (
    "'INITIAL_IMPORT', 'ADJUSTMENT', 'IN', 'OUT', 'PREPARATION_OUT', "
    "'PREPARATION_IN', 'PRODUCTION_OUT', 'PROTOTYPE_OUT'"
)


def _report_json(report: LotReconciliationReport) -> str:
    return json.dumps(asdict(report), default=lambda value: str(value), sort_keys=True)


def upgrade() -> None:
    # La reconciliacion W0 debe leer movimientos historicos reales y no puede
    # evaluarse desde una conexion MockConnection. La migracion falla cerrada
    # al intentar ejecutar SQL offline; el resto de la cadena sigue renderizable.
    context = op.get_context()
    if context.as_sql:
        op.execute(
            sa.text(
                "DO $$ BEGIN RAISE EXCEPTION "
                "'0045 requiere ejecucion online para reconciliar lotes historicos'; END $$;"
            )
        )
        return

    # ``preparation_id`` ya significa el lote que se esta produciendo en un
    # PREPARATION_OUT historico. Este campo aditivo permite conservar ese
    # vinculo y guardar, a la vez, el lote preparado que se consume.
    op.add_column(
        "stock_movements",
        sa.Column("source_preparation_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_stock_movements_source_preparation_id_recipe_preparations",
        "stock_movements",
        "recipe_preparations",
        ["source_preparation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_stock_movements_source_preparation_id",
        "stock_movements",
        ["source_preparation_id"],
    )
    op.add_column(
        "production_consumptions", sa.Column("preparation_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_production_consumptions_preparation_id_recipe_preparations",
        "production_consumptions",
        "recipe_preparations",
        ["preparation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_production_consumptions_preparation_id",
        "production_consumptions",
        ["preparation_id"],
    )
    op.add_column(
        "prototype_material_lines", sa.Column("preparation_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_prototype_material_lines_preparation_id_recipe_preparations",
        "prototype_material_lines",
        "recipe_preparations",
        ["preparation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_prototype_material_lines_preparation_id",
        "prototype_material_lines",
        ["preparation_id"],
    )
    op.add_column(
        "stock_movements",
        sa.Column("v2_quotation_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_stock_movements_v2_quotation_id_v2_quotations",
        "stock_movements",
        "v2_quotations",
        ["v2_quotation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_stock_movements_v2_quotation_id", "stock_movements", ["v2_quotation_id"])
    op.drop_constraint("movement_type_allowed", "stock_movements", type_="check")
    op.create_check_constraint(
        "movement_type_allowed",
        "stock_movements",
        f"movement_type IN ({_MOVEMENT_TYPES})",
    )

    op.add_column(
        "products",
        sa.Column("source_v2_quotation_product_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_products_source_v2_quotation_product_v2_quotation_products",
        "products",
        "v2_quotation_products",
        ["source_v2_quotation_product_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_products_source_v2_quotation_product",
        "products",
        ["source_v2_quotation_product_id"],
    )

    op.create_table(
        "stock_lot_balances",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("preparation_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("uom_code", sa.String(length=32), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_stock_lot_balances"),
        sa.ForeignKeyConstraint(
            ["preparation_id"],
            ["recipe_preparations.id"],
            ondelete="RESTRICT",
            name="fk_stock_lot_balances_preparation_id_recipe_preparations",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            ondelete="RESTRICT",
            name="fk_stock_lot_balances_product_id_products",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["stock_locations.id"],
            ondelete="RESTRICT",
            name="fk_stock_lot_balances_location_id_stock_locations",
        ),
        sa.ForeignKeyConstraint(
            ["uom_code"],
            ["units_of_measure.code"],
            ondelete="RESTRICT",
            name="fk_stock_lot_balances_uom_code_units_of_measure",
        ),
        sa.UniqueConstraint(
            "preparation_id", "location_id", name="uq_stock_lot_balances_preparation_location"
        ),
        sa.CheckConstraint("quantity >= 0", name="quantity_not_negative"),
    )
    op.create_index(
        "ix_stock_lot_balances_product_location",
        "stock_lot_balances",
        ["product_id", "location_id"],
    )

    op.create_table(
        "production_order_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("production_order_id", sa.Integer(), nullable=False),
        sa.Column("production_order_line_id", sa.Integer(), nullable=True),
        sa.Column("v2_quotation_product_id", sa.Integer(), nullable=True),
        sa.Column("v2_firing_quotation_line_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("started_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("good_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("scrap_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("scrap_reason", sa.String(length=240), nullable=True),
        sa.Column("recorded_by", sa.Uuid(), nullable=True),
        sa.Column("recorded_by_name", sa.String(length=120), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_production_order_results"),
        sa.ForeignKeyConstraint(
            ["production_order_id"],
            ["production_orders.id"],
            ondelete="RESTRICT",
            name="fk_por_order",
        ),
        sa.ForeignKeyConstraint(
            ["production_order_line_id"],
            ["production_order_lines.id"],
            ondelete="RESTRICT",
            name="fk_por_legacy_line",
        ),
        sa.ForeignKeyConstraint(
            ["v2_quotation_product_id"],
            ["v2_quotation_products.id"],
            ondelete="RESTRICT",
            name="fk_por_v2_product",
        ),
        sa.ForeignKeyConstraint(
            ["v2_firing_quotation_line_id"],
            ["v2_firing_quotation_lines.id"],
            ondelete="RESTRICT",
            name="fk_por_firing_line",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            ondelete="RESTRICT",
            name="fk_production_order_results_product_id_products",
        ),
        sa.CheckConstraint(
            "num_nonnulls(production_order_line_id, v2_quotation_product_id, "
            "v2_firing_quotation_line_id) = 1",
            name="exactly_one_origin_line",
        ),
        sa.CheckConstraint("started_quantity >= 0", name="started_non_negative"),
        sa.CheckConstraint("good_quantity >= 0", name="good_non_negative"),
        sa.CheckConstraint("scrap_quantity >= 0", name="scrap_non_negative"),
        sa.CheckConstraint(
            "good_quantity + scrap_quantity = started_quantity", name="result_matches_started"
        ),
        sa.UniqueConstraint(
            "production_order_id",
            "production_order_line_id",
            name="uq_production_results_order_legacy_line",
        ),
        sa.UniqueConstraint(
            "production_order_id",
            "v2_quotation_product_id",
            name="uq_production_results_order_v2_line",
        ),
        sa.UniqueConstraint(
            "production_order_id",
            "v2_firing_quotation_line_id",
            name="uq_production_results_order_firing_line",
        ),
    )

    op.create_table(
        "prototype_results",
        sa.Column("prototype_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("started_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("good_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("scrap_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("scrap_reason", sa.String(length=240), nullable=True),
        sa.Column("recorded_by", sa.Uuid(), nullable=True),
        sa.Column("recorded_by_name", sa.String(length=120), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("prototype_id", name="pk_prototype_results"),
        sa.ForeignKeyConstraint(
            ["prototype_id"], ["prototypes.id"], ondelete="RESTRICT", name="fk_pr_prototype"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], ondelete="RESTRICT", name="fk_pr_product"
        ),
        sa.CheckConstraint("started_quantity >= 0", name="started_non_negative"),
        sa.CheckConstraint("good_quantity >= 0", name="good_non_negative"),
        sa.CheckConstraint("scrap_quantity >= 0", name="scrap_non_negative"),
        sa.CheckConstraint(
            "good_quantity + scrap_quantity = started_quantity", name="result_matches_started"
        ),
    )

    # ProductCategory no tiene código: display_path es su clave natural y
    # única. Se conserva cualquier categoría equivalente ya existente.
    op.execute(
        sa.text(
            """
            INSERT INTO product_categories (name, parent_id, display_path, active)
            SELECT 'Piezas personalizadas', NULL, 'Piezas personalizadas', TRUE
             WHERE NOT EXISTS (
                 SELECT 1 FROM product_categories
                  WHERE lower(name) = lower('Piezas personalizadas')
             )
            ON CONFLICT DO NOTHING
            """
        )
    )

    connection = op.get_bind()
    report, uom_codes = load_lot_reconciliation(connection)
    if report.blocks_migration:
        raise RuntimeError("LOT_RECONCILIATION_REPORT bloquea 0045: " + _report_json(report))
    missing_uom = sorted({lot.product_id for lot in report.lot_balances} - set(uom_codes))
    if missing_uom:
        raise RuntimeError(
            "LOT_RECONCILIATION_REPORT bloquea 0045: preparados sin unidad base "
            + ", ".join(map(str, missing_uom))
        )

    for lot in report.lot_balances:
        connection.execute(
            sa.text(
                """
                INSERT INTO stock_lot_balances
                    (preparation_id, product_id, location_id, quantity, uom_code)
                VALUES (:preparation_id, :product_id, :location_id, :quantity, :uom_code)
                """
            ),
            {
                "preparation_id": lot.preparation_id,
                "product_id": lot.product_id,
                "location_id": lot.location_id,
                "quantity": lot.quantity,
                "uom_code": uom_codes[lot.product_id],
            },
        )

    divergences = (
        connection.execute(
            sa.text(
                """
            SELECT b.product_id, b.location_id,
                   b.quantity AS aggregate_balance,
                   COALESCE(sum(l.quantity), 0) AS lot_balance
              FROM stock_balances AS b
              JOIN products AS p ON p.id = b.product_id
              LEFT JOIN stock_lot_balances AS l
                ON l.product_id = b.product_id AND l.location_id = b.location_id
             WHERE p.product_type = 'PREPARED_MATERIAL'
             GROUP BY b.product_id, b.location_id, b.quantity
            HAVING b.quantity <> COALESCE(sum(l.quantity), 0)
            """
            )
        )
        .mappings()
        .all()
    )
    if divergences:
        raise RuntimeError(
            "LOT_RECONCILIATION_REPORT diverge tras backfill: "
            + json.dumps([dict(row) for row in divergences], default=str, sort_keys=True)
        )
    op.get_context().config.print_stdout("LOT_RECONCILIATION_REPORT: " + _report_json(report))


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE resultados bigint; resultados_prototipo bigint;
                    nuevos_movimientos bigint; productos_medida bigint;
                    consumos_lote bigint; prototipos_lote bigint;
            BEGIN
                SELECT count(*) INTO resultados FROM production_order_results;
                SELECT count(*) INTO resultados_prototipo FROM prototype_results;
                SELECT count(*) INTO nuevos_movimientos FROM stock_movements
                 WHERE movement_type IN ('PRODUCTION_IN', 'DELIVERY_OUT')
                    OR source_preparation_id IS NOT NULL OR v2_quotation_id IS NOT NULL;
                SELECT count(*) INTO productos_medida FROM products
                 WHERE source_v2_quotation_product_id IS NOT NULL;
                SELECT count(*) INTO consumos_lote FROM production_consumptions
                 WHERE preparation_id IS NOT NULL;
                SELECT count(*) INTO prototipos_lote FROM prototype_material_lines
                 WHERE preparation_id IS NOT NULL;
                IF resultados > 0 OR resultados_prototipo > 0 OR nuevos_movimientos > 0
                   OR productos_medida > 0
                   OR consumos_lote > 0 OR prototipos_lote > 0 THEN
                    RAISE EXCEPTION
                        'No se puede bajar 0045: resultados %, resultados de prototipo %, '
                        'movimientos nuevos %, '
                        'productos a medida %, consumos por lote %, prototipos por lote %',
                        resultados, resultados_prototipo, nuevos_movimientos, productos_medida,
                        consumos_lote, prototipos_lote;
                END IF;
            END $$;
            """
        )
    )
    op.drop_table("prototype_results")
    op.drop_table("production_order_results")
    op.drop_index(
        "ix_prototype_material_lines_preparation_id", table_name="prototype_material_lines"
    )
    op.drop_constraint(
        "fk_prototype_material_lines_preparation_id_recipe_preparations",
        "prototype_material_lines",
        type_="foreignkey",
    )
    op.drop_column("prototype_material_lines", "preparation_id")
    op.drop_index("ix_production_consumptions_preparation_id", table_name="production_consumptions")
    op.drop_constraint(
        "fk_production_consumptions_preparation_id_recipe_preparations",
        "production_consumptions",
        type_="foreignkey",
    )
    op.drop_column("production_consumptions", "preparation_id")
    op.drop_index("ix_stock_lot_balances_product_location", table_name="stock_lot_balances")
    op.drop_table("stock_lot_balances")
    op.drop_constraint("uq_products_source_v2_quotation_product", "products", type_="unique")
    op.drop_constraint(
        "fk_products_source_v2_quotation_product_v2_quotation_products",
        "products",
        type_="foreignkey",
    )
    op.drop_column("products", "source_v2_quotation_product_id")
    op.drop_constraint("movement_type_allowed", "stock_movements", type_="check")
    op.create_check_constraint(
        "movement_type_allowed",
        "stock_movements",
        f"movement_type IN ({_MOVEMENT_TYPES_BEFORE})",
    )
    op.drop_index("ix_stock_movements_v2_quotation_id", table_name="stock_movements")
    op.drop_constraint(
        "fk_stock_movements_v2_quotation_id_v2_quotations",
        "stock_movements",
        type_="foreignkey",
    )
    op.drop_column("stock_movements", "v2_quotation_id")
    op.drop_index("ix_stock_movements_source_preparation_id", table_name="stock_movements")
    op.drop_constraint(
        "fk_stock_movements_source_preparation_id_recipe_preparations",
        "stock_movements",
        type_="foreignkey",
    )
    op.drop_column("stock_movements", "source_preparation_id")
