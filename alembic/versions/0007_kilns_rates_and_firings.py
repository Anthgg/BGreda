"""Fase 4: hornos, tarifas, factores de ocupacion y hojas de quema.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-23

Crea el modulo de quemas:

- ``kilns``: maestro de hornos con su capacidad util.
- ``kiln_rates``: tarifa por horno y tipo de quema, con vigencia. Nunca se
  sobrescribe: cambiarla cierra la vigente y abre otra.
- ``kiln_occupancy_factors``: la tabla de tramos del documento funcional, por
  horno.
- ``firings`` / ``firing_kiln_sessions`` / ``firing_lines``: la hoja de quema.

## Sobre el seed

Los dos hornos, sus cuatro tarifas y las veinte filas de factores **no son
datos de prueba**: son el catalogo oficial de GREDA, transcrito de la hoja
«Costo de quema» del libro *Propuesta para cotizar* (celdas ``T4:V10`` para las
tarifas y ``T15:V25`` para los factores). Se cargan aqui, y no en un script
aparte, para que cualquier entorno que aplique la migracion tenga el mismo
catalogo y para que el historial de tarifas arranque con vigencia conocida.

Las capacidades y tarifas quedan editables por ADMIN desde la aplicacion: la
migracion las siembra, no las fija.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLES = (
    "kilns",
    "kiln_rates",
    "kiln_occupancy_factors",
    "firings",
    "firing_kiln_sessions",
    "firing_lines",
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

#: Fecha desde la que rigen las tarifas iniciales. El historial necesita un
#: origen explicito; sin el, una quema anterior no sabria que tarifa aplicarle.
RATES_VALID_FROM = date(2026, 1, 1)

#: Catalogo oficial: (codigo, nombre, capacidad cm3, tarifa baja, tarifa alta).
SEED_KILNS = (
    ("KILN-001", "Horno pequeno", 17000, "90.00", "180.00"),
    ("KILN-002", "Horno grande", 200000, "1000.00", "2000.00"),
)

#: Tramos de ocupacion y multiplicador. Transcripcion literal de ``T15:V25``:
#: (min %, max %, factor horno pequeno, factor horno grande).
SEED_FACTORS = (
    (1, 10, "2.0", "3.0"),
    (11, 20, "1.9", "2.8"),
    (21, 30, "1.8", "2.6"),
    (31, 40, "1.7", "2.3"),
    (41, 50, "1.6", "2.1"),
    (51, 60, "1.4", "1.9"),
    (61, 70, "1.3", "1.7"),
    (71, 80, "1.2", "1.4"),
    (81, 90, "1.1", "1.2"),
    (91, 100, "1.0", "1.0"),
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. kilns
    # ------------------------------------------------------------------
    op.create_table(
        "kilns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("capacity_volume_cm3", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.CheckConstraint("capacity_volume_cm3 > 0", name="ck_kilns_capacity_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_kilns"),
        sa.UniqueConstraint("code", name="uq_kilns_code"),
    )
    op.create_index("ix_kilns_active", "kilns", ["active"])

    # ------------------------------------------------------------------
    # 2. kiln_rates
    # ------------------------------------------------------------------
    op.create_table(
        "kiln_rates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kiln_id", sa.Integer(), nullable=False),
        sa.Column("firing_type", sa.String(length=16), nullable=False),
        sa.Column("rate", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
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
        sa.CheckConstraint("rate >= 0", name="ck_kiln_rates_rate_not_negative"),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_kiln_rates_validity_ordered",
        ),
        sa.CheckConstraint(
            "firing_type IN ('LOW', 'HIGH')", name="ck_kiln_rates_firing_type_allowed"
        ),
        sa.ForeignKeyConstraint(
            ["kiln_id"],
            ["public.kilns.id"],
            name="fk_kiln_rates_kiln_id_kilns",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kiln_rates"),
    )
    op.create_index("ix_kiln_rates_kiln_id", "kiln_rates", ["kiln_id"])
    # Como maximo una tarifa abierta por horno y tipo de quema.
    op.create_index(
        "ix_kiln_rates_single_open",
        "kiln_rates",
        ["kiln_id", "firing_type"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )

    # ------------------------------------------------------------------
    # 3. kiln_occupancy_factors
    # ------------------------------------------------------------------
    op.create_table(
        "kiln_occupancy_factors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kiln_id", sa.Integer(), nullable=False),
        sa.Column("min_percentage", sa.Integer(), nullable=False),
        sa.Column("max_percentage", sa.Integer(), nullable=False),
        sa.Column("factor", sa.Numeric(precision=10, scale=6), nullable=False),
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
            "min_percentage >= 1 AND max_percentage <= 100",
            name="ck_kiln_occupancy_factors_range_within_100",
        ),
        sa.CheckConstraint(
            "max_percentage >= min_percentage",
            name="ck_kiln_occupancy_factors_range_ordered",
        ),
        sa.CheckConstraint("factor > 0", name="ck_kiln_occupancy_factors_factor_positive"),
        sa.ForeignKeyConstraint(
            ["kiln_id"],
            ["public.kilns.id"],
            name="fk_kiln_occupancy_factors_kiln_id_kilns",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kiln_occupancy_factors"),
        sa.UniqueConstraint("kiln_id", "min_percentage", name="uq_kiln_occupancy_factors_kiln_id"),
    )
    op.create_index("ix_kiln_occupancy_factors_kiln_id", "kiln_occupancy_factors", ["kiln_id"])

    # ------------------------------------------------------------------
    # 4. firings
    # ------------------------------------------------------------------
    op.create_table(
        "firings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=True),
        sa.Column("firing_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "total_volume_cm3",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "occupancy_percentage",
            sa.Numeric(precision=10, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "occupancy_factor",
            sa.Numeric(precision=10, scale=6),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "subtotal",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_cost",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('DRAFT', 'CONFIRMED', 'CANCELLED')", name="ck_firings_status_allowed"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_firings"),
        sa.UniqueConstraint("code", name="uq_firings_code"),
    )
    op.create_index("ix_firings_status", "firings", ["status"])
    op.create_index("ix_firings_firing_date", "firings", ["firing_date"])

    # ------------------------------------------------------------------
    # 5. firing_kiln_sessions
    # ------------------------------------------------------------------
    op.create_table(
        "firing_kiln_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("firing_id", sa.Integer(), nullable=False),
        sa.Column("kiln_id", sa.Integer(), nullable=False),
        sa.Column("firing_type", sa.String(length=16), nullable=False),
        sa.Column("rate_snapshot", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("capacity_snapshot", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column(
            "subtotal",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
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
        sa.CheckConstraint("rate_snapshot >= 0", name="ck_firing_kiln_sessions_rate_not_negative"),
        sa.CheckConstraint(
            "capacity_snapshot > 0", name="ck_firing_kiln_sessions_capacity_positive"
        ),
        sa.CheckConstraint(
            "firing_type IN ('LOW', 'HIGH')",
            name="ck_firing_kiln_sessions_firing_type_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["firing_id"],
            ["public.firings.id"],
            name="fk_firing_kiln_sessions_firing_id_firings",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["kiln_id"],
            ["public.kilns.id"],
            name="fk_firing_kiln_sessions_kiln_id_kilns",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_firing_kiln_sessions"),
        sa.UniqueConstraint(
            "firing_id", "kiln_id", "firing_type", name="uq_firing_kiln_sessions_firing_id"
        ),
    )
    op.create_index("ix_firing_kiln_sessions_firing_id", "firing_kiln_sessions", ["firing_id"])
    op.create_index("ix_firing_kiln_sessions_kiln_id", "firing_kiln_sessions", ["kiln_id"])

    # ------------------------------------------------------------------
    # 6. firing_lines
    # ------------------------------------------------------------------
    op.create_table(
        "firing_lines",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("firing_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("length_cm", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("width_cm", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("height_cm", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("unit_volume_cm3", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("total_volume_cm3", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("low_session_id", sa.Integer(), nullable=True),
        sa.Column("high_session_id", sa.Integer(), nullable=True),
        sa.Column("factor_kiln_id", sa.Integer(), nullable=True),
        sa.Column(
            "occupancy_percentage",
            sa.Numeric(precision=10, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("occupancy_bracket", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column(
            "occupancy_factor",
            sa.Numeric(precision=10, scale=6),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "base_cost",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "allocated_cost",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
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
        sa.CheckConstraint("quantity > 0", name="ck_firing_lines_quantity_positive"),
        sa.CheckConstraint(
            "length_cm > 0 AND width_cm > 0 AND height_cm > 0",
            name="ck_firing_lines_dimensions_positive",
        ),
        sa.CheckConstraint(
            "occupancy_bracket BETWEEN 10 AND 100", name="ck_firing_lines_bracket_in_tens"
        ),
        sa.CheckConstraint(
            "occupancy_bracket % 10 = 0", name="ck_firing_lines_bracket_multiple_of_ten"
        ),
        sa.ForeignKeyConstraint(
            ["firing_id"],
            ["public.firings.id"],
            name="fk_firing_lines_firing_id_firings",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["public.products.id"],
            name="fk_firing_lines_product_id_products",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["low_session_id"],
            ["public.firing_kiln_sessions.id"],
            name="fk_firing_lines_low_session_id_firing_kiln_sessions",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["high_session_id"],
            ["public.firing_kiln_sessions.id"],
            name="fk_firing_lines_high_session_id_firing_kiln_sessions",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["factor_kiln_id"],
            ["public.kilns.id"],
            name="fk_firing_lines_factor_kiln_id_kilns",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_firing_lines"),
    )
    op.create_index("ix_firing_lines_firing_id", "firing_lines", ["firing_id"])
    op.create_index("ix_firing_lines_product_id", "firing_lines", ["product_id"])

    # ------------------------------------------------------------------
    # 7. Catalogo oficial de hornos, tarifas y factores
    # ------------------------------------------------------------------
    _seed_catalog()

    # ------------------------------------------------------------------
    # 8. Seguridad: RLS habilitado sin politicas y revoke a los roles publicos
    # ------------------------------------------------------------------
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;")
        for role in ("anon", "authenticated"):
            op.execute(_REVOKE_SQL.format(table=table, role=role))


def _seed_catalog() -> None:
    """Inserta el catalogo oficial transcrito del documento funcional."""
    kilns_table = sa.table(
        "kilns",
        sa.column("id", sa.Integer),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("capacity_volume_cm3", sa.Numeric),
        sa.column("active", sa.Boolean),
    )
    rates_table = sa.table(
        "kiln_rates",
        sa.column("kiln_id", sa.Integer),
        sa.column("firing_type", sa.String),
        sa.column("rate", sa.Numeric),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
    )
    factors_table = sa.table(
        "kiln_occupancy_factors",
        sa.column("kiln_id", sa.Integer),
        sa.column("min_percentage", sa.Integer),
        sa.column("max_percentage", sa.Integer),
        sa.column("factor", sa.Numeric),
    )

    op.bulk_insert(
        kilns_table,
        [
            {
                "id": index,
                "code": code,
                "name": name,
                "capacity_volume_cm3": capacity,
                "active": True,
            }
            for index, (code, name, capacity, _low, _high) in enumerate(SEED_KILNS, start=1)
        ],
    )
    # La secuencia del autoincremento debe quedar por encima de los ids fijados
    # a mano, o el primer horno que cree un usuario chocaria con la clave.
    op.execute(
        "SELECT setval(pg_get_serial_sequence('public.kilns', 'id'), "
        "(SELECT MAX(id) FROM public.kilns))"
    )

    op.bulk_insert(
        rates_table,
        [
            {
                "kiln_id": index,
                "firing_type": firing_type,
                "rate": rate,
                "valid_from": RATES_VALID_FROM,
                "valid_to": None,
            }
            for index, (_code, _name, _capacity, low, high) in enumerate(SEED_KILNS, start=1)
            for firing_type, rate in (("LOW", low), ("HIGH", high))
        ],
    )

    op.bulk_insert(
        factors_table,
        [
            {
                "kiln_id": kiln_index,
                "min_percentage": minimum,
                "max_percentage": maximum,
                "factor": small if kiln_index == 1 else large,
            }
            for kiln_index in (1, 2)
            for minimum, maximum, small, large in SEED_FACTORS
        ],
    )


def downgrade() -> None:
    op.drop_table("firing_lines")
    op.drop_table("firing_kiln_sessions")
    op.drop_table("firings")
    op.drop_table("kiln_occupancy_factors")
    op.drop_table("kiln_rates")
    op.drop_table("kilns")
