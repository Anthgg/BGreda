"""Permite origen MIXED en tax_rate_source_snapshot para cotizaciones multiproducto.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-26

Actualiza el check constraint ck_quotations_tax_rate_source_allowed en la tabla quotations
para permitir los valores: PRODUCT, COMMERCIAL_SETTINGS y MIXED.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_quotations_tax_rate_source_allowed", "quotations", type_="check")
    op.create_check_constraint(
        "ck_quotations_tax_rate_source_allowed",
        "quotations",
        "tax_rate_source_snapshot IN ('PRODUCT', 'COMMERCIAL_SETTINGS', 'MIXED')",
    )


def downgrade() -> None:
    # Un downgrade con cotizaciones 'MIXED' violaria el constraint narrower.
    # Bloquear explicitamente es seguro y previene fallos silenciosos o corrupcion.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM quotations WHERE tax_rate_source_snapshot = 'MIXED') THEN
                RAISE EXCEPTION '0013 downgrade bloqueado: existen cotizaciones MIXED';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint("ck_quotations_tax_rate_source_allowed", "quotations", type_="check")
    op.create_check_constraint(
        "ck_quotations_tax_rate_source_allowed",
        "quotations",
        "tax_rate_source_snapshot IN ('PRODUCT', 'COMMERCIAL_SETTINGS')",
    )
