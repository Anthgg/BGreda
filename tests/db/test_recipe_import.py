"""Pruebas de integracion para la clasificacion estructural e importacion de recetas."""

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


async def setup_base_master_data(api: httpx.AsyncClient, csrf: str) -> tuple[int, int]:
    """Crea categoria y subcategoria base."""
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

    return cat["id"], subcat["id"]


class TestStructuralRecipeImport:
    async def test_barniz_base_57_exact_structural_base_100(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Caso BARNIZ BASE 57: [50, 10.8, 18.6, 14.7, 5.9] = 100% BASE.

        - Óxido de zinc permanece BASE porque pertenece al bloque de 100%.
        - La keyword 'ÓXIDO' NO lo mueve a COLORANT.
        - Estado READY y commit inmediato permitido.
        """
        _, subcat_id = await setup_base_master_data(api, admin_csrf)

        # 1. Crear producto preparado destino
        prep = (
            await api.post(
                PRODUCTS,
                json={
                    "name": "BARNIZ BASE 57",
                    "product_type": "PREPARED_MATERIAL",
                    "product_category_id": subcat_id,
                    "base_uom_code": "g",
                },
                headers={"X-CSRF-Token": admin_csrf},
            )
        ).json()

        # 2. Crear componentes (incluyendo Óxido de zinc)
        raw_components = [
            ("Feldespato potásico", "0.50", "kg", "10.0"),
            ("Óxido de zinc", "0.108", "kg", "25.0"),
            ("Carbonato de calcio", "0.186", "kg", "5.0"),
            ("Caolín", "0.147", "kg", "8.0"),
            ("Cuarzo", "0.059", "kg", "6.0"),
        ]

        for name, _, uom, cost in raw_components:
            await api.post(
                PRODUCTS,
                json={
                    "name": name,
                    "product_type": "RAW_MATERIAL",
                    "product_category_id": subcat_id,
                    "base_uom_code": uom,
                    "cost": cost,
                },
                headers={"X-CSRF-Token": admin_csrf},
            )

        # 3. Crear lote de staging con las 5 filas
        batch = ImportBatch(
            filename="recetas_maestro.xlsx",
            file_hash="a" * 64,
            file_size=1024,
            status=ImportStatus.COMMITTED,
            summary={},
            source_snapshot={},
        )
        db_session.add(batch)
        await db_session.flush()

        for idx, (name, qty, uom, _) in enumerate(raw_components, start=1):
            row = ImportRow(
                batch_id=batch.id,
                entity=ImportEntity.RECIPE,
                sheet_name="Recetas",
                source_row=idx + 1,
                action=ImportAction.CREATE,
                status=ImportRowStatus.READY,
                raw={
                    "product": "BARNIZ BASE 57",
                    "component": name,
                    "component_quantity": qty,
                    "component_uom": uom,
                },
            )
            db_session.add(row)
        await db_session.commit()

        # 4. Preview
        res = await api.get(
            f"{RECIPE_IMPORTS}/{batch.id}/preview", headers={"X-CSRF-Token": admin_csrf}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["recipes_detected"] == 1
        assert data["ready_count"] == 1
        assert data["review_required_count"] == 0
        assert data["error_count"] == 0

        group = data["recipes"][0]
        assert group["status"] == "READY"
        assert group["is_valid"] is True
        assert group["has_structural_base_boundary"] is True
        assert Decimal(str(group["base_total"])) == Decimal("100.000000")
        assert Decimal(str(group["additional_total"])) == Decimal("0.000000")
        assert Decimal(str(group["yield_factor"])) == Decimal("1.000000")

        # Verificar que las 5 lineas son BASE y SOURCE_STRUCTURE
        assert len(group["lines"]) == 5
        for line in group["lines"]:
            assert line["component_type"] == "BASE"
            assert line["classification_role"] == "BASE"
            assert line["classification_source"] == "SOURCE_STRUCTURE"
            assert line["status"] == "READY"
            assert line["requires_review"] is False

        # Verificar especificamente Óxido de zinc
        zinc_line = next(
            line for line in group["lines"] if "zinc" in line["component_name_raw"].lower()
        )
        assert zinc_line["component_type"] == "BASE"
        assert Decimal(str(zinc_line["final_percentage"])) == Decimal("10.800000")

        # 5. Commit exitoso sin necesidad de resolucion manual previa
        commit_res = await api.post(
            f"{RECIPE_IMPORTS}/{batch.id}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit_res.status_code == 200
        commit_data = commit_res.json()
        assert commit_data["created"] == 1

        rec_res = await api.get(f"/api/v1/recipes?product_id={prep['id']}")
        assert rec_res.status_code == 200
        recipe_data = rec_res.json()["items"][0]
        assert len(recipe_data["current_version"]["lines"]) == 5

    async def test_barniz_base_54_bentonite_base_and_copper_additional(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Caso BARNIZ BASE 54: [40, 20, 2, 37, 1, 3].

        - Primeras 5 lineas (hasta Bentonita 1%) acumulan 100% -> BASE.
        - Bentonita permanece BASE y NO se convierte en ADDITIVE por keyword.
        - Carbonato de cobre (3%) es posterior al 100% -> ADDITIONAL.
        - Inicialmente REVIEW_REQUIRED hasta que el usuario resuelva el tipo adicional.
        - Tras resolver como COLORANT: BASE_TOTAL=100, ADDITIONAL_TOTAL=3, YIELD=1.03.
        """
        _, subcat_id = await setup_base_master_data(api, admin_csrf)

        await api.post(
            PRODUCTS,
            json={
                "name": "BARNIZ BASE 54",
                "product_type": "PREPARED_MATERIAL",
                "product_category_id": subcat_id,
                "base_uom_code": "g",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )

        raw_components = [
            ("Feldespato potásico", "0.40", "kg", "10.0"),
            ("Carbonato de bario", "0.20", "kg", "15.0"),
            ("Caolín", "0.02", "kg", "8.0"),
            ("Feldespato sódico", "0.37", "kg", "12.0"),
            ("Bentonita", "0.01", "kg", "14.0"),
            ("Carbonato de cobre", "0.03", "kg", "80.0"),
        ]

        for name, _, uom, cost in raw_components:
            await api.post(
                PRODUCTS,
                json={
                    "name": name,
                    "product_type": "RAW_MATERIAL",
                    "product_category_id": subcat_id,
                    "base_uom_code": uom,
                    "cost": cost,
                },
                headers={"X-CSRF-Token": admin_csrf},
            )

        batch = ImportBatch(
            filename="recetas_maestro.xlsx",
            file_hash="b" * 64,
            file_size=1024,
            status=ImportStatus.COMMITTED,
            summary={},
            source_snapshot={},
        )
        db_session.add(batch)
        await db_session.flush()

        row_ids = []
        for idx, (name, qty, uom, _) in enumerate(raw_components, start=1):
            row = ImportRow(
                batch_id=batch.id,
                entity=ImportEntity.RECIPE,
                sheet_name="Recetas",
                source_row=idx + 1,
                action=ImportAction.CREATE,
                status=ImportRowStatus.READY,
                raw={
                    "product": "BARNIZ BASE 54",
                    "component": name,
                    "component_quantity": qty,
                    "component_uom": uom,
                },
            )
            db_session.add(row)
            await db_session.flush()
            row_ids.append(row.id)
        await db_session.commit()

        # 1. Preview inicial: 1 receta en revision requerida por la linea adicional no clasificada
        res = await api.get(
            f"{RECIPE_IMPORTS}/{batch.id}/preview", headers={"X-CSRF-Token": admin_csrf}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["recipes_detected"] == 1
        assert data["ready_count"] == 0
        assert data["review_required_count"] == 1

        group = data["recipes"][0]
        assert group["status"] == "REVIEW_REQUIRED"
        assert group["has_structural_base_boundary"] is True

        # Primeras 5 lineas deben ser BASE estructural
        for line in group["lines"][:5]:
            assert line["component_type"] == "BASE"
            assert line["classification_role"] == "BASE"
            assert line["classification_source"] == "SOURCE_STRUCTURE"
            assert line["status"] == "READY"

        # Bentonita (linea 5) es BASE
        bentonita_line = group["lines"][4]
        assert bentonita_line["component_name_raw"] == "Bentonita"
        assert bentonita_line["component_type"] == "BASE"

        # Linea 6 (Carbonato de cobre) es ADDITIONAL y REVIEW_REQUIRED
        cobre_line = group["lines"][5]
        assert cobre_line["component_name_raw"] == "Carbonato de cobre"
        assert cobre_line["component_type"] is None
        assert cobre_line["classification_role"] == "ADDITIONAL"
        assert cobre_line["suggested_component_type"] == "COLORANT"
        assert cobre_line["status"] == "REVIEW_REQUIRED"
        assert cobre_line["requires_review"] is True

        # 2. Intento de commit debe ser bloqueado con 422
        commit_res = await api.post(
            f"{RECIPE_IMPORTS}/{batch.id}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit_res.status_code == 422

        # 3. Resolver la linea adicional de Carbonato de cobre como COLORANT
        resolve_res = await api.post(
            f"{RECIPE_IMPORTS}/{batch.id}/resolve",
            json=[
                {
                    "row_id": row_ids[5],
                    "component_type": "COLORANT",
                    "percentage": 3.0,
                    "action": "RESOLVE",
                }
            ],
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert resolve_res.status_code == 200
        preview_after = resolve_res.json()
        assert preview_after["ready_count"] == 1
        assert preview_after["review_required_count"] == 0

        group_after = preview_after["recipes"][0]
        assert group_after["status"] == "READY"
        assert group_after["is_valid"] is True
        assert Decimal(str(group_after["base_total"])) == Decimal("100.000000")
        assert Decimal(str(group_after["additional_total"])) == Decimal("3.000000")
        assert Decimal(str(group_after["yield_factor"])) == Decimal("1.030000")

        # 4. Commit ahora es exitoso
        commit_ok = await api.post(
            f"{RECIPE_IMPORTS}/{batch.id}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit_ok.status_code == 200

    async def test_barniz_base_42_high_yield_36_percent_additional(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Caso BARNIZ BASE 42: [64, 12, 9, 12, 3, 1, 2, 33].

        - Primeras 5 lineas acumulan 100% -> BASE.
        - Ultimas 3 lineas (1, 2, 33) -> ADDITIONAL (total 36%).
        - Tras resolver adicionales como COLORANT: YIELD_FACTOR = 1.36.
        """
        _, subcat_id = await setup_base_master_data(api, admin_csrf)

        await api.post(
            PRODUCTS,
            json={
                "name": "BARNIZ BASE 42",
                "product_type": "PREPARED_MATERIAL",
                "product_category_id": subcat_id,
                "base_uom_code": "g",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )

        raw_components = [
            ("Feldespato potásico", "0.64", "kg", "10.0"),
            ("Wollastonita", "0.12", "kg", "15.0"),
            ("Caolín", "0.09", "kg", "8.0"),
            ("Cuarzo", "0.12", "kg", "6.0"),
            ("Frita 3134", "0.03", "kg", "30.0"),
            ("Óxido de estaño", "0.01", "kg", "120.0"),
            ("Óxido de cobre", "0.02", "kg", "90.0"),
            ("Óxido de cobalto", "0.33", "kg", "250.0"),
        ]

        for name, _, uom, cost in raw_components:
            await api.post(
                PRODUCTS,
                json={
                    "name": name,
                    "product_type": "RAW_MATERIAL",
                    "product_category_id": subcat_id,
                    "base_uom_code": uom,
                    "cost": cost,
                },
                headers={"X-CSRF-Token": admin_csrf},
            )

        batch = ImportBatch(
            filename="recetas_maestro.xlsx",
            file_hash="c" * 64,
            file_size=1024,
            status=ImportStatus.COMMITTED,
            summary={},
            source_snapshot={},
        )
        db_session.add(batch)
        await db_session.flush()

        row_ids = []
        for idx, (name, qty, uom, _) in enumerate(raw_components, start=1):
            row = ImportRow(
                batch_id=batch.id,
                entity=ImportEntity.RECIPE,
                sheet_name="Recetas",
                source_row=idx + 1,
                action=ImportAction.CREATE,
                status=ImportRowStatus.READY,
                raw={
                    "product": "BARNIZ BASE 42",
                    "component": name,
                    "component_quantity": qty,
                    "component_uom": uom,
                },
            )
            db_session.add(row)
            await db_session.flush()
            row_ids.append(row.id)
        await db_session.commit()

        # Resolver las 3 ultimas lineas como COLORANT
        resolve_res = await api.post(
            f"{RECIPE_IMPORTS}/{batch.id}/resolve",
            json=[
                {
                    "row_id": row_ids[5],
                    "component_type": "COLORANT",
                    "percentage": 1.0,
                    "action": "RESOLVE",
                },
                {
                    "row_id": row_ids[6],
                    "component_type": "COLORANT",
                    "percentage": 2.0,
                    "action": "RESOLVE",
                },
                {
                    "row_id": row_ids[7],
                    "component_type": "COLORANT",
                    "percentage": 33.0,
                    "action": "RESOLVE",
                },
            ],
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert resolve_res.status_code == 200
        preview = resolve_res.json()
        assert preview["ready_count"] == 1
        group = preview["recipes"][0]
        assert Decimal(str(group["base_total"])) == Decimal("100.000000")
        assert Decimal(str(group["additional_total"])) == Decimal("36.000000")
        assert Decimal(str(group["yield_factor"])) == Decimal("1.360000")

    async def test_no_exact_boundary_crosses_100_requires_review(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Una receta que acumula [60, 30, 15] (suma 105) no tiene boundary y requiere revision."""
        _, subcat_id = await setup_base_master_data(api, admin_csrf)

        await api.post(
            PRODUCTS,
            json={
                "name": "PASTA ANOMALA",
                "product_type": "PREPARED_MATERIAL",
                "product_category_id": subcat_id,
                "base_uom_code": "g",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )

        for name, uom in [("Arcilla A", "kg"), ("Arcilla B", "kg"), ("Arcilla C", "kg")]:
            await api.post(
                PRODUCTS,
                json={
                    "name": name,
                    "product_type": "RAW_MATERIAL",
                    "product_category_id": subcat_id,
                    "base_uom_code": uom,
                },
                headers={"X-CSRF-Token": admin_csrf},
            )

        batch = ImportBatch(
            filename="recetas_maestro.xlsx",
            file_hash="d" * 64,
            file_size=1024,
            status=ImportStatus.COMMITTED,
            summary={},
            source_snapshot={},
        )
        db_session.add(batch)
        await db_session.flush()

        for idx, (name, qty) in enumerate(
            [("Arcilla A", "0.60"), ("Arcilla B", "0.30"), ("Arcilla C", "0.15")], start=1
        ):
            row = ImportRow(
                batch_id=batch.id,
                entity=ImportEntity.RECIPE,
                sheet_name="Recetas",
                source_row=idx + 1,
                action=ImportAction.CREATE,
                status=ImportRowStatus.READY,
                raw={
                    "product": "PASTA ANOMALA",
                    "component": name,
                    "component_quantity": qty,
                    "component_uom": "kg",
                },
            )
            db_session.add(row)
        await db_session.commit()

        res = await api.get(
            f"{RECIPE_IMPORTS}/{batch.id}/preview", headers={"X-CSRF-Token": admin_csrf}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["review_required_count"] == 1
        group = data["recipes"][0]
        assert group["has_structural_base_boundary"] is False
        assert group["status"] == "REVIEW_REQUIRED"
        assert any("BASE_BOUNDARY_NOT_FOUND" in w for w in group["warnings"])

        # Commit debe ser bloqueado con 422
        commit_res = await api.post(
            f"{RECIPE_IMPORTS}/{batch.id}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit_res.status_code == 422
