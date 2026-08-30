"""Contrato de la migracion 0015: preparaciones, volumen y estimacion (Fase 009D)."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATION_FILE = (
    Path(__file__).parents[2] / "alembic" / "versions" / "0015_recipe_preparations_and_volume.py"
)


def test_migration_0015_file_and_revisions() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "0015"' in content
    assert 'down_revision: str | None = "0014"' in content


def test_migration_0015_upgrade_contract() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert "def upgrade() -> None:" in content
    assert "def downgrade() -> None:" in content
    # Volumen: sin esto no existe el mililitro y todo el modelo de agua cae.
    assert "'MASS', 'COUNT', 'VOLUME'" in content
    assert "'ml', 'Mililitro'" in content
    assert "'l',  'Litro'" in content
    # Las dos tablas del lote.
    assert '"recipe_preparations"' in content
    assert '"recipe_preparation_lines"' in content
    # Trazabilidad del movimiento hasta el lote.
    assert "preparation_id" in content
    assert "'PREPARATION_OUT', 'PREPARATION_IN'" in content
    # El 15 % en la misma convencion que tax_percent.
    assert "estimated_glaze_percent" in content
    assert "estimated_glaze_percent > 0 AND estimated_glaze_percent <= 100" in content
    # Secuencia del lote, con la convencion documental ya existente.
    assert "'PREPARATION', 'PREP'" in content


def test_migration_0015_downgrade_undoes_everything() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    downgrade = content[content.index("def downgrade() -> None:") :]
    assert "drop_table" in downgrade
    assert "drop_column" in downgrade
    # Y deja el CHECK de unidades como estaba, sin VOLUME.
    assert "dimension IN ('MASS', 'COUNT')" in downgrade


def test_0015_sigue_en_la_cadena() -> None:
    """0015 tiene que seguir siendo alcanzable desde la cabeza actual.

    La comprobacion de "cabeza unica" vive con la ULTIMA migracion, no aqui:
    dejarla anclada a 0015 obligaba a editar este archivo en cada fase.
    """
    alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(alembic_cfg)
    revisiones = {revision.revision for revision in script.walk_revisions()}
    assert "0015" in revisiones
