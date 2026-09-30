"""Fase 010P W2 — capability MASTERS_QUICK_CREATE en perfiles.

Revision ID: 0044
Revises: 0043
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column(
            "capabilities",
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    op.create_check_constraint(
        "capabilities_allowed",
        "profiles",
        "capabilities <@ ARRAY['MASTERS_QUICK_CREATE']::text[]",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE asignadas integer;
            BEGIN
                SELECT count(*) INTO asignadas
                  FROM profiles
                 WHERE cardinality(capabilities) > 0;
                IF asignadas > 0 THEN
                    RAISE EXCEPTION
                        'No se puede bajar 0044: % perfiles perderian capacidades RBAC',
                        asignadas;
                END IF;
            END $$;
            """
        )
    )
    op.drop_constraint("capabilities_allowed", "profiles", type_="check")
    op.drop_column("profiles", "capabilities")
