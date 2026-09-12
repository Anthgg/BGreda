"""Fase 010E — motor de quema del Cotizador V2.

Ninguna tabla nueva. Las tarifas de horno de V2 —gas real, tarifa externa y
tarifa de alumno— ya existen desde 0029 en `v2_kiln_rates`, y la geometria de
los hornos vive en `kilns` desde mucho antes. Lo que falta es donde escribir el
RESULTADO: que horno se eligio, cuantas hornadas salieron, con que tarifas se
calcularon y cuanto le toca a cada producto.

Dos bloques de columnas, los dos anulables o con valor por defecto:

1. `v2_quotations` — el horno de la cotizacion, su capacidad congelada, la
   ocupacion, el conteo de hornadas, los cuatro importes por hornada con sus
   marcas de override y los dos totales.
2. `v2_quotation_products` — las medidas de la pieza, su volumen, cuanto horno
   ocupa y lo que absorbe del costo de quema.

## Lo que esta migracion NO hace

**No toca `kiln_occupancy_factors`.** Esa tabla es del motor historico y sigue
intacta: el Cotizador Legacy sigue multiplicando por su factor exactamente
igual que ayer. Lo que hace 010E es no usarlo en V2, no borrarlo de Legacy.

**No toca `firings`, `firing_lines` ni `firing_kiln_sessions`.** Las hojas de
quema reales son produccion y no cotizacion.

**No rellena nada hacia atras.** Las cotizaciones anteriores quedan con horno
en NULL y totales en cero, que es la verdad: se crearon antes de que existiera
la quema.

## Por que `firing_difference` es una columna generada

Es `firing_commercial_total - firing_gas_total`, y los dos sumandos viven en
esta misma fila. La base puede calcularla, y entonces no hay forma de que
contradiga a sus partes. Es el contraste deliberado con la tarifa por hora de
010D, que NO pudo ser generada porque dependia de otra tabla.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


#: Columnas anulables de `v2_quotations`. Anulables porque entre que la base
#: llega a 0032 y el backend nuevo recibe trafico, la revision anterior sigue
#: insertando cotizaciones sin saber que existen.
QUOTATION_NULLABLE_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("kiln_name_snapshot", sa.String(length=120)),
    ("kiln_capacity_snapshot", sa.Numeric(18, 6)),
    ("gas_cost_low_snapshot", sa.Numeric(18, 6)),
    ("gas_cost_high_snapshot", sa.Numeric(18, 6)),
    ("commercial_rate_low_snapshot", sa.Numeric(18, 6)),
    ("commercial_rate_high_snapshot", sa.Numeric(18, 6)),
)

#: Columnas con valor por defecto. Cero y falso son la verdad para una
#: cotizacion que nunca eligio horno: ni ocupa, ni cuesta, ni nadie pacto nada.
QUOTATION_DEFAULTED_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object], str], ...] = (
    ("firing_total_volume_cm3", sa.Numeric(18, 6), "0"),
    ("firing_occupancy_percent", sa.Numeric(18, 6), "0"),
    ("firing_count", sa.Integer(), "0"),
    ("low_fire_count", sa.Integer(), "0"),
    ("high_fire_count", sa.Integer(), "0"),
    ("gas_low_is_override", sa.Boolean(), "false"),
    ("gas_high_is_override", sa.Boolean(), "false"),
    ("commercial_low_is_override", sa.Boolean(), "false"),
    ("commercial_high_is_override", sa.Boolean(), "false"),
    ("firing_gas_total", sa.Numeric(36, 18), "0"),
    ("firing_commercial_total", sa.Numeric(36, 18), "0"),
)

#: Medidas de la pieza. NULL y no cero: un cero seria una pieza plana, y lo que
#: pasa de verdad es que todavia no se ha medido.
LINE_NULLABLE_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("length_cm", sa.Numeric(18, 6)),
    ("width_cm", sa.Numeric(18, 6)),
    ("height_cm", sa.Numeric(18, 6)),
)

LINE_DEFAULTED_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object], str], ...] = (
    ("unit_volume_cm3", sa.Numeric(18, 6), "0"),
    ("total_volume_cm3", sa.Numeric(18, 6), "0"),
    ("firing_occupancy_percent", sa.Numeric(18, 6), "0"),
    ("firing_volume_share_percent", sa.Numeric(18, 6), "0"),
    ("firing_commercial_cost", sa.Numeric(36, 18), "0"),
    ("firing_gas_cost", sa.Numeric(36, 18), "0"),
)

QUOTATION_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "kiln_capacity_snapshot_positive",
        "kiln_capacity_snapshot IS NULL OR kiln_capacity_snapshot > 0",
    ),
    ("firing_volume_non_negative", "firing_total_volume_cm3 >= 0"),
    ("firing_occupancy_non_negative", "firing_occupancy_percent >= 0"),
    ("firing_count_non_negative", "firing_count >= 0"),
    (
        "low_fire_count_within_firing_count",
        "low_fire_count >= 0 AND low_fire_count <= firing_count",
    ),
    (
        "high_fire_count_within_firing_count",
        "high_fire_count >= 0 AND high_fire_count <= firing_count",
    ),
    ("low_fire_off_costs_nothing", "coalesce(low_fire_enabled, false) OR low_fire_count = 0"),
    ("high_fire_off_costs_nothing", "coalesce(high_fire_enabled, false) OR high_fire_count = 0"),
    (
        "firing_requires_kiln",
        "kiln_id IS NOT NULL"
        " OR (firing_count = 0 AND firing_occupancy_percent = 0"
        "     AND firing_gas_total = 0 AND firing_commercial_total = 0)",
    ),
    (
        "no_firing_costs_nothing",
        "low_fire_count > 0 OR high_fire_count > 0"
        " OR (firing_gas_total = 0 AND firing_commercial_total = 0)",
    ),
    ("gas_low_non_negative", "gas_cost_low_snapshot IS NULL OR gas_cost_low_snapshot >= 0"),
    ("gas_high_non_negative", "gas_cost_high_snapshot IS NULL OR gas_cost_high_snapshot >= 0"),
    (
        "commercial_low_non_negative",
        "commercial_rate_low_snapshot IS NULL OR commercial_rate_low_snapshot >= 0",
    ),
    (
        "commercial_high_non_negative",
        "commercial_rate_high_snapshot IS NULL OR commercial_rate_high_snapshot >= 0",
    ),
    ("firing_gas_total_non_negative", "firing_gas_total >= 0"),
    ("firing_commercial_total_non_negative", "firing_commercial_total >= 0"),
)

LINE_CHECKS: tuple[tuple[str, str], ...] = (
    ("length_positive", "length_cm IS NULL OR length_cm > 0"),
    ("width_positive", "width_cm IS NULL OR width_cm > 0"),
    ("height_positive", "height_cm IS NULL OR height_cm > 0"),
    ("unit_volume_non_negative", "unit_volume_cm3 >= 0"),
    ("total_volume_non_negative", "total_volume_cm3 >= 0"),
    ("line_firing_occupancy_non_negative", "firing_occupancy_percent >= 0"),
    (
        "line_volume_share_range",
        "firing_volume_share_percent >= 0 AND firing_volume_share_percent <= 100",
    ),
    ("line_firing_cost_non_negative", "firing_commercial_cost >= 0"),
    ("line_firing_gas_non_negative", "firing_gas_cost >= 0"),
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. La cabecera: horno, hornadas, tarifas y totales
    # ------------------------------------------------------------------
    # RESTRICT y no CASCADE: retirar un horno del taller no puede llevarse por
    # delante el documento que explica un precio ya dado.
    op.add_column("v2_quotations", sa.Column("kiln_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_v2_quotations_kiln_id_kilns",
        "v2_quotations",
        "kilns",
        ["kiln_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # Una clave foranea RESTRICT sin indice obliga a PostgreSQL a recorrer
    # `v2_quotations` entera cada vez que alguien toca un horno, y a bloquearla
    # mientras lo hace.
    op.create_index("ix_v2_quotations_kiln_id", "v2_quotations", ["kiln_id"])

    for nombre, tipo in QUOTATION_NULLABLE_COLUMNS:
        op.add_column("v2_quotations", sa.Column(nombre, tipo, nullable=True))
    for nombre, tipo, defecto in QUOTATION_DEFAULTED_COLUMNS:
        op.add_column(
            "v2_quotations",
            sa.Column(nombre, tipo, nullable=False, server_default=sa.text(defecto)),
        )

    # Generada y persistida: la base la calcula y nadie puede escribir en ella
    # un numero que contradiga a sus dos sumandos.
    op.add_column(
        "v2_quotations",
        sa.Column(
            "firing_difference",
            sa.Numeric(36, 18),
            sa.Computed("firing_commercial_total - firing_gas_total", persisted=True),
            nullable=False,
        ),
    )

    for nombre, expresion in QUOTATION_CHECKS:
        op.create_check_constraint(nombre, "v2_quotations", expresion)

    # ------------------------------------------------------------------
    # 2. La linea: medidas, volumen y su parte de la quema
    # ------------------------------------------------------------------
    for nombre, tipo in LINE_NULLABLE_COLUMNS:
        op.add_column("v2_quotation_products", sa.Column(nombre, tipo, nullable=True))
    for nombre, tipo, defecto in LINE_DEFAULTED_COLUMNS:
        op.add_column(
            "v2_quotation_products",
            sa.Column(nombre, tipo, nullable=False, server_default=sa.text(defecto)),
        )
    for nombre, expresion in LINE_CHECKS:
        op.create_check_constraint(nombre, "v2_quotation_products", expresion)


def downgrade() -> None:
    """Se niega a revertir si alguna cotizacion ya tiene quema.

    Un horno elegido, una tarifa pactada dentro de la cotizacion o unas medidas
    tecleadas a mano no vuelven reaplicando la revision: no se derivan de nada
    que quede en la base. Mismo criterio que 0030 y 0031.
    """
    conexion = op.get_bind()
    con_horno = (
        conexion.scalar(
            sa.text(
                "SELECT count(*) FROM v2_quotations"
                " WHERE kiln_id IS NOT NULL"
                "    OR kiln_capacity_snapshot IS NOT NULL"
                "    OR gas_cost_low_snapshot IS NOT NULL"
                "    OR gas_cost_high_snapshot IS NOT NULL"
                "    OR commercial_rate_low_snapshot IS NOT NULL"
                "    OR commercial_rate_high_snapshot IS NOT NULL"
                "    OR firing_count <> 0"
            )
        )
        or 0
    )
    # Las medidas se cuentan aparte: una cotizacion puede tener las piezas
    # medidas y todavia no haber elegido horno, y esas medidas tampoco vuelven.
    con_medidas = (
        conexion.scalar(
            sa.text(
                "SELECT count(*) FROM v2_quotation_products"
                " WHERE length_cm IS NOT NULL"
                "    OR width_cm IS NOT NULL"
                "    OR height_cm IS NOT NULL"
                "    OR total_volume_cm3 <> 0"
            )
        )
        or 0
    )
    if con_horno or con_medidas:
        raise RuntimeError(
            f"0032 no puede revertirse: hay {con_horno} cotizacion(es) con quema y "
            f"{con_medidas} linea(s) con medidas. Revertir dejaria cotizaciones sin "
            "el horno, las tarifas y las medidas con las que se calcularon."
        )

    for nombre, _expresion in LINE_CHECKS:
        op.drop_constraint(nombre, "v2_quotation_products", type_="check")
    for nombre, _tipo, _defecto in reversed(LINE_DEFAULTED_COLUMNS):
        op.drop_column("v2_quotation_products", nombre)
    for nombre, _tipo in reversed(LINE_NULLABLE_COLUMNS):
        op.drop_column("v2_quotation_products", nombre)

    for nombre, _expresion in QUOTATION_CHECKS:
        op.drop_constraint(nombre, "v2_quotations", type_="check")
    op.drop_column("v2_quotations", "firing_difference")
    for nombre, _tipo, _defecto in reversed(QUOTATION_DEFAULTED_COLUMNS):
        op.drop_column("v2_quotations", nombre)
    for nombre, _tipo in reversed(QUOTATION_NULLABLE_COLUMNS):
        op.drop_column("v2_quotations", nombre)
    op.drop_constraint("fk_v2_quotations_kiln_id_kilns", "v2_quotations", type_="foreignkey")
    op.drop_index("ix_v2_quotations_kiln_id", table_name="v2_quotations")
    op.drop_column("v2_quotations", "kiln_id")
