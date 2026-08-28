"""Fase 009C — quemas opcionales, multi-hornada y dias, extremo a extremo.

La matematica pura vive en tests/unit/test_firing_batches.py; aqui se
comprueba que el Cotizador la usa de verdad: que baja y alta son
independientes, que el costo se multiplica por hornada, que los dias salen
a 3 por hornada y que nada de esto crea una quema real ni mueve inventario.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.firings import Firing
from app.models.inventory import StockMovement
from app.models.masters import Product
from tests.db.test_firings_api import FACTORES_CHICO, crear_horno
from tests.db.test_quotation_builder_api import _customer, head
from tests.db.test_quotations_api import _finished_product_and_recipe

BUILDER = "/api/v1/quotation-builder"
DAYS_PER_BATCH = 3


async def _setup(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    *,
    suffix: str,
    capacity: str,
    dimensions: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Cliente, producto con medidas fijas, receta y horno de capacidad dada."""
    customer = await _customer(api, csrf)
    product, recipe = await _finished_product_and_recipe(api, csrf, suffix)
    await db_session.execute(
        update(Product)
        .where(Product.id == product["id"])
        .values(**{field: Decimal(value) for field, value in dimensions.items()})
    )
    await db_session.commit()
    kiln = await crear_horno(
        api,
        csrf,
        db_session,
        nombre=f"Horno 009C{suffix}",
        capacidad=capacity,
        baja="400",
        alta="600",
        factores=FACTORES_CHICO,
    )
    return customer, product, recipe, kiln


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


def _sessions_by_type(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["firing_type"]: s for s in body["production_summary"]["sessions"]}


@pytest.mark.asyncio
async def test_low_only_no_cobra_ni_planifica_la_quema_alta(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """LOW_ONLY_ONE_BATCH: solo baja, una hornada, 3 dias."""
    customer, product, recipe, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_low",
        capacity="10000",
        dimensions={"width": "10", "height": "10", "length": "10"},  # 1000 cm3
    )
    payload = {
        "name": "009C solo baja",
        "customer_id": customer["id"],
        "items": [
            _item(
                product,
                recipe,
                low_kiln_id=kiln["id"],
                high_kiln_selected=False,
            )
        ],
    }
    response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert response.status_code == 200, response.text
    body = response.json()

    sessions = _sessions_by_type(body)
    assert set(sessions) == {"LOW"}, "no debe abrirse una sesion de quema alta"
    assert sessions["LOW"]["batches"] == 1
    assert body["production_summary"]["total_batches"] == 1
    # Una sola hornada de quema baja: se cobra su tarifa, no la de la alta.
    assert Decimal(sessions["LOW"]["subtotal"]) == Decimal("400")
    assert body["items"][0]["calculated_days"] == DAYS_PER_BATCH


@pytest.mark.asyncio
async def test_high_only_no_cobra_ni_planifica_la_quema_baja(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """HIGH_ONLY_ONE_BATCH."""
    customer, product, recipe, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_high",
        capacity="10000",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    payload = {
        "name": "009C solo alta",
        "customer_id": customer["id"],
        "items": [
            _item(
                product,
                recipe,
                high_kiln_id=kiln["id"],
                low_kiln_selected=False,
            )
        ],
    }
    response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert response.status_code == 200, response.text
    body = response.json()

    sessions = _sessions_by_type(body)
    assert set(sessions) == {"HIGH"}
    assert body["production_summary"]["total_batches"] == 1
    assert body["items"][0]["calculated_days"] == DAYS_PER_BATCH


@pytest.mark.asyncio
async def test_low_and_high_suman_hornadas_costos_y_dias(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """LOW_AND_HIGH: dos sesiones, dos hornadas, 6 dias."""
    customer, product, recipe, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_both",
        capacity="10000",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    payload = {
        "name": "009C baja y alta",
        "customer_id": customer["id"],
        "kiln_id": kiln["id"],
        "items": [_item(product, recipe)],
    }
    response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert response.status_code == 200, response.text
    body = response.json()

    sessions = _sessions_by_type(body)
    assert set(sessions) == {"LOW", "HIGH"}
    assert body["production_summary"]["total_batches"] == 2
    # 1 hornada baja + 1 alta = 2 hornadas = 6 dias.
    assert body["items"][0]["calculated_days"] == 2 * DAYS_PER_BATCH


@pytest.mark.asyncio
async def test_capacidad_exacta_es_una_hornada_y_uno_mas_son_dos(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """EXACT_CAPACITY_ONE_BATCH + CAPACITY_PLUS_ONE_TWO_BATCHES."""
    customer, product, recipe, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_exact",
        capacity="2000",  # exactamente 2 piezas de 1000 cm3
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    base = {
        "name": "009C capacidad exacta",
        "customer_id": customer["id"],
        "items": [_item(product, recipe, low_kiln_id=kiln["id"], high_kiln_selected=False)],
    }

    base["items"][0]["quantity"] = 2  # 2000 cm3 == capacidad
    exact = (await api.post(f"{BUILDER}/preview", json=base, headers=head(admin_csrf))).json()
    assert _sessions_by_type(exact)["LOW"]["batches"] == 1, "capacidad exacta = una hornada"

    base["items"][0]["quantity"] = 3  # 3000 cm3 > capacidad
    plus = (await api.post(f"{BUILDER}/preview", json=base, headers=head(admin_csrf))).json()
    assert _sessions_by_type(plus)["LOW"]["batches"] == 2


@pytest.mark.asyncio
async def test_multiples_hornadas_multiplican_costo_y_dias(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MULTIPLE_BATCHES_COST_MULTIPLIED + PRODUCTION_DAYS_THREE_PER_BATCH."""
    customer, product, recipe, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_multi",
        capacity="1000",  # una pieza por hornada
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    payload = {
        "name": "009C multi hornada",
        "customer_id": customer["id"],
        "items": [
            _item(
                product,
                recipe,
                quantity=3,  # 3000 cm3 -> 3 hornadas
                low_kiln_id=kiln["id"],
                high_kiln_selected=False,
            )
        ],
    }
    response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert response.status_code == 200, response.text
    body = response.json()

    session = _sessions_by_type(body)["LOW"]
    assert session["batches"] == 3
    # La tarifa de la sesion es el costo de UNA hornada: 3 hornadas cuestan
    # el triple (400 x 3 = 1200 de subtotal de la sesion).
    assert Decimal(session["subtotal"]) == Decimal("1200")
    assert body["items"][0]["calculated_days"] == 3 * DAYS_PER_BATCH


@pytest.mark.asyncio
async def test_dimensiones_efectivas_mandan_sobre_el_maestro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """EFFECTIVE_DIMENSIONS_USED: la medida personalizada cambia las hornadas."""
    customer, product, recipe, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_dims",
        capacity="1000",
        dimensions={"width": "10", "height": "10", "length": "10"},  # 1000 cm3
    )
    base = {
        "name": "009C dimensiones efectivas",
        "customer_id": customer["id"],
        "items": [_item(product, recipe, low_kiln_id=kiln["id"], high_kiln_selected=False)],
    }

    standard = (await api.post(f"{BUILDER}/preview", json=base, headers=head(admin_csrf))).json()
    assert _sessions_by_type(standard)["LOW"]["batches"] == 1

    # Misma cantidad, pieza el doble de grande en cada eje: 8000 cm3 -> 8.
    base["items"][0]["dimensions"] = {"width": "20", "height": "20", "length": "20"}
    base["items"][0]["dimensions_overridden"] = True
    custom = (await api.post(f"{BUILDER}/preview", json=base, headers=head(admin_csrf))).json()
    assert _sessions_by_type(custom)["LOW"]["batches"] == 8
    assert custom["items"][0]["calculated_days"] == 8 * DAYS_PER_BATCH

    # Y el maestro sigue intacto (regresion de Fase 009B).
    master = (await api.get(f"/api/v1/products/{product['id']}")).json()
    assert Decimal(master["width"]) == Decimal("10")


@pytest.mark.asyncio
async def test_multiproducto_comparte_hornada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MULTIPRODUCT_CAPACITY: dos piezas que caben juntas son UNA hornada."""
    customer, product_a, recipe_a, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_multi_a",
        capacity="10000",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    product_b, recipe_b = await _finished_product_and_recipe(api, admin_csrf, "_009c_multi_b")
    await db_session.execute(
        update(Product)
        .where(Product.id == product_b["id"])
        .values(width=Decimal(10), height=Decimal(10), length=Decimal(10))
    )
    await db_session.commit()

    payload = {
        "name": "009C multiproducto",
        "customer_id": customer["id"],
        "items": [
            _item(product_a, recipe_a, low_kiln_id=kiln["id"], high_kiln_selected=False),
            _item(
                product_b,
                recipe_b,
                low_kiln_id=kiln["id"],
                high_kiln_selected=False,
                sort_order=1,
            ),
        ],
    }
    response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert response.status_code == 200, response.text
    body = response.json()
    # 2000 cm3 en un horno de 10000: caben en la misma hornada.
    assert _sessions_by_type(body)["LOW"]["batches"] == 1


@pytest.mark.asyncio
async def test_draft_reabierto_conserva_la_seleccion_de_quemas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DRAFT_SAVE_REOPEN: un borrador de solo-baja no resucita como baja+alta."""
    customer, product, recipe, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_reopen",
        capacity="10000",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    payload = {
        "name": "009C reabrir",
        "customer_id": customer["id"],
        # kiln_id de cabecera presente a proposito: reabrir NO debe usarlo
        # para inventar la quema alta que el usuario no pidio.
        "kiln_id": kiln["id"],
        "items": [_item(product, recipe, low_kiln_id=kiln["id"], high_kiln_selected=False)],
    }
    created = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert created.status_code == 201, created.text

    reopened = (await api.get(f"{BUILDER}/{created.json()['id']}")).json()
    assert set(_sessions_by_type(reopened)) == {"LOW"}
    assert reopened["production_summary"]["total_batches"] == 1
    assert reopened["items"][0]["calculated_days"] == DAYS_PER_BATCH


@pytest.mark.asyncio
async def test_confirmar_congela_la_planificacion_aunque_cambie_la_tarifa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRMED_SNAPSHOT_IMMUTABLE + CONFIG_CHANGE_DOES_NOT_CHANGE_CONFIRMED."""
    customer, product, recipe, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_freeze",
        capacity="1000",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    payload = {
        "name": "009C congelar",
        "customer_id": customer["id"],
        "items": [
            _item(
                product,
                recipe,
                quantity=3,  # 3 hornadas
                low_kiln_id=kiln["id"],
                high_kiln_selected=False,
                commercial_sale_unit_price="900",
            )
        ],
    }
    created = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["complete"] is True

    confirmed = await api.post(
        f"{BUILDER}/{draft['id']}/confirm",
        json={"expected_updated_at": draft["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmed.status_code == 200, confirmed.text
    frozen_days = confirmed.json()["items"][0]["calculated_days"]
    frozen_cost = Decimal(confirmed.json()["items"][0]["firing_cost"])
    assert frozen_days == 3 * DAYS_PER_BATCH

    # La tarifa del horno sube DESPUES de confirmar.
    raised = await api.post(
        f"/api/v1/kilns/{kiln['id']}/rates",
        json={"firing_type": "LOW", "rate": "650", "valid_from": "2026-06-01"},
        headers=head(admin_csrf),
    )
    assert raised.status_code == 201, raised.text

    reopened = (await api.get(f"{BUILDER}/{draft['id']}")).json()
    assert reopened["items"][0]["calculated_days"] == frozen_days
    assert Decimal(reopened["items"][0]["firing_cost"]) == frozen_cost


@pytest.mark.asyncio
async def test_planificar_no_crea_quemas_reales_ni_mueve_inventario(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NO_REAL_FIRING_CREATED + NO_INVENTORY_MUTATION."""
    firings_before = int((await db_session.execute(select(func.count(Firing.id)))).scalar_one())
    movements_before = int(
        (await db_session.execute(select(func.count(StockMovement.id)))).scalar_one()
    )

    customer, product, recipe, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_nomut",
        capacity="1000",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    payload = {
        "name": "009C sin mutacion",
        "customer_id": customer["id"],
        "items": [
            _item(
                product,
                recipe,
                quantity=5,  # 5 hornadas: mucha planificacion, cero realidad
                low_kiln_id=kiln["id"],
                high_kiln_selected=False,
                commercial_sale_unit_price="900",
            )
        ],
    }
    created = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert created.status_code == 201, created.text
    draft = created.json()
    confirmed = await api.post(
        f"{BUILDER}/{draft['id']}/confirm",
        json={"expected_updated_at": draft["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["items"][0]["calculated_days"] == 5 * DAYS_PER_BATCH

    firings_after = int((await db_session.execute(select(func.count(Firing.id)))).scalar_one())
    movements_after = int(
        (await db_session.execute(select(func.count(StockMovement.id)))).scalar_one()
    )
    assert firings_after == firings_before, "cotizar NO puede crear una quema real"
    assert movements_after == movements_before, "cotizar NO puede mover inventario"


@pytest.mark.asyncio
async def test_sin_ninguna_quema_seleccionada_se_bloquea(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NO_FIRING_ALLOWED: NO — al menos una quema sigue siendo obligatoria."""
    customer, product, recipe, _kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_none",
        capacity="10000",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    payload = {
        "name": "009C sin quema",
        "customer_id": customer["id"],
        "items": [
            _item(product, recipe, low_kiln_selected=False, high_kiln_selected=False),
        ],
    }
    response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert response.status_code == 200, response.text
    body = response.json()
    assert "FIRING_REQUIRED" in body["warnings"]
    assert body["complete"] is False


# ---------------------------------------------------------------------------
# Regresiones de la revision automatica del PR #15 (Fase 009C)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_desmarcar_una_quema_manda_sobre_el_horno_que_quedo_en_el_payload(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La INTENCION manda: un horno olvidado en el payload no resucita la quema."""
    customer, product, recipe, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_flag",
        capacity="10000",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    payload = {
        "name": "009C flag manda",
        "customer_id": customer["id"],
        "items": [
            _item(
                product,
                recipe,
                low_kiln_id=kiln["id"],
                # El id de la quema alta sigue ahi de una seleccion anterior,
                # pero el usuario la desmarco: no debe cobrarse ni planificarse.
                high_kiln_id=kiln["id"],
                high_kiln_selected=False,
            )
        ],
    }
    response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(_sessions_by_type(body)) == {"LOW"}
    assert body["production_summary"]["total_batches"] == 1
    assert body["items"][0]["calculated_days"] == DAYS_PER_BATCH


@pytest.mark.asyncio
async def test_una_quema_pedida_sin_horno_bloquea_en_vez_de_omitirse(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Un payload previo a 009C no puede colar una cotizacion sin la quema alta."""
    customer, product, recipe, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_nokiln",
        capacity="10000",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    payload = {
        "name": "009C quema sin horno",
        "customer_id": customer["id"],
        # Sin kiln_id de cabecera: la quema alta queda pedida (default True)
        # pero sin horno con el que hacerse.
        "items": [_item(product, recipe, low_kiln_id=kiln["id"])],
    }
    response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert response.status_code == 200, response.text
    body = response.json()
    assert "FIRING_KILN_REQUIRED" in body["warnings"]
    assert body["complete"] is False


@pytest.mark.asyncio
async def test_las_hornadas_de_otra_linea_no_inflan_los_dias_de_esta(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Las sesiones se emparejan por (horno, tipo), no por producto cartesiano."""
    customer, product_a, recipe_a, kiln_1 = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_cross_a",
        capacity="10000",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    kiln_2 = await crear_horno(
        api,
        admin_csrf,
        db_session,
        nombre="Horno 009C cruzado",
        capacidad="10000",
        baja="400",
        alta="600",
        factores=FACTORES_CHICO,
    )
    product_b, recipe_b = await _finished_product_and_recipe(api, admin_csrf, "_009c_cross_b")
    await db_session.execute(
        update(Product)
        .where(Product.id == product_b["id"])
        .values(width=Decimal(10), height=Decimal(10), length=Decimal(10))
    )
    await db_session.commit()

    payload = {
        "name": "009C rutas cruzadas",
        "customer_id": customer["id"],
        "items": [
            # Linea A: baja en horno 1, alta en horno 2.
            _item(
                product_a,
                recipe_a,
                low_kiln_id=kiln_1["id"],
                high_kiln_id=kiln_2["id"],
            ),
            # Linea B: al reves. Sus sesiones (horno 2, BAJA) y (horno 1, ALTA)
            # NO deben contarse en la linea A.
            _item(
                product_b,
                recipe_b,
                low_kiln_id=kiln_2["id"],
                high_kiln_id=kiln_1["id"],
                sort_order=1,
            ),
        ],
    }
    response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert response.status_code == 200, response.text
    body = response.json()
    # Hay 4 sesiones en la hoja, pero cada linea solo usa 2 -> 6 dias, no 12.
    assert len(body["production_summary"]["sessions"]) == 4
    for item_out in body["items"]:
        assert item_out["calculated_days"] == 2 * DAYS_PER_BATCH


@pytest.mark.asyncio
async def test_duplicar_un_borrador_incompleto_conserva_la_seleccion_de_quemas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La intencion se persiste, no se deduce de una simulacion que no corrio."""
    customer, product, recipe, kiln = await _setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009c_incomplete",
        capacity="10000",
        dimensions={"width": "10", "height": "10", "length": "10"},
    )
    # Sin cantidad: la simulacion sale temprano y no deja low/high_kiln_id.
    incomplete = _item(product, recipe, low_kiln_id=kiln["id"], high_kiln_selected=False)
    del incomplete["quantity"]
    payload = {
        "name": "009C incompleto",
        "customer_id": customer["id"],
        "items": [incomplete],
    }
    created = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert created.status_code == 201, created.text

    duplicated = await api.post(
        f"{BUILDER}/{created.json()['id']}/duplicate", headers=head(admin_csrf)
    )
    assert duplicated.status_code == 200, duplicated.text
    # La copia conserva "solo quema baja": no aparece FIRING_REQUIRED ni
    # resucita la quema alta.
    assert "FIRING_REQUIRED" not in duplicated.json()["warnings"]
