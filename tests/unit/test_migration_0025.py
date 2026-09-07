"""0025 vigilandose a si misma: que solo anada.

El despliegue es DB primero. Entre que la base llega a 0025 y el backend nuevo
recibe trafico, la revision anterior sigue sirviendo: escribe cotizaciones y
emite correlativos sin saber que existen las columnas de actor. Cualquier cosa
que 0025 quite, renombre o vuelva obligatoria la tumbaria en esa ventana.

Estas pruebas leen el ARCHIVO. El efecto sobre una base real lo comprueba
`tests/db/test_migration_0025_runs.py`; lo que aqui se protege es que nadie
convierta la migracion en destructiva —o en un backfill— de un commit a otro
sin enterarse.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRACION = REPO_ROOT / "alembic" / "versions"


def _contenido() -> str:
    archivos = list(MIGRACION.glob("0025_*.py"))
    assert len(archivos) == 1, f"Se esperaba una sola 0025, hay {len(archivos)}"
    return archivos[0].read_text(encoding="utf-8")


def _upgrade() -> str:
    return _contenido().split("def upgrade()")[1].split("def downgrade()")[0]


def test_el_upgrade_solo_anade_columnas() -> None:
    """0025_ADDITIVE."""
    upgrade = _upgrade()
    assert upgrade.count("add_column") == 5
    for prohibido in ("drop_column", "drop_table", "create_table", "alter_column"):
        assert prohibido not in upgrade, prohibido
    assert "rename" not in upgrade.lower()


def test_el_upgrade_no_inventa_actores_historicos() -> None:
    """HISTORICAL_ACTOR_BACKFILL_FABRICATED: NO.

    La tentacion era rellenar los documentos viejos con el administrador
    actual para que la pantalla se vea completa. Seria una afirmacion falsa, y
    una vez escrita ya no se distingue de la verdad.
    """
    upgrade = _upgrade()
    for prohibido in ("UPDATE ", "update(", "INSERT ", "insert(", "DELETE ", "delete("):
        assert prohibido not in upgrade, prohibido


def test_las_cinco_columnas_nacen_anulables() -> None:
    """Hay cotizaciones anteriores sin actor: obligatorias no cabrian."""
    upgrade = _upgrade()
    assert upgrade.count("nullable=True") == 5
    assert "nullable=False" not in upgrade


def test_no_se_toca_el_perfil() -> None:
    """PROFILE_FIRST_LAST_ADDED: NO. PROFILE_EMAIL_ADDED: NO."""
    contenido = _contenido()
    assert "first_name" not in contenido
    assert "last_name" not in contenido
    assert '"profiles"' not in _upgrade()


def test_no_se_renombran_las_columnas_viejas() -> None:
    """`quotations.created_by_id` y `prototype_quotations.created_by` se quedan.

    Los dos nombres son asimetricos y lo seguiran siendo: unificarlos por
    estetica del diagrama seria un cambio destructivo a cambio de nada.
    """
    upgrade = _upgrade()
    assert '"created_by_id"' not in upgrade.replace('"confirmed_by_id"', "")
    assert "alter_column" not in upgrade


def test_la_cadena_no_se_rompe() -> None:
    """0025 cuelga de 0024, y 0024 sigue existiendo."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0024") is not None
    assert script.get_revision("0025").down_revision == "0024"
