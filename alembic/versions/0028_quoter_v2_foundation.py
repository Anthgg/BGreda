"""Fase 010A — el Cotizador V2 empieza a existir, sin tocar lo que ya existia.

Tres movimientos, los tres aditivos:

1. `quotations.pricing_engine_version`, NOT NULL con `server_default 'LEGACY'`.
   Las cotizaciones que ya hay quedan **selladas** como Legacy, que es
   literalmente lo que son: se calcularon con ese motor. No se recalcula ni un
   total, ni un snapshot, ni un PDF. El CHECK que la acompana solo admite
   'LEGACY', de modo que esta tabla no podra albergar jamas un documento V2.

2. tabla `v2_quotations`, vacia, con el CHECK espejo: aqui solo entra 'V2'.
   Los dos CHECK juntos son la frontera. Sin ellos el aislamiento dependeria de
   que nadie escriba el INSERT equivocado, y eso no es una garantia.

3. el tipo de secuencia `QUOTE_V2` con su fila `CTZ-V2`. El CHECK de
   `document_sequences.sequence_type` solo se AMPLIA —acepta lo que aceptaba
   mas el nuevo—, el mismo movimiento que hicieron 0021 y 0023.

**Nada se rellena hacia atras mas alla del sello.** No hay backfill de datos de
negocio, no se crea ninguna cotizacion V2 a partir de una Legacy y ningun
importe cambia de valor. El backend anterior a 010A sigue funcionando contra
este esquema: la columna nueva tiene default y la tabla nueva no le concierne.

Sobre el coste del sello: en PostgreSQL 11+ anadir una columna NOT NULL con
`server_default` constante no reescribe la tabla —el valor se resuelve desde el
catalogo—, asi que el sellado no toca fisicamente las filas historicas ni
bloquea la tabla mas que un instante.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


SEQUENCE_TYPES_BEFORE = (
    "'QUOTE', 'FIRING', 'PRODUCT_50', 'PRODUCT_70', 'PREPARATION', "
    "'PRODUCTION_ORDER', 'PROTOTYPE', 'PROTOTYPE_QUOTE'"
)
SEQUENCE_TYPES_AFTER = f"{SEQUENCE_TYPES_BEFORE}, 'QUOTE_V2'"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. El sello de motor sobre la cabecera historica
    # ------------------------------------------------------------------
    op.add_column(
        "quotations",
        sa.Column(
            "pricing_engine_version",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'LEGACY'"),
        ),
    )
    op.create_check_constraint(
        "engine_is_legacy", "quotations", "pricing_engine_version = 'LEGACY'"
    )

    # ------------------------------------------------------------------
    # 2. La cabecera del motor nuevo
    # ------------------------------------------------------------------
    op.create_table(
        "v2_quotations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column(
            "pricing_engine_version",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'V2'"),
        ),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'DRAFT'")
        ),
        sa.Column(
            "production_type",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'RETAIL'"),
        ),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("customer_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_name", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["partners.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("code", name="uq_v2_quotations_code"),
        sa.CheckConstraint("pricing_engine_version = 'V2'", name="engine_is_v2"),
        sa.CheckConstraint("status IN ('DRAFT', 'CONFIRMED', 'CANCELLED')", name="status_allowed"),
        sa.CheckConstraint(
            "production_type IN ('RETAIL', 'WHOLESALE')", name="production_type_allowed"
        ),
    )
    op.create_index("ix_v2_quotations_customer_id", "v2_quotations", ["customer_id"])
    op.create_index("ix_v2_quotations_status", "v2_quotations", ["status"])
    op.create_index("ix_v2_quotations_created_at", "v2_quotations", ["created_at"])

    # ------------------------------------------------------------------
    # 3. El talonario propio: CTZ-V2-2026-000001
    # ------------------------------------------------------------------
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint(
        "type_allowed", "document_sequences", f"sequence_type IN ({SEQUENCE_TYPES_AFTER})"
    )
    op.execute(
        sa.text(
            """
            INSERT INTO document_sequences
                (sequence_type, prefix, pattern, padding, reset_policy,
                 current_value, period_key, active)
            SELECT 'QUOTE_V2', 'CTZ-V2', :pattern, 6, 'YEARLY', 0, '', true
            WHERE NOT EXISTS (
                SELECT 1 FROM document_sequences WHERE sequence_type = 'QUOTE_V2'
            )
            """
        ).bindparams(pattern="{PREFIX}-{YYYY}-{NUMBER}")
    )


def downgrade() -> None:
    """Se niega a revertir si ya hay algo que perder.

    Dos cosas cuentan como «algo que perder», y las dos bloquean:

    1. una cotizacion V2 viva. Tiene numero emitido y pudo enviarse a un
       cliente; borrar la tabla la haria desaparecer sin rastro;
    2. un correlativo `QUOTE_V2` ya entregado, aunque su cotizacion se haya
       borrado despues. `document_sequence_issues` es el registro **inmutable**
       de que ese numero se gasto, y no se toca: un correlativo entregado no
       se reutiliza ni se desmiente. Ademas, revertir dejando el registro y
       volviendo a aplicar la revision mas tarde sembraria el contador en cero
       contra unos numeros que ya existen, y el primer alta chocaria con el
       UNIQUE.

    Es el mismo criterio que 0023 aplica a las cotizaciones de prototipo.
    """
    conexion = op.get_bind()
    documentos = conexion.scalar(sa.text("SELECT count(*) FROM v2_quotations")) or 0
    correlativos = (
        conexion.scalar(
            sa.text(
                "SELECT count(*) FROM document_sequence_issues WHERE sequence_type = 'QUOTE_V2'"
            )
        )
        or 0
    )
    if documentos or correlativos:
        raise RuntimeError(
            f"0028 no puede revertirse: hay {documentos} cotizacion(es) V2 y "
            f"{correlativos} correlativo(s) CTZ-V2 ya entregado(s). Revertir borraria "
            "documentos y desmentiria numeros que ya se emitieron."
        )

    op.drop_index("ix_v2_quotations_created_at", table_name="v2_quotations")
    op.drop_index("ix_v2_quotations_status", table_name="v2_quotations")
    op.drop_index("ix_v2_quotations_customer_id", table_name="v2_quotations")
    op.drop_table("v2_quotations")

    # Primero la fila, despues el CHECK: al reves, restringir el CHECK con la
    # fila 'QUOTE_V2' todavia dentro fallaria la validacion de la tabla.
    op.execute(sa.text("DELETE FROM document_sequences WHERE sequence_type = 'QUOTE_V2'"))
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint(
        "type_allowed", "document_sequences", f"sequence_type IN ({SEQUENCE_TYPES_BEFORE})"
    )

    # Nombre corto, no el completo: alembic aplica la convencion de nombres
    # del proyecto tambien al soltar, y pasarle `ck_quotations_engine_is_legacy`
    # acabaria buscando `ck_quotations_ck_quotations_engine_is_legacy`.
    op.drop_constraint("engine_is_legacy", "quotations", type_="check")
    op.drop_column("quotations", "pricing_engine_version")
