"""0040 vigilandose a si misma: aditiva, dos talonarios, cuatro origenes y una cabeza."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.firing_quotation_v2 import V2FiringProductionHandoff
from app.models.kiln_batches import (
    ASSIGNMENT_RELEASE_COHERENT,
    ASSIGNMENT_SOURCE_COHERENT,
    KILN_BATCH_GUARD_FUNCTION,
    KILN_BATCH_GUARD_TRIGGER,
    KILN_BATCH_STATUS_TIMESTAMPS,
    KILN_BATCH_VOLUME_FUNCTION,
    KILN_BATCH_VOLUME_TRIGGER,
    InternalLoad,
    InternalLoadLine,
    KilnBatch,
    KilnBatchAssignment,
    KilnBatchOperation,
)
from app.models.production import EXACTLY_ONE_ORIGIN, ProductionOrder
from app.models.sequence import SequenceType

REPO_ROOT = Path(__file__).resolve().parents[2]


def _modulo() -> object:
    import importlib.util

    archivos = list((REPO_ROOT / "alembic" / "versions").glob("0040_*.py"))
    assert len(archivos) == 1
    spec = importlib.util.spec_from_file_location("migracion_0040", archivos[0])
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _codigo() -> str:
    archivos = list((REPO_ROOT / "alembic" / "versions").glob("0040_*.py"))
    assert len(archivos) == 1
    return archivos[0].read_text(encoding="utf-8").split('"""', 2)[2]


def _upgrade() -> str:
    return _codigo().split("def upgrade()")[1].split("def downgrade()")[0]


def _downgrade() -> str:
    return _codigo().split("def downgrade()")[1]


def test_0040_es_la_unica_cabeza_y_cuelga_de_0039() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert list(script.get_heads()) == ["0040"]
    assert script.get_revision("0040").down_revision == "0039"


def test_el_upgrade_no_borra_datos() -> None:
    upgrade = _upgrade()
    for prohibido in ("drop_column", "drop_table", "drop_index", "DELETE "):
        assert prohibido not in upgrade, prohibido
    # Los unicos drop_constraint son los dos CHECK que se sustituyen en su sitio:
    # el de talonarios y el de origen de la orden.
    assert upgrade.count("drop_constraint") == 2


def test_los_dos_talonarios_se_dan_de_alta() -> None:
    upgrade = _upgrade()
    assert '("KILN_BATCH", "HOR")' in upgrade
    assert '("INTERNAL_LOAD", "CI")' in upgrade
    assert SequenceType.KILN_BATCH.value == "KILN_BATCH"
    assert SequenceType.INTERNAL_LOAD.value == "INTERNAL_LOAD"


def test_las_condiciones_de_la_migracion_son_las_del_modelo() -> None:
    """Escritas a mano en la migracion, y tienen que decir LO MISMO que el modelo.

    Si divergieran, la base tendria una regla y el modelo otra, y el siguiente
    autogenerate propondria cambiarla sin que nadie lo hubiera decidido.
    """
    migracion = _modulo()
    assert migracion._ESTADO_FECHAS == KILN_BATCH_STATUS_TIMESTAMPS  # type: ignore[attr-defined]
    assert migracion._ORIGEN_ASIGNACION == ASSIGNMENT_SOURCE_COHERENT  # type: ignore[attr-defined]
    assert migracion._LIBERACION == ASSIGNMENT_RELEASE_COHERENT  # type: ignore[attr-defined]
    assert migracion._ORIGEN_CUATRO_RAMAS == EXACTLY_ONE_ORIGIN  # type: ignore[attr-defined]


def test_cada_rama_del_origen_nombra_los_cuatro_campos() -> None:
    """Una rama que mirara solo tres dejaria pasar una fila con el cuarto relleno."""
    ramas = EXACTLY_ONE_ORIGIN.split(" OR ")
    assert len(ramas) == 4
    for rama in ramas:
        for campo in ("quotation_id", "prototype_id", "v2_handoff_id", "v2_firing_handoff_id"):
            assert campo in rama, (campo, rama)


def test_el_trigger_del_modelo_es_el_de_la_migracion() -> None:
    """Las bases de prueba se crean desde los modelos y no corren migraciones.

    Si el trigger del modelo y el de 0040 divergieran, las pruebas del servicio
    pasarian contra una base que no se comporta como la de verdad.
    """
    migracion = _modulo()
    assert KILN_BATCH_VOLUME_FUNCTION == migracion._FUNCION_VOLUMEN  # type: ignore[attr-defined]
    assert KILN_BATCH_VOLUME_TRIGGER == migracion._TRIGGER_VOLUMEN  # type: ignore[attr-defined]
    assert KILN_BATCH_GUARD_FUNCTION == migracion._GUARDA_FUNCION  # type: ignore[attr-defined]
    assert KILN_BATCH_GUARD_TRIGGER == migracion._GUARDA_TRIGGER  # type: ignore[attr-defined]


def test_la_guarda_distingue_el_trigger_de_una_escritura_directa() -> None:
    """Sin la guarda, un UPDATE directo al contador dejaria la suma falsa (Codex, L1)."""
    codigo = _codigo()
    assert "CREATE TRIGGER trg_kiln_batches_guard_assigned_volume" in codigo
    assert "BEFORE INSERT OR UPDATE ON kiln_batches" in codigo
    assert "pg_trigger_depth() > 1" in codigo


def test_el_trigger_de_volumen_aplica_el_delta_y_no_una_suma() -> None:
    """Delta: bajo READ COMMITTED una suma leida en una foto vieja perderia la del otro."""
    upgrade = _codigo()
    assert "CREATE TRIGGER trg_kiln_batch_assignments_volume" in upgrade
    assert "AFTER INSERT OR UPDATE OR DELETE ON kiln_batch_assignments" in upgrade
    assert "assigned_volume_cm3 + (nuevo - viejo)" in upgrade
    assert "SUM(" not in upgrade.upper().split("CREATE FUNCTION")[1].split("$$;")[0]


def test_el_downgrade_se_niega_con_datos() -> None:
    downgrade = _downgrade()
    assert "RAISE EXCEPTION" in downgrade
    assert downgrade.index("RAISE EXCEPTION") < downgrade.index("drop_table")
    # Y quita el trigger antes que las tablas de las que depende.
    assert downgrade.index("DROP TRIGGER") < downgrade.index("drop_table")
    assert "DROP TRIGGER trg_kiln_batches_guard_assigned_volume" in downgrade


def test_ningun_nombre_de_restriccion_pasa_de_63_caracteres() -> None:
    """PostgreSQL recorta en silencio los nombres largos y el modelo deja de coincidir."""
    for tabla in (
        KilnBatch.__table__,
        KilnBatchAssignment.__table__,
        KilnBatchOperation.__table__,
        InternalLoad.__table__,
        InternalLoadLine.__table__,
        V2FiringProductionHandoff.__table__,
        ProductionOrder.__table__,
    ):
        for restriccion in tabla.constraints:
            assert restriccion.name is None or len(str(restriccion.name)) <= 63, restriccion.name
        for indice in tabla.indexes:
            assert indice.name is None or len(str(indice.name)) <= 63, indice.name


def test_la_garantia_de_capacidad_esta_en_la_base() -> None:
    nombres = {str(c.name) for c in KilnBatch.__table__.constraints if c.name is not None}
    assert "ck_kiln_batches_assigned_within_capacity" in nombres
    assert "ck_kiln_batches_assigned_non_negative" in nombres
    assert "ck_kiln_batches_assigned_within_capacity" in _codigo()


def test_toda_la_cadena_se_renderiza_sin_base() -> None:
    entorno = {**os.environ, "DATABASE_URL": "postgresql://ci:ci@localhost:5432/ci"}
    resultado = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        env=entorno,
        capture_output=True,
        text=True,
        check=False,
    )
    assert resultado.returncode == 0, resultado.stderr[-2000:]
    assert "kiln_batches" in resultado.stdout
    assert "trg_kiln_batch_assignments_volume" in resultado.stdout
