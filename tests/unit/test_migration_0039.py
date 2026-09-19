"""0039 vigilandose a si misma: aditiva, talonario Q-V2, factor x1..x2 y una cabeza."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.firing_quotation_v2 import V2FiringQuotation, V2FiringQuotationLine
from app.models.quoter_v2 import V2QuotationProduct
from app.models.quoter_v2_settings import V2CommercialSettings
from app.models.sequence import SequenceType

REPO_ROOT = Path(__file__).resolve().parents[2]


def _codigo() -> str:
    archivos = list((REPO_ROOT / "alembic" / "versions").glob("0039_*.py"))
    assert len(archivos) == 1
    return archivos[0].read_text(encoding="utf-8").split('"""', 2)[2]


def _upgrade() -> str:
    return _codigo().split("def upgrade()")[1].split("def downgrade()")[0]


def _downgrade() -> str:
    return _codigo().split("def downgrade()")[1]


def test_0039_es_la_unica_cabeza() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert list(script.get_heads()) == ["0039"]
    assert script.get_revision("0039").down_revision == "0038"


def test_el_upgrade_no_borra_datos() -> None:
    upgrade = _upgrade()
    for prohibido in ("drop_column", "drop_table", "drop_index", "DELETE "):
        assert prohibido not in upgrade, prohibido
    # Los unicos drop_constraint son los CHECK que se sustituyen en su sitio.
    assert upgrade.count("drop_constraint") == 2


def test_el_talonario_q_v2_se_da_de_alta() -> None:
    upgrade = _upgrade()
    assert "'FIRING_V2', 'Q-V2'" in upgrade
    assert SequenceType.FIRING_V2.value == "FIRING_V2"


def test_el_downgrade_se_niega_con_datos() -> None:
    downgrade = _downgrade()
    assert "RAISE EXCEPTION" in downgrade
    assert downgrade.index("RAISE EXCEPTION") < downgrade.index("drop_table")


def test_ningun_nombre_de_restriccion_pasa_de_63_caracteres() -> None:
    """PostgreSQL recorta en silencio los nombres largos y el modelo deja de coincidir."""
    for tabla in (
        V2FiringQuotation.__table__,
        V2FiringQuotationLine.__table__,
        V2QuotationProduct.__table__,
        V2CommercialSettings.__table__,
    ):
        for restriccion in tabla.constraints:
            assert restriccion.name is None or len(str(restriccion.name)) <= 63, restriccion.name


def test_los_check_de_la_migracion_son_los_del_modelo() -> None:
    codigo = _codigo()
    del_modelo = {
        str(c.name) for c in V2FiringQuotation.__table__.constraints if c.name is not None
    }
    for nombre in (
        "ck_v2_firing_quotations_factor_range",
        "ck_v2_firing_quotations_lifecycle_coherent",
        "ck_v2_firing_quotations_glaze_off_costs_nothing",
        "ck_v2_firing_quotations_piece_separation_range",
    ):
        assert nombre in codigo, nombre
        assert nombre in del_modelo, nombre
    assert "fk_v2_firing_quotation_lines_quotation_id" in codigo


def test_toda_la_cadena_se_renderiza_sin_base() -> None:
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
    assert "v2_firing_quotations" in resultado.stdout
    assert "FIRING_V2" in resultado.stdout
