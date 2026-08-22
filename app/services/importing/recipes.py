"""Servicio de importacion y conversion de recetas desde staging a produccion.

No vuelve a leer el archivo Excel: opera exclusivamente sobre las filas de
staging preservadas en `import_rows` con entity='RECIPE'.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.masters import fold
from app.core.recipes import (
    BASE_PERCENTAGE_TARGET,
    PERCENTAGE_TOLERANCE,
    compute_recipe_fingerprint,
    normalize_component_unit_cost_to_grams,
    validate_recipe_percentages,
)
from app.models.audit import AuditAction
from app.models.importing import ImportEntity, ImportRow, ImportRowStatus
from app.models.masters import Product, ProductType
from app.models.recipes import RecipeComponentType
from app.schemas.auth import AuthenticatedUser
from app.schemas.recipes import (
    RecipeCreate,
    RecipeImportPreviewOut,
    RecipeLineIn,
    RecipeRowResolutionIn,
    RecipeStagingGroupOut,
    RecipeStagingLineOut,
    RecipeVersionIn,
)
from app.services.audit import AuditRecorder
from app.services.recipes import RecipeService


class RecipeImportError(APIError):
    status_code = 422
    code = "RECIPE_IMPORT_ERROR"
    message = "Error en la importacion de recetas"


COLORANT_KEYWORDS = ("ÓXIDO", "OXIDO", "CARBONATO DE COBRE", "OCRE", "PIGMENTO", "ESMALTE")
ADDITIVE_KEYWORDS = ("BENTONITA", "GOMA", "SUSPENSIVO", "CMC")


class RecipeImportService:
    """Procesamiento transaccional de recetas desde staging."""

    def __init__(
        self,
        session: AsyncSession,
        recipe_service: RecipeService,
        audit: AuditRecorder | None = None,
    ) -> None:
        self._session = session
        self._recipe_service = recipe_service
        self._audit = audit or AuditRecorder(session)

    async def preview(self, batch_id: int) -> RecipeImportPreviewOut:
        """Analiza las 592 filas de staging de recetas y genera un preview detallado."""
        rows = await self._get_staging_rows(batch_id)
        if not rows:
            raise RecipeImportError(f"No hay filas de recetas en el lote {batch_id}")

        all_products = list((await self._session.execute(select(Product))).scalars().all())
        products_by_fold: dict[str, Product] = {fold(p.name): p for p in all_products}

        recipes_by_prod_name = defaultdict(list)
        for r in rows:
            prod_name = r.raw.get("product")
            if prod_name:
                recipes_by_prod_name[prod_name].append(r)

        groups_out: list[RecipeStagingGroupOut] = []
        ready_count = 0
        review_count = 0
        error_count = 0

        for prod_name, group_rows in recipes_by_prod_name.items():
            target_prod = products_by_fold.get(fold(prod_name))
            target_qty_raw = group_rows[0].raw.get("quantity")
            target_uom_raw = group_rows[0].raw.get("uom")

            warnings: list[str] = []
            errors: list[str] = []

            if target_prod is None:
                errors.append(
                    f"Producto destino '{prod_name}' no existe en el catalogo de productos"
                )
            elif target_prod.product_type != ProductType.PREPARED_MATERIAL:
                type_val = target_prod.product_type.value
                msg = (
                    f"El producto '{target_prod.name}' es de tipo {type_val}, no PREPARED_MATERIAL"
                )
                errors.append(msg)

            staging_lines: list[RecipeStagingLineOut] = []
            parsed_tuples: list[tuple[RecipeComponentType, Decimal]] = []

            # Analisis preliminar de lineas
            raw_lines_data = []
            total_raw_sum = Decimal(0)
            for r in group_rows:
                comp_name = r.raw.get("component") or ""
                comp_prod = products_by_fold.get(fold(comp_name))
                qty_raw = Decimal(str(r.raw.get("component_quantity") or 0))
                total_raw_sum += qty_raw
                raw_lines_data.append((r, comp_name, comp_prod, qty_raw))

            # Clasificacion de componentes
            for r, comp_name, comp_prod, qty_raw in raw_lines_data:
                if comp_prod is None:
                    errors.append(f"Componente '{comp_name}' no existe en catalogo de productos")

                # Si la fila tiene resolucion humana explicita en staging, respetarla
                resolution = r.resolution or {}
                if resolution.get("component_type"):
                    comp_type = RecipeComponentType(resolution["component_type"])
                else:
                    comp_type = self._classify_component_type(comp_name, qty_raw, total_raw_sum)

                percentage = qty_raw * Decimal(100)
                parsed_tuples.append((comp_type, percentage))

                staging_lines.append(
                    RecipeStagingLineOut(
                        row_id=r.id,
                        source_row=r.source_row,
                        component_name_raw=comp_name,
                        component_product_id=comp_prod.id if comp_prod else None,
                        component_reference=comp_prod.internal_reference if comp_prod else None,
                        component_product_name=comp_prod.name if comp_prod else None,
                        component_type=comp_type,
                        percentage=percentage,
                        quantity_raw=qty_raw,
                        uom_raw=r.raw.get("component_uom"),
                    )
                )

            # Validar proporciones
            base_total = Decimal(0)
            add_total = Decimal(0)
            yield_factor = Decimal(1)

            try:
                base_total, add_total, yield_factor = validate_recipe_percentages(parsed_tuples)
            except Exception as exc:
                warnings.append(str(exc))

            # Costo estimado
            estimated_cost = Decimal(0)
            if target_prod and not errors:
                for line_out in staging_lines:
                    if line_out.component_product_id:
                        comp_p = next(
                            (p for p in all_products if p.id == line_out.component_product_id), None
                        )
                        if comp_p and comp_p.cost:
                            c_cost_g = normalize_component_unit_cost_to_grams(
                                comp_p.cost, comp_p.base_uom_code
                            )
                            estimated_cost += (line_out.percentage / Decimal(100)) * c_cost_g
                if yield_factor > Decimal(0):
                    estimated_cost = estimated_cost / yield_factor

            is_valid = (
                len(errors) == 0
                and abs(base_total - BASE_PERCENTAGE_TARGET) <= PERCENTAGE_TOLERANCE
            )

            if errors:
                error_count += 1
            elif not is_valid or warnings:
                review_count += 1
            else:
                ready_count += 1

            groups_out.append(
                RecipeStagingGroupOut(
                    target_product_id=target_prod.id if target_prod else 0,
                    target_internal_reference=target_prod.internal_reference if target_prod else "",
                    target_product_name=target_prod.name if target_prod else prod_name,
                    recipe_name=prod_name,
                    target_quantity=Decimal(str(target_qty_raw))
                    if target_qty_raw is not None
                    else None,
                    target_uom=target_uom_raw,
                    base_total=base_total,
                    additional_total=add_total,
                    yield_factor=yield_factor,
                    estimated_cost_per_gram=estimated_cost,
                    is_valid=is_valid,
                    warnings=warnings,
                    errors=errors,
                    lines=staging_lines,
                )
            )

        return RecipeImportPreviewOut(
            batch_id=batch_id,
            recipes_detected=len(groups_out),
            lines_detected=len(rows),
            ready_count=ready_count,
            review_required_count=review_count,
            error_count=error_count,
            recipes=groups_out,
        )

    async def resolve(
        self,
        batch_id: int,
        resolutions: list[RecipeRowResolutionIn],
        user: AuthenticatedUser,
    ) -> RecipeImportPreviewOut:
        """Aplica decisiones humanas sobre lineas individuales de staging."""
        for res in resolutions:
            row = await self._session.get(ImportRow, res.row_id)
            if row and row.batch_id == batch_id and row.entity == ImportEntity.RECIPE:
                res_dict = dict(row.resolution or {})
                if res.component_type:
                    res_dict["component_type"] = res.component_type.value
                if res.percentage is not None:
                    res_dict["percentage"] = str(res.percentage)
                res_dict["action"] = res.action
                res_dict["resolved_by"] = user.email or user.id
                row.resolution = res_dict
                row.status = ImportRowStatus.RESOLVED

        await self._session.flush()
        return await self.preview(batch_id)

    async def commit(
        self,
        batch_id: int,
        user: AuthenticatedUser,
    ) -> dict[str, Any]:
        """Convierte atomicamente las 93 recetas de staging en recetas productivas."""
        preview_data = await self.preview(batch_id)
        if preview_data.error_count > 0:
            msg = (
                f"El lote contiene {preview_data.error_count} recetas con errores: "
                "resuelvelas antes de confirmar"
            )
            raise RecipeImportError(msg)

        created_count = 0
        updated_count = 0
        skipped_count = 0

        for group in preview_data.recipes:
            # Construir lineas de entrada
            lines_in: list[RecipeLineIn] = [
                RecipeLineIn(
                    component_product_id=line.component_product_id,  # type: ignore[arg-type]
                    component_type=line.component_type,
                    percentage=line.percentage,
                    sort_order=idx,
                )
                for idx, line in enumerate(group.lines)
            ]

            existing_recipe = await self._recipe_service.get_recipe_by_product_id(
                group.target_product_id
            )

            if existing_recipe is None:
                # Crear nueva receta con V1 ACTIVE
                await self._recipe_service.create_recipe(
                    RecipeCreate(
                        product_id=group.target_product_id,
                        name=group.recipe_name,
                        lines=lines_in,
                        notes=f"Importado de staging (Rendimiento: {group.yield_factor:.4f})",
                        active=True,
                        activate_immediately=True,
                    ),
                    user=user,
                )
                created_count += 1
            else:
                # Comprobar fingerprint para idempotencia
                fingerprint_items = [
                    (
                        item.component_product_id,
                        item.component_type,
                        item.percentage,
                        item.sort_order,
                    )
                    for item in lines_in
                ]
                fp = compute_recipe_fingerprint(fingerprint_items)
                if (
                    existing_recipe.current_version
                    and existing_recipe.current_version.fingerprint == fp
                ):
                    skipped_count += 1
                else:
                    # Crear nueva version activa
                    y_factor = group.yield_factor
                    notes = f"Actualizacion importada de staging (Rendimiento: {y_factor:.4f})"
                    await self._recipe_service.create_version(
                        existing_recipe.id,
                        RecipeVersionIn(
                            lines=lines_in,
                            notes=notes,
                        ),
                        user=user,
                        activate=True,
                    )
                    updated_count += 1

        # Marcar las filas de staging como COMMITTED
        rows = await self._get_staging_rows(batch_id)
        for r in rows:
            r.status = ImportRowStatus.COMMITTED

        await self._session.flush()
        audit_msg = (
            f"Importadas {created_count} recetas, "
            f"{updated_count} actualizadas, {skipped_count} sin cambios"
        )
        self._record(
            batch_id,
            "recipe_import",
            AuditAction.CREATE,
            user,
            audit_msg,
        )

        return {
            "batch_id": batch_id,
            "recipes_detected": preview_data.recipes_detected,
            "created": created_count,
            "updated": updated_count,
            "skipped": skipped_count,
        }

    def _record(
        self,
        entity_id: Any,
        entity_type: str,
        action: AuditAction,
        user: AuthenticatedUser,
        label: str,
    ) -> None:
        self._audit.record_action(
            entity_type=entity_type,
            entity_id=str(entity_id),
            action=action,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"label": label},
        )

    async def _get_staging_rows(self, batch_id: int) -> list[ImportRow]:
        stmt = (
            select(ImportRow)
            .where(ImportRow.batch_id == batch_id, ImportRow.entity == ImportEntity.RECIPE)
            .order_by(ImportRow.source_row.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    def _classify_component_type(
        self,
        comp_name: str,
        qty_raw: Decimal,
        total_raw_sum: Decimal,
    ) -> RecipeComponentType:
        """Clasifica funcionalmente una linea segun su naturaleza ceramica."""
        # Si la suma total de la formula es 1.0 (100%), todos los componentes son base
        if abs(total_raw_sum - Decimal(1)) <= Decimal("0.0001"):
            return RecipeComponentType.BASE

        upper = fold(comp_name)
        if any(k in upper for k in ADDITIVE_KEYWORDS):
            return RecipeComponentType.ADDITIVE
        if any(k in upper for k in COLORANT_KEYWORDS):
            return RecipeComponentType.COLORANT

        return RecipeComponentType.BASE
