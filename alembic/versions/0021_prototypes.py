"""Prototipos: la muestra fisica previa a fabricar en serie (Fase 009K).

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-03

Hasta aqui el sistema sabia fabricar un pedido, pero no sabia hacer UNA pieza
de prueba antes de comprometerse. El taller lo llevaba en una hoja de calculo.

Esta migracion trae ese concepto al sistema, y lo trae APARTE de las ordenes de
produccion. No es una preferencia de organizacion: `production_orders` tiene un
UNIQUE por cotizacion —para que dos peticiones simultaneas no creen dos ordenes
del mismo pedido—, deriva sus materiales de la cotizacion confirmada en vez de
dejarlos elegir, y exige `quotation_id`. Un prototipo necesita convivir con la
orden final de la misma cotizacion, elegir a mano lo que gasta, y poder existir
sin cotizacion y sin producto. Las tres cosas chocan de frente.

Lo que crea:

- `prototypes`               : la muestra, su estado fisico y su evaluacion.
- `prototype_material_lines` : lo que ESA muestra gasta, elegido uno a uno.
- `stock_movements.prototype_id` : de que muestra salio cada consumo.
- `MovementType.PROTOTYPE_OUT`   : consumo de muestra, distinto del de produccion.
- `SequenceType.PROTOTYPE`       : el correlativo PRT-2026-000001.

Toda ella es ADITIVA. Un backend anterior a 009K sobre este esquema sigue
sirviendo: no se renombra ni se estrecha nada, no se anade ningun NOT NULL sin
default a una tabla en uso, y la unica restriccion que se reemplaza solo se
AMPLIA (acepta lo que aceptaba mas un valor nuevo).

BACKFILL: NONE. No hay prototipos historicos que modelar; los del Excel del
taller son datos de ejemplo y no se importan, porque inventar una muestra que
nadie fabrico seria inventar historia.

DOWNGRADE: destructivo por naturaleza —bajar borraria muestras y su evaluacion—
asi que se NIEGA cuando hay algo que perder, en vez de perderlo. Ver
`downgrade()`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MOVEMENT_TYPES_ALLOWED = (
    "movement_type IN ('INITIAL_IMPORT', 'ADJUSTMENT', 'IN', 'OUT', "
    "'PREPARATION_OUT', 'PREPARATION_IN', 'PRODUCTION_OUT', 'PROTOTYPE_OUT')"
)

MOVEMENT_TYPES_BEFORE = (
    "movement_type IN ('INITIAL_IMPORT', 'ADJUSTMENT', 'IN', 'OUT', "
    "'PREPARATION_OUT', 'PREPARATION_IN', 'PRODUCTION_OUT')"
)

SEQUENCE_TYPES_ALLOWED = (
    "sequence_type IN ('QUOTE', 'FIRING', 'PRODUCT_50', 'PRODUCT_70', "
    "'PREPARATION', 'PRODUCTION_ORDER', 'PROTOTYPE')"
)

SEQUENCE_TYPES_BEFORE = (
    "sequence_type IN ('QUOTE', 'FIRING', 'PRODUCT_50', 'PRODUCT_70', "
    "'PREPARATION', 'PRODUCTION_ORDER')"
)

#: Los `status IS NOT NULL` NO sobran aunque la columna sea NOT NULL. En SQL
#: `NULL = 'CREATED'` no es FALSE sino NULL, y un CHECK que evalua a NULL se da
#: por CUMPLIDO: un OR de ramas con igualdad deja pasar cualquier fila cuyo
#: discriminante sea nulo. Es el agujero que 0017 y 0019 taparon dos veces.
STATUS_TIMESTAMPS_COHERENT = (
    "(status IS NOT NULL AND status = 'CREATED'"
    " AND started_at IS NULL AND completed_at IS NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'STARTED'"
    " AND started_at IS NOT NULL AND completed_at IS NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'COMPLETED'"
    " AND started_at IS NOT NULL AND completed_at IS NOT NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'CANCELLED'"
    " AND started_at IS NULL AND completed_at IS NULL AND cancelled_at IS NOT NULL)"
)

APPROVAL_COHERENT = (
    "(approval IS NOT NULL AND approval = 'PENDING' AND decided_at IS NULL)"
    " OR (approval IS NOT NULL AND approval IN ('APPROVED', 'REJECTED')"
    " AND decided_at IS NOT NULL"
    " AND status IS NOT NULL AND status = 'COMPLETED')"
)

STARTED_REQUIRES_ORIGIN = (
    "status IS NULL"
    " OR status NOT IN ('STARTED', 'COMPLETED')"
    " OR (quotation_id IS NOT NULL AND stock_location_id IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        "prototypes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        # Nula: una muestra preliminar se registra antes de colgar de ningun
        # pedido. Es tambien la unica via de cobro, asi que sin ella no hay
        # arranque fisico posible.
        sa.Column("quotation_id", sa.Integer(), nullable=True),
        # Nulo: se prototipa justamente para decidir si la pieza merece existir
        # en el catalogo.
        sa.Column("product_id", sa.Integer(), nullable=True),
        # Nula hasta arrancar. No hay almacen por defecto ni aunque hoy solo
        # exista uno: el dia que haya dos, el default descontaria del
        # equivocado sin avisar.
        sa.Column("stock_location_id", sa.Integer(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'CREATED'")
        ),
        sa.Column(
            "approval", sa.String(length=16), nullable=False, server_default=sa.text("'PENDING'")
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("target_days", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("supersedes_prototype_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_name", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["quotation_id"], ["quotations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stock_location_id"], ["stock_locations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_prototype_id"], ["prototypes.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("code", name="uq_prototypes_code"),
        # Un prototipo tiene como mucho UN sucesor. Sin esto, dos iteraciones
        # colgando de la misma muestra rechazada dejarian la cadena bifurcada y
        # «cual es la vigente» pasaria a ser una opinion.
        sa.UniqueConstraint("supersedes_prototype_id", name="uq_prototypes_single_successor"),
        sa.CheckConstraint(
            "status IN ('CREATED', 'STARTED', 'COMPLETED', 'CANCELLED')", name="status_allowed"
        ),
        sa.CheckConstraint(
            "approval IN ('PENDING', 'APPROVED', 'REJECTED')", name="approval_allowed"
        ),
        sa.CheckConstraint(STATUS_TIMESTAMPS_COHERENT, name="status_timestamps_coherent"),
        sa.CheckConstraint(APPROVAL_COHERENT, name="approval_coherent"),
        sa.CheckConstraint(STARTED_REQUIRES_ORIGIN, name="started_requires_origin"),
        sa.CheckConstraint("length(btrim(code)) > 0", name="code_not_blank"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        sa.CheckConstraint("quantity > 0", name="quantity_positive"),
        sa.CheckConstraint("target_days IS NULL OR target_days > 0", name="target_days_positive"),
        sa.CheckConstraint(
            "supersedes_prototype_id IS NULL OR supersedes_prototype_id <> id",
            name="no_self_supersede",
        ),
    )
    op.create_index("ix_prototypes_quotation_id", "prototypes", ["quotation_id"])
    op.create_index("ix_prototypes_product_id", "prototypes", ["product_id"])
    op.create_index("ix_prototypes_stock_location_id", "prototypes", ["stock_location_id"])
    op.create_index("ix_prototypes_status", "prototypes", ["status"])
    op.create_index("ix_prototypes_approval", "prototypes", ["approval"])
    op.create_index("ix_prototypes_quotation_status", "prototypes", ["quotation_id", "status"])

    op.create_table(
        "prototype_material_lines",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("prototype_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("quantity", sa.Numeric(precision=18, scale=12), nullable=False),
        sa.Column("uom_code", sa.String(length=32), nullable=False),
        sa.Column("product_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("product_internal_reference_snapshot", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["prototype_id"], ["prototypes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        # El mismo material una sola vez: dos lineas del mismo insumo se
        # descontarian las dos, y la cantidad real acabaria siendo la suma de
        # dos numeros que nadie escribio juntos.
        sa.UniqueConstraint("prototype_id", "product_id", name="uq_prototype_material_product"),
        sa.UniqueConstraint("prototype_id", "sort_order", name="uq_prototype_material_sort_order"),
        sa.CheckConstraint("quantity > 0", name="quantity_positive"),
        sa.CheckConstraint("length(btrim(uom_code)) > 0", name="uom_not_blank"),
    )
    op.create_index(
        "ix_prototype_material_lines_prototype", "prototype_material_lines", ["prototype_id"]
    )
    op.create_index(
        "ix_prototype_material_lines_product", "prototype_material_lines", ["product_id"]
    )

    # De que muestra salio cada consumo. Un PROTOTYPE_OUT sin prototipo seria
    # un gasto sin responsable.
    op.add_column("stock_movements", sa.Column("prototype_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_stock_movements_prototype",
        "stock_movements",
        "prototypes",
        ["prototype_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_stock_movements_prototype", "stock_movements", ["prototype_id"])

    # La restriccion solo se AMPLIA: acepta lo que aceptaba mas PROTOTYPE_OUT.
    op.drop_constraint("movement_type_allowed", "stock_movements", type_="check")
    op.create_check_constraint("movement_type_allowed", "stock_movements", MOVEMENT_TYPES_ALLOWED)

    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint("type_allowed", "document_sequences", SEQUENCE_TYPES_ALLOWED)

    # Siembra idempotente del contador: PRT-2026-000001. Misma convencion
    # documental que CTZ, HR, PREP y OP.
    op.execute(
        sa.text(
            """
            INSERT INTO document_sequences
                (sequence_type, prefix, pattern, padding, reset_policy,
                 current_value, period_key, active)
            SELECT 'PROTOTYPE', 'PRT', :pattern, 6, 'YEARLY', 0, '', true
            WHERE NOT EXISTS (
                SELECT 1 FROM document_sequences WHERE sequence_type = 'PROTOTYPE'
            )
            """
        ).bindparams(pattern="{PREFIX}-{YYYY}-{NUMBER}")
    )


def downgrade() -> None:
    """Baja solo si no hay nada que perder, y lo dice cuando lo hay.

    Bajar de 0021 destruye las muestras, sus materiales y su evaluacion, y
    dejaria movimientos de inventario con un tipo que la restriccion restaurada
    ya no acepta. Ninguna de esas dos cosas se puede arreglar despues: un
    movimiento es historia y una decision de aprobacion no se reconstruye.

    Asi que se comprueba antes y se falla en voz alta. Borrar para poder bajar
    seria cambiar un problema visible por uno silencioso.
    """
    conexion = op.get_bind()

    movimientos = conexion.execute(
        sa.text("SELECT count(*) FROM stock_movements WHERE movement_type = 'PROTOTYPE_OUT'")
    ).scalar_one()
    if movimientos:
        raise RuntimeError(
            f"0021 no puede revertirse: hay {movimientos} movimiento(s) PROTOTYPE_OUT. "
            "Son historia de inventario y no se borran para poder bajar de version."
        )

    muestras = conexion.execute(sa.text("SELECT count(*) FROM prototypes")).scalar_one()
    if muestras:
        raise RuntimeError(
            f"0021 no puede revertirse: hay {muestras} prototipo(s) registrado(s). "
            "Bajar los destruiria junto con su evaluacion."
        )

    op.execute(sa.text("DELETE FROM document_sequences WHERE sequence_type = 'PROTOTYPE'"))
    op.drop_constraint("type_allowed", "document_sequences", type_="check")
    op.create_check_constraint("type_allowed", "document_sequences", SEQUENCE_TYPES_BEFORE)

    op.drop_constraint("movement_type_allowed", "stock_movements", type_="check")
    op.create_check_constraint("movement_type_allowed", "stock_movements", MOVEMENT_TYPES_BEFORE)

    op.drop_index("ix_stock_movements_prototype", table_name="stock_movements")
    op.drop_constraint("fk_stock_movements_prototype", "stock_movements", type_="foreignkey")
    op.drop_column("stock_movements", "prototype_id")

    op.drop_index("ix_prototype_material_lines_product", table_name="prototype_material_lines")
    op.drop_index("ix_prototype_material_lines_prototype", table_name="prototype_material_lines")
    op.drop_table("prototype_material_lines")

    for indice in (
        "ix_prototypes_quotation_status",
        "ix_prototypes_approval",
        "ix_prototypes_status",
        "ix_prototypes_stock_location_id",
        "ix_prototypes_product_id",
        "ix_prototypes_quotation_id",
    ):
        op.drop_index(indice, table_name="prototypes")
    op.drop_table("prototypes")
