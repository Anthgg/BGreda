"""Superficie HTTP de los materiales del Cotizador V2.

Dos grupos de rutas, separados porque son dos cosas distintas:

- `/quoter-v2/materials` — la valorizacion de un material del maestro. Es
  politica de la casa: afecta a lo que se cotice DESPUES, nunca a lo ya
  cotizado. Solo administracion.
- `/quotations-v2/{id}/products` — las lineas de una cotizacion, con su
  material ya congelado.

Ninguna de estas operaciones mueve existencia. Cotizar consulta el stock y
puede avisar; el consumo pertenece a produccion.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.api.deps import AdminUserDep, DbSessionDep, V2MaterialServiceDep
from app.models.quoter_v2 import V2QuotationProduct
from app.models.quoter_v2_materials import V2MaterialCost, V2MaterialKind
from app.schemas.quoter_v2_materials import (
    V2MaterialOut,
    V2MaterialPage,
    V2MaterialUpsertIn,
    V2QuotationProductIn,
    V2QuotationProductOut,
    V2QuotationProductsPage,
)

router = APIRouter(tags=["cotizador-v2"])

ZERO = Decimal(0)


def _material_out(fila: V2MaterialCost, stock: Decimal) -> V2MaterialOut:
    return V2MaterialOut(
        product_id=fila.product_id,
        product_name=fila.product.name,
        product_type=str(fila.product.product_type),
        uom_code=fila.product.base_uom_code,
        active=fila.product.active,
        material_kind=fila.material_kind,
        origin=fila.origin,
        purchase_quantity=fila.purchase_quantity,
        purchase_cost=fila.purchase_cost,
        transport_cost=fila.transport_cost,
        acquisition_total_cost=fila.purchase_cost + fila.transport_cost,
        costing_override_per_unit=fila.costing_override_per_unit,
        effective_cost_per_unit=fila.effective_cost_per_unit,
        ml_per_gram=fila.ml_per_gram,
        notes=fila.notes,
        stock=stock,
    )


def _line_out(fila: V2QuotationProduct, warnings: list[str]) -> V2QuotationProductOut:
    return V2QuotationProductOut(
        id=fila.id,
        sort_order=fila.sort_order,
        product_id=fila.product_id,
        product_name=fila.product_name_snapshot,
        quantity=fila.quantity,
        body_material_id=fila.body_material_id,
        body_material_name=fila.body_material_name_snapshot,
        body_unit_weight=fila.body_unit_weight,
        body_uom=fila.body_uom_snapshot,
        body_cost_per_unit=fila.body_cost_per_unit_snapshot,
        body_cost_is_override=fila.body_cost_is_override,
        body_total_weight=fila.body_total_weight,
        body_cost=fila.body_cost,
        requires_glaze=fila.requires_glaze,
        glaze_material_id=fila.glaze_material_id,
        glaze_material_name=fila.glaze_material_name_snapshot,
        glaze_is_reference=fila.glaze_is_reference,
        glaze_cost_per_unit=fila.glaze_cost_per_unit_snapshot,
        glaze_cost_is_override=fila.glaze_cost_is_override,
        glaze_percent=fila.glaze_percent_snapshot,
        glaze_ml_per_gram=fila.glaze_ml_per_gram_snapshot,
        glaze_conversion_is_fallback=fila.glaze_conversion_is_fallback,
        glaze_total_weight=fila.glaze_total_weight,
        glaze_volume_ml=fila.glaze_volume_ml,
        glaze_cost=fila.glaze_cost,
        warnings=warnings,
    )


def _page(lineas: list[V2QuotationProduct]) -> V2QuotationProductsPage:
    return V2QuotationProductsPage(
        items=[_line_out(fila, []) for fila in lineas],
        materials_cost=sum((fila.body_cost + fila.glaze_cost for fila in lineas), ZERO),
    )


# ---------------------------------------------------------------------------
# Valorizacion de materiales
# ---------------------------------------------------------------------------
@router.get("/quoter-v2/materials", response_model=V2MaterialPage)
async def list_v2_materials(
    service: V2MaterialServiceDep,
    _: AdminUserDep,
    kind: Annotated[V2MaterialKind | None, Query()] = None,
) -> V2MaterialPage:
    filas = await service.list_materials(kind=kind)
    return V2MaterialPage(items=[_material_out(fila, stock) for fila, stock in filas])


@router.put("/quoter-v2/materials/{product_id}", response_model=V2MaterialOut)
async def upsert_v2_material(
    product_id: Annotated[int, Path(ge=1)],
    payload: V2MaterialUpsertIn,
    service: V2MaterialServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2MaterialOut:
    """Valoriza un material. Cambia lo que se cotice DESPUES, nunca lo ya emitido."""
    fila = await service.upsert_material(product_id, payload.model_dump(), user=admin)
    stock = await service.stock_for(product_id)
    resultado = _material_out(fila, stock)
    await session.commit()
    return resultado


# ---------------------------------------------------------------------------
# Lineas de una cotizacion
# ---------------------------------------------------------------------------
@router.get("/quotations-v2/{quotation_id}/products", response_model=V2QuotationProductsPage)
async def list_v2_quotation_products(
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2MaterialServiceDep,
    _: AdminUserDep,
) -> V2QuotationProductsPage:
    return _page(await service.list_lines(quotation_id))


@router.post(
    "/quotations-v2/{quotation_id}/products",
    response_model=V2QuotationProductOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_v2_quotation_product(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2QuotationProductIn,
    service: V2MaterialServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2QuotationProductOut:
    fila, avisos = await service.add_line(
        quotation_id, payload.model_dump(exclude_unset=True), user=admin
    )
    resultado = _line_out(fila, avisos)
    await session.commit()
    return resultado


@router.put(
    "/quotations-v2/{quotation_id}/products/{line_id}",
    response_model=V2QuotationProductOut,
)
async def update_v2_quotation_product(
    quotation_id: Annotated[int, Path(ge=1)],
    line_id: Annotated[int, Path(ge=1)],
    payload: V2QuotationProductIn,
    service: V2MaterialServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2QuotationProductOut:
    fila, avisos = await service.update_line(
        quotation_id, line_id, payload.model_dump(exclude_unset=True), user=admin
    )
    resultado = _line_out(fila, avisos)
    await session.commit()
    return resultado


@router.delete(
    "/quotations-v2/{quotation_id}/products/{line_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_v2_quotation_product(
    quotation_id: Annotated[int, Path(ge=1)],
    line_id: Annotated[int, Path(ge=1)],
    service: V2MaterialServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> None:
    await service.delete_line(quotation_id, line_id, user=admin)
    await session.commit()
