"""Contrato de la migracion 0014: duracion de hornada por horno (Fase 009C)."""

from pathlib import Path

MIGRATION_FILE = (
    Path(__file__).parents[2] / "alembic" / "versions" / "0014_kiln_firing_days_per_batch.py"
)


def test_migration_0014_file_and_revisions() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "0014"' in content
    assert 'down_revision: str | None = "0013"' in content
    assert "firing_days_per_batch" in content


def test_migration_0014_upgrade_downgrade_contracts() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert "def upgrade() -> None:" in content
    assert "def downgrade() -> None:" in content
    # La columna es NOT NULL sobre una tabla con filas: sin server_default el
    # upgrade fallaria en produccion.
    assert "nullable=False" in content
    assert "server_default" in content
    # Un horno de cero dias no existe: la base lo impide, no solo Pydantic.
    assert "firing_days_per_batch >= 1" in content
    # El backfill del horno grande es un dato puntual, no logica de negocio.
    assert "UPDATE kilns SET firing_days_per_batch" in content
    assert "drop_column" in content


# La cabeza unica se comprueba junto a la migracion mas reciente
# (tests/unit/test_migration_0015.py): es una propiedad del arbol entero.
