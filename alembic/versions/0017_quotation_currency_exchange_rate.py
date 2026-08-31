"""Moneda de emision y tipo de cambio manual por cotizacion (Fase 009F).

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-30

PEN sigue siendo la moneda base del sistema: los costos, el inventario y los
maestros no se mueven de ahi. Lo que 009F agrega es la moneda en la que se
EMITE la cotizacion, y el tipo de cambio con el que se convirtio el precio
neto comercial.

`currency_code_snapshot` ya existia y no se toca. Faltaban dos cosas:

1. `exchange_rate_snapshot` — cuantos soles vale un dolar. La semantica es
   `1 USD = X PEN`, asi que el neto en dolares se obtiene DIVIDIENDO. Guardar
   la tasa por cotizacion (y no globalmente) es lo que permite que cambiar el
   tipo de cambio manana no altere lo ya confirmado.

2. `exchange_rate_source_snapshot` — de donde salio esa tasa. En 009F solo
   existe `MANUAL`; la columna esta para que el dia que aparezca una fuente
   automatica se distinga de la que tecleo una persona, sin tener que adivinar
   mirando fechas.

Para PEN las dos quedan NULL. Guardar `1` seria inventarse una conversion que
nunca ocurrio, y ademas haria indistinguible «no aplica» de «alguien escribio
un 1». El CHECK de coherencia hace que los dos unicos estados con sentido sean
los dos unicos representables.

Migracion aditiva y nullable: ninguna fila se reescribe. Las cotizaciones
historicas son todas PEN y quedan con NULL, que es exactamente lo que el
contrato exige de ellas.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Los dos unicos estados con sentido. Se escribe una vez y se usa en el CHECK
#: para que el esquema, y no solo Pydantic, sea quien los impone.
COHERENCIA = """
    (
        currency_code_snapshot = 'PEN'
        AND exchange_rate_snapshot IS NULL
        AND exchange_rate_source_snapshot IS NULL
    )
    OR (
        currency_code_snapshot = 'USD'
        AND exchange_rate_snapshot IS NOT NULL
        AND exchange_rate_snapshot > 0
        AND exchange_rate_source_snapshot = 'MANUAL'
    )
"""


def upgrade() -> None:
    op.add_column(
        "quotations",
        sa.Column(
            # NUMERIC(18,6) es la convencion MONEY de app/core/precision.py: el
            # divisor comparte escala con los importes que divide, y no hay
            # tope que una crisis cambiaria pueda alcanzar.
            "exchange_rate_snapshot",
            sa.Numeric(18, 6),
            nullable=True,
        ),
    )
    op.add_column(
        "quotations",
        sa.Column("exchange_rate_source_snapshot", sa.String(16), nullable=True),
    )

    # Explicito y aparte: sin el, un `currency <> 'PEN'` dejaria pasar EUR
    # siempre que trajera tasa. 009F autoriza dos monedas, no un mercado.
    op.create_check_constraint(
        op.f("ck_quotations_currency_supported"),
        "quotations",
        "currency_code_snapshot IN ('PEN', 'USD')",
    )

    # Positividad por separado, para que un rechazo diga cual es el problema.
    op.create_check_constraint(
        op.f("ck_quotations_exchange_rate_positive"),
        "quotations",
        "exchange_rate_snapshot IS NULL OR exchange_rate_snapshot > 0",
    )

    op.create_check_constraint(
        op.f("ck_quotations_exchange_rate_coherence"),
        "quotations",
        COHERENCIA,
    )

    # Sin backfill. Las filas existentes son PEN y quedan con NULL en ambas
    # columnas, que es justo lo que el CHECK de coherencia pide de ellas: un
    # UPDATE aqui escribiria lo que ya vale y ensuciaria `updated_at`.


def downgrade() -> None:
    op.drop_constraint(op.f("ck_quotations_exchange_rate_coherence"), "quotations", type_="check")
    op.drop_constraint(op.f("ck_quotations_exchange_rate_positive"), "quotations", type_="check")
    op.drop_constraint(op.f("ck_quotations_currency_supported"), "quotations", type_="check")
    op.drop_column("quotations", "exchange_rate_source_snapshot")
    op.drop_column("quotations", "exchange_rate_snapshot")
