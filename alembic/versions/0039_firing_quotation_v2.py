"""Fase 010K — Solo Quema V2: el servicio de quema como documento propio.

Hoja «Solo Quema» del Excel final: el cliente trae piezas hechas y se cotiza
solo la quema (y, si quiere, el vidriado). No es una cotizacion de fabricacion,
asi que no cabe en `v2_quotations`: esa tabla exige factor >= 2, pasta y mano de
obra, y aqui el factor va de x1,00 a x2,00 y no hay fabricacion.

Lo aditivo:

    v2_firing_quotations                              la cotizacion (Q-V2-AAAA-NNNNNN)
    v2_firing_quotation_lines                         las piezas: cantidad y medidas
    v2_commercial_settings.firing_service_factor_default    x1,00 por defecto
    document_sequences 'FIRING_V2'                  talonario propio, prefijo Q-V2

Y una correccion de 0038 sin editarla: su CHECK
`line_illustration_quantity_non_negative` pasaba de 63 caracteres y quedo en la
base con un nombre recortado (`..._non_f4ba`), distinto del modelo. Se
renombra a `line_illustration_qty_non_negative`, que cabe.

No se toca produccion (010I): la quema real de un servicio es la
planificacion de hornadas de 010L.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tipos de secuencia antes y despues. Escritos a mano: una migracion describe
#: lo que hizo aquel dia aunque el modelo cambie despues.
_TIPOS_ANTES = (
    "'QUOTE', 'FIRING', 'PRODUCT_50', 'PRODUCT_70', 'PREPARATION', "
    "'PRODUCTION_ORDER', 'PROTOTYPE', 'PROTOTYPE_QUOTE', 'QUOTE_V2'"
)
_TIPOS_DESPUES = f"{_TIPOS_ANTES}, 'FIRING_V2'"

#: El mismo CHECK de coherencia de estados que `v2_quotations` (fase 010H).
_CICLO_DE_VIDA = (
    "(status IS NOT NULL AND status = 'DRAFT'"
    " AND issued_at IS NULL AND valid_until IS NULL AND expires_at IS NULL"
    " AND commercial_fingerprint IS NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'CONFIRMED'"
    " AND issued_at IS NOT NULL AND valid_until IS NOT NULL AND expires_at IS NOT NULL"
    " AND commercial_fingerprint IS NOT NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'CANCELLED' AND cancelled_at IS NOT NULL"
    " AND ((issued_at IS NULL AND valid_until IS NULL AND expires_at IS NULL)"
    "   OR (issued_at IS NOT NULL AND valid_until IS NOT NULL AND expires_at IS NOT NULL"
    "       AND commercial_fingerprint IS NOT NULL)))"
)

_CK_ILUS_VIEJO = "ck_v2_quotation_products_line_illustration_quantity_non_f4ba"
_CK_ILUS_NUEVO = "line_illustration_qty_non_negative"
_CK_FACTOR_AJUSTES = "firing_service_factor_range"


def upgrade() -> None:
    # 1. El talonario Q-V2.
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint(
        "type_allowed", "document_sequences", f"sequence_type IN ({_TIPOS_DESPUES})"
    )
    op.execute(
        sa.text(
            """
            INSERT INTO document_sequences
                (sequence_type, prefix, pattern, padding, reset_policy,
                 current_value, period_key, active)
            SELECT 'FIRING_V2', 'Q-V2', :pattern, 6, 'YEARLY', 0, '', true
            WHERE NOT EXISTS (
                SELECT 1 FROM document_sequences WHERE sequence_type = 'FIRING_V2'
            )
            """
        ).bindparams(pattern="{PREFIX}-{YYYY}-{NUMBER}")
    )

    # 2. El factor por defecto del servicio.
    op.add_column(
        "v2_commercial_settings",
        sa.Column(
            "firing_service_factor_default",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("1.00"),
        ),
    )
    op.create_check_constraint(
        _CK_FACTOR_AJUSTES,
        "v2_commercial_settings",
        "firing_service_factor_default >= 1 AND firing_service_factor_default <= 2",
    )

    # 3. Las tablas del servicio.
    op.create_table(
        "v2_firing_quotations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'DRAFT'"),
            nullable=False,
        ),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("customer_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column("customer_document_type_snapshot", sa.String(length=16), nullable=True),
        sa.Column("customer_document_number_snapshot", sa.String(length=20), nullable=True),
        sa.Column("customer_address_snapshot", sa.String(length=240), nullable=True),
        sa.Column("customer_email_snapshot", sa.String(length=160), nullable=True),
        sa.Column("customer_phone_snapshot", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("client_notes", sa.Text(), nullable=True),
        sa.Column(
            "customer_kind",
            sa.String(length=16),
            server_default=sa.text("'EXTERNAL'"),
            nullable=False,
        ),
        sa.Column("currency_code_snapshot", sa.String(length=3), nullable=True),
        sa.Column("currency_symbol_snapshot", sa.String(length=8), nullable=True),
        sa.Column("exchange_rate_snapshot", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("tax_percent_snapshot", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("rounding_step_snapshot", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("validity_days_snapshot", sa.Integer(), nullable=True),
        sa.Column("settings_version_snapshot", sa.Integer(), nullable=True),
        sa.Column("kiln_id", sa.Integer(), nullable=True),
        sa.Column("kiln_name_snapshot", sa.String(length=120), nullable=True),
        sa.Column("kiln_capacity_snapshot", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column(
            "firing_mode",
            sa.String(length=16),
            server_default=sa.text("'SHARED'"),
            nullable=False,
        ),
        sa.Column("low_fire_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "high_fire_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "piece_separation_cm",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("3"),
            nullable=False,
        ),
        sa.Column("gas_cost_low_snapshot", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("gas_cost_high_snapshot", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("commercial_rate_low_snapshot", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column(
            "commercial_rate_high_snapshot", sa.Numeric(precision=18, scale=6), nullable=True
        ),
        sa.Column(
            "total_volume_cm3",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "occupancy_percent",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("firing_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "billed_load",
            sa.Numeric(precision=24, scale=12),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "firing_commercial_total",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "firing_gas_total",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("glaze_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "glaze_grams",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "glaze_cost_source",
            sa.String(length=16),
            server_default=sa.text("'MASTER'"),
            nullable=False,
        ),
        sa.Column("glaze_material_id", sa.Integer(), nullable=True),
        sa.Column("glaze_material_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column("glaze_manual_cost_per_gram", sa.Numeric(precision=24, scale=12), nullable=True),
        sa.Column(
            "glaze_cost_per_gram_snapshot", sa.Numeric(precision=24, scale=12), nullable=True
        ),
        sa.Column(
            "glaze_material_cost",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "glaze_labor_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("glaze_labor_worker_id", sa.Integer(), nullable=True),
        sa.Column("glaze_labor_technique_id", sa.Integer(), nullable=True),
        sa.Column(
            "glaze_labor_quantity",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("glaze_labor_worker_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column("glaze_labor_worker_type_snapshot", sa.String(length=16), nullable=True),
        sa.Column("glaze_labor_technique_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column(
            "glaze_labor_capacity_snapshot", sa.Numeric(precision=18, scale=6), nullable=True
        ),
        sa.Column(
            "glaze_labor_workday_hours_snapshot", sa.Numeric(precision=18, scale=6), nullable=True
        ),
        sa.Column(
            "glaze_labor_hourly_rate_snapshot", sa.Numeric(precision=24, scale=12), nullable=True
        ),
        sa.Column(
            "glaze_labor_hours",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "glaze_labor_cost",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "factor", sa.Numeric(precision=18, scale=6), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "base_amount",
            sa.Numeric(precision=36, scale=18),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "commercial_price",
            sa.Numeric(precision=36, scale=18),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "subtotal_amount",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "tax_amount",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_amount",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "real_cost_total",
            sa.Numeric(precision=36, scale=18),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "estimated_profit",
            sa.Numeric(precision=36, scale=18),
            server_default=sa.text("0"),
            nullable=False,
        ),
        # Ancha a proposito: una perdida grande da un margen de -2892 % y el
        # tope de 999,999999 reventaria la fila en vez de avisar.
        sa.Column(
            "effective_margin_percent",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_by_name", sa.String(length=200), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_by", sa.UUID(), nullable=True),
        sa.Column("issued_by_name", sa.String(length=200), nullable=True),
        sa.Column("commercial_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.UUID(), nullable=True),
        sa.Column("cancelled_by_name", sa.String(length=200), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("duplicated_from_id", sa.Integer(), nullable=True),
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
        sa.CheckConstraint(_CICLO_DE_VIDA, name=op.f("ck_v2_firing_quotations_lifecycle_coherent")),
        sa.CheckConstraint(
            "customer_kind IN ('EXTERNAL', 'STUDENT')",
            name=op.f("ck_v2_firing_quotations_customer_kind_allowed"),
        ),
        sa.CheckConstraint(
            "firing_mode IN ('SHARED', 'EXCLUSIVE')",
            name=op.f("ck_v2_firing_quotations_firing_mode_allowed"),
        ),
        sa.CheckConstraint(
            "glaze_cost_source IN ('MASTER', 'MANUAL')",
            name=op.f("ck_v2_firing_quotations_glaze_source_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'CONFIRMED', 'CANCELLED')",
            name=op.f("ck_v2_firing_quotations_status_allowed"),
        ),
        sa.CheckConstraint(
            "billed_load >= 0", name=op.f("ck_v2_firing_quotations_billed_load_non_negative")
        ),
        sa.CheckConstraint(
            "duplicated_from_id IS NULL OR duplicated_from_id <> id",
            name=op.f("ck_v2_firing_quotations_not_duplicated_from_itself"),
        ),
        sa.CheckConstraint(
            "exchange_rate_snapshot IS NULL OR exchange_rate_snapshot > 0",
            name=op.f("ck_v2_firing_quotations_exchange_rate_positive"),
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR issued_at IS NULL OR expires_at > issued_at",
            name=op.f("ck_v2_firing_quotations_expires_after_issue"),
        ),
        sa.CheckConstraint(
            "factor >= 1 AND factor <= 2", name=op.f("ck_v2_firing_quotations_factor_range")
        ),
        sa.CheckConstraint(
            "firing_commercial_total >= 0 AND firing_gas_total >= 0",
            name=op.f("ck_v2_firing_quotations_firing_amounts_non_negative"),
        ),
        sa.CheckConstraint(
            "firing_count >= 0", name=op.f("ck_v2_firing_quotations_firing_count_non_negative")
        ),
        sa.CheckConstraint(
            "glaze_enabled OR (glaze_material_cost = 0)",
            name=op.f("ck_v2_firing_quotations_glaze_off_costs_nothing"),
        ),
        sa.CheckConstraint(
            "glaze_grams >= 0", name=op.f("ck_v2_firing_quotations_glaze_grams_non_negative")
        ),
        sa.CheckConstraint(
            "glaze_labor_enabled OR (glaze_labor_hours = 0 AND glaze_labor_cost = 0)",
            name=op.f("ck_v2_firing_quotations_glaze_labor_off_costs_nothing"),
        ),
        sa.CheckConstraint(
            "glaze_labor_quantity >= 0",
            name=op.f("ck_v2_firing_quotations_glaze_labor_quantity_non_negative"),
        ),
        sa.CheckConstraint(
            "glaze_manual_cost_per_gram IS NULL OR glaze_manual_cost_per_gram >= 0",
            name=op.f("ck_v2_firing_quotations_glaze_manual_cost_non_negative"),
        ),
        sa.CheckConstraint(
            "glaze_material_cost >= 0 AND glaze_labor_hours >= 0 AND glaze_labor_cost >= 0",
            name=op.f("ck_v2_firing_quotations_glaze_amounts_non_negative"),
        ),
        sa.CheckConstraint(
            "kiln_capacity_snapshot IS NULL OR kiln_capacity_snapshot > 0",
            name=op.f("ck_v2_firing_quotations_kiln_capacity_positive"),
        ),
        sa.CheckConstraint(
            "occupancy_percent >= 0", name=op.f("ck_v2_firing_quotations_occupancy_non_negative")
        ),
        sa.CheckConstraint(
            "piece_separation_cm >= 0 AND piece_separation_cm <= 20",
            name=op.f("ck_v2_firing_quotations_piece_separation_range"),
        ),
        sa.CheckConstraint(
            "subtotal_amount >= 0 AND tax_amount >= 0 AND total_amount >= 0",
            name=op.f("ck_v2_firing_quotations_document_amounts_non_negative"),
        ),
        sa.CheckConstraint(
            "total_volume_cm3 >= 0", name=op.f("ck_v2_firing_quotations_volume_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["partners.id"],
            name=op.f("fk_v2_firing_quotations_customer_id_partners"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["duplicated_from_id"],
            ["v2_firing_quotations.id"],
            name=op.f("fk_v2_firing_quotations_duplicated_from_id_v2_firing_quotations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["glaze_labor_technique_id"],
            ["v2_techniques.id"],
            name=op.f("fk_v2_firing_quotations_glaze_labor_technique_id_v2_techniques"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["glaze_labor_worker_id"],
            ["v2_workers.id"],
            name=op.f("fk_v2_firing_quotations_glaze_labor_worker_id_v2_workers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["glaze_material_id"],
            ["products.id"],
            name=op.f("fk_v2_firing_quotations_glaze_material_id_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["kiln_id"],
            ["kilns.id"],
            name=op.f("fk_v2_firing_quotations_kiln_id_kilns"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_v2_firing_quotations")),
        sa.UniqueConstraint("code", name=op.f("uq_v2_firing_quotations_code")),
    )
    op.create_index(
        "ix_v2_firing_quotations_created_at", "v2_firing_quotations", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_v2_firing_quotations_customer_id"),
        "v2_firing_quotations",
        ["customer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_v2_firing_quotations_duplicated_from_id"),
        "v2_firing_quotations",
        ["duplicated_from_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_v2_firing_quotations_kiln_id"), "v2_firing_quotations", ["kiln_id"], unique=False
    )
    op.create_index(
        "ix_v2_firing_quotations_status", "v2_firing_quotations", ["status"], unique=False
    )
    op.create_index(
        "uq_v2_firing_quotations_open_duplicate",
        "v2_firing_quotations",
        ["duplicated_from_id"],
        unique=True,
        postgresql_where=sa.text("duplicated_from_id IS NOT NULL AND status = 'DRAFT'"),
    )
    op.create_table(
        "v2_firing_quotation_lines",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("v2_firing_quotation_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("product_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column("quantity", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("length_cm", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("width_cm", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("height_cm", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column(
            "unit_volume_cm3",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_volume_cm3",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "volume_share_percent",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "height_cm IS NULL OR height_cm > 0",
            name=op.f("ck_v2_firing_quotation_lines_height_positive"),
        ),
        sa.CheckConstraint(
            "length_cm IS NULL OR length_cm > 0",
            name=op.f("ck_v2_firing_quotation_lines_length_positive"),
        ),
        sa.CheckConstraint(
            "quantity >= 0", name=op.f("ck_v2_firing_quotation_lines_quantity_non_negative")
        ),
        sa.CheckConstraint(
            "unit_volume_cm3 >= 0 AND total_volume_cm3 >= 0",
            name=op.f("ck_v2_firing_quotation_lines_volumes_non_negative"),
        ),
        sa.CheckConstraint(
            "volume_share_percent >= 0 AND volume_share_percent <= 100",
            name=op.f("ck_v2_firing_quotation_lines_volume_share_range"),
        ),
        sa.CheckConstraint(
            "width_cm IS NULL OR width_cm > 0",
            name=op.f("ck_v2_firing_quotation_lines_width_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_v2_firing_quotation_lines_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["v2_firing_quotation_id"],
            ["v2_firing_quotations.id"],
            name=op.f("fk_v2_firing_quotation_lines_quotation_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_v2_firing_quotation_lines")),
    )
    op.create_index(
        op.f("ix_v2_firing_quotation_lines_product_id"),
        "v2_firing_quotation_lines",
        ["product_id"],
        unique=False,
    )
    op.create_index(
        "ix_v2_firing_quotation_lines_service",
        "v2_firing_quotation_lines",
        ["v2_firing_quotation_id", "sort_order"],
        unique=False,
    )

    # 4. El CHECK de 0038 con nombre recortado, al nombre que cabe.
    op.drop_constraint(op.f(_CK_ILUS_VIEJO), "v2_quotation_products", type_="check")
    op.create_check_constraint(
        _CK_ILUS_NUEVO, "v2_quotation_products", "illustration_quantity >= 0"
    )


def downgrade() -> None:
    # Bajar borraria cotizaciones de Solo Quema y los correlativos Q-V2 ya
    # entregados. Se aborta diciendo cuantas hay.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                servicios integer;
                entregados integer;
            BEGIN
                SELECT count(*) INTO servicios FROM v2_firing_quotations;
                SELECT count(*) INTO entregados
                FROM document_sequence_issues WHERE sequence_type = 'FIRING_V2';
                IF servicios > 0 OR entregados > 0 THEN
                    RAISE EXCEPTION USING MESSAGE =
                        format('No se puede bajar de 0039: %s servicios de Solo Quema', servicios)
                        || format(' y %s correlativos Q-V2 entregados', entregados);
                END IF;
            END $$;
            """
        )
    )
    op.drop_constraint(_CK_ILUS_NUEVO, "v2_quotation_products", type_="check")
    op.execute(
        sa.text(
            f'ALTER TABLE v2_quotation_products ADD CONSTRAINT "{_CK_ILUS_VIEJO}"'
            " CHECK (illustration_quantity >= 0)"
        )
    )

    op.drop_table("v2_firing_quotation_lines")
    op.drop_table("v2_firing_quotations")

    op.drop_constraint(_CK_FACTOR_AJUSTES, "v2_commercial_settings", type_="check")
    op.drop_column("v2_commercial_settings", "firing_service_factor_default")

    op.execute(sa.text("DELETE FROM document_sequences WHERE sequence_type = 'FIRING_V2'"))
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint(
        "type_allowed", "document_sequences", f"sequence_type IN ({_TIPOS_ANTES})"
    )
