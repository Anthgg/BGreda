"""Autoridad de negocio para maestros de costos y cotizaciones."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal, TypeVar, cast

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import APIError
from app.core.quotations import (
    AdditionalFormulaType as CoreAdditionalFormulaType,
)
from app.core.quotations import (
    AdditionalInput,
    OtherCostInput,
    QuotationCalculationError,
    QuotationInput,
    TechniqueInput,
    calculate_quotation,
)
from app.core.quotations import (
    OtherCostCalculationType as CoreOtherCostCalculationType,
)
from app.core.quotations import (
    TechniqueFormulaType as CoreTechniqueFormulaType,
)
from app.models.audit import AuditAction
from app.models.firings import Firing, FiringLine, FiringStatus
from app.models.masters import Partner, PartnerRole, Product, ProductType
from app.models.quotations import (
    Additional,
    AdditionalFormulaType,
    OtherCost,
    Quotation,
    QuotationAdditional,
    QuotationItem,
    QuotationOtherCost,
    QuotationProductPriceUpdate,
    QuotationStatus,
    QuotationTechnique,
    QuotationWorkflow,
    Technique,
)
from app.models.recipes import Recipe, RecipeStatus, RecipeVersion
from app.models.sequence import SequenceType
from app.models.settings import SINGLETON_ID, CommercialSettings
from app.schemas.auth import AuthenticatedUser
from app.schemas.quotations import (
    AdditionalCalculationOut,
    AdditionalCreate,
    AdditionalSelectionIn,
    AdditionalUpdate,
    OtherCostCalculationOut,
    OtherCostCreate,
    OtherCostSelectionIn,
    OtherCostUpdate,
    ProductPriceUpdateOut,
    QuotationCalculateIn,
    QuotationCalculateOut,
    QuotationCreateIn,
    QuotationOut,
    QuotationSummaryOut,
    QuotationUpdateIn,
    TechniqueCalculationOut,
    TechniqueCreate,
    TechniqueSelectionIn,
    TechniqueUpdate,
)
from app.schemas.recipes import RecipeCalculateIn
from app.services.audit import AuditRecorder
from app.services.recipes import RecipeService
from app.services.sequences import SequenceService

ZERO = Decimal(0)
QUOTATION_ENTITY = "quotation"


class QuotationError(APIError):
    status_code = 422
    code = "QUOTATION_INVALID"
    message = "La cotizacion no es valida"


class QuotationNotFoundError(APIError):
    status_code = 404
    code = "QUOTATION_NOT_FOUND"
    message = "La cotizacion no existe"


class QuotationNotEditableError(APIError):
    status_code = 409
    code = "QUOTATION_NOT_EDITABLE"
    message = "Solo una cotizacion en borrador se puede editar"


class QuotationNotConfirmableError(APIError):
    status_code = 409
    code = "QUOTATION_NOT_CONFIRMABLE"
    message = "La cotizacion no se puede confirmar en su estado actual"


class QuotationSourceChangedError(APIError):
    status_code = 409
    code = "SOURCE_CHANGED"
    message = "Una fuente de costos cambio; acepte recalcular antes de continuar"


class QuotationMasterConflictError(APIError):
    status_code = 409
    code = "QUOTATION_MASTER_CONFLICT"
    message = "El codigo o nombre activo ya existe"


class QuotationMasterNotFoundError(APIError):
    status_code = 404
    code = "QUOTATION_MASTER_NOT_FOUND"
    message = "El registro maestro no existe"


class RecipeRequiredError(APIError):
    status_code = 409
    code = "RECIPE_REQUIRED"
    message = "Se necesita una version de receta aplicable para confirmar"


class MaterialGramsRequiredError(APIError):
    status_code = 409
    code = "MATERIAL_GRAMS_PER_PIECE_REQUIRED"
    message = "Indique cuantos gramos de receta lleva una pieza para confirmar"


class FiringLineRequiredError(APIError):
    status_code = 409
    code = "FIRING_LINE_REQUIRED"
    message = "Se necesita una linea de quema confirmada para confirmar"


class ProductPriceUpdateError(APIError):
    status_code = 409
    code = "PRODUCT_PRICE_UPDATE_NOT_ALLOWED"
    message = "El precio solo se actualiza desde una cotizacion confirmada"


@dataclass(slots=True)
class _ResolvedCalculation:
    output: QuotationCalculateOut
    techniques: dict[int, Technique]
    additionals: dict[int, Additional]
    other_costs: dict[int, OtherCost]
    technique_inputs: list[TechniqueSelectionIn]
    additional_inputs: list[AdditionalSelectionIn]
    other_cost_inputs: list[OtherCostSelectionIn]


@dataclass(frozen=True, slots=True)
class FiringEstimateOverride:
    """Costo de quema simulado y trazable para consumidores internos."""

    cost: Decimal
    snapshot: dict[str, object]
    source_key: object


MasterT = TypeVar("MasterT", Technique, Additional, OtherCost)


class QuotationService:
    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRecorder,
        sequences: SequenceService,
        recipes: RecipeService,
    ) -> None:
        self._session = session
        self._audit = audit
        self._sequences = sequences
        self._recipes = recipes

    # -- Maestros ----------------------------------------------------------
    async def _flush_master(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise QuotationMasterConflictError() from exc

    async def _list_master(
        self,
        model: type[MasterT],
        *,
        search: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[MasterT], int]:
        stmt = select(model)
        if search:
            term = f"%{search.strip()}%"
            stmt = stmt.where(or_(model.code.ilike(term), model.name.ilike(term)))
        if active is not None:
            stmt = stmt.where(model.active.is_(active))
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        rows = list(
            (
                await self._session.execute(
                    stmt.order_by(model.name.asc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    async def list_techniques(
        self,
        *,
        search: str | None = None,
        active: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Technique], int]:
        return await self._list_master(
            Technique, search=search, active=active, limit=limit, offset=offset
        )

    async def list_additionals(
        self,
        *,
        search: str | None = None,
        active: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Additional], int]:
        return await self._list_master(
            Additional, search=search, active=active, limit=limit, offset=offset
        )

    async def list_other_costs(
        self,
        *,
        search: str | None = None,
        active: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[OtherCost], int]:
        return await self._list_master(
            OtherCost, search=search, active=active, limit=limit, offset=offset
        )

    def _audit_master(self, row: MasterT, action: AuditAction, user: AuthenticatedUser) -> None:
        self._audit.record_action(
            entity_type=row.__tablename__,
            entity_id=str(row.id),
            action=action,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"code": row.code, "name": row.name, "active": row.active},
        )

    async def create_technique(
        self, payload: TechniqueCreate, user: AuthenticatedUser
    ) -> Technique:
        row = Technique(**payload.model_dump())
        self._session.add(row)
        await self._flush_master()
        self._audit_master(row, AuditAction.CREATE, user)
        return row

    async def update_technique(
        self, row_id: int, payload: TechniqueUpdate, user: AuthenticatedUser
    ) -> Technique:
        row = await self._get_master(Technique, row_id, for_update=True)
        for field, value in payload.model_dump().items():
            setattr(row, field, value)
        await self._flush_master()
        self._audit_master(row, AuditAction.UPDATE, user)
        return row

    async def create_additional(
        self, payload: AdditionalCreate, user: AuthenticatedUser
    ) -> Additional:
        row = Additional(**payload.model_dump())
        self._session.add(row)
        await self._flush_master()
        self._audit_master(row, AuditAction.CREATE, user)
        return row

    async def update_additional(
        self, row_id: int, payload: AdditionalUpdate, user: AuthenticatedUser
    ) -> Additional:
        row = await self._get_master(Additional, row_id, for_update=True)
        for field, value in payload.model_dump().items():
            setattr(row, field, value)
        await self._flush_master()
        self._audit_master(row, AuditAction.UPDATE, user)
        return row

    async def create_other_cost(
        self, payload: OtherCostCreate, user: AuthenticatedUser
    ) -> OtherCost:
        row = OtherCost(**payload.model_dump())
        self._session.add(row)
        await self._flush_master()
        self._audit_master(row, AuditAction.CREATE, user)
        return row

    async def update_other_cost(
        self, row_id: int, payload: OtherCostUpdate, user: AuthenticatedUser
    ) -> OtherCost:
        row = await self._get_master(OtherCost, row_id, for_update=True)
        for field, value in payload.model_dump().items():
            setattr(row, field, value)
        await self._flush_master()
        self._audit_master(row, AuditAction.UPDATE, user)
        return row

    async def _get_master(
        self, model: type[MasterT], row_id: int, *, for_update: bool = False
    ) -> MasterT:
        stmt = select(model).where(model.id == row_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise QuotationMasterNotFoundError()
        return row

    # -- Resolucion de fuentes --------------------------------------------
    async def _product(
        self,
        product_id: int,
        *,
        for_update: bool = False,
        require_active: bool = True,
    ) -> Product:
        stmt = select(Product).where(Product.id == product_id)
        if for_update:
            stmt = stmt.with_for_update()
        product = (await self._session.execute(stmt)).scalar_one_or_none()
        if product is None or (require_active and not product.active):
            raise QuotationError("El producto no existe o esta archivado", code="PRODUCT_INVALID")
        if product.product_type is not ProductType.FINISHED_PRODUCT:
            raise QuotationError(
                "El cotizador solo admite productos terminados",
                code="FINISHED_PRODUCT_REQUIRED",
            )
        return product

    async def _customer(
        self,
        customer_id: int | None,
        *,
        for_update: bool = False,
        require_active: bool = True,
    ) -> Partner | None:
        if customer_id is None:
            return None
        stmt = select(Partner).where(Partner.id == customer_id)
        if for_update:
            stmt = stmt.with_for_update()
        customer = (await self._session.execute(stmt)).scalar_one_or_none()
        if customer is None or (require_active and not customer.active):
            raise QuotationError("El cliente no existe o esta archivado", code="CUSTOMER_INVALID")
        if customer.role not in (PartnerRole.CLIENT, PartnerRole.BOTH):
            raise QuotationError(
                "El tercero seleccionado no tiene rol de cliente",
                code="CUSTOMER_ROLE_REQUIRED",
            )
        return customer

    async def _commercial(self) -> CommercialSettings:
        settings = (
            await self._session.execute(
                select(CommercialSettings).where(CommercialSettings.id == SINGLETON_ID)
            )
        ).scalar_one_or_none()
        if settings is None:
            raise QuotationError("La configuracion comercial no esta inicializada")
        return settings

    async def resolve_product(
        self,
        product_id: int,
        *,
        for_update: bool = False,
        require_active: bool = True,
    ) -> Product:
        """Expone la validacion canonica de producto a orquestadores internos."""

        return await self._product(
            product_id,
            for_update=for_update,
            require_active=require_active,
        )

    async def resolve_customer(
        self,
        customer_id: int | None,
        *,
        require_active: bool = True,
    ) -> Partner | None:
        """Expone la validacion canonica de cliente a orquestadores internos."""

        return await self._customer(customer_id, require_active=require_active)

    async def commercial_settings(self) -> CommercialSettings:
        """Devuelve la configuracion comercial unica ya validada."""

        return await self._commercial()

    async def _recipe_materials(
        self, payload: QuotationCalculateIn, product: Product
    ) -> tuple[Decimal, RecipeVersion | None, list[str], list[str]]:
        """Costo de materiales, version usada, avisos y materiales sin precio."""
        if payload.recipe_id is None or payload.recipe_version_id is None:
            return ZERO, None, ["RECIPE_REQUIRED"], []
        version = (
            await self._session.execute(
                select(RecipeVersion)
                .join(Recipe, Recipe.id == RecipeVersion.recipe_id)
                .where(RecipeVersion.id == payload.recipe_version_id)
                .options(selectinload(RecipeVersion.recipe))
            )
        ).scalar_one_or_none()
        # La receta **no** tiene por que pertenecer al producto cotizado. En el
        # taller las recetas son de materiales preparados —pastas, barnices,
        # esmaltes— y una pieza terminada no tiene formula propia: lo que se
        # cotiza es el material con el que se hace. Exigir que coincidieran
        # dejaba el selector vacio para cualquier pieza.
        if (
            version is None
            or version.recipe_id != payload.recipe_id
            or version.status is not RecipeStatus.ACTIVE
            or not version.recipe.active
        ):
            raise QuotationError(
                "La version de receta indicada no existe o no esta activa",
                code="RECIPE_VERSION_INVALID",
            )
        # La receta se cotiza en gramos y la cotizacion cuenta piezas. Sin saber
        # cuantos gramos lleva una pieza no hay costo que calcular: se avisa y se
        # devuelve cero, y confirmar queda bloqueado.
        por_pieza = self._grams_per_piece(payload)
        if por_pieza is None or por_pieza <= ZERO:
            return ZERO, version, ["MATERIAL_GRAMS_PER_PIECE_REQUIRED"], []
        gramos = por_pieza * Decimal(payload.quantity)
        result = await self._recipes.calculate(
            RecipeCalculateIn(
                recipe_id=payload.recipe_id,
                recipe_version_id=payload.recipe_version_id,
                target_base_quantity=gramos,
                target_uom="g",
            )
        )

        # Un componente sin costo no rompe el calculo: suma cero y el material
        # sale barato sin que nada lo diga. Se avisa con nombre y referencia,
        # porque el usuario no tiene forma de adivinar cual falta.
        sin_precio = [
            f"{line.component_internal_reference} · {line.component_name}"
            for line in result.components
            if line.unit_cost_in_grams <= ZERO
        ]
        avisos = ["MATERIAL_WITHOUT_COST"] if sin_precio else []
        return result.total_material_cost, version, avisos, sin_precio

    @staticmethod
    def _resolve_tax(
        product: Product, settings: CommercialSettings
    ) -> tuple[Decimal, Literal["PRODUCT", "COMMERCIAL_SETTINGS"]]:
        """Tasa de IGV aplicable y de donde sale.

        Manda la del producto cuando la define, y «define» incluye el cero: un
        producto exento declara `0`, y tratarlo como ausencia le aplicaria el
        18 % de la configuracion. Por eso se compara contra ``None`` y no por
        veracidad.

        Ambas se guardan ya en porcentaje (18 = 18 %), la convencion unica del
        proyecto.
        """
        if product.sale_tax_rate is not None:
            return product.sale_tax_rate, "PRODUCT"
        # Tambien aqui se compara contra None y no por veracidad: si la
        # configuracion declara 0, esa es la tasa, no una ausencia.
        tasa = settings.tax_percent if settings.tax_percent is not None else ZERO
        return tasa, "COMMERCIAL_SETTINGS"

    @staticmethod
    def _grams_per_piece(payload: QuotationCalculateIn) -> Decimal | None:
        """Gramos de receta por pieza, o ``None`` si todavia no se han indicado.

        **No** hay valor por omision. Suponer un gramo convertiria un dato que
        falta en un costo de materiales creible pero falso, y nadie lo notaria:
        una cotizacion de 450 g/pieza recalculada a 1 g/pieza sigue pareciendo
        correcta hasta que alguien compara los importes.
        """
        return payload.material_grams_per_piece

    async def _firing_source(
        self, payload: QuotationCalculateIn, product: Product
    ) -> tuple[FiringLine | None, Decimal, dict[str, object], list[str]]:
        if payload.firing_line_id is None:
            return None, ZERO, {}, ["FIRING_LINE_REQUIRED"]
        line = (
            await self._session.execute(
                select(FiringLine)
                .where(FiringLine.id == payload.firing_line_id)
                .options(selectinload(FiringLine.firing).selectinload(Firing.sessions))
            )
        ).scalar_one_or_none()
        if line is None or line.firing.status is not FiringStatus.CONFIRMED:
            raise QuotationError(
                "La linea debe pertenecer a una quema confirmada",
                code="CONFIRMED_FIRING_LINE_REQUIRED",
            )
        if line.product_id != product.id:
            raise QuotationError(
                "La linea de quema no corresponde al producto",
                code="FIRING_LINE_PRODUCT_MISMATCH",
            )
        sessions = {item.id: item for item in line.firing.sessions}
        snapshot_sessions = []
        for session_id in (line.low_session_id, line.high_session_id):
            session = sessions.get(session_id) if session_id is not None else None
            if session is not None:
                snapshot_sessions.append(
                    {
                        "id": session.id,
                        "kiln_id": session.kiln_id,
                        "firing_type": session.firing_type.value,
                        "rate_snapshot": str(session.rate_snapshot),
                        "capacity_snapshot": str(session.capacity_snapshot),
                    }
                )
        snapshot: dict[str, object] = {
            "firing_id": line.firing_id,
            "firing_line_id": line.id,
            "firing_code": line.firing.code,
            "product_id": line.product_id,
            "description": line.description,
            "quantity": line.quantity,
            "total_volume_cm3": str(line.total_volume_cm3),
            "base_cost": str(line.base_cost),
            "allocated_cost": str(line.allocated_cost),
            "occupancy_factor": str(line.occupancy_factor),
            "sessions": snapshot_sessions,
        }
        return line, line.allocated_cost, snapshot, []

    async def _load_sources(self, model: type[MasterT], ids: list[int]) -> dict[int, MasterT]:
        if not ids:
            return {}
        rows = list(
            (await self._session.execute(select(model).where(model.id.in_(set(ids)))))
            .scalars()
            .all()
        )
        found = {row.id: row for row in rows}
        missing = set(ids) - found.keys()
        if missing:
            raise QuotationMasterNotFoundError(f"Maestros inexistentes: {sorted(missing)}")
        inactive = [row.name for row in rows if not row.active]
        if inactive:
            raise QuotationError(
                "Hay maestros archivados en la cotizacion: " + ", ".join(inactive),
                code="QUOTATION_MASTER_INACTIVE",
            )
        return found

    @staticmethod
    def _formula_explanation(formula: AdditionalFormulaType, factor: Decimal | None) -> str:
        """Explica la formula en el lenguaje del taller, no en el del contrato.

        El factor se imprime sin la cola de ceros de la columna NUMERIC: quien
        lee la cotizacion espera «cada 50 piezas», no «cada 50.000000 piezas».
        """
        if formula is AdditionalFormulaType.SIMPLE_QUANTITY:
            return "Una unidad por cada cantidad indicada"
        cada = format(factor.normalize(), "f") if factor is not None else "?"
        cadencia = f"1 aplicacion cada {cada} piezas"
        if formula is AdditionalFormulaType.PIECE_X_ADDITIONAL:
            return f"{cadencia}, multiplicado por la cantidad indicada"
        return cadencia

    @staticmethod
    def _source_hash(data: dict[str, object]) -> str:
        serialized = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    async def _calculate(
        self,
        payload: QuotationCalculateIn,
        *,
        firing_override: FiringEstimateOverride | None = None,
    ) -> _ResolvedCalculation:
        product = await self._product(payload.product_id)
        customer = await self._customer(payload.customer_id)
        settings = await self._commercial()
        (
            materials_calculated,
            recipe_version,
            recipe_warnings,
            materials_without_cost,
        ) = await self._recipe_materials(payload, product)
        if firing_override is None:
            firing_line, firing_cost, firing_snapshot, firing_warnings = await self._firing_source(
                payload, product
            )
            firing_source_key: object = [
                firing_line.id if firing_line else None,
                firing_line.updated_at if firing_line else None,
                firing_line.firing.updated_at if firing_line else None,
            ]
        else:
            firing_line = None
            firing_cost = firing_override.cost
            firing_snapshot = firing_override.snapshot
            firing_warnings = []
            firing_source_key = firing_override.source_key

        technique_inputs = list(payload.techniques)
        additional_inputs = list(payload.additionals)
        technique_rows = await self._load_sources(
            Technique, [item.technique_id for item in technique_inputs]
        )
        additional_rows = await self._load_sources(
            Additional, [item.additional_id for item in additional_inputs]
        )

        if payload.other_costs is None:
            active_other = list(
                (
                    await self._session.execute(
                        select(OtherCost).where(OtherCost.active.is_(True)).order_by(OtherCost.id)
                    )
                )
                .scalars()
                .all()
            )
            other_inputs = [OtherCostSelectionIn(other_cost_id=row.id) for row in active_other]
            other_rows = {row.id: row for row in active_other}
        else:
            other_inputs = list(payload.other_costs)
            other_rows = await self._load_sources(
                OtherCost, [item.other_cost_id for item in other_inputs]
            )

        core_techniques = tuple(
            TechniqueInput(
                reference_id=item.technique_id,
                name=technique_rows[item.technique_id].name,
                unit_price=item.unit_price
                if item.unit_price is not None
                else technique_rows[item.technique_id].unit_price,
                formula_type=CoreTechniqueFormulaType(
                    technique_rows[item.technique_id].formula_type.value
                ),
                factor_1=item.factor_1
                if item.factor_1 is not None
                else technique_rows[item.technique_id].factor_1,
                factor_2=item.factor_2
                if item.factor_2 is not None
                else technique_rows[item.technique_id].factor_2,
                quantity=item.quantity,
                applied_cost=item.applied_cost,
                applied_days=item.applied_days,
            )
            for item in technique_inputs
        )
        core_additionals = tuple(
            AdditionalInput(
                reference_id=item.additional_id,
                name=additional_rows[item.additional_id].name,
                unit_price=item.unit_price
                if item.unit_price is not None
                else additional_rows[item.additional_id].unit_price,
                formula_type=CoreAdditionalFormulaType(
                    additional_rows[item.additional_id].formula_type.value
                ),
                factor_1=item.factor_1
                if item.factor_1 is not None
                else additional_rows[item.additional_id].factor_1,
                total_quantity=payload.quantity,
                additional_quantity=item.additional_quantity,
                applied_cost=item.applied_cost,
            )
            for item in additional_inputs
        )
        core_other = tuple(
            OtherCostInput(
                reference_id=item.other_cost_id,
                name=other_rows[item.other_cost_id].name,
                unit_price=item.unit_price
                if item.unit_price is not None
                else other_rows[item.other_cost_id].unit_price,
                calculation_type=CoreOtherCostCalculationType(
                    other_rows[item.other_cost_id].calculation_type.value
                ),
            )
            for item in other_inputs
        )

        tasa_igv, origen_igv = self._resolve_tax(product, settings)
        default_factor = settings.default_quotation_factor
        factor = payload.commercial_factor or default_factor
        materials_applied = (
            payload.materials_applied
            if payload.materials_applied is not None
            else materials_calculated
        )
        try:
            result = calculate_quotation(
                QuotationInput(
                    quantity=payload.quantity,
                    materials_calculated=materials_calculated,
                    materials_applied=materials_applied,
                    firing_cost=firing_cost,
                    techniques=core_techniques,
                    additionals=core_additionals,
                    days_adjustment=payload.days_adjustment,
                    waiting_days=payload.waiting_days,
                    other_costs=core_other,
                    commercial_factor=factor,
                    markup_percent=payload.markup_percent,
                    manual_commercial_unit_price=payload.commercial_sale_unit_price,
                    tax_percentage=tasa_igv,
                )
            )
        except QuotationCalculationError as exc:
            raise QuotationError(str(exc)) from exc

        warnings = [
            *recipe_warnings,
            *firing_warnings,
            # El IGV ya tiene regla: la cotizacion es neta y el impuesto se anade
            # encima. Solo se avisa cuando no hay tasa en ningun sitio, y se
            # compara contra None: un 0 configurado a proposito es una tasa
            # —exento— y avisar de que «falta» seria mentir.
            *(
                []
                if product.sale_tax_rate is not None or settings.tax_percent is not None
                else ["IGV_RATE_NOT_CONFIGURED"]
            ),
            "DISCOUNT_RULE_BLOCKED_BY_SOURCE",
        ]
        source_data: dict[str, object] = {
            "customer": [customer.id, customer.updated_at] if customer else None,
            "product": [product.id, product.updated_at],
            "settings": [settings.id, settings.version, settings.updated_at],
            "recipe": [
                recipe_version.id if recipe_version else None,
                recipe_version.fingerprint if recipe_version else None,
                recipe_version.updated_at if recipe_version else None,
            ],
            "firing": firing_source_key,
            "techniques": sorted((row.id, row.updated_at) for row in technique_rows.values()),
            "additionals": sorted((row.id, row.updated_at) for row in additional_rows.values()),
            "other_costs": sorted((row.id, row.updated_at) for row in other_rows.values()),
        }
        fingerprint = self._source_hash(source_data)

        technique_out = [
            TechniqueCalculationOut(
                technique_id=calc.source.reference_id,
                name_snapshot=calc.source.name,
                unit_price_snapshot=calc.source.unit_price,
                formula_type_snapshot=technique_rows[calc.source.reference_id].formula_type,
                factor_1_snapshot=calc.source.factor_1,
                factor_2_snapshot=calc.source.factor_2,
                quantity=calc.source.quantity,
                proposed_cost=calc.proposed_cost,
                applied_cost=calc.applied_cost,
                proposed_days=calc.proposed_days,
                applied_days=calc.applied_days,
                adjusted=technique_inputs[index].unit_price is not None
                or technique_inputs[index].factor_1 is not None
                or technique_inputs[index].factor_2 is not None
                or calc.proposed_cost != calc.applied_cost
                or calc.proposed_days != calc.applied_days,
                sort_order=technique_inputs[index].sort_order,
            )
            for index, calc in enumerate(result.techniques)
        ]
        additional_out = [
            AdditionalCalculationOut(
                additional_id=calc.source.reference_id,
                name_snapshot=calc.source.name,
                unit_price_snapshot=calc.source.unit_price,
                formula_type_snapshot=additional_rows[calc.source.reference_id].formula_type,
                factor_1_snapshot=calc.source.factor_1,
                additional_quantity=calc.source.additional_quantity,
                proposed_cost=calc.proposed_cost,
                applied_cost=calc.applied_cost,
                adjusted=additional_inputs[index].unit_price is not None
                or additional_inputs[index].factor_1 is not None
                or calc.proposed_cost != calc.applied_cost,
                formula_explanation=self._formula_explanation(
                    additional_rows[calc.source.reference_id].formula_type,
                    calc.source.factor_1,
                ),
                sort_order=additional_inputs[index].sort_order,
            )
            for index, calc in enumerate(result.additionals)
        ]
        other_out = [
            OtherCostCalculationOut(
                other_cost_id=calc.source.reference_id,
                name_snapshot=calc.source.name,
                unit_price_snapshot=calc.source.unit_price,
                calculation_type_snapshot=other_rows[calc.source.reference_id].calculation_type,
                proposed_cost=calc.proposed_cost,
                applied_cost=calc.applied_cost,
                adjusted=other_inputs[index].unit_price is not None,
                sort_order=other_inputs[index].sort_order,
            )
            for index, calc in enumerate(result.other_costs)
        ]
        por_pieza = self._grams_per_piece(payload)
        currency_code_snap = settings.currency_code or "PEN"
        currency_symbol_snap = settings.currency_symbol or "S/"
        output = QuotationCalculateOut(
            name=payload.name,
            customer_id=customer.id if customer else None,
            customer_name_snapshot=customer.name if customer else None,
            customer_trade_name_snapshot=(customer.reference or customer.name)
            if customer
            else None,
            customer_document_type_snapshot=customer.document_type.value
            if (customer and customer.document_type)
            else None,
            customer_document_number_snapshot=customer.document_number if customer else None,
            customer_address_snapshot=customer.address if customer else None,
            customer_ubigeo_snapshot=(customer.district or customer.ubigeo_code)
            if customer
            else None,
            customer_email_snapshot=customer.email if customer else None,
            customer_phone_snapshot=(customer.phone or customer.mobile) if customer else None,
            product_id=product.id,
            product_internal_reference=product.internal_reference,
            product_name=product.name,
            product_name_snapshot=product.name,
            product_internal_reference_snapshot=product.internal_reference,
            product_type_snapshot=product.product_type.value,
            product_uom_snapshot=product.base_uom_code,
            product_material_snapshot=product.material,
            product_grammage_snapshot=product.grammage,
            product_width_snapshot=product.width,
            product_height_snapshot=product.height,
            product_length_snapshot=product.length,
            product_depth_snapshot=product.depth,
            quantity=payload.quantity,
            recipe_id=recipe_version.recipe_id if recipe_version else None,
            recipe_version_id=recipe_version.id if recipe_version else None,
            recipe_version_fingerprint_snapshot=recipe_version.fingerprint
            if recipe_version
            else None,
            firing_id=firing_line.firing_id if firing_line else None,
            firing_line_id=firing_line.id if firing_line else None,
            firing_code_snapshot=firing_line.firing.code if firing_line else None,
            firing_snapshot=firing_snapshot,
            materials_calculated=result.materials_calculated,
            materials_applied=result.materials_applied,
            firing_cost=result.firing_cost,
            labor_cost=result.labor_cost,
            calculated_days=result.calculated_days,
            days_adjustment=result.days_adjustment,
            waiting_days=result.waiting_days,
            total_days=result.total_days,
            space_cost=result.space_cost,
            commercial_factor_default_snapshot=default_factor,
            commercial_factor=result.commercial_factor,
            current_sale_price_snapshot=product.sale_price,
            base_commercial_cost=result.base_commercial_cost,
            calculated_total=result.calculated_total,
            calculated_unit_price=result.calculated_unit_price,
            final_unit_cost=result.final_unit_cost,
            final_total_cost=result.final_total_cost,
            markup_percent=result.markup_percent,
            target_profit_unit=result.target_profit_unit,
            calculated_sale_unit_price=result.calculated_sale_unit_price,
            suggested_commercial_unit_price=result.suggested_commercial_unit_price,
            commercial_sale_unit_price=result.commercial_sale_unit_price,
            effective_profit_unit=result.effective_profit_unit,
            effective_profit_total=result.effective_profit_total,
            effective_markup_percent=result.effective_markup_percent,
            commercial_subtotal=result.commercial_subtotal,
            commercial_total=result.commercial_total,
            commercial_unit_price_with_tax=result.commercial_unit_price_with_tax,
            currency_code_snapshot=currency_code_snap,
            currency_symbol_snapshot=currency_symbol_snap,
            materials_without_cost=materials_without_cost,
            material_grams_per_piece=por_pieza,
            material_total_grams=(
                por_pieza * Decimal(payload.quantity) if por_pieza is not None else None
            ),
            tax_percentage=result.tax_percentage,
            tax_rate_source=origen_igv,
            tax_amount=result.tax_amount,
            total_with_tax=result.total_with_tax,
            unit_price_with_tax=result.unit_price_with_tax,
            source_fingerprint=fingerprint,
            warnings=warnings,
            techniques=technique_out,
            additionals=additional_out,
            other_costs=other_out,
        )
        return _ResolvedCalculation(
            output=output,
            techniques=technique_rows,
            additionals=additional_rows,
            other_costs=other_rows,
            technique_inputs=technique_inputs,
            additional_inputs=additional_inputs,
            other_cost_inputs=other_inputs,
        )

    async def calculate(self, payload: QuotationCalculateIn) -> QuotationCalculateOut:
        """Simula sin insertar, emitir correlativo, consumir stock ni cambiar precios."""
        return (await self._calculate(payload)).output

    async def calculate_with_firing_estimate(
        self,
        payload: QuotationCalculateIn,
        estimate: FiringEstimateOverride,
    ) -> QuotationCalculateOut:
        """Cotiza usando una quema simulada sin crear un evento productivo."""

        return (await self._calculate(payload, firing_override=estimate)).output

    @staticmethod
    def _identity(payload: QuotationCalculateIn) -> tuple[object, ...]:
        return (
            payload.product_id,
            payload.recipe_id,
            payload.recipe_version_id,
            payload.firing_line_id,
            tuple(sorted(item.technique_id for item in payload.techniques)),
            tuple(sorted(item.additional_id for item in payload.additionals)),
            None
            if payload.other_costs is None
            else tuple(sorted(item.other_cost_id for item in payload.other_costs)),
        )

    # -- Persistencia de cotizaciones -------------------------------------
    async def _get(self, quotation_id: int, *, for_update: bool = False) -> Quotation:
        stmt = (
            select(Quotation)
            .where(
                Quotation.id == quotation_id,
                Quotation.workflow == QuotationWorkflow.LEGACY,
            )
            .options(
                selectinload(Quotation.techniques),
                selectinload(Quotation.additionals),
                selectinload(Quotation.other_costs),
            )
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        quotation = (await self._session.execute(stmt)).scalar_one_or_none()
        if quotation is None:
            raise QuotationNotFoundError()
        return quotation

    @staticmethod
    def _to_capture(quotation: Quotation) -> QuotationCalculateIn:
        if quotation.product_id is None or quotation.quantity is None:
            raise QuotationNotFoundError()
        return QuotationCalculateIn(
            name=quotation.name,
            customer_id=quotation.customer_id,
            product_id=quotation.product_id,
            quantity=quotation.quantity,
            recipe_id=quotation.recipe_id,
            recipe_version_id=quotation.recipe_version_id,
            firing_line_id=quotation.firing_line_id,
            materials_applied=quotation.materials_applied,
            # Sin esto, confirmar recalculaba con los gramos por omision y
            # convertia una hoja de 450 g/pieza en otra de 1 g/pieza.
            material_grams_per_piece=quotation.material_grams_per_piece,
            techniques=[
                TechniqueSelectionIn(
                    technique_id=line.technique_id,
                    quantity=line.quantity,
                    unit_price=line.unit_price_snapshot if line.unit_price_overridden else None,
                    factor_1=line.factor_1_snapshot if line.factors_overridden else None,
                    factor_2=line.factor_2_snapshot if line.factors_overridden else None,
                    applied_cost=line.applied_cost if line.applied_cost_overridden else None,
                    applied_days=line.applied_days if line.applied_days_overridden else None,
                    sort_order=line.sort_order,
                )
                for line in quotation.techniques
            ],
            additionals=[
                AdditionalSelectionIn(
                    additional_id=line.additional_id,
                    additional_quantity=line.additional_quantity,
                    unit_price=line.unit_price_snapshot if line.unit_price_overridden else None,
                    factor_1=line.factor_1_snapshot if line.factor_overridden else None,
                    applied_cost=line.applied_cost if line.applied_cost_overridden else None,
                    sort_order=line.sort_order,
                )
                for line in quotation.additionals
            ],
            days_adjustment=quotation.days_adjustment,
            waiting_days=quotation.waiting_days,
            other_costs=[
                OtherCostSelectionIn(
                    other_cost_id=line.other_cost_id,
                    unit_price=line.unit_price_snapshot if line.unit_price_overridden else None,
                    sort_order=line.sort_order,
                )
                for line in quotation.other_costs
            ],
            commercial_factor=quotation.commercial_factor,
            markup_percent=quotation.markup_percent,
            commercial_sale_unit_price=quotation.commercial_sale_unit_price,
        )

    async def _apply(
        self,
        quotation: Quotation,
        payload: QuotationCalculateIn,
        resolved: _ResolvedCalculation | None = None,
    ) -> QuotationCalculateOut:
        calculation = resolved or await self._calculate(payload)
        output = calculation.output

        quotation.name = output.name
        quotation.customer_id = output.customer_id
        quotation.customer_name_snapshot = output.customer_name_snapshot
        quotation.customer_trade_name_snapshot = output.customer_trade_name_snapshot
        quotation.customer_document_type_snapshot = output.customer_document_type_snapshot
        quotation.customer_document_number_snapshot = output.customer_document_number_snapshot
        quotation.customer_address_snapshot = output.customer_address_snapshot
        quotation.customer_ubigeo_snapshot = output.customer_ubigeo_snapshot
        quotation.customer_email_snapshot = output.customer_email_snapshot
        quotation.customer_phone_snapshot = output.customer_phone_snapshot

        quotation.product_id = output.product_id
        quotation.product_name_snapshot = output.product_name_snapshot
        quotation.product_internal_reference_snapshot = output.product_internal_reference_snapshot
        quotation.product_type_snapshot = output.product_type_snapshot
        quotation.product_uom_snapshot = output.product_uom_snapshot
        quotation.product_material_snapshot = output.product_material_snapshot
        quotation.product_grammage_snapshot = output.product_grammage_snapshot
        quotation.product_width_snapshot = output.product_width_snapshot
        quotation.product_height_snapshot = output.product_height_snapshot
        quotation.product_length_snapshot = output.product_length_snapshot
        quotation.product_depth_snapshot = output.product_depth_snapshot

        quotation.quantity = output.quantity
        quotation.recipe_id = output.recipe_id
        quotation.recipe_version_id = output.recipe_version_id
        quotation.recipe_version_fingerprint_snapshot = output.recipe_version_fingerprint_snapshot
        quotation.firing_id = output.firing_id
        quotation.firing_line_id = output.firing_line_id
        quotation.firing_code_snapshot = output.firing_code_snapshot
        quotation.firing_snapshot = output.firing_snapshot
        quotation.materials_calculated = output.materials_calculated
        quotation.materials_applied = output.materials_applied
        quotation.firing_cost = output.firing_cost
        quotation.labor_cost = output.labor_cost
        quotation.calculated_days = output.calculated_days
        quotation.days_adjustment = output.days_adjustment
        quotation.waiting_days = output.waiting_days
        quotation.total_days = output.total_days
        quotation.space_cost = output.space_cost
        quotation.commercial_factor_default_snapshot = output.commercial_factor_default_snapshot
        quotation.commercial_factor = output.commercial_factor
        quotation.current_sale_price_snapshot = output.current_sale_price_snapshot
        quotation.base_commercial_cost = output.base_commercial_cost
        quotation.calculated_total = output.calculated_total
        quotation.calculated_unit_price = output.calculated_unit_price
        quotation.final_unit_cost = output.final_unit_cost
        quotation.final_total_cost = output.final_total_cost
        quotation.markup_percent = output.markup_percent
        quotation.target_profit_unit = output.target_profit_unit
        quotation.calculated_sale_unit_price = output.calculated_sale_unit_price
        quotation.suggested_commercial_unit_price = output.suggested_commercial_unit_price
        quotation.commercial_sale_unit_price = output.commercial_sale_unit_price
        quotation.effective_profit_unit = output.effective_profit_unit
        quotation.effective_profit_total = output.effective_profit_total
        quotation.effective_markup_percent = output.effective_markup_percent
        quotation.commercial_subtotal = output.commercial_subtotal
        quotation.commercial_total = output.commercial_total
        quotation.commercial_unit_price_with_tax = output.commercial_unit_price_with_tax
        quotation.currency_code_snapshot = output.currency_code_snapshot
        quotation.currency_symbol_snapshot = output.currency_symbol_snapshot
        quotation.material_grams_per_piece = output.material_grams_per_piece
        quotation.tax_percentage_snapshot = output.tax_percentage
        quotation.tax_rate_source_snapshot = output.tax_rate_source
        quotation.tax_amount = output.tax_amount
        quotation.total_with_tax = output.total_with_tax
        quotation.unit_price_with_tax = output.unit_price_with_tax
        quotation.source_fingerprint = output.source_fingerprint
        quotation.calculation_warnings = output.warnings

        await self._session.execute(
            delete(QuotationTechnique).where(QuotationTechnique.quotation_id == quotation.id)
        )
        await self._session.execute(
            delete(QuotationAdditional).where(QuotationAdditional.quotation_id == quotation.id)
        )
        await self._session.execute(
            delete(QuotationOtherCost).where(QuotationOtherCost.quotation_id == quotation.id)
        )
        await self._session.execute(
            delete(QuotationItem).where(QuotationItem.quotation_id == quotation.id)
        )
        await self._session.flush()

        for index, technique_line in enumerate(output.techniques):
            technique_source = calculation.techniques[technique_line.technique_id]
            technique_capture = calculation.technique_inputs[index]
            self._session.add(
                QuotationTechnique(
                    quotation_id=quotation.id,
                    technique_id=technique_line.technique_id,
                    name_snapshot=technique_line.name_snapshot,
                    unit_price_snapshot=technique_line.unit_price_snapshot,
                    formula_type_snapshot=technique_line.formula_type_snapshot,
                    factor_1_snapshot=technique_line.factor_1_snapshot,
                    factor_2_snapshot=technique_line.factor_2_snapshot,
                    source_updated_at_snapshot=technique_source.updated_at,
                    quantity=technique_line.quantity,
                    proposed_cost=technique_line.proposed_cost,
                    applied_cost=technique_line.applied_cost,
                    proposed_days=technique_line.proposed_days,
                    applied_days=technique_line.applied_days,
                    unit_price_overridden=technique_capture.unit_price is not None,
                    factors_overridden=technique_capture.factor_1 is not None
                    or technique_capture.factor_2 is not None,
                    applied_cost_overridden=technique_capture.applied_cost is not None,
                    applied_days_overridden=technique_capture.applied_days is not None,
                    sort_order=technique_line.sort_order,
                )
            )
        for index, additional_line in enumerate(output.additionals):
            additional_source = calculation.additionals[additional_line.additional_id]
            additional_capture = calculation.additional_inputs[index]
            self._session.add(
                QuotationAdditional(
                    quotation_id=quotation.id,
                    additional_id=additional_line.additional_id,
                    name_snapshot=additional_line.name_snapshot,
                    unit_price_snapshot=additional_line.unit_price_snapshot,
                    formula_type_snapshot=additional_line.formula_type_snapshot,
                    factor_1_snapshot=additional_line.factor_1_snapshot,
                    source_updated_at_snapshot=additional_source.updated_at,
                    additional_quantity=additional_line.additional_quantity,
                    proposed_cost=additional_line.proposed_cost,
                    applied_cost=additional_line.applied_cost,
                    unit_price_overridden=additional_capture.unit_price is not None,
                    factor_overridden=additional_capture.factor_1 is not None,
                    applied_cost_overridden=additional_capture.applied_cost is not None,
                    sort_order=additional_line.sort_order,
                )
            )
        for index, other_cost_line in enumerate(output.other_costs):
            other_cost_source = calculation.other_costs[other_cost_line.other_cost_id]
            other_cost_capture = calculation.other_cost_inputs[index]
            self._session.add(
                QuotationOtherCost(
                    quotation_id=quotation.id,
                    other_cost_id=other_cost_line.other_cost_id,
                    name_snapshot=other_cost_line.name_snapshot,
                    unit_price_snapshot=other_cost_line.unit_price_snapshot,
                    calculation_type_snapshot=other_cost_line.calculation_type_snapshot,
                    source_updated_at_snapshot=other_cost_source.updated_at,
                    proposed_cost=other_cost_line.proposed_cost,
                    applied_cost=other_cost_line.applied_cost,
                    unit_price_overridden=other_cost_capture.unit_price is not None,
                    sort_order=other_cost_line.sort_order,
                )
            )
        # La ruta legacy mantiene una sola linea sincronizada con su cabecera.
        # Asi los registros migrados por 0012 y los creados despues de la
        # migracion producen el mismo item_count y el PDF usa el snapshot
        # vigente tras editar o confirmar.
        self._session.add(
            QuotationItem(
                quotation_id=quotation.id,
                product_id=output.product_id,
                sort_order=0,
                quantity=output.quantity,
                product_name_snapshot=output.product_name_snapshot,
                product_internal_reference_snapshot=output.product_internal_reference_snapshot,
                product_type_snapshot=output.product_type_snapshot,
                product_uom_snapshot=output.product_uom_snapshot,
                product_material_snapshot=output.product_material_snapshot,
                product_grammage_snapshot=output.product_grammage_snapshot,
                product_width_snapshot=output.product_width_snapshot,
                product_height_snapshot=output.product_height_snapshot,
                product_length_snapshot=output.product_length_snapshot,
                product_depth_snapshot=output.product_depth_snapshot,
                recipe_id=output.recipe_id,
                recipe_version_id=output.recipe_version_id,
                recipe_version_fingerprint_snapshot=(output.recipe_version_fingerprint_snapshot),
                material_grams_per_piece=output.material_grams_per_piece,
                kiln_id=None,
                kiln_snapshot={},
                production_snapshot=output.firing_snapshot,
                techniques_snapshot=[value.model_dump(mode="json") for value in output.techniques],
                additionals_snapshot=[
                    value.model_dump(mode="json") for value in output.additionals
                ],
                other_costs_snapshot=[
                    value.model_dump(mode="json") for value in output.other_costs
                ],
                materials_calculated=output.materials_calculated,
                materials_applied=output.materials_applied,
                firing_cost=output.firing_cost,
                labor_cost=output.labor_cost,
                calculated_days=output.calculated_days,
                days_adjustment=output.days_adjustment,
                waiting_days=output.waiting_days,
                total_days=output.total_days,
                space_cost=output.space_cost,
                final_unit_cost=output.final_unit_cost,
                final_total_cost=output.final_total_cost,
                markup_percent=output.markup_percent,
                calculated_sale_unit_price=output.calculated_sale_unit_price,
                suggested_commercial_unit_price=output.suggested_commercial_unit_price,
                commercial_sale_unit_price=output.commercial_sale_unit_price,
                effective_profit_unit=output.effective_profit_unit,
                effective_profit_total=output.effective_profit_total,
                effective_markup_percent=output.effective_markup_percent,
                commercial_subtotal=output.commercial_subtotal,
                tax_percentage_snapshot=output.tax_percentage,
                tax_rate_source_snapshot=output.tax_rate_source,
                tax_amount=output.tax_amount,
                source_fingerprint=output.source_fingerprint,
                calculation_warnings=output.warnings,
            )
        )
        await self._session.flush()
        self._session.expire(quotation, ["techniques", "additionals", "other_costs", "items"])
        return output

    async def create(self, payload: QuotationCreateIn, *, user: AuthenticatedUser) -> QuotationOut:
        code = await self._sequences.issue(SequenceType.QUOTE, user_id=user.id)
        quotation = Quotation(
            code=code,
            name=payload.name,
            status=QuotationStatus.DRAFT,
            customer_id=payload.customer_id,
            product_id=payload.product_id,
            quantity=payload.quantity,
            currency_code_snapshot="PEN",
            currency_symbol_snapshot="S/",
            commercial_factor_default_snapshot=Decimal(1),
            commercial_factor=Decimal(1),
            source_fingerprint="0" * 64,
            created_by_id=user.id,
        )
        self._session.add(quotation)
        await self._session.flush()
        await self._apply(quotation, payload)
        self._audit.record_action(
            entity_type=QUOTATION_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"code": code, "status": QuotationStatus.DRAFT.value},
        )
        return await self.get(quotation.id)

    async def update(
        self, quotation_id: int, payload: QuotationUpdateIn, *, user: AuthenticatedUser
    ) -> QuotationOut:
        quotation = await self._get(quotation_id, for_update=True)
        if quotation.status is not QuotationStatus.DRAFT:
            raise QuotationNotEditableError()
        if payload.expected_source_fingerprint != quotation.source_fingerprint:
            raise QuotationSourceChangedError()

        previous_capture = self._to_capture(quotation)
        calculation = await self._calculate(payload)
        same_sources = self._identity(previous_capture) == self._identity(payload)
        if (
            same_sources
            and calculation.output.source_fingerprint != quotation.source_fingerprint
            and not payload.accept_source_changes
        ):
            raise QuotationSourceChangedError()

        previous = {
            "name": quotation.name,
            "customer_id": quotation.customer_id,
            "materials_applied": quotation.materials_applied,
            "days_adjustment": quotation.days_adjustment,
            "waiting_days": quotation.waiting_days,
            "commercial_factor": quotation.commercial_factor,
            "commercial_sale_unit_price": quotation.commercial_sale_unit_price,
            "calculated_total": quotation.calculated_total,
        }
        await self._apply(quotation, payload, calculation)
        current = {
            "name": quotation.name,
            "customer_id": quotation.customer_id,
            "materials_applied": quotation.materials_applied,
            "days_adjustment": quotation.days_adjustment,
            "waiting_days": quotation.waiting_days,
            "commercial_factor": quotation.commercial_factor,
            "commercial_sale_unit_price": quotation.commercial_sale_unit_price,
            "calculated_total": quotation.calculated_total,
        }
        changes = {
            field: (previous[field], current[field])
            for field in previous
            if previous[field] != current[field]
        }
        if changes:
            self._audit.record_changes(
                entity_type=QUOTATION_ENTITY,
                entity_id=str(quotation.id),
                changes=changes,
                user_id=user.id,
                user_display_name=user.display_name,
            )
        return await self.get(quotation.id)

    async def confirm(
        self,
        quotation_id: int,
        *,
        accept_source_changes: bool,
        user: AuthenticatedUser,
    ) -> QuotationOut:
        quotation = await self._get(quotation_id, for_update=True)
        if quotation.status is not QuotationStatus.DRAFT:
            raise QuotationNotConfirmableError()
        payload = self._to_capture(quotation)
        calculation = await self._calculate(payload)
        if (
            calculation.output.source_fingerprint != quotation.source_fingerprint
            and not accept_source_changes
        ):
            raise QuotationSourceChangedError()
        if "RECIPE_REQUIRED" in calculation.output.warnings:
            raise RecipeRequiredError()
        if "MATERIAL_GRAMS_PER_PIECE_REQUIRED" in calculation.output.warnings:
            raise MaterialGramsRequiredError()
        if "FIRING_LINE_REQUIRED" in calculation.output.warnings:
            raise FiringLineRequiredError()

        await self._apply(quotation, payload, calculation)
        quotation.status = QuotationStatus.CONFIRMED
        quotation.confirmed_at = datetime.now(UTC)
        await self._session.flush()
        self._audit.record_action(
            entity_type=QUOTATION_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": quotation.code,
                "status": QuotationStatus.CONFIRMED.value,
                "calculated_total": str(quotation.calculated_total),
            },
        )
        return await self.get(quotation.id)

    async def cancel(self, quotation_id: int, *, user: AuthenticatedUser) -> QuotationOut:
        quotation = await self._get(quotation_id, for_update=True)
        if quotation.status is QuotationStatus.CANCELLED:
            raise QuotationError("La cotizacion ya esta cancelada", code="ALREADY_CANCELLED")
        previous = quotation.status
        quotation.status = QuotationStatus.CANCELLED
        quotation.cancelled_at = datetime.now(UTC)
        await self._session.flush()
        self._audit.record_action(
            entity_type=QUOTATION_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": quotation.code,
                "status": QuotationStatus.CANCELLED.value,
                "previous_status": previous.value,
            },
        )
        return await self.get(quotation.id)

    async def duplicate(self, quotation_id: int, *, user: AuthenticatedUser) -> QuotationOut:
        original = await self._get(quotation_id)
        capture = self._to_capture(original)
        payload = QuotationCreateIn.model_validate(capture.model_dump())
        return await self.create(payload, user=user)

    async def update_product_price(
        self, quotation_id: int, *, user: AuthenticatedUser
    ) -> ProductPriceUpdateOut:
        quotation = await self._get(quotation_id, for_update=True)
        if quotation.status is not QuotationStatus.CONFIRMED:
            raise ProductPriceUpdateError()
        existing = (
            await self._session.execute(
                select(QuotationProductPriceUpdate).where(
                    QuotationProductPriceUpdate.quotation_id == quotation.id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ProductPriceUpdateError("Esta cotizacion ya actualizo el precio del producto")

        if quotation.product_id is None:
            raise ProductPriceUpdateError("La cotizacion no tiene un unico producto")
        product = await self._product(quotation.product_id, for_update=True)
        old_price = product.sale_price
        new_price = quotation.calculated_unit_price
        product.sale_price = new_price
        event = QuotationProductPriceUpdate(
            quotation_id=quotation.id,
            product_id=product.id,
            actor_id=user.id,
            old_price=old_price,
            new_price=new_price,
        )
        self._session.add(event)
        await self._session.flush()
        self._audit.record_changes(
            entity_type="product",
            entity_id=str(product.id),
            changes={"sale_price": (old_price, new_price)},
            user_id=user.id,
            user_display_name=user.display_name,
        )
        self._audit.record_action(
            entity_type=QUOTATION_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "product_id": product.id,
                "old_price": str(old_price) if old_price is not None else None,
                "new_price": str(new_price),
            },
        )
        return ProductPriceUpdateOut(
            quotation_id=quotation.id,
            product_id=product.id,
            old_price=old_price,
            new_price=new_price,
            updated_at=event.created_at,
        )

    # -- Lectura -----------------------------------------------------------
    async def _stored_output(self, quotation: Quotation, product: Product) -> QuotationCalculateOut:
        if quotation.quantity is None:
            raise QuotationNotFoundError()
        quantity = quotation.quantity
        gramos_guardados = quotation.material_grams_per_piece
        return QuotationCalculateOut(
            name=quotation.name,
            customer_id=quotation.customer_id,
            customer_name_snapshot=quotation.customer_name_snapshot,
            customer_trade_name_snapshot=quotation.customer_trade_name_snapshot,
            customer_document_type_snapshot=quotation.customer_document_type_snapshot,
            customer_document_number_snapshot=quotation.customer_document_number_snapshot,
            customer_address_snapshot=quotation.customer_address_snapshot,
            customer_ubigeo_snapshot=quotation.customer_ubigeo_snapshot,
            customer_email_snapshot=quotation.customer_email_snapshot,
            customer_phone_snapshot=quotation.customer_phone_snapshot,
            product_id=product.id,
            product_internal_reference=product.internal_reference,
            product_name=product.name,
            product_name_snapshot=quotation.product_name_snapshot or product.name,
            product_internal_reference_snapshot=(
                quotation.product_internal_reference_snapshot or product.internal_reference
            ),
            product_type_snapshot=quotation.product_type_snapshot or product.product_type.value,
            product_uom_snapshot=quotation.product_uom_snapshot or product.base_uom_code,
            product_material_snapshot=quotation.product_material_snapshot or product.material,
            product_grammage_snapshot=quotation.product_grammage_snapshot
            if quotation.product_grammage_snapshot is not None
            else product.grammage,
            product_width_snapshot=quotation.product_width_snapshot
            if quotation.product_width_snapshot is not None
            else product.width,
            product_height_snapshot=quotation.product_height_snapshot
            if quotation.product_height_snapshot is not None
            else product.height,
            product_length_snapshot=quotation.product_length_snapshot
            if quotation.product_length_snapshot is not None
            else product.length,
            product_depth_snapshot=quotation.product_depth_snapshot
            if quotation.product_depth_snapshot is not None
            else product.depth,
            quantity=quantity,
            recipe_id=quotation.recipe_id,
            recipe_version_id=quotation.recipe_version_id,
            recipe_version_fingerprint_snapshot=(quotation.recipe_version_fingerprint_snapshot),
            firing_id=quotation.firing_id,
            firing_line_id=quotation.firing_line_id,
            firing_code_snapshot=quotation.firing_code_snapshot,
            firing_snapshot=quotation.firing_snapshot,
            materials_calculated=quotation.materials_calculated,
            materials_applied=quotation.materials_applied,
            firing_cost=quotation.firing_cost,
            labor_cost=quotation.labor_cost,
            calculated_days=quotation.calculated_days,
            days_adjustment=quotation.days_adjustment,
            waiting_days=quotation.waiting_days,
            total_days=quotation.total_days,
            space_cost=quotation.space_cost,
            commercial_factor_default_snapshot=(quotation.commercial_factor_default_snapshot),
            commercial_factor=quotation.commercial_factor,
            current_sale_price_snapshot=quotation.current_sale_price_snapshot,
            base_commercial_cost=quotation.base_commercial_cost,
            calculated_total=quotation.calculated_total,
            calculated_unit_price=quotation.calculated_unit_price,
            final_unit_cost=quotation.final_unit_cost,
            final_total_cost=quotation.final_total_cost,
            markup_percent=quotation.markup_percent,
            target_profit_unit=quotation.target_profit_unit,
            calculated_sale_unit_price=quotation.calculated_sale_unit_price,
            suggested_commercial_unit_price=quotation.suggested_commercial_unit_price,
            commercial_sale_unit_price=quotation.commercial_sale_unit_price,
            effective_profit_unit=quotation.effective_profit_unit,
            effective_profit_total=quotation.effective_profit_total,
            effective_markup_percent=quotation.effective_markup_percent,
            commercial_subtotal=quotation.commercial_subtotal,
            commercial_total=quotation.commercial_total,
            commercial_unit_price_with_tax=quotation.commercial_unit_price_with_tax,
            # Una cotizacion guardada no vuelve a mirar la receta: si algun
            # material perdio el precio despues, lo dira el recalculo, no esto.
            materials_without_cost=[],
            material_grams_per_piece=gramos_guardados,
            material_total_grams=(
                gramos_guardados * Decimal(quantity) if gramos_guardados is not None else None
            ),
            tax_percentage=quotation.tax_percentage_snapshot,
            tax_rate_source=cast(
                Literal["PRODUCT", "COMMERCIAL_SETTINGS"], quotation.tax_rate_source_snapshot
            ),
            tax_amount=quotation.tax_amount,
            total_with_tax=quotation.total_with_tax,
            unit_price_with_tax=quotation.unit_price_with_tax,
            source_fingerprint=quotation.source_fingerprint,
            warnings=list(quotation.calculation_warnings),
            techniques=[
                TechniqueCalculationOut(
                    id=line.id,
                    technique_id=line.technique_id,
                    name_snapshot=line.name_snapshot,
                    unit_price_snapshot=line.unit_price_snapshot,
                    formula_type_snapshot=line.formula_type_snapshot,
                    factor_1_snapshot=line.factor_1_snapshot,
                    factor_2_snapshot=line.factor_2_snapshot,
                    quantity=line.quantity,
                    proposed_cost=line.proposed_cost,
                    applied_cost=line.applied_cost,
                    proposed_days=line.proposed_days,
                    applied_days=line.applied_days,
                    adjusted=line.unit_price_overridden
                    or line.factors_overridden
                    or line.applied_cost_overridden
                    or line.applied_days_overridden,
                    sort_order=line.sort_order,
                )
                for line in quotation.techniques
            ],
            additionals=[
                AdditionalCalculationOut(
                    id=line.id,
                    additional_id=line.additional_id,
                    name_snapshot=line.name_snapshot,
                    unit_price_snapshot=line.unit_price_snapshot,
                    formula_type_snapshot=line.formula_type_snapshot,
                    factor_1_snapshot=line.factor_1_snapshot,
                    additional_quantity=line.additional_quantity,
                    proposed_cost=line.proposed_cost,
                    applied_cost=line.applied_cost,
                    adjusted=line.unit_price_overridden
                    or line.factor_overridden
                    or line.applied_cost_overridden,
                    formula_explanation=self._formula_explanation(
                        line.formula_type_snapshot, line.factor_1_snapshot
                    ),
                    sort_order=line.sort_order,
                )
                for line in quotation.additionals
            ],
            other_costs=[
                OtherCostCalculationOut(
                    id=line.id,
                    other_cost_id=line.other_cost_id,
                    name_snapshot=line.name_snapshot,
                    unit_price_snapshot=line.unit_price_snapshot,
                    calculation_type_snapshot=line.calculation_type_snapshot,
                    proposed_cost=line.proposed_cost,
                    applied_cost=line.applied_cost,
                    adjusted=line.unit_price_overridden,
                    sort_order=line.sort_order,
                )
                for line in quotation.other_costs
            ],
        )

    async def get(self, quotation_id: int) -> QuotationOut:
        quotation = await self._get(quotation_id)
        if quotation.product_id is None:
            raise QuotationNotFoundError()
        product = await self._product(quotation.product_id, require_active=False)
        output = await self._stored_output(quotation, product)
        return QuotationOut(
            **output.model_dump(),
            id=quotation.id,
            code=quotation.code,
            status=quotation.status,
            created_by_id=str(quotation.created_by_id)
            if quotation.created_by_id is not None
            else None,
            confirmed_at=quotation.confirmed_at,
            cancelled_at=quotation.cancelled_at,
            created_at=quotation.created_at,
            updated_at=quotation.updated_at,
        )

    async def list_quotations(
        self,
        *,
        search: str | None = None,
        status: QuotationStatus | None = None,
        product_id: int | None = None,
        customer_id: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[QuotationSummaryOut], int]:
        stmt = select(Quotation).options(
            selectinload(Quotation.product),
            selectinload(Quotation.items),
        )
        if search:
            term = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(
                    Quotation.code.ilike(term),
                    Quotation.name.ilike(term),
                    Quotation.customer_name_snapshot.ilike(term),
                    Quotation.customer_document_number_snapshot.ilike(term),
                    Quotation.product.has(Product.name.ilike(term)),
                    Quotation.product.has(Product.internal_reference.ilike(term)),
                    Quotation.items.any(
                        QuotationItem.product.has(
                            or_(
                                Product.name.ilike(term),
                                Product.internal_reference.ilike(term),
                            )
                        )
                    ),
                )
            )
        if status is not None:
            stmt = stmt.where(Quotation.status == status)
        if product_id is not None:
            stmt = stmt.where(
                or_(
                    Quotation.product_id == product_id,
                    Quotation.items.any(QuotationItem.product_id == product_id),
                )
            )
        if customer_id is not None:
            stmt = stmt.where(Quotation.customer_id == customer_id)
        if date_from is not None:
            stmt = stmt.where(func.date(Quotation.created_at) >= date_from)
        if date_to is not None:
            stmt = stmt.where(func.date(Quotation.created_at) <= date_to)

        total = int(
            (
                await self._session.execute(select(func.count()).select_from(stmt.subquery()))
            ).scalar_one()
        )
        rows = list(
            (
                await self._session.execute(
                    stmt.order_by(Quotation.id.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        items = [
            QuotationSummaryOut(
                id=quotation.id,
                code=quotation.code,
                name=quotation.name,
                status=quotation.status,
                workflow=quotation.workflow,
                customer_id=quotation.customer_id,
                customer_name=quotation.customer_name_snapshot,
                customer_document_number=quotation.customer_document_number_snapshot,
                product_id=quotation.product.id if quotation.product else None,
                product_internal_reference=(
                    quotation.product.internal_reference if quotation.product else None
                ),
                product_name=(
                    quotation.product.name
                    if quotation.product
                    else f"{len(quotation.items)} productos"
                ),
                quantity=quotation.quantity,
                item_count=len(quotation.items),
                calculated_unit_price=quotation.calculated_unit_price,
                calculated_total=quotation.calculated_total,
                final_unit_cost=quotation.final_unit_cost,
                commercial_sale_unit_price=quotation.commercial_sale_unit_price,
                commercial_total=quotation.commercial_total,
                total_with_tax=quotation.total_with_tax,
                created_at=quotation.created_at,
            )
            for quotation in rows
        ]
        return items, total
