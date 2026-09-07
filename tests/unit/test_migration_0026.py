"""0026 vigilandose a si misma: que solo anada, y solo cuatro columnas.

El despliegue es DB primero. Entre que la base llega a 0026 y el backend nuevo
recibe trafico, la revision anterior sigue sirviendo: escribe cotizaciones sin
saber que existen ni la bandera del factor ni el modo de horno. Cualquier cosa
que 0026 quite, renombre o vuelva obligatoria la tumbaria en esa ventana.

Estas pruebas leen el ARCHIVO. El efecto sobre una base real lo comprueba
`tests/db/test_migration_0026_runs.py`; lo que aqui se protege es que nadie
convierta la migracion en destructiva —o en un backfill— de un commit a otro
sin enterarse.

El backfill es la tentacion concreta de esta fase: rellenar
`production_factor_enabled` mirando el factor de cada cotizacion vieja parece
un favor, y seria escribir una decision que nadie tomo sobre documentos que
alguien ya firmo. La lectura de ese NULL vive en el codigo, donde se puede
cambiar de opinion; escrita en la base ya no se distingue de una eleccion.
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRACION = REPO_ROOT / "alembic" / "versions"

#: Lo que 0026 anade, por tabla. Cuatro, no cinco.
COLUMNAS_NUEVAS = {
    "quotations": ("production_factor_enabled", "kiln_mode"),
    "commercial_settings": ("production_factor_enabled_default", "kiln_mode_default"),
}


def _contenido() -> str:
    archivos = list(MIGRACION.glob("0026_*.py"))
    assert len(archivos) == 1, f"Se esperaba una sola 0026, hay {len(archivos)}"
    return archivos[0].read_text(encoding="utf-8")


def _upgrade() -> str:
    return _contenido().split("def upgrade()")[1].split("def downgrade()")[0]


def test_el_upgrade_solo_anade_cuatro_columnas() -> None:
    """EXPECTED_0026_NEW_COLUMNS: 4. MIGRATION_0026_DROP_COLUMN_COUNT: 0."""
    upgrade = _upgrade()
    assert upgrade.count("add_column") == 4
    for prohibido in ("drop_column", "drop_table", "create_table", "alter_column"):
        assert prohibido not in upgrade, prohibido
    assert "rename" not in upgrade.lower()


def test_las_cuatro_columnas_nacen_anulables() -> None:
    """Anulables porque NULL tiene lectura, no porque falte decidir.

    En `kiln_mode`, NULL es `TOGETHER`: lo que el motor hacia. En
    `production_factor_enabled`, NULL manda a mirar el factor que la propia
    cotizacion guardo. Obligatorias, la migracion tendria que inventarles un
    valor a diecisiete anos de historia.
    """
    upgrade = _upgrade()
    assert upgrade.count("nullable=True") == 4
    assert "nullable=False" not in upgrade


def test_el_upgrade_no_reinterpreta_ninguna_cotizacion() -> None:
    """MIGRATION_0026_UPDATE_COUNT: 0. INSERT: 0. DELETE: 0."""
    upgrade = _upgrade()
    for prohibido in ("UPDATE ", "update(", "INSERT ", "insert(", "DELETE ", "delete("):
        assert prohibido not in upgrade, prohibido


def test_no_se_toca_ningun_check_de_factor() -> None:
    """Los CHECK `> 0` siguen siendo ciertos y siguen en su sitio.

    Lo que cambia en 009K.3 es que se puede NO aplicar el factor, no que el
    factor pueda valer cero. Cero lo rechazan `price_line`, el CHECK de
    configuracion y el esquema de entrada, y esta fase no relaja ninguno de
    los tres: apagado se dice con una bandera.
    """
    upgrade = _upgrade()
    assert "production_factor_default" not in upgrade
    assert "commercial_factor" not in upgrade
    # El upgrade no retira ni recrea ninguna restriccion: solo crea las suyas.
    assert "drop_constraint" not in upgrade


def test_los_nombres_de_restriccion_van_desnudos() -> None:
    """La trampa que costo un despliegue en 0024.

    `alembic/env.py` entrega un `target_metadata` cuya convencion es
    `ck_%(table_name)s_%(constraint_name)s`, y `create_check_constraint` la
    APLICA. Pasar el nombre completo produce `ck_quotations_ck_quotations_...`
    y el `drop_constraint` del downgrade no encuentra nada.
    """
    contenido = _contenido()
    literales = re.findall(r"[\"']([^\"'\n]*)[\"']", contenido)
    con_prefijo = [texto for texto in literales if texto.startswith("ck_")]
    assert con_prefijo == [], con_prefijo


def test_el_modo_de_horno_no_admite_un_tercer_valor() -> None:
    """Sin CHECK, un modo invalido no fallaria: elegiria TOGETHER en silencio."""
    assert _upgrade().count("create_check_constraint") == 2
    assert "'TOGETHER', 'PER_PRODUCT'" in _contenido()


def test_alembic_una_sola_cabeza_y_es_0026() -> None:
    """Una sola cabeza, y es la nueva.

    Esta afirmacion acompana siempre a la ultima revision: fijarla en una
    concreta obliga a reescribir la prueba anterior cada vez, y entonces deja
    de comprobar nada.
    """
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_heads() == ["0026"], script.get_heads()


def test_la_cadena_no_se_rompe() -> None:
    """0026 cuelga de 0025, y 0025 sigue existiendo."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0025") is not None
    assert script.get_revision("0026").down_revision == "0025"


def test_el_downgrade_deshace_exactamente_lo_que_anadio() -> None:
    """Cuatro columnas fuera y las dos restricciones que nacieron con ellas."""
    downgrade = _contenido().split("def downgrade()")[1]
    assert downgrade.count("drop_column") == 4
    assert downgrade.count("drop_constraint") == 2
    for tabla, columnas in COLUMNAS_NUEVAS.items():
        assert tabla in downgrade
        for columna in columnas:
            assert columna in downgrade, columna
