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
) -> tuple[int, int, int, int]:
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

    # Crear 2 lineas en import_rows
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

    return batch.id, prep["id"], feldespato["id"], cuarzo["id"]


class TestRecipeImport:
    async def test_preview_and_commit_staging_recipe(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        batch_id, prep_id, _, _ = await setup_staging_recipes(api, admin_csrf, db_session)

        # 1. Preview
        preview_res = await api.get(
            f"{RECIPE_IMPORTS}/{batch_id}/preview", headers={"X-CSRF-Token": admin_csrf}
        )
        assert preview_res.status_code == 200, preview_res.text
        preview = preview_res.json()
        assert preview["recipes_detected"] == 1
        assert preview["lines_detected"] == 2
        assert preview["ready_count"] == 1
        assert preview["error_count"] == 0

        recipe_group = preview["recipes"][0]
        assert recipe_group["target_product_id"] == prep_id
        assert recipe_group["is_valid"] is True
        assert Decimal(str(recipe_group["yield_factor"])) == Decimal("1.000000")

        # 2. Commit
        commit_res = await api.post(
            f"{RECIPE_IMPORTS}/{batch_id}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit_res.status_code == 200, commit_res.text
        commit_data = commit_res.json()
        assert commit_data["created"] == 1
        assert commit_data["skipped"] == 0

        # Verificar que la receta se creo en la BD
        rec_res = await api.get(f"/api/v1/recipes?product_id={prep_id}")
        assert rec_res.status_code == 200
        recs = rec_res.json()["items"]
        assert len(recs) == 1
        assert recs[0]["current_version"]["status"] == "ACTIVE"
        assert len(recs[0]["current_version"]["lines"]) == 2

        # 3. Idempotencia: re-ejecutar commit no debe duplicar
        commit_res2 = await api.post(
            f"{RECIPE_IMPORTS}/{batch_id}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit_res2.status_code == 200
        commit_data2 = commit_res2.json()
        assert commit_data2["created"] == 0
        assert commit_data2["skipped"] == 1
