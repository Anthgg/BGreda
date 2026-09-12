"""0033 vigilandose a si misma: aditiva, y sin una segunda politica fiscal.

El riesgo de esta fase tiene dos nombres.

El primero es `commercial_settings`. El IGV, la moneda y el escalon de redondeo
viven alli y son la unica fuente canonica del proyecto. La tentacion evidente al
construir el motor economico de V2 es darle los suyos «para no depender de
Legacy»: seria abrir dos sitios donde mirar el impuesto, y el dia que uno se
quedara viejo emitiria documentos incorrectos. 0033 no crea ninguno: lo que
guarda es el porcentaje que se USO, que es otra cosa.

El segundo es la tentacion contraria: no guardar nada y derivar el precio al
leer. Funcionaria mientras nadie tocara un maestro; el dia que suba un jornal,
el precio que el cliente ya acepto cambiaria de valor al abrir la cotizacion.
Un precio unitario redondeado no es un calculo: es un compromiso.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRACION = REPO_ROOT / "alembic" / "versions"


def _contenido() -> str:
    archivos = list(MIGRACION.glob("0033_*.py"))
    assert len(archivos) == 1, f"Se esperaba una sola 0033, hay {len(archivos)}"
    return archivos[0].read_text(encoding="utf-8")


def _codigo() -> str:
    """El fichero sin su docstring de cabecera.

    La cabecera nombra `commercial_settings` justo para explicar por que no se
    duplica; buscar sobre el fichero entero encontraria esa explicacion.
    """
    return _contenido().split('"""', 2)[2]


def _upgrade() -> str:
    """Las declaraciones de columnas y CHECK mas el cuerpo del upgrade.

    Las dos mitades son la misma decision: el cuerpo solo recorre las
    constantes, asi que mirarlo solo a el no veria ni una columna.
    """
    codigo = _codigo()
    return (
        codigo.split("def upgrade()")[0]
        + codigo.split("def upgrade()")[1].split("def downgrade()")[0]
    )


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
    """El resultado economico vive en las filas que ya explican la cotizacion."""
    assert "op.create_table(" not in _upgrade()


def test_el_upgrade_no_reescribe_historicos() -> None:
    """Ni un UPDATE. Una cotizacion anterior no nace con un precio inventado."""
    upgrade = _upgrade()
    for prohibido in ("op.execute", "UPDATE ", "INSERT "):
        assert prohibido not in upgrade, prohibido


# ---------------------------------------------------------------------------
# Una sola politica fiscal
# ---------------------------------------------------------------------------
def test_no_se_crea_una_configuracion_fiscal_paralela() -> None:
    """El IGV y la moneda se leen de `commercial_settings`, no se duplican."""
    codigo = _codigo()
    assert "commercial_settings" not in codigo
    for prohibido in ("tax_percent_default", "currency_default", "rounding_step_default"):
        assert prohibido not in codigo, prohibido


def test_el_upgrade_no_toca_las_cotizaciones_de_legacy() -> None:
    upgrade = _upgrade()
    sin_v2 = upgrade.replace("v2_quotations", "").replace("v2_quotation_", "")
    assert "quotations" not in sin_v2


def test_el_upgrade_no_toca_el_inventario_ni_el_factor_antiguo() -> None:
    upgrade = _upgrade()
    for prohibido in (
        "stock_movements",
        "stock_balances",
        "kiln_occupancy_factors",
        "occupancy_factor",
    ):
        assert prohibido not in upgrade, prohibido


# ---------------------------------------------------------------------------
# Lo que si hace
# ---------------------------------------------------------------------------
def test_guarda_las_dos_bases_de_costo() -> None:
    """Costo real y costo de produccion, en columnas distintas.

    Si compartieran una, la diferencia de la quema —lo unico que separa a las
    dos— dejaria de poder calcularse.
    """
    upgrade = _upgrade()
    assert "real_cost_total" in upgrade
    assert "production_cost_total" in upgrade


def test_guarda_las_tres_salidas_comerciales() -> None:
    upgrade = _upgrade()
    for columna in ("price_min", "price_target", "negotiated_price"):
        assert columna in upgrade, columna


def test_guarda_el_documento_reconstruido() -> None:
    """Subtotal, IGV y total: lo que el cliente vera sumado."""
    upgrade = _upgrade()
    for columna in ("subtotal_amount", "tax_amount", "total_amount"):
        assert columna in upgrade, columna


def test_guarda_el_precio_unitario_redondeado_de_cada_linea() -> None:
    """Es un compromiso ya comunicado, no un calculo que se pueda rehacer."""
    upgrade = _upgrade()
    assert "unit_price" in upgrade
    assert "unit_price_raw" in upgrade


def test_el_suelo_no_puede_pedir_mas_que_el_objetivo() -> None:
    """Si eso pasara no existiria ningun factor valido para la cotizacion."""
    assert "price_min <= price_target" in _upgrade()


def test_una_linea_sin_piezas_no_puede_llevar_importe() -> None:
    assert "quantity > 0 OR (line_subtotal = 0" in _upgrade()


def test_la_ganancia_puede_ser_negativa() -> None:
    """Una venta a perdida tiene que poder verse.

    Los costos y los precios llevan CHECK de no negatividad; la ganancia, el
    ajuste por redondeo y el margen NO, y eso es deliberado: esconder una
    perdida tras un cero seria mentir sobre el unico numero que importa mirar.
    """
    upgrade = _upgrade()
    assert "estimated_profit >= 0" not in upgrade
    assert "rounding_adjustment >= 0" not in upgrade
    assert "effective_margin_percent >= 0" not in upgrade


def test_los_importes_llevan_la_precision_de_calculo() -> None:
    """36,18 mientras no se ha redondeado nada.

    Perder decimales antes del punto comercial arrastra el error a todas las
    lineas, y el precio unitario ya no seria el que explica el subtotal.
    """
    upgrade = _upgrade()
    assert "sa.Numeric(36, 18)" in upgrade


# ---------------------------------------------------------------------------
# Downgrade protegido
# ---------------------------------------------------------------------------
def test_el_downgrade_se_niega_con_precios() -> None:
    downgrade = _downgrade()
    assert "RuntimeError" in downgrade
    assert "SELECT count(*) FROM v2_quotations" in downgrade


def test_el_downgrade_tambien_mira_los_unitarios_de_las_lineas() -> None:
    """Una cotizacion puede tener precio unitario y total todavia en cero.

    Pasa con una linea sin piezas: el unitario existe y el subtotal no. Mirar
    solo la cabecera dejaria borrar ese precio sin avisar.
    """
    downgrade = _downgrade()
    assert "SELECT count(*) FROM v2_quotation_products" in downgrade
    assert "unit_price <> 0" in downgrade


def test_el_downgrade_no_toca_tablas_de_legacy() -> None:
    downgrade = _downgrade()
    sin_v2 = downgrade.replace("v2_quotations", "").replace("v2_quotation_", "")
    assert "quotations" not in sin_v2
    assert "drop_table" not in downgrade


# ---------------------------------------------------------------------------
# La cadena
# ---------------------------------------------------------------------------
def test_la_cadena_no_se_rompe() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0032") is not None
    assert script.get_revision("0033").down_revision == "0032"


def test_0033_es_la_unica_cabeza() -> None:
    """Dos cabezas son un despliegue que se detiene a mitad, en produccion.

    Esta afirmacion acompana siempre a la ULTIMA revision y se retira de la
    anterior cuando entra una nueva; por eso ya no vive en 0032.
    """
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert list(script.get_heads()) == ["0033"]
