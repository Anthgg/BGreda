"""Vigencia comercial congelada al confirmar (Fase 009G).

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-31

El PDF de una cotizacion confirmada decia «Cotizacion valida por N dias»
leyendo `commercial_settings.quote_validity_days` en el momento de generar el
documento. Ese numero es configuracion viva: cambiarlo de 20 a 30 movia la
vigencia de todos los PDF ya entregados, incluidos los firmados. Un documento
comercial no puede cambiar de contenido porque manana alguien edite un ajuste.

Esta columna congela el plazo en la transicion real a CONFIRMED, junto al
resto de la politica que ya se congelaba ahi: la moneda, el tipo de cambio, el
IGV y su procedencia. Se guarda el numero de dias, no una fecha: el documento
nunca mostro un «valida hasta», y calcular uno ahora seria inventar contenido
contractual que nadie acordo.

Nullable y sin backfill. Las confirmadas anteriores quedan en NULL, y el PDF ya
sabe callarse cuando no hay vigencia que mostrar: dice «no se capturo», que es
la verdad, en vez de afirmar un plazo que esta fila nunca guardo.

Conviene dejar dicho lo que NO justifica esa decision, porque a primera vista
lo parece: si existe rastro historico. `update_commercial` audita sus cambios
(app/services/settings.py), y en produccion `quote_validity_days` tiene un
unico evento —de nulo a 20, el 2026-08-22— y ninguno mas. Las 17 confirmadas
son todas posteriores, asi que su plazo se podria DEDUCIR de evidencia real, no
inventar. Aqui no se hace porque el backfill no esta autorizado, no porque sea
imposible. Si algun dia se autoriza, la fuente es esa tabla de auditoria y
nunca el valor que la configuracion tenga ese dia.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Mismo rango que `commercial_settings.quote_validity_days` en la 0002. El
#: snapshot no puede admitir plazos que su origen rechaza, o la copia acabaria
#: siendo mas permisiva que el original.
RANGO = """
    validity_days_snapshot IS NULL
    OR (
        validity_days_snapshot > 0
        AND validity_days_snapshot <= 3650
    )
"""


def upgrade() -> None:
    op.add_column(
        "quotations",
        sa.Column("validity_days_snapshot", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_quotations_validity_days_snapshot_range"),
        "quotations",
        RANGO,
    )

    # Sin backfill: ver el encabezado. Las confirmadas historicas quedan en
    # NULL a proposito, y el CHECK las admite tal cual.


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_quotations_validity_days_snapshot_range"), "quotations", type_="check"
    )
    op.drop_column("quotations", "validity_days_snapshot")
