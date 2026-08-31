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

Nullable y sin backfill, deliberadamente. No existe ningun rastro de que valia
el ajuste cuando se confirmaron las cotizaciones antiguas: `PUT
/settings/commercial` no escribe en `audit_events` (COMMERCIAL_SETTINGS_AUDIT_GAP,
pendiente y fuera del alcance de esta migracion). Rellenarlas con el valor de
hoy no recuperaria su vigencia; le pondria a un documento historico un dato
inventado con aspecto de dato real. NULL dice la verdad —no se capturo— y el
PDF ya sabe callarse cuando no hay vigencia que mostrar.
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
