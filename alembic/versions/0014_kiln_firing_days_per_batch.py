"""Duracion de una hornada, configurable por horno (Fase 009C).

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-28

Hasta ahora el Cotizador asumia 3 dias por hornada para cualquier horno. La
regla real del taller es que la duracion depende del horno: el pequeno tarda 3
dias y el grande 4. No existia ningun sitio donde guardarlo —ni en ``kilns``,
ni en ``kiln_rates``, ni en ``kiln_occupancy_factors``, ni en
``commercial_settings``— asi que la constante en codigo era la unica autoridad.

Esta migracion crea esa configuracion.

Sobre el ``server_default``: el 3 es **solo** para poder anadir una columna NOT
NULL a una tabla con filas. Es el valor historico, de modo que ningun horno ya
existente cambia de duracion por el hecho de migrar. El backfill posterior
corrige el unico horno cuyo valor real no es 3.

Sobre el backfill por ``code``: identificar el horno grande por su codigo es
aceptable en una migracion —es un dato puntual de este entorno, ejecutado una
vez—, pero **no** en la logica de negocio, que a partir de aqui lee siempre
``kilns.firing_days_per_batch``. En entornos donde ese codigo no exista el
UPDATE simplemente no afecta a ninguna fila.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Duracion historica, la que el sistema aplicaba a todos los hornos.
LEGACY_DAYS_PER_BATCH = 3
#: Duracion real del horno grande segun la regla del taller.
LARGE_KILN_DAYS_PER_BATCH = 4
LARGE_KILN_CODE = "KILN-002"


def upgrade() -> None:
    op.add_column(
        "kilns",
        sa.Column(
            "firing_days_per_batch",
            sa.Integer(),
            nullable=False,
            server_default=sa.text(str(LEGACY_DAYS_PER_BATCH)),
        ),
    )
    op.create_check_constraint(
        "ck_kilns_firing_days_per_batch_positive",
        "kilns",
        "firing_days_per_batch >= 1",
    )
    op.execute(
        sa.text("UPDATE kilns SET firing_days_per_batch = :days WHERE code = :code").bindparams(
            days=LARGE_KILN_DAYS_PER_BATCH, code=LARGE_KILN_CODE
        )
    )


def downgrade() -> None:
    op.drop_constraint("ck_kilns_firing_days_per_batch_positive", "kilns", type_="check")
    op.drop_column("kilns", "firing_days_per_batch")
