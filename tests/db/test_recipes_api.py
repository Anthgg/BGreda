"""Pruebas de integracion para la API de Recetas contra PostgreSQL."""

from __future__ import annotations

from decimal import Decimal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

CATEGORIES = "/api/v1/categories"
PRODUCTS = "/api/v1/products"
RECIPES = "/api/v1/recipes"
RECIPE_VERSIONS = "/api/v1/recipe-versions"


async def setup_recipe_products(api: httpx.AsyncClient, csrf: str) -> dict[str, int]:
    """Crea una jerarquia de categorias y productos para recetas."""
    cat = (
        await api.post(
            CATEGORIES,
            json={"name": "Insumos Taller"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    subcat = (
        await api.post(
            CATEGORIES,
            json={"name": "Esmaltes", "parent_id": cat["id"]},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    # Producto preparado
    prep = (
        await api.post(
            PRODUCTS,
            json={
                "name": "Esmalte Blanco Brillante",
                "product_type": "PREPARED_MATERIAL",
                "product_category_id": subcat["id"],
                "base_uom_code": "g",
                "purchasable": False,
                "sellable": True,
            },
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    # Insumos
    feldespato = (
        await api.post(
            PRODUCTS,
            json={
                "name": "Feldespato potasico",
                "product_type": "RAW_MATERIAL",
                "product_category_id": cat["id"],
                "base_uom_code": "kg",
                "cost": 25.0,  # S/ 25 por kg -> S/ 0.025 / g
                "purchasable": True,
            },
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    cuarzo = (
        await api.post(
            PRODUCTS,
            json={
                "name": "Cuarzo",
                "product_type": "RAW_MATERIAL",
                "product_category_id": cat["id"],
                "base_uom_code": "kg",
                "cost": 15.0,  # S/ 15 por kg -> S/ 0.015 / g
                "purchasable": True,
            },
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    oxido = (
        await api.post(
            PRODUCTS,
            json={
                "name": "Oxido de Cobalto",
                "product_type": "RAW_MATERIAL",
                "product_category_id": cat["id"],
                "base_uom_code": "g",
                "cost": 0.50,  # S/ 0.50 por g
                "purchasable": True,
            },
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    bentonita = (
        await api.post(
            PRODUCTS,
            json={
                "name": "Bentonita",
                "product_type": "RAW_MATERIAL",
                "product_category_id": cat["id"],
                "base_uom_code": "kg",
                "cost": 10.0,  # S/ 10 por kg -> S/ 0.010 / g
                "purchasable": True,
            },
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    return {
        "cat_id": cat["id"],
        "subcat_id": subcat["id"],
        "prep_id": prep["id"],
        "feldespato_id": feldespato["id"],
        "cuarzo_id": cuarzo["id"],
        "oxido_id": oxido["id"],
        "bentonita_id": bentonita["id"],
    }


class TestRecetasCRUD:
    async def test_crear_receta_con_base_100_y_colorante_y_aditivo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        p = await setup_recipe_products(api, admin_csrf)

        payload = {
            "product_id": p["prep_id"],
            "name": "Formula Esmalte Blanco",
            "lines": [
                {
                    "component_product_id": p["feldespato_id"],
                    "component_type": "BASE",
                    "percentage": 60.0,
                    "sort_order": 0,
                },
                {
                    "component_product_id": p["cuarzo_id"],
                    "component_type": "BASE",
                    "percentage": 40.0,
                    "sort_order": 1,
                },
                {
                    "component_product_id": p["oxido_id"],
                    "component_type": "COLORANT",
                    "percentage": 6.0,
                    "sort_order": 2,
                },
                {
                    "component_product_id": p["bentonita_id"],
                    "component_type": "ADDITIVE",
                    "percentage": 2.0,
                    "sort_order": 3,
                },
            ],
            "notes": "Formula base con colorante azul cobalto",
            "active": True,
            "activate_immediately": True,
        }

        response = await api.post(RECIPES, json=payload, headers={"X-CSRF-Token": admin_csrf})
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["name"] == "Formula Esmalte Blanco"
        assert data["active"] is True
        assert data["current_version_id"] is not None

        version = data["current_version"]
        assert version is not None
        assert version["version_number"] == 1
        assert version["status"] == "ACTIVE"
        assert Decimal(str(version["yield_factor"])) == Decimal("1.080000")
        assert Decimal(str(version["base_total"])) == Decimal("100.000000")
        assert Decimal(str(version["additional_total"])) == Decimal("8.000000")
        assert len(version["lines"]) == 4

    async def test_crear_receta_con_producto_no_preparado_falla(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        p = await setup_recipe_products(api, admin_csrf)
        # Intentar crear receta para feldespato (RAW_MATERIAL)
        payload = {
            "product_id": p["feldespato_id"],
            "name": "Receta Invalida",
            "lines": [
                {
                    "component_product_id": p["cuarzo_id"],
                    "component_type": "BASE",
                    "percentage": 100.0,
                }
            ],
        }
        response = await api.post(RECIPES, json=payload, headers={"X-CSRF-Token": admin_csrf})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "RECIPE_INVALID_TARGET_PRODUCT"

    async def test_crear_receta_con_base_menor_a_100_falla(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        p = await setup_recipe_products(api, admin_csrf)
        payload = {
            "product_id": p["prep_id"],
            "name": "Receta Incompleta",
            "lines": [
                {
                    "component_product_id": p["feldespato_id"],
                    "component_type": "BASE",
                    "percentage": 80.0,
                },
                {
                    "component_product_id": p["oxido_id"],
                    "component_type": "COLORANT",
                    "percentage": 5.0,
                },
            ],
        }
        response = await api.post(RECIPES, json=payload, headers={"X-CSRF-Token": admin_csrf})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "RECIPE_INVALID_BASE_PERCENTAGE"


class TestVersionamiento:
    async def test_crear_nueva_version_y_activar_archiva_previa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        p = await setup_recipe_products(api, admin_csrf)

        # 1. Crear receta con V1 ACTIVE
        res1 = await api.post(
            RECIPES,
            json={
                "product_id": p["prep_id"],
                "name": "Esmalte V1",
                "lines": [
                    {
                        "component_product_id": p["feldespato_id"],
                        "component_type": "BASE",
                        "percentage": 100.0,
                    }
                ],
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert res1.status_code == 201, res1.text
        recipe = res1.json()
        v1_id = recipe["current_version_id"]

        # 2. Crear V2 como DRAFT
        res2 = await api.post(
            f"{RECIPES}/{recipe['id']}/versions",
            json={
                "lines": [
                    {
                        "component_product_id": p["feldespato_id"],
                        "component_type": "BASE",
                        "percentage": 50.0,
                    },
                    {
                        "component_product_id": p["cuarzo_id"],
                        "component_type": "BASE",
                        "percentage": 50.0,
                    },
                ],
                "notes": "Ajuste de cuarzo",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert res2.status_code == 201, res2.text
        v2 = res2.json()
        assert v2["version_number"] == 2
        assert v2["status"] == "DRAFT"

        # V1 sigue siendo activa
        v1_check = (await api.get(f"{RECIPE_VERSIONS}/{v1_id}")).json()
        assert v1_check["status"] == "ACTIVE"

        # 3. Activar V2
        act_res = await api.post(
            f"{RECIPE_VERSIONS}/{v2['id']}/activate",
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert act_res.status_code == 200
        v2_act = act_res.json()
        assert v2_act["status"] == "ACTIVE"

        # V1 debe quedar ARCHIVED
        v1_archived = (await api.get(f"{RECIPE_VERSIONS}/{v1_id}")).json()
        assert v1_archived["status"] == "ARCHIVED"


class TestCalculador:
    async def test_calculo_sin_mutacion_con_costos_exactos(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        p = await setup_recipe_products(api, admin_csrf)

        # Feldespato: S/ 25 / kg -> S/ 0.025 / g
        # Cuarzo: S/ 15 / kg -> S/ 0.015 / g
        # Oxido: S/ 0.50 / g
        # Bentonita: S/ 10 / kg -> S/ 0.010 / g

        calc_payload = {
            "lines": [
                {
                    "component_product_id": p["feldespato_id"],
                    "component_type": "BASE",
                    "percentage": 60.0,
                },
                {
                    "component_product_id": p["cuarzo_id"],
                    "component_type": "BASE",
                    "percentage": 40.0,
                },
                {
                    "component_product_id": p["oxido_id"],
                    "component_type": "COLORANT",
                    "percentage": 6.0,
                },
                {
                    "component_product_id": p["bentonita_id"],
                    "component_type": "ADDITIVE",
                    "percentage": 2.0,
                },
            ],
            "target_base_quantity": 1000.0,
            "target_uom": "g",
        }

        res = await api.post(
            f"{RECIPES}/calculate",
            json=calc_payload,
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert res.status_code == 200, res.text
        calc = res.json()

        # Cantidad objetivo: 1000 g base -> 1080 g output real
        assert Decimal(str(calc["target_base_quantity"])) == Decimal("1000")
        assert Decimal(str(calc["yield_factor"])) == Decimal("1.08")
        assert Decimal(str(calc["real_output_quantity"])) == Decimal("1080")

        # Cantidades de ingredientes:
        # Feldespato 60% de 1000 = 600 g * 0.025 = S/ 15.00
        # Cuarzo 40% de 1000 = 400 g * 0.015 = S/ 6.00
        # Oxido 6% de 1000 = 60 g * 0.50 = S/ 30.00
        # Bentonita 2% de 1000 = 20 g * 0.010 = S/ 0.20
        # Base Cost = 15.00 + 6.00 = S/ 21.00
        # Colorant Cost = S/ 30.00
        # Additive Cost = S/ 0.20
        # Total Cost = S/ 51.20
        # Cost per real g = 51.20 / 1080 = 0.0474074074...

        assert Decimal(str(calc["base_cost"])) == Decimal("21.000000")
        assert Decimal(str(calc["colorant_cost"])) == Decimal("30.000000")
        assert Decimal(str(calc["additive_cost"])) == Decimal("0.200000")
        assert Decimal(str(calc["total_material_cost"])) == Decimal("51.200000")

        expected_unit_cost = Decimal("51.200000") / Decimal("1080")
        actual_unit_cost = Decimal(str(calc["cost_per_real_unit"]))
        assert abs(actual_unit_cost - expected_unit_cost) < Decimal("0.000001")

    async def test_calculador_rechaza_uom_distinta_de_gramos(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        p = await setup_recipe_products(api, admin_csrf)
        base_payload = {
            "lines": [
                {
                    "component_product_id": p["feldespato_id"],
                    "component_type": "BASE",
                    "percentage": 100.0,
                },
            ],
            "target_base_quantity": 1.0,
        }

        # target_uom = kg -> 422
        res_kg = await api.post(
            f"{RECIPES}/calculate",
            json={**base_payload, "target_uom": "kg"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert res_kg.status_code == 422, res_kg.text

        # target_uom = unit -> 422
        res_unit = await api.post(
            f"{RECIPES}/calculate",
            json={**base_payload, "target_uom": "unit"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert res_unit.status_code == 422, res_unit.text

        # target_uom = g -> 200 OK
        res_g = await api.post(
            f"{RECIPES}/calculate",
            json={**base_payload, "target_uom": "g"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert res_g.status_code == 200, res_g.text

    async def test_calculador_1000g_con_base60_base40_colorant6_sin_mutacion_stock(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        p = await setup_recipe_products(api, admin_csrf)

        # Consultar total de movimientos de stock antes
        movements_before = (await api.get("/api/v1/inventory/movements")).json().get("total", 0)

        calc_payload = {
            "lines": [
                {
                    "component_product_id": p["feldespato_id"],
                    "component_type": "BASE",
                    "percentage": 60.0,
                },
                {
                    "component_product_id": p["cuarzo_id"],
                    "component_type": "BASE",
                    "percentage": 40.0,
                },
                {
                    "component_product_id": p["oxido_id"],
                    "component_type": "COLORANT",
                    "percentage": 6.0,
                },
            ],
            "target_base_quantity": 1000.0,
            "target_uom": "g",
        }

        res = await api.post(
            f"{RECIPES}/calculate",
            json=calc_payload,
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert res.status_code == 200, res.text
        calc = res.json()

        # Cantidades esperadas:
        # BASE 60% -> 600 g
        # BASE 40% -> 400 g
        # COLORANT 6% -> 60 g
        # Output real: 1060 g
        # Yield: 1.06
        comp_map = {c["component_product_id"]: c for c in calc["components"]}
        assert Decimal(str(comp_map[p["feldespato_id"]]["required_quantity"])) == Decimal("600")
        assert Decimal(str(comp_map[p["cuarzo_id"]]["required_quantity"])) == Decimal("400")
        assert Decimal(str(comp_map[p["oxido_id"]]["required_quantity"])) == Decimal("60")

        assert Decimal(str(calc["real_output_quantity"])) == Decimal("1060")
        assert Decimal(str(calc["yield_factor"])) == Decimal("1.06")

        # Verificar que NO hubo mutación de stock (delta = 0)
        movements_after = (await api.get("/api/v1/inventory/movements")).json().get("total", 0)
        assert movements_after - movements_before == 0


class TestCiclos:
    async def test_deteccion_de_ciclos_en_recetas_anidadas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        p = await setup_recipe_products(api, admin_csrf)

        # Crear segundo producto preparado
        prep2 = (
            await api.post(
                PRODUCTS,
                json={
                    "name": "Engobe Base",
                    "product_type": "PREPARED_MATERIAL",
                    "product_category_id": p["subcat_id"],
                    "base_uom_code": "g",
                },
                headers={"X-CSRF-Token": admin_csrf},
            )
        ).json()

        # Receta 1: Prep1 usa Feldespato
        res_r1 = await api.post(
            RECIPES,
            json={
                "product_id": p["prep_id"],
                "name": "Receta Prep 1",
                "lines": [
                    {
                        "component_product_id": p["feldespato_id"],
                        "component_type": "BASE",
                        "percentage": 100.0,
                    }
                ],
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert res_r1.status_code == 201, res_r1.text

        # Receta 2: Prep2 usa Prep1
        res_r2 = await api.post(
            RECIPES,
            json={
                "product_id": prep2["id"],
                "name": "Receta Prep 2",
                "lines": [
                    {
                        "component_product_id": p["prep_id"],
                        "component_type": "BASE",
                        "percentage": 100.0,
                    }
                ],
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert res_r2.status_code == 201, res_r2.text

        # Intentar crear nueva version de Prep1 que use Prep2 -> Ciclo Prep1 -> Prep2 -> Prep1
        rec1_res = await api.get(f"{RECIPES}?product_id={p['prep_id']}")
        assert rec1_res.status_code == 200
        rec1_items = rec1_res.json()["items"]
        assert len(rec1_items) > 0, rec1_res.text
        rec1 = rec1_items[0]
        res_cycle = await api.post(
            f"{RECIPES}/{rec1['id']}/versions",
            json={
                "lines": [
                    {
                        "component_product_id": prep2["id"],
                        "component_type": "BASE",
                        "percentage": 100.0,
                    }
                ]
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert res_cycle.status_code == 422
        assert res_cycle.json()["error"]["code"] == "RECIPE_CYCLE_DETECTED"


async def test_recipe_tables_security_rls_not_forced(
    db_session: AsyncSession,
) -> None:
    """Verifica que las tablas de recetas tengan RLS habilitado pero SIN FORCE RLS."""
    from sqlalchemy import text

    tables = ["recipes", "recipe_versions", "recipe_lines"]
    for table in tables:
        result = await db_session.execute(
            text(
                "SELECT c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_class c "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE n.nspname = CURRENT_SCHEMA() AND c.relname = :table"
            ),
            {"table": table},
        )
        row = result.fetchone()
        if row is None:
            # Fallback a schema public si la tabla se crea en public
            result = await db_session.execute(
                text(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON c.relnamespace = n.oid "
                    "WHERE n.nspname = 'public' AND c.relname = :table"
                ),
                {"table": table},
            )
            row = result.fetchone()

        assert row is not None, f"Tabla {table} no encontrada en el catálogo de PostgreSQL"
        _relrowsecurity, relforcerowsecurity = row
        # FORCE RLS debe ser estrictamente False para no someter al
        # table owner/backend a bloqueos de seguridad
        assert relforcerowsecurity is False, (
            f"Tabla {table} no debe tener FORCE ROW LEVEL SECURITY (relforcerowsecurity=False)"
        )


async def test_solo_un_material_preparado_puede_tener_receta(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """Una pieza terminada no tiene formula propia.

    Que el cotizador pueda elegir cualquier receta para una pieza no convierte
    a la pieza en algo con receta: esa independencia se resuelve al cotizar,
    no relajando el maestro.
    """
    cat = (
        await api.post(
            CATEGORIES, json={"name": "Piezas sin receta"}, headers={"X-CSRF-Token": admin_csrf}
        )
    ).json()
    pieza = (
        await api.post(
            PRODUCTS,
            json={
                "name": "Jarra terminada",
                "product_type": "FINISHED_PRODUCT",
                "product_category_id": cat["id"],
                "base_uom_code": "unit",
                "sellable": True,
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
    ).json()

    respuesta = await api.post(
        RECIPES,
        json={"product_id": pieza["id"], "name": "Receta de la jarra", "lines": []},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert respuesta.status_code in (409, 422)
    assert respuesta.json()["error"]["code"] == "RECIPE_INVALID_TARGET_PRODUCT"
