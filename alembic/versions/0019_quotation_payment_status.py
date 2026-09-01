"""Eje de pago de la cotizacion, separado de su estado comercial (Fase 009H).

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-01

`status` sigue teniendo tres valores —DRAFT, CONFIRMED, CANCELLED— y no se
toca. Cobrar no es un cuarto estado: una cotizacion anulada puede estar pagada,
y una confirmada puede seguir sin cobrarse. Meter PAGADA en `status` obligaria
a elegir entre dos hechos que ocurren en ejes distintos.

De ahi las dos columnas nuevas. `payment_status` admite tres situaciones y la
tercera es la que importa:

- NULL  : el pago no lo registro el sistema. Es el estado de todo lo anterior
          a 009H, y NO quiere decir «impaga»: quiere decir que no sabemos.
- UNPAID: se sabe que no se ha cobrado.
- PAID  : se cobro, y `paid_at` dice cuando.

009H registra el hecho binario y su momento. No hay importe, ni medio de pago,
ni referencia bancaria, ni pagos parciales: no existen reglas acordadas para
nada de eso, y una columna vacia invita a rellenarla con suposiciones.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Los tres estados representables, y la fecha que cada uno exige. `paid_at`
#: sin PAID seria una fecha de cobro sin cobro; PAID sin `paid_at`, un cobro
#: sin fecha. El esquema impide las dos.
#:
#: Los `IS NOT NULL` de las dos ultimas ramas NO sobran. En SQL,
#: `NULL = 'PAID'` no es FALSE sino NULL, y un CHECK que evalua a NULL se da
#: por CUMPLIDO. Sin ellos, una fila con `payment_status` nulo y `paid_at`
#: puesta daba FALSE OR FALSE OR NULL = NULL y entraba tan campante: una fecha
#: de cobro colgando de una cotizacion de la que no sabemos si se cobro.
#:
#: Es el mismo agujero que 0017 tapo en el CHECK del tipo de cambio. Se repite
#: aqui porque la forma «OR de ramas con igualdad» lo invita cada vez.
COHERENCIA = """
    (payment_status IS NULL AND paid_at IS NULL)
    OR (
        payment_status IS NOT NULL
        AND payment_status = 'UNPAID'
        AND paid_at IS NULL
    )
    OR (
        payment_status IS NOT NULL
        AND payment_status = 'PAID'
        AND paid_at IS NOT NULL
    )
"""


def upgrade() -> None:
    # En DOS pasos, y no en uno, a proposito.
    #
    # `ADD COLUMN ... DEFAULT 'UNPAID'` en una sola sentencia haria que las
    # filas existentes LEYERAN 'UNPAID': PostgreSQL aplica el default a lo ya
    # guardado. Serian 347 cotizaciones afirmando que no se han cobrado cuando
    # de la mayoria no sabemos nada, y ese dato inventado no se distinguiria
    # despues de uno real.
    #
    # Primero la columna sin default, para que lo viejo quede en NULL.
    op.add_column(
        "quotations",
        sa.Column("payment_status", sa.String(16), nullable=True),
    )
    op.add_column(
        "quotations",
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Y ahora si el default, que solo alcanza a los INSERT futuros: una
    # cotizacion nueva nace sabiendo que aun no se ha cobrado.
    op.alter_column("quotations", "payment_status", server_default=sa.text("'UNPAID'"))

    op.create_check_constraint(
        op.f("ck_quotations_payment_status_allowed"),
        "quotations",
        "payment_status IS NULL OR payment_status IN ('UNPAID', 'PAID')",
    )
    op.create_check_constraint(
        op.f("ck_quotations_payment_coherence"),
        "quotations",
        COHERENCIA,
    )

    # Sin backfill. Ver el encabezado: NULL es la verdad sobre las filas
    # anteriores, y convertirlas a UNPAID seria afirmar algo que nadie sabe.


def downgrade() -> None:
    op.drop_constraint(op.f("ck_quotations_payment_coherence"), "quotations", type_="check")
    op.drop_constraint(op.f("ck_quotations_payment_status_allowed"), "quotations", type_="check")
    op.drop_column("quotations", "paid_at")
    op.drop_column("quotations", "payment_status")
