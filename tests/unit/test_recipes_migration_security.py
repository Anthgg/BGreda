"""Pruebas unitarias de seguridad y RLS para la migracion 0006."""

from pathlib import Path


def test_0006_migration_rls_enabled_and_not_forced() -> None:
    """Verifica que la migracion 0006 configure RLS sin FORCE ROW LEVEL SECURITY."""
    migration_file = (
        Path(__file__).resolve().parent.parent.parent
        / "alembic"
        / "versions"
        / "0006_recipes_and_versioning.py"
    )
    content = migration_file.read_text(encoding="utf-8")

    assert "ENABLE ROW LEVEL SECURITY" in content
    assert "FORCE ROW LEVEL SECURITY" not in content
    assert "REVOKE ALL" in content
    for table in ["recipes", "recipe_versions", "recipe_lines"]:
        assert table in content
