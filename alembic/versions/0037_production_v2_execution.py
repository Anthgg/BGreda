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
            BEGIN
                SELECT count(*) INTO pendientes
                FROM production_orders
                WHERE v2_handoff_id IS NOT NULL;

                IF pendientes > 0 THEN
                    RAISE EXCEPTION
                        '0037 downgrade bloqueado: % orden(es) de produccion nacieron de una '
                        'cotizacion V2. Volver a 0036 las dejaria sin origen valido. '
                        'Decide que hacer con ellas antes de bajar.', pendientes;
                END IF;
            END $$;
            """
        )
    )

    op.drop_constraint(_CK_ORIGEN, "production_orders", type_="check")
    op.create_check_constraint(_CK_ORIGEN, "production_orders", _ORIGEN_DOS_RAMAS)
    op.drop_constraint(_UQ_PUENTE, "production_orders", type_="unique")
    op.drop_constraint(_FK_PUENTE, "production_orders", type_="foreignkey")
    op.drop_column("production_orders", "v2_handoff_id")
