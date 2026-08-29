"""Fase 009D — el 15 %, la conversion g/ml y el reparto entre varios esmaltes.

La matematica pura ya esta cubierta en tests/unit/test_preparations_math.py.
Aqui se comprueba lo que solo se ve extremo a extremo: que el porcentaje sale
de la configuracion y no de una constante, que la conversion usa la
concentracion del lote elegido, y que estimar no mueve ni un gramo de almacen.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockBalance, StockMovement
from tests.db.test_quotation_builder_api import BUILDER, _complete_payload, head
from tests.db.test_recipe_preparations_api import _scenario

COMMERCIAL = "/api/v1/settings/commercial"
PREPARATIONS = "/api/v1/recipe-preparations"
ESTIMATE = f"{PREPARATIONS}/glaze-estimate"


async def _lote(
    api: httpx.AsyncClient,
    csrf: str,
    *,
    suffix: str,
    final_yield_ml: str,
) -> dict[str, Any]:
    """Un esmalte preparado listo para usarse en una estimacion.

    1000 g secos que cuestan 250 (50 % a 0,20 + 50 % a 0,30). El rendimiento
    lo decide quien llama, que es justo lo que cambia la concentracion.
    """
    scenario = await _scenario(api, csrf, suffix=suffix)
    response = await api.post(
        PREPARATIONS,
        json={
            "recipe_version_id": scenario["recipe"]["current_version"]["id"],
            "location_id": scenario["location_id"],
            "total_dry_weight_g": "1000",
            "water_amount_ml": "4200",
            "final_yield_ml": final_yield_ml,
            "idempotency_key": f"glaze{suffix}",
        },
        headers=head(csrf),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _inventory_fingerprint(session: AsyncSession) -> tuple[int, list[tuple[int, int, str]]]:
    """Movimientos y saldos. Si estimar tocara inventario, esto cambiaria."""
    session.expire_all()
    movements = await session.scalar(select(func.count()).select_from(StockMovement))
    balances = (
        await session.execute(
            select(
                StockBalance.product_id,
                StockBalance.location_id,
                StockBalance.quantity,
            ).order_by(StockBalance.product_id, StockBalance.location_id)
        )
    ).all()
    return int(movements or 0), [(p, loc, str(q)) for p, loc, q in balances]


# ---------------------------------------------------------------------------
# El 15 % y su origen
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_caso_del_enunciado_quinientos_gramos_por_veinte_piezas(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """ESTIMATED_15_PERCENT: 500 g x 15 % x 20 = 1500 g."""
    response = await api.post(
        ESTIMATE,
        json={"piece_weight_g": "500", "quantity": 20},
        headers=head(admin_csrf),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["estimated_glaze_percent"]) == Decimal(15)
    assert Decimal(body["grams_per_piece"]) == Decimal(75)
    assert Decimal(body["total_estimated_grams"]) == Decimal(1500)


@pytest.mark.asyncio
async def test_el_porcentaje_sale_de_la_configuracion_no_de_una_constante(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """Cambiar la configuracion cambia la estimacion.

    Es la prueba de que 15 no esta escrito en el codigo del calculo: al pasar
    la configuracion a 20, los mismos 500 g x 20 piezas pasan de 1500 a 2000.
    """
    cambio = await api.put(
        COMMERCIAL,
        json={"version": 1, "estimated_glaze_percent": "20"},
        headers=head(admin_csrf),
    )
    assert cambio.status_code == 200, cambio.text

    response = await api.post(
        ESTIMATE,
        json={"piece_weight_g": "500", "quantity": 20},
        headers=head(admin_csrf),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["estimated_glaze_percent"]) == Decimal(20)
    assert Decimal(body["total_estimated_grams"]) == Decimal(2000)


@pytest.mark.asyncio
async def test_el_cliente_no_puede_imponer_su_propio_porcentaje(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """El porcentaje es autoridad del backend: enviarlo se rechaza."""
    response = await api.post(
        ESTIMATE,
        json={"piece_weight_g": "500", "quantity": 20, "estimated_glaze_percent": "90"},
        headers=head(admin_csrf),
    )

    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# g <-> ml sobre un lote concreto
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_los_gramos_se_convierten_con_la_concentracion_del_lote(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """PREPARED_MATERIAL + GRAMS_TO_ML: nunca densidad 1.

    1000 g secos en 5000 ml dan 0,2 g/ml. Los 1500 g estimados son 7500 ml de
    esmalte preparado, no 1500. Y cuestan 7500 x (250/5000) = 375.
    """
    lote = await _lote(api, admin_csrf, suffix="_conv", final_yield_ml="5000")
    assert Decimal(lote["solids_g_per_ml"]) == Decimal("0.2")
    assert Decimal(lote["unit_cost_per_ml"]) == Decimal("0.05")

    response = await api.post(
        ESTIMATE,
        json={
            "piece_weight_g": "500",
            "quantity": 20,
            "glazes": [{"preparation_id": lote["id"]}],
        },
        headers=head(admin_csrf),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    asignacion = body["allocations"][0]
    assert Decimal(asignacion["grams"]) == Decimal(1500)
    assert Decimal(asignacion["solids_g_per_ml"]) == Decimal("0.2")
    assert Decimal(asignacion["millilitres"]) == Decimal(7500)
    assert Decimal(asignacion["millilitres"]) != Decimal(1500)
    assert Decimal(asignacion["estimated_cost"]) == Decimal(375)
    assert Decimal(body["total_estimated_cost"]) == Decimal(375)


@pytest.mark.asyncio
async def test_dos_lotes_del_mismo_esmalte_convierten_distinto(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """La concentracion vive en el lote, no en la unidad.

    Mismo peso seco y mas agua: los mismos gramos ocupan mas mililitros. Si la
    conversion viviera en `units_of_measure` ambos lotes darian lo mismo.
    """
    flojo = await _lote(api, admin_csrf, suffix="_flojo", final_yield_ml="10000")
    espeso = await _lote(api, admin_csrf, suffix="_espeso", final_yield_ml="5000")

    async def millilitros(preparation_id: int) -> Decimal:
        response = await api.post(
            ESTIMATE,
            json={
                "piece_weight_g": "500",
                "quantity": 20,
                "glazes": [{"preparation_id": preparation_id}],
            },
            headers=head(admin_csrf),
        )
        assert response.status_code == 200, response.text
        return Decimal(response.json()["allocations"][0]["millilitres"])

    assert await millilitros(flojo["id"]) == Decimal(15000)
    assert await millilitros(espeso["id"]) == Decimal(7500)


@pytest.mark.asyncio
async def test_la_conversion_directa_ida_y_vuelta(api: httpx.AsyncClient, admin_csrf: str) -> None:
    """El endpoint de conversion se apoya en el mismo lote y reconcilia."""
    lote = await _lote(api, admin_csrf, suffix="_ida", final_yield_ml="5000")

    ida = await api.post(
        f"{PREPARATIONS}/convert",
        json={"preparation_id": lote["id"], "value": "75", "from_unit": "g"},
        headers=head(admin_csrf),
    )
    assert ida.status_code == 200, ida.text
    assert Decimal(ida.json()["converted"]) == Decimal(375)

    vuelta = await api.post(
        f"{PREPARATIONS}/convert",
        json={"preparation_id": lote["id"], "value": "375", "from_unit": "ml"},
        headers=head(admin_csrf),
    )
    assert vuelta.status_code == 200, vuelta.text
    assert Decimal(vuelta.json()["converted"]) == Decimal(75)


# ---------------------------------------------------------------------------
# Varios esmaltes
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dos_esmaltes_reparten_los_mil_quinientos_gramos(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """MULTI_GLAZE_NO_DOUBLE_COUNT.

    Usar dos esmaltes no gasta 1500 g de cada uno: gasta 1500 en total. Es el
    error que multiplicaria por dos el costo de material de toda la pieza.
    """
    uno = await _lote(api, admin_csrf, suffix="_m1", final_yield_ml="5000")
    dos = await _lote(api, admin_csrf, suffix="_m2", final_yield_ml="5000")

    response = await api.post(
        ESTIMATE,
        json={
            "piece_weight_g": "500",
            "quantity": 20,
            "glazes": [
                {"preparation_id": uno["id"]},
                {"preparation_id": dos["id"]},
            ],
        },
        headers=head(admin_csrf),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    asignados = [Decimal(a["grams"]) for a in body["allocations"]]
    assert sum(asignados) == Decimal(1500)
    assert asignados == [Decimal(750), Decimal(750)]
    assert Decimal(body["total_estimated_grams"]) == Decimal(1500)


@pytest.mark.asyncio
async def test_el_reparto_desigual_tambien_suma_el_total(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    uno = await _lote(api, admin_csrf, suffix="_d1", final_yield_ml="5000")
    dos = await _lote(api, admin_csrf, suffix="_d2", final_yield_ml="5000")

    response = await api.post(
        ESTIMATE,
        json={
            "piece_weight_g": "500",
            "quantity": 20,
            "glazes": [
                {"preparation_id": uno["id"], "share": "70"},
                {"preparation_id": dos["id"], "share": "30"},
            ],
        },
        headers=head(admin_csrf),
    )

    assert response.status_code == 200, response.text
    asignados = [Decimal(a["grams"]) for a in response.json()["allocations"]]
    assert asignados == [Decimal(1050), Decimal(450)]
    assert sum(asignados) == Decimal(1500)


@pytest.mark.asyncio
async def test_el_mismo_preparado_no_puede_repetirse(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """Repetirlo seria pedir dos veces el mismo esmalte y partir su reparto."""
    lote = await _lote(api, admin_csrf, suffix="_rep", final_yield_ml="5000")

    response = await api.post(
        ESTIMATE,
        json={
            "piece_weight_g": "500",
            "quantity": 20,
            "glazes": [
                {"preparation_id": lote["id"]},
                {"preparation_id": lote["id"]},
            ],
        },
        headers=head(admin_csrf),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PREPARATION_INVALID"


# ---------------------------------------------------------------------------
# Estimar no consume
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_estimar_no_mueve_inventario(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """QUOTATION_SIMULATION_MUTATES_INVENTORY: NO.

    Estimar esmalte es prever, no gastar. El descuento real al vender es 009H.
    """
    lote = await _lote(api, admin_csrf, suffix="_puro", final_yield_ml="5000")
    antes = await _inventory_fingerprint(db_session)

    for _ in range(3):
        response = await api.post(
            ESTIMATE,
            json={
                "piece_weight_g": "500",
                "quantity": 20,
                "glazes": [{"preparation_id": lote["id"]}],
            },
            headers=head(admin_csrf),
        )
        assert response.status_code == 200, response.text

    assert await _inventory_fingerprint(db_session) == antes


@pytest.mark.asyncio
async def test_el_ciclo_completo_de_una_cotizacion_no_mueve_inventario(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Previsualizar, guardar, reabrir, recalcular y confirmar: cero consumo.

    Una cotizacion es una promesa de precio. Si pedir un presupuesto vaciara el
    almacen, bastarian unas cuantas cotizaciones descartadas para que el stock
    dejara de significar nada.
    """
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    antes = await _inventory_fingerprint(db_session)

    preview = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert preview.status_code == 200, preview.text
    assert await _inventory_fingerprint(db_session) == antes

    creada = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    quotation_id = creada.json()["id"]
    assert await _inventory_fingerprint(db_session) == antes

    reabierta = await api.get(f"{BUILDER}/{quotation_id}", headers=head(admin_csrf))
    assert reabierta.status_code == 200, reabierta.text
    assert await _inventory_fingerprint(db_session) == antes

    recalculada = await api.put(
        f"{BUILDER}/{quotation_id}",
        json={**payload, "expected_updated_at": reabierta.json()["updated_at"]},
        headers=head(admin_csrf),
    )
    assert recalculada.status_code == 200, recalculada.text
    assert await _inventory_fingerprint(db_session) == antes

    confirmada = await api.post(
        f"{BUILDER}/{quotation_id}/confirm",
        json={"expected_updated_at": recalculada.json()["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmada.status_code == 200, confirmada.text
    assert confirmada.json()["status"] == "CONFIRMED"
    assert await _inventory_fingerprint(db_session) == antes
