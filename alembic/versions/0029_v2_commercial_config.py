"""Fase 010B — la configuracion comercial del Cotizador V2.

Tres movimientos, los tres aditivos:

1. tabla `v2_commercial_settings`, fila unica, con los defaults de V2. Se
   siembra con los valores aprobados en el Excel «Cotizador Greda V2»: jornada
   de 8 h, espacio 140/dia, administracion 200 por cotizacion, factor x3 con
   suelo x2, vigencia 20 dias y tipo de cambio de referencia 3,5.

2. tabla `v2_kiln_rates`, VACIA. Guarda, por horno y tipo de quema, el costo
   real del gas y las dos tarifas comerciales —externo y alumno—.

3. columnas de snapshot en `v2_quotations`, todas anulables.

**Nada de esto toca Legacy.** `commercial_settings` no se modifica: el IGV, la
moneda y el paso de redondeo siguen siendo suyos y V2 los consume de alli. No
hay un segundo IGV.

## Por que `v2_kiln_rates` y no una columna mas en `kiln_rates`

Porque el costeo de quema Legacy resuelve la tarifa vigente tomando la PRIMERA
fila que encuentra para cada `(kiln_id, firing_type)`, sin mas discriminante
(`app/services/firings.py`). Meter ahi una tarifa de alumno o un costo de gas
haria que una quema Legacy se cobrara con un numero que no le corresponde, y no
fallaria: cobraria mal en silencio.

## Por que las tarifas de horno nacen vacias

Los importes aprobados existen —35/70 y 55/110 de gas, 200/250 y 700/1200 de
externo, 90/180 y 1000/2000 de alumno— pero estan definidos para «horno chico»
y «horno grande», y el taller da de alta sus hornos con nombre y capacidad
propios. El sistema no sabe cual es cual, y deducirlo de un umbral de capacidad
que nadie definio pondria una tarifa de 200 soles en el horno equivocado. Se
ofrecen como referencia en la API de configuracion y los aplica una persona.

Es la misma razon por la que 0023 dejo en cero las tarifas de prototipo: el
problema no es el numero, es atribuirlo sin que nadie lo haya dicho.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

#: La fila unica de configuracion, como en `company_settings` y
#: `commercial_settings`.
SINGLETON_ID = 1

#: Columnas de snapshot que se anaden a `v2_quotations`. Todas anulables: una
#: cotizacion creada en 010A nacio antes de que la configuracion existiera, y
#: NULL ahi significa «anterior al snapshot», no «vale cero».
SNAPSHOT_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("tax_percent_snapshot", sa.Numeric(9, 6)),
    ("currency_code_snapshot", sa.String(length=3)),
    ("currency_symbol_snapshot", sa.String(length=8)),
    ("exchange_rate_snapshot", sa.Numeric(18, 6)),
    ("validity_days_snapshot", sa.Integer()),
    ("workday_hours_snapshot", sa.Numeric(18, 6)),
    ("space_service_cost_per_day_snapshot", sa.Numeric(18, 6)),
    ("administrative_cost_snapshot", sa.Numeric(18, 6)),
    ("commercial_factor", sa.Numeric(18, 6)),
    ("commercial_factor_min_snapshot", sa.Numeric(18, 6)),
    ("commercial_factor_max_snapshot", sa.Numeric(18, 6)),
    ("customer_kind", sa.String(length=16)),
    ("low_fire_enabled", sa.Boolean()),
    ("high_fire_enabled", sa.Boolean()),
    ("settings_version_snapshot", sa.Integer()),
    ("settings_captured_at", sa.DateTime(timezone=True)),
)

#: Los CHECK que acompanan al snapshot. Admiten NULL —la cotizacion puede ser
#: anterior— pero, si hay valor, exigen que sea un valor posible: un IGV
#: negativo o un tipo de cambio en cero no son historia, son datos rotos que
#: mas adelante producirian un precio roto.
SNAPSHOT_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "tax_percent_snapshot_range",
        "tax_percent_snapshot IS NULL"
        " OR (tax_percent_snapshot >= 0 AND tax_percent_snapshot <= 100)",
    ),
    (
        "exchange_rate_snapshot_positive",
        "exchange_rate_snapshot IS NULL OR exchange_rate_snapshot > 0",
    ),
    (
        "base_currency_has_no_exchange_rate",
        "currency_code_snapshot IS NULL"
        " OR currency_code_snapshot <> 'PEN'"
        " OR exchange_rate_snapshot IS NULL",
    ),
    (
        "validity_days_snapshot_positive",
        "validity_days_snapshot IS NULL OR validity_days_snapshot > 0",
    ),
    (
        "workday_hours_snapshot_positive",
        "workday_hours_snapshot IS NULL OR workday_hours_snapshot > 0",
    ),
    (
        "space_cost_snapshot_non_negative",
        "space_service_cost_per_day_snapshot IS NULL OR space_service_cost_per_day_snapshot >= 0",
    ),
    (
        "admin_cost_snapshot_non_negative",
        "administrative_cost_snapshot IS NULL OR administrative_cost_snapshot >= 0",
    ),
    (
        "commercial_factor_floor",
        "commercial_factor IS NULL OR commercial_factor >= 2",
    ),
    (
        "commercial_factor_within_min",
        "commercial_factor IS NULL OR commercial_factor_min_snapshot IS NULL"
        " OR commercial_factor >= commercial_factor_min_snapshot",
    ),
    (
        "commercial_factor_within_max",
        "commercial_factor IS NULL OR commercial_factor_max_snapshot IS NULL"
        " OR commercial_factor <= commercial_factor_max_snapshot",
    ),
    (
        "customer_kind_allowed",
        "customer_kind IS NULL OR customer_kind IN ('EXTERNAL', 'STUDENT')",
    ),
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Los defaults de V2
    # ------------------------------------------------------------------
    op.create_table(
        "v2_commercial_settings",
        sa.Column("id", sa.SmallInteger(), primary_key=True, autoincrement=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("workday_hours", sa.Numeric(18, 6), nullable=False, server_default=sa.text("8")),
        sa.Column(
            "space_service_cost_per_day",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("140"),
        ),
        sa.Column(
            "administrative_cost_per_quote",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("200"),
        ),
        sa.Column(
            "commercial_factor_default",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("3"),
        ),
        sa.Column(
            "commercial_factor_min",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("2"),
        ),
        sa.Column(
            "commercial_factor_max",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("3"),
        ),
        sa.Column(
            "quotation_validity_days", sa.Integer(), nullable=False, server_default=sa.text("20")
        ),
        sa.Column(
            "default_exchange_rate",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("3.5"),
        ),
        sa.Column(
            "default_production_type",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'RETAIL'"),
        ),
        sa.Column(
            "default_customer_kind",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'EXTERNAL'"),
        ),
        sa.Column("retail_kiln_id", sa.Integer(), nullable=True),
        sa.Column("wholesale_kiln_id", sa.Integer(), nullable=True),
        sa.Column(
            "low_fire_enabled_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "high_fire_enabled_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "illustration_daily_rate",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("110"),
        ),
        sa.Column(
            "illustration_pieces_per_workday",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("50"),
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
        sa.ForeignKeyConstraint(["retail_kiln_id"], ["kilns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["wholesale_kiln_id"], ["kilns.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"id = {SINGLETON_ID}", name="singleton"),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("workday_hours > 0", name="workday_hours_positive"),
        sa.CheckConstraint("space_service_cost_per_day >= 0", name="space_cost_non_negative"),
        sa.CheckConstraint("administrative_cost_per_quote >= 0", name="admin_cost_non_negative"),
        sa.CheckConstraint("commercial_factor_min >= 2", name="factor_min_floor"),
        sa.CheckConstraint("commercial_factor_max >= commercial_factor_min", name="factor_range"),
        sa.CheckConstraint(
            "commercial_factor_default >= commercial_factor_min"
            " AND commercial_factor_default <= commercial_factor_max",
            name="factor_default_within_range",
        ),
        sa.CheckConstraint(
            "quotation_validity_days > 0 AND quotation_validity_days <= 3650",
            name="validity_range",
        ),
        sa.CheckConstraint("default_exchange_rate > 0", name="exchange_rate_positive"),
        sa.CheckConstraint(
            "default_production_type IN ('RETAIL', 'WHOLESALE')", name="production_type_allowed"
        ),
        sa.CheckConstraint(
            "default_customer_kind IN ('EXTERNAL', 'STUDENT')", name="customer_kind_allowed"
        ),
        sa.CheckConstraint("illustration_daily_rate >= 0", name="illustration_rate_non_negative"),
        sa.CheckConstraint(
            "illustration_pieces_per_workday > 0", name="illustration_capacity_positive"
        ),
    )
    # La fila unica. Todos los valores vienen de los `server_default`, que son
    # los aprobados: nombrarlos otra vez aqui daria dos sitios que podrian
    # discrepar.
    op.execute(
        sa.text(
            "INSERT INTO v2_commercial_settings (id) VALUES (:id) ON CONFLICT DO NOTHING"
        ).bindparams(id=SINGLETON_ID)
    )

    # ------------------------------------------------------------------
    # 2. Las tarifas de horno de V2
    # ------------------------------------------------------------------
    op.create_table(
        "v2_kiln_rates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kiln_id", sa.Integer(), nullable=False),
        sa.Column("firing_type", sa.String(length=16), nullable=False),
        sa.Column("gas_cost", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("external_rate", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("student_rate", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
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
        sa.ForeignKeyConstraint(["kiln_id"], ["kilns.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("kiln_id", "firing_type", name="uq_v2_kiln_rates_kiln_id"),
        sa.CheckConstraint("firing_type IN ('LOW', 'HIGH')", name="firing_type_allowed"),
        sa.CheckConstraint("gas_cost >= 0", name="gas_cost_non_negative"),
        sa.CheckConstraint("external_rate >= 0", name="external_rate_non_negative"),
        sa.CheckConstraint("student_rate >= 0", name="student_rate_non_negative"),
    )
    op.create_index("ix_v2_kiln_rates_kiln_id", "v2_kiln_rates", ["kiln_id"])

    # ------------------------------------------------------------------
    # 3. El snapshot en la cotizacion
    # ------------------------------------------------------------------
    for nombre, tipo in SNAPSHOT_COLUMNS:
        op.add_column("v2_quotations", sa.Column(nombre, tipo, nullable=True))
    for nombre, expresion in SNAPSHOT_CHECKS:
        op.create_check_constraint(nombre, "v2_quotations", expresion)


def downgrade() -> None:
    """Revierte la configuracion, negandose si ya congelo alguna cotizacion.

    Una cotizacion con snapshot es una cotizacion cuyo precio depende de esos
    numeros. Borrar las columnas la dejaria sin explicacion posible: seguiria
    existiendo, pero nadie podria decir con que costo de taller ni con que
    factor se armo.
    """
    conexion = op.get_bind()
    congeladas = (
        conexion.scalar(
            sa.text("SELECT count(*) FROM v2_quotations WHERE settings_captured_at IS NOT NULL")
        )
        or 0
    )
    if congeladas:
        raise RuntimeError(
            f"0029 no puede revertirse: hay {congeladas} cotizacion(es) V2 con la "
            "configuracion ya congelada. Revertir las dejaria sin los numeros con "
            "los que se calcularon."
        )

    for nombre, _ in SNAPSHOT_CHECKS:
        op.drop_constraint(nombre, "v2_quotations", type_="check")
    for nombre, _ in SNAPSHOT_COLUMNS:
        op.drop_column("v2_quotations", nombre)

    op.drop_index("ix_v2_kiln_rates_kiln_id", table_name="v2_kiln_rates")
    op.drop_table("v2_kiln_rates")
    op.drop_table("v2_commercial_settings")
