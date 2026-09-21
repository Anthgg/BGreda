"""0038 vigilandose a si misma: aditiva, conserva las filas viejas y una cabeza."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.quoter_v2 import V2Quotation, V2QuotationProduct
from app.models.quoter_v2_settings import V2CommercialSettings

REPO_ROOT = Path(__file__).resolve().parents[2]


def _codigo() -> str:
    archivos = list((REPO_ROOT / "alembic" / "versions").glob("0038_*.py"))
    assert len(archivos) == 1
    return archivos[0].read_text(encoding="utf-8").split('"""', 2)[2]


def _upgrade() -> str:
    return _codigo().split("def upgrade()")[1].split("def downgrade()")[0]


def _downgrade() -> str:
    return _codigo().split("def downgrade()")[1]


def test_0038_sigue_en_una_cadena_de_una_sola_cabeza() -> None:
    """La cabeza se la lleva la migracion mas nueva; esa afirmacion vive en 0039."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1
    assert script.get_revision("0038").down_revision == "0037"


def test_el_upgrade_no_destruye_datos() -> None:
    upgrade = _upgrade()
    for prohibido in ("drop_column", "drop_table", "drop_index", "drop_constraint", "DELETE "):
        assert prohibido not in upgrade, prohibido


def test_las_filas_viejas_quedan_exclusivas_y_sin_separacion() -> None:
    """El motor de 010E = exclusiva sin separacion: asi se rellenan, sin mover un numero.

    Las columnas nacen con el default que CONSERVA las filas existentes y solo
    despues el default pasa a ser el de las cotizaciones nuevas.
    """
    upgrade = _upgrade()
    modo = upgrade.index("server_default=sa.text(\"'EXCLUSIVE'\")")
    modo_nuevo = upgrade.index(
        'op.alter_column("v2_quotations", "firing_mode", server_default=sa.text("\'SHARED\'"))'
    )
    assert modo < modo_nuevo
    separacion = upgrade.index('"piece_separation_cm_snapshot",')
    separacion_nueva = upgrade.index(
        'op.alter_column("v2_quotations", "piece_separation_cm_snapshot"'
    )
    assert separacion < separacion_nueva
    assert "UPDATE v2_quotations SET firing_billed_load = firing_count" in upgrade
    assert "SET commercial_factor_target_snapshot = commercial_factor_max_snapshot" in upgrade


def test_los_unicos_update_son_los_rellenos() -> None:
    assert _upgrade().count("UPDATE ") == 2


def test_el_downgrade_se_niega_si_hay_reglas_nuevas_en_uso() -> None:
    downgrade = _downgrade()
    assert "RAISE EXCEPTION" in downgrade
    assert downgrade.index("RAISE EXCEPTION") < downgrade.index("drop_column")


def test_los_check_de_la_migracion_son_los_del_modelo() -> None:
    """Un CHECK solo en el modelo o solo en la migracion es una regla que miente."""
    codigo = _codigo()
    for tabla, nombres in (
        (
            V2Quotation.__table__,
            (
                "firing_mode_allowed",
                "firing_billed_load_non_negative",
                "piece_separation_snapshot_range",
                "commercial_factor_target_snapshot_floor",
            ),
        ),
        (
            V2QuotationProduct.__table__,
            (
                "line_illustration_hours_non_negative",
                "line_illustration_cost_non_negative",
            ),
        ),
        (V2CommercialSettings.__table__, ("piece_separation_range",)),
    ):
        # `line_illustration_quantity_non_negative` quedo con otro nombre en la
        # base (pasaba de 63 caracteres); 0039 lo renombra y lo comprueba su prueba.
        del_modelo = {str(c.name) for c in tabla.constraints if c.name is not None}
        for nombre in nombres:
            assert f'"{nombre}"' in codigo, nombre
            assert f"ck_{tabla.name}_{nombre}" in del_modelo, nombre


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
    assert "firing_mode" in resultado.stdout
    assert "piece_separation_cm" in resultado.stdout
