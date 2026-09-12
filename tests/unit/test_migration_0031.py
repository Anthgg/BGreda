"""0031 vigilandose a si misma: aditiva, y sin tocar el catalogo de Legacy.

El riesgo de esta fase tiene nombre: `techniques`. El Cotizador historico ya
tiene una tabla de tecnicas, con un **precio** por tecnica y unos factores de
formula, y la tentacion evidente es anadirle una columna de rendimiento «para
no tener dos catalogos». Seria un error caro y silencioso:

- esa columna de precio es la que cobra Legacy, y compartir fila hace que
  cualquier edicion pensada para V2 le mueva un precio;
- obligaria al dominio V2 a importar `app.models.quotations`, que es justo lo
  que 010A prohibio para que retirar Legacy en 010J no arrastre a V2;
- y mezclaria dos modelos economicos distintos —precio por tecnica frente a
  jornal por horas— en una sola tabla, sin que ninguno de los dos quede dicho.

La otra tentacion es guardar la tarifa por hora. Es `jornal / jornada`: un
numero almacenado que se desincroniza en cuanto alguien edita el jornal por
otra via, y entonces hay dos verdades sobre lo que cuesta una hora.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRACION = REPO_ROOT / "alembic" / "versions"


def _contenido() -> str:
    archivos = list(MIGRACION.glob("0031_*.py"))
    assert len(archivos) == 1, f"Se esperaba una sola 0031, hay {len(archivos)}"
    return archivos[0].read_text(encoding="utf-8")


def _upgrade() -> str:
    return _contenido().split("def upgrade()")[1].split("def downgrade()")[0]


def _downgrade() -> str:
    return _contenido().split("def downgrade()")[1]


# ---------------------------------------------------------------------------
# Aditiva
# ---------------------------------------------------------------------------
def test_el_upgrade_no_destruye_nada() -> None:
    upgrade = _upgrade()
    for prohibido in ("drop_column", "drop_table", "drop_index", "drop_constraint"):
        assert prohibido not in upgrade, prohibido
    assert "rename" not in upgrade.lower()


def test_el_upgrade_crea_exactamente_tres_tablas() -> None:
    upgrade = _upgrade()
    assert upgrade.count("op.create_table(") == 3
    for tabla in ('"v2_workers"', '"v2_techniques"', '"v2_quotation_labor"'):
        assert tabla in upgrade, tabla


def test_el_catalogo_de_tecnicas_de_legacy_no_se_toca() -> None:
    """`techniques` y `quotation_techniques` intactas.

    La comparacion tiene truco y por eso se hace con cuidado:
    «v2_techniques» CONTIENE «techniques», asi que buscar la subcadena a secas
    pasaria siempre y esta prueba no comprobaria nada.
    """
    upgrade = _upgrade()
    sin_v2 = upgrade.replace("v2_techniques", "").replace("v2_quotation_", "")
    assert "techniques" not in sin_v2
    assert "quotation_techniques" not in upgrade


def test_el_upgrade_no_toca_las_cotizaciones_de_legacy() -> None:
    upgrade = _upgrade()
    sin_v2 = upgrade.replace("v2_quotations", "").replace("v2_quotation_", "")
    assert "quotations" not in sin_v2


def test_el_upgrade_no_escribe_una_sola_fila() -> None:
    """Ni siembra tecnicas de ejemplo: el catalogo lo escribe el taller.

    Sembrar «torno, asas, vidriado» pareceria un favor y seria una decision de
    negocio tomada por la migracion, con rendimientos inventados que alguien
    acabaria cotizando.
    """
    mayusculas = _upgrade().upper()
    for prohibido in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert prohibido not in mayusculas, prohibido


def test_las_columnas_de_ilustracion_congelada_son_anulables() -> None:
    """NULL ahi significa «cotizacion anterior a la ilustracion», no «cero».

    Y es lo que deja que la revision anterior del backend siga insertando
    durante la ventana de despliegue.
    """
    upgrade = _upgrade()
    assert 'op.add_column("v2_quotations", sa.Column(nombre, tipo, nullable=True))' in upgrade
    constante = _contenido().split("ILLUSTRATION_COLUMNS:")[1].split("def upgrade")[0]
    assert "nullable=False" not in constante


# ---------------------------------------------------------------------------
# El costo sale del trabajador, no de la tecnica
# ---------------------------------------------------------------------------
def test_la_tecnica_guarda_rendimiento_y_no_precio() -> None:
    """Si tuviera precio, «torno = S/110» volveria por la puerta de atras."""
    tabla = _upgrade().split('"v2_techniques"')[1].split("op.create_index")[0]
    assert "default_capacity_per_workday" in tabla
    for prohibida in ("unit_price", "price", "cost", "rate"):
        assert prohibida not in tabla, f"v2_techniques declaro {prohibida}"


def test_el_trabajador_guarda_jornal_y_no_tarifa_por_hora() -> None:
    """La tarifa es `jornal / jornada`: guardarla seria una segunda verdad."""
    tabla = _upgrade().split('"v2_workers"')[1].split("op.create_index")[0]
    assert "daily_rate" in tabla
    assert "hourly_rate" not in tabla


def test_la_jornada_propia_del_trabajador_admite_nulo() -> None:
    """NULL es «la jornada del taller». Copiar la global daria dos jornadas."""
    tabla = _upgrade().split('"v2_workers"')[1].split("op.create_index")[0]
    bloque = tabla.split('"workday_hours"')[1].split("sa.Column(")[0]
    assert "nullable=True" in bloque


def test_la_tarea_congela_la_tarifa_por_hora_que_uso() -> None:
    """Aqui SI se guarda: esta fila tiene que explicarse sin la jornada de hoy."""
    tabla = _upgrade().split('"v2_quotation_labor"')[1]
    for columna in (
        "worker_name_snapshot",
        "worker_type_snapshot",
        "daily_rate_snapshot",
        "workday_hours_snapshot",
        "hourly_rate_snapshot",
        "technique_name_snapshot",
        "standard_capacity_snapshot",
    ):
        assert columna in tabla, columna


def test_la_tarea_distingue_lo_calculado_de_lo_acordado() -> None:
    """Sin las dos columnas no se sabria si unas horas son el estandar o un pacto."""
    tabla = _upgrade().split('"v2_quotation_labor"')[1]
    assert "calculated_hours" in tabla
    assert "final_hours" in tabla
    assert "hours_overridden" in tabla
    assert "rate_overridden" in tabla


# ---------------------------------------------------------------------------
# Lo que la base no deja escribir mal
# ---------------------------------------------------------------------------
def test_un_rendimiento_de_cero_no_cabe() -> None:
    """Seria una division por cero en la formula de horas."""
    assert "default_capacity_per_workday > 0" in _contenido()
    assert "standard_capacity_snapshot > 0" in _contenido()


def test_una_jornada_imposible_no_cabe() -> None:
    contenido = _contenido()
    assert "workday_hours IS NULL OR (workday_hours > 0 AND workday_hours <= 24)" in contenido
    assert "workday_hours_snapshot > 0 AND workday_hours_snapshot <= 24" in contenido


def test_ningun_importe_ni_ninguna_hora_admite_negativos() -> None:
    contenido = _contenido()
    for nombre in (
        "daily_rate_non_negative",
        "hourly_rate_non_negative",
        "calculated_hours_non_negative",
        "final_hours_non_negative",
        "labor_cost_non_negative",
        "quantity_non_negative",
    ):
        assert nombre in contenido, nombre


def test_la_ilustracion_apagada_no_puede_costar_nada() -> None:
    """Mismo criterio que el esmalte de 010C, y por el mismo motivo."""
    contenido = _contenido()
    assert "illustration_off_costs_nothing" in contenido
    assert "illustration_enabled OR (illustration_hours = 0 AND illustration_cost = 0)" in contenido


def test_borrar_la_cotizacion_se_lleva_sus_tareas_pero_no_a_las_personas() -> None:
    """La tarea es de la cotizacion; el trabajador, del taller."""
    upgrade = _upgrade()
    cotizacion = upgrade.split('["v2_quotation_id"], ["v2_quotations.id"]')[1].split("),")[0]
    assert 'ondelete="CASCADE"' in cotizacion
    # Trabajador y tecnica, RESTRICT: borrar a quien figura en una cotizacion
    # emitida dejaria el documento sin explicar.
    assert '["v2_workers.id"], ondelete="RESTRICT"' in upgrade
    assert '["v2_techniques.id"], ondelete="RESTRICT"' in upgrade


def test_la_jornada_compartida_tiene_su_indice() -> None:
    """Se agrupa por trabajador y cotizacion en cada guardado."""
    assert '["v2_quotation_id", "worker_id"]' in _upgrade()


# ---------------------------------------------------------------------------
# La vuelta
# ---------------------------------------------------------------------------
def test_el_downgrade_se_niega_antes_de_tocar_el_esquema() -> None:
    downgrade = _downgrade()
    assert "RuntimeError" in downgrade
    assert downgrade.index("RuntimeError") < downgrade.index("drop_constraint")


def test_el_downgrade_protege_lo_que_no_se_recupera() -> None:
    downgrade = _downgrade()
    for consulta in (
        "SELECT count(*) FROM v2_quotation_labor",
        "SELECT count(*) FROM v2_workers",
        "SELECT count(*) FROM v2_techniques",
    ):
        assert consulta in downgrade, consulta
    assert "illustration_enabled" in downgrade
    # Y los dias efectivos, que son una decision humana y no se recalculan.
    assert "effective_work_days IS NOT NULL" in downgrade


def test_el_downgrade_no_borra_filas_de_nadie() -> None:
    downgrade = _downgrade().upper()
    for prohibido in ("DELETE FROM", "UPDATE ", "TRUNCATE"):
        assert prohibido not in downgrade, prohibido


# ---------------------------------------------------------------------------
# La cadena
# ---------------------------------------------------------------------------
def test_la_cadena_no_se_rompe() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0030") is not None
    assert script.get_revision("0031").down_revision == "0030"


def test_0031_es_la_unica_cabeza() -> None:
    """Dos cabezas son un despliegue que se detiene a mitad, en produccion.

    Esta afirmacion acompana siempre a la ULTIMA revision y se retira de la
    anterior cuando entra una nueva; por eso ya no vive en 0030.
    """
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert list(script.get_heads()) == ["0031"]
