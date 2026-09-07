"""0024 vigilandose a si misma: que sólo ensanche el origen.

La 0024 arregla un defecto que llego a produccion —una muestra nacida de una
cotizacion de prototipo pagada no podia arrancarse— y lo hace tocando una sola
disyuncion de un CHECK. El riesgo de un arreglo asi no es que no funcione: es
que de paso afloje algo que si tenia que seguir apretado, o que alguien lo
convierta en un backfill.

Estas pruebas leen el ARCHIVO. Que la restriccion nueva se comporte contra una
base real lo comprueban `tests/db/test_prototype_start_origin.py` y las que
suben hasta la cabeza; lo que aqui se protege es que nadie convierta la
migracion en destructiva de un commit a otro sin enterarse.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRACION = REPO_ROOT / "alembic" / "versions"


def _contenido() -> str:
    archivos = list(MIGRACION.glob("0024_*.py"))
    assert len(archivos) == 1, f"Se esperaba una sola 0024, hay {len(archivos)}"
    return archivos[0].read_text(encoding="utf-8")


def _upgrade() -> str:
    return _contenido().split("def upgrade()")[1].split("def downgrade()")[0]


def _downgrade() -> str:
    return _contenido().split("def downgrade()")[1]


def test_el_upgrade_no_toca_ni_una_fila() -> None:
    """NO_BACKFILL.

    La tentacion evidente era copiar `prototype_quotation_id` a `quotation_id`
    y quedarse tranquilo. Seria mentir en la tabla: una muestra de prototipo
    diria colgar de una cotizacion de producto que no existe.
    """
    upgrade = _upgrade()
    for prohibido in ("UPDATE ", "update(", "INSERT ", "DELETE ", "insert(", "delete("):
        assert prohibido not in upgrade, prohibido


def test_el_upgrade_solo_ensancha_el_origen_y_no_afloja_el_almacen() -> None:
    """MINIMAL_CHANGE.

    Se comprueba la expresion entera, no cada mitad: «quotation_id IS NOT NULL»
    es subcadena de «prototype_quotation_id IS NOT NULL», asi que buscarlas por
    separado pasaria incluso sin el arreglo.
    """
    contenido = _contenido()
    assert "(quotation_id IS NOT NULL OR prototype_quotation_id IS NOT NULL)" in contenido, (
        contenido
    )
    # Lo que NO podia relajarse: sin almacen no se sabe de donde salio el
    # material, y eso no depende de quien pago.
    assert "AND stock_location_id IS NOT NULL" in contenido
    # Las otras ramas del CHECK siguen intactas.
    assert "status NOT IN ('STARTED', 'COMPLETED')" in contenido


def test_el_upgrade_no_crea_ni_borra_tablas_ni_columnas() -> None:
    upgrade = _upgrade()
    for prohibido in ("drop_table", "drop_column", "create_table", "add_column", "alter_column"):
        assert prohibido not in upgrade, prohibido


def test_el_nombre_de_la_restriccion_va_desnudo() -> None:
    """NAMING_CONVENTION_TRAP.

    `alembic/env.py` pasa `target_metadata`, cuya convencion es
    `ck_%(table_name)s_%(constraint_name)s`, y Alembic la aplica al nombre que
    se le da. Pasar el nombre completo genera
    `ck_prototypes_ck_prototypes_started_requires_origin` y el DROP falla con
    «constraint does not exist» — que es exactamente lo que paso la primera vez
    que se escribio esta migracion.
    """
    contenido = _contenido()
    assert 'CONSTRAINT = "started_requires_origin"' in contenido
    assert 'CONSTRAINT = "ck_prototypes_started_requires_origin"' not in contenido


def test_el_downgrade_aborta_si_hay_muestras_de_cpr_arrancadas() -> None:
    """DOWNGRADE_GUARD.

    Volver a la restriccion estrecha con esas filas dentro dejaria la tabla en
    un estado que ella misma prohibe. Abortar diciendo cuantas son es mejor que
    un error de PostgreSQL que no explica nada.
    """
    downgrade = _downgrade()
    assert "RAISE EXCEPTION" in downgrade
    assert "prototype_quotation_id IS NOT NULL" in downgrade
    assert "quotation_id IS NULL" in downgrade
    assert "status IN ('STARTED', 'COMPLETED')" in downgrade
    # Y tampoco el downgrade toca filas: sólo mira y decide.
    for prohibido in ("UPDATE prototypes SET", "DELETE FROM prototypes"):
        assert prohibido not in downgrade, prohibido


# La afirmacion de «una sola cabeza» vivia aqui y se mudo a
# `test_migration_0025.py`: siempre acompana a la ultima revision.


def test_la_cadena_no_se_rompe() -> None:
    """0024 cuelga de 0023, y 0023 sigue existiendo."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0023") is not None
    assert script.get_revision("0024").down_revision == "0023"
