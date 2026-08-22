"""Servicio de importacion y conversion de recetas desde staging a produccion.

No vuelve a leer el archivo Excel: opera exclusivamente sobre las filas de
staging preservadas en `import_rows` con entity='RECIPE'.

Clasificacion estructural obligatoria:
- La BASE de una receta esta formada por las lineas iniciales que en orden
  acumulan exactamente 100 % (dentro de PERCENTAGE_TOLERANCE).
- Las lineas posteriores a ese boundary son ADDITIONAL (COLORANT o ADDITIVE).
- Precedencia: HUMAN_RESOLUTION > SOURCE_STRUCTURE_BASE > SUGGESTION > REVIEW_REQUIRED.
- Las keywords NUNCA sobreescriben componentes que pertenecen a la BASE estructural.
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


COLORANT_KEYWORDS = (
    "ÓXIDO",
    "OXIDO",
    "CARBONATO DE COBRE",
    "CARBONATO DE COBALTO",
    "CARBONATO DE MANGANESO",
    "CARBONATO DE NIQUEL",
    "OCRE",
    "PIGMENTO",
    "ESMALTE",
    "COBRE",
    "COBALTO",
    "HIERRO",
    "MANGANESO",
    "NIQUEL",
    "CROMO",
    "RUTILO",
    "ESTAÑO",
    "ESTANO",
    "ZINC",
)
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
        """Analiza las filas de staging aplicando clasificacion estructural por boundary 100%."""
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

            # Paso 1: Ordenar filas por source_row ASC
            sorted_rows = sorted(group_rows, key=lambda item: item.source_row)

            # Paso 2: Detectar el boundary estructural de base 100%
            boundary_index: int | None = None
            cumulative_calc = Decimal(0)
            row_quantities: list[tuple[ImportRow, str, Product | None, Decimal, Decimal]] = []

            for idx, r in enumerate(sorted_rows):
                comp_name = r.raw.get("component") or ""
                comp_prod = products_by_fold.get(fold(comp_name))
                qty_raw = Decimal(str(r.raw.get("component_quantity") or 0))
                source_pct = qty_raw * Decimal(100)
                cumulative_calc += source_pct

                if (
                    boundary_index is None
                    and abs(cumulative_calc - BASE_PERCENTAGE_TARGET) <= PERCENTAGE_TOLERANCE
                ):
                    boundary_index = idx

                row_quantities.append((r, comp_name, comp_prod, qty_raw, cumulative_calc))

            has_structural_boundary = boundary_index is not None
            if not has_structural_boundary:
                warnings.append(
                    "BASE_BOUNDARY_NOT_FOUND: "
                    "La fórmula no alcanza exactamente 100% base en el orden del maestro"
                )

            staging_lines: list[RecipeStagingLineOut] = []
            parsed_tuples: list[tuple[RecipeComponentType, Decimal]] = []
            has_unresolved_lines = False

            # Paso 3: Clasificacion segun precedencia
            for idx, (r, comp_name, comp_prod, qty_raw, cum_pct) in enumerate(row_quantities):
                line_errors: list[str] = []
                line_warnings: list[str] = []

                if comp_prod is None:
                    line_errors.append(
                        f"Componente '{comp_name}' no existe en catalogo de productos"
                    )
                    errors.append(f"Componente '{comp_name}' no existe en catalogo de productos")

                resolution = r.resolution or {}
                res_action = resolution.get("action", "RESOLVE")
                source_percentage = qty_raw * Decimal(100)
                suggested_t = self._suggest_additional_type(comp_name)

                # Regla de boundary: ¿Pertenece al bloque base estructural inicial?
                is_in_structural_base = has_structural_boundary and idx <= boundary_index  # type: ignore[operator]

                if res_action == "SKIP":
                    # Precedencia 1: Human Resolution (SKIP)
                    action = "SKIP"
                    status = "RESOLVED"
                    requires_review = False
                    comp_type = None
                    classification_role = "BASE" if is_in_structural_base else "ADDITIONAL"
                    classification_source = "HUMAN_RESOLUTION"
                    resolution_source = "HUMAN"
                    final_percentage = (
                        Decimal(str(resolution["percentage"]))
                        if resolution.get("percentage") is not None
                        else source_percentage
                    )
                elif resolution.get("component_type"):
                    # Precedencia 1: Human Resolution (Component Type Explicito)
                    action = "CREATE"
                    comp_type = RecipeComponentType(resolution["component_type"])
                    status = "RESOLVED"
                    requires_review = False
                    classification_role = (
                        "BASE" if comp_type == RecipeComponentType.BASE else "ADDITIONAL"
                    )
                    classification_source = "HUMAN_RESOLUTION"
                    resolution_source = "HUMAN"
                    final_percentage = (
                        Decimal(str(resolution["percentage"]))
                        if resolution.get("percentage") is not None
                        else source_percentage
                    )
                elif is_in_structural_base:
                    # Precedencia 2: Source Structure BASE (100% exacto acumulado)
                    action = "CREATE"
                    comp_type = RecipeComponentType.BASE
                    status = "READY"
                    requires_review = False
                    classification_role = "BASE"
                    classification_source = "SOURCE_STRUCTURE"
                    resolution_source = "SOURCE"
                    final_percentage = (
                        Decimal(str(resolution["percentage"]))
                        if resolution.get("percentage") is not None
                        else source_percentage
                    )
                else:
                    # Precedencia 4 / 5: Linea adicional posterior sin resolucion -> REVIEW_REQUIRED
                    action = "CREATE"
                    comp_type = None
                    status = "REVIEW_REQUIRED"
                    requires_review = True
                    has_unresolved_lines = True
                    classification_role = "ADDITIONAL" if has_structural_boundary else "UNKNOWN"
                    classification_source = "UNRESOLVED"
                    resolution_source = "UNRESOLVED"
                    final_percentage = (
                        Decimal(str(resolution["percentage"]))
                        if resolution.get("percentage") is not None
                        else source_percentage
                    )

                percentage = final_percentage

                if action != "SKIP" and comp_type is not None:
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
                        suggested_component_type=suggested_t,
                        classification_role=classification_role,
                        classification_source=classification_source,
                        cumulative_percentage=cum_pct,
                        source_percentage=source_percentage,
                        final_percentage=final_percentage,
                        percentage=percentage,
                        resolution_source=resolution_source,
                        status=status,
                        action=action,
                        requires_review=requires_review,
                        quantity_raw=qty_raw,
                        uom_raw=r.raw.get("component_uom"),
                        warnings=line_warnings,
                        errors=line_errors,
                    )
                )

            # Paso 4: Validar proporciones y rendimiento
            base_total = Decimal(0)
            add_total = Decimal(0)
            yield_factor = Decimal(1)

            if has_unresolved_lines:
                warnings.append(
                    "La receta contiene componentes adicionales pendientes de clasificación"
                )
                is_valid = False
            elif not parsed_tuples:
                if all(line.action == "SKIP" for line in staging_lines):
                    warnings.append("Todas las lineas de la receta estan marcadas como SKIP")
                    is_valid = True
                else:
                    warnings.append("No hay componentes validos para calcular proporciones")
                    is_valid = False
            else:
                try:
                    base_total, add_total, yield_factor = validate_recipe_percentages(parsed_tuples)
                    is_valid = (
                        len(errors) == 0
                        and abs(base_total - BASE_PERCENTAGE_TARGET) <= PERCENTAGE_TOLERANCE
                    )
                except Exception as exc:
                    warnings.append(str(exc))
                    is_valid = False

            if not has_structural_boundary and not (is_valid and not has_unresolved_lines):
                warnings.append(
                    "BASE_BOUNDARY_NOT_FOUND: La fórmula no alcanza exactamente 100% "
                    "base en el orden del maestro"
                )

            # Paso 5: Costo estimado por unidad real de salida
            estimated_cost = Decimal(0)
            if target_prod and not errors and parsed_tuples:
                for line_out in staging_lines:
                    if line_out.action != "SKIP" and line_out.component_product_id:
                        comp_p = next(
                            (p for p in all_products if p.id == line_out.component_product_id), None
                        )
                        if comp_p and comp_p.cost:
                            c_cost_g = normalize_component_unit_cost_to_grams(
                                comp_p.cost, comp_p.base_uom_code
                            )
                            estimated_cost += (line_out.final_percentage / Decimal(100)) * c_cost_g
                if yield_factor > Decimal(0):
                    estimated_cost = estimated_cost / yield_factor

            if errors:
                group_status = "ERROR"
                error_count += 1
            elif not is_valid or has_unresolved_lines:
                group_status = "REVIEW_REQUIRED"
                review_count += 1
            else:
                group_status = "READY"
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
                    has_structural_base_boundary=has_structural_boundary,
                    status=group_status,
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
        """Convierte atomicamente las recetas de staging en recetas productivas.

        Bloquea el commit si existen errores O decisiones pendientes de revision.
        """
        preview_data = await self.preview(batch_id)
        if preview_data.error_count > 0 or preview_data.review_required_count > 0:
            msg = (
                f"RECIPE_IMPORT_ROWS_PENDING: El lote contiene {preview_data.error_count} errores "
                f"y {preview_data.review_required_count} recetas pendientes de revision: "
                "resuelve todas las lineas antes de confirmar"
            )
            raise RecipeImportError(msg)

        created_count = 0
        updated_count = 0
        skipped_count = 0

        for group in preview_data.recipes:
            # Construir lineas de entrada excluyendo SKIP
            active_lines = [line for line in group.lines if line.action != "SKIP"]
            if not active_lines:
                skipped_count += 1
                continue

            lines_in: list[RecipeLineIn] = [
                RecipeLineIn(
                    component_product_id=line.component_product_id,  # type: ignore[arg-type]
                    component_type=line.component_type,  # type: ignore[arg-type]
                    percentage=line.final_percentage,
                    sort_order=idx,
                )
                for idx, line in enumerate(active_lines)
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

    def _suggest_additional_type(
        self,
        comp_name: str,
    ) -> RecipeComponentType:
        """Sugiere COLORANT o ADDITIVE para lineas que estan fuera del boundary BASE."""
        upper = fold(comp_name)
        if any(k in upper for k in ADDITIVE_KEYWORDS):
            return RecipeComponentType.ADDITIVE

        return RecipeComponentType.COLORANT
