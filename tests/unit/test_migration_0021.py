"""Contrato de la migracion 0021: prototipos (Fase 009K).

Aqui se lee el ARCHIVO. Que la migracion corra de verdad contra PostgreSQL lo
comprueba `tests/db/test_migration_0021_runs.py`; esto fija las decisiones que
se tomaron al escribirla y que un cambio distraido podria deshacer.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).parents[2]
MIGRATION_FILE = REPO_ROOT / "alembic" / "versions" / "0021_prototypes.py"


def _contenido() -> str:
    return MIGRATION_FILE.read_text(encoding="utf-8")


def _bloque(nombre: str) -> str:
    """El texto de una constante multilinea del archivo.

    Se corta por el parentesis de cierre EN SU PROPIA LINEA, no por el primer
    `)` que aparezca: dentro de estas restricciones hay parentesis —`IN (...)`—
    y cortar por el primero devuelve media condicion, que es como una version
    anterior de esta prueba se creyo que faltaba texto que si estaba.
    """
    resto = _contenido().split(f"{nombre} = (", 1)[1]
    return resto.split("\n)", 1)[0]


def _upgrade() -> str:
    return _contenido().split("def upgrade()")[1].split("def downgrade()")[0]


def _downgrade() -> str:
    return _contenido().split("def downgrade()")[1]


def test_migracion_0021_y_su_cadena() -> None:
    contenido = _contenido()
    assert 'revision: str = "0021"' in contenido
    assert 'down_revision: str | None = "0020"' in contenido


def test_0021_sigue_estando_en_el_camino_a_la_cabeza() -> None:
    """0021 se alcanza desde la cabeza actual, sea cual sea.

    La afirmacion de cabeza unica se muda al archivo de la migracion mas
    reciente —ahora 0022—, como se hizo con 0020 cuando entro esta. Lo que hay
    que proteger aqui es que la revision siga en la cadena.
    """
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    assert "0021" in {revision.revision for revision in script.iterate_revisions(heads[0], "base")}


def test_0020_sigue_siendo_alcanzable() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0020") is not None
    assert script.get_revision("0021").down_revision == "0020"


def test_los_checks_de_estado_manejan_el_nulo_explicitamente() -> None:
    """El agujero de 0017 y 0019, cerrado por adelantado otra vez.

    En SQL `NULL = 'CREATED'` no es FALSE sino NULL, y un CHECK que evalua a
    NULL se da por CUMPLIDO. Un OR de ramas con igualdad deja pasar cualquier
    fila cuyo discriminante sea nulo, que es exactamente la forma de esta
    restriccion.

    Hoy `status` es NOT NULL y el guardia es redundante. Se exige igual: la
    correccion de una restriccion no puede depender de que otra clausula, en
    otra parte del esquema, siga estando manana.
    """
    coherencia = _bloque("STATUS_TIMESTAMPS_COHERENT")

    for estado in ("CREATED", "STARTED", "COMPLETED", "CANCELLED"):
        assert f"status IS NOT NULL AND status = '{estado}'" in coherencia, estado

    aprobacion = _bloque("APPROVAL_COHERENT")
    assert "approval IS NOT NULL AND approval = 'PENDING'" in aprobacion
    assert "approval IS NOT NULL AND approval IN ('APPROVED', 'REJECTED')" in aprobacion


def test_la_decision_exige_que_la_muestra_exista() -> None:
    """No se aprueba una pieza que nadie fabrico.

    Sin esta rama se podria dejar APPROVED sobre un prototipo en CREATED, y ese
    registro afirmaria que alguien vio algo que no se hizo.
    """
    aprobacion = _bloque("APPROVAL_COHERENT")
    assert "status IS NOT NULL AND status = 'COMPLETED'" in aprobacion


def test_una_muestra_arrancada_exige_origen_y_almacen() -> None:
    """Lo que hace falta para haber gastado material, escrito en la base.

    El PAGO no cabe aqui —vive en otra tabla y un CHECK no cruza filas— pero de
    que cotizacion y de que almacen si, y son justamente los dos campos que
    pueden faltar mientras la muestra es preliminar.
    """
    origen = _bloque("STARTED_REQUIRES_ORIGIN")
    assert "status IS NULL" in origen, "sin el guardia de nulo la rama se da por cumplida"
    assert "NOT IN ('STARTED', 'COMPLETED')" in origen
    assert "quotation_id IS NOT NULL AND stock_location_id IS NOT NULL" in origen


def test_las_restricciones_ampliadas_solo_anaden() -> None:
    """Un backend anterior a 009K sigue sirviendo sobre este esquema.

    Las dos listas que se reemplazan tienen que conservar TODOS sus valores
    anteriores. Estrechar una restriccion en una migracion aditiva rompe la
    revision que todavia esta sirviendo trafico mientras se despliega.
    """
    antes = _bloque("MOVEMENT_TYPES_BEFORE")
    despues = _bloque("MOVEMENT_TYPES_ALLOWED")
    for tipo in ("INITIAL_IMPORT", "ADJUSTMENT", "PREPARATION_OUT", "PRODUCTION_OUT"):
        assert tipo in antes and tipo in despues, tipo
    assert "PROTOTYPE_OUT" in despues and "PROTOTYPE_OUT" not in antes

    seq_antes = _bloque("SEQUENCE_TYPES_BEFORE")
    seq_despues = _bloque("SEQUENCE_TYPES_ALLOWED")
    for tipo in ("QUOTE", "FIRING", "PREPARATION", "PRODUCTION_ORDER"):
        assert tipo in seq_antes and tipo in seq_despues, tipo
    assert "PROTOTYPE'" in seq_despues


def test_la_semilla_del_correlativo_es_idempotente() -> None:
    """Correr la migracion dos veces no crea dos contadores PRT."""
    upgrade = _upgrade()
    assert "WHERE NOT EXISTS" in upgrade
    assert "'PROTOTYPE', 'PRT'" in upgrade
    assert "'YEARLY'" in upgrade


def test_no_hay_backfill_de_prototipos() -> None:
    """Inventar una muestra que nadie fabrico seria inventar historia."""
    upgrade = _upgrade()
    assert "INSERT INTO prototypes" not in upgrade


def test_el_downgrade_se_niega_en_vez_de_borrar_historia() -> None:
    """Bajar destruiria muestras y dejaria movimientos con un tipo ilegal.

    Ninguna de las dos cosas se arregla despues: un movimiento es historia y
    una decision de aprobacion no se reconstruye. Asi que se comprueba antes y
    se falla en voz alta; borrar para poder bajar cambiaria un problema visible
    por uno silencioso.
    """
    downgrade = _downgrade()
    assert "PROTOTYPE_OUT" in downgrade
    assert "raise RuntimeError" in downgrade
    assert "SELECT count(*) FROM prototypes" in downgrade
    # Y no puede haber ningun borrado de esas dos cosas para «poder bajar».
    assert "DELETE FROM stock_movements" not in downgrade
    assert "DELETE FROM prototypes" not in downgrade


def test_un_prototipo_no_puede_sustituirse_a_si_mismo() -> None:
    """El ciclo mas corto posible, cortado por la base.

    Los ciclos largos no los ve un CHECK de fila: los corta el servicio
    recorriendo la cadena.
    """
    upgrade = _upgrade()
    assert "supersedes_prototype_id <> id" in upgrade
    assert "no_self_supersede" in upgrade


def test_un_prototipo_tiene_como_mucho_un_sucesor() -> None:
    """Sin esto la cadena se bifurca y «cual es la vigente» pasa a ser opinion."""
    assert "uq_prototypes_single_successor" in _upgrade()


def test_un_material_no_puede_repetirse_en_la_misma_muestra() -> None:
    """Dos lineas del mismo insumo se descontarian las dos."""
    assert "uq_prototype_material_product" in _upgrade()
