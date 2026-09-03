"""Contrato de la migracion 0020: ordenes de produccion (Fase 009I).

Aqui se lee el ARCHIVO. Que la migracion corra de verdad contra PostgreSQL lo
comprueba `tests/db/test_migration_0020_runs.py`; esto fija las decisiones que
se tomaron al escribirla y que un cambio distraido podria deshacer.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).parents[2]
MIGRATION_FILE = REPO_ROOT / "alembic" / "versions" / "0020_production_orders.py"


def _upgrade() -> str:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    return content.split("def upgrade()")[1].split("def downgrade()")[0]


def test_migration_0020_file_and_revisions() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "0020"' in content
    assert 'down_revision: str | None = "0019"' in content


def test_0020_sigue_estando_en_el_camino_a_la_cabeza() -> None:
    """0020 se alcanza desde la cabeza actual, sea cual sea.

    Antes esta prueba exigia que la cabeza FUERA 0020, y 009K la rompio sin que
    nada estuviera mal: es la cuarta vez que pasa en este proyecto. La
    afirmacion de cabeza unica se muda al archivo de la migracion mas reciente
    —ahora 0021—; lo que hay que proteger aqui es que la revision siga en la
    cadena.
    """
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    assert "0020" in {revision.revision for revision in script.iterate_revisions(heads[0], "base")}


def test_0019_sigue_siendo_alcanzable() -> None:
    """La cadena no se rompe: 0020 cuelga de 0019 y 0019 sigue existiendo."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0019") is not None
    assert script.get_revision("0020").down_revision == "0019"


def test_los_checks_de_estado_y_fecha_manejan_el_nulo_explicitamente() -> None:
    """El agujero de 0017 y de 0019, cerrado por adelantado esta vez.

    En SQL `NULL = 'CREATED'` no es FALSE sino NULL, y un CHECK que evalua a
    NULL se da por CUMPLIDO. Un OR de ramas con igualdad deja pasar cualquier
    fila cuyo discriminante sea nulo, que es exactamente la forma que tiene
    esta restriccion.

    Hoy `status` es NOT NULL y el guardia es redundante. Se exige igual: la
    correccion de una restriccion no puede depender de que otra clausula, en
    otra parte del esquema, siga estando manana.
    """
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    coherencia = content.split("STATUS_TIMESTAMPS_COHERENT = ")[1].split('"""')[1]

    for estado in ("CREATED", "STARTED", "COMPLETED", "CANCELLED"):
        assert f"status = '{estado}'" in coherencia, f"falta la rama de {estado}"
    assert coherencia.count("status IS NOT NULL") == 4, (
        "cada rama necesita su guardia contra el nulo, no solo la primera"
    )
    # Cada estado exige y prohibe sus fechas.
    assert "status = 'STARTED'" in coherencia and "started_at IS NOT NULL" in coherencia
    assert "status = 'CANCELLED'" in coherencia and "cancelled_at IS NOT NULL" in coherencia


def test_una_cotizacion_solo_puede_tener_una_orden_y_lo_impone_la_base() -> None:
    """ONE_ORDER_PER_QUOTATION_DB_ENFORCED.

    Comprobarlo en el servicio no basta: dos peticiones simultaneas pasan las
    dos esa comprobacion. El UNIQUE es lo unico que no se puede correr.
    """
    upgrade = _upgrade()
    assert 'sa.UniqueConstraint("quotation_id"' in upgrade
    assert '"quotation_id", sa.Integer(), nullable=False' in upgrade


def test_la_ubicacion_de_stock_es_obligatoria() -> None:
    upgrade = _upgrade()
    assert '"stock_location_id", sa.Integer(), nullable=False' in upgrade
    assert "stock_locations.id" in upgrade


def test_el_movimiento_sabe_de_que_orden_salio() -> None:
    """Sin esta columna, un consumo de produccion es indistinguible de una merma."""
    # Se busca por contenido y no por el formato exacto de la llamada: el
    # formateador reparte los argumentos en una o varias lineas segun quepan, y
    # una prueba atada a esa forma se rompe sin que nada este mal.
    anadido = " ".join(_upgrade().split())
    assert 'op.add_column("stock_movements", sa.Column("production_order_id"' in anadido
    # Se corta por el add_column concreto: "production_order_id" aparece
    # tambien como columna NOT NULL de production_order_lines, y partir por el
    # nombre a secas miraria esa otra y daria por buena una columna que no es.
    columna = anadido.split('op.add_column("stock_movements", ')[1][:120]
    assert "nullable=True" in columna, "la columna nueva en una tabla en uso tiene que ser nula"
    assert "fk_stock_movements_production_order_id_production_orders" in anadido
    assert "ix_stock_movements_production_order_id" in anadido


def test_el_check_de_movimientos_solo_se_amplia() -> None:
    """MIGRATION_BACKWARD_COMPAT.

    Los 7 movimientos historicos de carga inicial y cualquier preparacion
    pasada tienen que seguir siendo validos despues de migrar. El CHECK nuevo
    contiene a los seis tipos anteriores mas PRODUCTION_OUT.
    """
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    permitidos = content.split("MOVEMENT_TYPES_ALLOWED = ")[1].split(")")[0]
    for tipo in (
        "INITIAL_IMPORT",
        "ADJUSTMENT",
        "IN",
        "OUT",
        "PREPARATION_OUT",
        "PREPARATION_IN",
        "PRODUCTION_OUT",
    ):
        assert f"'{tipo}'" in permitidos, f"{tipo} desapareceria del CHECK"


def test_la_secuencia_de_la_orden_se_siembra_sin_pisar_lo_que_haya() -> None:
    """El contador OP-2026-000001, sembrado de forma idempotente.

    `WHERE NOT EXISTS` y no un INSERT a secas: reejecutar la migracion sobre una
    base que ya la tiene no puede reventar ni reiniciar el contador a cero.
    """
    upgrade = _upgrade()
    assert "'PRODUCTION_ORDER'" in upgrade
    assert "WHERE NOT EXISTS" in upgrade
    assert "'OP'" in upgrade
    assert "{PREFIX}-{YYYY}-{NUMBER}" in upgrade


def test_la_columna_del_tipo_de_secuencia_se_ensancha() -> None:
    """ "PRODUCTION_ORDER" mide exactamente 16 y cabria por los pelos.

    Dejarlo asi seria poner una mina para el siguiente tipo de documento con un
    nombre un caracter mas largo. Ensanchar un varchar en PostgreSQL no
    reescribe la tabla.
    """
    upgrade = _upgrade()
    assert len("PRODUCTION_ORDER") == 16
    assert "type_=sa.String(24)" in upgrade
    assert '"document_sequences"' in upgrade
    assert '"document_sequence_issues"' in upgrade


def test_la_migracion_no_crea_ordenes_historicas() -> None:
    """HISTORICAL_PRODUCTION_ORDER_BACKFILL: NONE.

    Nadie ha decidido fabricar las 18 confirmadas que existen. Crearles una
    orden seria inventar una decision productiva que no tomo ninguna persona, y
    despues no se distinguiria de una real.
    """
    upgrade = _upgrade()
    assert "INSERT INTO production_orders" not in upgrade
    assert "SELECT" not in upgrade.split("INSERT INTO document_sequences")[0], (
        "no puede haber ningun INSERT ... SELECT contra production_orders"
    )


def test_la_migracion_es_aditiva() -> None:
    """OLD_REVISION_ON_SCHEMA_0020: HEALTHY.

    Un backend anterior a 009I tiene que seguir sirviendo sobre este esquema:
    ni se borran columnas, ni se renombran, ni se anaden NOT NULL sin default a
    tablas en uso.
    """
    upgrade = _upgrade()
    assert "op.drop_column" not in upgrade
    assert "op.drop_table" not in upgrade
    assert "op.alter_column" in upgrade  # solo el ensanchado del varchar
    # La unica columna anadida a una tabla existente es nullable.
    anadidas = [
        bloque for bloque in upgrade.split("op.add_column")[1:] if "stock_movements" in bloque
    ]
    assert len(anadidas) == 1
    assert "nullable=True" in anadidas[0]


def test_el_downgrade_deshace_lo_que_el_upgrade_hizo() -> None:
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    downgrade = content.split("def downgrade()")[1]
    assert 'op.drop_table("production_order_lines")' in downgrade
    assert 'op.drop_table("production_orders")' in downgrade
    assert 'op.drop_column("stock_movements", "production_order_id")' in downgrade
    assert "DELETE FROM document_sequences WHERE sequence_type = 'PRODUCTION_ORDER'" in downgrade
