"""Fase 010L — la base de PRUEBAS tiene las mismas garantias que la de verdad.

Las bases de prueba se crean con `Base.metadata.create_all` dentro de
`TEST_SCHEMA` y nunca corren migraciones. Los dos triggers de la capacidad viven
por eso tambien en el modelo, colgados de `after_create`. Esta prueba comprueba
que de verdad existen y funcionan AQUI, con este `search_path`: si no, todas las
pruebas del servicio pasarian contra una base que no garantiza nada.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def _id(session: AsyncSession, sql: str, parametros: dict[str, object] | None = None) -> int:
    valor = await session.scalar(text(sql), parametros or {})
    assert valor is not None
    return int(str(valor))


async def _hornada(session: AsyncSession, codigo: str) -> int:
    horno = await _id(
        session,
        "INSERT INTO kilns (code, name, capacity_volume_cm3, firing_days_per_batch, active)"
        " VALUES (:c, 'Horno', 1000, 3, true) RETURNING id",
        {"c": f"K-{codigo}"},
    )
    return await _id(
        session,
        "INSERT INTO kiln_batches (code, kiln_id, firing_type, scheduled_date,"
        " kiln_name_snapshot, capacity_snapshot_cm3)"
        " VALUES (:c, :k, 'LOW', current_date, 'Horno', 1000) RETURNING id",
        {"c": codigo, "k": horno},
    )


async def _linea(session: AsyncSession, codigo: str) -> tuple[int, int]:
    carga = await _id(
        session,
        "INSERT INTO internal_loads (code, name, low_fire_required)"
        " VALUES (:c, 'Carga', true) RETURNING id",
        {"c": codigo},
    )
    linea = await _id(
        session,
        "INSERT INTO internal_load_lines (load_id, sort_order, name, quantity, length_cm,"
        " width_cm, height_cm, unit_volume_cm3, total_volume_cm3)"
        " VALUES (:l, 1, 'Taza', 20, 5, 5, 4, 100, 2000) RETURNING id",
        {"l": carga},
    )
    return carga, linea


async def _asignar(
    session: AsyncSession, hornada: int, carga: int, linea: int, cantidad: int
) -> int:
    return await _id(
        session,
        "INSERT INTO kiln_batch_assignments (batch_id, source_kind, internal_load_id,"
        " internal_load_line_id, quantity, unit_volume_snapshot_cm3, assigned_volume_cm3,"
        " firing_mode, product_name_snapshot)"
        " VALUES (:b, 'INTERNAL', :c, :l, :q, 100, :v, 'SHARED', 'Taza') RETURNING id",
        {"b": hornada, "c": carga, "l": linea, "q": cantidad, "v": Decimal(cantidad * 100)},
    )


async def _asignado(session: AsyncSession, hornada: int) -> Decimal:
    valor = await session.scalar(
        text("SELECT assigned_volume_cm3 FROM kiln_batches WHERE id = :b"), {"b": hornada}
    )
    return Decimal(str(valor))


async def test_los_dos_triggers_existen_en_el_esquema_de_pruebas(db_session: AsyncSession) -> None:
    nombres = set(
        (
            await db_session.scalars(
                text(
                    "SELECT tgname FROM pg_trigger"
                    " WHERE tgname IN ('trg_kiln_batch_assignments_volume',"
                    " 'trg_kiln_batches_guard_assigned_volume')"
                )
            )
        ).all()
    )
    assert nombres == {
        "trg_kiln_batch_assignments_volume",
        "trg_kiln_batches_guard_assigned_volume",
    }


async def test_el_contador_se_mueve_en_el_esquema_de_pruebas(db_session: AsyncSession) -> None:
    hornada = await _hornada(db_session, "HOR-ESQ")
    carga, linea = await _linea(db_session, "CI-ESQ")
    await _asignar(db_session, hornada, carga, linea, 4)
    assert await _asignado(db_session, hornada) == Decimal(400)


async def test_el_contador_no_se_escribe_a_mano(db_session: AsyncSession) -> None:
    """Un UPDATE directo al contador lo rechaza la guarda, aunque baje."""
    hornada = await _hornada(db_session, "HOR-MANO")
    carga, linea = await _linea(db_session, "CI-MANO")
    await _asignar(db_session, hornada, carga, linea, 4)
    await db_session.commit()

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("UPDATE kiln_batches SET assigned_volume_cm3 = 0 WHERE id = :b"), {"b": hornada}
        )
    await db_session.rollback()
    assert await _asignado(db_session, hornada) == Decimal(400)


async def test_una_hornada_no_nace_con_volumen(db_session: AsyncSession) -> None:
    horno = await _id(
        db_session,
        "INSERT INTO kilns (code, name, capacity_volume_cm3, firing_days_per_batch, active)"
        " VALUES ('K-NACE', 'Horno', 1000, 3, true) RETURNING id",
    )
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO kiln_batches (code, kiln_id, firing_type, scheduled_date,"
                " kiln_name_snapshot, capacity_snapshot_cm3, assigned_volume_cm3)"
                " VALUES ('HOR-NACE', :k, 'LOW', current_date, 'Horno', 1000, 500)"
            ),
            {"k": horno},
        )


async def test_cambiar_otras_columnas_de_la_hornada_si_se_puede(
    db_session: AsyncSession,
) -> None:
    """La guarda solo mira el contador: version, notas y fecha se tocan con normalidad."""
    hornada = await _hornada(db_session, "HOR-OTRAS")
    await db_session.execute(
        text("UPDATE kiln_batches SET version = version + 1, notes = 'Revisar' WHERE id = :b"),
        {"b": hornada},
    )
    version = await db_session.scalar(
        text("SELECT version FROM kiln_batches WHERE id = :b"), {"b": hornada}
    )
    assert version == 2


async def test_cambiar_de_hornada_resta_en_una_y_suma_en_otra(db_session: AsyncSession) -> None:
    """Ningun camino del servicio lo hace, pero el trigger lo cubre igual."""
    origen = await _hornada(db_session, "HOR-ORIGEN")
    destino = await _hornada(db_session, "HOR-DESTINO")
    carga, linea = await _linea(db_session, "CI-MUEVE")
    asignacion = await _asignar(db_session, origen, carga, linea, 3)
    assert await _asignado(db_session, origen) == Decimal(300)

    await db_session.execute(
        text("UPDATE kiln_batch_assignments SET batch_id = :d WHERE id = :a"),
        {"d": destino, "a": asignacion},
    )
    assert await _asignado(db_session, origen) == Decimal(0)
    assert await _asignado(db_session, destino) == Decimal(300)
