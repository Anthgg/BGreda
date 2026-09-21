"""Fase 010M — persistencia de layout y dimensiones útiles del horno.

Lo aditivo:

    kilns.usable_width_cm, usable_depth_cm, usable_height_cm   dimensiones útiles del horno maestro
    kiln_batch_operations.kind incluye 'LAYOUT'               idempotencia del guardado de layout
    kiln_batch_layouts                                         layout por hornada con snapshots
    kiln_batch_layout_levels                                   niveles planificables del layout
    kiln_batch_layout_placements                               acomodo físico de piezas/grupos

Revision ID: 0041
Revises: 0040
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPERATIONS_KIND_ANTES = "'CREATE_BATCH', 'ASSIGN', 'RELEASE', 'MOVE'"
_OPERATIONS_KIND_DESPUES = f"{_OPERATIONS_KIND_ANTES}, 'LAYOUT'"


def upgrade() -> None:
    # 1. Dimensiones útiles opcionales en el maestro kilns
    op.add_column("kilns", sa.Column("usable_width_cm", sa.Numeric(18, 6), nullable=True))
    op.add_column("kilns", sa.Column("usable_depth_cm", sa.Numeric(18, 6), nullable=True))
    op.add_column("kilns", sa.Column("usable_height_cm", sa.Numeric(18, 6), nullable=True))

    op.create_check_constraint(
        "usable_width_positive",
        "kilns",
        "usable_width_cm IS NULL OR usable_width_cm > 0",
    )
    op.create_check_constraint(
        "usable_depth_positive",
        "kilns",
        "usable_depth_cm IS NULL OR usable_depth_cm > 0",
    )
    op.create_check_constraint(
        "usable_height_positive",
        "kilns",
        "usable_height_cm IS NULL OR usable_height_cm > 0",
    )

    # 2. Idempotencia: ampliar kind en kiln_batch_operations
    op.drop_constraint("kind_allowed", "kiln_batch_operations", type_="check")
    op.create_check_constraint(
        "kind_allowed",
        "kiln_batch_operations",
        f"kind IN ({_OPERATIONS_KIND_DESPUES})",
    )

    # 3. Tabla kiln_batch_layouts
    op.create_table(
        "kiln_batch_layouts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("kiln_width_cm_snapshot", sa.Numeric(18, 6), nullable=False),
        sa.Column("kiln_depth_cm_snapshot", sa.Numeric(18, 6), nullable=False),
        sa.Column("kiln_height_cm_snapshot", sa.Numeric(18, 6), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
            ["batch_id"],
            ["kiln_batches.id"],
            name="fk_kiln_batch_layouts_batch_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kiln_batch_layouts")),
        sa.UniqueConstraint("batch_id", name="uq_kiln_batch_layouts_batch_id"),
        sa.CheckConstraint("kiln_width_cm_snapshot > 0", name="ck_kiln_batch_layouts_width_positive"),
        sa.CheckConstraint("kiln_depth_cm_snapshot > 0", name="ck_kiln_batch_layouts_depth_positive"),
        sa.CheckConstraint("kiln_height_cm_snapshot > 0", name="ck_kiln_batch_layouts_height_positive"),
        sa.CheckConstraint("version >= 1", name="ck_kiln_batch_layouts_version_positive"),
    )

    # 4. Tabla kiln_batch_layout_levels
    op.create_table(
        "kiln_batch_layout_levels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("layout_id", sa.Integer(), nullable=False),
        sa.Column("level_index", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("z_cm", sa.Numeric(18, 6), nullable=False),
        sa.Column("usable_height_cm", sa.Numeric(18, 6), nullable=False),
        sa.Column("plate_label", sa.String(length=100), nullable=True),
        sa.Column("plate_thickness_cm", sa.Numeric(18, 6), nullable=True),
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
            ["layout_id"],
            ["kiln_batch_layouts.id"],
            name="fk_kiln_batch_layout_levels_layout_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kiln_batch_layout_levels")),
        sa.UniqueConstraint("layout_id", "level_index", name="uq_kiln_batch_layout_levels_layout_level"),
        sa.CheckConstraint("level_index >= 0", name="ck_kiln_batch_layout_levels_level_index_non_negative"),
        sa.CheckConstraint("z_cm >= 0", name="ck_kiln_batch_layout_levels_z_non_negative"),
        sa.CheckConstraint("usable_height_cm > 0", name="ck_kiln_batch_layout_levels_usable_height_positive"),
        sa.CheckConstraint(
            "plate_thickness_cm IS NULL OR plate_thickness_cm >= 0",
            name="ck_kiln_batch_layout_levels_plate_thickness_non_negative",
        ),
    )
    op.create_index(
        op.f("ix_kiln_batch_layout_levels_layout_id"),
        "kiln_batch_layout_levels",
        ["layout_id"],
        unique=False,
    )

    # 5. Tabla kiln_batch_layout_placements
    op.create_table(
        "kiln_batch_layout_placements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("layout_id", sa.Integer(), nullable=False),
        sa.Column("batch_assignment_id", sa.Integer(), nullable=False),
        sa.Column("group_index", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("unit_index", sa.Integer(), nullable=True),
        sa.Column("quantity", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("level_index", sa.Integer(), nullable=False),
        sa.Column("x_cm", sa.Numeric(18, 6), nullable=False),
        sa.Column("y_cm", sa.Numeric(18, 6), nullable=False),
        sa.Column("rotation_degrees", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("piece_length_cm_snapshot", sa.Numeric(18, 6), nullable=False),
        sa.Column("piece_width_cm_snapshot", sa.Numeric(18, 6), nullable=False),
        sa.Column("piece_height_cm_snapshot", sa.Numeric(18, 6), nullable=False),
        sa.Column("separation_cm_snapshot", sa.Numeric(18, 6), server_default=sa.text("0"), nullable=False),
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
            ["layout_id"],
            ["kiln_batch_layouts.id"],
            name="fk_kiln_batch_layout_placements_layout_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["batch_assignment_id"],
            ["kiln_batch_assignments.id"],
            name="fk_kiln_batch_layout_placements_assignment_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kiln_batch_layout_placements")),
        sa.CheckConstraint("quantity > 0", name="ck_kiln_batch_layout_placements_quantity_positive"),
        sa.CheckConstraint("level_index >= 0", name="ck_kiln_batch_layout_placements_level_index_non_negative"),
        sa.CheckConstraint("x_cm >= 0", name="ck_kiln_batch_layout_placements_x_non_negative"),
        sa.CheckConstraint("y_cm >= 0", name="ck_kiln_batch_layout_placements_y_non_negative"),
        sa.CheckConstraint("rotation_degrees IN (0, 90)", name="ck_kiln_batch_layout_placements_rotation_allowed"),
        sa.CheckConstraint("piece_length_cm_snapshot > 0", name="ck_kiln_batch_layout_placements_length_positive"),
        sa.CheckConstraint("piece_width_cm_snapshot > 0", name="ck_kiln_batch_layout_placements_width_positive"),
        sa.CheckConstraint("piece_height_cm_snapshot > 0", name="ck_kiln_batch_layout_placements_height_positive"),
        sa.CheckConstraint("separation_cm_snapshot >= 0", name="ck_kiln_batch_layout_placements_separation_non_negative"),
        sa.CheckConstraint("group_index >= 0", name="ck_kiln_batch_layout_placements_group_index_non_negative"),
        sa.CheckConstraint("unit_index IS NULL OR unit_index >= 0", name="ck_kiln_batch_layout_placements_unit_index_non_negative"),
    )
    op.create_index(
        op.f("ix_kiln_batch_layout_placements_layout_id"),
        "kiln_batch_layout_placements",
        ["layout_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_kiln_batch_layout_placements_assignment_id"),
        "kiln_batch_layout_placements",
        ["batch_assignment_id"],
        unique=False,
    )


def downgrade() -> None:
    # Bajar borraria layouts, niveles y placements fisicos. Se aborta diciendo cuantos hay.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                layouts integer;
                placements integer;
            BEGIN
                SELECT count(*) INTO layouts FROM kiln_batch_layouts;
                SELECT count(*) INTO placements FROM kiln_batch_layout_placements;
                IF layouts > 0 OR placements > 0 THEN
                    RAISE EXCEPTION USING MESSAGE =
                        format('No se puede bajar de 0041: %s layouts y %s placements persistidos',
                               layouts, placements);
                END IF;
            END $$;
            """
        )
    )
    op.drop_table("kiln_batch_layout_placements")
    op.drop_table("kiln_batch_layout_levels")
    op.drop_table("kiln_batch_layouts")

    op.drop_constraint("kind_allowed", "kiln_batch_operations", type_="check")
    op.create_check_constraint(
        "kind_allowed",
        "kiln_batch_operations",
        f"kind IN ({_OPERATIONS_KIND_ANTES})",
    )

    op.drop_constraint("usable_height_positive", "kilns", type_="check")
    op.drop_constraint("usable_depth_positive", "kilns", type_="check")
    op.drop_constraint("usable_width_positive", "kilns", type_="check")

    op.drop_column("kilns", "usable_height_cm")
    op.drop_column("kilns", "usable_depth_cm")
    op.drop_column("kilns", "usable_width_cm")
