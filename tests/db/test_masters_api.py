"""Maestros de categorias, unidades, productos y terceros contra PostgreSQL."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

CATEGORIES = "/api/v1/categories"
POS_CATEGORIES = "/api/v1/pos-categories"
UNITS = "/api/v1/units"
PRODUCTS = "/api/v1/products"
PARTNERS = "/api/v1/partners"


async def create_category(
    api: httpx.AsyncClient, csrf: str, name: str, parent_id: int | None = None
) -> dict[str, Any]:
    response = await api.post(
        CATEGORIES,
        json={"name": name, "parent_id": parent_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def create_product(api: httpx.AsyncClient, csrf: str, **overrides: Any) -> httpx.Response:
    payload: dict[str, Any] = {
        "internal_reference": "INS-1",
        "name": "Arcilla blanca",
        "product_type": "RAW_MATERIAL",
        "product_category_id": overrides.pop("product_category_id"),
        "base_uom_code": "g",
        "purchasable": True,
    }
    payload.update(overrides)
    return await api.post(PRODUCTS, json=payload, headers={"X-CSRF-Token": csrf})


# ---------------------------------------------------------------------------
# Autorizacion
# ---------------------------------------------------------------------------
class TestAutorizacion:
    @pytest.mark.parametrize("path", [CATEGORIES, UNITS, PRODUCTS, PARTNERS])
    async def test_sin_sesion_no_se_lee(self, api: httpx.AsyncClient, path: str) -> None:
        assert (await api.get(path)).status_code == 401

    async def test_operator_lee_los_maestros(
        self, api: httpx.AsyncClient, operator_csrf: str
    ) -> None:
        assert (await api.get(PRODUCTS)).status_code == 200
        assert (await api.get(PARTNERS)).status_code == 200

    async def test_operator_no_puede_crear(
        self, api: httpx.AsyncClient, operator_csrf: str
    ) -> None:
        response = await api.post(
            CATEGORIES,
            json={"name": "Intento"},
            headers={"X-CSRF-Token": operator_csrf},
        )
        assert response.status_code == 403

    async def test_una_escritura_sin_csrf_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        response = await api.post(CATEGORIES, json={"name": "Sin token"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_TOKEN_MISSING"


# ---------------------------------------------------------------------------
# Categorias y unidades
# ---------------------------------------------------------------------------
class TestCategorias:
    async def test_la_ruta_se_construye_desde_el_padre(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        root = await create_category(api, admin_csrf, "Insumos Taller")
        child = await create_category(api, admin_csrf, "Pastas", root["id"])
        assert child["display_path"] == "Insumos Taller / Pastas"

    async def test_dos_hermanas_no_pueden_llamarse_igual(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        root = await create_category(api, admin_csrf, "Insumos")
        await create_category(api, admin_csrf, "Pastas", root["id"])
        response = await api.post(
            CATEGORIES,
            json={"name": "Pastas", "parent_id": root["id"]},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 409

    async def test_renombrar_recalcula_la_ruta(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        root = await create_category(api, admin_csrf, "Insumos")
        response = await api.put(
            f"{CATEGORIES}/{root['id']}",
            json={"name": "Insumos Taller"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 200
        assert response.json()["display_path"] == "Insumos Taller"

    async def test_las_categorias_pos_son_un_maestro_aparte(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        response = await api.post(
            POS_CATEGORIES, json={"name": "Menaje"}, headers={"X-CSRF-Token": admin_csrf}
        )
        assert response.status_code == 201
        assert (await api.get(CATEGORIES)).json() == []


class TestUnidades:
    async def test_la_migracion_siembra_las_tres_canonicas(
        self, api: httpx.AsyncClient, operator_csrf: str
    ) -> None:
        codes = {item["code"] for item in (await api.get(UNITS)).json()}
        assert {"g", "kg", "unit"} <= codes

    async def test_el_factor_de_kilo_es_exacto(
        self, api: httpx.AsyncClient, operator_csrf: str
    ) -> None:
        units = {item["code"]: item for item in (await api.get(UNITS)).json()}
        assert Decimal(units["kg"]["factor_to_base"]) == Decimal(1000)


# ---------------------------------------------------------------------------
# Productos
# ---------------------------------------------------------------------------
class TestProductos:
    async def test_alta_y_lectura(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        category = await create_category(api, admin_csrf, "Insumos Taller")
        response = await create_product(api, admin_csrf, product_category_id=category["id"])
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["internal_reference"] == "INS-1"
        detail = await api.get(f"{PRODUCTS}/{body['id']}")
        assert detail.json()["product_category_path"] == "Insumos Taller"

    async def test_la_referencia_interna_es_unica(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        category = await create_category(api, admin_csrf, "Insumos Taller")
        await create_product(api, admin_csrf, product_category_id=category["id"])
        repeated = await create_product(
            api, admin_csrf, product_category_id=category["id"], name="Otro nombre"
        )
        assert repeated.status_code == 409
        assert repeated.json()["error"]["code"] == "MASTER_VALUE_EXISTS"

    async def test_dos_productos_pueden_llamarse_igual(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El nombre no deduplica: la clave de negocio es la referencia."""
        category = await create_category(api, admin_csrf, "Insumos Taller")
        await create_product(api, admin_csrf, product_category_id=category["id"], name="VASO")
        second = await create_product(
            api,
            admin_csrf,
            product_category_id=category["id"],
            internal_reference="INS-2",
            name="VASO",
        )
        assert second.status_code == 201

    async def test_el_costo_conserva_doce_decimales(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        category = await create_category(api, admin_csrf, "Insumos Taller")
        response = await create_product(
            api,
            admin_csrf,
            product_category_id=category["id"],
            cost="0.002541200000",
        )
        assert response.status_code == 201
        assert Decimal(response.json()["cost"]) == Decimal("0.002541200000")

    async def test_un_costo_por_debajo_del_centimo_no_se_pierde(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        category = await create_category(api, admin_csrf, "Insumos Taller")
        response = await create_product(
            api, admin_csrf, product_category_id=category["id"], cost="0.000000756400"
        )
        assert Decimal(response.json()["cost"]) > 0

    async def test_un_costo_mas_largo_se_ajusta_a_la_escala(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        category = await create_category(api, admin_csrf, "Insumos Taller")
        response = await create_product(
            api, admin_csrf, product_category_id=category["id"], cost="0.0169068431372549"
        )
        assert Decimal(response.json()["cost"]) == Decimal("0.016906843137")

    async def test_solo_un_servicio_puede_no_tener_unidad(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        category = await create_category(api, admin_csrf, "Insumos Taller")
        response = await create_product(
            api, admin_csrf, product_category_id=category["id"], base_uom_code=None
        )
        assert response.status_code == 422

    async def test_un_servicio_sin_unidad_se_acepta(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        category = await create_category(api, admin_csrf, "Servicios")
        response = await create_product(
            api,
            admin_csrf,
            product_category_id=category["id"],
            internal_reference="SER-1",
            name="Clase suelta",
            product_type="SERVICE",
            base_uom_code=None,
            sellable=True,
        )
        assert response.status_code == 201, response.text
        assert response.json()["base_uom_code"] is None

    async def test_una_categoria_inexistente_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        response = await create_product(api, admin_csrf, product_category_id=99999)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "MASTER_INVALID_REFERENCE"

    async def test_busqueda_y_paginacion_del_servidor(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        category = await create_category(api, admin_csrf, "Insumos Taller")
        for index in range(5):
            await create_product(
                api,
                admin_csrf,
                product_category_id=category["id"],
                internal_reference=f"INS-{index}",
                name=f"Arcilla {index}",
            )
        page = (await api.get(PRODUCTS, params={"limit": 2, "offset": 0})).json()
        assert page["total"] == 5
        assert len(page["items"]) == 2

        by_reference = (await api.get(PRODUCTS, params={"search": "INS-3"})).json()
        assert [item["internal_reference"] for item in by_reference["items"]] == ["INS-3"]

        by_name = (await api.get(PRODUCTS, params={"search": "Arcilla 4"})).json()
        assert by_name["total"] == 1

    async def test_filtro_por_tipo(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        category = await create_category(api, admin_csrf, "Insumos Taller")
        await create_product(api, admin_csrf, product_category_id=category["id"])
        filtered = await api.get(PRODUCTS, params={"product_type": "FINISHED_PRODUCT"})
        assert filtered.json()["total"] == 0

    async def test_el_texto_no_admite_html(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        category = await create_category(api, admin_csrf, "Insumos Taller")
        response = await create_product(
            api,
            admin_csrf,
            product_category_id=category["id"],
            name="<script>alert(1)</script>",
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Terceros
# ---------------------------------------------------------------------------
class TestTerceros:
    async def test_un_unico_maestro_con_rol(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        response = await api.post(
            PARTNERS,
            json={
                "name": "ACME S.A.",
                "role": "BOTH",
                "document_type": "RUC",
                "document_number": "20101194991",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 201, response.text
        assert response.json()["role"] == "BOTH"

    async def test_el_documento_no_se_repite(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        payload = {
            "name": "ACME",
            "role": "SUPPLIER",
            "document_type": "RUC",
            "document_number": "20101194991",
        }
        await api.post(PARTNERS, json=payload, headers={"X-CSRF-Token": admin_csrf})
        repeated = await api.post(
            PARTNERS,
            json={**payload, "name": "ACME duplicada"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert repeated.status_code == 409

    async def test_varios_terceros_pueden_no_tener_documento(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        for name in ("Sin documento uno", "Sin documento dos"):
            response = await api.post(
                PARTNERS,
                json={"name": name, "role": "CLIENT"},
                headers={"X-CSRF-Token": admin_csrf},
            )
            assert response.status_code == 201, response.text

    async def test_el_tipo_y_el_numero_van_juntos(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        response = await api.post(
            PARTNERS,
            json={"name": "Incompleto", "role": "CLIENT", "document_type": "DNI"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 422

    async def test_el_ubigeo_debe_existir(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        response = await api.post(
            PARTNERS,
            json={"name": "Cliente", "role": "CLIENT", "ubigeo_code": "999999"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code in {409, 422}

    async def test_filtrar_clientes_incluye_a_los_que_son_ambos(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        for name, role in (("Cliente", "CLIENT"), ("Ambos", "BOTH"), ("Proveedor", "SUPPLIER")):
            await api.post(
                PARTNERS,
                json={"name": name, "role": role},
                headers={"X-CSRF-Token": admin_csrf},
            )
        clients = (await api.get(PARTNERS, params={"role": "CLIENT"})).json()
        assert {item["name"] for item in clients["items"]} == {"Cliente", "Ambos"}

    async def test_busqueda_por_documento(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        await api.post(
            PARTNERS,
            json={
                "name": "ACME",
                "role": "SUPPLIER",
                "document_type": "RUC",
                "document_number": "20509927139",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        found = (await api.get(PARTNERS, params={"search": "20509927139"})).json()
        assert found["total"] == 1
