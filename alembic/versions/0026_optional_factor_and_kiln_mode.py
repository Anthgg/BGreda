"""Fase 009K.3 — el factor deja de ser obligatorio y el horno gana un modo.

Dos decisiones comerciales que hasta ahora no se podian ni escribir:

1. **El factor de produccion pasa a ser opcional.** Multiplicaba siempre el
   costo tecnico y no habia forma de no aplicarlo: `production_factor = 0` lo
   rechazan el motor de precios (`price_line`), el CHECK
   `production_factor_default_positive` y el propio esquema de entrada
   (`gt=0`). Apagarlo con un cero era, literalmente, imposible. Se representa
   entonces con una BANDERA, y el multiplicador efectivo pasa a ser 1.

2. **El horno gana un modo de carga.** El Cotizador siempre planifico en
   conjunto: una sola hoja donde todos los productos comparten sesion, el
   volumen se acumula y el costo se reparte por participacion. Eso sigue
   siendo `TOGETHER`. Lo nuevo es `PER_PRODUCT`, donde cada producto planifica
   su propia hornada aunque el horno sea el mismo.

Cuatro columnas, todas anulables:

    quotations.production_factor_enabled
    quotations.kiln_mode
    commercial_settings.production_factor_enabled_default
    commercial_settings.kiln_mode_default

**Ni una fila se toca.** NULL no es un tercer estado que haya que rellenar: es
historia con lectura conocida. En `kiln_mode`, NULL se lee `TOGETHER` porque la
auditoria demostro que ese fue el comportamiento real de todo lo anterior. En
`production_factor_enabled`, NULL se lee del factor que quedo guardado en el
snapshot de la propia cotizacion, que es donde estaba la verdad desde 009E.
Escribir hoy una bandera sobre documentos ya confirmados seria reinterpretar
precios que alguien firmo.

Los CHECK existentes —`production_factor_default > 0`, `commercial_factor > 0`—
NO se tocan. Siguen siendo ciertos: lo que cambia es que ahora se puede no
aplicar el factor, no que el factor pueda valer cero.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `VARCHAR` con CHECK y no un ENUM nativo, como el resto del proyecto: anadir
#: un modo el dia de manana no debe exigir un `ALTER TYPE`.
_MODO = sa.String(length=20)

#: Los nombres de restriccion van DESNUDOS. `alembic/env.py` entrega un
#: `target_metadata` cuya convencion es `ck_%(table_name)s_%(constraint_name)s`
#: y `create_check_constraint` la APLICA: pasar el nombre completo produce
#: `ck_quotations_ck_quotations_...`. Costo un despliegue en 0024.
_CK_QUOTATION = "kiln_mode_allowed"
_CK_SETTINGS = "kiln_mode_default_allowed"

#: La misma condicion en los dos sitios, escrita una sola vez.
_MODOS_VALIDOS = "IN ('TOGETHER', 'PER_PRODUCT')"


def upgrade() -> None:
    op.add_column(
        "quotations",
        sa.Column("production_factor_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column("quotations", sa.Column("kiln_mode", _MODO, nullable=True))
    op.add_column(
        "commercial_settings",
        sa.Column("production_factor_enabled_default", sa.Boolean(), nullable=True),
    )
    op.add_column("commercial_settings", sa.Column("kiln_mode_default", _MODO, nullable=True))

    # Un modo invalido no daria error en el codigo: elegiria en silencio la
    # rama TOGETHER, y nadie se enteraria hasta ver una factura.
    op.create_check_constraint(
        _CK_QUOTATION,
        "quotations",
        f"kiln_mode IS NULL OR kiln_mode {_MODOS_VALIDOS}",
    )
    op.create_check_constraint(
        _CK_SETTINGS,
        "commercial_settings",
        f"kiln_mode_default IS NULL OR kiln_mode_default {_MODOS_VALIDOS}",
    )


def downgrade() -> None:
    # Volver atras solo pierde lo que se haya elegido desde esta migracion, que
    # es inherente a volver atras. Nada anterior a 0026 dependia de estas
    # columnas: sin ellas el motor vuelve a aplicar el factor siempre y a
    # planificar en conjunto, que es lo que hacia.
    op.drop_constraint(_CK_SETTINGS, "commercial_settings", type_="check")
    op.drop_constraint(_CK_QUOTATION, "quotations", type_="check")
    op.drop_column("commercial_settings", "kiln_mode_default")
    op.drop_column("commercial_settings", "production_factor_enabled_default")
    op.drop_column("quotations", "kiln_mode")
    op.drop_column("quotations", "production_factor_enabled")
