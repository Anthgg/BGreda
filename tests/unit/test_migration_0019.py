"""Contrato de la migracion 0019: eje de pago de la cotizacion (Fase 009H)."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATION_FILE = (
    Path(__file__).parents[2] / "alembic" / "versions" / "0019_quotation_payment_status.py"
)


def test_migration_0019_file_and_revisions() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "0019"' in content
    assert 'down_revision: str | None = "0018"' in content


def test_migration_0019_agrega_las_dos_columnas_nullable() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    upgrade = content.split("def upgrade()")[1].split("def downgrade()")[0]
    assert "payment_status" in upgrade
    assert "paid_at" in upgrade
    assert "sa.DateTime(timezone=True)" in upgrade
    assert upgrade.count("nullable=True") == 2


def test_migration_0019_pone_el_default_en_un_paso_aparte() -> None:
    """La trampa central de esta migracion, escrita como prueba.

    `ADD COLUMN ... DEFAULT 'UNPAID'` en una sola sentencia haria que las filas
    YA EXISTENTES leyeran 'UNPAID': PostgreSQL aplica el default a lo guardado.
    Serian 347 cotizaciones afirmando que no se han cobrado cuando de casi
    todas no sabemos nada.

    Por eso la columna se crea sin default y el default se pone despues con un
    `alter_column`, que solo alcanza a los INSERT futuros.
    """
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    upgrade = content.split("def upgrade()")[1].split("def downgrade()")[0]

    creacion = upgrade.split("op.alter_column")[0]
    assert "server_default" not in creacion, (
        "la columna NO puede nacer con default: rellenaria las filas historicas"
    )
    assert "op.alter_column" in upgrade
    assert "'UNPAID'" in upgrade.split("op.alter_column")[1].split(")")[0]


def test_migration_0019_impone_los_tres_estados_coherentes() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    coherencia = content.split("COHERENCIA = ")[1].split('"""')[1]
    # Sin registro: ni estado ni fecha.
    assert "payment_status IS NULL AND paid_at IS NULL" in coherencia
    # Impaga: no puede traer fecha de cobro.
    assert "payment_status = 'UNPAID' AND paid_at IS NULL" in coherencia
    # Pagada: no puede faltarle el cuando.
    assert "payment_status = 'PAID' AND paid_at IS NOT NULL" in coherencia


def test_migration_0019_no_hace_backfill() -> None:
    """HISTORICAL_PAYMENT_BACKFILL: NONE.

    Antes de 009H el sistema no registraba pagos. Escribir UNPAID en lo viejo
    afirmaria que esas cotizaciones no se cobraron, y eso no se sabe.
    """
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    upgrade = content.split("def upgrade()")[1].split("def downgrade()")[0]
    codigo = "\n".join(linea for linea in upgrade.splitlines() if not linea.strip().startswith("#"))
    assert "op.execute" not in codigo
    assert "UPDATE" not in codigo


def test_migration_0019_no_toca_el_estado_comercial() -> None:
    """`status` sigue con tres valores. Pagar no es un cuarto estado.

    Se comprueba que no se toca el CHECK del estado comercial ni aparece
    PAGADA como valor de `status`. Ojo: `payment_status_allowed` contiene
    «status_allowed» como subcadena, asi que hay que nombrar la restriccion
    entera y no un trozo.
    """
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    upgrade = content.split("def upgrade()")[1].split("def downgrade()")[0]
    for prohibido in (
        "ck_quotations_status_allowed",
        "drop_column",
        "drop_constraint",
        "PAGADA",
        "CONFIRMED",
    ):
        assert prohibido not in upgrade, f"0019 no debe tocar {prohibido}"


def test_migration_0019_downgrade_quita_lo_suyo_y_nada_mas() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    downgrade = content.split("def downgrade()")[1]
    assert 'op.drop_column("quotations", "paid_at")' in downgrade
    assert 'op.drop_column("quotations", "payment_status")' in downgrade
    assert downgrade.index("drop_constraint") < downgrade.index("drop_column")
    for ajeno in ("validity_days_snapshot", "exchange_rate_snapshot", "confirmed_at"):
        assert ajeno not in downgrade, f"downgrade no debe tocar {ajeno}"


def test_alembic_single_head_is_0019() -> None:
    """Una sola cabeza: dos harian fallar `alembic upgrade head` en el deploy."""
    alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(alembic_cfg)
    assert script.get_heads() == ["0019"]
