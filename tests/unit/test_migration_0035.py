"""0035 vigilandose a si misma: aditiva, con backfill de lo ya cotizado y una cabeza."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]


def _codigo() -> str:
    archivos = list((REPO_ROOT / "alembic" / "versions").glob("0035_*.py"))
    assert len(archivos) == 1
    return archivos[0].read_text(encoding="utf-8").split('"""', 2)[2]


def _upgrade() -> str:
    return _codigo().split("def upgrade()")[1].split("def downgrade()")[0]


def test_el_upgrade_no_destruye_nada() -> None:
    for prohibido in ("drop_column", "drop_table", "drop_index", "drop_constraint", "DELETE "):
        assert prohibido not in _upgrade(), prohibido


def test_no_toca_legacy_ni_el_inventario() -> None:
    upgrade = _upgrade()
    for prohibido in ("quotations ", "production_orders", "stock_movements", "labor_rates"):
        assert prohibido not in upgrade, prohibido


def test_la_relacion_es_unica_por_par() -> None:
    assert 'sa.UniqueConstraint("worker_id", "technique_id"' in _upgrade()


def test_el_backfill_sale_de_lo_ya_cotizado_y_no_inventa() -> None:
    upgrade = _upgrade()
    assert "SELECT DISTINCT worker_id, technique_id FROM v2_quotation_labor" in upgrade
    assert "CROSS JOIN" not in upgrade.upper()


def test_las_horas_manuales_nacen_en_falso() -> None:
    assert (
        '"manual_hours", sa.Boolean(), nullable=False, server_default=sa.text("false")'
        in _upgrade()
    )


def test_el_downgrade_esta_protegido() -> None:
    assert "RuntimeError" in _codigo().split("def downgrade()")[1]


def test_0035_sigue_encadenada_a_0034() -> None:
    """La cabeza se la lleva la migracion mas nueva; el eslabon es lo que importa."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1
    assert script.get_revision("0035").down_revision == "0034"


def test_toda_la_cadena_se_renderiza_sin_base() -> None:
    """`alembic upgrade head --sql` no tiene conexion: ninguna migracion puede consultarla.

    La CI lo comprueba en un paso aparte y 0034 lo rompio con su comprobacion
    previa. Esta prueba lo detecta antes de llegar a la CI.
    """
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
    assert "v2_worker_techniques" in resultado.stdout
