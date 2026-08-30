"""Contrato de la migracion 0016: politica comercial configurable (Fase 009E)."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATION_FILE = (
    Path(__file__).parents[2] / "alembic" / "versions" / "0016_commercial_pricing_policy.py"
)


def test_migration_0016_file_and_revisions() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "0016"' in content
    assert 'down_revision: str | None = "0015"' in content


def test_migration_0016_upgrade_contract() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert "def upgrade() -> None:" in content
    assert "def downgrade() -> None:" in content
    assert "production_factor_default" in content
    assert "rounding_step" in content
    # El CHECK vive en la base: es el unico sitio por el que no se puede colar
    # un tercer paso de redondeo.
    assert "rounding_step IN (0.50, 1.00)" in content
    assert "production_factor_default > 0" in content
    # Backfill explicito: no basta con server_default para poder demostrarlo.
    assert "UPDATE commercial_settings" in content


def test_migration_0016_only_touches_commercial_settings() -> None:
    """No se aprovecha el viaje para colar columnas en otras tablas."""
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    for tabla in ("other_costs", "quotations", "quotation_items", "products"):
        assert f'"{tabla}"' not in content, f"0016 no debe tocar {tabla}"


def test_migration_0016_downgrade_undoes_everything() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    downgrade = content.split("def downgrade()")[1]
    assert 'op.drop_column("commercial_settings", "rounding_step")' in downgrade
    assert 'op.drop_column("commercial_settings", "production_factor_default")' in downgrade


def test_alembic_single_head_is_0016() -> None:
    """Una sola cabeza: dos harian fallar `alembic upgrade head` en el deploy."""
    alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(alembic_cfg)
    assert script.get_heads() == ["0016"]
