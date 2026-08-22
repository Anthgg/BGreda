"""Corrige la ortografia de los nombres de formatos de correlativos.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# La 0003 ya esta aplicada en Supabase, asi que el arreglo viaja en una
# migracion propia. El pattern es el identificador estable: tiene UNIQUE y la
# logica de correlativos depende de el, por eso no se toca.
_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("{PREFIX}-{YYYY}-{NUMBER}", "Prefijo - ano - numero", "Prefijo - año - número"),
    ("{PREFIX}-{YY}-{NUMBER}", "Prefijo - ano corto - numero", "Prefijo - año corto - número"),
    (
        "{PREFIX}-{YYYY}{MM}-{NUMBER}",
        "Prefijo - ano y mes - numero",
        "Prefijo - año y mes - número",
    ),
    ("{PREFIX}-{NUMBER}", "Prefijo - numero", "Prefijo - número"),
)

_UPDATE_SQL = """
UPDATE public.sequence_pattern_presets
SET name = :name
WHERE pattern = :pattern
  AND is_system
"""


def _rename(*, to_current: bool) -> None:
    for pattern, old_name, new_name in _RENAMES:
        target = new_name if to_current else old_name
        # bindparams (y no f-strings) para que el modo offline "alembic
        # upgrade head --sql" escriba los literales ya escapados.
        op.execute(
            sa.text(_UPDATE_SQL).bindparams(
                pattern=pattern,
                name=target,
            )
        )


def upgrade() -> None:
    _rename(to_current=True)


def downgrade() -> None:
    _rename(to_current=False)
