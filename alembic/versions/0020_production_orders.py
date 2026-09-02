"""Ordenes de produccion y consumo fisico de material preparado (Fase 009I).

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-01

Hasta aqui el sistema sabia cotizar y sabia preparar material, pero nada unia
las dos cosas: confirmar una cotizacion no movia un gramo, y el unico consumo
de inventario venia de importar, ajustar a mano o preparar una receta.

Esta migracion crea el documento que falta —la orden de produccion— y le abre
una puerta propia al inventario:

- `production_orders`      : la decision de fabricar UNA cotizacion confirmada.
- `production_order_lines` : que fabricar, copiado de la cotizacion.
- `stock_movements.production_order_id` : de que orden salio cada consumo.
- `MovementType.PRODUCTION_OUT`         : consumo de produccion, no una merma.
- `SequenceType.PRODUCTION_ORDER`       : el correlativo OP-2026-000001.

Toda ella es ADITIVA. Un backend anterior a 009I sobre este esquema sigue
sirviendo: no se renombra ni se estrecha ninguna columna existente, no se anade
ningun NOT NULL sin default a una tabla en uso, y las restricciones que se
reemplazan solo se AMPLIAN (aceptan lo que aceptaban mas un valor nuevo).

Sin backfill. Las 18 cotizaciones confirmadas que ya existen no reciben orden
de produccion: nadie ha decidido fabricarlas, y crearsela seria inventar una
decision productiva que no tomo ninguna persona.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Coherencia entre el estado de la orden y sus tres fechas.
#:
#: Los `status IS NOT NULL` de cada rama NO sobran. En SQL `NULL = 'CREATED'`
#: no es FALSE sino NULL, y un CHECK que evalua a NULL se da por CUMPLIDO: un
#: OR de ramas con igualdad deja pasar cualquier fila cuyo discriminante sea
#: nulo. Es el mismo agujero que 0017 abrio en el tipo de cambio y que 0019
#: volvio a abrir en el eje de pago, las dos veces por esta misma forma.
#:
#: Aqui `status` es NOT NULL, asi que hoy el guardia es redundante. Se escribe
#: igual: la correccion de una restriccion no debe depender de que otra
#: clausula, en otra parte del esquema, siga estando manana.
STATUS_TIMESTAMPS_COHERENT = """
    (status IS NOT NULL AND status = 'CREATED'
     AND started_at IS NULL AND completed_at IS NULL AND cancelled_at IS NULL)
    OR (status IS NOT NULL AND status = 'STARTED'
        AND started_at IS NOT NULL AND completed_at IS NULL AND cancelled_at IS NULL)
    OR (status IS NOT NULL AND status = 'COMPLETED'
        AND started_at IS NOT NULL AND completed_at IS NOT NULL AND cancelled_at IS NULL)
    OR (status IS NOT NULL AND status = 'CANCELLED'
        AND started_at IS NULL AND completed_at IS NULL AND cancelled_at IS NOT NULL)
"""

#: Los tipos de movimiento aceptados DESPUES de esta migracion. Los seis
#: anteriores siguen enteros: los 7 movimientos historicos de carga inicial y
#: cualquier preparacion pasada continuan siendo validos.
MOVEMENT_TYPES_ALLOWED = (
    "movement_type IN ('INITIAL_IMPORT', 'ADJUSTMENT', 'IN', 'OUT', "
    "'PREPARATION_OUT', 'PREPARATION_IN', 'PRODUCTION_OUT')"
)

SEQUENCE_TYPES_ALLOWED = (
    "sequence_type IN ('QUOTE', 'FIRING', 'PRODUCT_50', 'PRODUCT_70', "
    "'PREPARATION', 'PRODUCTION_ORDER')"
)

SEQUENCE_TYPES_BEFORE = (
    "sequence_type IN ('QUOTE', 'FIRING', 'PRODUCT_50', 'PRODUCT_70', 'PREPARATION')"
)

MOVEMENT_TYPES_BEFORE = (
    "movement_type IN ('INITIAL_IMPORT', 'ADJUSTMENT', 'IN', 'OUT', "
    "'PREPARATION_OUT', 'PREPARATION_IN')"
)


def upgrade() -> None:
    # ---- 1. La orden -----------------------------------------------------
    op.create_table(
        "production_orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(64), nullable=False),
        # UNIQUE, y la unicidad la impone la base y no el servicio: dos
        # peticiones simultaneas pasan las dos cualquier comprobacion previa y
        # crearian dos ordenes del mismo pedido, cada una dispuesta a consumir
        # el material entero.
        sa.Column("quotation_id", sa.Integer(), nullable=False),
        sa.Column("stock_location_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'CREATED'")),
        sa.Column("idempotency_key", sa.String(64), nullable=True),
        sa.Column("qr_token", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_name", sa.String(120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["quotation_id"],
            ["quotations.id"],
            name=op.f("fk_production_orders_quotation_id_quotations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["stock_location_id"],
            ["stock_locations.id"],
            name=op.f("fk_production_orders_stock_location_id_stock_locations"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("code", name=op.f("uq_production_orders_code")),
        sa.UniqueConstraint("quotation_id", name=op.f("uq_production_orders_quotation_id")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_production_orders_idempotency_key")),
        sa.UniqueConstraint("qr_token", name=op.f("uq_production_orders_qr_token")),
        sa.CheckConstraint(
            "status IN ('CREATED', 'STARTED', 'COMPLETED', 'CANCELLED')",
            name=op.f("ck_production_orders_status_allowed"),
        ),
        sa.CheckConstraint(
            STATUS_TIMESTAMPS_COHERENT,
            name=op.f("ck_production_orders_status_timestamps_coherent"),
        ),
        sa.CheckConstraint(
            "length(btrim(code)) > 0", name=op.f("ck_production_orders_code_not_blank")
        ),
        # El token del QR no puede ser un correlativo corto disfrazado.
        sa.CheckConstraint(
            "length(btrim(qr_token)) >= 32",
            name=op.f("ck_production_orders_qr_token_long_enough"),
        ),
    )
    op.create_index("ix_production_orders_status", "production_orders", ["status"])
    op.create_index(
        "ix_production_orders_stock_location_id", "production_orders", ["stock_location_id"]
    )

    # ---- 2. Las lineas ---------------------------------------------------
    op.create_table(
        "production_order_lines",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_order_id", sa.Integer(), nullable=False),
        sa.Column("quotation_item_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("product_name_snapshot", sa.String(200), nullable=False),
        sa.Column("product_internal_reference_snapshot", sa.String(64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("width_snapshot", sa.Numeric(18, 6), nullable=True),
        sa.Column("height_snapshot", sa.Numeric(18, 6), nullable=True),
        sa.Column("length_snapshot", sa.Numeric(18, 6), nullable=True),
        sa.Column("depth_snapshot", sa.Numeric(18, 6), nullable=True),
        sa.Column("recipe_id", sa.Integer(), nullable=True),
        sa.Column("recipe_version_id", sa.Integer(), nullable=True),
        sa.Column("recipe_version_fingerprint_snapshot", sa.String(64), nullable=True),
        sa.Column("material_grams_per_piece", sa.Numeric(18, 6), nullable=True),
        sa.Column("prepared_product_id", sa.Integer(), nullable=True),
        sa.Column("required_material_quantity", sa.Numeric(24, 12), nullable=True),
        sa.Column("required_material_uom", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["production_order_id"],
            ["production_orders.id"],
            name=op.f("fk_production_order_lines_production_order_id_production_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["quotation_item_id"],
            ["quotation_items.id"],
            name=op.f("fk_production_order_lines_quotation_item_id_quotation_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_production_order_lines_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipes.id"],
            name=op.f("fk_production_order_lines_recipe_id_recipes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_version_id"],
            ["recipe_versions.id"],
            name=op.f("fk_production_order_lines_recipe_version_id_recipe_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prepared_product_id"],
            ["products.id"],
            name=op.f("fk_production_order_lines_prepared_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "production_order_id", "quotation_item_id", name="uq_production_order_lines_item"
        ),
        sa.UniqueConstraint(
            "production_order_id", "sort_order", name="uq_production_order_lines_sort_order"
        ),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity > 0",
            name=op.f("ck_production_order_lines_quantity_positive"),
        ),
        sa.CheckConstraint(
            "material_grams_per_piece IS NULL OR material_grams_per_piece > 0",
            name=op.f("ck_production_order_lines_material_grams_positive"),
        ),
        sa.CheckConstraint(
            "required_material_quantity IS NULL OR required_material_quantity >= 0",
            name=op.f("ck_production_order_lines_required_material_non_negative"),
        ),
    )
    op.create_index(
        "ix_production_order_lines_production_order_id",
        "production_order_lines",
        ["production_order_id"],
    )
    op.create_index(
        "ix_production_order_lines_quotation_item", "production_order_lines", ["quotation_item_id"]
    )
    op.create_index(
        "ix_production_order_lines_prepared_product_id",
        "production_order_lines",
        ["prepared_product_id"],
    )

    # ---- 3. El movimiento sabe de que orden salio ------------------------
    op.add_column("stock_movements", sa.Column("production_order_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("fk_stock_movements_production_order_id_production_orders"),
        "stock_movements",
        "production_orders",
        ["production_order_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_stock_movements_production_order_id", "stock_movements", ["production_order_id"]
    )

    # Se AMPLIA el CHECK, no se cambia: los seis tipos anteriores siguen
    # aceptados, de modo que los 7 movimientos de carga inicial que hay en
    # produccion y cualquier preparacion pasada continuan siendo validos.
    op.drop_constraint(
        op.f("ck_stock_movements_movement_type_allowed"), "stock_movements", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_stock_movements_movement_type_allowed"),
        "stock_movements",
        MOVEMENT_TYPES_ALLOWED,
    )

    # ---- 4. El correlativo de la orden -----------------------------------
    # 24 y no 16: "PRODUCTION_ORDER" mide exactamente 16 y cabria por los
    # pelos. Ensanchar un varchar no reescribe la tabla y evita que el
    # siguiente tipo de documento reviente al insertar.
    op.alter_column(
        "document_sequences",
        "sequence_type",
        existing_type=sa.String(16),
        type_=sa.String(24),
        existing_nullable=False,
    )
    op.alter_column(
        "document_sequence_issues",
        "sequence_type",
        existing_type=sa.String(16),
        type_=sa.String(24),
        existing_nullable=False,
    )
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint("type_allowed", "document_sequences", SEQUENCE_TYPES_ALLOWED)

    # Siembra idempotente del contador: OP-2026-000001. Misma convencion
    # documental que CTZ, HR y PREP.
    op.execute(
        sa.text(
            """
            INSERT INTO document_sequences
                (sequence_type, prefix, pattern, padding, reset_policy,
                 current_value, period_key, active)
            SELECT 'PRODUCTION_ORDER', 'OP', :pattern, 6, 'YEARLY', 0, '', true
            WHERE NOT EXISTS (
                SELECT 1 FROM document_sequences WHERE sequence_type = 'PRODUCTION_ORDER'
            )
            """
        ).bindparams(pattern="{PREFIX}-{YYYY}-{NUMBER}")
    )

    # Sin backfill de ordenes. Ver el encabezado: nadie ha decidido fabricar
    # las 18 confirmadas que existen, y crearles una orden seria inventar una
    # decision productiva que no tomo ninguna persona.


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM document_sequences WHERE sequence_type = 'PRODUCTION_ORDER'"))
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint("type_allowed", "document_sequences", SEQUENCE_TYPES_BEFORE)
    op.alter_column(
        "document_sequence_issues",
        "sequence_type",
        existing_type=sa.String(24),
        type_=sa.String(16),
        existing_nullable=False,
    )
    op.alter_column(
        "document_sequences",
        "sequence_type",
        existing_type=sa.String(24),
        type_=sa.String(16),
        existing_nullable=False,
    )

    op.drop_constraint(
        op.f("ck_stock_movements_movement_type_allowed"), "stock_movements", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_stock_movements_movement_type_allowed"),
        "stock_movements",
        MOVEMENT_TYPES_BEFORE,
    )
    op.drop_index("ix_stock_movements_production_order_id", table_name="stock_movements")
    op.drop_constraint(
        op.f("fk_stock_movements_production_order_id_production_orders"),
        "stock_movements",
        type_="foreignkey",
    )
    op.drop_column("stock_movements", "production_order_id")

    op.drop_table("production_order_lines")
    op.drop_table("production_orders")
