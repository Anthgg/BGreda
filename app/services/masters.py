"""Lectura y escritura de los maestros operativos.

La busqueda y la paginacion son del servidor: el frontend nunca descarga el
maestro completo para filtrarlo en el navegador.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.models.audit import AuditAction
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
from app.schemas.masters import (
    PartnerCreate,
    PartnerUpdate,
    PosCategoryCreate,
    ProductCategoryCreate,
    ProductCategoryUpdate,
    ProductCreate,
    ProductUpdate,
    UnitOfMeasureCreate,
)
from app.services.audit import AuditRecorder
from app.services.sequences import SequenceService

MAX_PAGE_SIZE = 200


def sequence_type_for_product_type(product_type: ProductType) -> SequenceType:
    if product_type is ProductType.FINISHED_PRODUCT:
        return SequenceType.PRODUCT_50
    return SequenceType.PRODUCT_70


class MasterNotFoundError(APIError):
    status_code = 404
    code = "MASTER_NOT_FOUND"
    message = "El registro no existe"


class MasterConflictError(APIError):
    status_code = 409
    code = "MASTER_VALUE_EXISTS"
    message = "Ya existe un registro con esa clave"


class MasterReferenceError(APIError):
    status_code = 422
    code = "MASTER_INVALID_REFERENCE"
    message = "Alguna referencia enviada no existe en los catalogos"


def _limit(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE_SIZE))


class MasterDataService:
    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRecorder,
        sequences: SequenceService | None = None,
    ) -> None:
        self._session = session
        self._audit = audit
        self._sequences = sequences or SequenceService(session)

    # -- categorias ---------------------------------------------------------
    async def list_product_categories(self, *, only_active: bool = False) -> list[ProductCategory]:
        stmt = select(ProductCategory).order_by(ProductCategory.display_path)
        if only_active:
            stmt = stmt.where(ProductCategory.active.is_(True))
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_product_category(self, category_id: int) -> ProductCategory:
        category = await self._session.get(ProductCategory, category_id)
        if category is None:
            raise MasterNotFoundError("La categoria no existe")
        return category

    async def _display_path(self, name: str, parent_id: int | None) -> str:
        if parent_id is None:
            return name
        parent = await self.get_product_category(parent_id)
        return f"{parent.display_path} / {name}"

    async def create_product_category(
        self, payload: ProductCategoryCreate, user: AuthenticatedUser
    ) -> ProductCategory:
        category = ProductCategory(
            name=payload.name,
            parent_id=payload.parent_id,
            display_path=await self._display_path(payload.name, payload.parent_id),
            active=payload.active,
        )
        self._session.add(category)
        await self._flush(MasterConflictError("Ya existe una categoria con ese nombre"))
        self._record(category.id, "product_category", AuditAction.CREATE, user, category.name)
        return category

    async def update_product_category(
        self, category_id: int, payload: ProductCategoryUpdate, user: AuthenticatedUser
    ) -> ProductCategory:
        category = await self.get_product_category(category_id)
        if payload.name is not None:
            category.name = payload.name
        if payload.parent_id is not None or payload.name is not None:
            parent_id = payload.parent_id if payload.parent_id is not None else category.parent_id
            if parent_id == category.id:
                raise MasterReferenceError("Una categoria no puede ser su propia madre")
            category.parent_id = parent_id
            category.display_path = await self._display_path(category.name, parent_id)
        if payload.active is not None:
            category.active = payload.active
        await self._flush(MasterConflictError("Ya existe una categoria con ese nombre"))
        self._record(category.id, "product_category", AuditAction.UPDATE, user, category.name)
        return category

    async def list_pos_categories(self) -> list[PosCategory]:
        stmt = select(PosCategory).order_by(PosCategory.name)
        return list((await self._session.execute(stmt)).scalars().all())

    async def create_pos_category(
        self, payload: PosCategoryCreate, user: AuthenticatedUser
    ) -> PosCategory:
        category = PosCategory(
            name=payload.name, parent_id=payload.parent_id, active=payload.active
        )
        self._session.add(category)
        await self._flush(MasterConflictError("Ya existe una categoria POS con ese nombre"))
        self._record(category.id, "pos_category", AuditAction.CREATE, user, category.name)
        return category

    # -- unidades -----------------------------------------------------------
    async def list_units(self) -> list[UnitOfMeasure]:
        stmt = select(UnitOfMeasure).order_by(UnitOfMeasure.dimension, UnitOfMeasure.code)
        return list((await self._session.execute(stmt)).scalars().all())

    async def create_unit(
        self, payload: UnitOfMeasureCreate, user: AuthenticatedUser
    ) -> UnitOfMeasure:
        unit = UnitOfMeasure(
            code=payload.code,
            name=payload.name,
            symbol=payload.symbol,
            dimension=payload.dimension,
            factor_to_base=payload.factor_to_base,
            is_base=payload.factor_to_base == 1,
            active=payload.active,
        )
        self._session.add(unit)
        await self._flush(MasterConflictError("Ya existe una unidad con ese codigo"))
        self._record(unit.code, "unit_of_measure", AuditAction.CREATE, user, unit.name)
        return unit

    # -- productos ----------------------------------------------------------
    def _product_query(
        self,
        *,
        search: str | None,
        category_id: int | None,
        product_type: ProductType | None,
        active: bool | None,
        sellable: bool | None,
        purchasable: bool | None,
    ) -> Select[tuple[Product]]:
        stmt = select(Product)
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(Product.name.ilike(pattern), Product.internal_reference.ilike(pattern))
            )
        if category_id is not None:
            stmt = stmt.where(Product.product_category_id == category_id)
        if product_type is not None:
            stmt = stmt.where(Product.product_type == product_type)
        if active is not None:
            stmt = stmt.where(Product.active.is_(active))
        if sellable is not None:
            stmt = stmt.where(Product.sellable.is_(sellable))
        if purchasable is not None:
            stmt = stmt.where(Product.purchasable.is_(purchasable))
        return stmt

    async def list_products(
        self,
        *,
        search: str | None = None,
        category_id: int | None = None,
        product_type: ProductType | None = None,
        active: bool | None = None,
        sellable: bool | None = None,
        purchasable: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Product], int]:
        stmt = self._product_query(
            search=search,
            category_id=category_id,
            product_type=product_type,
            active=active,
            sellable=sellable,
            purchasable=purchasable,
        )
        total = await self._session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = await self._session.execute(
            stmt.order_by(Product.internal_reference).limit(_limit(limit)).offset(max(0, offset))
        )
        return list(rows.scalars().all()), int(total or 0)

    async def get_product(self, product_id: int) -> Product:
        product = await self._session.get(Product, product_id)
        if product is None:
            raise MasterNotFoundError("El producto no existe")
        return product

    async def create_product(self, payload: ProductCreate, user: AuthenticatedUser) -> Product:
        await self._check_product_refs(payload)
        seq_type = sequence_type_for_product_type(payload.product_type)
        reference = await self._sequences.issue(seq_type, user_id=user.id)
        product = Product(
            internal_reference=reference,
            **payload.model_dump(),
        )
        self._session.add(product)
        await self._flush(MasterConflictError("Ya existe un producto con esa referencia interna"))
        self._record(product.id, "product", AuditAction.CREATE, user, product.internal_reference)
        return product

    async def update_product(
        self, product_id: int, payload: ProductUpdate, user: AuthenticatedUser
    ) -> Product:
        product = await self.get_product(product_id)
        await self._check_product_refs(payload)
        for field, value in payload.model_dump().items():
            setattr(product, field, value)
        await self._flush(MasterConflictError("El producto choca con otro registro"))
        self._record(product.id, "product", AuditAction.UPDATE, user, product.internal_reference)
        return product

    async def _check_product_refs(self, payload: ProductCreate | ProductUpdate) -> None:
        if await self._session.get(ProductCategory, payload.product_category_id) is None:
            raise MasterReferenceError("La categoria de producto no existe")
        if payload.pos_category_id is not None:
            if await self._session.get(PosCategory, payload.pos_category_id) is None:
                raise MasterReferenceError("La categoria de punto de venta no existe")
        for code in (payload.base_uom_code, payload.purchase_uom_code):
            if code is not None and await self._session.get(UnitOfMeasure, code) is None:
                raise MasterReferenceError("La unidad de medida no existe")

    # -- terceros -----------------------------------------------------------
    async def list_partners(
        self,
        *,
        search: str | None = None,
        role: PartnerRole | None = None,
        document_type: str | None = None,
        active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Partner], int]:
        stmt = select(Partner)
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(Partner.name.ilike(pattern), Partner.document_number.ilike(pattern))
            )
        if role is not None:
            # BOTH satisface tanto el filtro de clientes como el de proveedores.
            stmt = (
                stmt.where(Partner.role == role)
                if role is PartnerRole.BOTH
                else stmt.where(Partner.role.in_([role, PartnerRole.BOTH]))
            )
        if document_type is not None:
            stmt = stmt.where(Partner.document_type == document_type)
        if active is not None:
            stmt = stmt.where(Partner.active.is_(active))
        total = await self._session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = await self._session.execute(
            stmt.order_by(Partner.name).limit(_limit(limit)).offset(max(0, offset))
        )
        return list(rows.scalars().all()), int(total or 0)

    async def get_partner(self, partner_id: int) -> Partner:
        partner = await self._session.get(Partner, partner_id)
        if partner is None:
            raise MasterNotFoundError("El tercero no existe")
        return partner

    async def create_partner(self, payload: PartnerCreate, user: AuthenticatedUser) -> Partner:
        partner = Partner(**payload.model_dump())
        self._session.add(partner)
        await self._flush(MasterConflictError("Ya existe un tercero con ese documento"))
        self._record(partner.id, "partner", AuditAction.CREATE, user, partner.name)
        return partner

    async def update_partner(
        self, partner_id: int, payload: PartnerUpdate, user: AuthenticatedUser
    ) -> Partner:
        partner = await self.get_partner(partner_id)
        for field, value in payload.model_dump().items():
            setattr(partner, field, value)
        await self._flush(MasterConflictError("Ya existe un tercero con ese documento"))
        self._record(partner.id, "partner", AuditAction.UPDATE, user, partner.name)
        return partner

    # -- utilidades ---------------------------------------------------------
    async def _flush(self, conflict: MasterConflictError) -> None:
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise conflict from exc

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
