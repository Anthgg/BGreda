"""Fase 010C — materiales, pastas y esmaltes del Cotizador V2.

Dos tablas nuevas y nada mas. Ni una columna de `products`, ni una de
`stock_balances`, ni una de `stock_movements`: el maestro de materiales y el
inventario del proyecto siguen siendo los mismos y V2 los usa tal cual.

1. `v2_material_costs` — como se valoriza un material PARA V2. Guarda los
   hechos de la adquisicion —cuanto, por cuanto, cuanto costo traerlo— y
   deriva el costo por unidad en una columna GENERADA. Al ser generada no
   puede quedar desfasada: no existe separada de sus operandos.

2. `v2_quotation_products` — las lineas de una cotizacion V2, con el material
   ya congelado: que pasta, a que costo por gramo, cuanto pesa, si lleva
   esmalte, cual y con que conversion.

## Por que NO se toca `products.cost`

Porque el costeo del Cotizador historico lo lee (`app/services/body_material.py`)
como costo unitario del cuerpo de la pieza. Recalcularlo a partir de compra mas
transporte cambiaria el precio de cotizaciones Legacy sin que nadie lo hubiera
pedido, y sin error visible. Es el mismo motivo por el que 010B puso las
tarifas de horno en tabla propia.

## Por que la columna es GENERADA

`effective_cost_per_unit` sale de `COALESCE(override, (compra + transporte) /
cantidad)`. Guardarlo como un numero corriente obligaria a recordar
recalcularlo en cada camino de escritura, y el dia que alguien edite la compra
por otra via el costo quedaria mintiendo. Generada, la identidad la garantiza
PostgreSQL.

Nada se rellena hacia atras: las cotizaciones V2 que ya existen no tenian
lineas, y no se les inventa ninguna.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

#: La expresion de la columna generada. Se escribe una vez y se usa en el
#: upgrade: tenerla en dos sitios seria tener dos definiciones del costo.
COSTO_EFECTIVO = (
    "COALESCE(costing_override_per_unit,"
    " (purchase_cost + transport_cost) / NULLIF(purchase_quantity, 0))"
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Valorizacion del material
    # ------------------------------------------------------------------
    op.create_table(
        "v2_material_costs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("material_kind", sa.String(length=16), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("purchase_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("purchase_cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("transport_cost", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("costing_override_per_unit", sa.Numeric(24, 12), nullable=True),
        sa.Column("ml_per_gram", sa.Numeric(24, 12), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "effective_cost_per_unit",
            sa.Numeric(24, 12),
            sa.Computed(COSTO_EFECTIVO, persisted=True),
            nullable=True,
        ),
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
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("product_id", name="uq_v2_material_costs_product_id"),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("purchase_quantity > 0", name="purchase_quantity_positive"),
        sa.CheckConstraint("purchase_cost >= 0", name="purchase_cost_non_negative"),
        sa.CheckConstraint("transport_cost >= 0", name="transport_cost_non_negative"),
        sa.CheckConstraint(
            "costing_override_per_unit IS NULL OR costing_override_per_unit >= 0",
            name="costing_override_non_negative",
        ),
        sa.CheckConstraint("ml_per_gram IS NULL OR ml_per_gram > 0", name="ml_per_gram_positive"),
        sa.CheckConstraint("material_kind IN ('BODY', 'GLAZE')", name="material_kind_allowed"),
        sa.CheckConstraint(
            "origin IN ('PURCHASE', 'MANUAL', 'DONATION', 'OTHER')", name="origin_allowed"
        ),
    )
    # Elegir «el esmalte mas caro por gramo» es un ORDER BY sobre este indice, y
    # se ejecuta en cada linea con esmalte que no eligio uno concreto.
    op.create_index(
        "ix_v2_material_costs_kind_cost",
        "v2_material_costs",
        ["material_kind", "effective_cost_per_unit"],
    )

    # ------------------------------------------------------------------
    # 2. Lineas de la cotizacion, con el material congelado
    # ------------------------------------------------------------------
    op.create_table(
        "v2_quotation_products",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("v2_quotation_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("product_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # ---- Pasta
        sa.Column("body_material_id", sa.Integer(), nullable=True),
        sa.Column("body_material_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column("body_unit_weight", sa.Numeric(18, 6), nullable=True),
        sa.Column("body_uom_snapshot", sa.String(length=32), nullable=True),
        sa.Column("body_cost_per_unit_snapshot", sa.Numeric(24, 12), nullable=True),
        sa.Column(
            "body_cost_is_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "body_total_weight", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("body_cost", sa.Numeric(36, 18), nullable=False, server_default=sa.text("0")),
        # ---- Esmalte
        sa.Column("requires_glaze", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("glaze_material_id", sa.Integer(), nullable=True),
        sa.Column("glaze_material_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column(
            "glaze_is_reference", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("glaze_cost_per_unit_snapshot", sa.Numeric(24, 12), nullable=True),
        sa.Column(
            "glaze_cost_is_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("glaze_percent_snapshot", sa.Numeric(9, 6), nullable=True),
        sa.Column("glaze_ml_per_gram_snapshot", sa.Numeric(24, 12), nullable=True),
        sa.Column(
            "glaze_conversion_is_fallback",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "glaze_total_weight", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "glaze_volume_ml", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("glaze_cost", sa.Numeric(36, 18), nullable=False, server_default=sa.text("0")),
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
        sa.ForeignKeyConstraint(["v2_quotation_id"], ["v2_quotations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["body_material_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["glaze_material_id"], ["products.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        sa.CheckConstraint(
            "body_unit_weight IS NULL OR body_unit_weight >= 0",
            name="body_unit_weight_non_negative",
        ),
        sa.CheckConstraint(
            "body_cost_per_unit_snapshot IS NULL OR body_cost_per_unit_snapshot >= 0",
            name="body_cost_non_negative",
        ),
        sa.CheckConstraint(
            "glaze_cost_per_unit_snapshot IS NULL OR glaze_cost_per_unit_snapshot >= 0",
            name="glaze_cost_non_negative",
        ),
        sa.CheckConstraint("body_total_weight >= 0", name="body_total_weight_non_negative"),
        sa.CheckConstraint("glaze_total_weight >= 0", name="glaze_total_weight_non_negative"),
        sa.CheckConstraint("body_cost >= 0", name="body_cost_total_non_negative"),
        sa.CheckConstraint("glaze_cost >= 0", name="glaze_cost_total_non_negative"),
        # Apagado es apagado. Sin este CHECK, «peso cero pero costo distinto de
        # cero» seria cobrar un esmalte que alguien dijo que no queria.
        sa.CheckConstraint(
            "requires_glaze OR (glaze_total_weight = 0 AND glaze_cost = 0 AND glaze_volume_ml = 0)",
            name="glaze_off_costs_nothing",
        ),
        sa.CheckConstraint(
            "glaze_percent_snapshot IS NULL"
            " OR (glaze_percent_snapshot >= 0 AND glaze_percent_snapshot <= 100)",
            name="glaze_percent_range",
        ),
        sa.CheckConstraint(
            "glaze_ml_per_gram_snapshot IS NULL OR glaze_ml_per_gram_snapshot > 0",
            name="glaze_ml_per_gram_positive",
        ),
    )
    op.create_index(
        "ix_v2_quotation_products_v2_quotation_id", "v2_quotation_products", ["v2_quotation_id"]
    )
    op.create_index("ix_v2_quotation_products_product_id", "v2_quotation_products", ["product_id"])
    op.create_index(
        "ix_v2_quotation_products_body_material_id",
        "v2_quotation_products",
        ["body_material_id"],
    )
    op.create_index(
        "ix_v2_quotation_products_glaze_material_id",
        "v2_quotation_products",
        ["glaze_material_id"],
    )
    op.create_index(
        "ix_v2_quotation_products_quotation",
        "v2_quotation_products",
        ["v2_quotation_id", "sort_order"],
    )


def downgrade() -> None:
    """Se niega a revertir si ya hay lineas o materiales valorizados.

    Una linea guarda el material con el que se calculo un precio; una
    valorizacion, la parametrizacion que alguien escribio material por
    material. Ninguna de las dos se recupera volviendo a aplicar la revision.
    """
    conexion = op.get_bind()
    lineas = conexion.scalar(sa.text("SELECT count(*) FROM v2_quotation_products")) or 0
    materiales = conexion.scalar(sa.text("SELECT count(*) FROM v2_material_costs")) or 0
    if lineas or materiales:
        raise RuntimeError(
            f"0030 no puede revertirse: hay {lineas} linea(s) de cotizacion V2 y "
            f"{materiales} material(es) valorizado(s). Revertir dejaria cotizaciones "
            "sin los materiales con los que se calcularon y borraria una "
            "valorizacion escrita a mano."
        )

    for indice in (
        "ix_v2_quotation_products_quotation",
        "ix_v2_quotation_products_glaze_material_id",
        "ix_v2_quotation_products_body_material_id",
        "ix_v2_quotation_products_product_id",
        "ix_v2_quotation_products_v2_quotation_id",
    ):
        op.drop_index(indice, table_name="v2_quotation_products")
    op.drop_table("v2_quotation_products")

    op.drop_index("ix_v2_material_costs_kind_cost", table_name="v2_material_costs")
    op.drop_table("v2_material_costs")
