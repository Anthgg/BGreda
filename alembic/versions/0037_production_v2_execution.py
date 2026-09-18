"""Fase 010I — la orden de produccion admite un tercer origen: la cotizacion V2.

010H construyo el PUENTE (`v2_production_handoffs`): «esta cotizacion V2
aceptada esta lista para fabricarse». No creo ninguna orden a proposito, porque
la orden existente colgaba solo de una cotizacion Legacy o de una muestra, y su
arranque descuenta la receta entera. Esta fase la conecta.

No se crea un sistema paralelo de ordenes V2. La cabecera `production_orders`
—correlativo, estados, fechas coherentes, QR de seguimiento, idempotencia,
auditoria— ya es el documento de ejecucion fisica del taller, y tener dos
acabaria con el taller aprendiendose dos pantallas para el mismo hecho, que es
justo lo que 009K.4 unifico.

Lo aditivo:

    production_orders.v2_handoff_id   FK a v2_production_handoffs, anulable, UNICA
    production_consumptions           el material REAL gastado en una orden V2

El consumo es un registro explicito por cada salida de material, y no la receta
entera descontada al arrancar como en una orden Legacy: en el taller lo cotizado
y lo gastado no coinciden —otra pasta, otro esmalte, una pieza que se rompe—, y
el inventario tiene que reflejar lo que de verdad salio. Cada consumo es
exactamente un movimiento `PRODUCTION_OUT` (UNIQUE sobre el movimiento) y lleva
una clave de idempotencia obligatoria y UNICA: el doble clic y el reintento de
red no pueden descontar dos veces.

Cuelga del PUENTE y no de la cotizacion: la clave foranea impide que exista
una orden V2 que no haya pasado por «Enviar a produccion», asi que no nace un
segundo camino hacia la fabrica. UNICA porque es, junto con el UNIQUE de
`v2_quotation_id` en el puente, lo que garantiza en la base que una cotizacion
V2 tenga como mucho una orden. Comprobarlo en el servicio no basta: dos
peticiones a la vez pasan las dos la comprobacion antes de que ninguna inserte.

Y el CHECK `exactly_one_origin` pasa de dos ramas a tres. Cada rama nombra los
TRES campos: una rama que solo mirara dos dejaria pasar una fila con el tercero
relleno.

**Ni una fila se toca.** Las ordenes que existen tienen una cotizacion Legacy o
una muestra, cumplen la primera o la segunda rama tal como estan y ninguna
recibe `v2_handoff_id`.

Lo que esta migracion NO hace, y por que:

- No devuelve `quotation_id` ni `quotation_item_id` a NOT NULL al bajar. Esa
  relajacion es de 0027 y la deshace el downgrade de 0027; si 0037 la
  deshiciera, bajar a 0036 romperia las ordenes de muestra que 0036 si admite.
- No copia lineas de la cotizacion V2 a `production_order_lines`. Aquella tabla
  exige `product_id` y apunta a lineas Legacy, y una cotizacion V2 confirmada ya
  es inmutable y lleva su huella congelada en el puente: el plan cotizado se lee
  de ella, no de una copia que podria contradecirla.

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: El CHECK va DESNUDO y la clave ajena y la unicidad COMPLETAS, igual que en
#: 0027 y por la misma razon: la convencion del proyecto (`app/db/base.py`) es
#: `ck_%(table_name)s_%(constraint_name)s` e incorpora el nombre que se le
#: pasa —pasarlo completo daria `ck_x_ck_x_...`, la trampa de 0024—, mientras
#: que FK y UQ se componen solo de tabla y columna y el nombre explicito se usa
#: tal cual.
_CK_ORIGEN = "exactly_one_origin"
_FK_PUENTE = "fk_production_orders_v2_handoff_id_v2_production_handoffs"
_UQ_PUENTE = "uq_production_orders_v2_handoff_id"

#: Las dos condiciones se escriben aqui y no se importan del modelo: una
#: migracion tiene que seguir describiendo lo que hizo aquel dia aunque el
#: modelo cambie despues.
_ORIGEN_DOS_RAMAS = (
    "(quotation_id IS NOT NULL AND prototype_id IS NULL)"
    " OR (quotation_id IS NULL AND prototype_id IS NOT NULL)"
)
_ORIGEN_TRES_RAMAS = (
    "(quotation_id IS NOT NULL AND prototype_id IS NULL AND v2_handoff_id IS NULL)"
    " OR (quotation_id IS NULL AND prototype_id IS NOT NULL AND v2_handoff_id IS NULL)"
    " OR (quotation_id IS NULL AND prototype_id IS NULL AND v2_handoff_id IS NOT NULL)"
)


def upgrade() -> None:
    # El orden importa: primero existe la columna del tercer origen, y solo
    # entonces se sustituye el CHECK. Soltar el CHECK antes dejaria un instante
    # sin regla de origen.
    op.add_column("production_orders", sa.Column("v2_handoff_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        _FK_PUENTE,
        "production_orders",
        "v2_production_handoffs",
        ["v2_handoff_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(_UQ_PUENTE, "production_orders", ["v2_handoff_id"])

    op.drop_constraint(_CK_ORIGEN, "production_orders", type_="check")
    op.create_check_constraint(_CK_ORIGEN, "production_orders", _ORIGEN_TRES_RAMAS)

    # Bloque B. El material REAL que el taller gasta en una orden V2.
    #
    # Dentro de `create_table` los CHECK van DESNUDOS: la convencion les pone
    # delante `ck_<tabla>_`. Es la trampa en la que cayo 0036, cuyos CHECK se
    # llaman `ck_v2_quotation_processes_ck_v2_quotation_processes_...` en la
    # base real. Las FK se nombran solas con la convencion; los UNIQUE
    # explicitos se usan tal cual.
    op.create_table(
        "production_consumptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_order_id", sa.Integer(), nullable=False),
        sa.Column("v2_quotation_product_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("stock_location_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=24, scale=12), nullable=False),
        sa.Column("uom_code", sa.String(length=32), nullable=False),
        sa.Column("unit_cost_snapshot", sa.Numeric(precision=24, scale=12), nullable=True),
        sa.Column("stock_movement_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_by_name", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["production_order_id"], ["production_orders.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["v2_quotation_product_id"], ["v2_quotation_products.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stock_location_id"], ["stock_locations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stock_movement_id"], ["stock_movements.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("quantity > 0", name="quantity_positive"),
        sa.CheckConstraint("kind IN ('BODY', 'GLAZE', 'OTHER')", name="kind_allowed"),
        sa.CheckConstraint("length(btrim(uom_code)) > 0", name="uom_not_blank"),
        sa.CheckConstraint(
            "unit_cost_snapshot IS NULL OR unit_cost_snapshot >= 0",
            name="unit_cost_non_negative",
        ),
        sa.CheckConstraint(
            "length(btrim(idempotency_key)) >= 8", name="idempotency_key_long_enough"
        ),
        # Un movimiento, un consumo; y una clave, un consumo.
        sa.UniqueConstraint(
            "stock_movement_id", name="uq_production_consumptions_stock_movement_id"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_production_consumptions_idempotency_key"),
    )
    op.create_index(
        "ix_production_consumptions_production_order_id",
        "production_consumptions",
        ["production_order_id"],
    )
    op.create_index(
        "ix_production_consumptions_v2_quotation_product_id",
        "production_consumptions",
        ["v2_quotation_product_id"],
    )
    op.create_index(
        "ix_production_consumptions_product_id", "production_consumptions", ["product_id"]
    )


def downgrade() -> None:
    # Bajar solo es posible si no hay ordenes V2. Si las hay, el CHECK de dos
    # ramas fallaria de todos modos contra ellas —no tienen ni cotizacion
    # Legacy ni muestra— y con un mensaje que no explicaria nada. Se aborta
    # antes, diciendo cuantas son y por que.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                pendientes integer;
                consumos integer;
            BEGIN
                SELECT count(*) INTO pendientes
                FROM production_orders
                WHERE v2_handoff_id IS NOT NULL;

                SELECT count(*) INTO consumos
                FROM production_consumptions;

                IF pendientes > 0 OR consumos > 0 THEN
                    RAISE EXCEPTION
                        '0037 downgrade bloqueado: % orden(es) de produccion nacieron de una '
                        'cotizacion V2 y hay % consumo(s) real(es) registrado(s). Volver a '
                        '0036 dejaria ordenes sin origen valido y borraria material gastado '
                        'que el inventario si refleja. Decide que hacer antes de bajar.',
                        pendientes, consumos;
                END IF;
            END $$;
            """
        )
    )

    # La guardia ya exigio que no haya ni un consumo. Se suelta primero porque
    # apunta a la orden.
    op.drop_table("production_consumptions")

    op.drop_constraint(_CK_ORIGEN, "production_orders", type_="check")
    op.create_check_constraint(_CK_ORIGEN, "production_orders", _ORIGEN_DOS_RAMAS)
    op.drop_constraint(_UQ_PUENTE, "production_orders", type_="unique")
    op.drop_constraint(_FK_PUENTE, "production_orders", type_="foreignkey")
    op.drop_column("production_orders", "v2_handoff_id")
