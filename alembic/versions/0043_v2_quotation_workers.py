"""Fase 010P W1 — tarifa congelada por trabajador y origen de cada asignacion.

Lo aditivo (010P_PLAN_REV2 §2, migracion 0043):

    v2_quotation_workers                una fila por persona y cotizacion, con su
                                        tipo, jornal y jornada CONGELADOS en la
                                        primera asignacion. De aqui salen los
                                        externos DISTINTOS del pedido.
    v2_quotation_labor.assignment_origin  DEFAULT (lo puso el sistema con el
                                        trabajador por defecto del tipo de
                                        pedido) o MANUAL (lo eligio alguien).

Sustituye al «external_worker_count» de REV1: el numero de externos se deriva
de las asignaciones, no se escribe.

Backfill:

- `assignment_origin` = MANUAL en TODAS las tareas existentes. Nunca se supone
  que una asignacion historica fue automatica.
- `v2_quotation_workers`, solo para BORRADORES: una fila por persona distinta,
  con el congelado de su tarea MAS RECIENTE (mayor id). Lo emitido no se toca:
  sus costos ya estan congelados y no se recalculan.

`downgrade` se niega si alguna asignacion es DEFAULT: bajar borraria quien la
eligio, y aceptar «Por mayor» despues pisaria como si fuera automatica una
eleccion de otra persona.

Revision ID: 0043
Revises: 0042
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLA = "v2_quotation_workers"
_TAREAS = "v2_quotation_labor"
_CK_ORIGEN = "assignment_origin_allowed"


def upgrade() -> None:
    op.create_table(
        _TABLA,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("v2_quotation_id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column("worker_type_snapshot", sa.String(length=16), nullable=False),
        sa.Column("daily_rate_snapshot", sa.Numeric(18, 6), nullable=False),
        sa.Column("workday_hours_snapshot", sa.Numeric(18, 6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_v2_quotation_workers"),
        sa.ForeignKeyConstraint(
            ["v2_quotation_id"],
            ["v2_quotations.id"],
            ondelete="CASCADE",
            name="fk_v2_quotation_workers_v2_quotation_id_v2_quotations",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["v2_workers.id"],
            ondelete="RESTRICT",
            name="fk_v2_quotation_workers_worker_id_v2_workers",
        ),
        sa.UniqueConstraint("v2_quotation_id", "worker_id", name="uq_v2_quotation_workers_worker"),
        sa.CheckConstraint("daily_rate_snapshot >= 0", name="daily_rate_non_negative"),
        sa.CheckConstraint(
            "workday_hours_snapshot IS NULL"
            " OR (workday_hours_snapshot > 0 AND workday_hours_snapshot <= 24)",
            name="workday_hours_range",
        ),
    )
    op.create_index(
        "ix_v2_quotation_workers_v2_quotation_id", _TABLA, ["v2_quotation_id"], unique=False
    )

    op.add_column(
        _TAREAS,
        sa.Column(
            "assignment_origin",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'MANUAL'"),
        ),
    )
    op.create_check_constraint(_CK_ORIGEN, _TAREAS, "assignment_origin IN ('DEFAULT', 'MANUAL')")

    # Borradores: una persona por fila, con el congelado de su tarea mas reciente.
    op.execute(
        sa.text(
            """
            INSERT INTO v2_quotation_workers
                (v2_quotation_id, worker_id, worker_type_snapshot,
                 daily_rate_snapshot, workday_hours_snapshot, created_at)
            SELECT DISTINCT ON (tarea.v2_quotation_id, tarea.worker_id)
                   tarea.v2_quotation_id, tarea.worker_id, tarea.worker_type_snapshot,
                   tarea.daily_rate_snapshot, tarea.workday_hours_snapshot, now()
              FROM v2_quotation_labor AS tarea
              JOIN v2_quotations AS cotizacion ON cotizacion.id = tarea.v2_quotation_id
             WHERE cotizacion.status = 'DRAFT'
             ORDER BY tarea.v2_quotation_id, tarea.worker_id, tarea.id DESC
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                automaticas integer;
            BEGIN
                SELECT count(*) INTO automaticas FROM v2_quotation_labor
                 WHERE assignment_origin = 'DEFAULT';
                IF automaticas > 0 THEN
                    RAISE EXCEPTION
                        'No se puede bajar 0043: % asignaciones automaticas perderian su origen',
                        automaticas;
                END IF;
            END $$;
            """
        )
    )
    op.drop_constraint(_CK_ORIGEN, _TAREAS, type_="check")
    op.drop_column(_TAREAS, "assignment_origin")
    op.drop_index("ix_v2_quotation_workers_v2_quotation_id", table_name=_TABLA)
    op.drop_table(_TABLA)
