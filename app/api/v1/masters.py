"""Endpoints de los maestros: categorias, unidades, productos y terceros.

Reparto de permisos, siempre impuesto por el backend:

- **Lectura**: cualquier usuario autenticado.
- **Escritura**: exclusivamente ADMIN.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import AdminUserDep, CurrentUserDep, DbSessionDep, MasterDataServiceDep
from app.models.masters import (
    Partner,
    PartnerRole,
    PosCategory,
    Product,
    ProductCategory,
    ProductType,
    UnitOfMeasure,
)
from app.schemas.common import ErrorResponse
from app.schemas.masters import (
    PartnerCreate,
    PartnerOut,
    PartnerPage,
    PartnerUpdate,
    PosCategoryCreate,
    PosCategoryOut,
    ProductCategoryCreate,
    ProductCategoryOut,
    ProductCategoryUpdate,
    ProductCreate,
    ProductOut,
    ProductPage,
    ProductUpdate,
    UnitOfMeasureCreate,
    UnitOfMeasureOut,
)

router = APIRouter(tags=["masters"])

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Sin sesion valida"},
    403: {"model": ErrorResponse, "description": "Requiere rol ADMIN"},
    404: {"model": ErrorResponse, "description": "El registro no existe"},
    409: {"model": ErrorResponse, "description": "Clave duplicada"},
    422: {"model": ErrorResponse, "description": "Datos invalidos"},
}

LimitDep = Annotated[int, Query(ge=1, le=200)]
OffsetDep = Annotated[int, Query(ge=0)]


def _product_out(
    product: Product,
    *,
    category: ProductCategory | None = None,
    pos_category: PosCategory | None = None,
) -> ProductOut:
    out = ProductOut.model_validate(product)
    out.product_category_path = category.display_path if category else None
    out.pos_category_name = pos_category.name if pos_category else None
    return out


# ---------------------------------------------------------------------------
# Categorias
# ---------------------------------------------------------------------------
@router.get("/categories", response_model=list[ProductCategoryOut], responses=_ERRORS)
async def list_categories(
    _: CurrentUserDep,
    service: MasterDataServiceDep,
    only_active: bool = False,
) -> list[ProductCategoryOut]:
    categories = await service.list_product_categories(only_active=only_active)
    return [ProductCategoryOut.model_validate(item) for item in categories]


@router.post(
    "/categories",
    response_model=ProductCategoryOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def create_category(
    payload: ProductCategoryCreate,
    user: AdminUserDep,
    service: MasterDataServiceDep,
    session: DbSessionDep,
) -> ProductCategoryOut:
    category = await service.create_product_category(payload, user)
    await session.commit()
    await session.refresh(category)
    return ProductCategoryOut.model_validate(category)


@router.put("/categories/{category_id}", response_model=ProductCategoryOut, responses=_ERRORS)
async def update_category(
    category_id: int,
    payload: ProductCategoryUpdate,
    user: AdminUserDep,
    service: MasterDataServiceDep,
    session: DbSessionDep,
) -> ProductCategoryOut:
    category = await service.update_product_category(category_id, payload, user)
    await session.commit()
    await session.refresh(category)
    return ProductCategoryOut.model_validate(category)


@router.get("/pos-categories", response_model=list[PosCategoryOut], responses=_ERRORS)
async def list_pos_categories(
    _: CurrentUserDep, service: MasterDataServiceDep
) -> list[PosCategoryOut]:
    return [PosCategoryOut.model_validate(item) for item in await service.list_pos_categories()]


@router.post(
    "/pos-categories",
    response_model=PosCategoryOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def create_pos_category(
    payload: PosCategoryCreate,
    user: AdminUserDep,
    service: MasterDataServiceDep,
    session: DbSessionDep,
) -> PosCategoryOut:
    category = await service.create_pos_category(payload, user)
    await session.commit()
    await session.refresh(category)
    return PosCategoryOut.model_validate(category)


# ---------------------------------------------------------------------------
# Unidades
# ---------------------------------------------------------------------------
@router.get("/units", response_model=list[UnitOfMeasureOut], responses=_ERRORS)
async def list_units(_: CurrentUserDep, service: MasterDataServiceDep) -> list[UnitOfMeasureOut]:
    return [UnitOfMeasureOut.model_validate(item) for item in await service.list_units()]


@router.post(
    "/units",
    response_model=UnitOfMeasureOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def create_unit(
    payload: UnitOfMeasureCreate,
    user: AdminUserDep,
    service: MasterDataServiceDep,
    session: DbSessionDep,
) -> UnitOfMeasureOut:
    unit = await service.create_unit(payload, user)
    await session.commit()
    await session.refresh(unit)
    return UnitOfMeasureOut.model_validate(unit)


# ---------------------------------------------------------------------------
# Productos
# ---------------------------------------------------------------------------
@router.get("/products", response_model=ProductPage, responses=_ERRORS)
async def list_products(
    _: CurrentUserDep,
    service: MasterDataServiceDep,
    session: DbSessionDep,
    search: str | None = None,
    category_id: int | None = None,
    product_type: ProductType | None = None,
    active: bool | None = None,
    sellable: bool | None = None,
    purchasable: bool | None = None,
    limit: LimitDep = 50,
    offset: OffsetDep = 0,
) -> ProductPage:
    """Busqueda y paginacion en el servidor: el maestro no viaja entero."""
    products, total = await service.list_products(
        search=search,
        category_id=category_id,
        product_type=product_type,
        active=active,
        sellable=sellable,
        purchasable=purchasable,
        limit=limit,
        offset=offset,
    )
    items: list[ProductOut] = []
    for product in products:
        category = await session.get(ProductCategory, product.product_category_id)
        pos_category = (
            await session.get(PosCategory, product.pos_category_id)
            if product.pos_category_id
            else None
        )
        items.append(_product_out(product, category=category, pos_category=pos_category))
    return ProductPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/products/{product_id}", response_model=ProductOut, responses=_ERRORS)
async def read_product(
    product_id: int,
    _: CurrentUserDep,
    service: MasterDataServiceDep,
    session: DbSessionDep,
) -> ProductOut:
    product = await service.get_product(product_id)
    category = await session.get(ProductCategory, product.product_category_id)
    pos_category = (
        await session.get(PosCategory, product.pos_category_id) if product.pos_category_id else None
    )
    return _product_out(product, category=category, pos_category=pos_category)


@router.post(
    "/products",
    response_model=ProductOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def create_product(
    payload: ProductCreate,
    user: AdminUserDep,
    service: MasterDataServiceDep,
    session: DbSessionDep,
) -> ProductOut:
    product = await service.create_product(payload, user)
    await session.commit()
    await session.refresh(product)
    return _product_out(product)


@router.put("/products/{product_id}", response_model=ProductOut, responses=_ERRORS)
async def update_product(
    product_id: int,
    payload: ProductUpdate,
    user: AdminUserDep,
    service: MasterDataServiceDep,
    session: DbSessionDep,
) -> ProductOut:
    product = await service.update_product(product_id, payload, user)
    await session.commit()
    await session.refresh(product)
    return _product_out(product)


# ---------------------------------------------------------------------------
# Terceros
# ---------------------------------------------------------------------------
@router.get("/partners", response_model=PartnerPage, responses=_ERRORS)
async def list_partners(
    _: CurrentUserDep,
    service: MasterDataServiceDep,
    search: str | None = None,
    role: PartnerRole | None = None,
    document_type: str | None = None,
    active: bool | None = None,
    limit: LimitDep = 50,
    offset: OffsetDep = 0,
) -> PartnerPage:
    partners, total = await service.list_partners(
        search=search,
        role=role,
        document_type=document_type,
        active=active,
        limit=limit,
        offset=offset,
    )
    return PartnerPage(
        items=[PartnerOut.model_validate(item) for item in partners],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/partners/{partner_id}", response_model=PartnerOut, responses=_ERRORS)
async def read_partner(
    partner_id: int, _: CurrentUserDep, service: MasterDataServiceDep
) -> PartnerOut:
    return PartnerOut.model_validate(await service.get_partner(partner_id))


@router.post(
    "/partners",
    response_model=PartnerOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def create_partner(
    payload: PartnerCreate,
    user: AdminUserDep,
    service: MasterDataServiceDep,
    session: DbSessionDep,
) -> PartnerOut:
    partner = await service.create_partner(payload, user)
    await session.commit()
    await session.refresh(partner)
    return PartnerOut.model_validate(partner)


@router.put("/partners/{partner_id}", response_model=PartnerOut, responses=_ERRORS)
async def update_partner(
    partner_id: int,
    payload: PartnerUpdate,
    user: AdminUserDep,
    service: MasterDataServiceDep,
    session: DbSessionDep,
) -> PartnerOut:
    partner = await service.update_partner(partner_id, payload, user)
    await session.commit()
    await session.refresh(partner)
    return PartnerOut.model_validate(partner)


__all__ = ["Partner", "UnitOfMeasure", "router"]
