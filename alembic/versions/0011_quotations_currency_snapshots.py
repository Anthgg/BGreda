"""Fase 006A: Snapshots historicos de moneda en cotizaciones.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-24

Agrega columnas currency_code_snapshot y currency_symbol_snapshot a quotations
para garantizar la inmutabilidad de la moneda en cotizaciones confirmadas e historicas.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "quotations",
        sa.Column(
            "currency_code_snapshot",
            sa.String(length=3),
            nullable=False,
            server_default=sa.text("'PEN'"),
        ),
    )
    op.add_column(
        "quotations",
        sa.Column(
            "currency_symbol_snapshot",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'S/'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("quotations", "currency_symbol_snapshot")
    op.drop_column("quotations", "currency_code_snapshot")
