"""Fase 010H — emision, vigencia, duplicacion y puente a produccion del Cotizador V2.

Todo aditivo. Ni una columna de Legacy, ni `production_orders`, ni el
inventario: el Cotizador historico y la produccion existente siguen
exactamente igual.

Tres bloques:

1. `v2_quotations` — la emision (`issued_at`, `valid_until`, `expires_at`,
   quien emitio y la huella comercial), los datos del cliente y las
   condiciones con los que se emitio, la cancelacion, el rastro de
   duplicacion y las observaciones para el cliente. Todo NULL: un borrador no
   tiene nada de eso, y las cotizaciones que ya existen son borradores.
2. `v2_quotation_products.client_observation` — la columna «Observacion» de la
   hoja PDF cliente.
3. `v2_production_handoffs` — el puente, UNICO por cotizacion.

## Por que no hay estado EXPIRED

El CHECK de estado NO cambia. «Vencida» y «lista para produccion» se derivan
de las fechas y del puente: guardarlas exigiria un proceso a medianoche que un
servicio que escala a cero no garantiza. Ver `app/core/quoter_v2_lifecycle.py`.

## Antes de anadir el CHECK de ciclo de vida

Hasta hoy no existia forma de emitir ni de cancelar una cotizacion V2, asi que
toda fila es un borrador. Si alguien hubiera cambiado el estado a mano, el
CHECK nuevo fallaria al validarse con un error opaco; la migracion lo comprueba
antes y lo dice con claridad. No inventa una fecha de emision para arreglarlo:
eso seria fabricar un historico.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


LIFECYCLE_COHERENT = (
    "(status IS NOT NULL AND status = 'DRAFT'"
    " AND issued_at IS NULL AND valid_until IS NULL AND expires_at IS NULL"
    " AND commercial_fingerprint IS NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'CONFIRMED'"
    " AND issued_at IS NOT NULL AND valid_until IS NOT NULL AND expires_at IS NOT NULL"
    " AND commercial_fingerprint IS NOT NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'CANCELLED' AND cancelled_at IS NOT NULL"
    " AND ((issued_at IS NULL AND valid_until IS NULL AND expires_at IS NULL)"
    "   OR (issued_at IS NOT NULL AND valid_until IS NOT NULL AND expires_at IS NOT NULL"
    "       AND commercial_fingerprint IS NOT NULL)))"
)

QUOTATION_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("client_notes", sa.Text()),
    ("issued_at", sa.DateTime(timezone=True)),
    ("valid_until", sa.Date()),
    ("expires_at", sa.DateTime(timezone=True)),
    ("issued_by", postgresql.UUID(as_uuid=True)),
    ("issued_by_name", sa.String(length=200)),
    ("commercial_fingerprint", sa.String(length=64)),
    ("customer_document_type_snapshot", sa.String(length=16)),
    ("customer_document_number_snapshot", sa.String(length=20)),
    ("customer_address_snapshot", sa.String(length=240)),
    ("customer_email_snapshot", sa.String(length=160)),
    ("customer_phone_snapshot", sa.String(length=32)),
    ("conditions_snapshot", sa.Text()),
    ("payment_notes_snapshot", sa.Text()),
    ("cancelled_at", sa.DateTime(timezone=True)),
    ("cancelled_by", postgresql.UUID(as_uuid=True)),
    ("cancelled_by_name", sa.String(length=200)),
    ("cancel_reason", sa.Text()),
    ("duplicated_from_id", sa.Integer()),
)

QUOTATION_CHECKS: tuple[tuple[str, str], ...] = (
    ("lifecycle_coherent", LIFECYCLE_COHERENT),
    ("expires_after_issue", "expires_at IS NULL OR issued_at IS NULL OR expires_at > issued_at"),
    ("not_duplicated_from_itself", "duplicated_from_id IS NULL OR duplicated_from_id <> id"),
)


def upgrade() -> None:
    # En modo offline (`alembic upgrade head --sql`, que la CI usa para comprobar
    # que la migracion se puede renderizar) no hay base que consultar: la
    # comprobacion previa solo tiene sentido con una conexion real.
    no_borradores = 0
    if not context.is_offline_mode():
        conexion = op.get_bind()
        no_borradores = (
            conexion.scalar(sa.text("SELECT count(*) FROM v2_quotations WHERE status <> 'DRAFT'"))
            or 0
        )
    if no_borradores:
        raise RuntimeError(
            f"0034 no puede aplicarse: hay {no_borradores} cotizacion(es) V2 que no son "
            "borrador, pero hasta 010H no existia forma de emitirlas ni cancelarlas. "
            "Alguien cambio el estado a mano; revisalas antes de migrar en vez de "
            "inventarles una fecha de emision."
        )

    for nombre, tipo in QUOTATION_COLUMNS:
        op.add_column("v2_quotations", sa.Column(nombre, tipo, nullable=True))

    op.create_foreign_key(
        "fk_v2_quotations_duplicated_from_id_v2_quotations",
        "v2_quotations",
        "v2_quotations",
        ["duplicated_from_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_v2_quotations_duplicated_from_id", "v2_quotations", ["duplicated_from_id"])
    op.create_index(
        "uq_v2_quotations_open_duplicate",
        "v2_quotations",
        ["duplicated_from_id"],
        unique=True,
        postgresql_where=sa.text("duplicated_from_id IS NOT NULL AND status = 'DRAFT'"),
    )
    for nombre, expresion in QUOTATION_CHECKS:
        op.create_check_constraint(nombre, "v2_quotations", expresion)

    op.add_column(
        "v2_quotation_products", sa.Column("client_observation", sa.Text(), nullable=True)
    )

    op.create_table(
        "v2_production_handoffs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("v2_quotation_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'READY_FOR_PRODUCTION'"),
        ),
        sa.Column("commercial_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_name", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["v2_quotation_id"], ["v2_quotations.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("v2_quotation_id", name="uq_v2_production_handoffs_v2_quotation_id"),
        sa.CheckConstraint("status IN ('READY_FOR_PRODUCTION')", name="status_allowed"),
        sa.CheckConstraint(
            "length(btrim(commercial_fingerprint)) = 64", name="fingerprint_is_sha256"
        ),
    )


def downgrade() -> None:
    """Se niega a revertir si ya hay algo emitido, cancelado, duplicado o en produccion.

    Una emision es un compromiso con fecha: el cliente tiene un papel que dice
    hasta cuando vale. Revertir borraria la fecha, el vencimiento, los datos con
    los que se emitio y el puente a produccion, y dejaria filas CONFIRMED que
    ya no podrian explicar su propio documento. Mismo criterio que 0028 a 0033.
    """
    conexion = op.get_bind()
    comprometidas = (
        conexion.scalar(
            sa.text(
                "SELECT count(*) FROM v2_quotations"
                " WHERE status <> 'DRAFT' OR duplicated_from_id IS NOT NULL"
            )
        )
        or 0
    )
    puentes = conexion.scalar(sa.text("SELECT count(*) FROM v2_production_handoffs")) or 0
    if comprometidas or puentes:
        raise RuntimeError(
            f"0034 no puede revertirse: hay {comprometidas} cotizacion(es) V2 emitidas, "
            f"canceladas o duplicadas y {puentes} pase(s) a produccion. Revertir borraria "
            "la fecha de emision, la vigencia y la trazabilidad de documentos ya entregados."
        )

    op.drop_table("v2_production_handoffs")
    op.drop_column("v2_quotation_products", "client_observation")

    for nombre, _expresion in QUOTATION_CHECKS:
        op.drop_constraint(nombre, "v2_quotations", type_="check")
    op.drop_index("uq_v2_quotations_open_duplicate", table_name="v2_quotations")
    op.drop_index("ix_v2_quotations_duplicated_from_id", table_name="v2_quotations")
    op.drop_constraint(
        "fk_v2_quotations_duplicated_from_id_v2_quotations", "v2_quotations", type_="foreignkey"
    )
    for nombre, _tipo in reversed(QUOTATION_COLUMNS):
        op.drop_column("v2_quotations", nombre)
