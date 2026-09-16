"""Correccion 010H — el producto manda sus procesos, y los adicionales existen.

Dos huecos que la auditoria del Excel dejo al descubierto:

1. **Nadie sabia que procesos necesita una pieza.** Ni el Excel, ni la base, ni
   el backend: producto, tecnica y trabajador eran tres listas sueltas y cada
   tarea se escribia a mano. Ahora la pieza de catalogo declara sus procesos
   (`v2_product_techniques`) y la cotizacion nace con ellos
   (`v2_quotation_processes`), con sus piezas ya puestas.
2. **Los «otros extras» del Excel no estaban en V2.** El libro los suma al
   Costo de Produccion y al Costo Real (hoja «Cotizador V2», B25). Aqui viven
   en un maestro propio (`v2_extras`) y en la cotizacion (`v2_quotation_extras`).

## Proceso y tarea son cosas distintas

El proceso dice QUE hay que hacerle a la pieza y CUANTAS piezas: existe antes de
que se sepa quien lo hara. La tarea —`v2_quotation_labor`, intacta— dice QUIEN
lo hace y cuanto cuesta, con todo congelado. Por eso el trabajador NO se vuelve
opcional en la tarea: una tarea sin trabajador no tendria jornal, ni jornada, ni
tarifa que congelar, y el reparto de costos, la duplicacion y el PDF cuentan con
que esos datos estan. Asignar a alguien a un proceso crea su tarea, y la tarea
apunta al proceso del que salio.

## Por que los quitados se marcan en vez de borrarse

Si un pedido no necesita el acabado, se quita. Borrar la fila haria que la
proxima regeneracion —al cambiar la cantidad, por ejemplo— lo devolviera como si
nadie hubiera decidido nada. `removed_at` recuerda esa decision.

Todo aditivo. No toca Legacy, ni los snapshots, ni ninguna cifra ya emitida.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def _fechas() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def upgrade() -> None:
    # ---- 1. Que procesos necesita una pieza del catalogo -----------------
    op.create_table(
        "v2_product_techniques",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("technique_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_fechas(),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["technique_id"], ["v2_techniques.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("product_id", "technique_id", name="uq_v2_product_techniques_pair"),
    )
    op.create_index("ix_v2_product_techniques_product", "v2_product_techniques", ["product_id"])

    # ---- 2. Los procesos de ESTA cotizacion ------------------------------
    op.create_table(
        "v2_quotation_processes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("v2_quotation_id", sa.Integer(), nullable=False),
        sa.Column("v2_quotation_product_id", sa.Integer(), nullable=False),
        sa.Column("technique_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # De donde salio: del maestro de la pieza o de una decision de esta
        # cotizacion. Lo segundo no se regenera ni se pierde al cambiar el
        # maestro.
        sa.Column("origin", sa.String(16), nullable=False, server_default=sa.text("'PRODUCT'")),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        # Piezas tocadas a mano: cambiar la cantidad del producto ya no las pisa.
        sa.Column(
            "quantity_overridden", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        *_fechas(),
        sa.ForeignKeyConstraint(["v2_quotation_id"], ["v2_quotations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["v2_quotation_product_id"], ["v2_quotation_products.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["technique_id"], ["v2_techniques.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("quantity >= 0", name="ck_v2_quotation_processes_quantity"),
        sa.CheckConstraint(
            "origin IN ('PRODUCT', 'MANUAL')", name="ck_v2_quotation_processes_origin"
        ),
        # Un proceso por tecnica y linea. Dos filas de «Torno» sobre la misma
        # pieza serian dos veces el mismo trabajo cobrado.
        sa.UniqueConstraint(
            "v2_quotation_product_id", "technique_id", name="uq_v2_quotation_processes_pair"
        ),
    )
    op.create_index(
        "ix_v2_quotation_processes_quotation", "v2_quotation_processes", ["v2_quotation_id"]
    )

    # La tarea recuerda de que proceso salio. NULL: las tareas de antes de esta
    # correccion, y las que alguien cree sin pasar por un proceso.
    op.add_column(
        "v2_quotation_labor",
        sa.Column("v2_quotation_process_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_v2_quotation_labor_process",
        "v2_quotation_labor",
        "v2_quotation_processes",
        ["v2_quotation_process_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ---- 3. Adicionales: maestro y cotizacion ----------------------------
    op.create_table(
        "v2_extras",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False, server_default=sa.text("'servicio'")),
        sa.Column("unit_cost", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        *_fechas(),
        sa.CheckConstraint("unit_cost >= 0", name="ck_v2_extras_unit_cost"),
        sa.UniqueConstraint("name", name="uq_v2_extras_name"),
    )

    op.create_table(
        "v2_quotation_extras",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("v2_quotation_id", sa.Integer(), nullable=False),
        # NULL: el adicional aplica al pedido entero, como en el Excel.
        sa.Column("v2_quotation_product_id", sa.Integer(), nullable=True),
        sa.Column("v2_extra_id", sa.Integer(), nullable=False),
        sa.Column("name_snapshot", sa.String(200), nullable=False),
        sa.Column("unit_snapshot", sa.String(32), nullable=False),
        sa.Column("unit_cost_snapshot", sa.Numeric(18, 6), nullable=False),
        sa.Column(
            "unit_cost_is_override", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("total_cost", sa.Numeric(38, 18), nullable=False, server_default=sa.text("0")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        *_fechas(),
        sa.ForeignKeyConstraint(["v2_quotation_id"], ["v2_quotations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["v2_quotation_product_id"], ["v2_quotation_products.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["v2_extra_id"], ["v2_extras.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("quantity >= 0", name="ck_v2_quotation_extras_quantity"),
        sa.CheckConstraint("unit_cost_snapshot >= 0", name="ck_v2_quotation_extras_unit_cost"),
    )
    op.create_index("ix_v2_quotation_extras_quotation", "v2_quotation_extras", ["v2_quotation_id"])

    # Lo que suman los adicionales, para que el resumen no tenga que rehacer la
    # cuenta y para que la cotizacion emitida lo conserve.
    op.add_column(
        "v2_quotations",
        sa.Column(
            "extras_cost_total",
            sa.Numeric(38, 18),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "extras_total_non_negative", "v2_quotations", "extras_cost_total >= 0"
    )


def downgrade() -> None:
    # Volver atras borra decisiones de taller que no estan en ningun otro sitio:
    # que procesos necesita cada pieza y que adicionales lleva cada cotizacion.
    if not context.is_offline_mode():
        conexion = op.get_bind()
        procesos = conexion.execute(
            sa.text("SELECT count(*) FROM v2_quotation_processes")
        ).scalar_one()
        extras = conexion.execute(sa.text("SELECT count(*) FROM v2_quotation_extras")).scalar_one()
        maestro = conexion.execute(
            sa.text("SELECT count(*) FROM v2_product_techniques")
        ).scalar_one()
        if procesos or extras or maestro:
            raise RuntimeError(
                "0036 no puede revertirse: hay procesos de producto o adicionales configurados "
                f"(procesos={procesos}, adicionales={extras}, maestro={maestro}). "
                "Borrelos explicitamente antes de bajar la migracion."
            )

    op.drop_constraint("extras_total_non_negative", "v2_quotations", type_="check")
    op.drop_column("v2_quotations", "extras_cost_total")
    op.drop_index("ix_v2_quotation_extras_quotation", table_name="v2_quotation_extras")
    op.drop_table("v2_quotation_extras")
    op.drop_table("v2_extras")
    op.drop_constraint("fk_v2_quotation_labor_process", "v2_quotation_labor", type_="foreignkey")
    op.drop_column("v2_quotation_labor", "v2_quotation_process_id")
    op.drop_index("ix_v2_quotation_processes_quotation", table_name="v2_quotation_processes")
    op.drop_table("v2_quotation_processes")
    op.drop_index("ix_v2_product_techniques_product", table_name="v2_product_techniques")
    op.drop_table("v2_product_techniques")
