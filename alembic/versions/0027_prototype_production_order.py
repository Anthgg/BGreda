"""Fase 009K.4 — la orden de produccion admite un segundo origen.

Hasta aqui, fabricar una muestra y fabricar una cotizacion eran dos sistemas
distintos: la orden de produccion servia a las cotizaciones y el prototipo
tenia su propio arranque, su propio consumo y su propia pantalla. Dos caminos
para el mismo hecho fisico acaban discrepando, y el taller tenia que
aprenderse los dos.

Esta migracion hace posible que la orden sea el UNICO documento de ejecucion
fisica. Para eso tiene que poder venir de una muestra, y eso obliga a algo que
el resto de la fase 009K evito con cuidado: **relajar dos NOT NULL**.

    production_orders.quotation_id            NOT NULL -> NULL
    production_order_lines.quotation_item_id  NOT NULL -> NULL

No es un descuido ni una comodidad. Es el precio de tener un solo modelo
operativo, y se paga entero con el CHECK que viene detras: una orden sigue
teniendo exactamente un origen —cotizacion o muestra—, y ahora lo dice la base
en vez de confiarlo al servicio. Lo que se pierde en NOT NULL se recupera en
una regla mas exacta.

Lo aditivo:

    production_orders.prototype_id   FK a prototypes, anulable, UNICA

UNICA a proposito: es lo unico que impide que dos cobros simultaneos de la
misma cotizacion de prototipo creen dos ordenes, cada una dispuesta a gastar
el barro entero. Comprobarlo en el servicio no basta —las dos peticiones pasan
la comprobacion antes de que ninguna inserte—.

`stock_location_id` NO se toca: sigue siendo obligatorio. Una orden que no
sabe de que almacen sale su material no es una orden. De donde viene ese
almacen para una muestra es decision de quien cobra, no de esta migracion.

**Ni una fila se toca.** Las cuatro ordenes que existen conservan su
cotizacion; las once muestras existentes NO reciben orden. Fabricarles una
ahora seria inventar un documento para un hecho que, en el caso de
PRT-2026-000009, ya ocurrio sin el.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: El CHECK va DESNUDO y el resto COMPLETO, y la diferencia no es capricho: la
#: convencion del proyecto (`app/db/base.py`) es
#: `ck_%(table_name)s_%(constraint_name)s` —incorpora el nombre que se le pasa,
#: asi que pasarlo completo produciria `ck_x_ck_x_...`, la trampa que costo un
#: despliegue en 0024— mientras que las de clave ajena y unicidad se componen
#: solo de tabla y columna y NO leen el nombre dado: ahi un nombre explicito se
#: usa tal cual, y hay que escribirlo ya conforme.
_CK_ORIGEN = "exactly_one_origin"
_FK_PROTOTIPO = "fk_production_orders_prototype_id_prototypes"
_UQ_PROTOTIPO = "uq_production_orders_prototype_id"

#: La misma condicion que vive en el modelo (`EXACTLY_ONE_ORIGIN`). Se escribe
#: aqui tambien porque una migracion no debe importar del codigo de la
#: aplicacion: el dia que el modelo cambie, esta revision tiene que seguir
#: describiendo lo que hizo aquel dia.
_ORIGEN_EXCLUSIVO = (
    "(quotation_id IS NOT NULL AND prototype_id IS NULL)"
    " OR (quotation_id IS NULL AND prototype_id IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column("production_orders", sa.Column("prototype_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        _FK_PROTOTIPO,
        "production_orders",
        "prototypes",
        ["prototype_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(_UQ_PROTOTIPO, "production_orders", ["prototype_id"])

    # El orden importa: primero existe la columna del segundo origen, y solo
    # entonces se afloja el primero. Al reves habria un instante en el que una
    # orden podria quedarse sin ningun origen.
    op.alter_column("production_orders", "quotation_id", existing_type=sa.Integer(), nullable=True)
    op.alter_column(
        "production_order_lines", "quotation_item_id", existing_type=sa.Integer(), nullable=True
    )

    # Y el CHECK va al final, cuando ya se puede cumplir. Las cuatro ordenes
    # existentes lo satisfacen sin tocarlas: todas tienen cotizacion y ninguna
    # tiene muestra.
    op.create_check_constraint(_CK_ORIGEN, "production_orders", _ORIGEN_EXCLUSIVO)


def downgrade() -> None:
    # Volver atras solo es posible si no hay ordenes de muestra. Si las hay,
    # devolver `quotation_id` a NOT NULL fallaria de todos modos contra las
    # filas que lo tienen en nulo, y con un mensaje que no explicaria nada. Se
    # aborta antes, diciendo cuantas son y por que.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                pendientes integer;
            BEGIN
                SELECT count(*) INTO pendientes
                FROM production_orders
                WHERE prototype_id IS NOT NULL;

                IF pendientes > 0 THEN
                    RAISE EXCEPTION
                        '0027 downgrade bloqueado: % orden(es) de produccion nacieron de una '
                        'muestra y no tienen cotizacion. Volver a 0026 las dejaria sin origen. '
                        'Decide que hacer con ellas antes de bajar.', pendientes;
                END IF;
            END $$;
            """
        )
    )

    op.drop_constraint(_CK_ORIGEN, "production_orders", type_="check")
    op.alter_column(
        "production_order_lines", "quotation_item_id", existing_type=sa.Integer(), nullable=False
    )
    op.alter_column("production_orders", "quotation_id", existing_type=sa.Integer(), nullable=False)
    op.drop_constraint(_UQ_PROTOTIPO, "production_orders", type_="unique")
    op.drop_constraint(_FK_PROTOTIPO, "production_orders", type_="foreignkey")
    op.drop_column("production_orders", "prototype_id")
