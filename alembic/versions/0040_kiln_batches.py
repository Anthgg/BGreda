"""Fase 010L — hornadas planificadas, sus asignaciones y el puente Solo Quema.

Lo aditivo:

    document_sequences 'KILN_BATCH', 'INTERNAL_LOAD'   talonarios HOR y CI
    kiln_batches                    un horno + un ciclo + una ejecucion con fecha
    kiln_batch_assignments          cuantas piezas de UNA linea van en UNA hornada
    kiln_batch_operations           idempotencia de las operaciones en lote
    internal_loads / _lines         carga interna del taller, sin cotizacion
    v2_firing_production_handoffs   puente Solo Quema -> produccion (K5)
    production_orders.v2_firing_handoff_id    cuarto origen, UNICO
    trg_kiln_batch_assignments_volume         mantiene el volumen asignado

## La capacidad la garantiza la base

`kiln_batches.assigned_volume_cm3` no lo escribe el servicio: lo mantiene un
TRIGGER que aplica el DELTA de volumen activo de cada escritura sobre las
asignaciones, y un CHECK impide que pase de la capacidad congelada. Delta y no
`SUM`: bajo READ COMMITTED la actualizacion que espera un bloqueo se reevalua
sobre la ULTIMA version de la fila y los deltas componen; una suma leida en una
foto vieja perderia la del otro. Es el primer trigger del esquema, y esta aqui
porque es la unica forma de que la regla valga «venga de donde venga» la
escritura.

## El cuarto origen de la orden

`exactly_one_origin` pasa de tres ramas a cuatro. Cada rama nombra los CUATRO
campos. **Ni una fila se toca**: las ordenes que existen cumplen una de las tres
primeras ramas tal como estan y ninguna recibe `v2_firing_handoff_id`.

## Lo que NO hace

- No reutiliza `firings`: es la hoja de costeo Legacy, con dinero y factor x3,
  y Legacy sigue intacto hasta 010N.
- No edita 0039 ni ninguna fusionada.
- No da de alta Legacy ni prototipos como origenes planificables: no tienen un
  volumen aprobado con separacion.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Escritos a mano: una migracion describe lo que hizo aquel dia aunque el
#: modelo cambie despues.
_TIPOS_ANTES = (
    "'QUOTE', 'FIRING', 'PRODUCT_50', 'PRODUCT_70', 'PREPARATION', "
    "'PRODUCTION_ORDER', 'PROTOTYPE', 'PROTOTYPE_QUOTE', 'QUOTE_V2', 'FIRING_V2'"
)
_TIPOS_DESPUES = f"{_TIPOS_ANTES}, 'KILN_BATCH', 'INTERNAL_LOAD'"

_CK_ORIGEN = "exactly_one_origin"
_ORIGEN_TRES_RAMAS = (
    "(quotation_id IS NOT NULL AND prototype_id IS NULL AND v2_handoff_id IS NULL)"
    " OR (quotation_id IS NULL AND prototype_id IS NOT NULL AND v2_handoff_id IS NULL)"
    " OR (quotation_id IS NULL AND prototype_id IS NULL AND v2_handoff_id IS NOT NULL)"
)
_ORIGEN_CUATRO_RAMAS = (
    "(quotation_id IS NOT NULL AND prototype_id IS NULL"
    " AND v2_handoff_id IS NULL AND v2_firing_handoff_id IS NULL)"
    " OR (quotation_id IS NULL AND prototype_id IS NOT NULL"
    " AND v2_handoff_id IS NULL AND v2_firing_handoff_id IS NULL)"
    " OR (quotation_id IS NULL AND prototype_id IS NULL"
    " AND v2_handoff_id IS NOT NULL AND v2_firing_handoff_id IS NULL)"
    " OR (quotation_id IS NULL AND prototype_id IS NULL"
    " AND v2_handoff_id IS NULL AND v2_firing_handoff_id IS NOT NULL)"
)
_FK_PUENTE_SQ = "fk_production_orders_v2_firing_handoff"
_UQ_PUENTE_SQ = "uq_production_orders_v2_firing_handoff_id"

#: El delta de volumen ACTIVO de una escritura sobre las asignaciones.
#:
#: `vol_activo(fila)` es su volumen si esta ACTIVE y 0 si no, y 0 si la fila no
#: existe (OLD en un INSERT, NEW en un DELETE). Si una UPDATE cambiara de
#: hornada —ningun camino lo hace: mover es liberar en una y dar de alta en
#: otra—, se resta en la vieja y se suma en la nueva.
_FUNCION_VOLUMEN = """
CREATE FUNCTION kiln_batch_assignments_apply_volume() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    viejo numeric := 0;
    nuevo numeric := 0;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.status = 'ACTIVE' THEN
        viejo := OLD.assigned_volume_cm3;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND NEW.status = 'ACTIVE' THEN
        nuevo := NEW.assigned_volume_cm3;
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.batch_id IS DISTINCT FROM NEW.batch_id THEN
        IF viejo <> 0 THEN
            UPDATE kiln_batches
               SET assigned_volume_cm3 = assigned_volume_cm3 - viejo
             WHERE id = OLD.batch_id;
        END IF;
        IF nuevo <> 0 THEN
            UPDATE kiln_batches
               SET assigned_volume_cm3 = assigned_volume_cm3 + nuevo
             WHERE id = NEW.batch_id;
        END IF;
    ELSIF nuevo - viejo <> 0 THEN
        UPDATE kiln_batches
           SET assigned_volume_cm3 = assigned_volume_cm3 + (nuevo - viejo)
         WHERE id = COALESCE(NEW.batch_id, OLD.batch_id);
    END IF;
    RETURN NULL;
END;
$$;
"""

_TRIGGER_VOLUMEN = """
CREATE TRIGGER trg_kiln_batch_assignments_volume
AFTER INSERT OR UPDATE OR DELETE ON kiln_batch_assignments
FOR EACH ROW EXECUTE FUNCTION kiln_batch_assignments_apply_volume();
"""


#: Coherencias largas, escritas a mano e identicas a las del modelo.
_ESTADO_FECHAS = (
    "(status IS NOT NULL AND status = 'PLANNED' AND started_at IS NULL AND completed_at"
    " IS NULL AND cancelled_at IS NULL) OR (status IS NOT NULL AND status = 'STARTED' AND"
    " started_at IS NOT NULL AND completed_at IS NULL AND cancelled_at IS NULL) OR (status"
    " IS NOT NULL AND status = 'COMPLETED' AND started_at IS NOT NULL AND completed_at IS"
    " NOT NULL AND cancelled_at IS NULL) OR (status IS NOT NULL AND status = 'CANCELLED'"
    " AND started_at IS NULL AND completed_at IS NULL AND cancelled_at IS NOT NULL)"
)

_ORIGEN_ASIGNACION = (
    "(source_kind IS NOT NULL AND source_kind = 'V2_QUOTATION' AND production_order_id IS"
    " NOT NULL AND v2_quotation_product_id IS NOT NULL AND v2_firing_quotation_line_id IS"
    " NULL AND internal_load_id IS NULL AND internal_load_line_id IS NULL) OR (source_kind"
    " IS NOT NULL AND source_kind = 'FIRING_V2' AND production_order_id IS NOT NULL AND"
    " v2_firing_quotation_line_id IS NOT NULL AND v2_quotation_product_id IS NULL AND"
    " internal_load_id IS NULL AND internal_load_line_id IS NULL) OR (source_kind IS NOT"
    " NULL AND source_kind = 'INTERNAL' AND internal_load_id IS NOT NULL AND"
    " internal_load_line_id IS NOT NULL AND production_order_id IS NULL AND"
    " v2_quotation_product_id IS NULL AND v2_firing_quotation_line_id IS NULL)"
)

_LIBERACION = (
    "(status IS NOT NULL AND status = 'ACTIVE' AND released_at IS NULL) OR (status IS NOT"
    " NULL AND status = 'RELEASED' AND released_at IS NOT NULL)"
)


def upgrade() -> None:
    # 1. Talonarios.
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint(
        "type_allowed", "document_sequences", f"sequence_type IN ({_TIPOS_DESPUES})"
    )
    for tipo, prefijo in (("KILN_BATCH", "HOR"), ("INTERNAL_LOAD", "CI")):
        op.execute(
            sa.text(
                """
                INSERT INTO document_sequences
                    (sequence_type, prefix, pattern, padding, reset_policy,
                     current_value, period_key, active)
                SELECT :tipo, :prefijo, :pattern, 6, 'YEARLY', 0, '', true
                WHERE NOT EXISTS (
                    SELECT 1 FROM document_sequences WHERE sequence_type = :tipo
                )
                """
            ).bindparams(tipo=tipo, prefijo=prefijo, pattern="{PREFIX}-{YYYY}-{NUMBER}")
        )

    # 2. Tablas (esqueleto de autogenerate, solo lo de 010L).
    op.create_table(
        "internal_loads",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "low_fire_required", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "high_fire_required", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "piece_separation_cm",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_by_name", sa.String(length=120), nullable=True),
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
            "length(btrim(code)) > 0", name=op.f("ck_internal_loads_code_not_blank")
        ),
        sa.CheckConstraint(
            "length(btrim(name)) > 0", name=op.f("ck_internal_loads_name_not_blank")
        ),
        sa.CheckConstraint(
            "low_fire_required OR high_fire_required", name=op.f("ck_internal_loads_some_cycle")
        ),
        sa.CheckConstraint(
            "notes IS NULL OR length(notes) <= 2000", name=op.f("ck_internal_loads_notes_length")
        ),
        sa.CheckConstraint(
            "piece_separation_cm >= 0 AND piece_separation_cm <= 20",
            name=op.f("ck_internal_loads_separation_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_internal_loads")),
        sa.UniqueConstraint("code", name=op.f("uq_internal_loads_code")),
    )
    op.create_table(
        "kiln_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("kiln_id", sa.Integer(), nullable=False),
        sa.Column("firing_type", sa.String(length=8), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'PLANNED'"),
            nullable=False,
        ),
        sa.Column("kiln_name_snapshot", sa.String(length=120), nullable=False),
        sa.Column("capacity_snapshot_cm3", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column(
            "assigned_volume_cm3",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("exclusive", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_by_name", sa.String(length=120), nullable=True),
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
            _ESTADO_FECHAS,
            name=op.f("ck_kiln_batches_status_timestamps_coherent"),
        ),
        sa.CheckConstraint(
            "firing_type IN ('LOW', 'HIGH')", name=op.f("ck_kiln_batches_firing_type_allowed")
        ),
        sa.CheckConstraint(
            "status IN ('PLANNED', 'STARTED', 'COMPLETED', 'CANCELLED')",
            name=op.f("ck_kiln_batches_status_allowed"),
        ),
        sa.CheckConstraint(
            "NOT exclusive OR assigned_volume_cm3 > 0",
            name=op.f("ck_kiln_batches_exclusive_not_empty"),
        ),
        sa.CheckConstraint(
            "assigned_volume_cm3 <= capacity_snapshot_cm3",
            name=op.f("ck_kiln_batches_assigned_within_capacity"),
        ),
        sa.CheckConstraint(
            "assigned_volume_cm3 >= 0", name=op.f("ck_kiln_batches_assigned_non_negative")
        ),
        sa.CheckConstraint(
            "cancel_reason IS NULL OR length(cancel_reason) <= 500",
            name=op.f("ck_kiln_batches_cancel_reason_length"),
        ),
        sa.CheckConstraint(
            "capacity_snapshot_cm3 > 0", name=op.f("ck_kiln_batches_capacity_positive")
        ),
        sa.CheckConstraint("length(btrim(code)) > 0", name=op.f("ck_kiln_batches_code_not_blank")),
        sa.CheckConstraint(
            "notes IS NULL OR length(notes) <= 2000", name=op.f("ck_kiln_batches_notes_length")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_kiln_batches_version_positive")),
        sa.ForeignKeyConstraint(
            ["kiln_id"],
            ["kilns.id"],
            name=op.f("fk_kiln_batches_kiln_id_kilns"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kiln_batches")),
        sa.UniqueConstraint("code", name=op.f("uq_kiln_batches_code")),
    )
    op.create_index(op.f("ix_kiln_batches_kiln_id"), "kiln_batches", ["kiln_id"], unique=False)
    op.create_index(
        op.f("ix_kiln_batches_scheduled_date"), "kiln_batches", ["scheduled_date"], unique=False
    )
    op.create_index(op.f("ix_kiln_batches_status"), "kiln_batches", ["status"], unique=False)
    op.create_table(
        "internal_load_lines",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("load_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("length_cm", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("width_cm", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("height_cm", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("unit_volume_cm3", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("total_volume_cm3", sa.Numeric(precision=18, scale=6), nullable=False),
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
            "length(btrim(name)) > 0", name=op.f("ck_internal_load_lines_name_not_blank")
        ),
        sa.CheckConstraint(
            "length_cm > 0 AND width_cm > 0 AND height_cm > 0",
            name=op.f("ck_internal_load_lines_dimensions_positive"),
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_internal_load_lines_quantity_positive")),
        sa.CheckConstraint(
            "total_volume_cm3 = unit_volume_cm3 * quantity",
            name=op.f("ck_internal_load_lines_total_volume_matches"),
        ),
        sa.CheckConstraint(
            "unit_volume_cm3 > 0", name=op.f("ck_internal_load_lines_unit_volume_positive")
        ),
        sa.ForeignKeyConstraint(
            ["load_id"],
            ["internal_loads.id"],
            name=op.f("fk_internal_load_lines_load_id_internal_loads"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_internal_load_lines_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_internal_load_lines")),
        sa.UniqueConstraint("id", "load_id", name="uq_internal_load_lines_id_load"),
    )
    op.create_index(
        op.f("ix_internal_load_lines_load_id"), "internal_load_lines", ["load_id"], unique=False
    )
    op.create_index(
        op.f("ix_internal_load_lines_product_id"),
        "internal_load_lines",
        ["product_id"],
        unique=False,
    )
    op.create_table(
        "kiln_batch_operations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
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
            "kind IN ('CREATE_BATCH', 'ASSIGN', 'RELEASE', 'MOVE')",
            name=op.f("ck_kiln_batch_operations_kind_allowed"),
        ),
        sa.CheckConstraint(
            "length(btrim(idempotency_key)) >= 8",
            name=op.f("ck_kiln_batch_operations_idempotency_key_long_enough"),
        ),
        sa.CheckConstraint(
            "length(payload_fingerprint) = 64",
            name=op.f("ck_kiln_batch_operations_fingerprint_is_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["kiln_batches.id"],
            name="fk_kiln_batch_operations_batch",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kiln_batch_operations")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_kiln_batch_operations_idempotency_key")
        ),
    )
    op.create_index(
        op.f("ix_kiln_batch_operations_batch_id"),
        "kiln_batch_operations",
        ["batch_id"],
        unique=False,
    )
    op.create_table(
        "v2_firing_production_handoffs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("v2_firing_quotation_id", sa.Integer(), nullable=False),
        sa.Column("commercial_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_by_name", sa.String(length=200), nullable=True),
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
            "length(btrim(commercial_fingerprint)) = 64",
            name=op.f("ck_v2_firing_production_handoffs_fingerprint_is_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["v2_firing_quotation_id"],
            ["v2_firing_quotations.id"],
            name="fk_v2_firing_production_handoffs_quotation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_v2_firing_production_handoffs")),
        sa.UniqueConstraint(
            "v2_firing_quotation_id",
            name=op.f("uq_v2_firing_production_handoffs_v2_firing_quotation_id"),
        ),
    )
    op.create_table(
        "kiln_batch_assignments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column("production_order_id", sa.Integer(), nullable=True),
        sa.Column("internal_load_id", sa.Integer(), nullable=True),
        sa.Column("v2_quotation_product_id", sa.Integer(), nullable=True),
        sa.Column("v2_firing_quotation_line_id", sa.Integer(), nullable=True),
        sa.Column("internal_load_line_id", sa.Integer(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_volume_snapshot_cm3", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("assigned_volume_cm3", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("firing_mode", sa.String(length=16), nullable=False),
        sa.Column("product_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_by_name", sa.String(length=120), nullable=True),
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
            _ORIGEN_ASIGNACION,
            name=op.f("ck_kiln_batch_assignments_source_coherent"),
        ),
        sa.CheckConstraint(
            _LIBERACION,
            name=op.f("ck_kiln_batch_assignments_release_coherent"),
        ),
        sa.CheckConstraint(
            "firing_mode IN ('SHARED', 'EXCLUSIVE')",
            name=op.f("ck_kiln_batch_assignments_firing_mode_allowed"),
        ),
        sa.CheckConstraint(
            "internal_load_id IS NULL OR firing_mode = 'SHARED'",
            name=op.f("ck_kiln_batch_assignments_internal_is_shared"),
        ),
        sa.CheckConstraint(
            "source_kind IN ('V2_QUOTATION', 'FIRING_V2', 'INTERNAL')",
            name=op.f("ck_kiln_batch_assignments_source_kind_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'RELEASED')",
            name=op.f("ck_kiln_batch_assignments_status_allowed"),
        ),
        sa.CheckConstraint(
            "assigned_volume_cm3 = quantity * unit_volume_snapshot_cm3",
            name=op.f("ck_kiln_batch_assignments_assigned_volume_matches"),
        ),
        sa.CheckConstraint(
            "assigned_volume_cm3 > 0",
            name=op.f("ck_kiln_batch_assignments_assigned_volume_positive"),
        ),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_kiln_batch_assignments_quantity_positive")
        ),
        sa.CheckConstraint(
            "unit_volume_snapshot_cm3 > 0",
            name=op.f("ck_kiln_batch_assignments_unit_volume_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["kiln_batches.id"],
            name="fk_kiln_batch_assignments_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["internal_load_id"],
            ["internal_loads.id"],
            name="fk_kiln_batch_assignments_load",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["internal_load_line_id", "internal_load_id"],
            ["internal_load_lines.id", "internal_load_lines.load_id"],
            name="fk_kiln_batch_assignments_load_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["production_order_id"],
            ["production_orders.id"],
            name="fk_kiln_batch_assignments_order",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["v2_firing_quotation_line_id"],
            ["v2_firing_quotation_lines.id"],
            name="fk_kiln_batch_assignments_fq_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["v2_quotation_product_id"],
            ["v2_quotation_products.id"],
            name="fk_kiln_batch_assignments_v2_line",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kiln_batch_assignments")),
    )
    op.create_index(
        op.f("ix_kiln_batch_assignments_batch_id"),
        "kiln_batch_assignments",
        ["batch_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_kiln_batch_assignments_internal_load_id"),
        "kiln_batch_assignments",
        ["internal_load_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_kiln_batch_assignments_internal_load_line_id"),
        "kiln_batch_assignments",
        ["internal_load_line_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_kiln_batch_assignments_production_order_id"),
        "kiln_batch_assignments",
        ["production_order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_kiln_batch_assignments_v2_firing_quotation_line_id"),
        "kiln_batch_assignments",
        ["v2_firing_quotation_line_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_kiln_batch_assignments_v2_quotation_product_id"),
        "kiln_batch_assignments",
        ["v2_quotation_product_id"],
        unique=False,
    )
    op.create_index(
        "uq_kiln_batch_assignments_active_fq",
        "kiln_batch_assignments",
        ["batch_id", "v2_firing_quotation_line_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE' AND v2_firing_quotation_line_id IS NOT NULL"),
    )
    op.create_index(
        "uq_kiln_batch_assignments_active_int",
        "kiln_batch_assignments",
        ["batch_id", "internal_load_line_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE' AND internal_load_line_id IS NOT NULL"),
    )
    op.create_index(
        "uq_kiln_batch_assignments_active_v2",
        "kiln_batch_assignments",
        ["batch_id", "v2_quotation_product_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE' AND v2_quotation_product_id IS NOT NULL"),
    )

    # 3. El cuarto origen de la orden. Primero la columna, despues el CHECK:
    #    soltar el CHECK antes dejaria un instante sin regla de origen.
    op.add_column(
        "production_orders", sa.Column("v2_firing_handoff_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        _FK_PUENTE_SQ,
        "production_orders",
        "v2_firing_production_handoffs",
        ["v2_firing_handoff_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(_UQ_PUENTE_SQ, "production_orders", ["v2_firing_handoff_id"])
    op.drop_constraint(_CK_ORIGEN, "production_orders", type_="check")
    op.create_check_constraint(_CK_ORIGEN, "production_orders", _ORIGEN_CUATRO_RAMAS)

    # 4. El contador de volumen, mantenido por la base.
    op.execute(sa.text(_FUNCION_VOLUMEN))
    op.execute(sa.text(_TRIGGER_VOLUMEN))


def downgrade() -> None:
    # Bajar borraria hornadas, asignaciones, cargas internas y puentes, y
    # correlativos ya entregados. Se aborta diciendo cuantos hay.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                hornadas integer;
                cargas integer;
                puentes integer;
                entregados integer;
            BEGIN
                SELECT count(*) INTO hornadas FROM kiln_batches;
                SELECT count(*) INTO cargas FROM internal_loads;
                SELECT count(*) INTO puentes FROM v2_firing_production_handoffs;
                SELECT count(*) INTO entregados FROM document_sequence_issues
                 WHERE sequence_type IN ('KILN_BATCH', 'INTERNAL_LOAD');
                IF hornadas > 0 OR cargas > 0 OR puentes > 0 OR entregados > 0 THEN
                    RAISE EXCEPTION USING MESSAGE =
                        format('No se puede bajar de 0040: %s hornadas, %s cargas internas',
                               hornadas, cargas)
                        || format(', %s puentes de Solo Quema y %s correlativos entregados',
                                  puentes, entregados);
                END IF;
            END $$;
            """
        )
    )
    op.execute(sa.text("DROP TRIGGER trg_kiln_batch_assignments_volume ON kiln_batch_assignments"))
    op.execute(sa.text("DROP FUNCTION kiln_batch_assignments_apply_volume()"))

    op.drop_constraint(_CK_ORIGEN, "production_orders", type_="check")
    op.create_check_constraint(_CK_ORIGEN, "production_orders", _ORIGEN_TRES_RAMAS)
    op.drop_constraint(_UQ_PUENTE_SQ, "production_orders", type_="unique")
    op.drop_constraint(_FK_PUENTE_SQ, "production_orders", type_="foreignkey")
    op.drop_column("production_orders", "v2_firing_handoff_id")

    op.drop_table("kiln_batch_assignments")
    op.drop_table("v2_firing_production_handoffs")
    op.drop_table("kiln_batch_operations")
    op.drop_table("internal_load_lines")
    op.drop_table("kiln_batches")
    op.drop_table("internal_loads")

    op.execute(
        sa.text(
            "DELETE FROM document_sequences WHERE sequence_type IN ('KILN_BATCH', 'INTERNAL_LOAD')"
        )
    )
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint(
        "type_allowed", "document_sequences", f"sequence_type IN ({_TIPOS_ANTES})"
    )
