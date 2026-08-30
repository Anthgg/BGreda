"""Politica comercial configurable: factor de produccion y paso de redondeo (Fase 009E).

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-30

Dos columnas en `commercial_settings`, y ninguna mas:

1. `production_factor_default` — el factor de PRODUCCION que multiplica el
   costo tecnico. Nada que ver con `default_quotation_factor`, que se deriva
   del markup y pertenece a la via heredada: son dos pasos distintos del
   costeo y confundirlos cobraria el margen dos veces.

2. `rounding_step` — el paso del redondeo contractual. Solo 0,50 o 1,00; el
   CHECK lo impone en la base para que ningun camino pueda dejar un tercer
   valor y producir precios que no sean multiplos de nada.

Los costos fijos NO se mueven aqui: siguen siendo los maestros `other_costs`
(OTH-001/002/003). Meterlos en la configuracion seria duplicar el maestro y
tener dos sitios donde cambiar el alquiler.

Migracion aditiva: ninguna fila se reescribe. La fila singleton existente
recibe los valores por `server_default`, y ademas se fija explicitamente con
un UPDATE para no depender de que el default se haya aplicado.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "commercial_settings",
        sa.Column(
            "production_factor_default",
            sa.Numeric(9, 6),
            nullable=False,
            server_default=sa.text("3"),
        ),
    )
    op.create_check_constraint(
        op.f("ck_commercial_settings_production_factor_default_positive"),
        "commercial_settings",
        "production_factor_default > 0",
    )

    op.add_column(
        "commercial_settings",
        sa.Column(
            "rounding_step",
            sa.Numeric(9, 6),
            nullable=False,
            server_default=sa.text("0.50"),
        ),
    )
    # Solo dos pasos. En la base y no solo en Pydantic: una politica de precios
    # que admitiera 0,25 produciria importes que no son multiplos de nada, y el
    # esquema es el unico sitio por el que no se puede colar.
    op.create_check_constraint(
        op.f("ck_commercial_settings_rounding_step_allowed"),
        "commercial_settings",
        "rounding_step IN (0.50, 1.00)",
    )

    # Backfill explicito. `server_default` cubre la fila existente, pero
    # dejarlo implicito significa no poder demostrar que quedo bien: el test de
    # migracion comprueba estos valores con un SELECT real.
    op.execute(
        sa.text(
            "UPDATE commercial_settings SET production_factor_default = 3, rounding_step = 0.50"
        )
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_commercial_settings_rounding_step_allowed"),
        "commercial_settings",
        type_="check",
    )
    op.drop_column("commercial_settings", "rounding_step")
    op.drop_constraint(
        op.f("ck_commercial_settings_production_factor_default_positive"),
        "commercial_settings",
        type_="check",
    )
    op.drop_column("commercial_settings", "production_factor_default")
