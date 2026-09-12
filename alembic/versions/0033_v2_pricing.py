"""Fase 010F — motor economico final del Cotizador V2.

Ninguna tabla nueva y ninguna columna de configuracion. Todo lo que hacia falta
para calcular —el IGV, la moneda, el tipo de cambio, el escalon de redondeo, el
factor y sus limites, el costo del espacio por dia y el de administracion— ya
estaba congelado en `v2_quotations` desde 0029. Lo que falta es donde escribir
el RESULTADO.

Dos bloques, los dos con valor por defecto cero:

1. `v2_quotations` — los totales por componente, las dos bases de costo, las
   tres salidas comerciales, el subtotal reconstruido, el IGV, el total, el
   ajuste por redondeo y la ganancia estimada.
2. `v2_quotation_products` — el costo directo de cada linea, lo que absorbe de
   los costos generales, su precio, su unitario redondeado y su parte de la
   ganancia.

## Por que se guarda y no se deriva al leer

Porque una cotizacion emitida tiene que poder explicarse sin consultar un solo
maestro. Derivarlo al vuelo funcionaria mientras nadie tocara nada; el dia que
suba un jornal, el precio que el cliente acepto cambiaria de valor al abrirlo.
El precio unitario redondeado, ademas, no es un calculo: es un compromiso
comercial ya comunicado.

## Lo que esta migracion NO hace

**No crea un IGV de V2.** Se lee de `commercial_settings`, que es la unica
fuente canonica del proyecto, y lo que se guarda aqui es el porcentaje que se
uso, no una segunda politica fiscal.

**No toca el factor por ocupacion de Legacy, ni `quotations`, ni el
inventario.** El Cotizador historico sigue cobrando exactamente igual.

**No rellena nada hacia atras.** Las cotizaciones anteriores quedan en cero,
que es la verdad: se crearon antes de que existiera el motor economico.

## Por que ninguna columna es GENERADA

En 010E la diferencia de quema si pudo serlo: era una resta entre dos columnas
de la misma fila. Aqui casi todo depende de las LINEAS —el subtotal se
reconstruye sumandolas— y una columna generada no puede leer otra tabla. El
unico candidato, `estimated_profit`, depende ademas del tipo de cambio, asi que
se calcula en Python y se prueba.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


#: Los totales de la cabecera. `calculation_numeric` —36,18— y no
#: `money_numeric`: aqui todavia no se ha redondeado nada y perder decimales
#: antes del punto comercial arrastra el error a todas las lineas.
QUOTATION_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object], str], ...] = (
    ("materials_cost_total", sa.Numeric(36, 18), "0"),
    ("labor_cost_total", sa.Numeric(36, 18), "0"),
    ("space_cost", sa.Numeric(36, 18), "0"),
    ("direct_cost_total", sa.Numeric(36, 18), "0"),
    ("real_cost_total", sa.Numeric(36, 18), "0"),
    ("production_cost_total", sa.Numeric(36, 18), "0"),
    ("price_min", sa.Numeric(36, 18), "0"),
    ("price_target", sa.Numeric(36, 18), "0"),
    ("negotiated_price", sa.Numeric(36, 18), "0"),
    ("subtotal_amount", sa.Numeric(36, 18), "0"),
    ("tax_amount", sa.Numeric(36, 18), "0"),
    ("total_amount", sa.Numeric(36, 18), "0"),
    ("rounding_adjustment", sa.Numeric(36, 18), "0"),
    ("estimated_profit", sa.Numeric(36, 18), "0"),
    ("effective_margin_percent", sa.Numeric(18, 6), "0"),
)

LINE_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object], str], ...] = (
    ("direct_cost", sa.Numeric(36, 18), "0"),
    ("allocated_space_cost", sa.Numeric(36, 18), "0"),
    ("allocated_general_cost", sa.Numeric(36, 18), "0"),
    ("allocated_production_cost", sa.Numeric(36, 18), "0"),
    ("allocated_real_cost", sa.Numeric(36, 18), "0"),
    ("line_price", sa.Numeric(36, 18), "0"),
    ("unit_price_raw", sa.Numeric(36, 18), "0"),
    # El unitario redondeado si va en escala de dinero: es el numero que se le
    # dice al cliente, y guardarlo con dieciocho decimales sugeriria una
    # precision que el precio comercial no tiene.
    ("unit_price", sa.Numeric(18, 6), "0"),
    ("line_subtotal", sa.Numeric(36, 18), "0"),
    ("line_tax", sa.Numeric(36, 18), "0"),
    ("line_total", sa.Numeric(36, 18), "0"),
    ("allocated_profit", sa.Numeric(36, 18), "0"),
)

#: Los costos y los precios no pueden ser negativos. La GANANCIA, el ajuste por
#: redondeo y el margen si: una cotizacion puede venderse a perdida, y
#: esconderlo tras un cero seria mentir sobre el unico numero que importa.
QUOTATION_CHECKS: tuple[tuple[str, str], ...] = (
    ("materials_total_non_negative", "materials_cost_total >= 0"),
    ("labor_total_non_negative", "labor_cost_total >= 0"),
    ("space_cost_non_negative", "space_cost >= 0"),
    ("direct_total_non_negative", "direct_cost_total >= 0"),
    ("real_cost_non_negative", "real_cost_total >= 0"),
    ("production_cost_non_negative", "production_cost_total >= 0"),
    ("price_min_non_negative", "price_min >= 0"),
    ("price_target_non_negative", "price_target >= 0"),
    ("negotiated_price_non_negative", "negotiated_price >= 0"),
    ("subtotal_non_negative", "subtotal_amount >= 0"),
    ("tax_amount_non_negative", "tax_amount >= 0"),
    ("total_amount_non_negative", "total_amount >= 0"),
    ("price_min_below_target", "price_min <= price_target"),
)

LINE_CHECKS: tuple[tuple[str, str], ...] = (
    ("line_direct_cost_non_negative", "direct_cost >= 0"),
    ("line_space_non_negative", "allocated_space_cost >= 0"),
    ("line_general_non_negative", "allocated_general_cost >= 0"),
    ("line_production_cost_non_negative", "allocated_production_cost >= 0"),
    ("line_real_cost_non_negative", "allocated_real_cost >= 0"),
    ("line_price_non_negative", "line_price >= 0"),
    ("line_unit_raw_non_negative", "unit_price_raw >= 0"),
    ("line_unit_price_non_negative", "unit_price >= 0"),
    ("line_subtotal_non_negative", "line_subtotal >= 0"),
    ("line_tax_non_negative", "line_tax >= 0"),
    ("line_total_non_negative", "line_total >= 0"),
    (
        "no_quantity_no_amount",
        "quantity > 0 OR (line_subtotal = 0 AND line_tax = 0 AND line_total = 0)",
    ),
)


def upgrade() -> None:
    for nombre, tipo, defecto in QUOTATION_COLUMNS:
        op.add_column(
            "v2_quotations",
            sa.Column(nombre, tipo, nullable=False, server_default=sa.text(defecto)),
        )
    for nombre, expresion in QUOTATION_CHECKS:
        op.create_check_constraint(nombre, "v2_quotations", expresion)

    for nombre, tipo, defecto in LINE_COLUMNS:
        op.add_column(
            "v2_quotation_products",
            sa.Column(nombre, tipo, nullable=False, server_default=sa.text(defecto)),
        )
    for nombre, expresion in LINE_CHECKS:
        op.create_check_constraint(nombre, "v2_quotation_products", expresion)


def downgrade() -> None:
    """Se niega a revertir si alguna cotizacion ya tiene precio.

    Un precio unitario redondeado es un compromiso comercial: puede estar ya
    enviado. No se recalcula reaplicando la revision —depende del factor, del
    IGV y del tipo de cambio que la cotizacion congelo— y borrarlo dejaria
    documentos sin la cifra que el cliente acepto. Mismo criterio que 0030,
    0031 y 0032.
    """
    conexion = op.get_bind()
    con_precio = (
        conexion.scalar(
            sa.text(
                "SELECT count(*) FROM v2_quotations"
                " WHERE production_cost_total <> 0"
                "    OR subtotal_amount <> 0"
                "    OR total_amount <> 0"
            )
        )
        or 0
    )
    lineas_con_precio = (
        conexion.scalar(sa.text("SELECT count(*) FROM v2_quotation_products WHERE unit_price <> 0"))
        or 0
    )
    if con_precio or lineas_con_precio:
        raise RuntimeError(
            f"0033 no puede revertirse: hay {con_precio} cotizacion(es) valorizada(s) y "
            f"{lineas_con_precio} linea(s) con precio unitario. Revertir borraria el "
            "precio con el que se comprometio una oferta."
        )

    for nombre, _expresion in LINE_CHECKS:
        op.drop_constraint(nombre, "v2_quotation_products", type_="check")
    for nombre, _tipo, _defecto in reversed(LINE_COLUMNS):
        op.drop_column("v2_quotation_products", nombre)

    for nombre, _expresion in QUOTATION_CHECKS:
        op.drop_constraint(nombre, "v2_quotations", type_="check")
    for nombre, _tipo, _defecto in reversed(QUOTATION_COLUMNS):
        op.drop_column("v2_quotations", nombre)
