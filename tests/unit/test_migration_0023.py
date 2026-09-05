"""0023 vigilandose a si misma: que siga siendo aditiva.

El despliegue es DB primero. Entre que la base llega a 0023 y el backend nuevo
recibe trafico, la revision 0022 sigue sirviendo: escribe cotizaciones, emite
correlativos y arranca muestras sin saber que existe `prototype_quotations`.
Cualquier cosa que 0023 quite, renombre o estreche la tumbaria en esa ventana.

Estas pruebas leen el ARCHIVO. Son feas a proposito: comprobar el efecto en una
base real ya lo hacen las de PostgreSQL, y lo que aqui se protege es que nadie
convierta la migracion en destructiva de un commit a otro sin enterarse.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRACION = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _contenido() -> str:
    archivos = list(MIGRACION.glob("0023_*.py"))
    assert len(archivos) == 1, f"Se esperaba una sola 0023, hay {len(archivos)}"
    return archivos[0].read_text(encoding="utf-8")


def _upgrade() -> str:
    return _contenido().split("def upgrade()")[1].split("def downgrade()")[0]


def test_el_upgrade_no_borra_ni_renombra_nada() -> None:
    """0023_ADDITIVE / OLD_0022_BACKEND_SCHEMA_COMPATIBLE_WITH_0023."""
    upgrade = _upgrade()
    assert "drop_table" not in upgrade
    assert "drop_column" not in upgrade
    assert "alter_column" not in upgrade
    assert "rename" not in upgrade.lower()


def test_el_upgrade_no_toca_las_cotizaciones_de_producto() -> None:
    """QUOTATIONS_TABLE_SEMANTICS_UNCHANGED.

    Meter el prototipo en `quotations` habria obligado a falsear
    `commercial_factor` y a rellenar con ceros el costeo de producto. Se decidio
    tabla propia justamente para no tocar esa semantica: aqui se comprueba.
    """
    upgrade = _upgrade()
    for tabla in ('"quotations"', '"quotation_items"', '"quotation_commercial_lines"'):
        assert tabla not in upgrade, tabla


def test_el_unico_check_reescrito_es_el_del_talonario_y_solo_para_ampliarlo() -> None:
    """Ampliar un CHECK es compatible: acepta lo de antes MAS lo nuevo.

    El backend 0022 sigue insertando los tipos de siempre sin enterarse. Es el
    mismo movimiento que hizo 0021 al anadir `PROTOTYPE`.
    """
    upgrade = _upgrade()
    assert upgrade.count("drop_constraint") == 1
    assert '"type_allowed", "document_sequences"' in upgrade
    assert "SEQUENCE_TYPES_AFTER" in upgrade

    # La lista vive en una constante del modulo, no dentro de `upgrade()`: se
    # comprueba ahi, que es donde de verdad se declara lo que sigue admitiendose.
    contenido = _contenido()
    assert "SEQUENCE_TYPES_AFTER = f\"{SEQUENCE_TYPES_BEFORE}, 'PROTOTYPE_QUOTE'\"" in contenido
    for tipo in ("'QUOTE'", "'FIRING'", "'PREPARATION'", "'PRODUCTION_ORDER'", "'PROTOTYPE'"):
        assert tipo in contenido.split("SEQUENCE_TYPES_BEFORE = ")[1], tipo


def test_lo_que_se_anade_a_tablas_en_uso_admite_nulo_o_trae_valor_por_defecto() -> None:
    """Una columna NOT NULL sin default sobre una tabla con filas falla al aplicar."""
    upgrade = _upgrade()
    for bloque in upgrade.split("op.add_column(")[1:]:
        columna = bloque.split(")")[0]
        if "nullable=False" in columna:
            assert "server_default" in columna, columna


def test_las_tarifas_del_prototipo_nacen_en_cero_y_no_con_los_ejemplos_del_excel() -> None:
    """El Excel marca 80, 100, 350 y 600 como VALORES DE EJEMPLO.

    Sembrarlos convertiria un ejemplo en un precio real en cuanto alguien
    cotizara sin mirar. La casa escribe los suyos en Configuracion.
    """
    upgrade = _upgrade()
    bloque = upgrade.split("commercial_settings")[1] if "commercial_settings" in upgrade else ""
    for ejemplo in ("80", "100", "350", "600"):
        assert f'text("{ejemplo}")' not in bloque, ejemplo


def test_no_se_rellena_hacia_atras_ninguna_muestra_existente() -> None:
    """HISTORICAL_PROTOTYPE_BACKFILL: NONE.

    Las muestras anteriores nacieron sin cotizacion de prototipo. Calcularles
    una con las tarifas de hoy seria inventar un precio que nadie acordo.
    """
    upgrade = _upgrade()
    assert "UPDATE prototypes" not in upgrade
    assert "UPDATE quotations" not in upgrade
    # El unico INSERT es la fila del talonario, y es idempotente.
    assert upgrade.count("INSERT INTO") == 1
    assert "WHERE NOT EXISTS" in upgrade


def test_la_vuelta_atras_se_niega_cuando_hay_documentos_emitidos() -> None:
    """Un CPR emitido pudo enviarse a un cliente; el correlativo no vuelve."""
    downgrade = _contenido().split("def downgrade()")[1]
    assert "RuntimeError" in downgrade
    assert "count(*) FROM prototype_quotations" in downgrade


def test_alembic_una_sola_cabeza_y_es_0023() -> None:
    """Una sola cabeza, y es la nueva.

    Esta afirmacion se muda al ultimo archivo de cada fase a proposito: fijarla
    en una revision concreta obliga a reescribir la prueba anterior cada vez, y
    entonces deja de comprobar nada.
    """
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_heads() == ["0023"], script.get_heads()


def test_la_cadena_no_se_rompe() -> None:
    """0023 cuelga de 0022, y 0022 sigue existiendo."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0022") is not None
    assert script.get_revision("0023").down_revision == "0022"
