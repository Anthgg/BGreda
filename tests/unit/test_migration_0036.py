"""0036 vigilandose a si misma: aditiva, con el trabajador intacto y una cabeza."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]


def _codigo() -> str:
    archivos = list((REPO_ROOT / "alembic" / "versions").glob("0036_*.py"))
    assert len(archivos) == 1
    return archivos[0].read_text(encoding="utf-8").split('"""', 2)[2]


def _upgrade() -> str:
    return _codigo().split("def upgrade()")[1].split("def downgrade()")[0]


def test_el_upgrade_no_destruye_nada() -> None:
    for prohibido in ("drop_column", "drop_table", "drop_index", "drop_constraint", "DELETE "):
        assert prohibido not in _upgrade(), prohibido


def test_no_toca_legacy_ni_el_inventario() -> None:
    upgrade = _upgrade()
    for prohibido in (
        "quotation_other_costs",
        "other_costs",
        "production_orders",
        "stock_movements",
    ):
        assert prohibido not in upgrade, prohibido


def test_el_trabajador_de_una_tarea_sigue_siendo_obligatorio() -> None:
    """El BLOCKER de la auditoria: un proceso sin trabajador es un PROCESO, no una tarea.

    Si esta migracion aflojara `worker_id`, una tarea podria existir sin jornal,
    sin jornada y sin tarifa que congelar, y el reparto de costos, la
    duplicacion y el PDF cuentan con que esos datos estan.
    """
    upgrade = _upgrade()
    assert "worker_id" not in upgrade
    assert "alter_column" not in upgrade


def test_un_proceso_por_tecnica_y_linea() -> None:
    assert (
        'sa.UniqueConstraint(\n            "v2_quotation_product_id", "technique_id"' in _upgrade()
    )


def test_el_quitado_se_marca_en_vez_de_borrarse() -> None:
    assert '"removed_at", sa.DateTime(timezone=True), nullable=True' in _upgrade()


def test_el_origen_solo_admite_producto_o_manual() -> None:
    assert "origin IN ('PRODUCT', 'MANUAL')" in _upgrade()


def test_el_adicional_de_una_cotizacion_congela_su_precio() -> None:
    upgrade = _upgrade()
    for columna in ("name_snapshot", "unit_snapshot", "unit_cost_snapshot"):
        assert columna in upgrade, columna


def test_el_downgrade_esta_protegido() -> None:
    assert "RuntimeError" in _codigo().split("def downgrade()")[1]


def test_0036_es_la_unica_cabeza() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert list(script.get_heads()) == ["0036"]
    assert script.get_revision("0036").down_revision == "0035"


def test_toda_la_cadena_se_renderiza_sin_base() -> None:
    """`alembic upgrade head --sql` no tiene conexion: ninguna migracion puede consultarla."""
    entorno = {**os.environ, "DATABASE_URL": "postgresql://ci:ci@localhost:5432/ci"}
    resultado = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        env=entorno,
        capture_output=True,
        text=True,
        check=False,
    )
    assert resultado.returncode == 0, resultado.stderr[-2000:]
    assert "v2_quotation_processes" in resultado.stdout
    assert "v2_extras" in resultado.stdout
