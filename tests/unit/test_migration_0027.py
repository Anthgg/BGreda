"""0027 vigilandose a si misma: lo que relaja y lo que pone en su lugar.

El despliegue es DB primero. Entre que la base llega a 0027 y el backend nuevo
recibe trafico, la revision anterior sigue sirviendo: crea ordenes de
produccion con `quotation_id` y lineas con `quotation_item_id`, sin saber que
existe un segundo origen. Relajar dos NOT NULL no la molesta —sigue
escribiendo los dos campos—, pero cualquier cosa que 0027 quitara, renombrara o
volviera obligatoria la tumbaria en esa ventana.

Estas pruebas leen el ARCHIVO. El efecto sobre una base real lo comprueba
`tests/db/test_migration_0027_runs.py`; lo que aqui se protege es que nadie
convierta la migracion en destructiva —o en un backfill— de un commit a otro
sin enterarse.

El backfill es la tentacion concreta de esta fase: crearle una orden a cada una
de las once muestras existentes parece dejar la casa ordenada, y seria fabricar
un documento para un hecho que ya ocurrio sin el —el de PRT-2026-000009— y
elegirle un almacen que nadie eligio.
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRACION = REPO_ROOT / "alembic" / "versions"

#: Las dos columnas que dejan de ser obligatorias, y ninguna mas.
RELAJADAS = {
    "production_orders": "quotation_id",
    "production_order_lines": "quotation_item_id",
}


def _contenido() -> str:
    archivos = list(MIGRACION.glob("0027_*.py"))
    assert len(archivos) == 1, f"Se esperaba una sola 0027, hay {len(archivos)}"
    return archivos[0].read_text(encoding="utf-8")


def _upgrade() -> str:
    return _contenido().split("def upgrade()")[1].split("def downgrade()")[0]


def test_el_upgrade_solo_anade_una_columna() -> None:
    """EXPECTED_0027_NEW_COLUMNS: 1. MIGRATION_0027_DROP_COLUMN_COUNT: 0."""
    upgrade = _upgrade()
    assert upgrade.count("add_column") == 1
    assert "prototype_id" in upgrade
    for prohibido in ("drop_column", "drop_table", "create_table", "drop_constraint"):
        assert prohibido not in upgrade, prohibido
    assert "rename" not in upgrade.lower()


def test_solo_se_relajan_las_dos_columnas_autorizadas() -> None:
    """Y el almacen NO es una de ellas.

    Una orden que no sabe de que almacen sale su material no es una orden. De
    donde viene ese almacen para una muestra es decision de quien cobra, no de
    esta migracion.
    """
    upgrade = _upgrade()
    assert upgrade.count("alter_column") == 2
    assert upgrade.count("nullable=True") == 3  # las dos relajadas y la nueva
    assert "nullable=False" not in upgrade
    for tabla, columna in RELAJADAS.items():
        assert tabla in upgrade
        assert columna in upgrade
    assert "stock_location_id" not in upgrade


def test_lo_que_se_pierde_en_not_null_se_recupera_en_el_check() -> None:
    """Una orden sigue teniendo exactamente un origen, y lo dice la BASE.

    Es el canje entero de esta revision: se aflojan dos obligatoriedades a
    cambio de una regla mas exacta que las dos juntas. Sin el CHECK, la
    migracion seria una perdida neta de garantias.
    """
    assert _upgrade().count("create_check_constraint") == 1
    # La condicion vive en una constante del modulo, al lado de la explicacion
    # de por que se escribe aqui y no se importa del modelo.
    contenido = _contenido()
    assert "quotation_id IS NOT NULL AND prototype_id IS NULL" in contenido
    assert "quotation_id IS NULL AND prototype_id IS NOT NULL" in contenido


def test_la_muestra_no_admite_dos_ordenes() -> None:
    """El UNIQUE es lo unico que para dos cobros simultaneos.

    Comprobarlo en el servicio no basta: las dos peticiones pasan la lectura
    previa antes de que ninguna haya insertado, y la segunda orden estaria
    dispuesta a gastar el barro entero por segunda vez.
    """
    upgrade = _upgrade()
    assert upgrade.count("create_unique_constraint") == 1
    assert upgrade.count("create_foreign_key") == 1
    # RESTRICT y no CASCADE: borrar una muestra no puede llevarse por delante
    # el documento que registra que se fabrico.
    assert 'ondelete="RESTRICT"' in upgrade


def test_el_upgrade_no_crea_ni_una_orden_ni_toca_una_fila() -> None:
    """MIGRATION_0027_BACKFILL_COUNT: 0. DATA_DELETE_COUNT: 0."""
    upgrade = _upgrade()
    for prohibido in ("UPDATE ", "update(", "INSERT ", "insert(", "DELETE ", "delete("):
        assert prohibido not in upgrade, prohibido


def test_el_check_va_desnudo_y_las_demas_completas() -> None:
    """La trampa que costo un despliegue en 0024, con su matiz.

    La convencion de `app/db/base.py` incorpora el nombre dado SOLO en los
    CHECK (`ck_%(table_name)s_%(constraint_name)s`); las de clave ajena y
    unicidad se componen de tabla y columna y no leen el nombre. Asi que el
    CHECK va desnudo y las otras dos van completas y ya conformes.
    """
    contenido = _contenido()
    literales = re.findall(r"[\"']([^\"'\n]*)[\"']", contenido)
    assert [texto for texto in literales if texto.startswith("ck_")] == []
    assert "fk_production_orders_prototype_id_prototypes" in contenido
    assert "uq_production_orders_prototype_id" in contenido


def test_el_downgrade_se_niega_antes_de_intentarlo() -> None:
    """DOWNGRADE_WITH_PROTOTYPE_ORDERS_REJECTED.

    Sin la guarda, el downgrade fallaria igual —contra el NOT NULL que intenta
    restaurar— pero a mitad de camino y con un mensaje que no explica por que.
    Se aborta antes, diciendo cuantas ordenes lo impiden.
    """
    downgrade = _contenido().split("def downgrade()")[1]
    assert "RAISE EXCEPTION" in downgrade
    assert "0027 downgrade bloqueado" in downgrade
    # La comprobacion va ANTES de tocar el esquema.
    assert downgrade.index("RAISE EXCEPTION") < downgrade.index("drop_constraint")


def test_el_downgrade_deshace_exactamente_lo_que_anadio() -> None:
    """La columna fuera, sus dos restricciones fuera, y el CHECK fuera."""
    downgrade = _contenido().split("def downgrade()")[1]
    assert downgrade.count("drop_column") == 1
    assert downgrade.count("drop_constraint") == 3
    assert downgrade.count("alter_column") == 2
    assert downgrade.count("nullable=False") == 2


def test_la_cadena_no_se_rompe() -> None:
    """0027 cuelga de 0026, y 0026 sigue existiendo."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0026") is not None
    assert script.get_revision("0027").down_revision == "0026"
