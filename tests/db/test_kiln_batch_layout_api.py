"""Pruebas de la API de layout físico del horno (Fase 010M - M1 y M2).

Cubre:
1. GET 404 si no hay layout
2. PUT 200 crea layout inicial (expected_version=0) con derivación de snapshots
3. GET 200 devuelve layout con niveles, placements y resumen operacional (placed_qty, pending_qty)
4. PUT 200 actualiza layout (expected_version=1 -> version=2)
5. PUT 409 por stale version
6. PUT 422 por rotación no permitida (ej: 45 o 180)
7. PUT 422 si el cliente intenta enviar snapshots en el payload (extra forbidden)
8. PUT 409 en estado no editable (ej: STARTED)
9. Privacidad: respuesta no expone precios, IGV, factor ni margen
10. Idempotencia HTTP:
    - Mismo idempotency_key + mismo payload: 200 sin 409 ni duplicación
    - Mismo idempotency_key + diferente payload: 409
    - Diferente idempotency_key + versión obsoleta: 409
11. Validaciones M2 por HTTP (422):
    - Colisión 2D entre piezas en el mismo nivel
    - Placement fuera de los límites del horno
    - Placement con quantity != 1
    - Atomicidad: error de validación no consume versión ni altera el layout existente
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kiln_batches import KilnBatchStatus
from tests.db.test_kiln_batch_layout_service import _assign_internal, _kiln_with_dims
from tests.db.test_kiln_batches_api import head
from tests.db.test_kiln_batches_service import _batch, _load

pytestmark = pytest.mark.asyncio

KILN_BATCHES = "/api/v1/kiln-batches"


async def test_layout_api_ciclo_completo(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Ciclo completo por HTTP: GET inicial (404), PUT creación, GET (200), PUT actualización."""
    kiln = await _kiln_with_dims(db_session, "K-API-LAY", height=Decimal("80"))
    batch = await _batch(db_session, kiln, "HOR-API-LAY")
    load, line = await _load(db_session, "L-API-LAY", quantity=10, unit_volume=Decimal("50"))
    asgn = await _assign_internal(db_session, batch, load, line, quantity=10)
    await db_session.commit()

    # 1. GET inicial -> 404
    res_get_init = await api.get(f"{KILN_BATCHES}/{batch.id}/layout")
    assert res_get_init.status_code == 404, res_get_init.text

    # 2. PUT inicial con expected_version=0 (M2: quantity=1)
    levels_payload = [
        {
            "level_index": 0,
            "name": "Nivel 0",
            "z_cm": "0.0",
            "usable_height_cm": "60.0",
            "plate_label": "Placa 1",
            "plate_thickness_cm": "1.5",
        }
    ]
    placements_payload = [
        {
            "batch_assignment_id": asgn.id,
            "group_index": 0,
            "unit_index": None,
            "quantity": 1,
            "level_index": 0,
            "x_cm": "2.0",
            "y_cm": "3.0",
            "rotation_degrees": 0,
        }
    ]
    put_payload = {
        "expected_version": 0,
        "levels": levels_payload,
        "placements": placements_payload,
    }
    res_put = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json=put_payload,
        headers=head(admin_csrf),
    )
    assert res_put.status_code == 200, res_put.text
    body = res_put.json()
    assert body["batch_id"] == batch.id
    assert body["version"] == 1
    assert body["kiln_width_cm_snapshot"] == "60.000000"
    assert body["placed_quantity"] == 1
    assert body["pending_quantity"] == 9
    assert body["invalid_quantity"] == 0
    assert len(body["levels"]) == 1
    assert len(body["placements"]) == 1
    pl = body["placements"][0]
    assert pl["rotation_degrees"] == 0
    assert pl["quantity"] == 1
    # Verificar que los snapshots fueron derivados desde la fuente productiva
    assert pl["piece_length_cm_snapshot"] == "1.000000"
    assert pl["piece_width_cm_snapshot"] == "1.000000"
    assert pl["piece_height_cm_snapshot"] == "50.000000"
    assert pl["separation_cm_snapshot"] == "0.000000"

    # Privacidad: verificar que ningún campo de precio/margen/IGV esté presente
    for forbidden in ("price", "subtotal", "igv", "margin", "factor", "ganancia"):
        assert forbidden not in str(body).lower()

    # 3. GET -> 200
    res_get = await api.get(f"{KILN_BATCHES}/{batch.id}/layout")
    assert res_get.status_code == 200, res_get.text
    get_body = res_get.json()
    assert get_body["version"] == 1
    assert get_body["placed_quantity"] == 1
    assert get_body["pending_quantity"] == 9
    assert get_body["invalid_quantity"] == 0
    assert len(get_body["levels"]) == 1
    assert len(get_body["placements"]) == 1

    # 4. PUT con rotación 90 y actualización de versión
    put_update = {
        "expected_version": 1,
        "levels": levels_payload,
        "placements": [
            {
                **placements_payload[0],
                "rotation_degrees": 90,
            }
        ],
    }
    res_put_upd = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json=put_update,
        headers=head(admin_csrf),
    )
    assert res_put_upd.status_code == 200, res_put_upd.text
    assert res_put_upd.json()["version"] == 2
    assert res_put_upd.json()["placements"][0]["rotation_degrees"] == 90

    # 5. PUT con stale version (expected_version=1 cuando actual es 2) -> 409
    res_stale = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json=put_update,
        headers=head(admin_csrf),
    )
    assert res_stale.status_code == 409, res_stale.text


async def test_layout_api_rechaza_campos_snapshot_en_payload_422(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """El cliente no puede enviar snapshots en placements; extra='forbid' retorna 422."""
    kiln = await _kiln_with_dims(db_session, "K-API-FORBID")
    batch = await _batch(db_session, kiln, "HOR-API-FORBID")
    load, line = await _load(db_session, "L-API-FORBID", quantity=5, unit_volume=Decimal("10"))
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)
    await db_session.commit()

    levels_payload = [
        {
            "level_index": 0,
            "name": "Nivel 0",
            "z_cm": "0.0",
            "usable_height_cm": "20.0",
        }
    ]
    forbidden_fields = [
        {"piece_length_cm_snapshot": "10.0"},
        {"piece_width_cm_snapshot": "8.0"},
        {"piece_height_cm_snapshot": "15.0"},
        {"separation_cm_snapshot": "1.0"},
    ]

    for extra in forbidden_fields:
        placement = {
            "batch_assignment_id": asgn.id,
            "group_index": 0,
            "unit_index": None,
            "quantity": 1,
            "level_index": 0,
            "x_cm": "0",
            "y_cm": "0",
            "rotation_degrees": 0,
            **extra,
        }
        res = await api.put(
            f"{KILN_BATCHES}/{batch.id}/layout",
            json={
                "expected_version": 0,
                "levels": levels_payload,
                "placements": [placement],
            },
            headers=head(admin_csrf),
        )
        assert res.status_code == 422, (
            f"Expected 422 for extra field {extra}, got {res.status_code}"
        )


async def test_layout_api_rotacion_invalida_422(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Rotaciones distintas de 0 o 90 (ej. 45 o 180) son rechazadas con 422 por schema."""
    kiln = await _kiln_with_dims(db_session, "K-API-ROT", height=Decimal("80"))
    batch = await _batch(db_session, kiln, "HOR-API-ROT")
    load, line = await _load(db_session, "L-API-ROT", quantity=10, unit_volume=Decimal("50"))
    asgn = await _assign_internal(db_session, batch, load, line, quantity=10)
    await db_session.commit()

    levels_payload = [
        {
            "level_index": 0,
            "name": "Nivel 0",
            "z_cm": "0.0",
            "usable_height_cm": "60.0",
        }
    ]
    for invalid_deg in (45, 180, 270):
        res = await api.put(
            f"{KILN_BATCHES}/{batch.id}/layout",
            json={
                "expected_version": 0,
                "levels": levels_payload,
                "placements": [
                    {
                        "batch_assignment_id": asgn.id,
                        "group_index": 0,
                        "unit_index": None,
                        "quantity": 1,
                        "level_index": 0,
                        "x_cm": "0",
                        "y_cm": "0",
                        "rotation_degrees": invalid_deg,
                    }
                ],
            },
            headers=head(admin_csrf),
        )
        assert (
            res.status_code == 422
        ), f"Expected 422 for rotation {invalid_deg}, got {res.status_code}"


async def test_layout_api_rechaza_put_en_estado_no_editable(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """PUT en hornada STARTED/COMPLETED/CANCELLED es rechazado con 409."""
    kiln = await _kiln_with_dims(db_session, "K-API-RO")
    batch = await _batch(db_session, kiln, "HOR-API-RO")
    batch.status = KilnBatchStatus.STARTED
    batch.started_at = datetime.now(UTC)
    await db_session.commit()

    res = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json={
            "expected_version": 0,
            "levels": [],
            "placements": [],
        },
        headers=head(admin_csrf),
    )
    assert res.status_code == 409, res.text


async def test_layout_api_idempotencia_http(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Prueba contrato de idempotencia HTTP:
    1. Primer PUT con idempotency_key='IDEMP-API-KEY-1' -> 200, versión 1.
    2. Reintento idéntico con misma key y mismo expected_version=0 -> 200, versión 1 (sin 409).
    3. Reintento con misma key pero payload distinto -> 409.
    4. PUT con distinta key pero expected_version obsoleto (0) -> 409.
    5. GET confirma que no hay duplicación de placements ni niveles.
    """
    kiln = await _kiln_with_dims(db_session, "K-API-IDEMP", height=Decimal("80"))
    batch = await _batch(db_session, kiln, "HOR-API-IDEMP")
    load, line = await _load(db_session, "L-API-IDEMP", quantity=10, unit_volume=Decimal("50"))
    asgn = await _assign_internal(db_session, batch, load, line, quantity=10)
    await db_session.commit()

    idemp_key = "IDEMP-API-KEY-1"
    levels_payload = [
        {
            "level_index": 0,
            "name": "Nivel 0",
            "z_cm": "0.0",
            "usable_height_cm": "60.0",
            "plate_label": "Placa 1",
            "plate_thickness_cm": "1.5",
        }
    ]
    placements_payload = [
        {
            "batch_assignment_id": asgn.id,
            "group_index": 0,
            "unit_index": None,
            "quantity": 1,
            "level_index": 0,
            "x_cm": "5.0",
            "y_cm": "5.0",
            "rotation_degrees": 0,
        }
    ]
    put_payload = {
        "expected_version": 0,
        "idempotency_key": idemp_key,
        "levels": levels_payload,
        "placements": placements_payload,
    }

    # 1. Primer PUT -> 200, versión 1
    res1 = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json=put_payload,
        headers=head(admin_csrf),
    )
    assert res1.status_code == 200, res1.text
    assert res1.json()["version"] == 1

    # 2. Reintento idéntico con misma key y expected_version=0 -> 200, versión 1 (idempotente)
    res2 = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json=put_payload,
        headers=head(admin_csrf),
    )
    assert res2.status_code == 200, res2.text
    assert res2.json()["version"] == 1
    assert len(res2.json()["placements"]) == 1

    # 3. Misma key con payload distinto (x_cm modificado) -> 409
    payload_modified = {
        "expected_version": 0,
        "idempotency_key": idemp_key,
        "levels": levels_payload,
        "placements": [
            {
                **placements_payload[0],
                "x_cm": "10.0",
            }
        ],
    }
    res3 = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json=payload_modified,
        headers=head(admin_csrf),
    )
    assert res3.status_code == 409, res3.text

    # 4. Distinta key con expected_version obsoleto (0 en vez de 1) -> 409
    payload_stale = {
        "expected_version": 0,
        "idempotency_key": "IDEMP-API-KEY-2",
        "levels": levels_payload,
        "placements": placements_payload,
    }
    res4 = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json=payload_stale,
        headers=head(admin_csrf),
    )
    assert res4.status_code == 409, res4.text

    # 5. GET final confirma versión 1 y exactamente 1 placement (sin duplicación)
    res_get = await api.get(f"{KILN_BATCHES}/{batch.id}/layout")
    assert res_get.status_code == 200, res_get.text
    get_body = res_get.json()
    assert get_body["version"] == 1
    assert len(get_body["levels"]) == 1
    assert len(get_body["placements"]) == 1
    assert get_body["placements"][0]["quantity"] == 1


# ---------------------------------------------------------------------------
# Validaciones Geométricas M2 por HTTP
# ---------------------------------------------------------------------------

async def test_layout_api_rechaza_colision_422(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Dos placements en el mismo nivel que solapan áreas reservadas retornan 422 COLLISION."""
    kiln = await _kiln_with_dims(db_session, "K-API-COL")
    batch = await _batch(db_session, kiln, "HOR-API-COL")
    load, line = await _load(db_session, "L-API-COL", quantity=5, unit_volume=Decimal("10"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("2.0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)
    await db_session.commit()

    levels_payload = [
        {"level_index": 0, "name": "N0", "z_cm": "0", "usable_height_cm": "20"}
    ]
    # Pieza 10x10 con sep 2 -> reservado 12x12
    # P1 en (0, 0) -> [0..12, 0..12]
    # P2 en (11, 0) -> [11..23, 0..12] -> solapa en [11..12]
    placements_payload = [
        {
            "batch_assignment_id": asgn.id,
            "group_index": 0,
            "unit_index": None,
            "quantity": 1,
            "level_index": 0,
            "x_cm": "0.0",
            "y_cm": "0.0",
            "rotation_degrees": 0,
        },
        {
            "batch_assignment_id": asgn.id,
            "group_index": 1,
            "unit_index": None,
            "quantity": 1,
            "level_index": 0,
            "x_cm": "11.0",
            "y_cm": "0.0",
            "rotation_degrees": 0,
        },
    ]
    res = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json={
            "expected_version": 0,
            "levels": levels_payload,
            "placements": placements_payload,
        },
        headers=head(admin_csrf),
    )
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "KILN_LAYOUT_COLLISION"


async def test_layout_api_rechaza_out_of_bounds_422(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Placement que excede el ancho del horno retorna 422 KILN_LAYOUT_OUT_OF_BOUNDS."""
    kiln = await _kiln_with_dims(db_session, "K-API-OOB", width=Decimal("50"))
    batch = await _batch(db_session, kiln, "HOR-API-OOB")
    load, line = await _load(db_session, "L-API-OOB", quantity=5, unit_volume=Decimal("10"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)
    await db_session.commit()

    levels_payload = [
        {"level_index": 0, "name": "N0", "z_cm": "0", "usable_height_cm": "20"}
    ]
    # x=45 para pieza de 10 -> right=55 > 50 (kiln_width)
    res = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json={
            "expected_version": 0,
            "levels": levels_payload,
            "placements": [
                {
                    "batch_assignment_id": asgn.id,
                    "group_index": 0,
                    "unit_index": None,
                    "quantity": 1,
                    "level_index": 0,
                    "x_cm": "45.0",
                    "y_cm": "0.0",
                    "rotation_degrees": 0,
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "KILN_LAYOUT_OUT_OF_BOUNDS"


async def test_layout_api_rechaza_quantity_invalida_422(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Placement con quantity != 1 retorna 422 KILN_LAYOUT_PHYSICAL_QUANTITY_INVALID."""
    kiln = await _kiln_with_dims(db_session, "K-API-QINV")
    batch = await _batch(db_session, kiln, "HOR-API-QINV")
    load, line = await _load(db_session, "L-API-QINV", quantity=5, unit_volume=Decimal("10"))
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)
    await db_session.commit()

    levels_payload = [
        {"level_index": 0, "name": "N0", "z_cm": "0", "usable_height_cm": "20"}
    ]
    res = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json={
            "expected_version": 0,
            "levels": levels_payload,
            "placements": [
                {
                    "batch_assignment_id": asgn.id,
                    "group_index": 0,
                    "unit_index": None,
                    "quantity": 2,  # Inválido en M2
                    "level_index": 0,
                    "x_cm": "0.0",
                    "y_cm": "0.0",
                    "rotation_degrees": 0,
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "KILN_LAYOUT_PHYSICAL_QUANTITY_INVALID"


async def test_layout_api_atomicidad_error_no_incrementa_version(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Si una actualización falla por colisión, la versión no cambia y el layout se conserva."""
    kiln = await _kiln_with_dims(db_session, "K-API-ATOM")
    batch = await _batch(db_session, kiln, "HOR-API-ATOM")
    load, line = await _load(db_session, "L-API-ATOM", quantity=5, unit_volume=Decimal("10"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("1.0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)
    await db_session.commit()

    levels_payload = [
        {"level_index": 0, "name": "N0", "z_cm": "0", "usable_height_cm": "20"}
    ]

    # 1. Crear versión 1 válida
    res_v1 = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json={
            "expected_version": 0,
            "levels": levels_payload,
            "placements": [
                {
                    "batch_assignment_id": asgn.id,
                    "group_index": 0,
                    "unit_index": None,
                    "quantity": 1,
                    "level_index": 0,
                    "x_cm": "0.0",
                    "y_cm": "0.0",
                    "rotation_degrees": 0,
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert res_v1.status_code == 200
    assert res_v1.json()["version"] == 1

    # 2. Intentar actualizar con placement colisionante
    res_fail = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json={
            "expected_version": 1,
            "levels": levels_payload,
            "placements": [
                {
                    "batch_assignment_id": asgn.id,
                    "group_index": 0,
                    "unit_index": None,
                    "quantity": 1,
                    "level_index": 0,
                    "x_cm": "0.0",
                    "y_cm": "0.0",
                    "rotation_degrees": 0,
                },
                {
                    "batch_assignment_id": asgn.id,
                    "group_index": 1,
                    "unit_index": None,
                    "quantity": 1,
                    "level_index": 0,
                    "x_cm": "5.0",  # Colisión
                    "y_cm": "0.0",
                    "rotation_degrees": 0,
                },
            ],
        },
        headers=head(admin_csrf),
    )
    assert res_fail.status_code == 422
    assert res_fail.json()["error"]["code"] == "KILN_LAYOUT_COLLISION"

    # 3. GET confirma que la versión sigue siendo 1 y hay exactamente 1 placement
    res_get = await api.get(f"{KILN_BATCHES}/{batch.id}/layout")
    assert res_get.status_code == 200
    assert res_get.json()["version"] == 1
    assert len(res_get.json()["placements"]) == 1


# ---------------------------------------------------------------------------
# Sugerencia de Layout M3 en API
# ---------------------------------------------------------------------------

async def test_suggest_layout_api_ciclo_y_no_mutacion(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """POST suggest devuelve sugerencia sin mutar el layout persistido en DB."""
    kiln = await _kiln_with_dims(db_session, "K-API-SUG", width=Decimal("60"), depth=Decimal("50"))
    batch = await _batch(db_session, kiln, "HOR-API-SUG")
    load, line = await _load(db_session, "L-API-SUG", quantity=5, unit_volume=Decimal("50"))
    line.length_cm = Decimal("10.0")
    line.width_cm = Decimal("10.0")
    line.height_cm = Decimal("10.0")
    load.piece_separation_cm = Decimal("0")
    await db_session.flush()
    asgn = await _assign_internal(db_session, batch, load, line, quantity=5)
    await db_session.commit()

    levels_payload = [
        {"level_index": 0, "name": "N0", "z_cm": "0", "usable_height_cm": "20"}
    ]
    # Guardar versión 1 con 1 placement
    res_init = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json={
            "expected_version": 0,
            "levels": levels_payload,
            "placements": [
                {
                    "batch_assignment_id": asgn.id,
                    "group_index": 0,
                    "unit_index": None,
                    "quantity": 1,
                    "level_index": 0,
                    "x_cm": "0.0",
                    "y_cm": "0.0",
                    "rotation_degrees": 0,
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert res_init.status_code == 200
    assert res_init.json()["version"] == 1

    # POST suggest para las 4 piezas pendientes
    res_sug = await api.post(
        f"{KILN_BATCHES}/{batch.id}/layout/suggest",
        json={"expected_version": 1},
        headers=head(admin_csrf),
    )
    assert res_sug.status_code == 200, res_sug.text
    sug_body = res_sug.json()
    assert sug_body["batch_id"] == batch.id
    assert sug_body["base_version"] == 1
    assert sug_body["total_pending"] == 4
    assert sug_body["suggested_count"] == 4
    assert sug_body["unplaced_count"] == 0
    assert len(sug_body["suggested_placements"]) == 4

    # Privacidad: verificar que ningún campo de precio/margen/IGV esté presente
    for forbidden in ("price", "subtotal", "igv", "margin", "factor", "ganancia"):
        assert forbidden not in str(sug_body).lower()

    # GET layout confirma NO MUTACIÓN: versión sigue siendo 1 y hay exactamente 1 placement
    res_get = await api.get(f"{KILN_BATCHES}/{batch.id}/layout")
    assert res_get.status_code == 200
    get_body = res_get.json()
    assert get_body["version"] == 1
    assert len(get_body["placements"]) == 1


async def test_suggest_layout_api_expected_version_invalida_409(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """POST suggest con expected_version obsoleta devuelve 409 KILN_LAYOUT_VERSION_CONFLICT."""
    kiln = await _kiln_with_dims(db_session, "K-API-SUG-ST")
    batch = await _batch(db_session, kiln, "HOR-API-SUG-ST")
    await db_session.commit()

    levels_payload = [
        {"level_index": 0, "name": "N0", "z_cm": "0", "usable_height_cm": "20"}
    ]
    # Crear versión 1
    res_init = await api.put(
        f"{KILN_BATCHES}/{batch.id}/layout",
        json={"expected_version": 0, "levels": levels_payload, "placements": []},
        headers=head(admin_csrf),
    )
    assert res_init.status_code == 200

    # Request con expected_version=0 cuando actual es 1
    res = await api.post(
        f"{KILN_BATCHES}/{batch.id}/layout/suggest",
        json={"expected_version": 0},
        headers=head(admin_csrf),
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "KILN_LAYOUT_VERSION_CONFLICT"


async def test_suggest_layout_api_rechaza_batch_no_planned_409(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """POST suggest en batch iniciado devuelve 409 KILN_LAYOUT_NOT_EDITABLE."""
    kiln = await _kiln_with_dims(db_session, "K-API-SUG-RO")
    batch = await _batch(db_session, kiln, "HOR-API-SUG-RO")
    batch.status = KilnBatchStatus.STARTED
    batch.started_at = datetime.now(UTC)
    await db_session.commit()

    res = await api.post(
        f"{KILN_BATCHES}/{batch.id}/layout/suggest",
        json={"expected_version": 0},
        headers=head(admin_csrf),
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "KILN_LAYOUT_NOT_EDITABLE"

