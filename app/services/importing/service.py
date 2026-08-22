"""Orquestacion del importador: subir, analizar, revisar, confirmar.

El commit es una unica transaccion. Si algo falla a mitad no queda medio
maestro importado: o entra todo o no entra nada.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.masters import RESOLUTION_USER, ROLE_PENDING, fold
from app.models.catalog import UbigeoDistrict
from app.models.importing import (
    ImportAction,
    ImportBatch,
    ImportEntity,
    ImportRow,
    ImportRowStatus,
    ImportStatus,
)
from app.models.inventory import MovementType
from app.models.masters import (
    Partner,
    PartnerRole,
    PosCategory,
    Product,
    ProductCategory,
    ProductType,
    UnitOfMeasure,
)
from app.models.sequence import SequenceType
from app.schemas.auth import AuthenticatedUser
from app.schemas.imports import RowResolution
from app.services.importing.staging import ExistingData, StagingBuilder
from app.services.importing.workbook import analyze_workbook, read_workbook
from app.services.inventory import InventoryService
from app.services.sequences import SequenceService

MAX_PAGE_SIZE = 500


class ImportNotFoundError(APIError):
    status_code = 404
    code = "IMPORT_BATCH_NOT_FOUND"
    message = "La importacion no existe"


class ImportStateError(APIError):
    status_code = 409
    code = "IMPORT_INVALID_STATE"
    message = "La importacion no esta en un estado que permita esta operacion"


class ImportBlockedError(APIError):
    status_code = 422
    code = "IMPORT_ROWS_PENDING"
    message = "Hay filas sin resolver: revisalas antes de confirmar"


class ImportService:
    def __init__(
        self,
        session: AsyncSession,
        inventory: InventoryService,
        sequences: SequenceService | None = None,
    ) -> None:
        self._session = session
        self._inventory = inventory
        self._sequences = sequences or SequenceService(session)

    # -- carga y analisis ---------------------------------------------------
    async def upload(
        self, *, filename: str, payload: bytes, user: AuthenticatedUser
    ) -> ImportBatch:
        digest = hashlib.sha256(payload).hexdigest()
        duplicate = await self._session.scalar(
            select(ImportBatch)
            .where(ImportBatch.file_hash == digest, ImportBatch.status == ImportStatus.COMMITTED)
            .order_by(ImportBatch.id.desc())
            .limit(1)
        )

        sheets = read_workbook(payload)
        existing = await self._load_existing()
        rows, snapshot = StagingBuilder(existing).build(sheets)

        recipe_rows = [row for row in rows if row.entity is ImportEntity.RECIPE]
        recipe_parents = {row.raw.get("product") for row in recipe_rows if row.raw.get("product")}
        summary = _summarize(rows)
        summary.update(
            {
                "sheets": analyze_workbook(sheets),
                "recipes_detected": len(recipe_parents),
                "recipe_lines_detected": len(recipe_rows),
                "recipes_imported": 0,
                "duplicate_file": duplicate is not None,
                "duplicate_of_batch_id": duplicate.id if duplicate else None,
            }
        )

        batch = ImportBatch(
            filename=filename,
            file_hash=digest,
            file_size=len(payload),
            status=ImportStatus.ANALYZED,
            summary=summary,
            source_snapshot=snapshot,
            created_by=user.id,
            created_by_name=user.display_name,
            analyzed_at=datetime.now(UTC),
        )
        self._session.add(batch)
        await self._session.flush()
        for row in rows:
            row.batch_id = batch.id
            self._session.add(row)
        await self._session.flush()
        return batch

    async def _load_existing(self) -> ExistingData:
        data = ExistingData()
        for category in (await self._session.execute(select(ProductCategory))).scalars():
            data.categories_by_path[fold(category.display_path)] = category.id
            data.category_paths.append(category.display_path)
        for pos in (await self._session.execute(select(PosCategory))).scalars():
            data.pos_categories_by_name[fold(pos.name)] = pos.id
        for unit in (await self._session.execute(select(UnitOfMeasure))).scalars():
            data.units[unit.code] = unit.factor_to_base
        for product in (await self._session.execute(select(Product))).scalars():
            data.products_by_reference[product.internal_reference] = product.id
            data.products_by_name.setdefault(fold(product.name), []).append(
                (product.id, product.internal_reference, product.name)
            )
        for partner in (await self._session.execute(select(Partner))).scalars():
            if partner.document_type and partner.document_number:
                data.partners_by_document[(str(partner.document_type), partner.document_number)] = (
                    partner.id
                )
        for district in (await self._session.execute(select(UbigeoDistrict))).scalars():
            triplet = (
                fold(district.district_name),
                fold(district.province_name),
                fold(district.department_name),
            )
            data.ubigeo_by_triplet[triplet] = district.code
            data.ubigeo_by_district.setdefault(fold(district.district_name), []).append(
                {
                    "code": district.code,
                    "district_name": district.district_name,
                    "province_name": district.province_name,
                    "department_name": district.department_name,
                }
            )
        return data

    # -- consulta -----------------------------------------------------------
    async def get_batch(self, batch_id: int) -> ImportBatch:
        batch = await self._session.get(ImportBatch, batch_id)
        if batch is None:
            raise ImportNotFoundError()
        return batch

    async def list_batches(self, *, limit: int = 20) -> tuple[list[ImportBatch], int]:
        total = await self._session.scalar(select(func.count()).select_from(ImportBatch))
        rows = await self._session.execute(
            select(ImportBatch).order_by(ImportBatch.id.desc()).limit(max(1, min(limit, 100)))
        )
        return list(rows.scalars().all()), int(total or 0)

    async def preview(
        self,
        batch_id: int,
        *,
        entity: ImportEntity | None = None,
        status: ImportRowStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[ImportBatch, list[ImportRow], int]:
        batch = await self.get_batch(batch_id)
        stmt = select(ImportRow).where(ImportRow.batch_id == batch.id)
        if entity is not None:
            stmt = stmt.where(ImportRow.entity == entity)
        if status is not None:
            stmt = stmt.where(ImportRow.status == status)
        total = await self._session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = await self._session.execute(
            stmt.order_by(ImportRow.entity, ImportRow.sort_order, ImportRow.source_row)
            .limit(max(1, min(limit, MAX_PAGE_SIZE)))
            .offset(max(0, offset))
        )
        return batch, list(rows.scalars().all()), int(total or 0)

    # -- resolucion ---------------------------------------------------------
    async def resolve(self, batch_id: int, resolutions: list[RowResolution]) -> ImportBatch:
        batch = await self.get_batch(batch_id)
        if batch.status in {ImportStatus.COMMITTED, ImportStatus.CANCELLED}:
            raise ImportStateError()

        by_id = {resolution.row_id: resolution for resolution in resolutions}
        rows = (
            await self._session.execute(
                select(ImportRow).where(
                    ImportRow.batch_id == batch.id, ImportRow.id.in_(by_id.keys())
                )
            )
        ).scalars()

        for row in rows:
            decision = by_id[row.id]
            payload = dict(row.normalized)
            if decision.action == "SKIP":
                row.action = ImportAction.SKIP
                row.status = ImportRowStatus.RESOLVED
                row.resolution = decision.model_dump(exclude_none=True)
                continue

            if decision.product_id is not None:
                payload["product_id"] = decision.product_id
            if decision.base_uom_code is not None:
                # Se conserva que el archivo no la traia y quien la decidio.
                payload["base_uom_code"] = decision.base_uom_code
                payload["resolved_uom"] = decision.base_uom_code
                payload["resolution_source"] = RESOLUTION_USER
            if decision.partner_role is not None:
                payload["role"] = decision.partner_role.value
            if decision.ubigeo_code is not None:
                payload["ubigeo_code"] = decision.ubigeo_code
            if decision.document_number is not None:
                payload["document_number"] = decision.document_number
            elif decision.accept_suggestion and payload.get("document_suggestion"):
                payload["document_number"] = payload["document_suggestion"]

            row.normalized = payload
            row.resolution = decision.model_dump(exclude_none=True)
            row.status = (
                ImportRowStatus.RESOLVED
                if not self._still_pending(row)
                else ImportRowStatus.REVIEW_REQUIRED
            )
            if row.action is ImportAction.ERROR and row.status is ImportRowStatus.RESOLVED:
                row.action = ImportAction.CREATE

        await self._session.flush()
        await self._refresh_summary(batch)
        return batch

    def _still_pending(self, row: ImportRow) -> bool:
        payload = row.normalized
        if row.entity is ImportEntity.PARTNER:
            return payload.get("role") in (None, ROLE_PENDING)
        if row.entity is ImportEntity.STOCK:
            return payload.get("product_id") is None and not payload.get("internal_reference")
        if row.entity is ImportEntity.PRODUCT:
            # Solo un servicio puede quedarse sin unidad.
            return payload.get("product_type") != "SERVICE" and not payload.get("base_uom_code")
        return False

    async def _refresh_summary(self, batch: ImportBatch) -> None:
        rows = (
            await self._session.execute(select(ImportRow).where(ImportRow.batch_id == batch.id))
        ).scalars()
        summary = dict(batch.summary)
        summary.update(_summarize(list(rows)))
        batch.summary = summary
        batch.status = (
            ImportStatus.READY
            if summary.get("review_required", 0) == 0 and summary.get("errors", 0) == 0
            else ImportStatus.ANALYZED
        )
        await self._session.flush()

    # -- commit -------------------------------------------------------------
    async def commit(self, batch_id: int, user: AuthenticatedUser) -> dict[str, Any]:
        batch = await self.get_batch(batch_id)
        if batch.status is ImportStatus.COMMITTED:
            raise ImportStateError("La importacion ya se confirmo")
        if batch.status in {ImportStatus.CANCELLED, ImportStatus.FAILED}:
            raise ImportStateError()

        rows = list(
            (
                await self._session.execute(
                    select(ImportRow)
                    .where(ImportRow.batch_id == batch.id)
                    .order_by(ImportRow.entity, ImportRow.sort_order, ImportRow.source_row)
                )
            ).scalars()
        )
        pending = [
            row
            for row in rows
            if row.status in {ImportRowStatus.BLOCKED, ImportRowStatus.REVIEW_REQUIRED}
        ]
        if pending:
            raise ImportBlockedError(
                f"Quedan {len(pending)} filas sin resolver: revisalas antes de confirmar"
            )

        result: dict[str, dict[str, int]] = {}
        by_entity: dict[ImportEntity, list[ImportRow]] = {}
        for row in rows:
            by_entity.setdefault(row.entity, []).append(row)

        await self._commit_categories(by_entity.get(ImportEntity.PRODUCT_CATEGORY, []), result)
        await self._commit_pos_categories(by_entity.get(ImportEntity.POS_CATEGORY, []), result)
        await self._commit_products(by_entity.get(ImportEntity.PRODUCT, []), result)
        await self._commit_partners(by_entity.get(ImportEntity.PARTNER, []), result)
        await self._commit_stock(by_entity.get(ImportEntity.STOCK, []), batch, user, result)

        for row in by_entity.get(ImportEntity.RECIPE, []):
            row.status = ImportRowStatus.COMMITTED
        bucket = result.setdefault(
            ImportEntity.RECIPE.value, {"created": 0, "updated": 0, "skipped": 0}
        )
        bucket["skipped"] = len(by_entity.get(ImportEntity.RECIPE, []))

        batch.status = ImportStatus.COMMITTED
        batch.confirmed_at = datetime.now(UTC)
        batch.completed_at = datetime.now(UTC)
        summary = dict(batch.summary)
        summary["committed"] = result
        batch.summary = summary
        await self._session.flush()
        return result

    def _count(self, result: dict[str, dict[str, int]], entity: ImportEntity, key: str) -> None:
        bucket = result.setdefault(entity.value, {"created": 0, "updated": 0, "skipped": 0})
        bucket[key] += 1

    async def _commit_categories(
        self, rows: list[ImportRow], result: dict[str, dict[str, int]]
    ) -> None:
        by_path: dict[str, ProductCategory] = {}
        for category in (await self._session.execute(select(ProductCategory))).scalars():
            by_path[fold(category.display_path)] = category

        for row in sorted(rows, key=lambda item: item.sort_order):
            payload = row.normalized
            path = str(payload.get("display_path") or "")
            if row.action is ImportAction.SKIP or not path:
                row.status = ImportRowStatus.COMMITTED
                self._count(result, ImportEntity.PRODUCT_CATEGORY, "skipped")
                continue

            parent_path = payload.get("parent_path")
            parent = by_path.get(fold(parent_path)) if parent_path else None
            if parent is None and parent_path:
                # El padre puede venir por nombre suelto en vez de por ruta.
                parent = next(
                    (
                        candidate
                        for key, candidate in by_path.items()
                        if key.rpartition("/")[2].strip() == fold(parent_path)
                    ),
                    None,
                )

            existing = by_path.get(fold(path))
            if existing is None:
                existing = ProductCategory(
                    name=str(payload.get("name")),
                    parent_id=parent.id if parent else None,
                    display_path=path,
                )
                self._session.add(existing)
                await self._session.flush()
                self._count(result, ImportEntity.PRODUCT_CATEGORY, "created")
            else:
                existing.name = str(payload.get("name"))
                existing.parent_id = parent.id if parent else None
                self._count(result, ImportEntity.PRODUCT_CATEGORY, "updated")
            by_path[fold(path)] = existing
            row.target_table = "product_categories"
            row.target_id = str(existing.id)
            row.status = ImportRowStatus.COMMITTED

    async def _commit_pos_categories(
        self, rows: list[ImportRow], result: dict[str, dict[str, int]]
    ) -> None:
        by_name: dict[str, PosCategory] = {
            fold(item.name): item
            for item in (await self._session.execute(select(PosCategory))).scalars()
        }
        for row in sorted(rows, key=lambda item: item.sort_order):
            payload = row.normalized
            name = str(payload.get("name") or "")
            if row.action is ImportAction.SKIP or not name:
                row.status = ImportRowStatus.COMMITTED
                self._count(result, ImportEntity.POS_CATEGORY, "skipped")
                continue
            existing = by_name.get(fold(name))
            if existing is None:
                parent_name = payload.get("parent_name")
                parent = by_name.get(fold(parent_name)) if parent_name else None
                existing = PosCategory(name=name, parent_id=parent.id if parent else None)
                self._session.add(existing)
                await self._session.flush()
                by_name[fold(name)] = existing
                self._count(result, ImportEntity.POS_CATEGORY, "created")
            else:
                self._count(result, ImportEntity.POS_CATEGORY, "skipped")
            row.target_table = "pos_categories"
            row.target_id = str(existing.id)
            row.status = ImportRowStatus.COMMITTED

    async def _commit_products(
        self, rows: list[ImportRow], result: dict[str, dict[str, int]]
    ) -> None:
        categories = {
            fold(item.display_path): item.id
            for item in (await self._session.execute(select(ProductCategory))).scalars()
        }
        pos_categories = {
            fold(item.name): item.id
            for item in (await self._session.execute(select(PosCategory))).scalars()
        }
        products = {
            item.internal_reference: item
            for item in (await self._session.execute(select(Product))).scalars()
        }

        max_50 = 0
        max_70 = 0
        import re

        for row in rows:
            payload = row.normalized
            reference = str(payload.get("internal_reference") or "")
            if row.action is ImportAction.SKIP or not reference:
                row.status = ImportRowStatus.COMMITTED
                self._count(result, ImportEntity.PRODUCT, "skipped")
                continue

            m50 = re.match(r"^LAB50(\d+)$", reference)
            if m50:
                max_50 = max(max_50, int(m50.group(1)))
            m70 = re.match(r"^LAB70(\d+)$", reference)
            if m70:
                max_70 = max(max_70, int(m70.group(1)))

            category_id = categories.get(fold(payload.get("category_path") or ""))
            pos_id = pos_categories.get(fold(payload.get("pos_category_name") or ""))
            values: dict[str, Any] = {
                "name": payload.get("name"),
                "product_type": ProductType(str(payload.get("product_type"))),
                "product_category_id": category_id,
                "pos_category_id": pos_id,
                "base_uom_code": payload.get("base_uom_code"),
                "purchase_uom_code": payload.get("purchase_uom_code"),
                "cost": _decimal(payload.get("cost")),
                "sale_price": _decimal(payload.get("sale_price")),
                "sale_tax_rate": _decimal(payload.get("sale_tax_rate")),
                "purchase_tax_rate": _decimal(payload.get("purchase_tax_rate")),
                "sellable": bool(payload.get("sellable")),
                "purchasable": bool(payload.get("purchasable")),
                "available_in_pos": bool(payload.get("available_in_pos")),
            }

            product = products.get(reference)
            if product is None:
                product = Product(internal_reference=reference, **values)
                self._session.add(product)
                await self._session.flush()
                products[reference] = product
                self._count(result, ImportEntity.PRODUCT, "created")
            else:
                for field_name, value in values.items():
                    setattr(product, field_name, value)
                self._count(result, ImportEntity.PRODUCT, "updated")
            row.target_table = "products"
            row.target_id = str(product.id)
            row.status = ImportRowStatus.COMMITTED

        if max_50 > 0:
            await self._sequences.synchronize(SequenceType.PRODUCT_50, max_50)
        if max_70 > 0:
            await self._sequences.synchronize(SequenceType.PRODUCT_70, max_70)

    async def _commit_partners(
        self, rows: list[ImportRow], result: dict[str, dict[str, int]]
    ) -> None:
        districts = {
            item.code: item
            for item in (await self._session.execute(select(UbigeoDistrict))).scalars()
        }
        partners = {
            (str(item.document_type), item.document_number): item
            for item in (await self._session.execute(select(Partner))).scalars()
            if item.document_type and item.document_number
        }

        for row in rows:
            payload = row.normalized
            if row.action is ImportAction.SKIP:
                row.status = ImportRowStatus.COMMITTED
                self._count(result, ImportEntity.PARTNER, "skipped")
                continue

            code = payload.get("ubigeo_code")
            district = districts.get(str(code)) if code else None
            values: dict[str, Any] = {
                "name": payload.get("name"),
                "role": PartnerRole(str(payload.get("role"))),
                "document_type": payload.get("document_type"),
                "document_number": payload.get("document_number"),
                "address": payload.get("address"),
                "email": payload.get("email"),
                "mobile": payload.get("mobile"),
                "ubigeo_code": district.code if district else None,
                # La jerarquia la manda el catalogo, no el texto del archivo.
                "district": district.district_name if district else None,
                "province": district.province_name if district else None,
                "department": district.department_name if district else None,
                "country": "Peru" if district else payload.get("country"),
            }

            key = (str(values["document_type"]), str(values["document_number"]))
            partner = partners.get(key) if values["document_number"] else None
            if partner is None:
                partner = Partner(**values)
                self._session.add(partner)
                await self._session.flush()
                if values["document_number"]:
                    partners[key] = partner
                self._count(result, ImportEntity.PARTNER, "created")
            else:
                for field_name, value in values.items():
                    setattr(partner, field_name, value)
                self._count(result, ImportEntity.PARTNER, "updated")
            row.target_table = "partners"
            row.target_id = str(partner.id)
            row.status = ImportRowStatus.COMMITTED

    async def _commit_stock(
        self,
        rows: list[ImportRow],
        batch: ImportBatch,
        user: AuthenticatedUser,
        result: dict[str, dict[str, int]],
    ) -> None:
        units = {
            item.code: item.factor_to_base
            for item in (await self._session.execute(select(UnitOfMeasure))).scalars()
        }
        by_reference = {
            item.internal_reference: item
            for item in (await self._session.execute(select(Product))).scalars()
        }
        for row in rows:
            payload = row.normalized
            product_id = payload.get("product_id")
            reference = payload.get("internal_reference")
            if row.action is ImportAction.SKIP or (product_id is None and not reference):
                row.status = ImportRowStatus.COMMITTED
                self._count(result, ImportEntity.STOCK, "skipped")
                continue

            product = (
                await self._session.get(Product, int(product_id))
                if product_id is not None
                else by_reference.get(str(reference))
            )
            if product is None:
                raise ImportBlockedError("Una fila de stock apunta a un producto inexistente")

            quantity = _decimal(payload.get("quantity")) or Decimal(0)
            source_uom = payload.get("uom_code") or product.base_uom_code
            if source_uom and product.base_uom_code and source_uom != product.base_uom_code:
                factor_from = units.get(str(source_uom), Decimal(1))
                factor_to = units.get(str(product.base_uom_code), Decimal(1))
                quantity = (quantity * factor_from) / factor_to

            location = await self._inventory.get_or_create_location(
                str(payload.get("location_name") or "Sin ubicacion")
            )
            if quantity != 0:
                await self._inventory.apply_movement(
                    product=product,
                    location=location,
                    quantity=quantity,
                    movement_type=MovementType.INITIAL_IMPORT,
                    reason=f"Importacion inicial (lote {batch.id})",
                    user_id=user.id,
                    user_name=user.display_name,
                    import_batch_id=batch.id,
                )
            row.target_table = "stock_balances"
            row.target_id = f"{product.id}:{location.id}"
            row.status = ImportRowStatus.COMMITTED
            self._count(result, ImportEntity.STOCK, "created")


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _summarize(rows: list[ImportRow]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "creates": 0,
        "updates": 0,
        "skips": 0,
        "errors": 0,
        "warnings": 0,
        "review_required": 0,
        "by_entity": {},
    }
    for row in rows:
        bucket = summary["by_entity"].setdefault(
            row.entity.value,
            {"creates": 0, "updates": 0, "skips": 0, "errors": 0, "review_required": 0},
        )
        if row.action is ImportAction.CREATE:
            summary["creates"] += 1
            bucket["creates"] += 1
        elif row.action is ImportAction.UPDATE:
            summary["updates"] += 1
            bucket["updates"] += 1
        elif row.action is ImportAction.SKIP:
            summary["skips"] += 1
            bucket["skips"] += 1
        else:
            summary["errors"] += 1
            bucket["errors"] += 1
        summary["warnings"] += len(row.warnings)
        if row.status in {ImportRowStatus.REVIEW_REQUIRED, ImportRowStatus.BLOCKED}:
            summary["review_required"] += 1
            bucket["review_required"] += 1
    return summary
