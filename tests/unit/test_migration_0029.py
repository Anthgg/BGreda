"""0029 vigilandose a si misma: aditiva, y sin un segundo IGV.

El despliegue es DB primero. Entre que la base llega a 0029 y el backend nuevo
recibe trafico, la revision anterior sigue sirviendo: crea cotizaciones V2 sin
saber que existen columnas de snapshot. Anadirlas anulables no la molesta;
cualquier cosa que 0029 quitara o volviera obligatoria la tumbaria.

La tentacion concreta de esta fase es distinta a la de 010A. Aqui no es el
backfill: es **duplicar**. Copiar el IGV o la moneda a la tabla nueva «para que
V2 sea autonomo» parece limpio y crea dos verdades, y el dia que alguien edite
la que no es, el documento sale con el impuesto equivocado.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRACION = REPO_ROOT / "alembic" / "versions"


def _contenido() -> str:
    archivos = list(MIGRACION.glob("0029_*.py"))
    assert len(archivos) == 1, f"Se esperaba una sola 0029, hay {len(archivos)}"
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


def test_el_upgrade_no_toca_la_configuracion_de_la_empresa() -> None:
    """`commercial_settings` se queda exactamente como estaba.

    Es la fuente canonica del IGV, la moneda y el redondeo. 0029 la consulta
    desde el servicio, pero no le anade ni le cambia una sola columna.
    """
    # Precision deliberada: "v2_commercial_settings" CONTIENE
    # "commercial_settings", asi que una comparacion ingenua pasaria siempre.
    upgrade = _upgrade()
    sin_v2 = upgrade.replace("v2_commercial_settings", "")
    assert "commercial_settings" not in sin_v2


def test_el_upgrade_no_toca_las_tarifas_de_horno_de_legacy() -> None:
    """`kiln_rates` intacta.

    El costeo Legacy toma la primera tarifa que encuentra para cada
    `(horno, tipo)`. Una fila nueva ahi dentro le haria cobrar mal en silencio.
    """
    upgrade = _upgrade()
    assert '"kiln_rates"' not in upgrade
    assert "v2_kiln_rates" in upgrade


def test_el_upgrade_no_escribe_sobre_datos_de_negocio() -> None:
    """El unico INSERT es la fila unica de configuracion, y es idempotente."""
    upgrade = _upgrade()
    mayusculas = upgrade.upper()
    assert "UPDATE " not in mayusculas
    assert "DELETE FROM" not in mayusculas
    assert mayusculas.count("INSERT INTO") == 1
    assert "v2_commercial_settings" in upgrade
    assert "ON CONFLICT DO NOTHING" in upgrade


def test_las_columnas_de_snapshot_son_todas_anulables() -> None:
    """NULL ahi significa «cotizacion anterior al snapshot», no «vale cero».

    Y es lo que permite que la revision anterior del backend siga insertando
    durante la ventana de despliegue.
    """
    # Las columnas se declaran en la constante del modulo y se aplican en
    # bucle, asi que la afirmacion se hace sobre la sentencia que las anade.
    upgrade = _upgrade()
    assert 'op.add_column("v2_quotations", sa.Column(nombre, tipo, nullable=True))' in upgrade
    # Y ninguna de ellas se declara obligatoria en la constante que las lista.
    constante = _contenido().split("SNAPSHOT_COLUMNS:")[1].split("SNAPSHOT_CHECKS")[0]
    assert "nullable=False" not in constante


# ---------------------------------------------------------------------------
# Sin duplicar
# ---------------------------------------------------------------------------
def test_la_configuracion_v2_no_declara_su_propio_igv() -> None:
    """Ni moneda, ni simbolo, ni paso de redondeo."""
    tabla = _upgrade().split('"v2_commercial_settings"')[1].split("op.execute")[0]
    for prohibida in ("tax_percent", "currency_code", "currency_symbol", "rounding_step"):
        assert prohibida not in tabla, f"v2_commercial_settings declaro {prohibida}"


def test_las_tres_tarifas_de_horno_son_columnas_distintas() -> None:
    """Costo del gas y precio a cada publico son tres conceptos, no uno."""
    tabla = _upgrade().split('"v2_kiln_rates"')[1]
    for columna in ("gas_cost", "external_rate", "student_rate"):
        assert columna in tabla


def test_el_snapshot_congela_tambien_el_redondeo() -> None:
    """Decide el precio final de cualquier cotizacion, la calcule quien la calcule."""
    assert "rounding_step_snapshot" in _contenido()


def test_moneda_y_tipo_de_cambio_solo_admiten_combinaciones_posibles() -> None:
    """Tres casos validos y ninguno mas.

    Una tasa sin moneda, un tipo de cambio en moneda base o una cotizacion en
    moneda extranjera SIN tipo de cambio son datos rotos, no historia.
    """
    contenido = _contenido()
    assert "currency_and_exchange_rate_coherent" in contenido
    # `upper()` porque la comparacion en SQL distingue mayusculas y un 'pen'
    # minusculo se colaria como moneda extranjera.
    assert "upper(currency_code_snapshot)" in contenido
    assert "exchange_rate_snapshot IS NOT NULL" in contenido


def test_el_downgrade_protege_las_tarifas_configuradas_a_mano() -> None:
    """Se escriben horno por horno: borrarlas no se recupera con un upgrade."""
    assert "SELECT count(*) FROM v2_kiln_rates" in _downgrade()


def test_el_suelo_del_factor_esta_en_la_base() -> None:
    """x2 es regla cerrada: no puede depender de que el servicio la recuerde."""
    contenido = _contenido()
    # En la configuracion: el minimo no puede bajar de x2.
    assert "commercial_factor_min >= 2" in contenido
    # Y en cada cotizacion: ni el factor congelado ni el minimo que copio.
    assert "commercial_factor IS NULL OR commercial_factor >= 2" in contenido
    assert "commercial_factor_min_snapshot >= 2" in contenido


# ---------------------------------------------------------------------------
# La vuelta
# ---------------------------------------------------------------------------
def test_el_downgrade_se_niega_antes_de_tocar_el_esquema() -> None:
    downgrade = _downgrade()
    assert "RuntimeError" in downgrade
    assert downgrade.index("RuntimeError") < downgrade.index("drop_column")


def test_el_downgrade_protege_las_cotizaciones_ya_congeladas() -> None:
    """Sin sus numeros, una cotizacion emitida deja de poder explicarse."""
    downgrade = _downgrade()
    assert "settings_captured_at IS NOT NULL" in downgrade


# ---------------------------------------------------------------------------
# La cadena
# ---------------------------------------------------------------------------
def test_la_cadena_no_se_rompe() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0028") is not None
    assert script.get_revision("0029").down_revision == "0028"
