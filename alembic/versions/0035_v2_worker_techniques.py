"""Correccion 010H — trabajadores con tecnicas configuradas en su maestro.

Regla explicita del taller, que manda sobre el Excel: **un trabajador tiene sus
tecnicas configuradas**. Al elegirlo en una cotizacion se cargan esas tecnicas,
y el backend rechaza combinar a alguien con una tecnica que no tiene. Hasta aqui
`v2_workers` y `v2_techniques` no estaban relacionadas: cualquier trabajador
podia cotizarse con cualquier tecnica activa.

Dos movimientos, los dos aditivos:

1. `v2_worker_techniques` — el par trabajador-tecnica, unico, con `active`.
2. `v2_techniques.manual_hours` — la tecnica cuyo tiempo se decide a mano (el
   «personal adicional» del Excel). Falso para todas las que ya existen.

## Backfill

Cada par (trabajador, tecnica) que YA aparece en alguna tarea cotizada se da de
alta como capacidad activa. Sin esto, un borrador existente no podria volver a
elegir la misma combinacion con la que se armo, y la regla nueva encallaria
trabajo hecho antes de existir. No se inventa ninguna capacidad que nadie haya
usado: un trabajador sin historial queda sin tecnicas hasta que alguien las
configure.

No toca Legacy, ni el inventario, ni ninguna cifra de ninguna cotizacion.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "v2_techniques",
        sa.Column("manual_hours", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.create_table(
        "v2_worker_techniques",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column("technique_id", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["worker_id"], ["v2_workers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["technique_id"], ["v2_techniques.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("worker_id", "technique_id", name="uq_v2_worker_techniques_pair"),
    )
    op.create_index(
        "ix_v2_worker_techniques_technique_id", "v2_worker_techniques", ["technique_id"]
    )

    # Las combinaciones ya cotizadas siguen siendo posibles.
    op.execute(
        sa.text(
            "INSERT INTO v2_worker_techniques (worker_id, technique_id)"
            " SELECT DISTINCT worker_id, technique_id FROM v2_quotation_labor"
        )
    )


def downgrade() -> None:
    """Se niega a revertir si alguien ya configuro capacidades o horas manuales.

    El backfill se puede rehacer; una capacidad dada de alta a mano, o una
    tecnica marcada como de horas manuales, no. Revertir las borraria y la
    cotizacion volveria a dejar combinar a cualquiera con cualquier cosa.
    """
    conexion = op.get_bind()
    configuradas = (
        conexion.scalar(
            sa.text(
                "SELECT count(*) FROM v2_worker_techniques wt"
                " WHERE NOT EXISTS (SELECT 1 FROM v2_quotation_labor l"
                "   WHERE l.worker_id = wt.worker_id AND l.technique_id = wt.technique_id)"
                "    OR wt.active IS FALSE"
            )
        )
        or 0
    )
    manuales = (
        conexion.scalar(sa.text("SELECT count(*) FROM v2_techniques WHERE manual_hours")) or 0
    )
    if configuradas or manuales:
        raise RuntimeError(
            f"0035 no puede revertirse: hay {configuradas} capacidad(es) de trabajador "
            f"configuradas a mano y {manuales} tecnica(s) de horas manuales. Revertir "
            "borraria esa configuracion."
        )
    op.drop_index("ix_v2_worker_techniques_technique_id", table_name="v2_worker_techniques")
    op.drop_table("v2_worker_techniques")
    op.drop_column("v2_techniques", "manual_hours")
