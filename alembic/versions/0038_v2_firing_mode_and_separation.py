"""Fase 010J — la quema y el costo del Cotizador V2 segun el Excel final.

El Excel corregido del dueno cambia cuatro reglas del Cotizador V2, y cada una
necesita un dato que hoy no existe:

    v2_commercial_settings.piece_separation_cm      separacion entre piezas, default 3 cm
    v2_quotations.firing_mode                        COMPARTIDA (default) o EXCLUSIVA/URGENTE
    v2_quotations.piece_separation_cm_snapshot       la separacion con la que se midio
    v2_quotations.firing_billed_load                 la carga que se FACTURA, en hornadas
    v2_quotations.commercial_factor_target_snapshot  el factor objetivo (x3) congelado
    v2_quotation_products.illustration_quantity      ilustracion POR PRODUCTO
    v2_quotation_products.illustration_hours
    v2_quotation_products.illustration_cost

**Las filas existentes no cambian de numero.** El motor de 010E cobraba cada
hornada entera y media las piezas sin separacion: eso es exactamente una quema
EXCLUSIVA con separacion 0, y asi se rellenan. La carga facturada de esas filas
es su numero de hornadas, y su factor objetivo el maximo que congelaron (lo que
010F usaba como precio objetivo). Las cotizaciones NUEVAS nacen compartidas y con
la separacion de la configuracion; eso lo decide el servicio al crearlas.

Todo es aditivo. Los CHECK van DESNUDOS por la convencion del proyecto
(`ck_%(table_name)s_%(constraint_name)s`), igual que en 0027 y 0037.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Las mismas precisiones que `app.core.precision`, escritas a mano: una
#: migracion describe lo que hizo aquel dia aunque el modelo cambie despues.
_CANTIDAD = sa.Numeric(18, 6)
_DINERO = sa.Numeric(18, 6)
_CARGA = sa.Numeric(24, 12)

_CK_SEPARACION_AJUSTES = "piece_separation_range"
_CK_MODO = "firing_mode_allowed"
_CK_SEPARACION = "piece_separation_snapshot_range"
_CK_CARGA = "firing_billed_load_non_negative"
_CK_OBJETIVO = "commercial_factor_target_snapshot_floor"
_CK_ILUS_CANTIDAD = "line_illustration_quantity_non_negative"
_CK_ILUS_HORAS = "line_illustration_hours_non_negative"
_CK_ILUS_COSTO = "line_illustration_cost_non_negative"


def upgrade() -> None:
    op.add_column(
        "v2_commercial_settings",
        sa.Column("piece_separation_cm", _CANTIDAD, nullable=False, server_default=sa.text("3")),
    )
    op.create_check_constraint(
        _CK_SEPARACION_AJUSTES,
        "v2_commercial_settings",
        "piece_separation_cm >= 0 AND piece_separation_cm <= 20",
    )

    # Primero se crean con el valor que conserva las filas viejas; despues el
    # default pasa a ser el de las nuevas.
    op.add_column(
        "v2_quotations",
        sa.Column(
            "firing_mode", sa.String(16), nullable=False, server_default=sa.text("'EXCLUSIVE'")
        ),
    )
    op.alter_column("v2_quotations", "firing_mode", server_default=sa.text("'SHARED'"))
    op.create_check_constraint(_CK_MODO, "v2_quotations", "firing_mode IN ('SHARED', 'EXCLUSIVE')")

    op.add_column(
        "v2_quotations",
        sa.Column(
            "piece_separation_cm_snapshot",
            _CANTIDAD,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.alter_column("v2_quotations", "piece_separation_cm_snapshot", server_default=sa.text("3"))
    op.create_check_constraint(
        _CK_SEPARACION,
        "v2_quotations",
        "piece_separation_cm_snapshot >= 0 AND piece_separation_cm_snapshot <= 20",
    )

    op.add_column(
        "v2_quotations",
        sa.Column("firing_billed_load", _CARGA, nullable=False, server_default=sa.text("0")),
    )
    op.execute(sa.text("UPDATE v2_quotations SET firing_billed_load = firing_count"))
    op.create_check_constraint(_CK_CARGA, "v2_quotations", "firing_billed_load >= 0")

    op.add_column(
        "v2_quotations",
        sa.Column("commercial_factor_target_snapshot", _CANTIDAD, nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE v2_quotations"
            " SET commercial_factor_target_snapshot = commercial_factor_max_snapshot"
        )
    )
    op.create_check_constraint(
        _CK_OBJETIVO,
        "v2_quotations",
        "commercial_factor_target_snapshot IS NULL OR commercial_factor_target_snapshot >= 2",
    )

    for columna, tipo in (
        ("illustration_quantity", _CANTIDAD),
        ("illustration_hours", _CANTIDAD),
        ("illustration_cost", _DINERO),
    ):
        op.add_column(
            "v2_quotation_products",
            sa.Column(columna, tipo, nullable=False, server_default=sa.text("0")),
        )
    op.create_check_constraint(
        _CK_ILUS_CANTIDAD, "v2_quotation_products", "illustration_quantity >= 0"
    )
    op.create_check_constraint(_CK_ILUS_HORAS, "v2_quotation_products", "illustration_hours >= 0")
    op.create_check_constraint(_CK_ILUS_COSTO, "v2_quotation_products", "illustration_cost >= 0")


def downgrade() -> None:
    # Bajar borraria reglas que ya costearon algo: una quema compartida, una
    # separacion o una ilustracion por producto. Con el esquema de 0037 esas
    # cotizaciones quedarian con importes que ya nadie sabria explicar. Se
    # aborta diciendo cuantas son.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                compartidas integer;
                ilustradas integer;
            BEGIN
                SELECT count(*) INTO compartidas
                FROM v2_quotations
                WHERE firing_mode <> 'EXCLUSIVE' OR piece_separation_cm_snapshot <> 0;

                SELECT count(*) INTO ilustradas
                FROM v2_quotation_products
                WHERE illustration_quantity <> 0 OR illustration_cost <> 0;

                IF compartidas > 0 OR ilustradas > 0 THEN
                    RAISE EXCEPTION USING MESSAGE =
                        format('No se puede bajar de 0038: %s cotizaciones V2 usan', compartidas)
                        || ' quema compartida o separacion y '
                        || format('%s lineas tienen ilustracion por producto', ilustradas);
                END IF;
            END $$;
            """
        )
    )
    for nombre in (_CK_ILUS_COSTO, _CK_ILUS_HORAS, _CK_ILUS_CANTIDAD):
        op.drop_constraint(nombre, "v2_quotation_products", type_="check")
    for columna in ("illustration_cost", "illustration_hours", "illustration_quantity"):
        op.drop_column("v2_quotation_products", columna)

    for nombre in (_CK_OBJETIVO, _CK_CARGA, _CK_SEPARACION, _CK_MODO):
        op.drop_constraint(nombre, "v2_quotations", type_="check")
    for columna in (
        "commercial_factor_target_snapshot",
        "firing_billed_load",
        "piece_separation_cm_snapshot",
        "firing_mode",
    ):
        op.drop_column("v2_quotations", columna)

    op.drop_constraint(_CK_SEPARACION_AJUSTES, "v2_commercial_settings", type_="check")
    op.drop_column("v2_commercial_settings", "piece_separation_cm")
