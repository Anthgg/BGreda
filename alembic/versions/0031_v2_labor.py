"""Fase 010D — mano de obra, tecnicas e ilustracion del Cotizador V2.

Tres tablas nuevas y un bloque de columnas anulables en `v2_quotations`. Ni una
columna de `techniques`, ni de `quotations`, ni de `quotation_techniques`: el
Cotizador historico sigue cobrando exactamente igual.

1. `v2_workers` — quien trabaja y cuanto cuesta su jornada.
2. `v2_techniques` — que se sabe hacer y cuanto rinde una jornada. RENDIMIENTO,
   no precio.
3. `v2_quotation_labor` — quien hace que en una cotizacion, con todo lo que
   hizo falta para calcular el costo ya congelado.

## Por que no se reutiliza `techniques`

Porque guarda otra cosa. La tabla de Legacy lleva `unit_price` y unos factores
de formula: ahi el costo de tornear es un precio por tecnica. En V2 el costo
sale de quien lo hace —jornal entre jornada, por horas—, y la tecnica solo
aporta cuanto rinde una jornada. Anadirle una columna de rendimiento a la tabla
de Legacy mezclaria los dos modelos en una sola fila y obligaria al dominio V2
a importar `app.models.quotations`, que es justo lo que 010A prohibio.

## Por que la tarifa por hora no es una columna

Es `jornal / jornada`, y la jornada puede venir de la configuracion global
cuando el trabajador no declara una propia. Una columna generada no puede leer
otra tabla, asi que se deriva al leer. Lo unico que se guarda es el resultado
DENTRO de la linea de cotizacion, que es donde tiene que dejar de moverse.

## Ilustracion

Va en `v2_quotations` y no en el catalogo de tecnicas. Es una sola por
cotizacion, esta apagada por defecto, y su semantica es comercial y no
productiva: meterla entre las tecnicas la haria heredar reglas que no son
suyas. Sus snapshots nacen anulables, como los de 010B: NULL ahi significa
«cotizacion anterior a la ilustracion», no «vale cero».

Nada se rellena hacia atras.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

#: Columnas que se anaden a `v2_quotations`. Todas ANULABLES: entre que la base
#: llega a 0031 y el backend nuevo recibe trafico, la revision anterior sigue
#: insertando cotizaciones sin saber que existen.
ILLUSTRATION_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("illustration_daily_rate_snapshot", sa.Numeric(18, 6)),
    ("illustration_workday_hours_snapshot", sa.Numeric(18, 6)),
    ("illustration_capacity_snapshot", sa.Numeric(18, 6)),
    ("illustration_hourly_rate_snapshot", sa.Numeric(24, 12)),
    ("illustration_notes", sa.Text()),
    ("effective_work_days", sa.Integer()),
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Trabajadores
    # ------------------------------------------------------------------
    op.create_table(
        "v2_workers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("worker_type", sa.String(length=16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("daily_rate", sa.Numeric(18, 6), nullable=False),
        # NULL = «la jornada del taller». Copiar aqui la global permitiria que
        # se separaran sin que nadie lo pidiera, y entonces habria dos.
        sa.Column("workday_hours", sa.Numeric(18, 6), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        sa.CheckConstraint("daily_rate >= 0", name="daily_rate_non_negative"),
        sa.CheckConstraint(
            "workday_hours IS NULL OR (workday_hours > 0 AND workday_hours <= 24)",
            name="workday_hours_range",
        ),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("worker_type IN ('INTERNAL', 'EXTERNAL')", name="worker_type_allowed"),
    )
    op.create_index("ix_v2_workers_active", "v2_workers", ["active"])

    # ------------------------------------------------------------------
    # 2. Tecnicas: rendimiento, sin precio
    # ------------------------------------------------------------------
    op.create_table(
        "v2_techniques",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("default_capacity_per_workday", sa.Numeric(18, 6), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False, server_default=sa.text("'piezas'")),
        # Marca del catalogo, no una lista de nombres en el codigo: asi la
        # regla de 010C —sin esmalte, la tecnica de vidriado nace apagada— la
        # sigue tambien una tecnica que se anada manana.
        sa.Column("requires_glaze", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("code", name="uq_v2_techniques_code"),
        sa.CheckConstraint("length(btrim(code)) > 0", name="code_not_blank"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        # Cero no es un rendimiento pobre: es una division por cero en la
        # formula de horas. Negativo daria horas negativas.
        sa.CheckConstraint("default_capacity_per_workday > 0", name="capacity_positive"),
        sa.CheckConstraint("version > 0", name="version_positive"),
    )
    op.create_index("ix_v2_techniques_active", "v2_techniques", ["active"])

    # ------------------------------------------------------------------
    # 3. Tareas de una cotizacion, con todo congelado
    # ------------------------------------------------------------------
    op.create_table(
        "v2_quotation_labor",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("v2_quotation_id", sa.Integer(), nullable=False),
        sa.Column("v2_quotation_product_id", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # ---- Quien
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column("worker_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("worker_type_snapshot", sa.String(length=16), nullable=False),
        sa.Column("daily_rate_snapshot", sa.Numeric(18, 6), nullable=False),
        sa.Column("workday_hours_snapshot", sa.Numeric(18, 6), nullable=False),
        sa.Column("hourly_rate_snapshot", sa.Numeric(24, 12), nullable=False),
        # ---- Que
        sa.Column("technique_id", sa.Integer(), nullable=False),
        sa.Column("technique_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("technique_unit_snapshot", sa.String(length=32), nullable=False),
        sa.Column("standard_capacity_snapshot", sa.Numeric(18, 6), nullable=False),
        # ---- Cuanto
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "calculated_hours", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("final_hours", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "hours_overridden", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("rate_overridden", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "is_additional_personnel",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("labor_cost", sa.Numeric(36, 18), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["v2_quotation_id"], ["v2_quotations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["v2_quotation_product_id"], ["v2_quotation_products.id"], ondelete="CASCADE"
        ),
        # RESTRICT: la tarea guarda el nombre, pero borrar a la persona que
        # figura en una cotizacion emitida dejaria el documento sin explicar.
        sa.ForeignKeyConstraint(["worker_id"], ["v2_workers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["technique_id"], ["v2_techniques.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        sa.CheckConstraint("calculated_hours >= 0", name="calculated_hours_non_negative"),
        sa.CheckConstraint("final_hours >= 0", name="final_hours_non_negative"),
        sa.CheckConstraint("labor_cost >= 0", name="labor_cost_non_negative"),
        sa.CheckConstraint("daily_rate_snapshot >= 0", name="daily_rate_non_negative"),
        sa.CheckConstraint("hourly_rate_snapshot >= 0", name="hourly_rate_non_negative"),
        sa.CheckConstraint(
            "workday_hours_snapshot > 0 AND workday_hours_snapshot <= 24",
            name="workday_hours_range",
        ),
        sa.CheckConstraint("standard_capacity_snapshot > 0", name="capacity_positive"),
    )
    op.create_index(
        "ix_v2_quotation_labor_v2_quotation_id", "v2_quotation_labor", ["v2_quotation_id"]
    )
    op.create_index(
        "ix_v2_quotation_labor_v2_quotation_product_id",
        "v2_quotation_labor",
        ["v2_quotation_product_id"],
    )
    op.create_index("ix_v2_quotation_labor_worker_id", "v2_quotation_labor", ["worker_id"])
    op.create_index("ix_v2_quotation_labor_technique_id", "v2_quotation_labor", ["technique_id"])
    op.create_index(
        "ix_v2_quotation_labor_quotation", "v2_quotation_labor", ["v2_quotation_id", "sort_order"]
    )
    # La jornada compartida se mira agrupando por trabajador dentro de la
    # cotizacion, y esa consulta se hace en cada guardado.
    op.create_index(
        "ix_v2_quotation_labor_worker", "v2_quotation_labor", ["v2_quotation_id", "worker_id"]
    )

    # ------------------------------------------------------------------
    # 4. Ilustracion y planificacion, en la cabecera
    # ------------------------------------------------------------------
    for nombre, tipo in ILLUSTRATION_COLUMNS:
        op.add_column("v2_quotations", sa.Column(nombre, tipo, nullable=True))

    # Estas tres NO son snapshots sino estado, y por eso nacen con valor: una
    # cotizacion sin ilustracion tiene cero horas y cero costo, que es verdad,
    # no ausencia de dato.
    op.add_column(
        "v2_quotations",
        sa.Column(
            "illustration_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "v2_quotations",
        sa.Column(
            "illustration_quantity", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "v2_quotations",
        sa.Column(
            "illustration_hours", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "v2_quotations",
        sa.Column(
            "illustration_cost", sa.Numeric(36, 18), nullable=False, server_default=sa.text("0")
        ),
    )

    for nombre, expresion in (
        (
            "illustration_off_costs_nothing",
            "illustration_enabled OR (illustration_hours = 0 AND illustration_cost = 0)",
        ),
        ("illustration_quantity_non_negative", "illustration_quantity >= 0"),
        ("illustration_hours_non_negative", "illustration_hours >= 0"),
        ("illustration_cost_non_negative", "illustration_cost >= 0"),
        (
            "illustration_capacity_positive",
            "illustration_capacity_snapshot IS NULL OR illustration_capacity_snapshot > 0",
        ),
        (
            "illustration_workday_range",
            "illustration_workday_hours_snapshot IS NULL"
            " OR (illustration_workday_hours_snapshot > 0"
            "     AND illustration_workday_hours_snapshot <= 24)",
        ),
        (
            "effective_work_days_non_negative",
            "effective_work_days IS NULL OR effective_work_days >= 0",
        ),
    ):
        op.create_check_constraint(nombre, "v2_quotations", expresion)


def downgrade() -> None:
    """Se niega a revertir si ya hay trabajo registrado.

    Una tarea guarda con quien y a que precio se calculo una cotizacion; un
    trabajador o una tecnica, parametrizacion escrita a mano. Nada de eso
    vuelve reaplicando la revision.
    """
    conexion = op.get_bind()
    tareas = conexion.scalar(sa.text("SELECT count(*) FROM v2_quotation_labor")) or 0
    trabajadores = conexion.scalar(sa.text("SELECT count(*) FROM v2_workers")) or 0
    tecnicas = conexion.scalar(sa.text("SELECT count(*) FROM v2_techniques")) or 0
    ilustradas = (
        conexion.scalar(
            sa.text(
                "SELECT count(*) FROM v2_quotations"
                " WHERE illustration_enabled OR illustration_daily_rate_snapshot IS NOT NULL"
            )
        )
        or 0
    )
    if tareas or trabajadores or tecnicas or ilustradas:
        raise RuntimeError(
            f"0031 no puede revertirse: hay {tareas} tarea(s) de mano de obra, "
            f"{trabajadores} trabajador(es), {tecnicas} tecnica(s) y {ilustradas} "
            "cotizacion(es) con ilustracion. Revertir dejaria cotizaciones sin la "
            "mano de obra con la que se calcularon y borraria configuracion "
            "escrita a mano."
        )

    for nombre in (
        "effective_work_days_non_negative",
        "illustration_workday_range",
        "illustration_capacity_positive",
        "illustration_cost_non_negative",
        "illustration_hours_non_negative",
        "illustration_quantity_non_negative",
        "illustration_off_costs_nothing",
    ):
        op.drop_constraint(nombre, "v2_quotations", type_="check")

    for columna in (
        "illustration_cost",
        "illustration_hours",
        "illustration_quantity",
        "illustration_enabled",
    ):
        op.drop_column("v2_quotations", columna)
    for nombre, _tipo in reversed(ILLUSTRATION_COLUMNS):
        op.drop_column("v2_quotations", nombre)

    for indice in (
        "ix_v2_quotation_labor_worker",
        "ix_v2_quotation_labor_quotation",
        "ix_v2_quotation_labor_technique_id",
        "ix_v2_quotation_labor_worker_id",
        "ix_v2_quotation_labor_v2_quotation_product_id",
        "ix_v2_quotation_labor_v2_quotation_id",
    ):
        op.drop_index(indice, table_name="v2_quotation_labor")
    op.drop_table("v2_quotation_labor")

    op.drop_index("ix_v2_techniques_active", table_name="v2_techniques")
    op.drop_table("v2_techniques")

    op.drop_index("ix_v2_workers_active", table_name="v2_workers")
    op.drop_table("v2_workers")
