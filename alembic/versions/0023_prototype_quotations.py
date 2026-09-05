"""Fase 009K.1.1 — la cotizacion de prototipo como documento propio.

Un prototipo se cotiza antes de fabricarse: cuanto cuesta y cuanto tarda. Eso
es un documento comercial con cliente, moneda, correlativo, confirmacion, PDF y
cobro, y hasta ahora no existia en ninguna parte.

**Puramente aditiva.** No toca `quotations`, ni `quotation_items`, ni una sola
columna de 0022. El backend 0022 sigue funcionando contra este esquema porque
todo lo que se anade es nuevo o admite nulo:

- tabla `prototype_quotations` y su tabla de materiales
- cinco tarifas por defecto en `commercial_settings`, con `server_default` 0
- `prototypes.prototype_quotation_id` NULL
- el tipo de secuencia `PROTOTYPE_QUOTE` con su fila `CPR`

El unico CHECK que se reescribe es el de `document_sequences.sequence_type`, y
solo para AMPLIARLO: acepta lo que aceptaba mas el tipo nuevo. Es el mismo
movimiento que hizo 0021 al anadir `PROTOTYPE`.

Nada se rellena hacia atras. Las muestras que ya existen se quedan con
`prototype_quotation_id` nulo, que es la verdad: nacieron antes de que hubiera
cotizacion de prototipo, y ponerles una calculada con las tarifas de hoy seria
inventar un precio que nadie acordo.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


SEQUENCE_TYPES_BEFORE = (
    "'QUOTE', 'FIRING', 'PRODUCT_50', 'PRODUCT_70', 'PREPARATION', 'PRODUCTION_ORDER', 'PROTOTYPE'"
)
SEQUENCE_TYPES_AFTER = f"{SEQUENCE_TYPES_BEFORE}, 'PROTOTYPE_QUOTE'"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. El documento
    # ------------------------------------------------------------------
    op.create_table(
        "prototype_quotations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Nulo mientras es borrador: un documento que nunca se emite no debe
        # gastar un numero del talonario.
        sa.Column("code", sa.String(length=32), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("customer_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'DRAFT'")
        ),
        sa.Column(
            "payment_status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'UNPAID'"),
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("width_cm", sa.Numeric(18, 6), nullable=True),
        sa.Column("length_cm", sa.Numeric(18, 6), nullable=True),
        sa.Column("height_cm", sa.Numeric(18, 6), nullable=True),
        sa.Column("depth_cm", sa.Numeric(18, 6), nullable=True),
        sa.Column("technical_specifications", postgresql.JSONB(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        # Entradas del costeo
        sa.Column("design_days", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("design_rate_override", sa.Numeric(18, 6), nullable=True),
        sa.Column("artist_days", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("artist_rate_override", sa.Numeric(18, 6), nullable=True),
        sa.Column("mold_maker_partner_id", sa.Integer(), nullable=True),
        sa.Column("mold_maker_price_override", sa.Numeric(18, 6), nullable=True),
        sa.Column(
            "mold_maker_days", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("kiln_id", sa.Integer(), nullable=True),
        sa.Column("firing_type", sa.String(length=8), nullable=True),
        sa.Column("firing_batches", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("drying_days", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "adjustment_days", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("fixed_cost_override", sa.Numeric(18, 6), nullable=True),
        # Politica monetaria congelada al confirmar
        sa.Column("currency_code_snapshot", sa.String(length=3), nullable=True),
        sa.Column("currency_symbol_snapshot", sa.String(length=8), nullable=True),
        sa.Column("exchange_rate_snapshot", sa.Numeric(18, 6), nullable=True),
        sa.Column("tax_percent_snapshot", sa.Numeric(9, 4), nullable=True),
        sa.Column("rounding_step_snapshot", sa.Numeric(18, 6), nullable=True),
        sa.Column("rounding_source_snapshot", sa.String(length=32), nullable=True),
        # Resultado congelado
        sa.Column("cost_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("commercial_net_total", sa.Numeric(18, 6), nullable=True),
        sa.Column("commercial_tax_total", sa.Numeric(18, 6), nullable=True),
        sa.Column("commercial_gross_total", sa.Numeric(18, 6), nullable=True),
        sa.Column("estimated_days", sa.Numeric(18, 6), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_name", sa.String(length=200), nullable=True),
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
        sa.ForeignKeyConstraint(["customer_id"], ["partners.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["mold_maker_partner_id"], ["partners.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["kiln_id"], ["kilns.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("code", name="uq_prototype_quotations_code"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'CONFIRMED', 'CANCELLED')", name="pq_status_allowed"
        ),
        sa.CheckConstraint(
            "payment_status IN ('UNPAID', 'PAID')", name="pq_payment_status_allowed"
        ),
        sa.CheckConstraint(
            "firing_type IS NULL OR firing_type IN ('BAJA', 'ALTA')", name="pq_firing_type_allowed"
        ),
        sa.CheckConstraint("quantity > 0", name="pq_quantity_positive"),
        sa.CheckConstraint("design_days >= 0", name="pq_design_days_non_negative"),
        sa.CheckConstraint("artist_days >= 0", name="pq_artist_days_non_negative"),
        sa.CheckConstraint("mold_maker_days >= 0", name="pq_mold_maker_days_non_negative"),
        sa.CheckConstraint("drying_days >= 0", name="pq_drying_days_non_negative"),
        sa.CheckConstraint("adjustment_days >= 0", name="pq_adjustment_days_non_negative"),
        sa.CheckConstraint("firing_batches >= 0", name="pq_firing_batches_non_negative"),
        sa.CheckConstraint(
            "status <> 'CONFIRMED' OR code IS NOT NULL", name="pq_confirmed_has_code"
        ),
    )
    op.create_index("ix_prototype_quotations_customer_id", "prototype_quotations", ["customer_id"])
    op.create_index("ix_prototype_quotations_product_id", "prototype_quotations", ["product_id"])
    op.create_index("ix_prototype_quotations_status", "prototype_quotations", ["status"])

    # ------------------------------------------------------------------
    # 2. Los materiales previstos, como lineas y no como un JSON opaco
    # ------------------------------------------------------------------
    op.create_table(
        "prototype_quotation_materials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("prototype_quotation_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("quantity_per_prototype", sa.Numeric(18, 6), nullable=False),
        sa.Column("uom_code", sa.String(length=32), nullable=False),
        sa.Column("unit_cost_snapshot", sa.Numeric(18, 6), nullable=True),
        sa.Column("product_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column(
            "is_body_material", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
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
            ["prototype_quotation_id"], ["prototype_quotations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("quantity_per_prototype > 0", name="pqm_quantity_positive"),
        sa.CheckConstraint(
            "unit_cost_snapshot IS NULL OR unit_cost_snapshot >= 0",
            name="pqm_unit_cost_non_negative",
        ),
    )
    op.create_index(
        "ix_prototype_quotation_materials_quotation",
        "prototype_quotation_materials",
        ["prototype_quotation_id"],
    )

    # ------------------------------------------------------------------
    # 3. Tarifas por defecto del prototipo
    # ------------------------------------------------------------------
    # Nacen en CERO. Un valor de arranque inventado se convierte en un precio
    # real en cuanto alguien cotiza sin mirar, y los numeros del Excel (80, 100,
    # 350) estan marcados ahi mismo como EJEMPLO. Que la casa escriba los suyos.
    for columna in (
        "prototype_design_rate",
        "prototype_artist_rate",
        "prototype_mold_maker_price",
        "prototype_mold_maker_days",
        "prototype_fixed_cost",
    ):
        op.add_column(
            "commercial_settings",
            sa.Column(columna, sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        )

    # ------------------------------------------------------------------
    # 4. El vinculo con la muestra fisica
    # ------------------------------------------------------------------
    # Convive con `quotation_id`, que sigue siendo el vinculo de 009K. No se
    # renombra ni se sustituye: las muestras que ya existen se explican por ahi.
    op.add_column("prototypes", sa.Column("prototype_quotation_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_prototypes_prototype_quotation_id",
        "prototypes",
        "prototype_quotations",
        ["prototype_quotation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_prototypes_prototype_quotation_id", "prototypes", ["prototype_quotation_id"]
    )

    # ------------------------------------------------------------------
    # 5. El talonario propio: CPR-2026-000001
    # ------------------------------------------------------------------
    # El CHECK solo se AMPLIA: acepta lo que aceptaba mas el tipo nuevo, asi que
    # el backend anterior sigue insertando lo de siempre sin enterarse.
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint(
        "type_allowed", "document_sequences", f"sequence_type IN ({SEQUENCE_TYPES_AFTER})"
    )
    op.execute(
        sa.text(
            """
            INSERT INTO document_sequences
                (sequence_type, prefix, pattern, padding, reset_policy,
                 current_value, period_key, active)
            SELECT 'PROTOTYPE_QUOTE', 'CPR', :pattern, 6, 'YEARLY', 0, '', true
            WHERE NOT EXISTS (
                SELECT 1 FROM document_sequences WHERE sequence_type = 'PROTOTYPE_QUOTE'
            )
            """
        ).bindparams(pattern="{PREFIX}-{YYYY}-{NUMBER}")
    )


def downgrade() -> None:
    """Se niega a revertir si hay algo que perder.

    Una cotizacion de prototipo emitida es un documento con numero que alguien
    pudo enviar a un cliente. Borrar la tabla lo haria desaparecer sin dejar
    rastro, y el correlativo gastado no vuelve.
    """
    conexion = op.get_bind()
    documentos = conexion.scalar(sa.text("SELECT count(*) FROM prototype_quotations")) or 0
    vinculos = (
        conexion.scalar(
            sa.text("SELECT count(*) FROM prototypes WHERE prototype_quotation_id IS NOT NULL")
        )
        or 0
    )
    if documentos or vinculos:
        raise RuntimeError(
            f"0023 no puede revertirse: hay {documentos} cotizacion(es) de prototipo y "
            f"{vinculos} muestra(s) vinculada(s). Anularlas primero si de verdad se quiere."
        )

    op.execute(sa.text("DELETE FROM document_sequences WHERE sequence_type = 'PROTOTYPE_QUOTE'"))
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint(
        "type_allowed", "document_sequences", f"sequence_type IN ({SEQUENCE_TYPES_BEFORE})"
    )

    op.drop_index("ix_prototypes_prototype_quotation_id", table_name="prototypes")
    op.drop_constraint("fk_prototypes_prototype_quotation_id", "prototypes", type_="foreignkey")
    op.drop_column("prototypes", "prototype_quotation_id")

    for columna in (
        "prototype_fixed_cost",
        "prototype_mold_maker_days",
        "prototype_mold_maker_price",
        "prototype_artist_rate",
        "prototype_design_rate",
    ):
        op.drop_column("commercial_settings", columna)

    op.drop_index(
        "ix_prototype_quotation_materials_quotation", table_name="prototype_quotation_materials"
    )
    op.drop_table("prototype_quotation_materials")
    op.drop_index("ix_prototype_quotations_status", table_name="prototype_quotations")
    op.drop_index("ix_prototype_quotations_product_id", table_name="prototype_quotations")
    op.drop_index("ix_prototype_quotations_customer_id", table_name="prototype_quotations")
    op.drop_table("prototype_quotations")
