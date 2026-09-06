"""Fase 009K.1.1 — la cotizacion de prototipo tambien es origen para arrancar.

La restriccion `ck_prototypes_started_requires_origin` nacio en 0021, cuando el
unico origen comercial de una muestra era una cotizacion de producto
(`quotation_id`). 0023 anadio `prototypes.prototype_quotation_id`: una muestra
puede nacer de su propia cotizacion de prototipo, que tambien se emite y
tambien se cobra.

El servicio ya lo sabia —`evaluate_readiness` pregunta primero por el CPR y
despues por la CTZ— pero la base no. El resultado en produccion fue el peor
posible: `readiness` respondia `{"ready": true, "issues": []}` y el `START`
moria con `CheckViolationError`, que sale por la API como un 500 sin explicar
nada. La aplicacion decia que si y la tabla decia que no.

Esta migracion alinea las dos. Cambia UNICAMENTE la parte de origen:

    antes:   quotation_id IS NOT NULL AND stock_location_id IS NOT NULL
    despues: (quotation_id IS NOT NULL OR prototype_quotation_id IS NOT NULL)
             AND stock_location_id IS NOT NULL

`stock_location_id` se conserva: sin almacen no se sabe de donde salio el
material, y eso no depende de quien pago. Las otras ramas del CHECK —una
muestra sin arrancar, o cancelada— tampoco se tocan.

NO se toca ni una fila. Los dos origenes tienen semantica distinta y deben
coexistir: copiar `prototype_quotation_id` a `quotation_id` haria que una
muestra de prototipo dijera que colgo de una cotizacion de producto que no
existe.

Revision ID: 0024
Revises: 0023
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: DESNUDO, sin el prefijo `ck_prototypes_`.
#:
#: `alembic/env.py` pasa `target_metadata`, cuya convencion es
#: `ck_%(table_name)s_%(constraint_name)s`. Alembic la aplica al nombre que se
#: le da, asi que pasar el nombre completo produce
#: `ck_prototypes_ck_prototypes_started_requires_origin` y el DROP falla con
#: «constraint does not exist». La 0021 la creo dentro de `create_table` con
#: `name="started_requires_origin"`, expandido una sola vez: ese es el nombre
#: real en la base, y por eso aqui se pasa igual de corto.
CONSTRAINT = "started_requires_origin"

#: El nombre ya expandido, para leerlo en las pruebas y en el log.
CONSTRAINT_COMPLETO = "ck_prototypes_started_requires_origin"

#: Con los dos origenes. Es la expresion que vive en el modelo.
ORIGEN_AMPLIADO = (
    "status IS NULL"
    " OR status NOT IN ('STARTED', 'COMPLETED')"
    " OR ((quotation_id IS NOT NULL OR prototype_quotation_id IS NOT NULL)"
    " AND stock_location_id IS NOT NULL)"
)

#: La de 0021, solo con el origen legacy.
ORIGEN_LEGACY = (
    "status IS NULL"
    " OR status NOT IN ('STARTED', 'COMPLETED')"
    " OR (quotation_id IS NOT NULL AND stock_location_id IS NOT NULL)"
)


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "prototypes", type_="check")
    op.create_check_constraint(CONSTRAINT, "prototypes", ORIGEN_AMPLIADO)


def downgrade() -> None:
    # Volver a la restriccion estrecha con muestras ya arrancadas cuyo unico
    # origen es un CPR dejaria la tabla en un estado que ella misma prohibe.
    # PostgreSQL lo rechazaria al validar, con un mensaje que no dice cual es
    # el problema. Se aborta antes, diciendo exactamente que pasa y cuantas
    # filas lo causan.
    op.execute(
        """
        DO $$
        DECLARE
            afectadas integer;
        BEGIN
            SELECT count(*) INTO afectadas
            FROM prototypes
            WHERE status IN ('STARTED', 'COMPLETED')
              AND prototype_quotation_id IS NOT NULL
              AND quotation_id IS NULL;

            IF afectadas > 0 THEN
                RAISE EXCEPTION
                    '0024 downgrade bloqueado: % muestra(s) arrancadas cuyo unico '
                    'origen es una cotizacion de prototipo. Volver a 0023 las '
                    'dejaria violando ck_prototypes_started_requires_origin.',
                    afectadas;
            END IF;
        END
        $$
        """
    )
    op.drop_constraint(CONSTRAINT, "prototypes", type_="check")
    op.create_check_constraint(CONSTRAINT, "prototypes", ORIGEN_LEGACY)
