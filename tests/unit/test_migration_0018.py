"""Contrato de la migracion 0018: vigencia comercial congelada (Fase 009G)."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATION_FILE = (
    Path(__file__).parents[2] / "alembic" / "versions" / "0018_quotation_validity_days_snapshot.py"
)


def test_migration_0018_file_and_revisions() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "0018"' in content
    assert 'down_revision: str | None = "0017"' in content


def test_migration_0018_agrega_la_columna_nullable() -> None:
    """Nullable no es un descuido: distingue «no se capturo» de un plazo real."""
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    upgrade = content.split("def upgrade()")[1].split("def downgrade()")[0]
    assert "validity_days_snapshot" in upgrade
    assert "sa.Integer()" in upgrade
    assert "nullable=True" in upgrade


def test_migration_0018_limita_el_rango_como_su_origen() -> None:
    """El snapshot no puede admitir plazos que la configuracion rechaza.

    `commercial_settings.quote_validity_days` acepta de 1 a 3650 desde la 0002.
    Una copia mas permisiva dejaria entrar por la puerta de atras un plazo que
    la pantalla de configuracion nunca habria dejado escribir.
    """
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    rango = content.split("RANGO = ")[1].split('"""')[1]
    assert "validity_days_snapshot IS NULL" in rango
    assert "validity_days_snapshot > 0" in rango
    assert "validity_days_snapshot <= 3650" in rango


def test_migration_0018_no_hace_backfill() -> None:
    """La prohibicion central de 009G, escrita como prueba.

    Rellenar las confirmadas antiguas con el valor que la configuracion tenga
    hoy no recuperaria su vigencia: le pondria a un documento ya entregado un
    plazo inventado que despues nadie sabria distinguir de uno acordado.

    Que exista rastro en `audit_events` no cambia esta prueba. Si algun dia se
    autoriza rellenarlas, sera desde ahi y con una migracion propia, no
    colandolo en esta.
    """
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    upgrade = content.split("def upgrade()")[1].split("def downgrade()")[0]
    # Solo codigo: el comentario que explica por que NO hay backfill nombra
    # UPDATE, y no debe hacer fallar la prueba.
    codigo = "\n".join(linea for linea in upgrade.splitlines() if not linea.strip().startswith("#"))
    assert "op.execute" not in codigo
    assert "UPDATE" not in codigo
    assert "quote_validity_days" not in codigo


def test_migration_0018_no_toca_nada_mas() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    upgrade = content.split("def upgrade()")[1].split("def downgrade()")[0]
    for prohibido in ("alter_column", "drop_column", "drop_table", "commercial_settings"):
        assert prohibido not in upgrade, f"0018 no debe usar {prohibido}"


def test_migration_0018_downgrade_quita_el_check_antes_que_la_columna() -> None:
    """El orden importa: PostgreSQL no deja soltar una columna con CHECK vivo."""
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    downgrade = content.split("def downgrade()")[1]
    assert "ck_quotations_validity_days_snapshot_range" in downgrade
    assert 'op.drop_column("quotations", "validity_days_snapshot")' in downgrade
    assert downgrade.index("drop_constraint") < downgrade.index("drop_column")
    # Bajar 0018 no puede llevarse por delante lo que congelo 009F.
    for ajeno in ("exchange_rate_snapshot", "currency_code_snapshot", "confirmed_at"):
        assert ajeno not in downgrade, f"downgrade no debe tocar {ajeno}"


def test_alembic_single_head_is_0018() -> None:
    """Una sola cabeza: dos harian fallar `alembic upgrade head` en el deploy."""
    alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(alembic_cfg)
    assert script.get_heads() == ["0018"]
