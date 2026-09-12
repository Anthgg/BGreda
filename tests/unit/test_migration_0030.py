"""0030 vigilandose a si misma: dos tablas nuevas y ni un dedo sobre lo demas.

El riesgo de esta fase no es perder datos: es **contaminar**. El maestro de
materiales, el inventario y el costeo Legacy ya existen y ya funcionan, y hay
tres formas comodas de romperlos sin que nada falle:

1. recalcular `products.cost` con compra mas transporte «para unificar el
   costo». `app/services/body_material.py` lo lee como costo unitario del
   cuerpo de la pieza: cambiarlo mueve el precio de cotizaciones Legacy que
   nadie pidio recalcular;
2. anadir filas a `stock_movements` al cotizar. Cotizar no consume material —
   el consumo es de produccion, en 010I— y un movimiento aqui descuadraria el
   inventario real contra una cotizacion que quiza no se acepte;
3. guardar `effective_cost_per_unit` como un numero corriente. Ese costo ES
   `COALESCE(override, (compra + transporte) / cantidad)`, y almacenarlo
   aparte crea dos verdades que se separan en cuanto alguien edite la compra
   por otra via.

Esta prueba lee el fichero de la migracion. La que lo ejecuta contra
PostgreSQL de verdad es `tests/db/test_migration_0030_runs.py`.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRACION = REPO_ROOT / "alembic" / "versions"


def _contenido() -> str:
    archivos = list(MIGRACION.glob("0030_*.py"))
    assert len(archivos) == 1, f"Se esperaba una sola 0030, hay {len(archivos)}"
    return archivos[0].read_text(encoding="utf-8")


def _upgrade() -> str:
    return _contenido().split("def upgrade()")[1].split("def downgrade()")[0]


def _downgrade() -> str:
    return _contenido().split("def downgrade()")[1]


# ---------------------------------------------------------------------------
# Aditiva
# ---------------------------------------------------------------------------
def test_el_upgrade_no_destruye_nada() -> None:
    upgrade = _upgrade()
    for prohibido in ("drop_column", "drop_table", "drop_index", "drop_constraint"):
        assert prohibido not in upgrade, prohibido
    assert "rename" not in upgrade.lower()


def test_el_upgrade_crea_exactamente_dos_tablas() -> None:
    """Ni una tabla puente ni una copia del maestro: dos, y las dos son de V2."""
    upgrade = _upgrade()
    assert upgrade.count("op.create_table(") == 2
    assert '"v2_material_costs"' in upgrade
    assert '"v2_quotation_products"' in upgrade


def test_el_upgrade_no_toca_el_maestro_de_productos() -> None:
    """Sobre todo `products.cost`, que es lo que cobra el Cotizador historico."""
    upgrade = _upgrade()
    assert "add_column" not in upgrade
    assert "alter_column" not in upgrade
    # `products` solo puede aparecer como destino de una clave foranea.
    for linea in upgrade.splitlines():
        if "products" in linea and "v2_" not in linea:
            assert "products.id" in linea, linea


def test_el_upgrade_no_toca_el_inventario() -> None:
    """Cotizar no consume material. El consumo llega en produccion, en 010I."""
    upgrade = _upgrade()
    for tabla in ("stock_movements", "stock_balances", "stock_locations"):
        assert tabla not in upgrade, f"0030 menciono {tabla}"


def test_el_upgrade_no_escribe_una_sola_fila() -> None:
    """No hay nada que rellenar: las cotizaciones V2 existentes no tenian lineas."""
    mayusculas = _upgrade().upper()
    for prohibido in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert prohibido not in mayusculas, prohibido


def test_el_upgrade_no_toca_las_tablas_de_las_fases_anteriores() -> None:
    """0028 y 0029 quedaron cerradas. 0030 solo se apoya en ellas."""
    upgrade = _upgrade()
    assert "v2_commercial_settings" not in upgrade
    assert "v2_kiln_rates" not in upgrade
    # `v2_quotations` aparece unicamente como destino de la clave foranea.
    assert upgrade.count('"v2_quotations.id"') == 1


# ---------------------------------------------------------------------------
# El costo no puede desfasarse
# ---------------------------------------------------------------------------
def test_el_costo_efectivo_es_una_columna_generada() -> None:
    """Generada, no calculada al guardar: no hay camino de escritura que olvidarla."""
    upgrade = _upgrade()
    assert "sa.Computed(" in upgrade
    assert "persisted=True" in upgrade
    assert "effective_cost_per_unit" in upgrade


def test_la_expresion_del_costo_se_escribe_una_sola_vez() -> None:
    """Dos copias de la formula son dos definiciones del costo esperando a divergir."""
    contenido = _contenido()
    assert contenido.count("COALESCE(costing_override_per_unit,") == 1
    assert "sa.Computed(COSTO_EFECTIVO" in contenido


def test_el_transporte_entra_en_el_costo_por_unidad() -> None:
    """El error silencioso de la fase: dividir solo la compra da un 23 % de menos."""
    assert "(purchase_cost + transport_cost) / NULLIF(purchase_quantity, 0)" in _contenido()


def test_una_cantidad_de_cero_no_revienta_la_columna_generada() -> None:
    """`NULLIF` deja el costo en NULL en vez de abortar el INSERT entero."""
    assert "NULLIF(purchase_quantity, 0)" in _contenido()


def test_un_material_no_admite_dos_valorizaciones() -> None:
    """Dos filas serian dos costos para el mismo material y ningun criterio."""
    assert "uq_v2_material_costs_product_id" in _upgrade()


def test_el_transporte_nace_en_cero_y_no_en_nulo() -> None:
    """Sin transporte el costo es solo la compra; NULL lo volveria indefinido."""
    tabla = _upgrade().split('"v2_material_costs"')[1].split("op.create_index")[0]
    bloque = tabla.split('"transport_cost"')[1].split("sa.Column(")[0]
    assert "nullable=False" in bloque
    assert "server_default" in bloque


# ---------------------------------------------------------------------------
# Lo que la base no deja escribir mal
# ---------------------------------------------------------------------------
def test_el_esmalte_apagado_no_puede_costar_nada() -> None:
    """Peso cero con costo distinto de cero seria cobrar lo que alguien apago."""
    contenido = _contenido()
    assert "glaze_off_costs_nothing" in contenido
    assert "requires_glaze OR (glaze_total_weight = 0 AND glaze_cost = 0" in contenido
    # El volumen tambien: un esmalte apagado no ocupa mililitros.
    assert "glaze_volume_ml = 0" in contenido


def test_ningun_importe_ni_ningun_peso_admite_negativos() -> None:
    contenido = _contenido()
    for nombre in (
        "purchase_cost_non_negative",
        "transport_cost_non_negative",
        "costing_override_non_negative",
        "body_total_weight_non_negative",
        "glaze_total_weight_non_negative",
        "body_cost_total_non_negative",
        "glaze_cost_total_non_negative",
    ):
        assert nombre in contenido, nombre


def test_las_conversiones_imposibles_no_caben_en_la_base() -> None:
    """Cero o negativa daria un volumen con aspecto real y equivocado."""
    contenido = _contenido()
    assert "ml_per_gram IS NULL OR ml_per_gram > 0" in contenido
    assert "glaze_ml_per_gram_snapshot IS NULL OR glaze_ml_per_gram_snapshot > 0" in contenido


def test_la_cantidad_comprada_tiene_que_ser_positiva() -> None:
    assert "purchase_quantity > 0" in _contenido()


def test_los_tipos_y_las_procedencias_son_listas_cerradas() -> None:
    """Extensible es el maestro de materiales, no el vocabulario del sistema."""
    contenido = _contenido()
    assert "material_kind IN ('BODY', 'GLAZE')" in contenido
    assert "origin IN ('PURCHASE', 'MANUAL', 'DONATION', 'OTHER')" in contenido


def test_el_esmalte_mas_caro_por_gramo_tiene_su_indice() -> None:
    """Se consulta en cada linea con esmalte que no eligio uno concreto."""
    upgrade = _upgrade()
    assert "ix_v2_material_costs_kind_cost" in upgrade
    assert '["material_kind", "effective_cost_per_unit"]' in upgrade


def test_una_linea_congela_el_material_en_lugar_de_apuntarlo() -> None:
    """Sin snapshot, cambiar el maestro reescribiria precios ya entregados."""
    upgrade = _upgrade()
    for columna in (
        "body_material_name_snapshot",
        "body_uom_snapshot",
        "body_cost_per_unit_snapshot",
        "glaze_material_name_snapshot",
        "glaze_cost_per_unit_snapshot",
        "glaze_percent_snapshot",
        "glaze_ml_per_gram_snapshot",
    ):
        assert columna in upgrade, columna


def test_borrar_una_cotizacion_se_lleva_sus_lineas_pero_no_sus_materiales() -> None:
    """La linea pertenece a la cotizacion; el material, al maestro de la empresa."""
    upgrade = _upgrade()
    cotizacion = upgrade.split('["v2_quotation_id"], ["v2_quotations.id"]')[1].split("),")[0]
    assert 'ondelete="CASCADE"' in cotizacion
    # Y ningun material se borra por arrastre: RESTRICT en los cuatro.
    assert upgrade.count('ondelete="RESTRICT"') == 4


def test_una_linea_dice_si_el_esmalte_es_referencia_y_si_la_conversion_es_de_reserva() -> None:
    """Las dos son advertencias sobre el dato, y se guardan junto al dato.

    Sin ellas, dentro de un ano nadie podria distinguir un esmalte elegido a
    mano de uno propuesto por el sistema, ni un volumen medido de uno supuesto.
    """
    upgrade = _upgrade()
    assert "glaze_is_reference" in upgrade
    assert "glaze_conversion_is_fallback" in upgrade


# ---------------------------------------------------------------------------
# La vuelta
# ---------------------------------------------------------------------------
def test_el_downgrade_se_niega_antes_de_tocar_el_esquema() -> None:
    downgrade = _downgrade()
    assert "RuntimeError" in downgrade
    assert downgrade.index("RuntimeError") < downgrade.index("drop_table")


def test_el_downgrade_protege_las_dos_cosas_que_no_se_recuperan() -> None:
    """Las lineas ya calculadas y la valorizacion escrita material por material."""
    downgrade = _downgrade()
    assert "SELECT count(*) FROM v2_quotation_products" in downgrade
    assert "SELECT count(*) FROM v2_material_costs" in downgrade


def test_el_downgrade_no_borra_filas_de_nadie() -> None:
    downgrade = _downgrade().upper()
    for prohibido in ("DELETE FROM", "UPDATE ", "TRUNCATE"):
        assert prohibido not in downgrade, prohibido


# ---------------------------------------------------------------------------
# La cadena
# ---------------------------------------------------------------------------
def test_la_cadena_no_se_rompe() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0029") is not None
    assert script.get_revision("0030").down_revision == "0029"


def test_0030_sigue_en_el_camino_a_la_cabeza() -> None:
    """Lo que hay que proteger aqui es que la revision siga en la cadena.

    La afirmacion de «cabeza unica» acompana siempre a la ULTIMA revision y se
    mudo a `test_migration_0031.py` cuando entro. Dejarla aqui obligaria a
    reescribir esta prueba en cada fase y, mientras tanto, no comprobaria nada
    de 0030.
    """
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    assert "0030" in {revision.revision for revision in script.iterate_revisions(heads[0], "base")}
