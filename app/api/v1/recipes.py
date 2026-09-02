"""Rutas REST para el motor de recetas, versiones, calculos e importacion."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select

from app.api.deps import (
    AdminUserDep,
    CurrentUserDep,
    DbSessionDep,
    PreparationServiceDep,
    RecipeImportServiceDep,
    RecipeServiceDep,
    WorkshopUserDep,
)
from app.core.preparations import PreparationError, grams_to_ml, ml_to_grams
from app.models.importing import ImportEntity, ImportRow
from app.models.recipes import RecipePreparation
from app.schemas.recipes import (
    GlazeAllocationOut,
    GlazeEstimateIn,
    GlazeEstimateOut,
    RecipeCalculateIn,
    RecipeCalculateOut,
    RecipeCreate,
    RecipeImportPreviewOut,
    RecipeOut,
    RecipePage,
    RecipePreparationIn,
    RecipePreparationLineOut,
    RecipePreparationOut,
    RecipePreparationPage,
    RecipeRowResolutionIn,
    RecipeUpdate,
    RecipeVersionIn,
    RecipeVersionOut,
    UnitConversionIn,
    UnitConversionOut,
)
from app.services.preparations import GlazeChoice, PreparationValidationError

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
@router.get("/recipe-imports/latest-batch")
async def get_latest_recipe_import_batch(
    session: DbSessionDep,
    _: AdminUserDep,
) -> dict[str, Any]:
    """Obtiene el batch_id mas reciente que contiene filas de recetas en staging."""
    stmt = (
        select(ImportRow.batch_id)
        .where(ImportRow.entity == ImportEntity.RECIPE)
        .order_by(ImportRow.batch_id.desc())
        .limit(1)
    )
    batch_id = (await session.execute(stmt)).scalar_one_or_none()
    return {"batch_id": batch_id}


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


# ---------------------------------------------------------------------------
# Preparaciones (Fase 009D)
# ---------------------------------------------------------------------------
def _preparation_out(row: RecipePreparation) -> RecipePreparationOut:
    return RecipePreparationOut(
        id=row.id,
        code=row.code,
        recipe_version_id=row.recipe_version_id,
        prepared_product_id=row.prepared_product_id,
        prepared_product_internal_reference=row.prepared_product.internal_reference,
        prepared_product_name=row.prepared_product.name,
        location_id=row.location_id,
        total_dry_weight_g=row.total_dry_weight_g,
        water_amount_ml=row.water_amount_ml,
        final_yield_ml=row.final_yield_ml,
        solids_g_per_ml=row.solids_g_per_ml,
        batch_total_cost=row.batch_total_cost,
        unit_cost_per_ml=row.unit_cost_per_ml,
        status=row.status.value,
        prepared_at=row.prepared_at,
        lines=[
            RecipePreparationLineOut(
                id=line.id,
                component_product_id=line.component_product_id,
                component_internal_reference=line.component_product.internal_reference,
                component_name=line.component_product.name,
                quantity_g=line.quantity_g,
                unit_cost_snapshot=line.unit_cost_snapshot,
                line_cost=line.line_cost,
            )
            for line in row.lines
        ],
    )


@router.get("/recipe-preparations", response_model=RecipePreparationPage)
async def list_recipe_preparations(
    service: PreparationServiceDep,
    _: CurrentUserDep,
    recipe_id: int | None = Query(None),
    prepared_product_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> RecipePreparationPage:
    items, total = await service.list_preparations(
        recipe_id=recipe_id,
        prepared_product_id=prepared_product_id,
        limit=limit,
        offset=offset,
    )
    return RecipePreparationPage(
        items=[_preparation_out(row) for row in items], total=total, limit=limit, offset=offset
    )


@router.get("/recipe-preparations/{preparation_id}", response_model=RecipePreparationOut)
async def get_recipe_preparation(
    preparation_id: int,
    service: PreparationServiceDep,
    _: CurrentUserDep,
) -> RecipePreparationOut:
    return _preparation_out(await service.get(preparation_id))


@router.post("/recipe-preparations", response_model=RecipePreparationOut)
async def create_recipe_preparation(
    payload: RecipePreparationIn,
    service: PreparationServiceDep,
    actor: WorkshopUserDep,
    session: DbSessionDep,
    response: Response,
) -> RecipePreparationOut:
    """Registra una preparacion fisica: consume materia prima y produce preparado.

    Devuelve 201 cuando la preparacion se ejecuta y 200 cuando la clave de
    idempotencia ya la habia ejecutado. Un reintento no repite el descuento, y
    el codigo distingue "lo acabo de hacer" de "ya estaba hecho" sin que el
    cliente tenga que adivinarlo.
    """
    preparation, created = await service.prepare(
        recipe_version_id=payload.recipe_version_id,
        location_id=payload.location_id,
        total_dry_weight_g=payload.total_dry_weight_g,
        water_amount_ml=payload.water_amount_ml,
        final_yield_ml=payload.final_yield_ml,
        idempotency_key=payload.idempotency_key,
        user=actor,
    )
    if created:
        await session.commit()
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return _preparation_out(await service.get(preparation.id))


@router.post("/recipe-preparations/convert", response_model=UnitConversionOut)
async def convert_units(
    payload: UnitConversionIn,
    service: PreparationServiceDep,
    _: CurrentUserDep,
) -> UnitConversionOut:
    """Convierte g <-> ml usando la concentracion de una preparacion.

    La autoridad es el backend: el frontend puede previsualizar, pero el numero
    que vale es este. No existe una densidad universal, asi que la conversion
    siempre se apoya en un lote concreto.
    """
    preparation = await service.get(payload.preparation_id)
    concentration = preparation.solids_g_per_ml
    try:
        if payload.from_unit == "g":
            converted = grams_to_ml(payload.value, concentration)
            to_unit = "ml"
        else:
            converted = ml_to_grams(payload.value, concentration)
            to_unit = "g"
    except PreparationError as error:
        raise PreparationValidationError(str(error)) from error
    return UnitConversionOut(
        preparation_id=preparation.id,
        solids_g_per_ml=concentration,
        value=payload.value,
        from_unit=payload.from_unit,
        converted=converted,
        to_unit=to_unit,
    )


@router.post("/recipe-preparations/glaze-estimate", response_model=GlazeEstimateOut)
async def estimate_glaze(
    payload: GlazeEstimateIn,
    service: PreparationServiceDep,
    _: CurrentUserDep,
) -> GlazeEstimateOut:
    """Estima el esmalte de una cotizacion y lo reparte entre los elegidos.

    Es una simulacion pura: no descuenta existencias ni crea movimientos.
    Cotizar no consume material.

    El porcentaje no viaja en la peticion: lo pone
    ``commercial_settings.estimated_glaze_percent``.
    """
    estimate = await service.estimate_glaze(
        piece_weight_g=payload.piece_weight_g,
        quantity=payload.quantity,
        unit=payload.unit,
        glazes=[
            GlazeChoice(
                share=glaze.share,
                preparation_id=glaze.preparation_id,
                prepared_product_id=glaze.prepared_product_id,
            )
            for glaze in payload.glazes
        ],
    )
    return GlazeEstimateOut(
        estimated_glaze_percent=estimate.estimated_glaze_percent,
        piece_weight_g=payload.piece_weight_g,
        quantity=payload.quantity,
        unit=payload.unit,
        grams_per_piece=estimate.grams_per_piece,
        total_estimated_grams=estimate.total_grams,
        allocations=[
            GlazeAllocationOut(
                preparation_id=(allocation.preparation.id if allocation.preparation else None),
                preparation_code=(allocation.preparation.code if allocation.preparation else None),
                prepared_product_id=allocation.prepared_product.id,
                prepared_product_internal_reference=(
                    allocation.prepared_product.internal_reference
                ),
                prepared_product_name=allocation.prepared_product.name,
                share=allocation.share,
                allocation_percent=allocation.allocation_percent,
                grams=allocation.grams,
                solids_g_per_ml=allocation.solids_g_per_ml,
                millilitres=allocation.millilitres,
                unit_cost_per_ml=allocation.unit_cost_per_ml,
                estimated_cost=allocation.cost,
            )
            for allocation in estimate.allocations
        ],
        total_estimated_cost=estimate.total_cost,
    )
