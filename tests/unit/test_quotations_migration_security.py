from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2] / "alembic" / "versions" / "0008_quotations_and_cost_masters.py"
)


def content() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_all_new_tables_enable_rls_without_public_policies() -> None:
    source = content()
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "CREATE POLICY" not in source.upper()
    assert "FORCE ROW LEVEL SECURITY" not in source


def test_public_postgrest_roles_are_revoked() -> None:
    source = content()
    assert 'for role in ("anon", "authenticated")' in source
    assert "REVOKE ALL ON TABLE" in source


def test_excel_seed_counts_and_provenance_are_explicit() -> None:
    """El seed dice de que celda sale cada dato, y no siembra nada sin fuente.

    El unico valor que no estaba en la tabla maestra es el factor de «Con
    ilustracion»: ``O22`` esta vacia. No se inventa, se toma de ``E24``, donde
    el propio libro lo aplica, y el seed deja constancia de ello.
    """
    source = content()
    assert "SEED_TECHNIQUES" in source
    assert "SEED_ADDITIONALS" in source
    assert "SEED_OTHER_COSTS" in source
    assert "E24" in source and "O22" in source
