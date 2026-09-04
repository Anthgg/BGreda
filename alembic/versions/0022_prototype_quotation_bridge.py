"""Puente entre la muestra aprobada y la cotizacion final (Fase 009K.1).

009K dejo el prototipo funcionando como dominio propio, pero incomunicado: la
muestra se aprobaba y despues alguien volvia a teclear a mano lo que ya estaba
decidido. Esta migracion trae las cuatro piezas que faltaban para cruzar ese
puente sin que ninguno de los dos dominios invada al otro.

**1. `quotations.origin_prototype_id`** — de que muestra nacio esta cotizacion.

Es una relacion DISTINTA de `prototypes.quotation_id`, y por eso no reutiliza
ese campo ni se llama igual. Aquella dice «con que pedido se autorizo fabricar
la muestra»; esta dice «esta cotizacion nacio de esta muestra aprobada». Las dos
pueden existir a la vez y apuntar a cotizaciones distintas.

El indice unico es PARCIAL, y ahi esta toda la regla de negocio: un prototipo
puede originar VARIAS cotizaciones a lo largo de su vida —se recotiza, el
cliente cambia de idea, pasa un ano— pero solo UNA puede estar viva como
borrador al mismo tiempo. Sin esa restriccion, un doble clic o un reintento de
red dejarian dos borradores gemelos y nadie sabria cual es el bueno. Con ella,
la segunda peticion recupera el borrador que ya existe.

**2. `prototypes.technical_specifications`** — la ficha del taller, estructurada.

En 009K esos campos se guardaron componiendo texto en `notes` porque no habia
esquema autorizado. Funcionaba para leerlo una persona. No sirve para
transferirlo: construir una cotizacion partiendo un texto con expresiones
regulares ata el backend a un formato que decide el navegador, y basta que
alguien edite la nota para que el puente empiece a inventar medidas.

`notes` vuelve a ser lo que su nombre dice —observaciones humanas— y el dato
estructurado vive aparte.

**3. `prototype_material_lines`: rol, etapa y las DOS cantidades.**

`material_role` (BODY / GLAZE / OTHER) responde que papel juega el material en
la pieza. Sin el no habia forma honesta de saber cual es el cuerpo, y
adivinarlo por el nombre del producto funciona hasta el dia que alguien
registra una arcilla que se usa de engobe.

`stage` (PREPARATION / FIRING / LIQUID_TEST / ADJUSTMENT) responde otra cosa
distinta: en que momento del trabajo se gasta. Son ejes INDEPENDIENTES, y el
cuaderno del taller los lleva en columnas separadas porque lo son. Un barniz
puede ser GLAZE en etapa FIRING, y una mezcla de prueba puede ser BODY en
LIQUID_TEST.

Y aparece `quantity_actual`, porque `quantity` significaba dos cosas a la vez.
El cuaderno ya las distinguia («Cantidad prevista» / «Cantidad real») y unirlas
escondia justamente la diferencia que importa: la cotizacion final se deriva de
lo REAL, no de lo previsto.

**La columna fisica `quantity` NO se renombra**, y eso es deliberado. El
despliegue de este proyecto es DB primero y backend despues (Fase 007), asi que
entre que la base llega a 0022 y el backend nuevo recibe trafico hay una
ventana en la que la revision anterior sigue leyendo `quantity`. Renombrarla la
romperia en ese hueco. La columna se queda donde esta y pasa a significar «lo
previsto»; en Python el atributo se llama `quantity_planned` y apunta a ella.

Ese mismo hecho ahorra un backfill: lo previsto historico ya esta escrito.

`quantity_actual` la escribe el arranque, dentro de la misma transaccion que
crea el `PROTOTYPE_OUT`. Dos sitios diciendo cuanto se gasto acabarian
discrepando, asi que el movimiento manda y la columna lo copia.

**4. `quotation_commercial_lines`** — el cobro de la muestra, aparte del producto.

El prototipo se cobra «como una linea mas», pero NO puede ser un
`quotation_items`: esa tabla exige `product_id` y es de la que la orden de
produccion deriva lo que hay que fabricar y descontar. Un cargo comercial que
entrara ahi acabaria, antes o despues, intentando salir del almacen.

Tampoco puede ir por adicionales u otros costos: esos son entradas de COSTO
tecnico, y el motor los multiplica por el factor de produccion y el margen. Un
cargo de 200 no se cobraria a 200.

BACKFILL: NONE. Lo previsto historico se queda intacto en su columna de
siempre, y `quantity_actual` en NULL: que lo previsto coincidiera con lo real
no esta demostrado para las muestras anteriores, y afirmarlo seria inventarlo.
Las cotizaciones existentes no nacieron de ninguna muestra, los prototipos
anteriores no tienen ficha estructurada y sus materiales no tienen rol. NULL
dice exactamente eso. Deducir el rol de las
lineas historicas por el nombre del producto —«LAB70005 parece barniz»— seria
reescribir historia con una heuristica.

DOWNGRADE: se niega cuando hay algo que perder, igual que en 0021.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Roles de un material dentro de una muestra. `NULL` sigue siendo valido: es
#: lo que tienen las lineas anteriores a esta migracion, y significa «nadie lo
#: declaro», no «otro».
MATERIAL_ROLE_ALLOWED = "material_role IS NULL OR material_role IN ('BODY', 'GLAZE', 'OTHER')"

#: Etapa del trabajo, tal como la nombra el cuaderno del taller. Eje distinto
#: del rol: responde CUANDO se gasta, no QUE papel juega.
MATERIAL_STAGE_ALLOWED = (
    "stage IS NULL OR stage IN ('PREPARATION', 'FIRING', 'LIQUID_TEST', 'ADJUSTMENT')"
)

#: Tipos de linea comercial. Empieza con uno solo a proposito: inventar veinte
#: conceptos que nadie ha pedido es como se llenan los enums de valores muertos.
COMMERCIAL_LINE_KINDS = "kind IN ('PROTOTYPE')"


def upgrade() -> None:
    # ---------------------------------------------------------------- 1
    op.add_column(
        "quotations",
        sa.Column("origin_prototype_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_quotations_origin_prototype_id",
        "quotations",
        "prototypes",
        ["origin_prototype_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_quotations_origin_prototype_id",
        "quotations",
        ["origin_prototype_id"],
    )
    # Un solo borrador VIVO por muestra. Las confirmadas y las anuladas quedan
    # fuera del indice, asi que no estorban a una recotizacion futura.
    op.create_index(
        "uq_quotations_active_draft_per_prototype",
        "quotations",
        ["origin_prototype_id"],
        unique=True,
        postgresql_where=sa.text("status = 'DRAFT' AND origin_prototype_id IS NOT NULL"),
    )

    # ---------------------------------------------------------------- 2
    op.add_column(
        "prototypes",
        sa.Column(
            "technical_specifications",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # ---------------------------------------------------------------- 3
    op.add_column(
        "prototype_material_lines",
        sa.Column("material_role", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "material_role_allowed",
        "prototype_material_lines",
        MATERIAL_ROLE_ALLOWED,
    )
    op.add_column(
        "prototype_material_lines",
        sa.Column("stage", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "stage_allowed",
        "prototype_material_lines",
        MATERIAL_STAGE_ALLOWED,
    )
    # `quantity` se queda como esta: es lo que lee el backend todavia en
    # produccion mientras la base ya esta en 0022. Solo se le anade al lado la
    # cantidad realmente consumida.
    op.add_column(
        "prototype_material_lines",
        sa.Column("quantity_actual", sa.Numeric(18, 6), nullable=True),
    )
    op.create_check_constraint(
        "quantity_actual_positive",
        "prototype_material_lines",
        "quantity_actual IS NULL OR quantity_actual > 0",
    )

    # ---------------------------------------------------------------- 4
    op.create_table(
        "quotation_commercial_lines",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("quotation_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        # Congelada al escribirla: «Prototipo PRT-2026-000007» tiene que seguir
        # diciendo lo mismo dentro de un ano, aunque la muestra se renombre.
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("prototype_id", sa.Integer(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # El importe NETO que teclea una persona autorizada. No hay formula: el
        # negocio dijo que la muestra se cobra, no cuanto cuesta.
        sa.Column("manual_net_amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
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
        sa.ForeignKeyConstraint(["quotation_id"], ["quotations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["prototype_id"], ["prototypes.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(COMMERCIAL_LINE_KINDS, name="kind_allowed"),
        sa.CheckConstraint("quantity > 0", name="quantity_positive"),
        # Cero no es un cargo: es una linea que no cobra nada y que nadie
        # revisaria. Si algun dia hay muestras gratuitas, sera una decision
        # explicita del negocio y traera su propia forma de decirlo.
        sa.CheckConstraint("manual_net_amount > 0", name="amount_positive"),
        # Un cargo de prototipo sin prototipo no se puede describir ni auditar.
        sa.CheckConstraint(
            "kind <> 'PROTOTYPE' OR prototype_id IS NOT NULL",
            name="prototype_kind_requires_prototype",
        ),
    )
    op.create_index(
        "ix_quotation_commercial_lines_quotation",
        "quotation_commercial_lines",
        ["quotation_id"],
    )
    op.create_index(
        "ix_quotation_commercial_lines_prototype",
        "quotation_commercial_lines",
        ["prototype_id"],
    )


def downgrade() -> None:
    """Baja solo si no hay nada que perder, y lo dice cuando lo hay.

    Misma politica que 0021. Un cargo comercial cobrado y el origen de una
    cotizacion son hechos: borrarlos para poder bajar de version cambiaria un
    problema visible por uno silencioso.
    """
    conexion = op.get_bind()

    cargos = conexion.execute(
        sa.text("SELECT count(*) FROM quotation_commercial_lines")
    ).scalar_one()
    if cargos:
        raise RuntimeError(
            f"0022 no puede revertirse: hay {cargos} linea(s) comercial(es) registrada(s). "
            "Son importes cotizados y no se borran para poder bajar de version."
        )

    origenes = conexion.execute(
        sa.text("SELECT count(*) FROM quotations WHERE origin_prototype_id IS NOT NULL")
    ).scalar_one()
    if origenes:
        raise RuntimeError(
            f"0022 no puede revertirse: hay {origenes} cotizacion(es) originada(s) en una "
            "muestra. Bajar borraria de que prototipo nacieron."
        )

    fichas = conexion.execute(
        sa.text("SELECT count(*) FROM prototypes WHERE technical_specifications IS NOT NULL")
    ).scalar_one()
    if fichas:
        raise RuntimeError(
            f"0022 no puede revertirse: hay {fichas} prototipo(s) con ficha tecnica "
            "estructurada. Bajar la destruiria."
        )

    consumos = conexion.execute(
        sa.text("SELECT count(*) FROM prototype_material_lines WHERE quantity_actual IS NOT NULL")
    ).scalar_one()
    if consumos:
        raise RuntimeError(
            f"0022 no puede revertirse: hay {consumos} linea(s) con consumo real registrado. "
            "Es lo que de verdad salio del almacen y no se borra para bajar de version."
        )

    roles = conexion.execute(
        sa.text(
            "SELECT count(*) FROM prototype_material_lines"
            " WHERE material_role IS NOT NULL OR stage IS NOT NULL"
        )
    ).scalar_one()
    if roles:
        raise RuntimeError(
            f"0022 no puede revertirse: hay {roles} linea(s) de material con rol o etapa "
            "declarados. Bajar borraria cual era el cuerpo de la pieza y cuando se gasto."
        )

    op.drop_index(
        "ix_quotation_commercial_lines_prototype", table_name="quotation_commercial_lines"
    )
    op.drop_index(
        "ix_quotation_commercial_lines_quotation", table_name="quotation_commercial_lines"
    )
    op.drop_table("quotation_commercial_lines")

    op.drop_constraint("quantity_actual_positive", "prototype_material_lines", type_="check")
    op.drop_column("prototype_material_lines", "quantity_actual")
    op.drop_constraint("stage_allowed", "prototype_material_lines", type_="check")
    op.drop_column("prototype_material_lines", "stage")
    op.drop_constraint("material_role_allowed", "prototype_material_lines", type_="check")
    op.drop_column("prototype_material_lines", "material_role")

    op.drop_column("prototypes", "technical_specifications")

    op.drop_index("uq_quotations_active_draft_per_prototype", table_name="quotations")
    op.drop_index("ix_quotations_origin_prototype_id", table_name="quotations")
    op.drop_constraint("fk_quotations_origin_prototype_id", "quotations", type_="foreignkey")
    op.drop_column("quotations", "origin_prototype_id")
