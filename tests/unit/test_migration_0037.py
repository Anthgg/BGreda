"""0037 vigilandose a si misma: aditiva, tres origenes y una sola cabeza."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.production import EXACTLY_ONE_ORIGIN

REPO_ROOT = Path(__file__).resolve().parents[2]


def _codigo() -> str:
    archivos = list((REPO_ROOT / "alembic" / "versions").glob("0037_*.py"))
    assert len(archivos) == 1
    return archivos[0].read_text(encoding="utf-8").split('"""', 2)[2]


def _upgrade() -> str:
    return _codigo().split("def upgrade()")[1].split("def downgrade()")[0]


def _downgrade() -> str:
    return _codigo().split("def downgrade()")[1]


def test_0037_sigue_en_una_cadena_de_una_sola_cabeza() -> None:
    """La cabeza se la lleva la migracion mas nueva; esa afirmacion vive en 0038."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1
    assert script.get_revision("0037").down_revision == "0036"


def test_el_upgrade_no_destruye_datos() -> None:
    upgrade = _upgrade()
    for prohibido in ("drop_column", "drop_table", "drop_index", "DELETE ", "UPDATE "):
        assert prohibido not in upgrade, prohibido


def test_el_unico_drop_del_upgrade_es_el_check_que_se_sustituye() -> None:
    """PostgreSQL no altera un CHECK en su sitio: se suelta y se vuelve a crear.

    Se permite ese drop y ninguno mas, y tiene que ir seguido de la creacion del
    mismo CHECK: sin ella la tabla se quedaria sin regla de origen.
    """
    upgrade = _upgrade()
    assert upgrade.count("drop_constraint") == 1
    suelto = upgrade.index('op.drop_constraint(_CK_ORIGEN, "production_orders", type_="check")')
    creado = upgrade.index('op.create_check_constraint(_CK_ORIGEN, "production_orders"')
    assert suelto < creado


def test_el_tercer_origen_cuelga_del_puente_con_restrict_y_es_unico() -> None:
    upgrade = _upgrade()
    assert '"v2_handoff_id", sa.Integer(), nullable=True' in upgrade
    assert '"v2_production_handoffs"' in upgrade
    assert 'ondelete="RESTRICT"' in upgrade
    assert 'create_unique_constraint(_UQ_PUENTE, "production_orders", ["v2_handoff_id"])' in (
        upgrade
    )


def test_el_check_de_la_migracion_es_el_que_0040_sustituye_y_restaura() -> None:
    """Las tres ramas de 0037 son exactamente las que 0040 reemplaza y devuelve al bajar.

    Hasta 010L esta prueba comparaba con la constante del MODELO, y 0040 la dejo
    en cuatro ramas: la comparacion ya no decia nada de 0037. Lo que importa de
    verdad es la cadena: que bajar de 0040 deje la base como la dejo 0037.
    """
    import importlib.util

    codigo = _codigo()
    # El texto de la constante de tres ramas, reconstruido tal como Python lo une.
    inicio = codigo.index("_ORIGEN_TRES_RAMAS = (")
    fin = codigo.index(")\n\n", inicio)
    trozos = [linea.strip() for linea in codigo[inicio:fin].splitlines()[1:]]
    texto = "".join(trozo.strip('"') for trozo in trozos)

    archivo = next((REPO_ROOT / "alembic" / "versions").glob("0040_*.py"))
    spec = importlib.util.spec_from_file_location("migracion_0040", archivo)
    assert spec is not None and spec.loader is not None
    migracion_0040 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracion_0040)
    assert texto == migracion_0040._ORIGEN_TRES_RAMAS
    # Y el modelo de hoy son esas tres ramas mas la de Solo Quema.
    assert EXACTLY_ONE_ORIGIN.count(" OR ") == 3


def test_cada_rama_del_origen_nombra_los_tres_campos() -> None:
    """Una rama que mirara solo dos campos dejaria pasar el tercero relleno."""
    for rama in EXACTLY_ONE_ORIGIN.split(" OR "):
        for campo in ("quotation_id", "prototype_id", "v2_handoff_id"):
            assert campo in rama, (campo, rama)


def test_el_downgrade_esta_protegido_por_las_ordenes_v2() -> None:
    downgrade = _downgrade()
    assert "RAISE EXCEPTION" in downgrade
    assert "WHERE v2_handoff_id IS NOT NULL" in downgrade
    # Y tambien los consumos: que solo cuelguen de ordenes V2 lo garantiza el
    # servicio, no la base, asi que la guardia no puede darlo por hecho.
    assert "FROM production_consumptions" in downgrade
    # Bloque C: las notas y quemas del seguimiento tambien son historia.
    assert "FROM production_order_notes" in downgrade
    # Bloque D: los avisos registrados tambien.
    assert "FROM production_order_communications" in downgrade
    assert downgrade.index('drop_table("production_order_communications")') < downgrade.index(
        'drop_table("production_order_notes")'
    )
    assert downgrade.index('drop_table("production_order_notes")') < downgrade.index(
        'drop_table("production_consumptions")'
    )
    assert downgrade.index("RAISE EXCEPTION") < downgrade.index("drop_table")
    # Y la guardia va ANTES de tocar nada.
    assert downgrade.index("RAISE EXCEPTION") < downgrade.index("op.drop_constraint")


def _renderizar(*args: str) -> subprocess.CompletedProcess[str]:
    """Alembic en modo `--sql`: sin conexion, ninguna migracion puede consultar."""
    entorno = {**os.environ, "DATABASE_URL": "postgresql://ci:ci@localhost:5432/ci"}
    return subprocess.run(  # noqa: S603 - comando fijo, revisiones literales
        [sys.executable, "-m", "alembic", *args, "--sql"],
        cwd=REPO_ROOT,
        env=entorno,
        capture_output=True,
        text=True,
        check=False,
    )


def test_la_subida_de_0037_se_renderiza_sin_base() -> None:
    resultado = _renderizar("upgrade", "0036:0037")
    assert resultado.returncode == 0, resultado.stderr[-2000:]
    assert "v2_handoff_id" in resultado.stdout
    assert "ck_production_orders_exactly_one_origin" in resultado.stdout


def test_la_bajada_de_0037_se_renderiza_sin_base() -> None:
    """Hallazgo de Copilot en el bloque A: el downgrade no tenia prueba offline.

    La guardia es SQL (`DO $$ ... $$`) y no una consulta desde Python, asi que
    se puede emitir sin conexion: la comprobacion ocurre al ejecutar el script,
    no al generarlo.
    """
    resultado = _renderizar("downgrade", "0037:0036")
    assert resultado.returncode == 0, resultado.stderr[-2000:]
    assert "0037 downgrade bloqueado" in resultado.stdout
    assert "DROP COLUMN v2_handoff_id" in resultado.stdout


def test_el_downgrade_no_deshace_lo_que_es_de_0027() -> None:
    """Aflojar `quotation_id` y `quotation_item_id` lo hizo 0027, y lo deshace 0027.

    Si 0037 los devolviera a NOT NULL, bajar a 0036 romperia las ordenes de
    muestra, que 0036 si admite.
    """
    downgrade = _downgrade()
    assert "alter_column" not in downgrade
    assert "nullable=False" not in downgrade


def test_el_check_de_las_notas_es_el_del_modelo() -> None:
    """Nota o quema: la migracion y el modelo dicen lo mismo."""
    from app.models.production import ProductionOrderNote

    codigo = _codigo()
    inicio = codigo.index("_NOTA_COHERENTE = (")
    fin = codigo.index(")\n\n", inicio)
    trozos = [linea.strip() for linea in codigo[inicio:fin].splitlines()[1:]]
    texto = "".join(trozo.strip('"') for trozo in trozos)
    [check] = [
        c
        for c in ProductionOrderNote.__table__.constraints
        if getattr(c, "name", None) and str(c.name).endswith("kind_fields_consistent")
    ]
    assert texto == str(check.sqltext)  # type: ignore[attr-defined]
