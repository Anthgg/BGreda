"""Fase 009C — la duracion por horno, extremo a extremo.

La matematica pura vive en tests/unit/test_kiln_firing_days.py. Aqui se
comprueba lo que solo se puede comprobar con base de datos y API: que el valor
sale de ``kilns.firing_days_per_batch`` y es editable, que el ajuste manual de
dias sigue siendo un sumando aparte, y que cambiar la configuracion recalcula
los borradores pero no toca lo ya confirmado.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.masters import Product
from tests.db.test_firings_api import FACTORES_CHICO, KILNS, crear_horno
from tests.db.test_quotation_builder_api import _customer, head
from tests.db.test_quotations_api import _finished_product_and_recipe

BUILDER = "/api/v1/quotation-builder"
SMALL_KILN_DAYS = 3
LARGE_KILN_DAYS = 4


async def _kiln(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    *,
    nombre: str,
    capacidad: str,
    dias: int,
    baja: str = "400",
    alta: str = "600",
) -> dict[str, Any]:
    """Horno con tarifas, factores y duracion de hornada configurada."""
    kiln = await crear_horno(
        api,
        csrf,
        db_session,
        nombre=nombre,
        capacidad=capacidad,
        baja=baja,
        alta=alta,
        factores=FACTORES_CHICO,
    )
    updated = await api.patch(
        f"{KILNS}/{kiln['id']}",
        json={"firing_days_per_batch": dias},
        headers=head(csrf),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["firing_days_per_batch"] == dias
    return updated.json()


async def _product(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    *,
    suffix: str,
    dimensions: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    product, recipe = await _finished_product_and_recipe(api, csrf, suffix)
    await db_session.execute(
        update(Product)
        .where(Product.id == product["id"])
        .values(**{field: Decimal(value) for field, value in dimensions.items()})
    )
    await db_session.commit()
    return product, recipe


def _item(product: dict[str, Any], recipe: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    base = {
        "product_id": product["id"],
        "quantity": 1,
        "recipe_id": recipe["id"],
        "recipe_version_id": recipe["current_version"]["id"],
        "material_grams_per_piece": "10",
        "markup_percent": "100",
        "sort_order": 0,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_el_horno_expone_y_edita_su_duracion_de_hornada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La duracion es configuracion persistente, no una constante de codigo."""
    created = await api.post(
        KILNS,
        json={"name": "Horno 009C alta", "capacity_volume_cm3": "10000"},
        headers=head(admin_csrf),
    )
    assert created.status_code == 201, created.text
    # Por omision, la duracion historica: dar de alta un horno no cambia nada.
    assert created.json()["firing_days_per_batch"] == 3

    kiln_id = created.json()["id"]
    updated = await api.patch(
        f"{KILNS}/{kiln_id}", json={"firing_days_per_batch": 5}, headers=head(admin_csrf)
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["firing_days_per_batch"] == 5

    # Cero dias no es un horno: es un error de configuracion.
    invalid = await api.patch(
        f"{KILNS}/{kiln_id}", json={"firing_days_per_batch": 0}, headers=head(admin_csrf)
    )
    assert invalid.status_code == 422, invalid.text


@pytest.mark.asyncio
async def test_small_kiln_3_days_y_large_kiln_4_days(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """SMALL_KILN_3_DAYS + LARGE_KILN_4_DAYS sobre el Cotizador real."""
    customer = await _customer(api, admin_csrf)
    product, recipe = await _product(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_days",
        dimensions={"width": "10", "height": "10", "length": "10"},  # 1000 cm3
    )
    small = await _kiln(
        api, admin_csrf, db_session, nombre="Pequeno 009C", capacidad="10000", dias=SMALL_KILN_DAYS
    )
    large = await _kiln(
        api, admin_csrf, db_session, nombre="Grande 009C", capacidad="10000", dias=LARGE_KILN_DAYS
    )

    for kiln, expected in ((small, SMALL_KILN_DAYS), (large, LARGE_KILN_DAYS)):
        payload = {
            "name": f"009C dias {kiln['code']}",
            "customer_id": customer["id"],
            "items": [
                _item(product, recipe, low_kiln_id=kiln["id"], high_kiln_selected=False)
            ],
        }
        body = (await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))).json()
        assert body["items"][0]["calculated_days"] == expected, (
            f"una hornada en {kiln['name']} debe durar {expected} dias"
        )


@pytest.mark.asyncio
async def test_mixed_kilns_total_days_no_usa_un_numero_global(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MIXED_KILNS_TOTAL_DAYS: el caso de CTZ-2026-000129 -> 93 dias, no 90.

    300 jarras de 10x10x15 = 450 000 cm3. Baja en el pequeno (17 000 cm3) son
    27 hornadas x 3 = 81 dias; alta en el grande (200 000 cm3) son 3 x 4 = 12.
    El calculo anterior sumaba 27 + 3 hornadas y multiplicaba por 3: 90.
    """
    customer = await _customer(api, admin_csrf)
    product, recipe = await _product(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_mixed",
        dimensions={"width": "10", "height": "10", "length": "15"},  # 1500 cm3
    )
    small = await _kiln(
        api, admin_csrf, db_session, nombre="Pequeno mixto", capacidad="17000", dias=SMALL_KILN_DAYS
    )
    large = await _kiln(
        api, admin_csrf, db_session, nombre="Grande mixto", capacidad="200000", dias=LARGE_KILN_DAYS
    )

    payload = {
        "name": "009C hornos mixtos",
        "customer_id": customer["id"],
        "items": [
            _item(
                product,
                recipe,
                quantity=300,
                low_kiln_id=small["id"],
                high_kiln_id=large["id"],
                factor_kiln_id=small["id"],
            )
        ],
    }
    body = (await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))).json()

    sessions = {s["firing_type"]: s for s in body["production_summary"]["sessions"]}
    assert sessions["LOW"]["batches"] == 27
    assert sessions["HIGH"]["batches"] == 3
    assert sessions["LOW"]["days"] == 81
    assert sessions["HIGH"]["days"] == 12
    assert body["production_summary"]["total_batches"] == 30
    assert body["items"][0]["calculated_days"] == 93, "30 hornadas x 3 daria 90"


@pytest.mark.asyncio
async def test_adjustment_days_separate(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """ADJUSTMENT_DAYS_SEPARATE.

    El ajuste manual es OTRO sumando: no representa la duracion del horno ni
    la corrige. Cambiarlo mueve `total_days` y deja `calculated_days` intacto.
    """
    customer = await _customer(api, admin_csrf)
    product, recipe = await _product(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_adj",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    kiln = await _kiln(
        api, admin_csrf, db_session, nombre="Ajuste 009C", capacidad="10000", dias=LARGE_KILN_DAYS
    )

    def payload(adjustment: int) -> dict[str, Any]:
        return {
            "name": "009C ajuste",
            "customer_id": customer["id"],
            "items": [
                _item(
                    product,
                    recipe,
                    low_kiln_id=kiln["id"],
                    high_kiln_selected=False,
                    days_adjustment=adjustment,
                )
            ],
        }

    base = (
        await api.post(f"{BUILDER}/preview", json=payload(0), headers=head(admin_csrf))
    ).json()["items"][0]
    adjusted = (
        await api.post(f"{BUILDER}/preview", json=payload(7), headers=head(admin_csrf))
    ).json()["items"][0]

    assert base["calculated_days"] == LARGE_KILN_DAYS
    # La quema no cambia porque el usuario ajuste: son cosas distintas.
    assert adjusted["calculated_days"] == LARGE_KILN_DAYS
    assert adjusted["days_adjustment"] == 7
    assert adjusted["total_days"] == base["total_days"] + 7


@pytest.mark.asyncio
async def test_confirmed_snapshot_immutable_y_draft_recalcula(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRMED_SNAPSHOT_IMMUTABLE + CONFIG_CHANGE_RECALCULATES_DRAFT_ONLY.

    Subir el horno de 4 a 6 dias es una decision hacia adelante: los
    borradores adoptan la regla vigente, y lo confirmado conserva la que se le
    cotizo al cliente.
    """
    customer = await _customer(api, admin_csrf)
    product, recipe = await _product(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_frozen",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    kiln = await _kiln(
        api, admin_csrf, db_session, nombre="Congelado 009C", capacidad="10000", dias=4
    )

    payload = {
        "name": "009C congelado",
        "customer_id": customer["id"],
        "items": [_item(product, recipe, low_kiln_id=kiln["id"], high_kiln_selected=False)],
    }

    confirmed_draft = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert confirmed_draft.status_code == 201, confirmed_draft.text
    confirmed_id = confirmed_draft.json()["id"]
    confirmed = await api.post(
        f"{BUILDER}/{confirmed_id}/confirm",
        json={"expected_updated_at": confirmed_draft.json()["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmed.status_code == 200, confirmed.text
    frozen_item = confirmed.json()["items"][0]
    assert frozen_item["calculated_days"] == 4
    # Requisito 8: el snapshot explica el numero sin volver a la configuracion.
    plan = frozen_item["production_snapshot"]["firing_plan"]
    assert plan[0]["firing_days_per_batch"] == 4
    assert plan[0]["required_batches"] == 1
    assert plan[0]["calculated_firing_days"] == 4
    assert plan[0]["kiln_id"] == kiln["id"]

    open_draft = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert open_draft.status_code == 201, open_draft.text
    draft_id = open_draft.json()["id"]

    # El taller reconfigura el horno DESPUES.
    changed = await api.patch(
        f"{KILNS}/{kiln['id']}", json={"firing_days_per_batch": 6}, headers=head(admin_csrf)
    )
    assert changed.status_code == 200, changed.text

    reread_confirmed = await api.get(f"{BUILDER}/{confirmed_id}", headers=head(admin_csrf))
    assert reread_confirmed.status_code == 200, reread_confirmed.text
    assert reread_confirmed.json()["items"][0]["calculated_days"] == 4, (
        "una cotizacion confirmada no se recalcula con la configuracion nueva"
    )
    assert (
        reread_confirmed.json()["items"][0]["production_snapshot"]["firing_plan"][0][
            "firing_days_per_batch"
        ]
        == 4
    )

    recalculated = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert recalculated.status_code == 200, recalculated.text
    assert recalculated.json()["items"][0]["calculated_days"] == 6, (
        "un borrador si adopta la regla vigente"
    )
    assert draft_id is not None
