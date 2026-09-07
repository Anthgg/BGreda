"""Fase 009K.2 — quien preparo y quien emitio cada documento.

Hasta aqui el sistema sabia QUE se cotizo y CUANDO, pero de QUIEN sólo
guardaba, y a medias, el identificador de quien creo la cotizacion. Un
documento comercial lo firma alguien, y ese alguien tiene que salir en el
papel.

Se anaden cinco columnas, todas anulables:

    quotations.created_by_name        el nombre visible de quien la escribio
    quotations.confirmed_by_id        quien la emitio
    quotations.confirmed_by_name      su nombre visible
    prototype_quotations.confirmed_by       lo mismo, en el CPR
    prototype_quotations.confirmed_by_name

Los `*_name` NO son un dato repetido del perfil: son una COPIA del momento.
El identificador dice a quien preguntar hoy; el nombre dice que ponia en el
papel aquel dia. Si esa persona se renombra o deja la casa, un documento de
hace un ano no puede empezar a decir otra cosa. Es el mismo criterio que ya
seguian `audit_events.user_display_name` y `prototype_quotations.created_by_name`.

**Ni una fila se toca.** No hay backfill: los documentos anteriores no
registraron a nadie, y asignarlos al administrador actual convertiria un hueco
honesto en una afirmacion falsa. Se quedan en NULL, y la presentacion dice
«No registrado», que es exactamente lo que se sabe.

Asimetria a proposito: en `quotations` la columna vieja se llama
`created_by_id` y en `prototype_quotations`, `created_by`. Las nuevas siguen a
su vecina en cada tabla en vez de imponer un nombre comun. Renombrar lo
existente por simetria seria un cambio destructivo a cambio de nada.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Mismo tamano que `prototype_quotations.created_by_name`, que ya existia.
#: Un nombre visible cabe de sobra; el limite del perfil son 120.
_NOMBRE = sa.String(length=200)


def upgrade() -> None:
    op.add_column("quotations", sa.Column("created_by_name", _NOMBRE, nullable=True))
    op.add_column(
        "quotations",
        sa.Column("confirmed_by_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("quotations", sa.Column("confirmed_by_name", _NOMBRE, nullable=True))

    op.add_column(
        "prototype_quotations",
        sa.Column("confirmed_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("prototype_quotations", sa.Column("confirmed_by_name", _NOMBRE, nullable=True))


def downgrade() -> None:
    # Retirar columnas anulables recien anadidas no destruye nada que existiera
    # antes de esta migracion: lo unico que se pierde son los actores que se
    # hayan registrado desde entonces, y eso es inherente a volver atras.
    op.drop_column("prototype_quotations", "confirmed_by_name")
    op.drop_column("prototype_quotations", "confirmed_by")
    op.drop_column("quotations", "confirmed_by_name")
    op.drop_column("quotations", "confirmed_by_id")
    op.drop_column("quotations", "created_by_name")
