"""Lectura del XLSX y construccion del staging.

Los libros de prueba se generan aqui, en memoria y con datos sinteticos: el
maestro real de la empresa no se versiona.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Any

import pytest
from openpyxl import Workbook

from app.core.masters import (
    ERR_DUPLICATE_DOCUMENT,
    ERR_DUPLICATE_REFERENCE,
    ERR_INVALID_DECIMAL,
    ERR_MISSING_ROLE,
    ERR_NEGATIVE_STOCK,
    ERR_UBIGEO_NOT_FOUND,
    ERR_UNKNOWN_CATEGORY,
    ERR_UNKNOWN_UOM,
    ERR_UNRESOLVED_PRODUCT,
    ROLE_PENDING,
    WARN_CATEGORY_MISMATCH,
    WARN_DOCUMENT_FORMAT_LOST,
    WARN_LOCATION_MISMATCH,
    WARN_NORMALIZED_LOCATION,
    WARN_ROUNDED_COST,
    WARN_SOURCE_UOM_MISSING,
    WARN_VARIABLE_PRICE_ZERO,
)
from app.models.importing import ImportAction, ImportEntity, ImportRowStatus
from app.services.importing.staging import ExistingData, StagingBuilder
from app.services.importing.workbook import WorkbookError, read_workbook

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
CATEGORY_HEADERS = ["Categoria", "Categoria padre", "Nombre a mostrar"]


def build_workbook(sheets: dict[str, list[list[Any]]]) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(title=name)
        for row in rows:
            sheet.append(row)
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def existing_data() -> ExistingData:
    data = ExistingData()
    for index, path in enumerate(
        [
            "Insumos Taller",
            "Insumos Taller / Pastas",
            "Servicios",
            "Servicios / Clases",
            "Servicios / Clases / Adultos",
            "Servicios / Clases / Ninos",
        ],
        start=1,
    ):
        data.categories_by_path[path.upper()] = index
        data.category_paths.append(path)
    data.units = {"g": Decimal(1), "kg": Decimal(1000), "unit": Decimal(1)}
    data.products_by_reference = {"INS-1": 10}
    data.products_by_name = {"ARCILLA BLANCA": [(10, "INS-1", "Arcilla blanca")]}
    data.ubigeo_by_triplet = {("BARRANCO", "LIMA", "LIMA"): "150104"}
    data.ubigeo_by_district = {
        "BARRANCO": [
            {
                "code": "150104",
                "district_name": "BARRANCO",
                "province_name": "LIMA",
                "department_name": "LIMA",
            }
        ],
        "LA PUNTA": [
            {
                "code": "070105",
                "district_name": "LA PUNTA",
                "province_name": "CALLAO",
                "department_name": "CALLAO",
            }
        ],
    }
    return data


def stage(sheets: dict[str, list[list[Any]]]) -> dict[ImportEntity, list[Any]]:
    rows, _snapshot = StagingBuilder(existing_data()).build(read_workbook(build_workbook(sheets)))
    grouped: dict[ImportEntity, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row.entity, []).append(row)
    return grouped


def codes(items: list[dict[str, Any]]) -> set[str]:
    return {item["code"] for item in items}


# ---------------------------------------------------------------------------
# Lectura del archivo
# ---------------------------------------------------------------------------
class TestLectura:
    def test_un_libro_valido_identifica_cada_hoja(self) -> None:
        sheets = read_workbook(
            build_workbook(
                {
                    "Productos": [PRODUCT_HEADERS],
                    "Proveedores y clientes": [PARTNER_HEADERS],
                    "Stock": [STOCK_HEADERS],
                }
            )
        )
        assert [sheet.entity for sheet in sheets] == [
            ImportEntity.PRODUCT,
            ImportEntity.PARTNER,
            ImportEntity.STOCK,
        ]

    def test_las_columnas_se_localizan_por_nombre_no_por_posicion(self) -> None:
        reordered = [
            "Costo",
            "Referencia interna",
            "Nombre",
            "Categoria de producto",
            "Unidad de medida",
        ]
        sheets = read_workbook(build_workbook({"Hoja": [reordered]}))
        assert sheets[0].entity is ImportEntity.PRODUCT
        assert sheets[0].columns["name"] == 2
        assert sheets[0].columns["cost"] == 0

    def test_una_columna_renombrada_se_reporta(self) -> None:
        headers = list(PRODUCT_HEADERS)
        headers[10] = "Costo unitario historico"
        sheets = read_workbook(build_workbook({"Productos": [headers]}))
        assert sheets[0].entity is ImportEntity.PRODUCT
        assert any("cost" in warning for warning in sheets[0].warnings)

    def test_una_hoja_desconocida_no_se_interpreta(self) -> None:
        sheets = read_workbook(build_workbook({"Otra": [["Alfa", "Beta"]]}))
        assert sheets[0].entity is None
        assert sheets[0].warnings

    def test_un_archivo_vacio_se_rechaza(self) -> None:
        with pytest.raises(WorkbookError):
            read_workbook(b"")

    def test_un_archivo_corrupto_se_rechaza(self) -> None:
        with pytest.raises(WorkbookError):
            read_workbook(b"esto no es un xlsx" * 100)

    def test_las_filas_en_blanco_no_cuentan(self) -> None:
        sheets = read_workbook(
            build_workbook(
                {
                    "Stock": [
                        STOCK_HEADERS,
                        ["Arcilla blanca", "g", 100, "Deposito"],
                        [None, None, None, None],
                    ]
                }
            )
        )
        assert len(sheets[0].rows) == 1


# ---------------------------------------------------------------------------
# Productos
# ---------------------------------------------------------------------------
class TestProductos:
    def _sheet(self, rows: list[list[Any]]) -> dict[ImportEntity, list[Any]]:
        return stage({"Productos": [PRODUCT_HEADERS, *rows]})

    def test_un_producto_valido_queda_listo(self) -> None:
        rows = self._sheet(
            [
                [
                    "Arcilla roja",
                    "INS-2",
                    "Insumos Taller / Pastas",
                    None,
                    None,
                    0.18,
                    "No",
                    "Si",
                    "No",
                    None,
                    0.002541200,
                    "gr",
                    "kg",
                ]
            ]
        )[ImportEntity.PRODUCT][0]
        assert rows.status is ImportRowStatus.READY
        assert rows.action is ImportAction.CREATE
        assert rows.normalized["product_type"] == "RAW_MATERIAL"
        assert rows.normalized["base_uom_code"] == "g"
        assert rows.normalized["purchase_uom_code"] == "kg"
        assert rows.normalized["purchase_tax_rate"] == "18.00"

    def test_el_costo_largo_se_redondea_y_conserva_el_original(self) -> None:
        row = self._sheet(
            [
                [
                    "Arcilla",
                    "INS-3",
                    "Insumos Taller / Pastas",
                    None,
                    None,
                    None,
                    "No",
                    "Si",
                    "No",
                    None,
                    0.0169068431372549,
                    "gr",
                    None,
                ]
            ]
        )[ImportEntity.PRODUCT][0]
        assert WARN_ROUNDED_COST in codes(row.warnings)
        assert row.normalized["cost"] == "0.016906843137"
        assert row.raw["cost"] == "0.0169068431372549"

    def test_el_precio_con_separador_de_millares_se_parsea(self) -> None:
        row = self._sheet(
            [
                [
                    "Jarra grande",
                    "TER-1",
                    "Insumos Taller",
                    None,
                    0.18,
                    None,
                    "Si",
                    "No",
                    "Si",
                    "3,500.00",
                    None,
                    "Unidad",
                    None,
                ]
            ]
        )[ImportEntity.PRODUCT][0]
        assert row.normalized["sale_price"] == "3500.00"

    def test_precio_cero_se_importa_con_aviso(self) -> None:
        row = self._sheet(
            [
                [
                    "Clase suelta",
                    "SER-1",
                    "Servicios / Clases / Adultos",
                    "Clases adultos",
                    0.18,
                    None,
                    "Si",
                    "No",
                    "Si",
                    "0.00",
                    None,
                    None,
                    None,
                ]
            ]
        )[ImportEntity.PRODUCT][0]
        assert WARN_VARIABLE_PRICE_ZERO in codes(row.warnings)
        assert row.action is ImportAction.CREATE
        assert row.normalized["product_type"] == "SERVICE"
        assert row.normalized["base_uom_code"] is None

    def test_una_referencia_repetida_bloquea_las_dos_filas(self) -> None:
        rows = self._sheet(
            [
                [
                    "A",
                    "DUP-1",
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
                [
                    "B",
                    "DUP-1",
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
            ]
        )[ImportEntity.PRODUCT]
        assert rows[0].status is ImportRowStatus.READY
        assert rows[1].status is ImportRowStatus.BLOCKED
        assert ERR_DUPLICATE_REFERENCE in codes(rows[1].errors)

    def test_una_categoria_inexistente_bloquea(self) -> None:
        row = self._sheet(
            [
                [
                    "X",
                    "REF-9",
                    "Categoria fantasma",
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
                ]
            ]
        )[ImportEntity.PRODUCT][0]
        assert ERR_UNKNOWN_CATEGORY in codes(row.errors)
        assert row.action is ImportAction.ERROR

    def test_sin_unidad_el_archivo_no_bloquea_pero_pide_decision(self) -> None:
        """Solo un servicio puede prescindir de la unidad; el resto se revisa."""
        row = self._sheet(
            [
                [
                    "Esmalte sin unidad",
                    "PRE-1",
                    "Insumos Taller",
                    None,
                    None,
                    None,
                    "No",
                    "Si",
                    "No",
                    None,
                    None,
                    None,
                    None,
                ]
            ]
        )[ImportEntity.PRODUCT][0]
        assert row.status is ImportRowStatus.REVIEW_REQUIRED
        assert WARN_SOURCE_UOM_MISSING in codes(row.warnings)
        # El preview conserva que el origen venia vacio.
        assert row.normalized["source_uom"] is None
        assert row.normalized["base_uom_code"] is None

    def test_una_unidad_desconocida_bloquea(self) -> None:
        row = self._sheet(
            [
                [
                    "X",
                    "REF-8",
                    "Insumos Taller",
                    None,
                    None,
                    None,
                    "No",
                    "Si",
                    "No",
                    None,
                    None,
                    "cucharadas",
                    None,
                ]
            ]
        )[ImportEntity.PRODUCT][0]
        assert ERR_UNKNOWN_UOM in codes(row.errors)

    def test_un_costo_no_numerico_bloquea(self) -> None:
        row = self._sheet(
            [
                [
                    "X",
                    "REF-7",
                    "Insumos Taller",
                    None,
                    None,
                    None,
                    "No",
                    "Si",
                    "No",
                    None,
                    "casi cero",
                    "gr",
                    None,
                ]
            ]
        )[ImportEntity.PRODUCT][0]
        assert ERR_INVALID_DECIMAL in codes(row.errors)

    def test_un_producto_existente_se_marca_como_actualizacion(self) -> None:
        row = self._sheet(
            [
                [
                    "Arcilla blanca",
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
                ]
            ]
        )[ImportEntity.PRODUCT][0]
        assert row.action is ImportAction.UPDATE
        assert row.target_id == "10"

    def test_la_categoria_pos_incoherente_avisa_sin_corregir(self) -> None:
        row = self._sheet(
            [
                [
                    "Paquete 4 clases ninos",
                    "SER-2",
                    "Servicios / Clases / Adultos",
                    "Clases ninos",
                    0.18,
                    None,
                    "Si",
                    "No",
                    "Si",
                    "400.00",
                    None,
                    None,
                    None,
                ]
            ]
        )[ImportEntity.PRODUCT][0]
        warning = next(item for item in row.warnings if item["code"] == WARN_CATEGORY_MISMATCH)
        assert warning["suggested_category"].endswith("Ninos")
        # La categoria del archivo se respeta: solo se avisa.
        assert row.normalized["category_path"] == "Servicios / Clases / Adultos"


# ---------------------------------------------------------------------------
# Terceros
# ---------------------------------------------------------------------------
class TestTerceros:
    def _sheet(self, rows: list[list[Any]]) -> list[Any]:
        return stage({"Proveedores y clientes": [PARTNER_HEADERS, *rows]})[ImportEntity.PARTNER]

    def test_todo_tercero_espera_clasificacion_del_usuario(self) -> None:
        row = self._sheet(
            [
                [
                    "ACME S.A.",
                    "RUC",
                    "20101194991",
                    "Av. 1",
                    "Barranco",
                    "Lima",
                    "Lima",
                    "Perú",
                    None,
                    None,
                ]
            ]
        )[0]
        assert row.normalized["role"] == ROLE_PENDING
        assert row.status is ImportRowStatus.REVIEW_REQUIRED
        assert ERR_MISSING_ROLE in codes(row.warnings)

    def test_el_ruc_no_se_infiere_como_proveedor(self) -> None:
        row = self._sheet(
            [
                [
                    "ACME S.A.",
                    "RUC",
                    "20101194991",
                    None,
                    "Barranco",
                    "Lima",
                    "Lima",
                    "Perú",
                    None,
                    None,
                ]
            ]
        )[0]
        assert row.normalized["role"] not in {"SUPPLIER", "CLIENT", "BOTH"}

    def test_el_ubigeo_exacto_se_resuelve(self) -> None:
        row = self._sheet(
            [["Cliente", "DNI", "40903769", None, "Barranco", "Lima", "Lima", "Perú", None, None]]
        )[0]
        assert row.normalized["ubigeo_code"] == "150104"

    def test_el_catalogo_corrige_el_departamento_declarado(self) -> None:
        row = self._sheet(
            [["Cliente", "DNI", "73093648", None, "La Punta", "Callao", "Lima", "Perú", None, None]]
        )[0]
        warning = next(item for item in row.warnings if item["code"] == WARN_LOCATION_MISMATCH)
        assert warning["ubigeo_code"] == "070105"
        assert row.normalized["ubigeo_code"] == "070105"

    def test_una_direccion_fuera_de_peru_no_inventa_ubigeo(self) -> None:
        row = self._sheet(
            [
                [
                    "Cliente",
                    "CE",
                    "585664999",
                    None,
                    "Virginia",
                    None,
                    None,
                    "Estados Unidos",
                    None,
                    None,
                ]
            ]
        )[0]
        assert ERR_UBIGEO_NOT_FOUND in codes(row.warnings)
        assert "ubigeo_code" not in row.normalized

    def test_un_dni_mutilado_se_sugiere_pero_no_se_aplica(self) -> None:
        row = self._sheet(
            [["Cliente", "DNI", 1234567, None, None, None, None, "Perú", None, None]]
        )[0]
        assert WARN_DOCUMENT_FORMAT_LOST in codes(row.warnings)
        assert row.normalized["document_number"] == "1234567"
        assert row.normalized["document_suggestion"] == "01234567"

    def test_un_documento_repetido_bloquea(self) -> None:
        rows = self._sheet(
            [
                ["Uno", "RUC", "20101194991", None, None, None, None, "Perú", None, None],
                ["Otro", "RUC", "20101194991", None, None, None, None, "Perú", None, None],
            ]
        )
        assert ERR_DUPLICATE_DOCUMENT in codes(rows[1].errors)
        assert rows[1].status is ImportRowStatus.BLOCKED


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------
class TestStock:
    def _sheet(self, rows: list[list[Any]]) -> list[Any]:
        return stage({"Stock": [STOCK_HEADERS, *rows]})[ImportEntity.STOCK]

    def test_un_nombre_exacto_se_resuelve(self) -> None:
        row = self._sheet([["Arcilla blanca", "g", 120, "Deposito"]])[0]
        assert row.normalized["product_id"] == 10
        assert row.status is ImportRowStatus.READY

    def test_un_nombre_parecido_ofrece_candidatos_sin_elegir(self) -> None:
        row = self._sheet([["ARCILLA BLANCA SACO 25 KG", "g", 120, "Deposito"]])[0]
        assert row.status is ImportRowStatus.REVIEW_REQUIRED
        assert row.normalized["product_id"] is None
        assert row.candidates[0]["reference"] == "INS-1"

    def test_un_producto_creado_en_el_mismo_archivo_tambien_vale(self) -> None:
        """El producto aun no esta en la base, pero nace en este mismo libro."""
        grouped = stage(
            {
                "Productos": [
                    PRODUCT_HEADERS,
                    [
                        "Arcilla roja",
                        "INS-9",
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
                "Stock": [STOCK_HEADERS, ["Arcilla roja", "g", 50, "Deposito"]],
            }
        )
        row = grouped[ImportEntity.STOCK][0]
        assert row.status is ImportRowStatus.READY
        assert row.normalized["internal_reference"] == "INS-9"
        assert row.normalized["product_id"] is None

    def test_un_producto_inexistente_bloquea_la_fila(self) -> None:
        row = self._sheet([["Plato palta", "Unidad", 3, "Tienda"]])[0]
        assert row.status is ImportRowStatus.BLOCKED
        assert ERR_UNRESOLVED_PRODUCT in codes(row.errors)

    def test_la_existencia_negativa_no_se_corrige_sola(self) -> None:
        row = self._sheet([["Arcilla blanca", "g", -5, "Deposito"]])[0]
        assert row.status is ImportRowStatus.REVIEW_REQUIRED
        assert ERR_NEGATIVE_STOCK in codes(row.warnings)
        assert row.normalized["quantity"] == "-5"

    def test_la_ubicacion_con_erratas_se_normaliza_conservando_el_origen(self) -> None:
        row = self._sheet([["Arcilla blanca", "g", 1, "Marino pastor"]])[0]
        assert row.normalized["location_name"] == "Mariano Pastor"
        assert row.raw["location"] == "Marino pastor"
        assert WARN_NORMALIZED_LOCATION in codes(row.warnings)


# ---------------------------------------------------------------------------
# Recetas y categorias
# ---------------------------------------------------------------------------
class TestRecetasYCategorias:
    def test_las_recetas_se_detectan_pero_no_se_importan(self) -> None:
        headers = [
            "Nombre del producto a preparar",
            "Cantidad",
            "Unidad de medida del producto preparado",
            "Insumo",
            "Cantidad del insumo",
            "Unidad de medida del insumo",
        ]
        rows = stage(
            {
                "Recetas": [
                    headers,
                    ["BARNIZ 1", 1.02, "gr", "Arcilla blanca", 0.5, "gr"],
                    [None, None, None, "Cuarzo", 0.52, "gr"],
                ]
            }
        )[ImportEntity.RECIPE]
        assert len(rows) == 2
        assert all(row.action is ImportAction.SKIP for row in rows)
        # La cabecera se arrastra para no perder a que preparado pertenece.
        assert rows[1].raw["product"] == "BARNIZ 1"

    def test_las_categorias_se_ordenan_por_profundidad(self) -> None:
        rows = stage(
            {
                "Categorias": [
                    CATEGORY_HEADERS,
                    ["Insumos Taller", None, "Insumos Taller"],
                    ["Vidrios", "Insumos Taller", "Insumos Taller / Vidrios"],
                ]
            }
        )[ImportEntity.PRODUCT_CATEGORY]
        assert [row.sort_order for row in rows] == [0, 1]
        assert rows[0].action is ImportAction.UPDATE
        assert rows[1].action is ImportAction.CREATE
