from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATION_FILE = (
    Path(__file__).parents[2] / "alembic" / "versions" / "0013_allow_mixed_tax_rate_source.py"
)


def test_migration_0013_file_and_revisions() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "0013"' in content
    assert 'down_revision: str | None = "0012"' in content
    assert "ck_quotations_tax_rate_source_allowed" in content
    assert "PRODUCT" in content
    assert "COMMERCIAL_SETTINGS" in content
    assert "MIXED" in content


def test_migration_0013_upgrade_downgrade_contracts() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert "def upgrade() -> None:" in content
    assert "def downgrade() -> None:" in content
    assert "drop_constraint" in content
    assert "create_check_constraint" in content
    assert "'PRODUCT', 'COMMERCIAL_SETTINGS', 'MIXED'" in content
    assert "'PRODUCT', 'COMMERCIAL_SETTINGS'" in content


def test_alembic_heads_single_0013() -> None:
    alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(alembic_cfg)
    heads = script.get_heads()
    assert heads == ["0013"]
