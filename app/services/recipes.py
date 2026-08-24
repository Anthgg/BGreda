"""Servicio de negocio para gestion de recetas, versiones y calculos."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import APIError
from app.core.recipes import (
    MAX_RECIPE_RECURSION_DEPTH,
    RecipeComponentProductError,
    RecipeCycleError,
    RecipeError,
    RecipeRecursionLimitError,
    RecipeTargetProductError,
    compute_recipe_fingerprint,
    normalize_component_unit_cost_to_grams,
    validate_recipe_percentages,
)
from app.models.audit import AuditAction
from app.models.masters import Product, ProductType
from app.models.recipes import (
    Recipe,
    RecipeComponentType,
    RecipeLine,
    RecipeStatus,
    RecipeVersion,
)
from app.schemas.auth import AuthenticatedUser
from app.schemas.recipes import (
    CalculatedComponentLineOut,
    RecipeCalculateIn,
    RecipeCalculateOut,
    RecipeCreate,
    RecipeLineOut,
    RecipeOut,
    RecipeUpdate,
    RecipeVersionIn,
    RecipeVersionOut,
)
from app.services.audit import AuditRecorder

MAX_PAGE_SIZE = 200


class RecipeNotFoundError(APIError):
    status_code = 404
    code = "RECIPE_NOT_FOUND"
    message = "La receta no existe"


class RecipeVersionNotFoundError(APIError):
    status_code = 404
    code = "RECIPE_VERSION_NOT_FOUND"
    message = "La version de receta no existe"


def _limit(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE_SIZE))


class RecipeService:
    """Servicio de dominio para el motor de recetas y costeo."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder | None = None) -> None:
        self._session = session
        self._audit = audit or AuditRecorder(session)

    # -----------------------------------------------------------------------
    # Consultas
    # -----------------------------------------------------------------------
    async def list_recipes(
        self,
        *,
        search: str | None = None,
        product_id: int | None = None,
        active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RecipeOut], int]:
        stmt = (
            select(Recipe)
            .join(Product, Recipe.product_id == Product.id)
            .options(
                selectinload(Recipe.current_version)
                .selectinload(RecipeVersion.lines)
                .selectinload(RecipeLine.component_product),
                selectinload(Recipe.versions)
                .selectinload(RecipeVersion.lines)
                .selectinload(RecipeLine.component_product),
            )
        )

        if active is not None:
            stmt = stmt.where(Recipe.active.is_(active))
        if product_id is not None:
            stmt = stmt.where(Recipe.product_id == product_id)
        if search:
            term = f"%{search.strip()}%"
            stmt = stmt.where(
                (Recipe.name.ilike(term))
                | (Product.name.ilike(term))
                | (Product.internal_reference.ilike(term))
            )

        total = await self._session.scalar(select(func.count()).select_from(stmt.subquery()))
        stmt = (
            stmt.order_by(Product.internal_reference.asc())
            .limit(_limit(limit))
            .offset(max(0, offset))
        )
        recipes = list((await self._session.execute(stmt)).scalars().all())

        return [self._to_recipe_out(r) for r in recipes], int(total or 0)

    async def get_recipe(self, recipe_id: int) -> Recipe:
        stmt = (
            select(Recipe)
            .where(Recipe.id == recipe_id)
            .options(
                selectinload(Recipe.current_version)
                .selectinload(RecipeVersion.lines)
                .selectinload(RecipeLine.component_product),
                selectinload(Recipe.versions)
                .selectinload(RecipeVersion.lines)
                .selectinload(RecipeLine.component_product),
            )
        )
        recipe = (await self._session.execute(stmt)).scalar_one_or_none()
        if recipe is None:
            raise RecipeNotFoundError()
        return recipe

    async def get_recipe_by_product_id(self, product_id: int) -> Recipe | None:
        stmt = (
            select(Recipe)
            .where(Recipe.product_id == product_id)
            .options(
                selectinload(Recipe.current_version).selectinload(RecipeVersion.lines),
                selectinload(Recipe.versions).selectinload(RecipeVersion.lines),
            )
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_version(self, version_id: int) -> RecipeVersion:
        stmt = (
            select(RecipeVersion)
            .where(RecipeVersion.id == version_id)
            .options(
                selectinload(RecipeVersion.lines).selectinload(RecipeLine.component_product),
                selectinload(RecipeVersion.recipe),
            )
        )
        version = (await self._session.execute(stmt)).scalar_one_or_none()
        if version is None:
            raise RecipeVersionNotFoundError()
        return version

    # -----------------------------------------------------------------------
    # Mutaciones
    # -----------------------------------------------------------------------
    async def create_recipe(
        self,
        payload: RecipeCreate,
        user: AuthenticatedUser,
    ) -> RecipeOut:
        # 1. Validar producto destino
        product = await self._session.get(Product, payload.product_id)
        if product is None:
            raise RecipeError("El producto seleccionado no existe")
        # Una receta describe un material preparado —pasta, barniz, esmalte—, no
        # una pieza. Que una cotizacion de pieza terminada pueda **elegir** una
        # receta no significa que la pieza tenga formula propia: esa
        # independencia se resuelve en el cotizador, no relajando el maestro.
        if product.product_type is not ProductType.PREPARED_MATERIAL:
            msg = (
                f"El producto '{product.name}' es de tipo {product.product_type.value}, "
                "solo PREPARED_MATERIAL admite recetas"
            )
            raise RecipeTargetProductError(msg)

        existing = await self.get_recipe_by_product_id(payload.product_id)
        if existing is not None:
            raise RecipeError(
                f"El producto '{product.name}' ya tiene una receta asignada (ID {existing.id})"
            )

        # 2. Validar componentes
        component_ids = [line.component_product_id for line in payload.lines]
        if payload.product_id in component_ids:
            raise RecipeCycleError("Una receta no puede tenerse a si misma como ingrediente")

        await self._validate_components(component_ids)
        await self._check_cycles(payload.product_id, component_ids)

        # 3. Validar porcentajes y proporciones
        percentages_tuples = [(line.component_type, line.percentage) for line in payload.lines]
        base_total, add_total, yield_factor = validate_recipe_percentages(percentages_tuples)

        # 4. Fingerprint
        fingerprint_items = [
            (item.component_product_id, item.component_type, item.percentage, item.sort_order)
            for item in payload.lines
        ]
        fingerprint = compute_recipe_fingerprint(fingerprint_items)

        # 5. Crear Receta
        recipe = Recipe(
            product_id=payload.product_id,
            name=payload.name,
            active=payload.active,
        )
        self._session.add(recipe)
        await self._session.flush()

        # 6. Crear Version 1
        version_status = RecipeStatus.ACTIVE if payload.activate_immediately else RecipeStatus.DRAFT
        version = RecipeVersion(
            recipe_id=recipe.id,
            version_number=1,
            status=version_status,
            yield_factor=yield_factor,
            base_total=base_total,
            additional_total=add_total,
            fingerprint=fingerprint,
            notes=payload.notes,
            created_by_id=user.id,
        )
        self._session.add(version)
        await self._session.flush()

        # 7. Crear Lineas
        for idx, line_in in enumerate(payload.lines):
            line = RecipeLine(
                recipe_version_id=version.id,
                component_product_id=line_in.component_product_id,
                component_type=line_in.component_type,
                percentage=line_in.percentage,
                sort_order=line_in.sort_order if line_in.sort_order else idx,
            )
            self._session.add(line)

        if payload.activate_immediately:
            recipe.current_version_id = version.id

        await self._session.flush()
        self._record(
            recipe.id,
            "recipe",
            AuditAction.CREATE,
            user,
            f"{product.internal_reference}: {recipe.name} (V1 {version_status.value})",
        )

        full_recipe = await self.get_recipe(recipe.id)
        return self._to_recipe_out(full_recipe)

    async def create_version(
        self,
        recipe_id: int,
        payload: RecipeVersionIn,
        user: AuthenticatedUser,
        *,
        activate: bool = False,
    ) -> RecipeVersionOut:
        recipe = await self.get_recipe(recipe_id)

        component_ids = [line.component_product_id for line in payload.lines]
        if recipe.product_id in component_ids:
            raise RecipeCycleError("Una receta no puede tenerse a si misma como ingrediente")

        await self._validate_components(component_ids)
        await self._check_cycles(recipe.product_id, component_ids)

        percentages_tuples = [(line.component_type, line.percentage) for line in payload.lines]
        base_total, add_total, yield_factor = validate_recipe_percentages(percentages_tuples)

        fingerprint_items = [
            (item.component_product_id, item.component_type, item.percentage, item.sort_order)
            for item in payload.lines
        ]
        fingerprint = compute_recipe_fingerprint(fingerprint_items)

        max_version = max((v.version_number for v in recipe.versions), default=0)
        next_version_number = max_version + 1

        if activate:
            # Archivar version activa previa
            stmt_active = select(RecipeVersion).where(
                RecipeVersion.recipe_id == recipe.id,
                RecipeVersion.status == RecipeStatus.ACTIVE,
            )
            for prev_active in (await self._session.execute(stmt_active)).scalars().all():
                prev_active.status = RecipeStatus.ARCHIVED
            await self._session.flush()

        new_status = RecipeStatus.ACTIVE if activate else RecipeStatus.DRAFT
        version = RecipeVersion(
            recipe_id=recipe.id,
            version_number=next_version_number,
            status=new_status,
            yield_factor=yield_factor,
            base_total=base_total,
            additional_total=add_total,
            fingerprint=fingerprint,
            notes=payload.notes,
            created_by_id=user.id,
        )
        self._session.add(version)
        await self._session.flush()

        for idx, line_in in enumerate(payload.lines):
            line = RecipeLine(
                recipe_version_id=version.id,
                component_product_id=line_in.component_product_id,
                component_type=line_in.component_type,
                percentage=line_in.percentage,
                sort_order=idx + 1,
            )
            self._session.add(line)

        if activate:
            recipe.current_version_id = version.id

        await self._session.flush()
        self._record(
            recipe.id,
            "recipe_version",
            AuditAction.CREATE,
            user,
            f"Creada V{version.version_number} para receta {recipe.name}",
        )

        full_version = await self.get_version(version.id)
        return self._to_version_out(full_version)

    async def activate_version(
        self,
        version_id: int,
        user: AuthenticatedUser,
    ) -> RecipeVersionOut:
        version = await self.get_version(version_id)
        recipe = await self.get_recipe(version.recipe_id)

        if version.status == RecipeStatus.ACTIVE:
            return self._to_version_out(version)

        # Archivar activa actual
        stmt_active = select(RecipeVersion).where(
            RecipeVersion.recipe_id == version.recipe_id,
            RecipeVersion.status == RecipeStatus.ACTIVE,
            RecipeVersion.id != version.id,
        )
        for prev_active in (await self._session.execute(stmt_active)).scalars().all():
            prev_active.status = RecipeStatus.ARCHIVED
        await self._session.flush()

        version.status = RecipeStatus.ACTIVE
        recipe.current_version_id = version.id
        await self._session.flush()

        self._record(
            recipe.id,
            "recipe_version",
            AuditAction.UPDATE,
            user,
            f"Activada V{version.version_number} para receta {recipe.name}",
        )

        full_version = await self.get_version(version.id)
        return self._to_version_out(full_version)

    async def update_recipe(
        self,
        recipe_id: int,
        payload: RecipeUpdate,
        user: AuthenticatedUser,
    ) -> RecipeOut:
        recipe = await self.get_recipe(recipe_id)
        if payload.name is not None:
            recipe.name = payload.name
        if payload.active is not None:
            recipe.active = payload.active

        await self._session.flush()
        self._record(
            recipe.id,
            "recipe",
            AuditAction.UPDATE,
            user,
            f"Actualizada receta {recipe.name}",
        )
        full_recipe = await self.get_recipe(recipe.id)
        return self._to_recipe_out(full_recipe)

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

    # -----------------------------------------------------------------------
    # Calculador / Simulador sin mutacion
    # -----------------------------------------------------------------------
    async def calculate(self, payload: RecipeCalculateIn) -> RecipeCalculateOut:
        """Calcula el desglose de insumos, cantidades reales y costos."""
        lines_to_calculate: list[tuple[Product, RecipeComponentType, Decimal]] = []

        if payload.recipe_version_id is not None:
            version = await self.get_version(payload.recipe_version_id)
            for line in version.lines:
                lines_to_calculate.append(
                    (line.component_product, line.component_type, line.percentage)
                )
        elif payload.recipe_id is not None:
            recipe = await self.get_recipe(payload.recipe_id)
            if recipe.current_version is None:
                raise RecipeError("La receta no tiene una version activa para calcular")
            for line in recipe.current_version.lines:
                lines_to_calculate.append(
                    (line.component_product, line.component_type, line.percentage)
                )
        elif payload.lines:
            component_ids = [item.component_product_id for item in payload.lines]
            products_map = {
                p.id: p
                for p in (
                    await self._session.execute(
                        select(Product).where(Product.id.in_(component_ids))
                    )
                )
                .scalars()
                .all()
            }
            for item in payload.lines:
                prod = products_map.get(item.component_product_id)
                if prod is None:
                    raise RecipeComponentProductError(
                        f"Componente ID {item.component_product_id} no encontrado"
                    )
                lines_to_calculate.append((prod, item.component_type, item.percentage))
        else:
            raise RecipeError("Debe especificar recipe_version_id, recipe_id o una lista de lineas")

        # Validar porcentajes
        percentage_pairs = [(item[1], item[2]) for item in lines_to_calculate]
        _, _, yield_factor = validate_recipe_percentages(percentage_pairs)

        target_base = payload.target_base_quantity
        real_output_qty = target_base * yield_factor

        base_cost = Decimal(0)
        colorant_cost = Decimal(0)
        additive_cost = Decimal(0)
        total_material_cost = Decimal(0)

        calc_components: list[CalculatedComponentLineOut] = []

        for prod, comp_type, pct in lines_to_calculate:
            # required_quantity = target_base * (pct / 100)
            req_qty = (target_base * pct) / Decimal(100)

            # Obtener costo unitario efectivo por gramo
            cost_per_gram = await self._resolve_component_cost_per_gram(
                prod, depth=0, visited=set()
            )
            line_cost = req_qty * cost_per_gram

            if comp_type == RecipeComponentType.BASE:
                base_cost += line_cost
            elif comp_type == RecipeComponentType.COLORANT:
                colorant_cost += line_cost
            elif comp_type == RecipeComponentType.ADDITIVE:
                additive_cost += line_cost

            total_material_cost += line_cost

            calc_components.append(
                CalculatedComponentLineOut(
                    component_product_id=prod.id,
                    component_internal_reference=prod.internal_reference,
                    component_name=prod.name,
                    component_type=comp_type,
                    percentage=pct,
                    required_quantity=req_qty,
                    uom="g",
                    unit_cost_in_grams=cost_per_gram,
                    component_cost=line_cost,
                )
            )

        cost_per_real_unit = (
            (total_material_cost / real_output_qty) if real_output_qty > Decimal(0) else Decimal(0)
        )

        return RecipeCalculateOut(
            target_base_quantity=target_base,
            target_uom=payload.target_uom,
            yield_factor=yield_factor,
            real_output_quantity=real_output_qty,
            base_cost=base_cost,
            colorant_cost=colorant_cost,
            additive_cost=additive_cost,
            total_material_cost=total_material_cost,
            cost_per_real_unit=cost_per_real_unit,
            components=calc_components,
        )

    # -----------------------------------------------------------------------
    # Validaciones internas y resolucion de costos recursivos
    # -----------------------------------------------------------------------
    async def _validate_components(self, component_ids: list[int]) -> None:
        if not component_ids:
            raise RecipeError("La receta debe incluir componentes")

        stmt = select(Product).where(Product.id.in_(component_ids))
        products = list((await self._session.execute(stmt)).scalars().all())
        found_ids = {p.id for p in products}

        missing = set(component_ids) - found_ids
        if missing:
            raise RecipeComponentProductError(f"Componentes no encontrados: {missing}")

        for p in products:
            if not p.active:
                raise RecipeComponentProductError(
                    f"El producto '{p.name}' ({p.internal_reference}) esta inactivo"
                )
            if p.product_type == ProductType.SERVICE:
                raise RecipeComponentProductError(
                    f"Un servicio no puede ser componente de receta: {p.name}"
                )

    async def _check_cycles(self, target_product_id: int, component_ids: list[int]) -> None:
        """Verifica que no existan ciclos recursivos en recetas anidadas."""
        visited = {target_product_id}
        stack = list(component_ids)

        depth = 0
        while stack:
            depth += 1
            if depth > 100:
                raise RecipeRecursionLimitError("Demasiados niveles de anidamiento en la receta")

            current_id = stack.pop()
            if current_id in visited:
                raise RecipeCycleError(f"Ciclo detectado con el producto ID {current_id}")

            # Buscar si el componente tiene una receta
            recipe = await self.get_recipe_by_product_id(current_id)
            if recipe and recipe.current_version:
                for line in recipe.current_version.lines:
                    if line.component_product_id == target_product_id:
                        msg = (
                            f"Ciclo detectado: '{recipe.name}' referencia de vuelta al "
                            "producto inicial"
                        )
                        raise RecipeCycleError(msg)
                    stack.append(line.component_product_id)

    async def _resolve_component_cost_per_gram(
        self,
        product: Product,
        depth: int,
        visited: set[int],
    ) -> Decimal:
        """Calcula el costo por gramo de un componente, recursivo si es preparado."""
        if depth > MAX_RECIPE_RECURSION_DEPTH:
            raise RecipeRecursionLimitError("Limite de profundidad alcanzado en costeo recursivo")

        if product.id in visited:
            raise RecipeCycleError(f"Ciclo de costeo detectado en producto ID {product.id}")

        # Si el producto tiene un costo explicito en el maestro, se usa como base
        if product.cost is not None and product.cost > Decimal(0):
            return normalize_component_unit_cost_to_grams(product.cost, product.base_uom_code)

        # Si es un PREPARED_MATERIAL y no tiene costo directo, buscar su receta activa
        if product.product_type == ProductType.PREPARED_MATERIAL:
            recipe = await self.get_recipe_by_product_id(product.id)
            if recipe and recipe.current_version and recipe.current_version.lines:
                visited.add(product.id)
                total_recipe_cost = Decimal(0)
                base_qty = Decimal("1000.000000")
                version = recipe.current_version

                for line in version.lines:
                    comp_qty = (base_qty * line.percentage) / Decimal(100)
                    c_cost_g = await self._resolve_component_cost_per_gram(
                        line.component_product, depth + 1, visited
                    )
                    total_recipe_cost += comp_qty * c_cost_g

                real_output = base_qty * version.yield_factor
                visited.remove(product.id)
                if real_output > Decimal(0):
                    return total_recipe_cost / real_output

        return normalize_component_unit_cost_to_grams(product.cost, product.base_uom_code)

    def _to_recipe_out(self, recipe: Recipe) -> RecipeOut:
        current_version_out = (
            self._to_version_out(recipe.current_version) if recipe.current_version else None
        )
        versions_out = (
            [
                self._to_version_out(v)
                for v in sorted(recipe.versions, key=lambda x: x.version_number, reverse=True)
            ]
            if recipe.versions
            else []
        )
        return RecipeOut(
            id=recipe.id,
            product_id=recipe.product_id,
            product_internal_reference=recipe.product.internal_reference if recipe.product else "",
            product_name=recipe.product.name if recipe.product else "",
            product_category_path=None,
            name=recipe.name,
            active=recipe.active,
            current_version_id=recipe.current_version_id,
            current_version=current_version_out,
            versions=versions_out,
            versions_count=len(recipe.versions) if recipe.versions else 0,
            created_at=recipe.created_at,
            updated_at=recipe.updated_at,
        )

    def _to_version_out(self, version: RecipeVersion) -> RecipeVersionOut:
        lines_out = [
            RecipeLineOut(
                id=line.id,
                recipe_version_id=line.recipe_version_id,
                component_product_id=line.component_product_id,
                component_internal_reference=line.component_product.internal_reference
                if line.component_product
                else "",
                component_name=line.component_product.name if line.component_product else "",
                component_type=line.component_type,
                percentage=line.percentage,
                sort_order=line.sort_order,
                component_cost=line.component_product.cost if line.component_product else None,
                component_uom=line.component_product.base_uom_code
                if line.component_product
                else None,
            )
            for line in version.lines
        ]
        return RecipeVersionOut(
            id=version.id,
            recipe_id=version.recipe_id,
            version_number=version.version_number,
            status=version.status,
            yield_factor=version.yield_factor,
            base_total=version.base_total,
            additional_total=version.additional_total,
            fingerprint=version.fingerprint,
            notes=version.notes,
            created_by_id=version.created_by_id,
            created_at=version.created_at,
            updated_at=version.updated_at,
            lines=lines_out,
        )
