"""Seguridad de la migracion 0009 (cache y metricas de identidad)."""

from pathlib import Path

MIGRATION = Path(__file__).parents[2] / "alembic" / "versions" / "0009_identity_lookups.py"

TABLAS = (
    "identity_lookup_cache",
    "identity_lookup_provider_metrics",
    "identity_lookup_daily_stats",
    "identity_lookup_audit_events",
)


def content() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_todas_las_tablas_nuevas_habilitan_rls_sin_policies_publicas() -> None:
    source = content()
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "CREATE POLICY" not in source.upper()
    assert "FORCE ROW LEVEL SECURITY" not in source


def test_se_revocan_los_roles_publicos_de_postgrest() -> None:
    source = content()
    assert 'for role in ("anon", "authenticated")' in source
    assert "REVOKE ALL ON TABLE" in source


def test_las_cuatro_tablas_estan_presentes() -> None:
    source = content()
    for tabla in TABLAS:
        assert tabla in source, tabla


def test_no_hay_columna_con_el_documento_en_claro() -> None:
    """La cache indexa por hash, nunca guarda el DNI o el RUC legible."""
    source = content()
    assert "document_hash" in source
    assert '"document_number"' not in source


def test_no_siembra_ningun_dato() -> None:
    """A diferencia de 0007/0008, no hay catalogo oficial que transcribir aqui."""
    source = content()
    assert "bulk_insert" not in source


def test_la_cache_tiene_expiracion() -> None:
    source = content()
    assert "expires_at" in source
