"""Orquestador transaccional del Cotizador multiproducto de Fase 005.11."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import APIError
from app.models.audit import AuditAction
from app.models.firings import FiringType
from app.models.masters import Partner, Product
from app.models.quotations import (
    Quotation,
    QuotationItem,
    QuotationStatus,
    QuotationWorkflow,
)
from app.models.recipes import Recipe, RecipeStatus, RecipeVersion
from app.models.sequence import SequenceType
from app.schemas.auth import AuthenticatedUser
from app.schemas.firings import FiringIn, FiringLineIn, FiringSessionIn
from app.schemas.quotation_builder import (
    ProductDimensionCompletionIn,
    QuotationBuilderCreateIn,
    QuotationBuilderDraftIn,
    QuotationBuilderItemIn,
    QuotationBuilderItemOut,
    QuotationBuilderOut,
)
from app.schemas.quotations import (
    AdditionalSelectionIn,
    OtherCostSelectionIn,
    QuotationCalculateIn,
    TechniqueSelectionIn,
)
from app.services.audit import AuditRecorder
from app.services.firings import FiringService
from app.services.quotations import FiringEstimateOverride, QuotationService
from app.services.sequences import SequenceService

ZERO = Decimal(0)
BUILDER_ENTITY = "quotation_builder"
PRODUCTION_DIMENSIONS = ("length", "width", "height")
ALL_DIMENSIONS = ("width", "height", "length", "depth")


class QuotationBuilderNotFoundError(APIError):
    status_code = 404
    code = "QUOTATION_BUILDER_NOT_FOUND"
    message = "La cotizacion del Cotizador no existe"


class QuotationBuilderNotEditableError(APIError):
    status_code = 409
    code = "QUOTATION_BUILDER_NOT_EDITABLE"
    message = "Solo un borrador del Cotizador se puede editar"


class QuotationBuilderConflictError(APIError):
    status_code = 409
    code = "QUOTATION_BUILDER_CONFLICT"
    message = "El borrador cambio en otra sesion; vuelva a cargarlo"


class QuotationBuilderSourceChangedError(APIError):
    status_code = 409
    code = "QUOTATION_BUILDER_SOURCE_CHANGED"
    message = "Una fuente cambio; guarde el recalculo antes de confirmar"


class ProductDimensionConflictError(APIError):
    status_code = 409
    code = "PRODUCT_DIMENSION_CONFLICT"
    message = "Una dimension del producto ya fue completada por otro usuario"


class QuotationBuilderIncompleteError(APIError):
    status_code = 409
    code = "QUOTATION_BUILDER_INCOMPLETE"
    message = "Complete los datos obligatorios antes de confirmar"


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


class QuotationBuilderService:
    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRecorder,
        sequences: SequenceService,
        firings: FiringService,
        quotations: QuotationService,
    ) -> None:
        self._session = session
        self._audit = audit
        self._sequences = sequences
        self._firings = firings
        self._quotations = quotations

    async def _get(self, quotation_id: int, *, for_update: bool = False) -> Quotation:
        stmt = (
            select(Quotation)
            .where(
                Quotation.id == quotation_id,
                Quotation.workflow == QuotationWorkflow.COTIZADOR,
            )
            .options(selectinload(Quotation.items))
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise QuotationBuilderNotFoundError()
        return row

    @staticmethod
    def _ensure_draft(row: Quotation) -> None:
        if row.status is not QuotationStatus.DRAFT:
            raise QuotationBuilderNotEditableError()

    @staticmethod
    def _ensure_fresh(row: Quotation, expected: datetime) -> None:
        current = row.updated_at
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        if expected.tzinfo is None:
            expected = expected.replace(tzinfo=UTC)
        if current != expected:
            raise QuotationBuilderConflictError()

    async def _complete_dimensions(
        self, payload: QuotationBuilderDraftIn, user: AuthenticatedUser
    ) -> None:
        """Completa solo NULL bajo bloqueo, sin sobrescrituras silenciosas."""

        for item in sorted(payload.items, key=lambda value: value.product_id):
            submitted = item.dimensions.model_dump(exclude_none=True)
            if not submitted:
                continue
            product = await self._quotations.resolve_product(item.product_id, for_update=True)
            changes: dict[str, tuple[object, object]] = {}
            for field, value in submitted.items():
                current = getattr(product, field)
                if current is not None and current != value:
                    raise ProductDimensionConflictError(
                        f"{field} ya tiene el valor {current}",
                        details=[{"product_id": product.id, "field": field}],
                    )
                if current is None:
                    setattr(product, field, value)
                    changes[field] = (None, value)
            if changes:
                self._audit.record_changes(
                    entity_type="product",
                    entity_id=str(product.id),
                    changes=changes,
                    user_id=user.id,
                    user_display_name=user.display_name,
                )
        await self._session.flush()

    async def _recipe_for(
        self, item: QuotationBuilderItemIn
    ) -> tuple[int | None, int | None, str | None, bool]:
        recipe_id = item.recipe_id
        version_id = item.recipe_version_id
        if version_id is not None:
            version = (
                await self._session.execute(
                    select(RecipeVersion).where(RecipeVersion.id == version_id)
                )
            ).scalar_one_or_none()
            return recipe_id, version_id, version.fingerprint if version else None, False

        stmt = (
            select(RecipeVersion)
            .join(Recipe, Recipe.id == RecipeVersion.recipe_id)
            .where(
                RecipeVersion.status == RecipeStatus.ACTIVE,
                Recipe.active.is_(True),
            )
            .order_by(RecipeVersion.id)
        )
        if recipe_id is not None:
            stmt = stmt.where(Recipe.id == recipe_id)
        versions = list((await self._session.execute(stmt)).scalars().all())
        if len(versions) == 1:
            version = versions[0]
            return version.recipe_id, version.id, version.fingerprint, True
        return recipe_id, None, None, False

    @staticmethod
    def _effective_dimensions(
        product: Product, completion: ProductDimensionCompletionIn
    ) -> dict[str, Decimal | None]:
        submitted = completion.model_dump(exclude_none=True)
        result: dict[str, Decimal | None] = {}
        for field in ALL_DIMENSIONS:
            current = getattr(product, field)
            candidate = submitted.get(field)
            if current is not None and candidate is not None and current != candidate:
                raise ProductDimensionConflictError(
                    f"{field} ya tiene el valor {current}",
                    details=[{"product_id": product.id, "field": field}],
                )
            result[field] = current if current is not None else candidate
        return result

    async def _resolve_items(
        self, payload: QuotationBuilderDraftIn
    ) -> list[
        tuple[
            QuotationBuilderItemIn,
            Product,
            dict[str, Decimal | None],
            tuple[int | None, int | None, str | None, bool],
        ]
    ]:
        resolved = []
        for item in sorted(payload.items, key=lambda value: (value.sort_order, value.product_id)):
            product = await self._quotations.resolve_product(item.product_id)
            dimensions = self._effective_dimensions(product, item.dimensions)
            recipe = await self._recipe_for(item)
            resolved.append((item, product, dimensions, recipe))
        return resolved

    async def _simulate_production(
        self,
        kiln_id: int | None,
        resolved: list[
            tuple[
                QuotationBuilderItemIn,
                Product,
                dict[str, Decimal | None],
                tuple[int | None, int | None, str | None, bool],
            ]
        ],
    ) -> tuple[dict[str, Any], dict[int, dict[str, Any]], dict[str, Any], list[str]]:
        warnings: list[str] = []
        if kiln_id is None:
            warnings.append("KILN_REQUIRED")
        if not resolved:
            warnings.append("ITEM_REQUIRED")
        for item, _product, dimensions, _recipe in resolved:
            if item.quantity is None:
                warnings.append("QUANTITY_REQUIRED")
            if any(dimensions[field] is None for field in PRODUCTION_DIMENSIONS):
                warnings.append("PRODUCTION_DIMENSIONS_REQUIRED")

        if warnings:
            summary = {"estimated": True, "complete": False, "warnings": _unique(warnings)}
            return summary, {}, {}, _unique(warnings)

        assert kiln_id is not None
        for item, _product, dimensions, _recipe in resolved:
            assert item.quantity is not None
            assert all(dimensions[field] is not None for field in PRODUCTION_DIMENSIONS)
        firing_payload = FiringIn(
            sessions=[
                FiringSessionIn(kiln_id=kiln_id, firing_type=FiringType.LOW, sort_order=0),
                FiringSessionIn(kiln_id=kiln_id, firing_type=FiringType.HIGH, sort_order=1),
            ],
            lines=[
                FiringLineIn(
                    product_id=product.id,
                    description=product.name,
                    quantity=cast(int, item.quantity),
                    length_cm=cast(Decimal, dimensions["length"]),
                    width_cm=cast(Decimal, dimensions["width"]),
                    height_cm=cast(Decimal, dimensions["height"]),
                    low_kiln_id=kiln_id,
                    high_kiln_id=kiln_id,
                    factor_kiln_id=kiln_id,
                    sort_order=index,
                )
                for index, (item, product, dimensions, _recipe) in enumerate(resolved)
            ],
        )
        result = await self._firings.calculate(firing_payload)
        raw = result.model_dump(mode="json")
        raw["estimated"] = True
        raw["complete"] = not result.capacity_exceeded
        line_by_product = {
            line.product_id: line.model_dump(mode="json")
            for line in result.lines
            if line.product_id is not None
        }
        kiln_snapshot = result.sessions[0].model_dump(mode="json") if result.sessions else {}
        if result.capacity_exceeded:
            warnings.append("KILN_CAPACITY_EXCEEDED")
        return raw, line_by_product, kiln_snapshot, warnings

    async def preview(self, payload: QuotationBuilderDraftIn) -> QuotationBuilderOut:
        customer = await self._quotations.resolve_customer(payload.customer_id)
        settings = await self._quotations.commercial_settings()
        resolved = await self._resolve_items(payload)
        (
            production,
            production_lines,
            kiln_snapshot,
            production_warnings,
        ) = await self._simulate_production(payload.kiln_id, resolved)

        item_outputs: list[QuotationBuilderItemOut] = []
        for item, product, dimensions, recipe in resolved:
            recipe_id, version_id, version_fingerprint, auto_selected = recipe
            warnings: list[str] = []
            if item.quantity is None:
                warnings.append("QUANTITY_REQUIRED")
            missing_dimensions = [
                field for field in PRODUCTION_DIMENSIONS if dimensions[field] is None
            ]
            if missing_dimensions:
                warnings.append("PRODUCTION_DIMENSIONS_REQUIRED")
            if payload.kiln_id is None:
                warnings.append("KILN_REQUIRED")
            if recipe_id is None or version_id is None:
                warnings.append("RECIPE_REQUIRED")
            if item.material_grams_per_piece is None:
                warnings.append("MATERIAL_GRAMS_PER_PIECE_REQUIRED")
            if "KILN_CAPACITY_EXCEEDED" in production_warnings:
                warnings.append("KILN_CAPACITY_EXCEEDED")

            line_snapshot = production_lines.get(product.id, {})
            source_key = {
                "simulation": _fingerprint(production),
                "product_id": product.id,
                "line": line_snapshot,
            }
            estimate = FiringEstimateOverride(
                cost=Decimal(str(line_snapshot.get("allocated_cost", "0"))),
                snapshot={
                    "estimated": True,
                    "kiln": kiln_snapshot,
                    "production": line_snapshot,
                }
                if line_snapshot
                else {"estimated": True},
                source_key=source_key,
            )

            calculation = None
            if item.quantity is not None:
                calculation = await self._quotations.calculate_with_firing_estimate(
                    QuotationCalculateIn(
                        name=payload.name,
                        customer_id=payload.customer_id,
                        product_id=product.id,
                        quantity=item.quantity,
                        recipe_id=recipe_id,
                        recipe_version_id=version_id,
                        material_grams_per_piece=item.material_grams_per_piece,
                        techniques=item.techniques,
                        additionals=item.additionals,
                        days_adjustment=item.days_adjustment,
                        waiting_days=item.waiting_days,
                        other_costs=item.other_costs,
                        markup_percent=item.markup_percent,
                        commercial_sale_unit_price=item.commercial_sale_unit_price,
                    ),
                    estimate,
                )
                warnings.extend(calculation.warnings)

            editable_dimensions = [
                field for field in ALL_DIMENSIONS if getattr(product, field) is None
            ]
            item_fingerprint = (
                calculation.source_fingerprint
                if calculation is not None
                else _fingerprint(
                    {
                        "product": [product.id, product.updated_at],
                        "recipe": [recipe_id, version_id, version_fingerprint],
                        "production": source_key,
                        "input": item.model_dump(mode="json"),
                    }
                )
            )
            item_outputs.append(
                QuotationBuilderItemOut(
                    id=item.id,
                    product_id=product.id,
                    product_internal_reference=product.internal_reference,
                    product_name=product.name,
                    product_type=product.product_type.value,
                    product_uom=product.base_uom_code,
                    product_material=product.material,
                    product_grammage=product.grammage,
                    width=dimensions["width"],
                    height=dimensions["height"],
                    length=dimensions["length"],
                    depth=dimensions["depth"],
                    editable_dimensions=editable_dimensions,
                    quantity=item.quantity,
                    recipe_id=recipe_id,
                    recipe_version_id=version_id,
                    recipe_version_fingerprint_snapshot=version_fingerprint,
                    recipe_auto_selected=auto_selected,
                    material_grams_per_piece=item.material_grams_per_piece,
                    kiln_id=payload.kiln_id,
                    production_snapshot=line_snapshot,
                    techniques=(
                        [value.model_dump(mode="json") for value in calculation.techniques]
                        if calculation
                        else [value.model_dump(mode="json") for value in item.techniques]
                    ),
                    additionals=(
                        [value.model_dump(mode="json") for value in calculation.additionals]
                        if calculation
                        else [value.model_dump(mode="json") for value in item.additionals]
                    ),
                    other_costs=(
                        [value.model_dump(mode="json") for value in calculation.other_costs]
                        if calculation
                        else [value.model_dump(mode="json") for value in (item.other_costs or [])]
                    ),
                    materials_calculated=calculation.materials_calculated if calculation else ZERO,
                    materials_applied=calculation.materials_applied if calculation else ZERO,
                    firing_cost=calculation.firing_cost if calculation else ZERO,
                    labor_cost=calculation.labor_cost if calculation else ZERO,
                    calculated_days=calculation.calculated_days if calculation else 0,
                    days_adjustment=item.days_adjustment,
                    waiting_days=item.waiting_days,
                    total_days=calculation.total_days if calculation else 0,
                    space_cost=calculation.space_cost if calculation else ZERO,
                    final_unit_cost=calculation.final_unit_cost if calculation else ZERO,
                    final_total_cost=calculation.final_total_cost if calculation else ZERO,
                    markup_percent=item.markup_percent,
                    calculated_sale_unit_price=(
                        calculation.calculated_sale_unit_price if calculation else ZERO
                    ),
                    suggested_commercial_unit_price=(
                        calculation.suggested_commercial_unit_price if calculation else ZERO
                    ),
                    commercial_sale_unit_price=(
                        calculation.commercial_sale_unit_price if calculation else ZERO
                    ),
                    effective_profit_unit=(
                        calculation.effective_profit_unit if calculation else ZERO
                    ),
                    effective_profit_total=(
                        calculation.effective_profit_total if calculation else ZERO
                    ),
                    effective_markup_percent=(
                        calculation.effective_markup_percent if calculation else ZERO
                    ),
                    commercial_subtotal=(calculation.commercial_subtotal if calculation else ZERO),
                    tax_percentage_snapshot=(calculation.tax_percentage if calculation else ZERO),
                    tax_rate_source_snapshot=(
                        calculation.tax_rate_source if calculation else "COMMERCIAL_SETTINGS"
                    ),
                    tax_amount=(
                        calculation.commercial_total - calculation.commercial_subtotal
                        if calculation
                        else ZERO
                    ),
                    source_fingerprint=item_fingerprint,
                    warnings=_unique(warnings),
                    complete=not any(
                        code in warnings
                        for code in (
                            "QUANTITY_REQUIRED",
                            "PRODUCTION_DIMENSIONS_REQUIRED",
                            "KILN_REQUIRED",
                            "KILN_CAPACITY_EXCEEDED",
                            "RECIPE_REQUIRED",
                            "MATERIAL_GRAMS_PER_PIECE_REQUIRED",
                        )
                    ),
                    sort_order=item.sort_order,
                )
            )

        subtotal = sum((item.commercial_subtotal for item in item_outputs), ZERO)
        tax_amount = sum((item.tax_amount for item in item_outputs), ZERO)
        total = subtotal + tax_amount
        tax_rates = {item.tax_percentage_snapshot for item in item_outputs}
        tax_sources = {item.tax_rate_source_snapshot for item in item_outputs}
        header_warnings = list(production_warnings)
        if not payload.name:
            header_warnings.append("QUOTATION_NAME_REQUIRED")
        if customer is None:
            header_warnings.append("CUSTOMER_REQUIRED")
        if len(tax_rates) > 1:
            header_warnings.append("MIXED_TAX_RATES")
        complete = bool(
            payload.name
            and customer is not None
            and item_outputs
            and all(item.complete for item in item_outputs)
        )
        if not payload.name or customer is None:
            next_step = "GENERAL_DATA"
        elif not item_outputs or any(
            "PRODUCTION_DIMENSIONS_REQUIRED" in item.warnings for item in item_outputs
        ):
            next_step = "ITEMS"
        elif not complete:
            next_step = "PRODUCTION"
        else:
            next_step = "SUMMARY"

        source = _fingerprint(
            {
                "customer": [customer.id, customer.updated_at] if customer else None,
                "settings": [settings.id, settings.version, settings.updated_at],
                "production": production,
                "items": [item.source_fingerprint for item in item_outputs],
            }
        )
        return QuotationBuilderOut(
            name=payload.name,
            customer_id=customer.id if customer else None,
            customer_name_snapshot=customer.name if customer else None,
            kiln_id=payload.kiln_id,
            kiln_snapshot=kiln_snapshot,
            production_summary=production,
            items=item_outputs,
            item_count=len(item_outputs),
            commercial_subtotal=subtotal,
            tax_percentage_snapshot=next(iter(tax_rates)) if len(tax_rates) == 1 else ZERO,
            tax_rate_source_snapshot=(
                next(iter(tax_sources)) if len(tax_sources) == 1 else "MIXED"
            ),
            tax_amount=tax_amount,
            total_with_tax=total,
            currency_code_snapshot=settings.currency_code or "PEN",
            currency_symbol_snapshot=settings.currency_symbol or "S/",
            warnings=_unique(header_warnings),
            complete=complete,
            next_step=next_step,
            source_fingerprint=source,
        )

    @staticmethod
    def _customer_snapshot(row: Quotation, customer: Partner | None) -> None:
        row.customer_id = customer.id if customer else None
        row.customer_name_snapshot = customer.name if customer else None
        row.customer_trade_name_snapshot = customer.reference or customer.name if customer else None
        row.customer_document_type_snapshot = (
            customer.document_type.value if customer and customer.document_type else None
        )
        row.customer_document_number_snapshot = customer.document_number if customer else None
        row.customer_address_snapshot = customer.address if customer else None
        row.customer_ubigeo_snapshot = (
            customer.district or customer.ubigeo_code if customer else None
        )
        row.customer_email_snapshot = customer.email if customer else None
        row.customer_phone_snapshot = customer.phone or customer.mobile if customer else None

    async def _apply(self, row: Quotation, preview: QuotationBuilderOut) -> None:
        customer = await self._quotations.resolve_customer(preview.customer_id)
        settings = await self._quotations.commercial_settings()
        row.name = preview.name
        self._customer_snapshot(row, customer)
        row.product_id = None
        row.quantity = None
        row.firing_id = None
        row.firing_line_id = None
        row.firing_code_snapshot = None
        row.firing_snapshot = preview.production_summary
        row.materials_calculated = sum((item.materials_calculated for item in preview.items), ZERO)
        row.materials_applied = sum((item.materials_applied for item in preview.items), ZERO)
        row.firing_cost = sum((item.firing_cost for item in preview.items), ZERO)
        row.labor_cost = sum((item.labor_cost for item in preview.items), ZERO)
        row.calculated_days = max((item.calculated_days for item in preview.items), default=0)
        row.days_adjustment = max((item.days_adjustment for item in preview.items), default=0)
        row.waiting_days = max((item.waiting_days for item in preview.items), default=0)
        row.total_days = max((item.total_days for item in preview.items), default=0)
        row.space_cost = sum((item.space_cost for item in preview.items), ZERO)
        row.commercial_factor_default_snapshot = settings.default_quotation_factor
        row.commercial_factor = settings.default_quotation_factor
        row.base_commercial_cost = sum((item.final_total_cost for item in preview.items), ZERO)
        row.calculated_total = row.base_commercial_cost
        row.calculated_unit_price = ZERO
        row.final_unit_cost = ZERO
        row.final_total_cost = row.base_commercial_cost
        row.markup_percent = ZERO
        row.target_profit_unit = ZERO
        row.calculated_sale_unit_price = ZERO
        row.suggested_commercial_unit_price = ZERO
        row.commercial_sale_unit_price = ZERO
        row.effective_profit_unit = ZERO
        row.effective_profit_total = sum(
            (item.effective_profit_total for item in preview.items), ZERO
        )
        row.effective_markup_percent = ZERO
        row.commercial_subtotal = preview.commercial_subtotal
        row.commercial_total = preview.total_with_tax
        row.commercial_unit_price_with_tax = ZERO
        row.currency_code_snapshot = preview.currency_code_snapshot
        row.currency_symbol_snapshot = preview.currency_symbol_snapshot
        row.tax_percentage_snapshot = preview.tax_percentage_snapshot
        row.tax_rate_source_snapshot = preview.tax_rate_source_snapshot
        row.tax_amount = preview.tax_amount
        row.total_with_tax = preview.total_with_tax
        row.unit_price_with_tax = ZERO
        row.source_fingerprint = preview.source_fingerprint
        row.calculation_warnings = preview.warnings
        row.updated_at = datetime.now(UTC)

        row.items.clear()
        # La unicidad (quotation_id, sort_order) exige materializar primero los
        # DELETE de las lineas anteriores antes de insertar el nuevo snapshot.
        await self._session.flush()
        for item in preview.items:
            row.items.append(
                QuotationItem(
                    product_id=item.product_id,
                    sort_order=item.sort_order,
                    quantity=item.quantity,
                    product_name_snapshot=item.product_name,
                    product_internal_reference_snapshot=item.product_internal_reference,
                    product_type_snapshot=item.product_type,
                    product_uom_snapshot=item.product_uom,
                    product_material_snapshot=item.product_material,
                    product_grammage_snapshot=item.product_grammage,
                    product_width_snapshot=item.width,
                    product_height_snapshot=item.height,
                    product_length_snapshot=item.length,
                    product_depth_snapshot=item.depth,
                    recipe_id=item.recipe_id,
                    recipe_version_id=item.recipe_version_id,
                    recipe_version_fingerprint_snapshot=(item.recipe_version_fingerprint_snapshot),
                    material_grams_per_piece=item.material_grams_per_piece,
                    kiln_id=item.kiln_id,
                    kiln_snapshot=preview.kiln_snapshot,
                    production_snapshot=item.production_snapshot,
                    techniques_snapshot=item.techniques,
                    additionals_snapshot=item.additionals,
                    other_costs_snapshot=item.other_costs,
                    materials_calculated=item.materials_calculated,
                    materials_applied=item.materials_applied,
                    firing_cost=item.firing_cost,
                    labor_cost=item.labor_cost,
                    calculated_days=item.calculated_days,
                    days_adjustment=item.days_adjustment,
                    waiting_days=item.waiting_days,
                    total_days=item.total_days,
                    space_cost=item.space_cost,
                    final_unit_cost=item.final_unit_cost,
                    final_total_cost=item.final_total_cost,
                    markup_percent=item.markup_percent,
                    calculated_sale_unit_price=item.calculated_sale_unit_price,
                    suggested_commercial_unit_price=item.suggested_commercial_unit_price,
                    commercial_sale_unit_price=item.commercial_sale_unit_price,
                    effective_profit_unit=item.effective_profit_unit,
                    effective_profit_total=item.effective_profit_total,
                    effective_markup_percent=item.effective_markup_percent,
                    commercial_subtotal=item.commercial_subtotal,
                    tax_percentage_snapshot=item.tax_percentage_snapshot,
                    tax_rate_source_snapshot=item.tax_rate_source_snapshot,
                    tax_amount=item.tax_amount,
                    source_fingerprint=item.source_fingerprint,
                    calculation_warnings=item.warnings,
                )
            )
        await self._session.flush()

    async def create(
        self, payload: QuotationBuilderCreateIn, *, user: AuthenticatedUser
    ) -> QuotationBuilderOut:
        await self._complete_dimensions(payload, user)
        preview = await self.preview(payload)
        settings = await self._quotations.commercial_settings()
        row = Quotation(
            code=await self._sequences.issue(SequenceType.QUOTE, user_id=user.id),
            workflow=QuotationWorkflow.COTIZADOR,
            status=QuotationStatus.DRAFT,
            name=preview.name,
            product_id=None,
            quantity=None,
            commercial_factor_default_snapshot=settings.default_quotation_factor,
            commercial_factor=settings.default_quotation_factor,
            source_fingerprint=preview.source_fingerprint,
            created_by_id=user.id,
            items=[],
        )
        self._session.add(row)
        await self._session.flush()
        await self._apply(row, preview)
        self._audit.record_action(
            entity_type=BUILDER_ENTITY,
            entity_id=str(row.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"code": row.code, "status": row.status.value, "items": len(row.items)},
        )
        return await self.get(row.id)

    async def update(
        self,
        quotation_id: int,
        payload: QuotationBuilderDraftIn,
        *,
        expected_updated_at: datetime,
        user: AuthenticatedUser,
    ) -> QuotationBuilderOut:
        row = await self._get(quotation_id, for_update=True)
        self._ensure_draft(row)
        self._ensure_fresh(row, expected_updated_at)
        await self._complete_dimensions(payload, user)
        preview = await self.preview(payload)
        await self._apply(row, preview)
        self._audit.record_action(
            entity_type=BUILDER_ENTITY,
            entity_id=str(row.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"code": row.code, "status": row.status.value, "items": len(row.items)},
        )
        return await self.get(row.id)

    async def confirm(
        self,
        quotation_id: int,
        *,
        expected_updated_at: datetime,
        user: AuthenticatedUser,
    ) -> QuotationBuilderOut:
        row = await self._get(quotation_id, for_update=True)
        self._ensure_draft(row)
        self._ensure_fresh(row, expected_updated_at)
        output = self._stored_output(row)
        if not output.complete:
            raise QuotationBuilderIncompleteError(details=[{"warnings": output.warnings}])
        recalculated = await self.preview(self._to_input(row))
        if recalculated.source_fingerprint != row.source_fingerprint:
            raise QuotationBuilderSourceChangedError()
        if not recalculated.complete:
            raise QuotationBuilderIncompleteError(details=[{"warnings": recalculated.warnings}])
        row.status = QuotationStatus.CONFIRMED
        row.confirmed_at = datetime.now(UTC)
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        self._audit.record_action(
            entity_type=BUILDER_ENTITY,
            entity_id=str(row.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"code": row.code, "status": row.status.value},
        )
        return await self.get(row.id)

    async def cancel(self, quotation_id: int, *, user: AuthenticatedUser) -> QuotationBuilderOut:
        row = await self._get(quotation_id, for_update=True)
        if row.status is QuotationStatus.CANCELLED:
            raise QuotationBuilderNotEditableError("La cotizacion ya esta cancelada")
        row.status = QuotationStatus.CANCELLED
        row.cancelled_at = datetime.now(UTC)
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        self._audit.record_action(
            entity_type=BUILDER_ENTITY,
            entity_id=str(row.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"code": row.code, "status": row.status.value},
        )
        return await self.get(row.id)

    @staticmethod
    def _technique_input(value: dict[str, Any]) -> TechniqueSelectionIn:
        return TechniqueSelectionIn(
            technique_id=value["technique_id"],
            unit_price=value.get("unit_price_snapshot"),
            factor_1=value.get("factor_1_snapshot"),
            factor_2=value.get("factor_2_snapshot"),
            quantity=value.get("quantity", 1),
            applied_cost=value.get("applied_cost"),
            applied_days=value.get("applied_days"),
            sort_order=value.get("sort_order", 0),
        )

    @staticmethod
    def _additional_input(value: dict[str, Any]) -> AdditionalSelectionIn:
        return AdditionalSelectionIn(
            additional_id=value["additional_id"],
            unit_price=value.get("unit_price_snapshot"),
            factor_1=value.get("factor_1_snapshot"),
            additional_quantity=value.get("additional_quantity", 1),
            applied_cost=value.get("applied_cost"),
            sort_order=value.get("sort_order", 0),
        )

    @staticmethod
    def _other_cost_input(value: dict[str, Any]) -> OtherCostSelectionIn:
        return OtherCostSelectionIn(
            other_cost_id=value["other_cost_id"],
            unit_price=value.get("unit_price_snapshot"),
            sort_order=value.get("sort_order", 0),
        )

    def _to_input(self, row: Quotation) -> QuotationBuilderCreateIn:
        return QuotationBuilderCreateIn(
            name=row.name,
            customer_id=row.customer_id,
            kiln_id=next((item.kiln_id for item in row.items if item.kiln_id), None),
            items=[
                QuotationBuilderItemIn(
                    product_id=item.product_id,
                    quantity=item.quantity,
                    recipe_id=item.recipe_id,
                    recipe_version_id=item.recipe_version_id,
                    material_grams_per_piece=item.material_grams_per_piece,
                    techniques=[self._technique_input(value) for value in item.techniques_snapshot],
                    additionals=[
                        self._additional_input(value) for value in item.additionals_snapshot
                    ],
                    other_costs=[
                        self._other_cost_input(value) for value in item.other_costs_snapshot
                    ],
                    days_adjustment=item.days_adjustment,
                    waiting_days=item.waiting_days,
                    markup_percent=item.markup_percent,
                    commercial_sale_unit_price=item.commercial_sale_unit_price,
                    sort_order=item.sort_order,
                )
                for item in row.items
            ],
        )

    async def duplicate(self, quotation_id: int, *, user: AuthenticatedUser) -> QuotationBuilderOut:
        row = await self._get(quotation_id)
        return await self.create(self._to_input(row), user=user)

    @staticmethod
    def _item_complete(item: QuotationItem) -> bool:
        required = (
            item.quantity,
            item.product_length_snapshot,
            item.product_width_snapshot,
            item.product_height_snapshot,
            item.kiln_id,
            item.recipe_id,
            item.recipe_version_id,
            item.material_grams_per_piece,
        )
        blocked = {
            "KILN_CAPACITY_EXCEEDED",
            "RECIPE_REQUIRED",
            "MATERIAL_GRAMS_PER_PIECE_REQUIRED",
            "PRODUCTION_DIMENSIONS_REQUIRED",
            "QUANTITY_REQUIRED",
            "KILN_REQUIRED",
        }
        return all(value is not None for value in required) and not blocked.intersection(
            item.calculation_warnings
        )

    def _stored_output(self, row: Quotation) -> QuotationBuilderOut:
        item_outputs = [
            QuotationBuilderItemOut(
                id=item.id,
                product_id=item.product_id,
                product_internal_reference=item.product_internal_reference_snapshot,
                product_name=item.product_name_snapshot,
                product_type=item.product_type_snapshot,
                product_uom=item.product_uom_snapshot,
                product_material=item.product_material_snapshot,
                product_grammage=item.product_grammage_snapshot,
                width=item.product_width_snapshot,
                height=item.product_height_snapshot,
                length=item.product_length_snapshot,
                depth=item.product_depth_snapshot,
                editable_dimensions=(
                    []
                    if row.status is not QuotationStatus.DRAFT
                    else [
                        field
                        for field in ALL_DIMENSIONS
                        if getattr(item, f"product_{field}_snapshot") is None
                    ]
                ),
                quantity=item.quantity,
                recipe_id=item.recipe_id,
                recipe_version_id=item.recipe_version_id,
                recipe_version_fingerprint_snapshot=(item.recipe_version_fingerprint_snapshot),
                material_grams_per_piece=item.material_grams_per_piece,
                kiln_id=item.kiln_id,
                production_snapshot=item.production_snapshot,
                techniques=item.techniques_snapshot,
                additionals=item.additionals_snapshot,
                other_costs=item.other_costs_snapshot,
                materials_calculated=item.materials_calculated,
                materials_applied=item.materials_applied,
                firing_cost=item.firing_cost,
                labor_cost=item.labor_cost,
                calculated_days=item.calculated_days,
                days_adjustment=item.days_adjustment,
                waiting_days=item.waiting_days,
                total_days=item.total_days,
                space_cost=item.space_cost,
                final_unit_cost=item.final_unit_cost,
                final_total_cost=item.final_total_cost,
                markup_percent=item.markup_percent,
                calculated_sale_unit_price=item.calculated_sale_unit_price,
                suggested_commercial_unit_price=item.suggested_commercial_unit_price,
                commercial_sale_unit_price=item.commercial_sale_unit_price,
                effective_profit_unit=item.effective_profit_unit,
                effective_profit_total=item.effective_profit_total,
                effective_markup_percent=item.effective_markup_percent,
                commercial_subtotal=item.commercial_subtotal,
                tax_percentage_snapshot=item.tax_percentage_snapshot,
                tax_rate_source_snapshot=item.tax_rate_source_snapshot,
                tax_amount=item.tax_amount,
                source_fingerprint=item.source_fingerprint,
                warnings=item.calculation_warnings,
                complete=self._item_complete(item),
                sort_order=item.sort_order,
            )
            for item in row.items
        ]
        complete = bool(
            row.name
            and row.customer_id
            and item_outputs
            and all(item.complete for item in item_outputs)
        )
        if not row.name or not row.customer_id:
            next_step = "GENERAL_DATA"
        elif not item_outputs or any(
            "PRODUCTION_DIMENSIONS_REQUIRED" in item.warnings for item in item_outputs
        ):
            next_step = "ITEMS"
        elif not complete:
            next_step = "PRODUCTION"
        else:
            next_step = "SUMMARY"
        return QuotationBuilderOut(
            id=row.id,
            code=row.code,
            workflow=row.workflow,
            status=row.status,
            name=row.name,
            customer_id=row.customer_id,
            customer_name_snapshot=row.customer_name_snapshot,
            kiln_id=next((item.kiln_id for item in row.items if item.kiln_id), None),
            kiln_snapshot=next(
                (item.kiln_snapshot for item in row.items if item.kiln_snapshot), {}
            ),
            production_summary=row.firing_snapshot,
            items=item_outputs,
            item_count=len(item_outputs),
            commercial_subtotal=row.commercial_subtotal,
            tax_percentage_snapshot=row.tax_percentage_snapshot,
            tax_rate_source_snapshot=row.tax_rate_source_snapshot,
            tax_amount=row.tax_amount,
            total_with_tax=row.total_with_tax,
            currency_code_snapshot=row.currency_code_snapshot,
            currency_symbol_snapshot=row.currency_symbol_snapshot,
            warnings=row.calculation_warnings,
            complete=complete,
            next_step=next_step,
            source_fingerprint=row.source_fingerprint,
            created_at=row.created_at,
            updated_at=row.updated_at,
            confirmed_at=row.confirmed_at,
            cancelled_at=row.cancelled_at,
        )

    async def get(self, quotation_id: int) -> QuotationBuilderOut:
        return self._stored_output(await self._get(quotation_id))
