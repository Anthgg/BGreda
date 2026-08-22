"""Inventario contra PostgreSQL: saldos, ajustes y trazabilidad."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

INVENTORY = "/api/v1/inventory"
LOCATIONS = f"{INVENTORY}/locations"
MOVEMENTS = f"{INVENTORY}/movements"
ADJUSTMENTS = f"{INVENTORY}/adjustments"
CATEGORIES = "/api/v1/categories"
PRODUCTS = "/api/v1/products"


async def _setup(api: httpx.AsyncClient, csrf: str) -> tuple[int, int]:
    category = await api.post(
        CATEGORIES, json={"name": "Insumos Taller"}, headers={"X-CSRF-Token": csrf}
    )
    product = await api.post(
        PRODUCTS,
        json={
            "name": "Arcilla blanca",
            "product_type": "RAW_MATERIAL",
            "product_category_id": category.json()["id"],
            "base_uom_code": "g",
            "purchasable": True,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert product.status_code == 201, product.text
    location = await api.post(LOCATIONS, json={"name": "Deposito"}, headers={"X-CSRF-Token": csrf})
    assert location.status_code == 201, location.text
    return product.json()["id"], location.json()["id"]


async def _adjust(
    api: httpx.AsyncClient, csrf: str, product_id: int, location_id: int, quantity: str
) -> httpx.Response:
    return await api.post(
        ADJUSTMENTS,
        json={
            "product_id": product_id,
            "location_id": location_id,
            "quantity": quantity,
            "reason": "Conteo fisico",
        },
        headers={"X-CSRF-Token": csrf},
    )


class TestAutorizacion:
    async def test_sin_sesion_no_se_consulta(self, api: httpx.AsyncClient) -> None:
        assert (await api.get(INVENTORY)).status_code == 401

    async def test_operator_consulta_pero_no_ajusta(
        self, api: httpx.AsyncClient, operator_csrf: str
    ) -> None:
        assert (await api.get(INVENTORY)).status_code == 200
        response = await api.post(
            ADJUSTMENTS,
            json={
                "product_id": 1,
                "location_id": 1,
                "quantity": "1",
                "reason": "Intento",
            },
            headers={"X-CSRF-Token": operator_csrf},
        )
        assert response.status_code == 403


class TestAjustes:
    async def test_un_ajuste_crea_saldo_y_movimiento(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        product_id, location_id = await _setup(api, admin_csrf)
        response = await _adjust(api, admin_csrf, product_id, location_id, "120")
        assert response.status_code == 201, response.text
        assert Decimal(response.json()["balance_after"]) == Decimal(120)

        stock = (await api.get(INVENTORY)).json()
        assert Decimal(stock["items"][0]["quantity"]) == Decimal(120)

        movements = (await api.get(MOVEMENTS)).json()
        assert movements["total"] == 1
        assert movements["items"][0]["movement_type"] == "ADJUSTMENT"
        assert movements["items"][0]["reason"] == "Conteo fisico"

    async def test_los_ajustes_se_acumulan(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        product_id, location_id = await _setup(api, admin_csrf)
        await _adjust(api, admin_csrf, product_id, location_id, "100")
        await _adjust(api, admin_csrf, product_id, location_id, "-40")
        stock = (await api.get(INVENTORY)).json()
        assert Decimal(stock["items"][0]["quantity"]) == Decimal(60)
        assert (await api.get(MOVEMENTS)).json()["total"] == 2

    async def test_no_se_admite_dejar_existencia_negativa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        product_id, location_id = await _setup(api, admin_csrf)
        await _adjust(api, admin_csrf, product_id, location_id, "10")
        response = await _adjust(api, admin_csrf, product_id, location_id, "-11")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "NEGATIVE_STOCK_NOT_ALLOWED"
        stock = (await api.get(INVENTORY)).json()
        assert Decimal(stock["items"][0]["quantity"]) == Decimal(10)

    async def test_un_ajuste_de_cero_no_es_un_movimiento(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        product_id, location_id = await _setup(api, admin_csrf)
        response = await _adjust(api, admin_csrf, product_id, location_id, "0")
        assert response.status_code == 422

    async def test_la_cantidad_conserva_decimales_finos(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        product_id, location_id = await _setup(api, admin_csrf)
        await _adjust(api, admin_csrf, product_id, location_id, "0.000000000001")
        stock = (await api.get(INVENTORY)).json()
        assert Decimal(stock["items"][0]["quantity"]) == Decimal("0.000000000001")

    async def test_el_producto_debe_existir(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        _product_id, location_id = await _setup(api, admin_csrf)
        response = await _adjust(api, admin_csrf, 99999, location_id, "1")
        assert response.status_code == 404

    async def test_no_existe_ninguna_ruta_que_escriba_el_saldo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El saldo solo cambia por movimiento; no hay PUT sobre el balance."""
        product_id, location_id = await _setup(api, admin_csrf)
        for method, path in (
            ("PUT", f"{INVENTORY}/{product_id}"),
            ("PATCH", f"{INVENTORY}/{product_id}"),
            ("POST", f"{INVENTORY}/balances"),
            ("PUT", f"{INVENTORY}/balances/{location_id}"),
        ):
            response = await api.request(
                method, path, json={"quantity": "999"}, headers={"X-CSRF-Token": admin_csrf}
            )
            assert response.status_code in {404, 405}, f"{method} {path}"


class TestFiltros:
    async def test_busqueda_por_producto(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        product_id, location_id = await _setup(api, admin_csrf)
        await _adjust(api, admin_csrf, product_id, location_id, "5")
        found = (await api.get(INVENTORY, params={"search": "Arcilla"})).json()
        assert found["total"] == 1
        missing = (await api.get(INVENTORY, params={"search": "no existe"})).json()
        assert missing["total"] == 0

    async def test_historial_por_producto(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        product_id, location_id = await _setup(api, admin_csrf)
        await _adjust(api, admin_csrf, product_id, location_id, "5")
        movements = (await api.get(MOVEMENTS, params={"product_id": product_id})).json()
        assert movements["total"] == 1
        other: dict[str, Any] = (await api.get(MOVEMENTS, params={"product_id": 99999})).json()
        assert other["total"] == 0
