"""Fase 010P W1 — reglas comerciales v2 del Cotizador V2.

Lo aditivo (010P_PLAN_REV2 §2, migracion 0042):

    v2_quotation_products
        production_time_per_unit_minutes   minutos por pieza (NULL = sin decidir)
        mold_count                         moldes a la vez (>= 1, default 1)
        line_active_minutes                ceil(cantidad/moldes) x minutos
        allocated_external_commercial_cost parte del externo comercial del pedido
        allocated_external_real_cost       parte del externo real del pedido
    v2_quotations
        pricing_rules_version              1 = anterior a 010P, 2 = 010P
        space_cost_per_hour_snapshot       costo/dia / jornada, congelado
        space_cost_per_hour_override       acuerdo de la cotizacion
        passive_time_hours                 solo sugerencia, nunca costo
        wholesale_threshold_snapshot       umbral congelado al crear
        wholesale_suggestion_declined_at   la sugerencia se rechazo
        active_production_minutes          MAX de las lineas
        commercial_external_labor_cost     lo que se imputa al cliente
        real_external_labor_cost           lo que paga el taller
    v2_commercial_settings
        wholesale_quantity_threshold       umbral de unidades (NULL = sin sugerencia)
        retail_default_worker_id           trabajador por defecto por menor
        wholesale_default_worker_id        trabajador por defecto por mayor

Backfill:

- `pricing_rules_version`: DRAFT = 2; CONFIRMED, CANCELLED (y lo vencido, que
  en la base es CONFIRMED con vigencia pasada) = 1. Lo emitido conserva su
  historia: no se recalcula.
- Borradores: costo de espacio por hora = costo/dia / jornada congelados, y el
  umbral de la configuracion (NULL mientras nadie lo configure).
- P7 del owner: los borradores POR MENOR pasan a administracion 0. Lo emitido
  no se toca.

`downgrade` quita lo anadido y se niega si hay datos que solo existen desde
010P (tiempos, moldes, acuerdos de espacio, tiempo pasivo, rechazos). No puede
devolver los S/200 de administracion a los borradores por menor: ese valor ya
no existe en ninguna parte, y la regla vigente dice que es 0.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MINUTOS = sa.Numeric(10, 2)
_CANTIDAD = sa.Numeric(18, 6)
_COSTO_UNITARIO = sa.Numeric(24, 12)
_CALCULO = sa.Numeric(36, 18)

_LINEAS = "v2_quotation_products"
_COTIZACIONES = "v2_quotations"
_AJUSTES = "v2_commercial_settings"

_CK_LINEAS = (
    (
        "time_per_unit_positive",
        "production_time_per_unit_minutes IS NULL OR production_time_per_unit_minutes > 0",
    ),
    ("mold_count_positive", "mold_count >= 1"),
    (
        "line_active_minutes_non_negative",
        "line_active_minutes IS NULL OR line_active_minutes >= 0",
    ),
    ("line_external_commercial_non_negative", "allocated_external_commercial_cost >= 0"),
    ("line_external_real_non_negative", "allocated_external_real_cost >= 0"),
)
_CK_COTIZACIONES = (
    ("pricing_rules_version_known", "pricing_rules_version IN (1, 2)"),
    (
        "space_cost_per_hour_override_non_negative",
        "space_cost_per_hour_override IS NULL OR space_cost_per_hour_override >= 0",
    ),
    ("passive_time_hours_non_negative", "passive_time_hours >= 0"),
    (
        "wholesale_threshold_positive",
        "wholesale_threshold_snapshot IS NULL OR wholesale_threshold_snapshot > 0",
    ),
    ("active_minutes_non_negative", "active_production_minutes >= 0"),
    ("external_commercial_non_negative", "commercial_external_labor_cost >= 0"),
    ("external_real_non_negative", "real_external_labor_cost >= 0"),
)
_CK_AJUSTES = (
    (
        "wholesale_threshold_positive",
        "wholesale_quantity_threshold IS NULL OR wholesale_quantity_threshold > 0",
    ),
)
_FK_TRABAJADORES = (
    ("fk_v2_settings_retail_worker", "retail_default_worker_id"),
    ("fk_v2_settings_wholesale_worker", "wholesale_default_worker_id"),
)


def upgrade() -> None:
    # ---- Lineas: tiempo, moldes y reparto del externo -----------------------
    op.add_column(_LINEAS, sa.Column("production_time_per_unit_minutes", _MINUTOS, nullable=True))
    op.add_column(
        _LINEAS,
        sa.Column("mold_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(_LINEAS, sa.Column("line_active_minutes", _CANTIDAD, nullable=True))
    for columna in ("allocated_external_commercial_cost", "allocated_external_real_cost"):
        op.add_column(
            _LINEAS, sa.Column(columna, _CALCULO, nullable=False, server_default=sa.text("0"))
        )
    for nombre, condicion in _CK_LINEAS:
        op.create_check_constraint(nombre, _LINEAS, condicion)

    # ---- Cotizaciones ------------------------------------------------------
    # La version nace en 1 para TODAS las filas existentes; solo los borradores
    # pasan a 2. Despues el default de la columna es 2: todo lo nuevo es 010P.
    op.add_column(
        _COTIZACIONES,
        sa.Column(
            "pricing_rules_version", sa.SmallInteger(), nullable=False, server_default=sa.text("1")
        ),
    )
    op.add_column(
        _COTIZACIONES, sa.Column("space_cost_per_hour_snapshot", _COSTO_UNITARIO, nullable=True)
    )
    op.add_column(
        _COTIZACIONES, sa.Column("space_cost_per_hour_override", _COSTO_UNITARIO, nullable=True)
    )
    op.add_column(
        _COTIZACIONES,
        sa.Column("passive_time_hours", _CANTIDAD, nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        _COTIZACIONES, sa.Column("wholesale_threshold_snapshot", sa.Integer(), nullable=True)
    )
    op.add_column(
        _COTIZACIONES,
        sa.Column("wholesale_suggestion_declined_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        _COTIZACIONES,
        sa.Column(
            "active_production_minutes", _CANTIDAD, nullable=False, server_default=sa.text("0")
        ),
    )
    for columna in ("commercial_external_labor_cost", "real_external_labor_cost"):
        op.add_column(
            _COTIZACIONES,
            sa.Column(columna, _CALCULO, nullable=False, server_default=sa.text("0")),
        )
    for nombre, condicion in _CK_COTIZACIONES:
        op.create_check_constraint(nombre, _COTIZACIONES, condicion)

    # ---- Configuracion -----------------------------------------------------
    op.add_column(_AJUSTES, sa.Column("wholesale_quantity_threshold", sa.Integer(), nullable=True))
    for nombre, columna in _FK_TRABAJADORES:
        op.add_column(_AJUSTES, sa.Column(columna, sa.Integer(), nullable=True))
        op.create_foreign_key(
            nombre, _AJUSTES, "v2_workers", [columna], ["id"], ondelete="RESTRICT"
        )
    for nombre, condicion in _CK_AJUSTES:
        op.create_check_constraint(nombre, _AJUSTES, condicion)

    # ---- Backfill ----------------------------------------------------------
    # Solo los borradores adoptan 010P. Lo emitido, cancelado o vencido queda en 1.
    op.execute(sa.text("UPDATE v2_quotations SET pricing_rules_version = 2 WHERE status = 'DRAFT'"))
    op.execute(
        sa.text(
            """
            UPDATE v2_quotations
               SET space_cost_per_hour_snapshot =
                       space_service_cost_per_day_snapshot / workday_hours_snapshot
             WHERE status = 'DRAFT'
               AND space_service_cost_per_day_snapshot IS NOT NULL
               AND workday_hours_snapshot IS NOT NULL
               AND workday_hours_snapshot > 0
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE v2_quotations
               SET wholesale_threshold_snapshot =
                       (SELECT wholesale_quantity_threshold FROM v2_commercial_settings LIMIT 1)
             WHERE status = 'DRAFT'
            """
        )
    )
    # P7: los borradores por menor dejan de pagar administracion.
    op.execute(
        sa.text(
            "UPDATE v2_quotations SET administrative_cost_snapshot = 0"
            " WHERE status = 'DRAFT' AND production_type = 'RETAIL'"
        )
    )
    op.alter_column(_COTIZACIONES, "pricing_rules_version", server_default=sa.text("2"))


def downgrade() -> None:
    # Bajar borraria decisiones que solo existen desde 010P. Se aborta diciendo
    # cuantas son, como hacen 0038 y las demas migraciones con datos.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                lineas integer;
                cotizaciones integer;
                ajustes integer;
            BEGIN
                SELECT count(*) INTO lineas FROM v2_quotation_products
                 WHERE production_time_per_unit_minutes IS NOT NULL OR mold_count <> 1;
                SELECT count(*) INTO cotizaciones FROM v2_quotations
                 WHERE space_cost_per_hour_override IS NOT NULL
                    OR passive_time_hours <> 0
                    OR wholesale_suggestion_declined_at IS NOT NULL
                    OR wholesale_threshold_snapshot IS NOT NULL
                    OR (status = 'DRAFT' AND production_type = 'RETAIL');
                SELECT count(*) INTO ajustes FROM v2_commercial_settings
                 WHERE wholesale_quantity_threshold IS NOT NULL
                    OR retail_default_worker_id IS NOT NULL
                    OR wholesale_default_worker_id IS NOT NULL;
                IF lineas > 0 OR cotizaciones > 0 OR ajustes > 0 THEN
                    RAISE EXCEPTION
                        'No se puede bajar 0042: % lineas y % cotizaciones y % ajustes'
                        ' tienen datos 010P que perderian', lineas, cotizaciones, ajustes;
                END IF;
            END $$;
            """
        )
    )
    for nombre, _condicion in _CK_AJUSTES:
        op.drop_constraint(nombre, _AJUSTES, type_="check")
    for nombre, columna in _FK_TRABAJADORES:
        op.drop_constraint(nombre, _AJUSTES, type_="foreignkey")
        op.drop_column(_AJUSTES, columna)
    op.drop_column(_AJUSTES, "wholesale_quantity_threshold")

    for nombre, _condicion in _CK_COTIZACIONES:
        op.drop_constraint(nombre, _COTIZACIONES, type_="check")
    for columna in (
        "real_external_labor_cost",
        "commercial_external_labor_cost",
        "active_production_minutes",
        "wholesale_suggestion_declined_at",
        "wholesale_threshold_snapshot",
        "passive_time_hours",
        "space_cost_per_hour_override",
        "space_cost_per_hour_snapshot",
        "pricing_rules_version",
    ):
        op.drop_column(_COTIZACIONES, columna)

    for nombre, _condicion in _CK_LINEAS:
        op.drop_constraint(nombre, _LINEAS, type_="check")
    for columna in (
        "allocated_external_real_cost",
        "allocated_external_commercial_cost",
        "line_active_minutes",
        "mold_count",
        "production_time_per_unit_minutes",
    ):
        op.drop_column(_LINEAS, columna)
