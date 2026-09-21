"""Pruebas de la API de layout físico del horno (Fase 010M - M1).

Cubre:
1. GET 404 si no hay layout
2. PUT 200 crea layout inicial (expected_version=0) con derivación de snapshots
3. GET 200 devuelve layout con niveles y placements
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
    kiln = await _kiln_with_dims(db_session, "K-API-LAY")
    batch = await _batch(db_session, kiln, "HOR-API-LAY")
    load, line = await _load(db_session, "L-API-LAY", quantity=10, unit_volume=Decimal("50"))
    asgn = await _assign_internal(db_session, batch, load, line, quantity=10)
    await db_session.commit()

    # 1. GET inicial -> 404
    res_get_init = await api.get(f"{KILN_BATCHES}/{batch.id}/layout")
    assert res_get_init.status_code == 404, res_get_init.text

    # 2. PUT inicial con expected_version=0 (sin snapshots en payload)
    levels_payload = [
        {
            "level_index": 0,
            "name": "Nivel 0",
            "z_cm": "0.0",
            "usable_height_cm": "20.0",
            "plate_label": "Placa 1",
            "plate_thickness_cm": "1.5",
        }
    ]
    placements_payload = [
        {
            "batch_assignment_id": asgn.id,
            "group_index": 0,
            "unit_index": None,
            "quantity": 5,
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
    assert len(body["levels"]) == 1
    assert len(body["placements"]) == 1
    pl = body["placements"][0]
    assert pl["rotation_degrees"] == 0
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
                "levels": [],
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
    kiln = await _kiln_with_dims(db_session, "K-API-ROT")
    batch = await _batch(db_session, kiln, "HOR-API-ROT")
    load, line = await _load(db_session, "L-API-ROT", quantity=10, unit_volume=Decimal("50"))
    asgn = await _assign_internal(db_session, batch, load, line, quantity=10)
    await db_session.commit()

    for invalid_deg in (45, 180, 270):
        res = await api.put(
            f"{KILN_BATCHES}/{batch.id}/layout",
            json={
                "expected_version": 0,
                "levels": [],
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
    kiln = await _kiln_with_dims(db_session, "K-API-IDEMP")
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
            "usable_height_cm": "20.0",
            "plate_label": "Placa 1",
            "plate_thickness_cm": "1.5",
        }
    ]
    placements_payload = [
        {
            "batch_assignment_id": asgn.id,
            "group_index": 0,
            "unit_index": None,
            "quantity": 4,
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
    assert get_body["placements"][0]["quantity"] == 4
