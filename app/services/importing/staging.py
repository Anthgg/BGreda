"""Construccion del staging: que dice el archivo y que hara el sistema.

Nada de lo que ocurre aqui toca los maestros. El resultado son filas de
``import_rows`` con su accion propuesta, sus avisos y —cuando hace falta una
decision humana— los candidatos entre los que el usuario elegira.

Criterio general: ante la duda, se marca la fila y se pregunta. El importador
no adivina.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.core.masters import (
    ERR_AMBIGUOUS_PRODUCT,
    ERR_DUPLICATE_DOCUMENT,
    ERR_DUPLICATE_REFERENCE,
    ERR_INVALID_DECIMAL,
    ERR_MISSING_REQUIRED,
    ERR_MISSING_ROLE,
    ERR_NEGATIVE_STOCK,
    ERR_UBIGEO_AMBIGUOUS,
    ERR_UBIGEO_NOT_FOUND,
    ERR_UNKNOWN_CATEGORY,
    ERR_UNKNOWN_UOM,
    ERR_UNRESOLVED_PRODUCT,
    ROLE_PENDING,
    WARN_CATEGORY_MISMATCH,
    WARN_DOCUMENT_FORMAT_LOST,
    WARN_DUPLICATE_NAME,
    WARN_LOCATION_MISMATCH,
    WARN_NORMALIZED_LOCATION,
    WARN_RECIPE_DEFERRED,
    WARN_ROUNDED_COST,
    WARN_SOURCE_UOM_MISSING,
    WARN_VARIABLE_PRICE_ZERO,
    fold,
    normalize_document,
    normalize_location,
    normalize_uom,
    parse_boolean,
    parse_decimal,
    product_type_for_path,
    quantize_cost,
)
from app.models.importing import ImportAction, ImportEntity, ImportRow, ImportRowStatus
from app.services.importing.workbook import WorkbookSheet

#: Orden de proceso: las dependencias primero. Sin categorias no hay productos,
#: y sin productos no hay existencias.
ENTITY_ORDER: tuple[ImportEntity, ...] = (
    ImportEntity.PRODUCT_CATEGORY,
    ImportEntity.POS_CATEGORY,
    ImportEntity.PRODUCT,
    ImportEntity.PARTNER,
    ImportEntity.STOCK,
    ImportEntity.RECIPE,
)

#: Palabras demasiado genericas para delatar una categoria mal asignada.
_STOPWORDS = frozenset({"CLASES", "CLASE", "GREDA", "TALLER", "PRODUCTOS", "SERVICIOS"})


@dataclass(slots=True)
class ExistingData:
    """Foto del estado actual de la base contra la que se compara el archivo."""

    categories_by_path: dict[str, int] = field(default_factory=dict)
    category_paths: list[str] = field(default_factory=list)
    pos_categories_by_name: dict[str, int] = field(default_factory=dict)
    units: dict[str, Decimal] = field(default_factory=dict)
    products_by_reference: dict[str, int] = field(default_factory=dict)
    products_by_name: dict[str, list[tuple[int, str, str]]] = field(default_factory=dict)
    partners_by_document: dict[tuple[str, str], int] = field(default_factory=dict)
    ubigeo_by_triplet: dict[tuple[str, str, str], str] = field(default_factory=dict)
    ubigeo_by_district: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    locations_by_name: dict[str, int] = field(default_factory=dict)


class _RowBuilder:
    """Acumula avisos y errores de una fila mientras se normaliza."""

    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.candidates: list[dict[str, Any]] = []
        self.review = False

    def error(self, code: str, message: str, **extra: Any) -> None:
        self.errors.append({"code": code, "message": message, **extra})

    def warn(self, code: str, message: str, **extra: Any) -> None:
        self.warnings.append({"code": code, "message": message, **extra})

    def needs_review(self) -> None:
        self.review = True

    def status(self) -> ImportRowStatus:
        if self.errors:
            return ImportRowStatus.BLOCKED
        if self.review:
            return ImportRowStatus.REVIEW_REQUIRED
        return ImportRowStatus.READY

    def action(self, proposed: ImportAction) -> ImportAction:
        return ImportAction.ERROR if self.errors else proposed


def _decimal_or_error(builder: _RowBuilder, value: Any, field_name: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return parse_decimal(value)
    except ValueError:
        builder.error(
            ERR_INVALID_DECIMAL,
            f"El campo {field_name} no es un numero valido",
            field=field_name,
        )
        return None


def _tax_percent(value: Decimal | None) -> Decimal | None:
    """Convierte la tasa del libro a la convencion del proyecto.

    El libro escribe la **fraccion** (0.18) y el proyecto almacena el
    **porcentaje** (18). La conversion es siempre la misma multiplicacion, sin
    mirar la magnitud: la regla anterior —multiplicar solo si el valor era
    menor o igual que uno— convertia un 1 % legitimo, escrito como 0.01, en
    1 %, pero dejaba pasar un 1 escrito como "1" creyendo que ya era un
    porcentaje. Con dos convenciones posibles para el mismo numero, adivinar
    es equivocarse la mitad de las veces.

    Si el resultado se sale del rango razonable se deja pasar el valor: la
    validacion del maestro lo rechaza con un mensaje claro, que es mejor que
    corregirlo en silencio.
    """
    if value is None:
        return None
    return value * 100


class StagingBuilder:
    """Traduce las hojas del libro en filas de staging."""

    def __init__(self, existing: ExistingData) -> None:
        self._existing = existing
        #: Claves vistas dentro del propio archivo, para detectar duplicados.
        self._seen_references: dict[str, int] = {}
        self._seen_documents: dict[tuple[str, str], int] = {}
        self._batch_paths: set[str] = set()
        #: nombre normalizado -> (referencia, nombre tal cual)
        self._batch_products: dict[str, tuple[str, str]] = {}

    # -- API ----------------------------------------------------------------
    def build(self, sheets: list[WorkbookSheet]) -> tuple[list[ImportRow], dict[str, Any]]:
        rows: list[ImportRow] = []
        snapshot: dict[str, Any] = {"accounting_accounts": [], "recipes": []}

        by_entity = {sheet.entity: sheet for sheet in sheets if sheet.entity is not None}
        for entity in ENTITY_ORDER:
            sheet = by_entity.get(entity)
            if sheet is None:
                continue
            handler = getattr(self, f"_build_{entity.value.lower()}")
            rows.extend(handler(sheet, snapshot))
        return rows, snapshot

    # -- categorias ---------------------------------------------------------
    def _build_product_category(
        self, sheet: WorkbookSheet, snapshot: dict[str, Any]
    ) -> list[ImportRow]:
        rows: list[ImportRow] = []
        for number, values in sheet.rows:
            builder = _RowBuilder()
            name = sheet.value(values, "name")
            parent = sheet.value(values, "parent")
            display_path = sheet.value(values, "display_path")
            if not name:
                builder.error(ERR_MISSING_REQUIRED, "La categoria no tiene nombre")
            path = str(display_path or name or "").strip()

            accounts = {
                key: sheet.value(values, key)
                for key in (
                    "expense_account",
                    "income_account",
                    "valuation_account",
                    "stock_in_account",
                    "stock_out_account",
                )
                if sheet.value(values, key) is not None
            }
            if accounts:
                # Fuera del modelo funcional: se conserva solo como rastro del
                # archivo, no como columna operativa.
                snapshot["accounting_accounts"].append(
                    {"row": number, "category": name, **{k: str(v) for k, v in accounts.items()}}
                )

            existing = self._existing.categories_by_path.get(fold(path))
            action = ImportAction.UPDATE if existing else ImportAction.CREATE
            if fold(path) in self._batch_paths:
                action = ImportAction.SKIP
            self._batch_paths.add(fold(path))

            rows.append(
                ImportRow(
                    entity=ImportEntity.PRODUCT_CATEGORY,
                    sheet_name=sheet.name,
                    source_row=number,
                    sort_order=path.count("/"),
                    raw={
                        "name": name,
                        "parent": parent,
                        "display_path": display_path,
                        **{k: str(v) for k, v in accounts.items()},
                    },
                    normalized={
                        "name": name,
                        "parent_path": str(parent).strip() if parent else None,
                        "display_path": path,
                    },
                    action=builder.action(action),
                    status=builder.status(),
                    errors=builder.errors,
                    warnings=builder.warnings,
                    candidates=[],
                    target_id=str(existing) if existing else None,
                )
            )
        return rows

    def _build_pos_category(
        self, sheet: WorkbookSheet, _snapshot: dict[str, Any]
    ) -> list[ImportRow]:
        rows: list[ImportRow] = []
        for number, values in sheet.rows:
            builder = _RowBuilder()
            name = sheet.value(values, "name")
            parent = sheet.value(values, "parent")
            if not name:
                builder.error(ERR_MISSING_REQUIRED, "La categoria POS no tiene nombre")
            existing = self._existing.pos_categories_by_name.get(fold(name or ""))
            rows.append(
                ImportRow(
                    entity=ImportEntity.POS_CATEGORY,
                    sheet_name=sheet.name,
                    source_row=number,
                    sort_order=0 if not parent else 1,
                    raw={"name": name, "parent": parent},
                    normalized={"name": name, "parent_name": parent},
                    action=builder.action(ImportAction.SKIP if existing else ImportAction.CREATE),
                    status=builder.status(),
                    errors=builder.errors,
                    warnings=builder.warnings,
                    candidates=[],
                    target_id=str(existing) if existing else None,
                )
            )
        return rows

    # -- productos ----------------------------------------------------------
    def _category_mismatch(self, pos_name: str | None, category_path: str) -> str | None:
        """Detecta un producto colocado en la hermana equivocada.

        Compara los terminos distintivos de la categoria de punto de venta con
        las hermanas de la categoria asignada. Si alguno nombra a una hermana
        distinta de la asignada, la fila se marca para revision. No corrige
        nada: solo avisa.
        """
        if not pos_name or "/" not in category_path:
            return None
        parent_path, _, leaf = category_path.rpartition("/")
        siblings = [
            path
            for path in self._existing.category_paths + sorted(self._batch_paths)
            if path.rpartition("/")[0].strip().upper() == fold(parent_path)
            or fold(path.rpartition("/")[0]) == fold(parent_path)
        ]
        tokens = {
            token for token in fold(pos_name).split() if len(token) >= 4 and token not in _STOPWORDS
        }
        if not tokens:
            return None
        for sibling in siblings:
            sibling_leaf = fold(sibling.rpartition("/")[2])
            if sibling_leaf and sibling_leaf in tokens and sibling_leaf != fold(leaf):
                return sibling.strip()
        return None

    def _build_product(self, sheet: WorkbookSheet, _snapshot: dict[str, Any]) -> list[ImportRow]:
        rows: list[ImportRow] = []
        name_counts: dict[str, int] = {}
        for _, values in sheet.rows:
            name = sheet.value(values, "name")
            if name:
                key = fold(name)
                name_counts[key] = name_counts.get(key, 0) + 1

        for number, values in sheet.rows:
            builder = _RowBuilder()
            name = sheet.value(values, "name")
            reference = sheet.value(values, "internal_reference")
            category_path = sheet.value(values, "category")
            pos_category = sheet.value(values, "pos_category")

            if not name:
                builder.error(ERR_MISSING_REQUIRED, "El producto no tiene nombre")
            if not reference:
                builder.error(
                    ERR_MISSING_REQUIRED,
                    "El producto no tiene referencia interna y es la clave de deduplicacion",
                )
            reference_key = str(reference).strip() if reference else ""
            if reference_key:
                previous = self._seen_references.get(reference_key)
                if previous is not None:
                    builder.error(
                        ERR_DUPLICATE_REFERENCE,
                        f"La referencia se repite en la fila {previous} del archivo",
                        first_row=previous,
                    )
                else:
                    self._seen_references[reference_key] = number

            path = str(category_path).strip() if category_path else ""
            if not path:
                builder.error(ERR_UNKNOWN_CATEGORY, "El producto no tiene categoria")
            elif fold(path) not in self._existing.categories_by_path and (
                fold(path) not in self._batch_paths
            ):
                builder.error(
                    ERR_UNKNOWN_CATEGORY,
                    f"La categoria {path!r} no existe ni se crea en este archivo",
                )

            product_type = product_type_for_path(path) if path else None
            if path and product_type is None:
                builder.error(
                    ERR_UNKNOWN_CATEGORY,
                    f"No se pudo derivar el tipo de producto desde {path!r}",
                )

            uom_literal = sheet.value(values, "uom")
            uom = normalize_uom(uom_literal)
            if uom_literal and uom is None:
                builder.error(ERR_UNKNOWN_UOM, f"Unidad desconocida: {uom_literal!r}")
            if uom is None and product_type is not None and product_type.value != "SERVICE":
                # El archivo no la trae y solo un servicio puede prescindir de
                # ella: la decide una persona, no el importador.
                builder.warn(
                    WARN_SOURCE_UOM_MISSING,
                    "El archivo no indica unidad de medida y este tipo la exige",
                    source_uom=None,
                )
                builder.needs_review()

            purchase_literal = sheet.value(values, "purchase_uom")
            purchase_uom = normalize_uom(purchase_literal)
            if purchase_literal and purchase_uom is None:
                builder.error(
                    ERR_UNKNOWN_UOM, f"Unidad de compra desconocida: {purchase_literal!r}"
                )

            raw_cost = sheet.value(values, "cost")
            cost = _decimal_or_error(builder, raw_cost, "Costo")
            normalized_cost: str | None = None
            if cost is not None:
                quantized, rounded = quantize_cost(cost)
                normalized_cost = str(quantized)
                if rounded:
                    builder.warn(
                        WARN_ROUNDED_COST,
                        "El costo del archivo excede la escala de 12 decimales del proyecto",
                        source=str(raw_cost),
                        normalized=normalized_cost,
                    )

            raw_price = sheet.value(values, "sale_price")
            price = _decimal_or_error(builder, raw_price, "Precio de venta")
            if price is not None and price == 0:
                builder.warn(
                    WARN_VARIABLE_PRICE_ZERO,
                    "Precio cero: el maestro lo usa para los productos de precio variable",
                    source=str(raw_price),
                )

            sale_tax = _tax_percent(
                _decimal_or_error(builder, sheet.value(values, "sale_tax"), "Impuesto venta")
            )
            purchase_tax = _tax_percent(
                _decimal_or_error(builder, sheet.value(values, "purchase_tax"), "Impuesto compra")
            )

            if name and name_counts.get(fold(name), 0) > 1:
                builder.warn(
                    WARN_DUPLICATE_NAME,
                    "Otro producto del archivo se llama igual; se distinguen por referencia",
                )

            mismatch = self._category_mismatch(pos_category, path)
            if mismatch:
                builder.warn(
                    WARN_CATEGORY_MISMATCH,
                    f"La categoria de punto de venta {pos_category!r} sugiere {mismatch!r}",
                    pos_category=pos_category,
                    suggested_category=mismatch,
                    assigned_category=path,
                )

            existing = self._existing.products_by_reference.get(reference_key)
            if reference_key and name:
                self._batch_products[fold(name)] = (reference_key, str(name))

            rows.append(
                ImportRow(
                    entity=ImportEntity.PRODUCT,
                    sheet_name=sheet.name,
                    source_row=number,
                    sort_order=0,
                    raw={
                        "name": name,
                        "internal_reference": reference,
                        "category": category_path,
                        "pos_category": pos_category,
                        "cost": None if raw_cost is None else str(raw_cost),
                        "sale_price": None if raw_price is None else str(raw_price),
                        "uom": uom_literal,
                        "purchase_uom": purchase_literal,
                        "sellable": sheet.value(values, "sellable"),
                        "purchasable": sheet.value(values, "purchasable"),
                        "available_in_pos": sheet.value(values, "available_in_pos"),
                    },
                    normalized={
                        "internal_reference": reference_key,
                        "name": name,
                        "product_type": product_type.value if product_type else None,
                        "category_path": path,
                        "pos_category_name": pos_category,
                        "source_uom": uom_literal,
                        "base_uom_code": uom,
                        "purchase_uom_code": purchase_uom,
                        "cost": normalized_cost,
                        "sale_price": None if price is None else str(price),
                        "sale_tax_rate": None if sale_tax is None else str(sale_tax),
                        "purchase_tax_rate": None if purchase_tax is None else str(purchase_tax),
                        "sellable": bool(parse_boolean(sheet.value(values, "sellable"))),
                        "purchasable": bool(parse_boolean(sheet.value(values, "purchasable"))),
                        "available_in_pos": bool(
                            parse_boolean(sheet.value(values, "available_in_pos"))
                        ),
                    },
                    action=builder.action(ImportAction.UPDATE if existing else ImportAction.CREATE),
                    status=builder.status(),
                    errors=builder.errors,
                    warnings=builder.warnings,
                    candidates=[],
                    target_id=str(existing) if existing else None,
                )
            )
        return rows

    # -- terceros -----------------------------------------------------------
    def _resolve_ubigeo(
        self, builder: _RowBuilder, district: Any, province: Any, department: Any, country: Any
    ) -> dict[str, Any] | None:
        if not district:
            builder.warn(ERR_UBIGEO_NOT_FOUND, "El tercero no trae distrito")
            return None
        triplet = (fold(district), fold(province or ""), fold(department or ""))
        code = self._existing.ubigeo_by_triplet.get(triplet)
        if code is not None:
            return {"ubigeo_code": code}

        homonyms = self._existing.ubigeo_by_district.get(fold(district), [])
        if len(homonyms) == 1:
            match = homonyms[0]
            builder.warn(
                WARN_LOCATION_MISMATCH,
                "El catalogo INEI corrige la ubicacion declarada en el archivo",
                source=f"{district} / {province} / {department}",
                canonical=(
                    f"{match['district_name']} / {match['province_name']} / "
                    f"{match['department_name']}"
                ),
                ubigeo_code=match["code"],
            )
            return {"ubigeo_code": match["code"]}
        if homonyms:
            builder.warn(
                ERR_UBIGEO_AMBIGUOUS,
                f"Hay {len(homonyms)} distritos llamados {district!r}; elige cual corresponde",
            )
            builder.candidates.extend(
                {
                    "type": "ubigeo",
                    "code": item["code"],
                    "label": (
                        f"{item['district_name']} / {item['province_name']} / "
                        f"{item['department_name']}"
                    ),
                }
                for item in homonyms[:20]
            )
            builder.needs_review()
            return None

        if country and fold(country) not in {"PERU"}:
            builder.warn(
                ERR_UBIGEO_NOT_FOUND,
                "Direccion fuera de Peru: el catalogo INEI no aplica",
                country=str(country),
            )
            return None
        builder.error(
            ERR_UBIGEO_NOT_FOUND, f"El distrito {district!r} no existe en el catalogo INEI"
        )
        return None

    def _build_partner(self, sheet: WorkbookSheet, _snapshot: dict[str, Any]) -> list[ImportRow]:
        rows: list[ImportRow] = []
        for number, values in sheet.rows:
            builder = _RowBuilder()
            name = sheet.value(values, "name")
            if not name:
                builder.error(ERR_MISSING_REQUIRED, "El tercero no tiene nombre")

            raw_type = sheet.value(values, "document_type")
            raw_number = sheet.value(values, "document_number")
            document_number: str | None = None
            suggestion: str | None = None
            if raw_number is not None:
                document_number, suggestion, needs_review = normalize_document(raw_type, raw_number)
                if needs_review:
                    builder.warn(
                        WARN_DOCUMENT_FORMAT_LOST,
                        "Excel guardo el documento como numero y pudo perder formato",
                        source=str(raw_number),
                        suggested=suggestion,
                        stored=document_number,
                    )
                    builder.needs_review()

            document_type = str(raw_type).strip().upper() if raw_type else None
            key = (document_type or "", document_number or "")
            if document_number:
                previous = self._seen_documents.get(key)
                if previous is not None:
                    builder.error(
                        ERR_DUPLICATE_DOCUMENT,
                        f"El documento se repite en la fila {previous} del archivo",
                        first_row=previous,
                    )
                else:
                    self._seen_documents[key] = number

            ubigeo = self._resolve_ubigeo(
                builder,
                sheet.value(values, "district"),
                sheet.value(values, "province"),
                sheet.value(values, "department"),
                sheet.value(values, "country"),
            )

            # El archivo no dice quien es cliente y quien proveedor. No se
            # infiere por tipo de documento: lo decide el usuario en el preview.
            builder.warn(
                ERR_MISSING_ROLE,
                "Falta clasificar el tercero como CLIENT, SUPPLIER o BOTH",
            )
            builder.needs_review()

            existing = self._existing.partners_by_document.get(key) if document_number else None
            rows.append(
                ImportRow(
                    entity=ImportEntity.PARTNER,
                    sheet_name=sheet.name,
                    source_row=number,
                    sort_order=0,
                    raw={
                        "name": name,
                        "document_type": raw_type,
                        "document_number": None if raw_number is None else str(raw_number),
                        "address": sheet.value(values, "address"),
                        "district": sheet.value(values, "district"),
                        "province": sheet.value(values, "province"),
                        "department": sheet.value(values, "department"),
                        "country": sheet.value(values, "country"),
                        "email": sheet.value(values, "email"),
                        "mobile": sheet.value(values, "mobile"),
                        "account": sheet.value(values, "account"),
                        "bank": sheet.value(values, "bank"),
                    },
                    normalized={
                        "name": name,
                        "role": ROLE_PENDING,
                        "document_type": document_type,
                        "document_number": document_number,
                        "document_suggestion": suggestion,
                        "address": sheet.value(values, "address"),
                        "email": sheet.value(values, "email"),
                        "mobile": _as_text(sheet.value(values, "mobile")),
                        "country": sheet.value(values, "country"),
                        **(ubigeo or {}),
                    },
                    action=builder.action(ImportAction.UPDATE if existing else ImportAction.CREATE),
                    status=builder.status(),
                    errors=builder.errors,
                    warnings=builder.warnings,
                    candidates=builder.candidates,
                    target_id=str(existing) if existing else None,
                )
            )
        return rows

    # -- stock --------------------------------------------------------------
    def _product_candidates(self, raw_name: str) -> list[dict[str, Any]]:
        key = fold(raw_name)
        tokens = [token for token in key.split() if len(token) > 3]
        if not tokens:
            return []
        head = tokens[0]
        found: list[dict[str, Any]] = []
        for folded_name, entries in self._existing.products_by_name.items():
            if head in folded_name:
                found.extend(
                    {"type": "product", "product_id": pid, "reference": ref, "label": label}
                    for pid, ref, label in entries
                )
        # Un producto que se crea en este mismo archivo tambien es candidato:
        # todavia no tiene id, asi que se enlaza por referencia interna.
        for folded_name, (ref, label) in self._batch_products.items():
            if head in folded_name:
                found.append(
                    {"type": "product", "product_id": None, "reference": ref, "label": label}
                )
        return found[:20]

    def _build_stock(self, sheet: WorkbookSheet, _snapshot: dict[str, Any]) -> list[ImportRow]:
        rows: list[ImportRow] = []
        for number, values in sheet.rows:
            builder = _RowBuilder()
            raw_product = sheet.value(values, "product")
            raw_location = sheet.value(values, "location")
            raw_quantity = sheet.value(values, "quantity")
            uom_literal = sheet.value(values, "uom")

            product_id: int | None = None
            reference: str | None = None
            if not raw_product:
                builder.error(ERR_MISSING_REQUIRED, "La fila de stock no indica producto")
            else:
                matches = self._existing.products_by_name.get(fold(raw_product), [])
                # Un producto que nace en este mismo archivo tambien cuenta: se
                # enlaza por referencia y el commit lo resuelve tras crearlo.
                in_batch = self._batch_products.get(fold(raw_product))
                if len(matches) == 1:
                    product_id, reference, _ = matches[0]
                elif not matches and in_batch is not None:
                    reference = in_batch[0]
                elif len(matches) > 1:
                    builder.error(
                        ERR_AMBIGUOUS_PRODUCT,
                        f"Hay {len(matches)} productos llamados {raw_product!r}",
                    )
                else:
                    candidates = self._product_candidates(str(raw_product))
                    builder.candidates.extend(candidates)
                    if candidates:
                        builder.warn(
                            ERR_UNRESOLVED_PRODUCT,
                            "El nombre del archivo no coincide con ningun producto; "
                            "elige uno de los candidatos",
                            source=str(raw_product),
                        )
                        builder.needs_review()
                    else:
                        builder.error(
                            ERR_UNRESOLVED_PRODUCT,
                            f"{raw_product!r} no existe en el maestro de productos",
                        )

            uom = normalize_uom(uom_literal)
            if uom_literal and uom is None:
                builder.error(ERR_UNKNOWN_UOM, f"Unidad desconocida: {uom_literal!r}")

            quantity = _decimal_or_error(builder, raw_quantity, "Cantidad")
            if quantity is not None and quantity < 0:
                # No se corrige sola: el usuario decide en el preview.
                builder.warn(
                    ERR_NEGATIVE_STOCK,
                    "El archivo trae existencia negativa y requiere decision del usuario",
                    source=str(raw_quantity),
                )
                builder.needs_review()

            location_name, renamed = normalize_location(raw_location or "")
            if renamed:
                builder.warn(
                    WARN_NORMALIZED_LOCATION,
                    "La ubicacion del archivo se normaliza al nombre canonico",
                    source=str(raw_location),
                    normalized=location_name,
                )

            rows.append(
                ImportRow(
                    entity=ImportEntity.STOCK,
                    sheet_name=sheet.name,
                    source_row=number,
                    sort_order=0,
                    raw={
                        "product": raw_product,
                        "uom": uom_literal,
                        "quantity": None if raw_quantity is None else str(raw_quantity),
                        "location": raw_location,
                    },
                    normalized={
                        "product_id": product_id,
                        "internal_reference": reference,
                        "uom_code": uom,
                        "quantity": None if quantity is None else str(quantity),
                        "location_name": location_name,
                    },
                    action=builder.action(ImportAction.CREATE),
                    status=builder.status(),
                    errors=builder.errors,
                    warnings=builder.warnings,
                    candidates=builder.candidates,
                    target_id=None,
                )
            )
        return rows

    # -- recetas ------------------------------------------------------------
    def _build_recipe(self, sheet: WorkbookSheet, snapshot: dict[str, Any]) -> list[ImportRow]:
        """Se analizan y se conservan en crudo. No se importan en Fase 3."""
        rows: list[ImportRow] = []
        current_parent: str | None = None
        for number, values in sheet.rows:
            parent = sheet.value(values, "product")
            if parent:
                current_parent = str(parent)
            builder = _RowBuilder()
            builder.warn(
                WARN_RECIPE_DEFERRED,
                "Las recetas se analizan pero su modelo productivo es de Fase 3.5",
            )
            payload = {
                "product": current_parent,
                "quantity": _as_text(sheet.value(values, "quantity")),
                "uom": sheet.value(values, "uom"),
                "component": sheet.value(values, "component"),
                "component_quantity": _as_text(sheet.value(values, "component_quantity")),
                "component_uom": sheet.value(values, "component_uom"),
            }
            snapshot["recipes"].append({"row": number, **payload})
            rows.append(
                ImportRow(
                    entity=ImportEntity.RECIPE,
                    sheet_name=sheet.name,
                    source_row=number,
                    sort_order=0,
                    raw=payload,
                    normalized={},
                    action=ImportAction.SKIP,
                    status=ImportRowStatus.READY,
                    errors=[],
                    warnings=builder.warnings,
                    candidates=[],
                    target_id=None,
                )
            )
        return rows


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        return repr(value)
    return str(value)
