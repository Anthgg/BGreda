"""Pruebas de integracion para la importacion de recetas desde staging."""

from __future__ import annotations

from decimal import Decimal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.importing import (
    ImportAction,
    ImportBatch,
    ImportEntity,
    ImportRow,
    ImportRowStatus,
    ImportStatus,
)

CATEGORIES = "/api/v1/categories"
PRODUCTS = "/api/v1/products"
RECIPE_IMPORTS = "/api/v1/recipe-imports"


async def setup_staging_recipes(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession
) -> tuple[int, int, int, int, int, int]:
    """Crea un lote de staging y productos para probar el importador."""
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

    prep = (
        await api.post(
            PRODUCTS,
            json={
                "name": "BARNIZ BASE 57",
                "product_type": "PREPARED_MATERIAL",
                "product_category_id": subcat["id"],
                "base_uom_code": "g",
            },
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    feldespato = (
        await api.post(
            PRODUCTS,
            json={
                "name": "Feldespato potasico",
                "product_type": "RAW_MATERIAL",
                "product_category_id": cat["id"],
                "base_uom_code": "kg",
                "cost": 25.0,
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
                "cost": 15.0,
            },
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    # Crear lote de staging
    batch = ImportBatch(
        filename="test_recipes.xlsx",
        file_hash="a" * 64,
        file_size=1024,
        status=ImportStatus.ANALYZED,
        summary={"entity": "RECIPE"},
        source_snapshot={},
    )
    db_session.add(batch)
    await db_session.flush()

    # Crear 2 lineas en import_rows sin resolucion humana previa
    row1 = ImportRow(
        batch_id=batch.id,
        entity=ImportEntity.RECIPE,
        sheet_name="RECETAS",
        source_row=2,
        raw={
            "product": "BARNIZ BASE 57",
            "quantity": 1.0,
            "uom": "gr",
            "component": "Feldespato potasico",
            "component_quantity": 0.6,
            "component_uom": "gr",
        },
        normalized={"product": "BARNIZ BASE 57", "component": "Feldespato potasico"},
        action=ImportAction.CREATE,
        status=ImportRowStatus.READY,
    )
    row2 = ImportRow(
        batch_id=batch.id,
        entity=ImportEntity.RECIPE,
        sheet_name="RECETAS",
        source_row=3,
        raw={
            "product": "BARNIZ BASE 57",
            "quantity": 1.0,
            "uom": "gr",
            "component": "Cuarzo",
            "component_quantity": 0.4,
            "component_uom": "gr",
        },
        normalized={"product": "BARNIZ BASE 57", "component": "Cuarzo"},
        action=ImportAction.CREATE,
        status=ImportRowStatus.READY,
    )
    db_session.add_all([row1, row2])
    await db_session.commit()

    return batch.id, prep["id"], feldespato["id"], cuarzo["id"], row1.id, row2.id


class TestRecipeImport:
    async def test_unresolved_components_remain_review_required_and_block_commit(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Una receta sin resolucion humana queda como REVIEW_REQUIRED y bloquea commit."""
        batch_id, _prep_id, _, _, _row1_id, _row2_id = await setup_staging_recipes(
            api, admin_csrf, db_session
        )

        # 1. Preview inicial: las lineas no tienen component_type definitivo (solo sugerencia)
        res = await api.get(
            f"{RECIPE_IMPORTS}/{batch_id}/preview", headers={"X-CSRF-Token": admin_csrf}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["recipes_detected"] == 1
        assert data["review_required_count"] == 1
        assert data["ready_count"] == 0
        assert data["error_count"] == 0

        recipe_group = data["recipes"][0]
        assert recipe_group["status"] == "REVIEW_REQUIRED"
        assert recipe_group["is_valid"] is False

        for line in recipe_group["lines"]:
            assert line["component_type"] is None
            assert line["suggested_component_type"] == "BASE"
            assert line["status"] == "REVIEW_REQUIRED"
            assert line["requires_review"] is True
            assert line["resolution_source"] == "UNRESOLVED"

        # 2. Intento de commit directo DEBE ser rechazado con 422
        commit_res = await api.post(
            f"{RECIPE_IMPORTS}/{batch_id}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit_res.status_code == 422, commit_res.text
        assert "RECIPE_IMPORT_ROWS_PENDING" in commit_res.text or "revision" in commit_res.text

    async def test_resolve_component_type_and_percentage_applied_in_preview(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """La resolucion humana aplica component_type y porcentaje, preservando status RESOLVED."""
        batch_id, _prep_id, _, _, row1_id, row2_id = await setup_staging_recipes(
            api, admin_csrf, db_session
        )

        # Resolver ambas lineas como BASE (60% y 40%)
        resolve_res = await api.post(
            f"{RECIPE_IMPORTS}/{batch_id}/resolve",
            json=[
                {
                    "row_id": row1_id,
                    "component_type": "BASE",
                    "percentage": 60.0,
                    "action": "RESOLVE",
                },
                {
                    "row_id": row2_id,
                    "component_type": "BASE",
                    "percentage": 40.0,
                    "action": "RESOLVE",
                },
            ],
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert resolve_res.status_code == 200
        preview = resolve_res.json()
        assert preview["ready_count"] == 1
        assert preview["review_required_count"] == 0

        group = preview["recipes"][0]
        assert group["status"] == "READY"
        assert group["is_valid"] is True
        assert Decimal(str(group["base_total"])) == Decimal("100.000000")

        for line in group["lines"]:
            assert line["component_type"] == "BASE"
            assert line["status"] == "RESOLVED"
            assert line["resolution_source"] == "HUMAN"
            assert line["requires_review"] is False

        # Ahora el commit debe ejecutarse exitosamente
        commit_res = await api.post(
            f"{RECIPE_IMPORTS}/{batch_id}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit_res.status_code == 200
        commit_data = commit_res.json()
        assert commit_data["created"] == 1

    async def test_skip_line_excluded_from_recipe_and_calculations(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Una linea con accion SKIP se excluye de las proporciones y de la version creada."""
        batch_id, prep_id, _, _, row1_id, row2_id = await setup_staging_recipes(
            api, admin_csrf, db_session
        )

        # Resolver row1 como BASE 100% y row2 como SKIP
        resolve_res = await api.post(
            f"{RECIPE_IMPORTS}/{batch_id}/resolve",
            json=[
                {
                    "row_id": row1_id,
                    "component_type": "BASE",
                    "percentage": 100.0,
                    "action": "RESOLVE",
                },
                {"row_id": row2_id, "action": "SKIP"},
            ],
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert resolve_res.status_code == 200
        preview = resolve_res.json()
        assert preview["ready_count"] == 1

        group = preview["recipes"][0]
        assert group["is_valid"] is True

        lines = group["lines"]
        assert lines[0]["action"] == "CREATE"
        assert lines[0]["component_type"] == "BASE"
        assert lines[1]["action"] == "SKIP"

        # Commit crea la receta solo con la linea no salteada
        commit_res = await api.post(
            f"{RECIPE_IMPORTS}/{batch_id}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit_res.status_code == 200

        rec_res = await api.get(f"/api/v1/recipes?product_id={prep_id}")
        assert rec_res.status_code == 200
        recipe_data = rec_res.json()["items"][0]
        # Solo 1 linea en la version activa
        assert len(recipe_data["current_version"]["lines"]) == 1
        assert recipe_data["current_version"]["lines"][0]["percentage"] == "100.000000"

    async def test_base_not_100_blocks_commit(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Si la suma de bases != 100%, el commit es bloqueado."""
        batch_id, _prep_id, _, _, row1_id, row2_id = await setup_staging_recipes(
            api, admin_csrf, db_session
        )

        # Resolver con suma base = 90%
        await api.post(
            f"{RECIPE_IMPORTS}/{batch_id}/resolve",
            json=[
                {
                    "row_id": row1_id,
                    "component_type": "BASE",
                    "percentage": 50.0,
                    "action": "RESOLVE",
                },
                {
                    "row_id": row2_id,
                    "component_type": "BASE",
                    "percentage": 40.0,
                    "action": "RESOLVE",
                },
            ],
            headers={"X-CSRF-Token": admin_csrf},
        )

        # Commit debe fallar
        commit_res = await api.post(
            f"{RECIPE_IMPORTS}/{batch_id}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit_res.status_code == 422
