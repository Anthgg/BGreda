"""Rutas REST para el motor de recetas, versiones, calculos e importacion."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, status

from app.api.deps import (
    AdminUserDep,
    CurrentUserDep,
    DbSessionDep,
    RecipeImportServiceDep,
    RecipeServiceDep,
)
from app.schemas.recipes import (
    RecipeCalculateIn,
    RecipeCalculateOut,
    RecipeCreate,
    RecipeImportPreviewOut,
    RecipeOut,
    RecipePage,
    RecipeRowResolutionIn,
    RecipeUpdate,
    RecipeVersionIn,
    RecipeVersionOut,
)

router = APIRouter(tags=["recetas"])


# ---------------------------------------------------------------------------
# Recetas Cabecera
# ---------------------------------------------------------------------------
@router.get("/recipes", response_model=RecipePage)
async def list_recipes(
    service: RecipeServiceDep,
    _: CurrentUserDep,
    search: str | None = Query(None, description="Busqueda por nombre o codigo de producto"),
    product_id: int | None = Query(None, description="Filtrar por producto destino"),
    active: bool | None = Query(None, description="Filtrar por estado activo"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> RecipePage:
    """Lista las recetas registradas con paginacion y busqueda."""
    items, total = await service.list_recipes(
        search=search,
        product_id=product_id,
        active=active,
        limit=limit,
        offset=offset,
    )
    return RecipePage(items=items, total=total, limit=limit, offset=offset)


@router.post("/recipes", response_model=RecipeOut, status_code=status.HTTP_201_CREATED)
async def create_recipe(
    payload: RecipeCreate,
    service: RecipeServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> RecipeOut:
    """Crea una receta con su version 1 inicial."""
    recipe_out = await service.create_recipe(payload, user=admin)
    await session.commit()
    return recipe_out


@router.get("/recipes/{recipe_id}", response_model=RecipeOut)
async def get_recipe(
    recipe_id: int,
    service: RecipeServiceDep,
    _: CurrentUserDep,
) -> RecipeOut:
    """Obtiene el detalle completo de una receta y su version activa."""
    recipe = await service.get_recipe(recipe_id)
    return service._to_recipe_out(recipe)


@router.put("/recipes/{recipe_id}", response_model=RecipeOut)
async def update_recipe(
    recipe_id: int,
    payload: RecipeUpdate,
    service: RecipeServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> RecipeOut:
    """Actualiza metadatos de la receta."""
    recipe_out = await service.update_recipe(recipe_id, payload, user=admin)
    await session.commit()
    return recipe_out


# ---------------------------------------------------------------------------
# Versiones de Receta
# ---------------------------------------------------------------------------
@router.post(
    "/recipes/{recipe_id}/versions",
    response_model=RecipeVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_recipe_version(
    recipe_id: int,
    payload: RecipeVersionIn,
    service: RecipeServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
    activate: bool = Query(False, description="Activar inmediatamente la nueva version"),
) -> RecipeVersionOut:
    """Crea una nueva version inmutable para una receta existente."""
    version_out = await service.create_version(recipe_id, payload, user=admin, activate=activate)
    await session.commit()
    return version_out


@router.get("/recipe-versions/{version_id}", response_model=RecipeVersionOut)
async def get_recipe_version(
    version_id: int,
    service: RecipeServiceDep,
    _: CurrentUserDep,
) -> RecipeVersionOut:
    """Obtiene el detalle de una version historica o activa con sus lineas."""
    version = await service.get_version(version_id)
    return service._to_version_out(version)


@router.post("/recipe-versions/{version_id}/activate", response_model=RecipeVersionOut)
async def activate_recipe_version(
    version_id: int,
    service: RecipeServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> RecipeVersionOut:
    """Activa una version de receta archivando la version activa previa."""
    version_out = await service.activate_version(version_id, user=admin)
    await session.commit()
    return version_out


# ---------------------------------------------------------------------------
# Calculador / Simulador sin mutacion
# ---------------------------------------------------------------------------
@router.post("/recipes/calculate", response_model=RecipeCalculateOut)
async def calculate_recipe(
    payload: RecipeCalculateIn,
    service: RecipeServiceDep,
    _: CurrentUserDep,
) -> RecipeCalculateOut:
    """Calcula las cantidades de insumos, rendimiento real y costo total sin mutar inventario."""
    return await service.calculate(payload)


# ---------------------------------------------------------------------------
# Importacion desde Staging
# ---------------------------------------------------------------------------
@router.get("/recipe-imports/{batch_id}/preview", response_model=RecipeImportPreviewOut)
async def preview_recipe_import(
    batch_id: int,
    import_service: RecipeImportServiceDep,
    _: AdminUserDep,
) -> RecipeImportPreviewOut:
    """Genera una vista previa de las recetas contenidas en el lote de staging."""
    return await import_service.preview(batch_id)


@router.post("/recipe-imports/{batch_id}/resolve", response_model=RecipeImportPreviewOut)
async def resolve_recipe_import_rows(
    batch_id: int,
    resolutions: list[RecipeRowResolutionIn],
    import_service: RecipeImportServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> RecipeImportPreviewOut:
    """Aplica decisiones humanas sobre componentes o porcentajes de staging."""
    preview_out = await import_service.resolve(batch_id, resolutions, user=admin)
    await session.commit()
    return preview_out


@router.post("/recipe-imports/{batch_id}/commit")
async def commit_recipe_import(
    batch_id: int,
    import_service: RecipeImportServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> dict[str, Any]:
    """Confirma la importacion de staging creando las recetas productivas."""
    result = await import_service.commit(batch_id, user=admin)
    await session.commit()
    return result
