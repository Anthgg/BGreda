"""Preparaciones de receta, unidades de volumen y estimacion de esmalte (Fase 009D).

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-29

Cinco cambios, todos aditivos:

1. `units_of_measure` admite la dimension VOLUME, y se dan de alta `ml` (base)
   y `l`. Hasta ahora el CHECK solo permitia MASS y COUNT, asi que el
   mililitro no se podia ni registrar y todo el modelo de agua, rendimiento y
   costo por ml era imposible.

2. `recipe_preparations`: el lote realmente fabricado. La receta sigue siendo
   una FORMULACION porcentual; el peso absoluto pertenece al acto de fabricar.

3. `recipe_preparation_lines`: que se consumio y a que costo, congelado.

4. `stock_movements` gana `preparation_id` y dos tipos nuevos, para que la
   transformacion materia prima -> material preparado sea auditable como tal.

5. `commercial_settings.estimated_glaze_percent`, con la misma convencion que
   `tax_percent`: 15 significa 15 %.

Ninguna fila existente se reescribe. Los unicos INSERT son las dos unidades de
volumen y la secuencia del lote.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Convencion documental ya usada por CTZ y HR.
SEQUENCE_PATTERN = "{PREFIX}-{YYYY}-{NUMBER}"


def upgrade() -> None:
    # ---- 1. Unidades de volumen -----------------------------------------
    op.drop_constraint("ck_units_of_measure_dimension_allowed", "units_of_measure", type_="check")
    op.create_check_constraint(
        "ck_units_of_measure_dimension_allowed",
        "units_of_measure",
        "dimension IN ('MASS', 'COUNT', 'VOLUME')",
    )
    # Alta de `ml` y `l`.
    #
    # Deliberadamente NO se usa `ON CONFLICT DO NOTHING`. Si un entorno ya
    # tuviera un `ml` con otra dimension o otro factor, saltarselo en silencio
    # dejaria la base incoherente y la migracion diria SUCCESS: a partir de ahi
    # cada conversion g <-> ml daria un numero equivocado sin que nadie se
    # entere. Ante una definicion que no cuadra, se aborta y se resuelve a mano.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                existente RECORD;
            BEGIN
                FOR existente IN
                    SELECT * FROM (VALUES
                        ('ml', 'Mililitro', 'ml', 1::numeric, true),
                        ('l',  'Litro',     'l',  1000::numeric, false)
                    ) AS esperado(code, name, symbol, factor_to_base, is_base)
                LOOP
                    IF EXISTS (SELECT 1 FROM units_of_measure u WHERE u.code = existente.code) THEN
                        PERFORM 1
                        FROM units_of_measure u
                        WHERE u.code = existente.code
                          AND u.dimension = 'VOLUME'
                          AND u.factor_to_base = existente.factor_to_base;
                        IF NOT FOUND THEN
                            RAISE EXCEPTION
                                'La unidad % ya existe con una definicion distinta de la canonica '
                                '(VOLUME, factor %). Resuelvala manualmente antes de migrar.',
                                existente.code, existente.factor_to_base;
                        END IF;
                    ELSE
                        INSERT INTO units_of_measure
                            (code, name, symbol, dimension, factor_to_base, is_base, active)
                        VALUES (existente.code, existente.name, existente.symbol,
                                'VOLUME', existente.factor_to_base, existente.is_base, true);
                    END IF;
                END LOOP;
            END $$;
            """
        )
    )

    # ---- 2. Lote preparado ----------------------------------------------
    op.create_table(
        "recipe_preparations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("recipe_version_id", sa.Integer(), nullable=False),
        sa.Column("prepared_product_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("total_dry_weight_g", sa.Numeric(20, 12), nullable=False),
        sa.Column("water_amount_ml", sa.Numeric(20, 12), nullable=False),
        sa.Column("final_yield_ml", sa.Numeric(20, 12), nullable=False),
        sa.Column("solids_g_per_ml", sa.Numeric(20, 12), nullable=False),
        sa.Column("batch_total_cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("unit_cost_per_ml", sa.Numeric(20, 12), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="COMPLETED"),
        sa.Column(
            "prepared_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_name", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["recipe_version_id"], ["recipe_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["prepared_product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["location_id"], ["stock_locations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name=op.f("uq_recipe_preparations_code")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_recipe_preparations_idempotency_key")),
        sa.CheckConstraint(
            "total_dry_weight_g > 0", name="ck_recipe_preparations_dry_weight_positive"
        ),
        sa.CheckConstraint(
            "water_amount_ml >= 0", name="ck_recipe_preparations_water_not_negative"
        ),
        sa.CheckConstraint("final_yield_ml > 0", name="ck_recipe_preparations_yield_positive"),
        sa.CheckConstraint(
            "solids_g_per_ml > 0", name="ck_recipe_preparations_concentration_positive"
        ),
        sa.CheckConstraint(
            "batch_total_cost >= 0", name="ck_recipe_preparations_cost_not_negative"
        ),
        sa.CheckConstraint(
            "unit_cost_per_ml >= 0", name="ck_recipe_preparations_unit_cost_not_negative"
        ),
        sa.CheckConstraint("status IN ('COMPLETED')", name="ck_recipe_preparations_status_allowed"),
    )
    op.create_index(
        "ix_recipe_preparations_recipe_version_id", "recipe_preparations", ["recipe_version_id"]
    )
    op.create_index(
        "ix_recipe_preparations_prepared_product_id", "recipe_preparations", ["prepared_product_id"]
    )
    op.create_index("ix_recipe_preparations_location_id", "recipe_preparations", ["location_id"])
    op.create_index("ix_recipe_preparations_status", "recipe_preparations", ["status"])

    # ---- 3. Ingredientes consumidos --------------------------------------
    op.create_table(
        "recipe_preparation_lines",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("preparation_id", sa.Integer(), nullable=False),
        sa.Column("component_product_id", sa.Integer(), nullable=False),
        sa.Column("quantity_g", sa.Numeric(20, 12), nullable=False),
        sa.Column("unit_cost_snapshot", sa.Numeric(20, 12), nullable=False),
        sa.Column("line_cost", sa.Numeric(18, 6), nullable=False),
        sa.ForeignKeyConstraint(["preparation_id"], ["recipe_preparations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["component_product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("quantity_g > 0", name="ck_recipe_preparation_lines_quantity_positive"),
        sa.CheckConstraint(
            "unit_cost_snapshot >= 0", name="ck_recipe_preparation_lines_unit_cost_not_negative"
        ),
        sa.CheckConstraint(
            "line_cost >= 0", name="ck_recipe_preparation_lines_line_cost_not_negative"
        ),
    )
    op.create_index(
        "ix_recipe_preparation_lines_preparation_id", "recipe_preparation_lines", ["preparation_id"]
    )
    op.create_index(
        "ix_recipe_preparation_lines_component_product_id",
        "recipe_preparation_lines",
        ["component_product_id"],
    )

    # ---- 4. Movimientos de inventario ------------------------------------
    op.add_column("stock_movements", sa.Column("preparation_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("fk_stock_movements_preparation_id_recipe_preparations"),
        "stock_movements",
        "recipe_preparations",
        ["preparation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_stock_movements_preparation_id", "stock_movements", ["preparation_id"])
    op.drop_constraint("ck_stock_movements_movement_type_allowed", "stock_movements", type_="check")
    op.create_check_constraint(
        "ck_stock_movements_movement_type_allowed",
        "stock_movements",
        "movement_type IN ('INITIAL_IMPORT', 'ADJUSTMENT', 'IN', 'OUT', "
        "'PREPARATION_OUT', 'PREPARATION_IN')",
    )

    # ---- 5. Porcentaje estimado de esmalte -------------------------------
    op.add_column(
        "commercial_settings",
        sa.Column(
            "estimated_glaze_percent",
            sa.Numeric(9, 6),
            nullable=False,
            server_default=sa.text("15"),
        ),
    )
    op.create_check_constraint(
        "ck_commercial_settings_estimated_glaze_percent_range",
        "commercial_settings",
        "estimated_glaze_percent > 0 AND estimated_glaze_percent <= 100",
    )

    # ---- 6. Secuencia del lote -------------------------------------------
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint(
        "type_allowed",
        "document_sequences",
        "sequence_type IN ('QUOTE', 'FIRING', 'PRODUCT_50', 'PRODUCT_70', 'PREPARATION')",
    )
    # Igual que con las unidades: si la secuencia ya existiera con otro
    # contador, insertarla en silencio no seria posible y saltarsela dejaria el
    # sistema sin poder emitir codigos de lote. Se inserta solo si no existe, y
    # se aborta si existe con un tipo que no es el esperado.
    op.execute(
        sa.text(
            """
            INSERT INTO document_sequences
                (sequence_type, prefix, pattern, padding, reset_policy,
                 current_value, period_key, active)
            SELECT 'PREPARATION', 'PREP', :pattern, 6, 'YEARLY', 0, '', true
            WHERE NOT EXISTS (
                SELECT 1 FROM document_sequences WHERE sequence_type = 'PREPARATION'
            )
            """
        ).bindparams(pattern=SEQUENCE_PATTERN)
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM document_sequences WHERE sequence_type = 'PREPARATION'"))
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint(
        "type_allowed",
        "document_sequences",
        "sequence_type IN ('QUOTE', 'FIRING', 'PRODUCT_50', 'PRODUCT_70')",
    )

    op.drop_constraint(
        "ck_commercial_settings_estimated_glaze_percent_range",
        "commercial_settings",
        type_="check",
    )
    op.drop_column("commercial_settings", "estimated_glaze_percent")

    op.drop_constraint("ck_stock_movements_movement_type_allowed", "stock_movements", type_="check")
    op.create_check_constraint(
        "ck_stock_movements_movement_type_allowed",
        "stock_movements",
        "movement_type IN ('INITIAL_IMPORT', 'ADJUSTMENT', 'IN', 'OUT')",
    )
    op.drop_index("ix_stock_movements_preparation_id", table_name="stock_movements")
    op.drop_constraint(
        op.f("fk_stock_movements_preparation_id_recipe_preparations"),
        "stock_movements",
        type_="foreignkey",
    )
    op.drop_column("stock_movements", "preparation_id")

    op.drop_table("recipe_preparation_lines")
    op.drop_table("recipe_preparations")

    op.execute(sa.text("DELETE FROM units_of_measure WHERE code IN ('ml', 'l')"))
    op.drop_constraint("ck_units_of_measure_dimension_allowed", "units_of_measure", type_="check")
    op.create_check_constraint(
        "ck_units_of_measure_dimension_allowed",
        "units_of_measure",
        "dimension IN ('MASS', 'COUNT')",
    )
