"""Fase 009D — preparar una receta, extremo a extremo.

La matematica pura vive en tests/unit/test_preparations_math.py. Aqui se
comprueba lo unico que no se puede comprobar sin base de datos: que la
transformacion materia prima -> preparado ocurre de verdad, entera o nada, una
sola vez, y sin dejar existencias en negativo.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import MovementType, StockBalance, StockMovement
from tests.db.test_masters_api import create_category, create_product
from tests.db.test_quotation_builder_api import head

RECIPES = "/api/v1/recipes"
PREPARATIONS = "/api/v1/recipe-preparations"
INVENTORY = "/api/v1/inventory"
LOCATIONS = f"{INVENTORY}/locations"
ADJUSTMENTS = f"{INVENTORY}/adjustments"


async def _scenario(
    api: httpx.AsyncClient, csrf: str, *, suffix: str, stock_g: str = "10000"
) -> dict[str, Any]:
    """Dos insumos con existencias, un preparado y una receta 50/50.

    Los porcentajes son 50/50 porque asi el reparto es evidente al leer el
    test; no representan ninguna formula real del taller.
    """
    category = await create_category(api, csrf, f"Insumos 009D{suffix}")

    componentes = []
    for nombre, costo in (("Insumo A", "0.20"), ("Insumo B", "0.30")):
        response = await create_product(
            api,
            csrf,
            product_category_id=category["id"],
            name=f"{nombre} 009D{suffix}",
            product_type="RAW_MATERIAL",
            base_uom_code="g",
            cost=costo,
        )
        assert response.status_code == 201, response.text
        componentes.append(response.json())

    prepared = await create_product(
        api,
        csrf,
        product_category_id=category["id"],
        name=f"Preparado 009D{suffix}",
        product_type="PREPARED_MATERIAL",
        base_uom_code="g",
    )
    assert prepared.status_code == 201, prepared.text

    recipe = await api.post(
        RECIPES,
        json={
            "product_id": prepared.json()["id"],
            "name": f"Formula 009D{suffix}",
            "lines": [
                {
                    "component_product_id": componentes[0]["id"],
                    "component_type": "BASE",
                    "percentage": "50",
                    "sort_order": 0,
                },
                {
                    "component_product_id": componentes[1]["id"],
                    "component_type": "BASE",
                    "percentage": "50",
                    "sort_order": 1,
                },
            ],
            "active": True,
            "activate_immediately": True,
        },
        headers=head(csrf),
    )
    assert recipe.status_code == 201, recipe.text

    location = await api.post(
        LOCATIONS, json={"name": f"Deposito 009D{suffix}"}, headers=head(csrf)
    )
    assert location.status_code == 201, location.text
    location_id = location.json()["id"]

    for componente in componentes:
        adjust = await api.post(
            ADJUSTMENTS,
            json={
                "product_id": componente["id"],
                "location_id": location_id,
                "quantity": stock_g,
                "reason": "Carga de prueba 009D",
            },
            headers=head(csrf),
        )
        assert adjust.status_code == 201, adjust.text

    return {
        "components": componentes,
        "prepared": prepared.json(),
        "recipe": recipe.json(),
        "location_id": location_id,
    }


def _payload(scenario: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    base = {
        "recipe_version_id": scenario["recipe"]["current_version"]["id"],
        "location_id": scenario["location_id"],
        "total_dry_weight_g": "1000",
        "water_amount_ml": "800",
        "final_yield_ml": "1500",
        "idempotency_key": "prep-009d-referencia",
    }
    base.update(overrides)
    return base


async def _balance(session: AsyncSession, product_id: int, location_id: int) -> Decimal:
    value = await session.scalar(
        select(StockBalance.quantity).where(
            StockBalance.product_id == product_id, StockBalance.location_id == location_id
        )
    )
    return value if value is not None else Decimal(0)


@pytest.mark.asyncio
async def test_preparar_transforma_materia_prima_en_preparado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """RAW_STOCK_DECREASE + PREPARED_STOCK_INCREASE + BATCH_COST_SNAPSHOT."""
    scenario = await _scenario(api, admin_csrf, suffix="_ok")
    a, b = scenario["components"]
    location_id = scenario["location_id"]

    antes_a = await _balance(db_session, a["id"], location_id)
    antes_b = await _balance(db_session, b["id"], location_id)
    antes_prep = await _balance(db_session, scenario["prepared"]["id"], location_id)

    response = await api.post(
        PREPARATIONS, json=_payload(scenario, idempotency_key="prep-ok-1"), headers=head(admin_csrf)
    )
    assert response.status_code == 201, response.text
    body = response.json()

    # PREPARATION_CODE_BACKEND: el cliente no mando ningun codigo.
    assert body["code"].startswith("PREP-"), body["code"]

    # 1000 g al 50/50 -> 500 g de cada insumo.
    assert Decimal(body["total_dry_weight_g"]) == Decimal(1000)
    quantities = {
        line["component_product_id"]: Decimal(line["quantity_g"]) for line in body["lines"]
    }
    assert quantities[a["id"]] == Decimal(500)
    assert quantities[b["id"]] == Decimal(500)

    # 500 x 0,20 + 500 x 0,30 = 250. Y las lineas reconcilian con el total.
    assert Decimal(body["batch_total_cost"]) == Decimal(250)
    assert sum(Decimal(line["line_cost"]) for line in body["lines"]) == Decimal(250)

    # Rendimiento 1500 ml: 1000/1500 g/ml y 250/1500 por ml.
    assert Decimal(body["solids_g_per_ml"]) == Decimal("0.666666666667")
    assert Decimal(body["unit_cost_per_ml"]) == Decimal("0.166666666667")

    db_session.expire_all()
    assert await _balance(db_session, a["id"], location_id) == antes_a - Decimal(500)
    assert await _balance(db_session, b["id"], location_id) == antes_b - Decimal(500)
    # El preparado se lleva en gramos: entran los solidos, no el agua.
    assert await _balance(
        db_session, scenario["prepared"]["id"], location_id
    ) == antes_prep + Decimal(1000)

    movimientos = await db_session.execute(
        select(StockMovement.movement_type, StockMovement.preparation_id).where(
            StockMovement.preparation_id == body["id"]
        )
    )
    tipos = [row[0] for row in movimientos.all()]
    assert tipos.count(MovementType.PREPARATION_OUT) == 2
    assert tipos.count(MovementType.PREPARATION_IN) == 1


@pytest.mark.asyncio
async def test_stock_insuficiente_no_toca_nada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """INSUFFICIENT_STOCK_ROLLBACK + PREPARATION_ATOMIC.

    El segundo insumo no alcanza. Lo que se comprueba no es solo que falle,
    sino que el PRIMERO tampoco se descuente: si se validara ingrediente a
    ingrediente mientras se descuenta, la mitad del lote se habria consumido
    para nada.
    """
    scenario = await _scenario(api, admin_csrf, suffix="_falta", stock_g="400")
    a, b = scenario["components"]
    location_id = scenario["location_id"]

    antes_a = await _balance(db_session, a["id"], location_id)
    antes_b = await _balance(db_session, b["id"], location_id)
    movimientos_antes = await db_session.scalar(select(func.count()).select_from(StockMovement))

    response = await api.post(
        PREPARATIONS,
        json=_payload(scenario, idempotency_key="prep-falta-1"),
        headers=head(admin_csrf),
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "PREPARATION_INSUFFICIENT_STOCK"
    # Se dice QUE falta y CUANTO, sin exponer nada mas.
    detalle = error["details"][0]
    assert set(detalle) == {"product_id", "internal_reference", "name", "required", "available"}
    assert Decimal(detalle["required"]) == Decimal(500)
    assert Decimal(detalle["available"]) == Decimal(400)

    db_session.expire_all()
    assert await _balance(db_session, a["id"], location_id) == antes_a
    assert await _balance(db_session, b["id"], location_id) == antes_b
    assert await db_session.scalar(select(func.count()).select_from(StockMovement)) == (
        movimientos_antes
    )


@pytest.mark.asyncio
async def test_la_misma_clave_no_prepara_dos_veces(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """IDEMPOTENCY: reintentar devuelve el mismo lote y no descuenta otra vez."""
    scenario = await _scenario(api, admin_csrf, suffix="_idem")
    a = scenario["components"][0]
    location_id = scenario["location_id"]

    primera = await api.post(
        PREPARATIONS,
        json=_payload(scenario, idempotency_key="prep-idem-1"),
        headers=head(admin_csrf),
    )
    assert primera.status_code == 201, primera.text
    db_session.expire_all()
    tras_primera = await _balance(db_session, a["id"], location_id)

    segunda = await api.post(
        PREPARATIONS,
        json=_payload(scenario, idempotency_key="prep-idem-1"),
        headers=head(admin_csrf),
    )
    # 200, no 201: ya estaba hecho. Y no es un error: el reintento es legitimo.
    assert segunda.status_code == 200, segunda.text
    assert segunda.json()["id"] == primera.json()["id"]
    assert segunda.json()["code"] == primera.json()["code"]

    db_session.expire_all()
    assert await _balance(db_session, a["id"], location_id) == tras_primera

    total = await db_session.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.preparation_id == primera.json()["id"])
    )
    assert total == 3, "dos salidas y una entrada, no seis"


@pytest.mark.asyncio
async def test_dos_envios_simultaneos_con_la_misma_clave(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """IDEMPOTENCY bajo concurrencia.

    Un doble clic manda las dos peticiones a la vez. Sin serializar, ambas
    pasarian la comprobacion de existencia y una moriria contra el UNIQUE con
    un error de integridad. Se exige que las dos terminen bien y que solo haya
    un lote.
    """
    scenario = await _scenario(api, admin_csrf, suffix="_idem_conc")
    payload = _payload(scenario, idempotency_key="prep-idem-concurrente")

    first, second = await asyncio.gather(
        api.post(PREPARATIONS, json=payload, headers=head(admin_csrf)),
        api.post(PREPARATIONS, json=payload, headers=head(admin_csrf)),
    )
    assert {first.status_code, second.status_code} <= {200, 201}, (
        f"{first.status_code}/{second.status_code} -> {first.text} | {second.text}"
    )
    assert first.json()["id"] == second.json()["id"]
    # Exactamente una de las dos lo creo.
    assert sorted([first.status_code, second.status_code]) == [200, 201]


@pytest.mark.asyncio
async def test_dos_preparaciones_simultaneas_no_dejan_stock_negativo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONCURRENT_PREPARATION + NO_NEGATIVE_STOCK.

    1000 g disponibles y dos preparaciones que piden 700 g cada una. Juntas
    necesitarian 1400. Una debe poder hacerse; la otra debe recibir un
    conflicto claro. Lo que jamas puede pasar es que el saldo quede negativo.
    """
    scenario = await _scenario(api, admin_csrf, suffix="_conc", stock_g="1000")
    a, b = scenario["components"]
    location_id = scenario["location_id"]

    # 1400 g al 50/50 son 700 de cada insumo.
    first, second = await asyncio.gather(
        api.post(
            PREPARATIONS,
            json=_payload(scenario, total_dry_weight_g="1400", idempotency_key="prep-conc-a"),
            headers=head(admin_csrf),
        ),
        api.post(
            PREPARATIONS,
            json=_payload(scenario, total_dry_weight_g="1400", idempotency_key="prep-conc-b"),
            headers=head(admin_csrf),
        ),
    )
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [201, 409], f"{statuses} -> {first.text} | {second.text}"

    db_session.expire_all()
    for componente in (a, b):
        saldo = await _balance(db_session, componente["id"], location_id)
        assert saldo >= 0, f"{componente['internal_reference']} quedo en {saldo}"
        assert saldo == Decimal(300)


@pytest.mark.asyncio
async def test_el_costo_del_lote_no_se_recalcula_si_cambia_el_precio(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """HISTORICAL_COST_IMMUTABLE."""
    scenario = await _scenario(api, admin_csrf, suffix="_hist")
    a = scenario["components"][0]

    creada = await api.post(
        PREPARATIONS,
        json=_payload(scenario, idempotency_key="prep-hist-1"),
        headers=head(admin_csrf),
    )
    assert creada.status_code == 201, creada.text
    costo_original = Decimal(creada.json()["batch_total_cost"])

    # El insumo se encarece DESPUES de preparar.
    subida = await api.put(
        f"/api/v1/products/{a['id']}",
        json={
            "name": a["name"],
            "product_type": "RAW_MATERIAL",
            "product_category_id": a["product_category_id"],
            "base_uom_code": "g",
            "cost": "99",
        },
        headers=head(admin_csrf),
    )
    assert subida.status_code == 200, subida.text

    releida = await api.get(f"{PREPARATIONS}/{creada.json()['id']}", headers=head(admin_csrf))
    assert releida.status_code == 200, releida.text
    assert Decimal(releida.json()["batch_total_cost"]) == costo_original
    linea = next(
        line for line in releida.json()["lines"] if line["component_product_id"] == a["id"]
    )
    assert Decimal(linea["unit_cost_snapshot"]) == Decimal("0.20"), (
        "el costo congelado no debe seguir al maestro"
    )


@pytest.mark.asyncio
async def test_conversion_g_ml_usa_la_concentracion_del_lote(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """G_TO_ML + ML_TO_G contra una preparacion real."""
    scenario = await _scenario(api, admin_csrf, suffix="_conv")
    # 200 g secos con rendimiento 1000 ml -> 0,2 g/ml, el caso del enunciado.
    creada = await api.post(
        PREPARATIONS,
        json=_payload(
            scenario,
            total_dry_weight_g="200",
            water_amount_ml="900",
            final_yield_ml="1000",
            idempotency_key="prep-conv-1",
        ),
        headers=head(admin_csrf),
    )
    assert creada.status_code == 201, creada.text
    preparation_id = creada.json()["id"]
    assert Decimal(creada.json()["solids_g_per_ml"]) == Decimal("0.200000000000")

    a_ml = await api.post(
        f"{PREPARATIONS}/convert",
        json={"preparation_id": preparation_id, "value": "75", "from_unit": "g"},
        headers=head(admin_csrf),
    )
    assert a_ml.status_code == 200, a_ml.text
    assert Decimal(a_ml.json()["converted"]) == Decimal(375)
    assert a_ml.json()["to_unit"] == "ml"

    a_g = await api.post(
        f"{PREPARATIONS}/convert",
        json={"preparation_id": preparation_id, "value": "375", "from_unit": "ml"},
        headers=head(admin_csrf),
    )
    assert a_g.status_code == 200, a_g.text
    assert Decimal(a_g.json()["converted"]) == Decimal(75)


@pytest.mark.asyncio
async def test_una_receta_que_no_produce_material_preparado_se_rechaza(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """Preparar solo tiene sentido si el resultado es un PREPARED_MATERIAL."""
    scenario = await _scenario(api, admin_csrf, suffix="_tipo")
    # Se degrada el producto de la receta a materia prima.
    prepared = scenario["prepared"]
    degradado = await api.put(
        f"/api/v1/products/{prepared['id']}",
        json={
            "name": prepared["name"],
            "product_type": "RAW_MATERIAL",
            "product_category_id": prepared["product_category_id"],
            "base_uom_code": "g",
        },
        headers=head(admin_csrf),
    )
    assert degradado.status_code == 200, degradado.text

    response = await api.post(
        PREPARATIONS,
        json=_payload(scenario, idempotency_key="prep-tipo-1"),
        headers=head(admin_csrf),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PREPARATION_INVALID"


@pytest.mark.asyncio
async def test_cotizar_no_mueve_ni_materia_prima_ni_preparado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """QUOTATION_SIMULATION_MUTATES_INVENTORY: NO.

    Cotizar es simular. El descuento real de material al vender pertenece a
    009H, y mezclarlo aqui haria que pedir un presupuesto vaciara el almacen.
    Se comprueba sobre los saldos de las DOS familias, no solo la materia
    prima: un preparado consumido en silencio seria igual de grave.
    """
    from tests.db.test_quotation_builder_api import _customer

    scenario = await _scenario(api, admin_csrf, suffix="_cotiza")
    a, b = scenario["components"]
    prepared = scenario["prepared"]
    location_id = scenario["location_id"]

    # Se prepara un lote para que exista stock preparado que vigilar.
    creada = await api.post(
        PREPARATIONS,
        json=_payload(scenario, idempotency_key="prep-cotiza-1"),
        headers=head(admin_csrf),
    )
    assert creada.status_code == 201, creada.text

    db_session.expire_all()
    antes = {
        producto["id"]: await _balance(db_session, producto["id"], location_id)
        for producto in (a, b, prepared)
    }
    movimientos_antes = await db_session.scalar(select(func.count()).select_from(StockMovement))

    customer = await _customer(api, admin_csrf)
    finished = await create_product(
        api,
        admin_csrf,
        product_category_id=a["product_category_id"],
        product_type="FINISHED_PRODUCT",
        name="Pieza 009D cotiza",
        base_uom_code="unit",
        sellable=True,
        sale_price="100",
    )
    assert finished.status_code == 201, finished.text

    payload = {
        "name": "009D simulacion",
        "customer_id": customer["id"],
        "items": [
            {
                "product_id": finished.json()["id"],
                "quantity": 20,
                "dimensions": {},
                "techniques": [],
                "additionals": [],
                "days_adjustment": 0,
                "waiting_days": 0,
                "markup_percent": "100",
                "sort_order": 0,
                "recipe_id": scenario["recipe"]["id"],
                "recipe_version_id": scenario["recipe"]["current_version"]["id"],
                "material_grams_per_piece": "75",
            }
        ],
    }
    preview = await api.post(
        "/api/v1/quotation-builder/preview", json=payload, headers=head(admin_csrf)
    )
    assert preview.status_code == 200, preview.text
    guardada = await api.post("/api/v1/quotation-builder", json=payload, headers=head(admin_csrf))
    assert guardada.status_code == 201, guardada.text
    reabierta = await api.get(
        f"/api/v1/quotation-builder/{guardada.json()['id']}", headers=head(admin_csrf)
    )
    assert reabierta.status_code == 200, reabierta.text

    db_session.expire_all()
    for producto in (a, b, prepared):
        assert await _balance(db_session, producto["id"], location_id) == antes[producto["id"]], (
            f"cotizar movio el stock de {producto['internal_reference']}"
        )
    assert await db_session.scalar(select(func.count()).select_from(StockMovement)) == (
        movimientos_antes
    )
