"""0034 vigilandose a si misma: aditiva, sin tocar Legacy ni el inventario.

El riesgo de esta fase no es un precio: es el HISTORICO. Una migracion que
«arreglara» las cotizaciones existentes inventandoles una fecha de emision, o
que colgara V2 de `production_orders` para reutilizar su arranque, destruiria
justo lo que 010H existe para proteger.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRACION = REPO_ROOT / "alembic" / "versions"


def _contenido() -> str:
    archivos = list(MIGRACION.glob("0034_*.py"))
    assert len(archivos) == 1, f"Se esperaba una sola 0034, hay {len(archivos)}"
    return archivos[0].read_text(encoding="utf-8")


def _codigo() -> str:
    """Sin la docstring de cabecera, que nombra lo prohibido para explicarlo."""
    return _contenido().split('"""', 2)[2]


def _upgrade() -> str:
    codigo = _codigo()
    return (
        codigo.split("def upgrade()")[0]
        + codigo.split("def upgrade()")[1].split("def downgrade()")[0]
    )


def _downgrade() -> str:
    return _codigo().split("def downgrade()")[1]


def test_el_upgrade_no_destruye_nada() -> None:
    upgrade = _upgrade()
    for prohibido in ("drop_column", "drop_table", "drop_index", "drop_constraint"):
        assert prohibido not in upgrade, prohibido
    assert "rename" not in upgrade.lower()


def test_el_upgrade_no_reescribe_historicos() -> None:
    """Ni un UPDATE ni un INSERT: una cotizacion existente no nace emitida."""
    upgrade = _upgrade()
    for prohibido in ("op.execute", "UPDATE ", "INSERT ", "bulk_insert"):
        assert prohibido not in upgrade, prohibido


def test_las_columnas_nuevas_son_anulables() -> None:
    """Un borrador no tiene emision: NOT NULL obligaria a inventarla."""
    assert "nullable=False" not in _upgrade().split("op.create_table(")[0]


def test_el_upgrade_no_toca_legacy_ni_la_produccion_existente() -> None:
    upgrade = _upgrade()
    sin_v2 = upgrade.replace("v2_quotations", "").replace("v2_quotation_", "")
    assert "quotations" not in sin_v2
    for prohibido in ("production_orders", "production_order_lines", "quotation_items"):
        assert prohibido not in upgrade, prohibido


def test_el_upgrade_no_toca_el_inventario() -> None:
    upgrade = _upgrade()
    for prohibido in ("stock_movements", "stock_balances", "inventory"):
        assert prohibido not in upgrade, prohibido


def test_no_se_anade_un_estado_vencida_persistido() -> None:
    """`EXPIRED` se deriva de `expires_at`: guardarlo exigiria un proceso a medianoche."""
    upgrade = _upgrade()
    assert "EXPIRED" not in upgrade
    assert "status_allowed" not in upgrade.replace(
        'sa.CheckConstraint("status IN (\'READY_FOR_PRODUCTION\')", name="status_allowed")', ""
    )


def test_el_puente_es_unico_por_cotizacion() -> None:
    """Dos clics simultaneos en «Enviar a produccion» solo los detiene la base."""
    upgrade = _upgrade()
    assert "v2_production_handoffs" in upgrade
    assert 'sa.UniqueConstraint("v2_quotation_id"' in upgrade


def test_un_solo_borrador_abierto_por_duplicacion() -> None:
    upgrade = _upgrade()
    assert "uq_v2_quotations_open_duplicate" in upgrade
    assert "unique=True" in upgrade
    assert "status = 'DRAFT'" in upgrade


def test_el_ciclo_de_vida_se_protege_con_check() -> None:
    upgrade = _upgrade()
    assert "lifecycle_coherent" in upgrade
    assert "status IS NOT NULL AND status = 'CONFIRMED'" in upgrade
    assert "expires_at > issued_at" in upgrade


def test_el_upgrade_se_niega_si_ya_hay_no_borradores() -> None:
    """Sin forma de emitir hasta hoy, una no-borrador es un cambio a mano."""
    upgrade = _upgrade()
    assert "status <> 'DRAFT'" in upgrade
    assert "RuntimeError" in upgrade


def test_el_downgrade_se_niega_con_documentos_emitidos() -> None:
    downgrade = _downgrade()
    assert "RuntimeError" in downgrade
    assert "status <> 'DRAFT'" in downgrade
    assert "v2_production_handoffs" in downgrade


def test_la_cadena_no_se_rompe() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0034").down_revision == "0033"


def test_0034_es_la_unica_cabeza() -> None:
    """Dos cabezas son un despliegue que se detiene a mitad, en produccion."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert list(script.get_heads()) == ["0034"]
