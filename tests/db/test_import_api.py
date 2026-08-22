"""Importador de maestros de extremo a extremo contra PostgreSQL.

Los libros son sinteticos y se generan en memoria: el maestro real de la
empresa no entra en el repositorio.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Any

import httpx
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

IMPORTS = "/api/v1/imports"
UPLOAD = f"{IMPORTS}/master/upload"
PRODUCTS = "/api/v1/products"
PARTNERS = "/api/v1/partners"
INVENTORY = "/api/v1/inventory"

CATEGORY_HEADERS = ["Categoria", "Categoria padre", "Nombre a mostrar", "cuenta de gasto"]
POS_HEADERS = ["Nombre", "Categoria padre"]
PRODUCT_HEADERS = [
    "Nombre",
    "Referencia interna",
    "Categoria de producto",
    "Categoria PdV",
    "Impuesto venta",
    "Impuesto de compra",
    "¿Se puede vender?",
    "¿Se puede comprar?",
    "Disponible en punto de venta?",
    "Precio de venta",
    "Costo",
    "Unidad de medida",
    "Unidad de medida de compra",
]
PARTNER_HEADERS = [
    "Nombre",
    "Tipo de identificación",
    "Número de indentificación",
    "Calle",
    "distrito",
    "provincia",
    "Departamento",
    "país",
    "Correo",
    "Celular",
]
STOCK_HEADERS = ["Producto", "Unidad de medida", "Cantidad", "Ubicación"]
RECIPE_HEADERS = [
    "Nombre del producto a preparar",
    "Cantidad",
    "Unidad de medida del producto preparado",
    "Insumo",
    "Cantidad del insumo",
    "Unidad de medida del insumo",
]


def workbook_bytes(sheets: dict[str, list[list[Any]]]) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(title=name)
        for row in rows:
            sheet.append(row)
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def full_workbook() -> bytes:
    return workbook_bytes(
        {
            "Categoria de producto": [
                CATEGORY_HEADERS,
                ["Insumos Taller", None, "Insumos Taller", 6021031.0],
                ["Pastas", "Insumos Taller", "Insumos Taller / Pastas", 6021032.0],
            ],
            "Categoria en Punto de venta": [POS_HEADERS, ["Menaje", None]],
            "Productos": [
                PRODUCT_HEADERS,
                [
                    "Arcilla blanca",
                    "INS-1",
                    "Insumos Taller / Pastas",
                    None,
                    None,
                    0.18,
                    "No",
                    "Si",
                    "No",
                    None,
                    0.0169068431372549,
                    "gr",
                    "kg",
                ],
            ],
            "Proveedores y clientes": [
                PARTNER_HEADERS,
                [
                    "QUIMICA PANAMERICANA S.A.",
                    "RUC",
                    "20101194991",
                    "Av. Republica",
                    "Barranco",
                    "Lima",
                    "Lima",
                    "Perú",
                    None,
                    "996689100",
                ],
            ],
            "Stock": [STOCK_HEADERS, ["Arcilla blanca", "g", 120, "Marino pastor"]],
            "Recetas": [
                RECIPE_HEADERS,
                ["BARNIZ 1", 1.02, "gr", "Arcilla blanca", 0.5, "gr"],
                [None, None, None, "Arcilla blanca", 0.52, "gr"],
            ],
        }
    )


async def count_rows(session: AsyncSession, table: str) -> int:
    """Cuenta filas de una tabla fija del propio test, no de entrada externa."""
    return int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608


async def upload(api: httpx.AsyncClient, csrf: str, payload: bytes) -> httpx.Response:
    return await api.post(
        UPLOAD,
        files={
            "file": (
                "maestros.xlsx",
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        headers={"X-CSRF-Token": csrf},
    )


async def rows_of(api: httpx.AsyncClient, batch_id: int, entity: str) -> list[dict[str, Any]]:
    response = await api.get(
        f"{IMPORTS}/{batch_id}/preview", params={"entity": entity, "limit": 500}
    )
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def resolve(
    api: httpx.AsyncClient, csrf: str, batch_id: int, resolutions: list[dict[str, Any]]
) -> httpx.Response:
    return await api.post(
        f"{IMPORTS}/{batch_id}/resolve",
        json={"resolutions": resolutions},
        headers={"X-CSRF-Token": csrf},
    )


async def resolve_everything(api: httpx.AsyncClient, csrf: str, batch_id: int) -> None:
    """Clasifica terceros y confirma el stock, que es lo que queda pendiente."""
    resolutions: list[dict[str, Any]] = []
    for row in await rows_of(api, batch_id, "PARTNER"):
        resolutions.append({"row_id": row["id"], "partner_role": "SUPPLIER"})
    for row in await rows_of(api, batch_id, "STOCK"):
        if row["status"] == "REVIEW_REQUIRED":
            resolutions.append(
                {"row_id": row["id"], "product_id": row["candidates"][0]["product_id"]}
            )
    if resolutions:
        response = await resolve(api, csrf, batch_id, resolutions)
        assert response.status_code == 200, response.text


class TestAutorizacion:
    async def test_sin_token_csrf_no_se_sube(self, api: httpx.AsyncClient) -> None:
        response = await api.post(UPLOAD, files={"file": ("x.xlsx", b"x", "application/xlsx")})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_TOKEN_MISSING"

    async def test_con_csrf_pero_sin_sesion_tampoco(self, api: httpx.AsyncClient) -> None:
        token = (await api.get("/api/v1/auth/csrf")).json()["csrf_token"]
        response = await api.post(
            UPLOAD,
            files={"file": ("x.xlsx", b"x", "application/xlsx")},
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 401

    async def test_operator_no_puede_importar(
        self, api: httpx.AsyncClient, operator_csrf: str
    ) -> None:
        response = await upload(api, operator_csrf, full_workbook())
        assert response.status_code == 403

    async def test_operator_puede_consultar_el_historial(
        self, api: httpx.AsyncClient, operator_csrf: str
    ) -> None:
        assert (await api.get(IMPORTS)).status_code == 200


class TestAnalisis:
    async def test_la_subida_analiza_sin_tocar_los_maestros(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        response = await upload(api, admin_csrf, full_workbook())
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "ANALYZED"
        assert len(body["file_hash"]) == 64

        # Preview listo, maestros intactos.
        assert (await api.get(PRODUCTS)).json()["total"] == 0
        assert (await api.get(PARTNERS)).json()["total"] == 0
        assert (await api.get(INVENTORY)).json()["total"] == 0

    async def test_el_resumen_cuenta_hojas_y_recetas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        summary = (await upload(api, admin_csrf, full_workbook())).json()["summary"]
        assert summary["recipes_detected"] == 1
        assert summary["recipe_lines_detected"] == 2
        assert summary["recipes_imported"] == 0
        assert len(summary["sheets"]) == 6

    async def test_el_costo_largo_avisa_con_el_valor_original(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        batch = (await upload(api, admin_csrf, full_workbook())).json()
        row = (await rows_of(api, batch["id"], "PRODUCT"))[0]
        warning = next(item for item in row["warnings"] if item["code"] == "ROUNDED_TO_12_DECIMALS")
        assert warning["source"] == "0.0169068431372549"
        assert warning["normalized"] == "0.016906843137"

    async def test_el_archivo_repetido_avisa(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        payload = full_workbook()
        first = (await upload(api, admin_csrf, payload)).json()
        await resolve_everything(api, admin_csrf, first["id"])
        commit = await api.post(
            f"{IMPORTS}/{first['id']}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit.status_code == 200, commit.text

        second = (await upload(api, admin_csrf, payload)).json()
        assert second["summary"]["duplicate_file"] is True
        assert second["summary"]["duplicate_of_batch_id"] == first["id"]

    async def test_un_archivo_ilegible_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        response = await upload(api, admin_csrf, b"esto no es un xlsx")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "IMPORT_FILE_INVALID"

    async def test_un_libro_sin_hojas_conocidas_no_propone_nada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        payload = workbook_bytes({"Otra": [["Alfa", "Beta"], ["1", "2"]]})
        batch = (await upload(api, admin_csrf, payload)).json()
        assert batch["summary"]["creates"] == 0
        preview = (await api.get(f"{IMPORTS}/{batch['id']}/preview")).json()
        assert preview["total"] == 0


class TestPreviewYResolucion:
    async def test_el_preview_no_muta_nada(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        batch = (await upload(api, admin_csrf, full_workbook())).json()
        for _ in range(3):
            await api.get(f"{IMPORTS}/{batch['id']}/preview")
        for table in ("products", "partners", "stock_balances", "stock_movements"):
            assert await count_rows(db_session, table) == 0, table

    async def test_los_terceros_esperan_clasificacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        batch = (await upload(api, admin_csrf, full_workbook())).json()
        row = (await rows_of(api, batch["id"], "PARTNER"))[0]
        assert row["status"] == "REVIEW_REQUIRED"
        assert row["normalized"]["role"] == "PENDING_CLASSIFICATION"

    async def test_sin_clasificar_terceros_el_commit_se_bloquea(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        batch = (await upload(api, admin_csrf, full_workbook())).json()
        response = await api.post(
            f"{IMPORTS}/{batch['id']}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "IMPORT_ROWS_PENDING"
        assert (await api.get(PRODUCTS)).json()["total"] == 0

    async def test_resolver_deja_el_lote_listo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        batch = (await upload(api, admin_csrf, full_workbook())).json()
        await resolve_everything(api, admin_csrf, batch["id"])
        updated = (await api.get(f"{IMPORTS}/{batch['id']}")).json()
        assert updated["status"] == "READY"
        assert updated["summary"]["review_required"] == 0

    async def test_una_fila_puede_descartarse(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        batch = (await upload(api, admin_csrf, full_workbook())).json()
        partner_row = (await rows_of(api, batch["id"], "PARTNER"))[0]
        await resolve(
            api, admin_csrf, batch["id"], [{"row_id": partner_row["id"], "action": "SKIP"}]
        )
        for row in await rows_of(api, batch["id"], "STOCK"):
            if row["status"] == "REVIEW_REQUIRED":
                await resolve(
                    api, admin_csrf, batch["id"], [{"row_id": row["id"], "action": "SKIP"}]
                )
        commit = await api.post(
            f"{IMPORTS}/{batch['id']}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit.status_code == 200, commit.text
        assert (await api.get(PARTNERS)).json()["total"] == 0
        assert (await api.get(PRODUCTS)).json()["total"] == 1

    async def test_material_preparado_sin_uom_requiere_revision_y_admite_gramos(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        payload = workbook_bytes(
            {
                "Categoria de producto": [
                    CATEGORY_HEADERS,
                    ["Productos Terminados Taller", None, "Productos Terminados Taller", 6021031.0],
                    [
                        "Esmaltes",
                        "Productos Terminados Taller",
                        "Productos Terminados Taller / Esmaltes",
                        6021032.0,
                    ],
                ],
                "Productos": [
                    PRODUCT_HEADERS,
                    [
                        "BARNIZ BASE 25",
                        "LAB70140",
                        "Productos Terminados Taller / Esmaltes",
                        None,
                        None,
                        0.18,
                        "No",
                        "No",
                        "No",
                        None,
                        None,
                        None,  # UOM vacia en Excel
                        None,
                    ],
                ],
            }
        )
        batch = (await upload(api, admin_csrf, payload)).json()
        row = (await rows_of(api, batch["id"], "PRODUCT"))[0]
        # Debe requerir revision y no auto-asignar 'unit'
        assert row["status"] == "REVIEW_REQUIRED"
        assert row["normalized"]["base_uom_code"] is None

        # La decision humana resuelve a gramos 'g'
        await resolve(
            api,
            admin_csrf,
            batch["id"],
            [{"row_id": row["id"], "base_uom_code": "g"}],
        )
        commit = await api.post(
            f"{IMPORTS}/{batch['id']}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit.status_code == 200, commit.text
        product = (await api.get(PRODUCTS)).json()["items"][0]
        assert product["product_type"] == "PREPARED_MATERIAL"
        assert product["base_uom_code"] == "g"


class TestCommit:
    async def test_el_commit_escribe_los_maestros(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        batch = (await upload(api, admin_csrf, full_workbook())).json()
        await resolve_everything(api, admin_csrf, batch["id"])
        response = await api.post(
            f"{IMPORTS}/{batch['id']}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["batch"]["status"] == "COMMITTED"
        assert result["by_entity"]["PRODUCT"]["created"] == 1
        assert result["by_entity"]["PARTNER"]["created"] == 1
        assert result["by_entity"]["RECIPE"]["skipped"] == 2

        products = (await api.get(PRODUCTS)).json()
        assert products["total"] == 1
        product = products["items"][0]
        assert product["internal_reference"] == "INS-1"
        assert product["product_type"] == "RAW_MATERIAL"
        assert product["product_category_path"] == "Insumos Taller / Pastas"
        assert Decimal(product["cost"]) == Decimal("0.016906843137")
        assert product["base_uom_code"] == "g"

    async def test_el_tercero_toma_el_rol_elegido_y_el_ubigeo_canonico(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        batch = (await upload(api, admin_csrf, full_workbook())).json()
        await resolve_everything(api, admin_csrf, batch["id"])
        await api.post(f"{IMPORTS}/{batch['id']}/commit", headers={"X-CSRF-Token": admin_csrf})
        partner = (await api.get(PARTNERS)).json()["items"][0]
        assert partner["role"] == "SUPPLIER"
        assert partner["ubigeo_code"] == "150104"
        assert partner["district"] == "BARRANCO"
        assert partner["country"] == "Peru"

    async def test_el_stock_inicial_deja_movimiento_trazable(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        batch = (await upload(api, admin_csrf, full_workbook())).json()
        await resolve_everything(api, admin_csrf, batch["id"])
        await api.post(f"{IMPORTS}/{batch['id']}/commit", headers={"X-CSRF-Token": admin_csrf})

        stock = (await api.get(INVENTORY)).json()
        assert stock["total"] == 1
        assert Decimal(stock["items"][0]["quantity"]) == Decimal(120)
        # La errata del archivo se normalizo a la ubicacion canonica.
        assert stock["items"][0]["location_name"] == "Mariano Pastor"

        movements = (await api.get(f"{INVENTORY}/movements")).json()
        assert movements["total"] == 1
        movement = movements["items"][0]
        assert movement["movement_type"] == "INITIAL_IMPORT"
        assert movement["import_batch_id"] == batch["id"]

    async def test_las_recetas_no_crean_nada(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        batch = (await upload(api, admin_csrf, full_workbook())).json()
        await resolve_everything(api, admin_csrf, batch["id"])
        await api.post(f"{IMPORTS}/{batch['id']}/commit", headers={"X-CSRF-Token": admin_csrf})
        # Solo el producto del maestro; ningun preparado inventado por receta.
        assert (await api.get(PRODUCTS)).json()["total"] == 1
        snapshot = await db_session.scalar(
            text("SELECT source_snapshot FROM import_batches WHERE id = :id"),
            {"id": batch["id"]},
        )
        assert len(snapshot["recipes"]) == 2

    async def test_las_cuentas_contables_solo_quedan_en_el_rastro(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        batch = (await upload(api, admin_csrf, full_workbook())).json()
        snapshot = await db_session.scalar(
            text("SELECT source_snapshot FROM import_batches WHERE id = :id"),
            {"id": batch["id"]},
        )
        assert snapshot["accounting_accounts"]
        columns = await db_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'product_categories'"
            )
        )
        names = {row[0] for row in columns}
        assert not {name for name in names if "account" in name or "cuenta" in name}

    async def test_reimportar_actualiza_en_vez_de_duplicar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        first = (await upload(api, admin_csrf, full_workbook())).json()
        await resolve_everything(api, admin_csrf, first["id"])
        await api.post(f"{IMPORTS}/{first['id']}/commit", headers={"X-CSRF-Token": admin_csrf})

        second = (await upload(api, admin_csrf, full_workbook())).json()
        product_row = (await rows_of(api, second["id"], "PRODUCT"))[0]
        assert product_row["action"] == "UPDATE"
        await resolve_everything(api, admin_csrf, second["id"])
        result = (
            await api.post(f"{IMPORTS}/{second['id']}/commit", headers={"X-CSRF-Token": admin_csrf})
        ).json()
        assert result["by_entity"]["PRODUCT"]["updated"] == 1
        assert (await api.get(PRODUCTS)).json()["total"] == 1

    async def test_no_se_puede_confirmar_dos_veces(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        batch = (await upload(api, admin_csrf, full_workbook())).json()
        await resolve_everything(api, admin_csrf, batch["id"])
        await api.post(f"{IMPORTS}/{batch['id']}/commit", headers={"X-CSRF-Token": admin_csrf})
        again = await api.post(
            f"{IMPORTS}/{batch['id']}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert again.status_code == 409

    async def test_un_fallo_deja_la_base_como_estaba(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """El commit es una transaccion: no puede quedar medio maestro escrito."""
        payload = workbook_bytes(
            {
                "Categoria de producto": [
                    CATEGORY_HEADERS,
                    ["Insumos Taller", None, "Insumos Taller", None],
                ],
                "Productos": [
                    PRODUCT_HEADERS,
                    [
                        "Arcilla",
                        "INS-1",
                        "Insumos Taller",
                        None,
                        None,
                        None,
                        "No",
                        "Si",
                        "No",
                        None,
                        None,
                        "gr",
                        None,
                    ],
                ],
                "Stock": [STOCK_HEADERS, ["Arcilla", "g", 5, "Deposito"]],
            }
        )
        batch = (await upload(api, admin_csrf, payload)).json()
        stock_row = (await rows_of(api, batch["id"], "STOCK"))[0]
        # Se apunta a un producto que no existe: el commit debe fallar entero.
        await resolve(
            api, admin_csrf, batch["id"], [{"row_id": stock_row["id"], "product_id": 424242}]
        )
        response = await api.post(
            f"{IMPORTS}/{batch['id']}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert response.status_code >= 400

        for table in ("products", "product_categories", "stock_balances"):
            assert await count_rows(db_session, table) == 0, table

    async def test_la_importacion_preserva_referencias_y_sincroniza_contadores(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Los productos importados conservan su codigo y sincronizan el contador familiar."""
        payload = workbook_bytes(
            {
                "Categoria de producto": [
                    CATEGORY_HEADERS,
                    ["Insumos Taller", None, "Insumos Taller", 6021031.0],
                    [
                        "Artesanias Greda",
                        "Productos Terminados Taller",
                        "Productos Terminados Taller / Artesanias Greda",
                        6021032.0,
                    ],
                ],
                "Productos": [
                    PRODUCT_HEADERS,
                    [
                        "Vaso Ceramica",
                        "LAB50121",
                        "Productos Terminados Taller / Artesanias Greda",
                        None,
                        None,
                        0.18,
                        "Si",
                        "No",
                        "Si",
                        25.0,
                        10.0,
                        "Unidad",
                        "Unidad",
                    ],
                    [
                        "Arcilla Roja",
                        "LAB70144",
                        "Insumos Taller",
                        None,
                        None,
                        0.18,
                        "No",
                        "Si",
                        "No",
                        None,
                        0.0169,
                        "gr",
                        "kg",
                    ],
                ],
            }
        )
        batch = (await upload(api, admin_csrf, payload)).json()
        await resolve_everything(api, admin_csrf, batch["id"])
        commit = await api.post(
            f"{IMPORTS}/{batch['id']}/commit", headers={"X-CSRF-Token": admin_csrf}
        )
        assert commit.status_code == 200, commit.text

        # 1. Los codigos del excel se preservaron intactos y NO crearon issues en secuencias
        prods = (await api.get(PRODUCTS, params={"limit": 10})).json()["items"]
        refs = {p["internal_reference"] for p in prods}
        assert "LAB50121" in refs
        assert "LAB70144" in refs
        assert await count_rows(db_session, "document_sequence_issues") == 0

        # 2. Las siguientes creaciones manuales arrancan por encima del maximo importado
        cat_term = next(
            p["product_category_id"] for p in prods if p["internal_reference"] == "LAB50121"
        )
        cat_ins = next(
            p["product_category_id"] for p in prods if p["internal_reference"] == "LAB70144"
        )

        nuevo_50 = await api.post(
            PRODUCTS,
            json={
                "name": "Nuevo Terminado Manual",
                "product_type": "FINISHED_PRODUCT",
                "product_category_id": cat_term,
                "base_uom_code": "unit",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert nuevo_50.status_code == 201, nuevo_50.text
        assert nuevo_50.json()["internal_reference"] == "LAB50122"

        nuevo_70 = await api.post(
            PRODUCTS,
            json={
                "name": "Nuevo Insumo Manual",
                "product_type": "RAW_MATERIAL",
                "product_category_id": cat_ins,
                "base_uom_code": "g",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert nuevo_70.status_code == 201, nuevo_70.text
        assert nuevo_70.json()["internal_reference"] == "LAB70145"

        # 3. Solo las 2 creaciones manuales generaron issues auditables
        assert await count_rows(db_session, "document_sequence_issues") == 2
