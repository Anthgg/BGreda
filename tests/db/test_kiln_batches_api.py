from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quoter_v2 import V2FiringMode
from tests.db.test_kiln_batches_service import _batch, _kiln, _load, _v2_order

KILN_BATCHES = "/api/v1/kiln-batches"


def head(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf}


async def test_crear_listar_y_asignar_hornada_por_api(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    kiln = await _kiln(db_session, "K-API", capacity=Decimal("1000"))
    load, line = await _load(db_session, "CI-API", quantity=10, unit_volume=Decimal("50"))
    await db_session.commit()

    created = await api.post(
        KILN_BATCHES,
        json={
            "kiln_id": kiln.id,
            "firing_type": "LOW",
            "scheduled_date": date.today().isoformat(),
            "notes": "Primera hornada API",
            "idempotency_key": "api-create-001",
        },
        headers=head(admin_csrf),
    )
    assert created.status_code == 201, created.text
    batch = created.json()
    assert batch["code"].startswith("HOR-")
    assert batch["assigned_volume_cm3"] == "0.000000"
    assert batch["available_percent"] == "100.000000"

    replay = await api.post(
        KILN_BATCHES,
        json={
            "kiln_id": kiln.id,
            "firing_type": "LOW",
            "scheduled_date": date.today().isoformat(),
            "notes": "Primera hornada API",
            "idempotency_key": "api-create-001",
        },
        headers=head(admin_csrf),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == batch["id"]

    assigned = await api.post(
        f"{KILN_BATCHES}/{batch['id']}/assignments",
        json={
            "internal_load_id": load.id,
            "items": [{"line_id": line.id, "quantity": 4}],
            "expected_version": batch["version"],
            "idempotency_key": "api-assign-001",
        },
        headers=head(admin_csrf),
    )
    assert assigned.status_code == 200, assigned.text
    assigned_body = assigned.json()
    assert assigned_body["assigned_volume_cm3"] == "200.000000"
    assert assigned_body["occupancy_percent"] == "20.000000"
    assert assigned_body["assignments"][0]["quantity"] == 4

    listed = await api.get(KILN_BATCHES)
    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == batch["id"]


async def test_plan_y_sugerencias_de_orden_por_api(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    kiln = await _kiln(db_session, "K-API-SUG", capacity=Decimal("1000"))
    order, line = await _v2_order(
        db_session,
        "API-SUG",
        kiln=kiln,
        firing_mode=V2FiringMode.SHARED,
        quantity=5,
        unit_volume=Decimal("20"),
    )
    batch = await _batch(db_session, kiln, "HOR-API-SUG")
    await db_session.commit()

    plan = await api.get(f"{KILN_BATCHES}/production-orders/{order.id}/firing-plan")
    assert plan.status_code == 200, plan.text
    plan_body = plan.json()
    assert plan_body["production_order_id"] == order.id
    assert plan_body["lines"][0]["line_id"] == line.id
    assert plan_body["lines"][0]["remaining_low"] == 5

    suggestions = await api.get(
        f"{KILN_BATCHES}/production-orders/{order.id}/batch-suggestions",
        params={"firing_type": "LOW"},
    )
    assert suggestions.status_code == 200, suggestions.text
    body = suggestions.json()
    assert body[0]["batch_id"] == batch.id
    assert body[0]["covers_all"] is True
    assert body[0]["covered_percent"] == "100.000000"
