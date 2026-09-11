"""0028 vigilandose a si misma: que sea aditiva y que lo siga siendo.

El despliegue es DB primero. Entre que la base llega a 0028 y el backend nuevo
recibe trafico, la revision anterior sigue sirviendo: crea cotizaciones Legacy
sin saber que existe una columna de motor. Anadirla con `server_default` no la
molesta; quitarle o renombrarle cualquier cosa la tumbaria en esa ventana.

Estas pruebas leen el ARCHIVO. El efecto sobre una base real lo comprueba
`tests/db/test_migration_0028_runs.py`; lo que aqui se protege es que nadie
convierta la migracion en destructiva —o en un backfill de negocio— de un
commit a otro sin enterarse.

El backfill es la tentacion concreta de esta fase: «ya que sellamos las filas
como LEGACY, aprovechemos para normalizar tal snapshot». Sellar es escribir lo
que siempre fue verdad; tocar un importe historico es recalcular una cotizacion
que alguien ya envio.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRACION = REPO_ROOT / "alembic" / "versions"


def _contenido() -> str:
    archivos = list(MIGRACION.glob("0028_*.py"))
    assert len(archivos) == 1, f"Se esperaba una sola 0028, hay {len(archivos)}"
    return archivos[0].read_text(encoding="utf-8")


def _upgrade() -> str:
    return _contenido().split("def upgrade()")[1].split("def downgrade()")[0]


def _downgrade() -> str:
    return _contenido().split("def downgrade()")[1]


def test_el_upgrade_no_destruye_nada() -> None:
    """Ni una columna menos, ni una tabla menos, ni un renombrado."""
    upgrade = _upgrade()
    for prohibido in ("drop_column", "drop_table", "drop_index"):
        assert prohibido not in upgrade, prohibido
    assert "rename" not in upgrade.lower()


def test_el_upgrade_solo_anade_una_columna_a_la_tabla_historica() -> None:
    upgrade = _upgrade()
    assert upgrade.count("add_column") == 1
    assert "pricing_engine_version" in upgrade


def test_la_columna_nueva_llega_con_valor_por_defecto() -> None:
    """Sin `server_default`, un NOT NULL sobre una tabla con filas no entra.

    Y con el, PostgreSQL 11+ ni siquiera reescribe la tabla: el valor se
    resuelve desde el catalogo. El sello no toca fisicamente los historicos.
    """
    upgrade = _upgrade()
    bloque = upgrade.split("add_column")[1].split(")")[0] + upgrade.split("add_column")[1][:400]
    assert "server_default" in bloque
    assert "'LEGACY'" in bloque


def test_el_upgrade_no_escribe_sobre_datos_de_negocio() -> None:
    """Ni UPDATE, ni backfill, ni recalculo. Solo el INSERT del talonario."""
    upgrade = _upgrade()
    mayusculas = upgrade.upper()
    # Formas completas: `ondelete="RESTRICT"` y `updated_at` contienen las
    # palabras sueltas y no son escrituras sobre datos.
    assert "UPDATE " not in mayusculas
    assert "DELETE FROM" not in mayusculas
    # El unico INSERT admitido es el del talonario, y es idempotente.
    assert mayusculas.count("INSERT INTO") == 1
    assert "document_sequences" in upgrade
    assert "WHERE NOT EXISTS" in upgrade


def test_el_check_de_secuencias_solo_se_amplia() -> None:
    """Acepta lo que aceptaba, mas el tipo nuevo. Nunca menos."""
    contenido = _contenido()
    assert "SEQUENCE_TYPES_AFTER = f\"{SEQUENCE_TYPES_BEFORE}, 'QUOTE_V2'\"" in contenido


def test_las_dos_tablas_declaran_su_motor() -> None:
    """La frontera vive en la base, no en la buena fe del codigo."""
    upgrade = _upgrade()
    assert "pricing_engine_version = 'LEGACY'" in upgrade
    assert "pricing_engine_version = 'V2'" in upgrade


def test_el_downgrade_se_niega_antes_de_tocar_el_esquema() -> None:
    """La guarda va primero; si no, fallaria a medias y sin explicar por que."""
    downgrade = _downgrade()
    assert "RuntimeError" in downgrade
    assert downgrade.index("RuntimeError") < downgrade.index("drop_table")


def test_el_downgrade_no_borra_el_registro_de_correlativos_emitidos() -> None:
    """`document_sequence_issues` es inmutable: un numero entregado no se desmiente.

    En vez de borrarlo, el downgrade se niega cuando existe.
    """
    downgrade = _downgrade()
    assert "DELETE FROM document_sequence_issues" not in downgrade
    assert "document_sequence_issues" in downgrade  # se consulta para bloquear


def test_el_downgrade_deshace_exactamente_lo_que_anadio() -> None:
    downgrade = _downgrade()
    assert downgrade.count("drop_column") == 1
    assert downgrade.count("drop_table") == 1
    assert "DELETE FROM document_sequences" in downgrade


def test_alembic_una_sola_cabeza_y_es_0028() -> None:
    """Una sola cabeza, y es la nueva.

    Esta afirmacion acompana siempre a la ultima revision y se retira de la
    anterior: fijarla en una concreta obliga a reescribir la prueba vieja cada
    fase, y entonces deja de comprobar nada.
    """
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_heads() == ["0028"], script.get_heads()


def test_la_cadena_no_se_rompe() -> None:
    """0028 cuelga de 0027, y 0027 sigue existiendo."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0027") is not None
    assert script.get_revision("0028").down_revision == "0027"
