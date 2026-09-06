"""Contrato de la migracion 0022: puente prototipo -> cotizacion (Fase 009K.1).

Aqui se lee el ARCHIVO. Que la migracion corra de verdad contra PostgreSQL lo
comprueba `tests/db/test_migration_0022_runs.py`; esto fija las decisiones que
se tomaron al escribirla y que un cambio distraido podria deshacer.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).parents[2]
MIGRATION_FILE = REPO_ROOT / "alembic" / "versions" / "0022_prototype_quotation_bridge.py"


def _contenido() -> str:
    return MIGRATION_FILE.read_text(encoding="utf-8")


def test_migracion_0022_y_su_cadena() -> None:
    contenido = _contenido()
    assert 'revision: str = "0022"' in contenido
    assert 'down_revision: str | None = "0021"' in contenido


def test_0021_sigue_siendo_alcanzable() -> None:
    """La cadena no se rompe: 0022 cuelga de 0021 y 0021 sigue existiendo."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0021") is not None
    assert script.get_revision("0022").down_revision == "0021"


def test_el_origen_no_reutiliza_el_campo_que_ya_significa_otra_cosa() -> None:
    """`origin_prototype_id`, nunca `prototype_id` a secas.

    `prototypes.quotation_id` ya dice «con que pedido se autorizo fabricar la
    muestra». El origen es la relacion inversa y posterior: llamarlos igual
    invitaria a confundirlos en la primera consulta que los cruce.
    """
    contenido = _contenido()
    assert '"origin_prototype_id"' in contenido
    assert 'sa.Column("prototype_id", sa.Integer(), nullable=True)' in contenido  # solo en el cargo
    assert '"quotations",\n        sa.Column("prototype_id"' not in contenido


def test_el_unico_es_parcial_y_solo_alcanza_a_los_borradores() -> None:
    """Varias cotizaciones por muestra, un solo borrador vivo.

    Un unico total impediria recotizar la misma muestra el ano que viene; sin
    ningun unico, un doble clic dejaria dos borradores gemelos. El indice
    parcial es lo unico que dice las dos cosas a la vez.
    """
    contenido = _contenido()
    assert "uq_quotations_active_draft_per_prototype" in contenido
    assert "postgresql_where" in contenido
    assert "status = 'DRAFT' AND origin_prototype_id IS NOT NULL" in contenido


def test_el_cargo_comercial_no_vive_en_las_lineas_de_producto() -> None:
    """Tabla propia, y no `quotation_items`.

    `quotation_items` exige `product_id` y es de donde la orden de produccion
    saca lo que hay que fabricar. Un cargo comercial ahi acabaria intentando
    salir del almacen.
    """
    contenido = _contenido()
    assert 'op.create_table(\n        "quotation_commercial_lines"' in contenido
    assert '["quotations.id"], ondelete="CASCADE"' in contenido
    assert '["prototypes.id"], ondelete="RESTRICT"' in contenido


def test_un_cargo_no_puede_ser_gratis_ni_huerfano() -> None:
    """Cero no es un cargo, y un cargo de prototipo sin prototipo no se audita."""
    contenido = _contenido()
    assert 'sa.CheckConstraint("manual_net_amount > 0", name="amount_positive")' in contenido
    assert "kind <> 'PROTOTYPE' OR prototype_id IS NOT NULL" in contenido


def test_el_rol_del_material_admite_el_nulo_historico() -> None:
    """NULL significa «nadie lo declaro», no «otro».

    Las lineas anteriores a 0022 no tienen rol y no se les inventa: deducirlo
    del nombre del producto seria reescribir historia con una heuristica.
    """
    contenido = _contenido()
    assert "material_role IS NULL OR material_role IN ('BODY', 'GLAZE', 'OTHER')" in contenido


def test_no_hay_backfill_ni_renombrado() -> None:
    """BACKFILL: 0, y ni una sola columna cambia de nombre.

    Lo previsto historico ya esta escrito en `quantity` y ahi se queda. Lo que
    NO se hace es copiarlo a `quantity_actual`: que lo previsto coincidiera con
    lo consumido no esta demostrado para las muestras anteriores, y afirmarlo
    lo inventaria. Del rol y de la etapa, ni eso.
    """
    contenido = _contenido()
    assert "BACKFILL: NONE" in contenido
    for prohibido in (
        "UPDATE quotations SET origin",
        "UPDATE prototype_material_lines SET material_role",
        "UPDATE prototype_material_lines SET stage",
        "UPDATE prototype_material_lines SET quantity_actual",
    ):
        assert prohibido not in contenido


def test_la_migracion_es_puramente_aditiva() -> None:
    """OLD_0021_BACKEND_SCHEMA_COMPATIBLE_WITH_0022.

    El despliegue es DB primero: entre que la base llega a 0022 y el backend
    nuevo recibe trafico, la revision anterior sigue leyendo
    `prototype_material_lines.quantity`. Renombrarla o quitarla la romperia en
    esa ventana, y la ventana dura minutos pero la caida seria total.

    Por eso 0022 no renombra, no borra y no estrecha nada: solo anade.
    """
    contenido = _contenido()
    assert "alter_column" not in contenido
    assert "drop_column" in contenido  # solo en downgrade()
    upgrade = contenido.split("def upgrade()")[1].split("def downgrade()")[0]
    assert "drop_column" not in upgrade
    assert "drop_table" not in upgrade
    assert "drop_constraint" not in upgrade
    # Y ningun NOT NULL nuevo sobre una tabla en uso.
    assert "nullable=False" not in upgrade.split("op.create_table(")[0]


def test_el_rol_y_la_etapa_son_ejes_distintos() -> None:
    """MATERIAL_STAGE_EQUALS_ROLE: NO.

    Dos columnas, dos CHECK, dos vocabularios. Fundirlos habria obligado a
    elegir si el campo dice QUE papel juega el material o CUANDO se gasta.
    """
    contenido = _contenido()
    assert "material_role IS NULL OR material_role IN ('BODY', 'GLAZE', 'OTHER')" in contenido
    assert (
        "stage IS NULL OR stage IN ('PREPARATION', 'FIRING', 'LIQUID_TEST', 'ADJUSTMENT')"
        in contenido
    )


def test_lo_previsto_y_lo_real_son_columnas_distintas() -> None:
    """Una sola `quantity` significaba dos cosas, y escondia la que importa."""
    contenido = _contenido()
    assert 'sa.Column("quantity_actual"' in contenido
    assert "quantity_actual IS NULL OR quantity_actual > 0" in contenido


def test_la_vuelta_se_niega_en_voz_alta() -> None:
    """Misma politica que 0021: fallar antes que borrar en silencio."""
    contenido = _contenido()
    assert contenido.count("no puede revertirse") >= 4
    assert "RuntimeError" in contenido
