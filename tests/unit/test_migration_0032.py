"""0032 vigilandose a si misma: aditiva, y sin tocar el motor de quema Legacy.

El riesgo de esta fase tiene nombre: `kiln_occupancy_factors`. El Cotizador
historico multiplica el costo de una pieza por un factor que crece cuando la
pieza ocupa poco horno —hasta x3—, y esa tabla es donde vive la curva de cada
horno. V2 elimina ese factor, y de ahi las dos tentaciones:

- **borrarla**, «total ya no se usa». Seria retirarle a Legacy la mitad de su
  formula: los precios historicos dejarian de poder explicarse y la hoja de
  quema real de produccion se quedaria sin factor que resolver;
- **reutilizarla** en V2 bajo otro nombre —un «factor de eficiencia», un
  «ajuste por carga»—. Volveria el x3 por la puerta de atras, que es justo lo
  que la fase decidio eliminar.

La respuesta de 0032 a las dos es la misma: no la menciona. Legacy sigue
multiplicando igual que ayer y V2 no la lee nunca.

La otra tentacion es guardar la diferencia de quema como una columna corriente.
Es `comercial - gas`, y los dos sumandos viven en la misma fila: una columna
generada no puede contradecirlos, una corriente si.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRACION = REPO_ROOT / "alembic" / "versions"


def _contenido() -> str:
    archivos = list(MIGRACION.glob("0032_*.py"))
    assert len(archivos) == 1, f"Se esperaba una sola 0032, hay {len(archivos)}"
    return archivos[0].read_text(encoding="utf-8")


def _codigo() -> str:
    """El fichero sin su docstring de cabecera.

    La cabecera explica POR QUE no se toca el factor por ocupacion, asi que
    nombra la tabla. Buscar la palabra sobre el fichero entero encontraria esa
    explicacion y la prueba fallaria por leer su propio motivo.
    """
    return _contenido().split('"""', 2)[2]


def _upgrade() -> str:
    """Lo que aplica la revision: sus declaraciones y el cuerpo del upgrade.

    Las columnas y los CHECK se declaran en constantes de modulo y el cuerpo
    solo las recorre, asi que mirar unicamente el cuerpo no veria ni una sola
    columna. Las dos mitades son la misma decision.
    """
    codigo = _codigo()
    declaraciones = codigo.split("def upgrade()")[0]
    cuerpo = codigo.split("def upgrade()")[1].split("def downgrade()")[0]
    return declaraciones + cuerpo


def _downgrade() -> str:
    return _codigo().split("def downgrade()")[1]


# ---------------------------------------------------------------------------
# Aditiva
# ---------------------------------------------------------------------------
def test_el_upgrade_no_destruye_nada() -> None:
    upgrade = _upgrade()
    for prohibido in ("drop_column", "drop_table", "drop_index", "drop_constraint"):
        assert prohibido not in upgrade, prohibido
    assert "rename" not in upgrade.lower()


def test_el_upgrade_no_crea_ninguna_tabla() -> None:
    """Las tarifas de horno ya existen desde 0029 y los hornos desde mucho antes.

    Crear una segunda tabla de tarifas seria abrir dos sitios donde mirar el
    precio de una quema, y uno de los dos acabaria desactualizado.
    """
    assert "op.create_table(" not in _upgrade()


def test_el_upgrade_no_reescribe_historicos() -> None:
    """Ni un UPDATE. Una cotizacion anterior no nace con un horno inventado."""
    upgrade = _upgrade()
    for prohibido in ("op.execute", "UPDATE ", "INSERT "):
        assert prohibido not in upgrade, prohibido


# ---------------------------------------------------------------------------
# El factor antiguo no reaparece
# ---------------------------------------------------------------------------
def test_el_factor_por_ocupacion_de_legacy_no_se_toca() -> None:
    """`kiln_occupancy_factors` ni se borra ni se copia ni se lee.

    Se mira el CODIGO y no el fichero entero: la cabecera nombra la tabla justo
    para explicar por que no se toca, y buscar ahi encontraria la explicacion.
    """
    codigo = _codigo()
    assert "kiln_occupancy_factors" not in codigo
    assert "occupancy_factor" not in codigo


def test_no_aparece_ninguna_columna_de_multiplicador() -> None:
    """El x3 no vuelve con otro nombre.

    Se comprueban las palabras con las que volveria: un «factor» o un
    «multiplicador» dentro del bloque de quema seria exactamente la regla que
    esta fase elimino, escrita de nuevo.
    """
    upgrade = _upgrade().lower()
    for prohibido in ("factor", "multiplier", "multiplicador", "bracket", "tramo"):
        assert prohibido not in upgrade, prohibido


def test_las_hojas_de_quema_reales_no_se_tocan() -> None:
    """`firings`, `firing_lines` y `firing_kiln_sessions` son produccion."""
    upgrade = _upgrade()
    for tabla in ("firing_lines", "firing_kiln_sessions", '"firings"'):
        assert tabla not in upgrade, tabla


def test_el_upgrade_no_toca_las_cotizaciones_de_legacy() -> None:
    upgrade = _upgrade()
    sin_v2 = upgrade.replace("v2_quotations", "").replace("v2_quotation_", "")
    assert "quotations" not in sin_v2


def test_el_upgrade_no_toca_el_inventario() -> None:
    """Cotizar no consume existencia, y menos aun encender un horno."""
    upgrade = _upgrade()
    for tabla in ("stock_movements", "stock_balances", "stock_locations"):
        assert tabla not in upgrade, tabla


# ---------------------------------------------------------------------------
# Lo que si hace
# ---------------------------------------------------------------------------
def test_anade_el_horno_a_la_cotizacion_con_restrict() -> None:
    """RESTRICT: retirar un horno no puede borrar el documento que lo uso."""
    upgrade = _upgrade()
    assert '"kiln_id"' in upgrade
    assert "create_foreign_key" in upgrade
    assert 'ondelete="RESTRICT"' in upgrade


def test_congela_la_capacidad_del_horno() -> None:
    """Sin la capacidad congelada, remedir el horno recalcularia las hornadas."""
    assert "kiln_capacity_snapshot" in _upgrade()


def test_separa_el_gas_real_de_la_tarifa_comercial() -> None:
    """Cuatro columnas y no dos: son COSTO y PRECIO, y su diferencia importa."""
    upgrade = _upgrade()
    for columna in (
        "gas_cost_low_snapshot",
        "gas_cost_high_snapshot",
        "commercial_rate_low_snapshot",
        "commercial_rate_high_snapshot",
    ):
        assert columna in upgrade, columna


def test_la_diferencia_es_una_columna_generada() -> None:
    upgrade = _upgrade()
    assert "sa.Computed(" in upgrade
    assert "firing_commercial_total - firing_gas_total" in upgrade


def test_las_medidas_admiten_nulo_pero_no_cero() -> None:
    """NULL es «sin medir»; un cero seria una pieza plana que no existe."""
    upgrade = _upgrade()
    for expresion in (
        "length_cm IS NULL OR length_cm > 0",
        "width_cm IS NULL OR width_cm > 0",
        "height_cm IS NULL OR height_cm > 0",
    ):
        assert expresion in upgrade, expresion


def test_una_quema_apagada_no_puede_tener_hornadas() -> None:
    """Mismo criterio que el esmalte de 010C y la ilustracion de 010D."""
    upgrade = _upgrade()
    assert "coalesce(low_fire_enabled, false) OR low_fire_count = 0" in upgrade
    assert "coalesce(high_fire_enabled, false) OR high_fire_count = 0" in upgrade


def test_sin_horno_no_puede_haber_importe() -> None:
    assert "firing_requires_kiln" in _upgrade()


def test_las_hornadas_de_baja_y_alta_no_superan_las_del_volumen() -> None:
    """Baja y alta se hacen sobre la MISMA carga: no pueden pedir mas encendidos."""
    upgrade = _upgrade()
    assert "low_fire_count <= firing_count" in upgrade
    assert "high_fire_count <= firing_count" in upgrade


# ---------------------------------------------------------------------------
# Downgrade protegido
# ---------------------------------------------------------------------------
def test_el_downgrade_se_niega_con_datos() -> None:
    downgrade = _downgrade()
    assert "RuntimeError" in downgrade
    assert "SELECT count(*) FROM v2_quotations" in downgrade


def test_el_downgrade_tambien_mira_las_medidas() -> None:
    """Una cotizacion puede tener las piezas medidas y no haber elegido horno.

    Esas medidas se tecleraron a mano y no se derivan de nada: revertir sin
    mirarlas las borraria sin forma de recuperarlas.
    """
    downgrade = _downgrade()
    assert "SELECT count(*) FROM v2_quotation_products" in downgrade
    assert "length_cm IS NOT NULL" in downgrade


def test_el_downgrade_no_toca_tablas_de_legacy() -> None:
    downgrade = _downgrade()
    sin_v2 = downgrade.replace("v2_quotations", "").replace("v2_quotation_", "")
    assert "quotations" not in sin_v2
    assert "kiln_occupancy_factors" not in downgrade
    for prohibido in ("drop_table", "kiln_rates"):
        assert prohibido not in downgrade, prohibido


# ---------------------------------------------------------------------------
# La cadena
# ---------------------------------------------------------------------------
def test_la_cadena_no_se_rompe() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0031") is not None
    assert script.get_revision("0032").down_revision == "0031"


def test_0032_sigue_en_el_camino_a_la_cabeza() -> None:
    """Lo que hay que proteger aqui es que la revision siga en la cadena.

    La afirmacion de «cabeza unica» acompana siempre a la ULTIMA revision y se
    mudo a `test_migration_0033.py` cuando entro. Dejarla aqui obligaria a
    reescribir esta prueba en cada fase y, mientras tanto, no comprobaria nada
    de 0032.
    """
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    assert "0032" in {revision.revision for revision in script.iterate_revisions(heads[0], "base")}
