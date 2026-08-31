"""Contrato de la migracion 0017: moneda de emision y tipo de cambio (Fase 009F)."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATION_FILE = (
    Path(__file__).parents[2] / "alembic" / "versions" / "0017_quotation_currency_exchange_rate.py"
)


def test_migration_0017_file_and_revisions() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "0017"' in content
    assert 'down_revision: str | None = "0016"' in content


def test_migration_0017_upgrade_contract() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert "def upgrade() -> None:" in content
    assert "def downgrade() -> None:" in content
    assert "exchange_rate_snapshot" in content
    assert "exchange_rate_source_snapshot" in content
    # La escala decide si una tasa como 3.756789 se guarda entera o truncada.
    assert "sa.Numeric(18, 6)" in content


def test_migration_0017_restringe_las_monedas() -> None:
    """009F autoriza dos monedas, no un mercado.

    Sin este CHECK, el de coherencia dejaria pasar cualquier moneda que
    trajera tasa, porque su rama negativa solo dice «distinto de PEN».
    """
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert "currency_code_snapshot IN ('PEN', 'USD')" in content


def test_migration_0017_impone_los_dos_estados_validos() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    coherencia = content.split("COHERENCIA = ")[1].split('"""')[1]
    # PEN: sin tasa y sin fuente. Guardar un 1 describiria una conversion que
    # nunca ocurrio.
    assert "currency_code_snapshot = 'PEN'" in coherencia
    assert "exchange_rate_snapshot IS NULL" in coherencia
    assert "exchange_rate_source_snapshot IS NULL" in coherencia
    # USD: tasa positiva y fuente manual, que es la unica de 009F.
    assert "currency_code_snapshot = 'USD'" in coherencia
    assert "exchange_rate_snapshot > 0" in coherencia
    assert "exchange_rate_source_snapshot = 'MANUAL'" in coherencia
    # Sin este IS NOT NULL, `NULL = 'MANUAL'` evalua a NULL en vez de FALSE y
    # el CHECK se da por cumplido: una fila USD con tasa y sin fuente entraba.
    assert "exchange_rate_source_snapshot IS NOT NULL" in coherencia


def test_migration_0017_no_toca_la_moneda_existente() -> None:
    """`currency_code_snapshot` y su simbolo son de 0015 y no se modifican."""
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    upgrade = content.split("def upgrade()")[1].split("def downgrade()")[0]
    for prohibido in ("alter_column", "drop_column", "currency_catalog"):
        assert prohibido not in upgrade, f"0017 no debe usar {prohibido}"


def test_migration_0017_no_hace_backfill() -> None:
    """Las filas historicas ya cumplen el contrato: un UPDATE seria ruido.

    Son 346 cotizaciones en PEN sin tasa, que es exactamente el estado que el
    CHECK pide de ellas. Reescribirlas solo ensuciaria `updated_at`.
    """
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    upgrade = content.split("def upgrade()")[1].split("def downgrade()")[0]
    # Solo lineas de codigo: el comentario que explica por que NO hay backfill
    # contiene la palabra UPDATE y no debe hacer fallar la prueba.
    codigo = "\n".join(linea for linea in upgrade.splitlines() if not linea.strip().startswith("#"))
    assert "op.execute" not in codigo
    assert "UPDATE" not in codigo


def test_migration_0017_downgrade_undoes_everything() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    downgrade = content.split("def downgrade()")[1]
    assert 'op.drop_column("quotations", "exchange_rate_source_snapshot")' in downgrade
    assert 'op.drop_column("quotations", "exchange_rate_snapshot")' in downgrade
    for constraint in (
        "ck_quotations_exchange_rate_coherence",
        "ck_quotations_exchange_rate_positive",
        "ck_quotations_currency_supported",
    ):
        assert constraint in downgrade, f"downgrade no elimina {constraint}"
    # La moneda es de 0015: bajar 0017 no puede llevarsela por delante.
    assert "currency_code_snapshot" not in downgrade


def test_alembic_single_head_is_0017() -> None:
    """Una sola cabeza: dos harian fallar `alembic upgrade head` en el deploy."""
    alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(alembic_cfg)
    assert script.get_heads() == ["0017"]
