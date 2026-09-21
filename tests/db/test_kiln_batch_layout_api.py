"""Pruebas de la API de layout fisico del horno (Fase 010M - M1).

Cubre:
1. GET 404 si no hay layout
2. PUT 200 crea layout inicial (expected_version=0)
3. GET 200 devuelve layout con niveles y placements
4. PUT 200 actualiza layout (expected_version=1 -> version=2)
5. PUT 409 por stale version
6. PUT 422 por rotacion no permitida (ej: 45 o 180)
7. PUT 409 en estado no editable (ej: STARTED)
8. Privacidad: respuesta no expone precios, IGV, factor ni margen
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kiln_batches import KilnBatchStatus
from tests.db.test_kiln_batch_layout_service import _assign_raw, _kiln_with_dims
from tests.db.test_kiln_batches_api import head
from tests.db.test_kiln_batches_service import _batch, _load

pytestmark = pytest.mark.asyncio

KILN_BATCHES = "/api/v1/kiln-batches"


async def test_layout_api_ciclo_completo(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Ciclo completo por HTTP: GET inicial (404), PUT creacion, GET (200), PUT actualizacion."""
    kiln = await _kiln_with_dims(db_session, "K-API-LAY")
    batch = await _batch(db_session, kiln, "HOR-API-LAY")
    load, line = await _load(db_session, "L-API-LAY", quantity=10, unit_volume=Decimal("50"))
    asgn = await _assign_raw(db_session, batch, load.id, line.id, quantity=10)
    await db_session.commit()

    # 1. GET inicial -> 404
    res_get_init = await api.get(f"{KILN_BATCHES}/{batch.id}/layout")
    assert res_get_init.status_code == 404, res_get_init.text

    # 2. PUT inicial con expected_version=0
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
            "piece_length_cm_snapshot": "10.0",
            "piece_width_cm_snapshot": "8.0",
            "piece_height_cm_snapshot": "15.0",
            "separation_cm_snapshot": "1.0",
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
    assert body["placements"][0]["rotation_degrees"] == 0

    # Privacidad: verificar que ningun campo de precio/margen/IGV este presente
    for forbidden in ("price", "subtotal", "igv", "margin", "factor", "ganancia"):
        assert forbidden not in str(body).lower()

    # 3. GET -> 200
    res_get = await api.get(f"{KILN_BATCHES}/{batch.id}/layout")
    assert res_get.status_code == 200, res_get.text
    get_body = res_get.json()
    assert get_body["version"] == 1
    assert len(get_body["levels"]) == 1
    assert len(get_body["placements"]) == 1

    # 4. PUT con rotacion 90 y actualizacion de version
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


async def test_layout_api_rotacion_invalida_422(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Rotaciones distintas de 0 o 90 (ej. 45 o 180) son rechazadas con 422 por schema."""
    kiln = await _kiln_with_dims(db_session, "K-API-ROT")
    batch = await _batch(db_session, kiln, "HOR-API-ROT")
    load, line = await _load(db_session, "L-API-ROT", quantity=10, unit_volume=Decimal("50"))
    asgn = await _assign_raw(db_session, batch, load.id, line.id, quantity=10)
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
                        "piece_length_cm_snapshot": "10",
                        "piece_width_cm_snapshot": "10",
                        "piece_height_cm_snapshot": "10",
                        "separation_cm_snapshot": "0",
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
