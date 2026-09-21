"""Solo Quema V2: cotizar la quema de piezas que el cliente ya trae (fase 010K).

Hoja «Solo Quema» del Excel final. Cuatro cosas, en este orden:

1. **medir** — el volumen de cada pieza con la separacion de la cotizacion;
2. **comparar** — la misma carga en cada horno, compartida y exclusiva. El
   sistema sugiere el horno mas barato y NUNCA lo cambia solo;
3. **valorar** — quema comercial del horno elegido + vidriado opcional, por
   el factor de x1,00 a x2,00, redondeado sobre el total;
4. **emitir** — congelar todo con una huella, como el Cotizador V2.

## Lo que este modulo NO hace

- **No fabrica.** Sin pasta, torno, colada, mano de obra productiva,
  administracion ni espacio.
- **No crea ordenes de produccion ni toca inventario.** La quema real de un
  servicio es la planificacion de hornadas de 010L.
- **No reutiliza el ciclo de vida del Cotizador V2 tal cual.** Aquel exige
  pasta y mano de obra para emitir. Se reutilizan sus piezas puras: vigencia,
  estado efectivo y huella.
- **No escribe en ningun maestro.** Un costo por gramo manual vive en esta
  cotizacion.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.errors import APIError
from app.core.firing_quotation_v2 import (
    CycleRates,
    FiringQuotationMathError,
    KilnQuote,
    ModeQuote,
    glaze_material_cost,
    kiln_quote,
    service_price,
    volume_shares,
)
from app.core.quoter_v2_firing import (
    MAX_VOLUME_CM3,
    FiringMathError,
    piece_volume,
    total_volume,
)
from app.core.quoter_v2_labor import (
    LaborMathError,
    hourly_rate,
    hours_required,
    labor_cost,
    quantize_hours,
    quantize_rate,
)
from app.core.quoter_v2_lifecycle import (
    V2EffectiveStatus,
    commercial_fingerprint,
    compute_validity,
    effective_status,
)
from app.core.quoter_v2_pricing import quantize_money
from app.models.audit import AuditAction
from app.models.firing_quotation_v2 import (
    V2FiringQuotation,
    V2FiringQuotationLine,
    V2GlazeCostSource,
)
from app.models.firings import FiringType, Kiln
from app.models.masters import Partner, PartnerRole, Product
from app.models.quoter_v2 import (
    V2CustomerKind,
    V2FiringMode,
    V2ProductionType,
    V2QuotationStatus,
)
from app.models.quoter_v2_labor import V2Technique, V2Worker, V2WorkerType
from app.models.quoter_v2_materials import V2MaterialCost, V2MaterialKind
from app.models.quoter_v2_settings import V2KilnRate
from app.models.sequence import SequenceType
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder
from app.services.quoter_v2_settings import V2SettingsService
from app.services.sequences import SequenceService

ZERO = Decimal(0)
_PERCENT_STEP = Decimal("0.000001")

V2_FIRING_QUOTATION_ENTITY = "v2_firing_quotation"
CUSTOMER_ROLES = (PartnerRole.CLIENT, PartnerRole.BOTH)

#: Avisos: no impiden guardar un borrador.
WARN_NO_LINES = "V2_FQ_NO_LINES"
WARN_LINE_WITHOUT_DIMENSIONS = "V2_FQ_LINE_WITHOUT_DIMENSIONS"
WARN_NO_KILN = "V2_FQ_NO_KILN"
WARN_KILN_INACTIVE = "V2_FQ_KILN_INACTIVE"
WARN_NO_CYCLE = "V2_FQ_NO_CYCLE"
WARN_RATES_MISSING = "V2_FQ_RATES_MISSING"
WARN_MULTIPLE_FIRINGS = "V2_FQ_MULTIPLE_FIRINGS"
WARN_EXCLUSIVE_RAISES_PRICE = "V2_FQ_EXCLUSIVE_RAISES_PRICE"
WARN_GLAZE_NO_MATERIAL = "V2_FQ_GLAZE_NO_MATERIAL"
WARN_GLAZE_MANUAL_COST_MISSING = "V2_FQ_GLAZE_MANUAL_COST_MISSING"
WARN_GLAZE_WITHOUT_GRAMS = "V2_FQ_GLAZE_WITHOUT_GRAMS"
WARN_GLAZE_LABOR_INCOMPLETE = "V2_FQ_GLAZE_LABOR_INCOMPLETE"
WARN_SELLING_BELOW_COST = "V2_FQ_SELLING_BELOW_COST"

#: Avisos que hablan de COSTO o MARGEN. Son de administracion y no viajan en
#: la vista del cliente: el resumen que se le ensena no dice que el taller esta
#: vendiendo por debajo de su costo.
WARNINGS_INTERNOS = frozenset({"V2_FQ_SELLING_BELOW_COST"})

#: Bloqueos de emision.
BLOCK_NO_CUSTOMER = "V2_FQ_NO_CUSTOMER"
BLOCK_NO_LINES = "V2_FQ_NO_LINES"
BLOCK_LINE_WITHOUT_NAME = "V2_FQ_LINE_WITHOUT_NAME"
BLOCK_LINE_WITHOUT_QUANTITY = "V2_FQ_LINE_WITHOUT_QUANTITY"
BLOCK_LINE_WITHOUT_DIMENSIONS = "V2_FQ_LINE_WITHOUT_DIMENSIONS"
BLOCK_NO_KILN = "V2_FQ_NO_KILN"
BLOCK_KILN_INACTIVE = "V2_FQ_KILN_INACTIVE"
BLOCK_NO_CYCLE = "V2_FQ_NO_CYCLE"
BLOCK_RATES_MISSING = "V2_FQ_RATES_MISSING"
BLOCK_GLAZE_INCOMPLETE = "V2_FQ_GLAZE_INCOMPLETE"
BLOCK_GLAZE_LABOR_INCOMPLETE = "V2_FQ_GLAZE_LABOR_INCOMPLETE"
BLOCK_NO_VALIDITY = "V2_FQ_NO_VALIDITY"
BLOCK_ZERO_TOTAL = "V2_FQ_ZERO_TOTAL"

#: Avisos de duplicacion.
DUP_CUSTOMER_UNAVAILABLE = "V2_FQ_DUP_CUSTOMER_UNAVAILABLE"
DUP_KILN_UNAVAILABLE = "V2_FQ_DUP_KILN_UNAVAILABLE"
DUP_GLAZE_UNAVAILABLE = "V2_FQ_DUP_GLAZE_UNAVAILABLE"
DUP_LABOR_UNAVAILABLE = "V2_FQ_DUP_LABOR_UNAVAILABLE"
DUP_PRODUCT_UNAVAILABLE = "V2_FQ_DUP_PRODUCT_UNAVAILABLE"

EVENT_CREATED = "created"
EVENT_UPDATED = "updated"
EVENT_CONFIRMED = "confirmed"
EVENT_CANCELLED = "cancelled"
EVENT_DUPLICATED = "duplicated"


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------
class V2FiringQuotationNotFoundError(APIError):
    status_code = 404
    code = "V2_FQ_NOT_FOUND"
    message = "La cotizacion de Solo Quema no existe"


class V2FiringQuotationLineNotFoundError(APIError):
    status_code = 404
    code = "V2_FQ_LINE_NOT_FOUND"
    message = "La pieza no existe en esta cotizacion"


class V2FiringQuotationNotEditableError(APIError):
    status_code = 409
    code = "V2_FQ_NOT_EDITABLE"
    message = "La cotizacion ya no es un borrador y no puede cambiarse"


class V2FiringQuotationInputInvalid(APIError):
    status_code = 422
    code = "V2_FQ_INPUT_INVALID"
    message = "Los datos no permiten calcular el servicio de quema"


class V2FiringQuotationChangedError(APIError):
    status_code = 409
    code = "V2_FQ_CHANGED"
    message = "La cotizacion cambio desde el resumen: revisela antes de emitir"


class V2FiringQuotationIncompleteError(APIError):
    status_code = 422
    code = "V2_FQ_INCOMPLETE"
    message = "A la cotizacion le falta informacion para emitirse"


class V2FiringQuotationAlreadyIssuedError(APIError):
    status_code = 409
    code = "V2_FQ_ALREADY_ISSUED"
    message = "La cotizacion ya se emitio con otro contenido"


class V2FiringQuotationNotConfirmableError(APIError):
    status_code = 409
    code = "V2_FQ_NOT_CONFIRMABLE"
    message = "Solo un borrador se puede emitir"


class V2FiringQuotationNotDuplicableError(APIError):
    status_code = 409
    code = "V2_FQ_NOT_DUPLICABLE"
    message = "Solo una cotizacion vencida o anulada se puede duplicar"


# ---------------------------------------------------------------------------
# Estado
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class KilnComparison:
    kiln_id: int
    name: str
    capacity_cm3: Decimal
    active: bool
    selected: bool
    quote: KilnQuote


@dataclass(frozen=True)
class KilnSuggestion:
    kiln_id: int
    name: str
    commercial: Decimal
    savings: Decimal


@dataclass
class FiringQuotationState:
    quotation: V2FiringQuotation
    lines: list[V2FiringQuotationLine]
    kilns: list[KilnComparison]
    suggestion: KilnSuggestion | None
    batch_loads: tuple[Decimal, ...]
    effective_status: V2EffectiveStatus
    warnings: list[str] = field(default_factory=list)


def _faltan_tarifas(fila: V2FiringQuotation) -> bool:
    """Si a algun ciclo encendido le falta su tarifa.

    Se mira el SNAPSHOT, no el importe. Una tarifa puesta a cero es legitima
    —el contrato de tarifas por horno admite `ge=0`, y hay quemas de cortesia—
    y da un total de cero sin que falte nada. Deducirlo del importe bloqueaba
    una quema gratis con vidriado cobrado: todo estaba configurado y aun asi
    no se dejaba emitir. Lo encontro la revision de Codex en el PR.
    """
    if fila.low_fire_enabled and fila.commercial_rate_low_snapshot is None:
        return True
    return fila.high_fire_enabled and fila.commercial_rate_high_snapshot is None


@dataclass(frozen=True)
class Blocker:
    code: str
    line_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "line_id": self.line_id}


@dataclass(frozen=True)
class FiringQuotationPreview:
    state: FiringQuotationState
    can_confirm: bool
    blockers: list[Blocker]
    fingerprint: str
    customer_name: str | None
    service_label: str
    valid_until: Any


def service_label(low: bool, high: bool) -> str:
    """Como se llama el servicio en el documento (hoja «PDF Quema», B6)."""
    if low and high:
        return "Quema baja + alta"
    if low:
        return "Quema baja"
    if high:
        return "Quema alta"
    return "Sin quema seleccionada"


def document_payload(
    quotation: V2FiringQuotation,
    lines: list[V2FiringQuotationLine],
    customer: dict[str, str | None],
) -> dict[str, Any]:
    """Lo que la huella protege: todo lo que cambia el documento o su precio."""
    return {
        "code": quotation.code,
        "customer": customer,
        "name": quotation.name,
        "client_notes": quotation.client_notes,
        "currency": quotation.currency_code_snapshot,
        "exchange_rate": quotation.exchange_rate_snapshot,
        "tax_percent": quotation.tax_percent_snapshot,
        "rounding_step": quotation.rounding_step_snapshot,
        "validity_days": quotation.validity_days_snapshot,
        "customer_kind": quotation.customer_kind,
        "kiln": [quotation.kiln_id, quotation.kiln_name_snapshot, quotation.kiln_capacity_snapshot],
        "firing_mode": quotation.firing_mode,
        "low": quotation.low_fire_enabled,
        "high": quotation.high_fire_enabled,
        "separation": quotation.piece_separation_cm,
        "rates": [
            quotation.commercial_rate_low_snapshot,
            quotation.commercial_rate_high_snapshot,
            quotation.gas_cost_low_snapshot,
            quotation.gas_cost_high_snapshot,
        ],
        "factor": quotation.factor,
        "glaze": [
            quotation.glaze_enabled,
            quotation.glaze_grams,
            quotation.glaze_cost_source,
            quotation.glaze_material_name_snapshot,
            quotation.glaze_cost_per_gram_snapshot,
            quotation.glaze_material_cost,
        ],
        "glaze_labor": [
            quotation.glaze_labor_enabled,
            quotation.glaze_labor_worker_name_snapshot,
            quotation.glaze_labor_technique_name_snapshot,
            quotation.glaze_labor_quantity,
            quotation.glaze_labor_hours,
            quotation.glaze_labor_cost,
        ],
        "lines": [
            [
                linea.id,
                linea.product_name_snapshot,
                linea.quantity,
                linea.length_cm,
                linea.width_cm,
                linea.height_cm,
            ]
            for linea in lines
        ],
        "subtotal": quotation.subtotal_amount,
        "tax": quotation.tax_amount,
        "total": quotation.total_amount,
    }


class V2FiringQuotationService:
    """Solo Quema V2."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._audit = audit
        self._settings = V2SettingsService(session, audit)
        self._sequences = SequenceService(session)

    # ------------------------------------------------------------------
    # Crear y leer
    # ------------------------------------------------------------------
    async def create_draft(
        self, data: dict[str, Any], *, user: AuthenticatedUser
    ) -> FiringQuotationState:
        cliente = await self._customer(data.get("customer_id"))
        v2 = await self._settings.get()
        politica = await self._settings.commercial_policy()
        moneda = await self._settings.currency_snapshot(
            data.get("currency_code") or politica.currency_code, data.get("exchange_rate")
        )
        # El horno con el que nace: el del Excel («Chico», el sugerido para
        # por menor). Es solo el valor inicial; el sistema nunca lo cambia solo.
        horno = await self._settings.suggested_kiln_for(V2ProductionType.RETAIL)

        fila = V2FiringQuotation(
            code=await self._sequences.issue(SequenceType.FIRING_V2, user_id=user.id),
            status=V2QuotationStatus.DRAFT,
            customer_id=cliente.id if cliente else None,
            customer_name_snapshot=cliente.name if cliente else None,
            name=data.get("name"),
            notes=data.get("notes"),
            client_notes=data.get("client_notes"),
            customer_kind=data.get("customer_kind") or v2.default_customer_kind,
            tax_percent_snapshot=politica.tax_percent,
            rounding_step_snapshot=politica.rounding_step,
            validity_days_snapshot=v2.quotation_validity_days,
            settings_version_snapshot=v2.version,
            kiln_id=horno.id if horno is not None else None,
            firing_mode=V2FiringMode.SHARED,
            low_fire_enabled=True,
            high_fire_enabled=True,
            piece_separation_cm=v2.piece_separation_cm,
            factor=v2.firing_service_factor_default,
            glaze_enabled=False,
            glaze_grams=ZERO,
            glaze_cost_source=V2GlazeCostSource.MASTER,
            glaze_labor_enabled=False,
            glaze_labor_quantity=ZERO,
            created_by=user.id,
            created_by_name=user.display_name,
            **moneda,
        )
        self._session.add(fila)
        await self._session.flush()
        self._record(fila, user, EVENT_CREATED)
        return await self._state(fila)

    async def list_quotations(
        self, *, limit: int, offset: int
    ) -> tuple[list[tuple[Any, ...]], int]:
        total = await self._session.scalar(select(func.count()).select_from(V2FiringQuotation))
        filas = (
            await self._session.scalars(
                select(V2FiringQuotation)
                .order_by(V2FiringQuotation.created_at.desc(), V2FiringQuotation.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        ahora = await self.db_now()
        return [(fila, self._effective(fila, ahora)) for fila in filas], int(total or 0)

    async def get_state(self, quotation_id: int) -> FiringQuotationState:
        """La lectura interna. Recalcula un borrador; la ruta no confirma nada."""
        return await self._state(await self._get(quotation_id))

    # ------------------------------------------------------------------
    # Editar
    # ------------------------------------------------------------------
    async def update(
        self, quotation_id: int, data: dict[str, Any], *, user: AuthenticatedUser
    ) -> FiringQuotationState:
        fila = await self._draft(quotation_id)
        cambios: list[str] = []

        for campo in ("name", "notes", "client_notes"):
            if campo in data:
                setattr(fila, campo, data[campo])
                cambios.append(campo)
        if "customer_id" in data:
            cliente = await self._customer(data["customer_id"])
            fila.customer_id = cliente.id if cliente else None
            fila.customer_name_snapshot = cliente.name if cliente else None
            cambios.append("customer_id")
        if "customer_kind" in data and data["customer_kind"] is not None:
            fila.customer_kind = V2CustomerKind(data["customer_kind"])
            cambios.append("customer_kind")
        if "currency_code" in data or "exchange_rate" in data:
            moneda = await self._settings.currency_snapshot(
                data.get("currency_code") or fila.currency_code_snapshot,
                data.get("exchange_rate"),
            )
            for campo, valor in moneda.items():
                setattr(fila, campo, valor)
            cambios.append("currency")
        if "kiln_id" in data and data["kiln_id"] != fila.kiln_id:
            if data["kiln_id"] is not None:
                horno = await self._session.get(Kiln, data["kiln_id"])
                if horno is None:
                    raise V2FiringQuotationInputInvalid(
                        "El horno no existe", code="V2_FQ_KILN_NOT_FOUND"
                    )
                if not horno.active:
                    raise V2FiringQuotationInputInvalid(
                        f"«{horno.name}» esta dado de baja", code="V2_FQ_KILN_INACTIVE"
                    )
            fila.kiln_id = data["kiln_id"]
            cambios.append("kiln_id")
        for campo in ("low_fire_enabled", "high_fire_enabled", "glaze_enabled"):
            if campo in data and data[campo] is not None:
                setattr(fila, campo, bool(data[campo]))
                cambios.append(campo)
        if "firing_mode" in data and data["firing_mode"] is not None:
            fila.firing_mode = V2FiringMode(data["firing_mode"])
            cambios.append("firing_mode")
        for campo in ("piece_separation_cm", "factor", "glaze_grams"):
            if campo in data and data[campo] is not None:
                setattr(fila, campo, data[campo])
                cambios.append(campo)
        if "glaze_cost_source" in data and data["glaze_cost_source"] is not None:
            fila.glaze_cost_source = V2GlazeCostSource(data["glaze_cost_source"])
            cambios.append("glaze_cost_source")
        if "glaze_material_id" in data:
            if data["glaze_material_id"] is not None:
                await self._glaze_material(data["glaze_material_id"], elegido_ahora=True)
            fila.glaze_material_id = data["glaze_material_id"]
            cambios.append("glaze_material_id")
        if "glaze_manual_cost_per_gram" in data:
            fila.glaze_manual_cost_per_gram = data["glaze_manual_cost_per_gram"]
            cambios.append("glaze_manual_cost_per_gram")
        await self._apply_labor(fila, data, cambios)

        await self._recalculate(fila, await self._lines(fila.id))
        await self._session.flush()
        self._record(fila, user, EVENT_UPDATED, campos=",".join(cambios))
        return await self._state(fila)

    async def _apply_labor(
        self, fila: V2FiringQuotation, data: dict[str, Any], cambios: list[str]
    ) -> None:
        if "glaze_labor_enabled" in data and data["glaze_labor_enabled"] is not None:
            fila.glaze_labor_enabled = bool(data["glaze_labor_enabled"])
            cambios.append("glaze_labor_enabled")
        if "glaze_labor_worker_id" in data and data["glaze_labor_worker_id"] != (
            fila.glaze_labor_worker_id
        ):
            if data["glaze_labor_worker_id"] is not None:
                trabajador = await self._session.get(V2Worker, data["glaze_labor_worker_id"])
                if trabajador is None or not trabajador.active:
                    raise V2FiringQuotationInputInvalid(
                        "La persona no existe o esta dada de baja",
                        code="V2_FQ_WORKER_UNAVAILABLE",
                    )
            fila.glaze_labor_worker_id = data["glaze_labor_worker_id"]
            cambios.append("glaze_labor_worker_id")
        if "glaze_labor_technique_id" in data and data["glaze_labor_technique_id"] != (
            fila.glaze_labor_technique_id
        ):
            if data["glaze_labor_technique_id"] is not None:
                tecnica = await self._session.get(V2Technique, data["glaze_labor_technique_id"])
                if tecnica is None or not tecnica.active:
                    raise V2FiringQuotationInputInvalid(
                        "La tecnica no existe o esta retirada",
                        code="V2_FQ_TECHNIQUE_UNAVAILABLE",
                    )
            fila.glaze_labor_technique_id = data["glaze_labor_technique_id"]
            cambios.append("glaze_labor_technique_id")
        if "glaze_labor_quantity" in data and data["glaze_labor_quantity"] is not None:
            fila.glaze_labor_quantity = data["glaze_labor_quantity"]
            cambios.append("glaze_labor_quantity")

    # ------------------------------------------------------------------
    # Piezas
    # ------------------------------------------------------------------
    async def add_line(
        self, quotation_id: int, data: dict[str, Any], *, user: AuthenticatedUser
    ) -> FiringQuotationState:
        fila = await self._draft(quotation_id)
        siguiente = await self._session.scalar(
            select(func.coalesce(func.max(V2FiringQuotationLine.sort_order), -1) + 1).where(
                V2FiringQuotationLine.v2_firing_quotation_id == fila.id
            )
        )
        linea = V2FiringQuotationLine(
            v2_firing_quotation_id=fila.id,
            sort_order=int(siguiente or 0),
            quantity=0,
        )
        await self._fill_line(linea, data)
        self._session.add(linea)
        await self._session.flush()
        await self._recalculate(fila, await self._lines(fila.id))
        await self._session.flush()
        self._record(fila, user, EVENT_UPDATED, campos="line_added")
        return await self._state(fila)

    async def update_line(
        self, quotation_id: int, line_id: int, data: dict[str, Any], *, user: AuthenticatedUser
    ) -> FiringQuotationState:
        fila = await self._draft(quotation_id)
        linea = await self._line(fila.id, line_id)
        await self._fill_line(linea, data)
        await self._session.flush()
        await self._recalculate(fila, await self._lines(fila.id))
        await self._session.flush()
        self._record(fila, user, EVENT_UPDATED, campos=f"line_updated:{line_id}")
        return await self._state(fila)

    async def delete_line(
        self, quotation_id: int, line_id: int, *, user: AuthenticatedUser
    ) -> FiringQuotationState:
        fila = await self._draft(quotation_id)
        linea = await self._line(fila.id, line_id)
        await self._session.delete(linea)
        await self._session.flush()
        await self._recalculate(fila, await self._lines(fila.id))
        await self._session.flush()
        self._record(fila, user, EVENT_UPDATED, campos=f"line_deleted:{line_id}")
        return await self._state(fila)

    async def _fill_line(self, linea: V2FiringQuotationLine, data: dict[str, Any]) -> None:
        """Rellena la pieza. Una pieza del catalogo ofrece nombre y medidas sin pisar."""
        if "product_id" in data:
            linea.product_id = data["product_id"]
            if linea.product_id is not None:
                producto = await self._session.get(Product, linea.product_id)
                if producto is None:
                    raise V2FiringQuotationInputInvalid(
                        "El producto no existe", code="V2_FQ_PRODUCT_NOT_FOUND"
                    )
                linea.product_name_snapshot = producto.name
                for campo, maestro in (
                    ("length_cm", producto.length),
                    ("width_cm", producto.width),
                    ("height_cm", producto.height),
                ):
                    if getattr(linea, campo) is None and campo not in data and maestro:
                        setattr(linea, campo, maestro)
        if "product_name" in data and linea.product_id is None:
            linea.product_name_snapshot = data["product_name"]
        if "quantity" in data and data["quantity"] is not None:
            linea.quantity = int(data["quantity"])
        for campo in ("length_cm", "width_cm", "height_cm"):
            if campo in data:
                setattr(linea, campo, data[campo])

    # ------------------------------------------------------------------
    # Recalculo
    # ------------------------------------------------------------------
    async def _state(self, fila: V2FiringQuotation) -> FiringQuotationState:
        lineas = await self._lines(fila.id)
        if fila.status is V2QuotationStatus.DRAFT:
            with self._session.no_autoflush:
                comparacion, sugerencia, avisos = await self._recalculate(fila, lineas)
        else:
            comparacion, sugerencia, avisos = await self._compare_frozen(fila)
        ahora = await self.db_now()
        from app.core.quoter_v2_firing import batch_loads

        return FiringQuotationState(
            quotation=fila,
            lines=lineas,
            kilns=comparacion,
            suggestion=sugerencia,
            batch_loads=batch_loads(fila.occupancy_percent, fila.firing_count),
            effective_status=self._effective(fila, ahora),
            warnings=avisos,
        )

    async def _compare_frozen(
        self, fila: V2FiringQuotation
    ) -> tuple[list[KilnComparison], KilnSuggestion | None, list[str]]:
        """Una emitida no se recalcula: se ensena lo que se congelo."""
        if fila.kiln_id is None or fila.kiln_capacity_snapshot is None:
            return [], None, []
        tarifas = CycleRates(
            fila.commercial_rate_low_snapshot,
            fila.commercial_rate_high_snapshot,
            fila.gas_cost_low_snapshot,
            fila.gas_cost_high_snapshot,
        )
        quote = kiln_quote(
            fila.total_volume_cm3,
            fila.kiln_capacity_snapshot,
            tarifas,
            low=fila.low_fire_enabled,
            high=fila.high_fire_enabled,
        )
        horno = await self._session.get(Kiln, fila.kiln_id)
        return (
            [
                KilnComparison(
                    kiln_id=fila.kiln_id,
                    name=fila.kiln_name_snapshot or "",
                    capacity_cm3=fila.kiln_capacity_snapshot,
                    active=bool(horno and horno.active),
                    selected=True,
                    quote=quote,
                )
            ],
            None,
            [],
        )

    async def _recalculate(
        self, fila: V2FiringQuotation, lineas: list[V2FiringQuotationLine]
    ) -> tuple[list[KilnComparison], KilnSuggestion | None, list[str]]:
        """Recalcula un borrador entero desde los maestros de HOY."""
        avisos: list[str] = []
        try:
            for linea in lineas:
                linea.unit_volume_cm3 = piece_volume(
                    linea.length_cm, linea.width_cm, linea.height_cm, fila.piece_separation_cm
                )
                linea.total_volume_cm3 = total_volume(linea.unit_volume_cm3, linea.quantity)
        except FiringMathError as error:
            raise V2FiringQuotationInputInvalid(str(error)) from error
        for linea, cuota in zip(
            lineas, volume_shares([linea.total_volume_cm3 for linea in lineas]), strict=True
        ):
            linea.volume_share_percent = cuota
        volumen = sum((linea.total_volume_cm3 for linea in lineas), ZERO)
        # Cada linea cabe por separado; la suma de muchas puede no caber.
        if volumen > MAX_VOLUME_CM3:
            raise V2FiringQuotationInputInvalid(
                f"El volumen total del pedido es demasiado grande ({volumen} cm3): "
                f"el maximo es {MAX_VOLUME_CM3} cm3. Revise medidas y cantidades."
            )
        fila.total_volume_cm3 = volumen
        if not lineas:
            avisos.append(WARN_NO_LINES)
        if any(linea.total_volume_cm3 <= ZERO for linea in lineas):
            avisos.append(WARN_LINE_WITHOUT_DIMENSIONS)
        baja, alta = fila.low_fire_enabled, fila.high_fire_enabled
        if not baja and not alta:
            avisos.append(WARN_NO_CYCLE)

        comparacion = await self._compare(fila, volumen)
        elegido = next((horno for horno in comparacion if horno.selected), None)
        modo_elegido: ModeQuote | None = None
        if fila.kiln_id is None or elegido is None:
            avisos.append(WARN_NO_KILN)
            fila.kiln_name_snapshot = None
            fila.kiln_capacity_snapshot = None
            fila.occupancy_percent = ZERO
            fila.firing_count = 0
            fila.billed_load = ZERO
        else:
            if not elegido.active:
                avisos.append(WARN_KILN_INACTIVE)
            fila.kiln_name_snapshot = elegido.name
            fila.kiln_capacity_snapshot = elegido.capacity_cm3
            fila.occupancy_percent = elegido.quote.occupancy_percent
            fila.firing_count = elegido.quote.firing_count
            modo_elegido = (
                elegido.quote.shared
                if fila.firing_mode is V2FiringMode.SHARED
                else elegido.quote.exclusive
            )
            if elegido.quote.firing_count > 1:
                avisos.append(WARN_MULTIPLE_FIRINGS)
            if (
                fila.firing_mode is V2FiringMode.EXCLUSIVE
                and elegido.quote.shared is not None
                and elegido.quote.exclusive is not None
                and elegido.quote.exclusive.commercial > elegido.quote.shared.commercial
            ):
                avisos.append(WARN_EXCLUSIVE_RAISES_PRICE)
            if modo_elegido is None and (baja or alta):
                avisos.append(WARN_RATES_MISSING)
        fila.billed_load = modo_elegido.billed_load if modo_elegido else ZERO
        fila.firing_commercial_total = modo_elegido.commercial if modo_elegido else ZERO
        fila.firing_gas_total = modo_elegido.gas if modo_elegido else ZERO

        avisos += await self._recalculate_glaze(fila)
        avisos += await self._recalculate_labor(fila)

        try:
            precio = service_price(
                firing_commercial=fila.firing_commercial_total,
                firing_gas=fila.firing_gas_total,
                glaze_material=fila.glaze_material_cost,
                glaze_labor=fila.glaze_labor_cost,
                factor=fila.factor,
                tax_percent=fila.tax_percent_snapshot or ZERO,
                rounding_step=fila.rounding_step_snapshot,
                exchange_rate=fila.exchange_rate_snapshot,
            )
        except FiringQuotationMathError as error:
            raise V2FiringQuotationInputInvalid(str(error)) from error
        fila.base_amount = precio.base_amount
        fila.commercial_price = precio.commercial_price
        fila.subtotal_amount = precio.subtotal
        fila.tax_amount = quantize_money(precio.tax)
        fila.total_amount = fila.subtotal_amount + fila.tax_amount
        fila.real_cost_total = precio.real_cost
        fila.estimated_profit = precio.profit
        fila.effective_margin_percent = precio.margin_percent.quantize(_PERCENT_STEP)
        if precio.subtotal > ZERO and precio.profit < ZERO:
            avisos.append(WARN_SELLING_BELOW_COST)

        sugerencia = self._suggest(fila, comparacion, modo_elegido)
        return comparacion, sugerencia, avisos

    async def _compare(self, fila: V2FiringQuotation, volumen: Decimal) -> list[KilnComparison]:
        """Cada horno activo (y el elegido aunque se diera de baja), con sus dos modos."""
        condicion: ColumnElement[bool] = Kiln.active.is_(True)
        if fila.kiln_id is not None:
            condicion = or_(condicion, Kiln.id == fila.kiln_id)
        hornos = list(
            (await self._session.scalars(select(Kiln).where(condicion).order_by(Kiln.code))).all()
        )
        if not hornos:
            return []
        tarifas: dict[int, dict[FiringType, V2KilnRate]] = {}
        for tarifa in (
            await self._session.scalars(
                select(V2KilnRate).where(V2KilnRate.kiln_id.in_([h.id for h in hornos]))
            )
        ).all():
            tarifas.setdefault(tarifa.kiln_id, {})[tarifa.firing_type] = tarifa
        alumno = fila.customer_kind is V2CustomerKind.STUDENT
        comparacion: list[KilnComparison] = []
        for horno in hornos:
            filas = tarifas.get(horno.id, {})
            baja = filas.get(FiringType.LOW)
            alta = filas.get(FiringType.HIGH)
            ciclo = CycleRates(
                commercial_low=(baja.student_rate if alumno else baja.external_rate)
                if baja
                else None,
                commercial_high=(alta.student_rate if alumno else alta.external_rate)
                if alta
                else None,
                gas_low=baja.gas_cost if baja else None,
                gas_high=alta.gas_cost if alta else None,
            )
            if horno.id == fila.kiln_id:
                fila.commercial_rate_low_snapshot = ciclo.commercial_low
                fila.commercial_rate_high_snapshot = ciclo.commercial_high
                fila.gas_cost_low_snapshot = ciclo.gas_low
                fila.gas_cost_high_snapshot = ciclo.gas_high
            try:
                quote = kiln_quote(
                    volumen,
                    horno.capacity_volume_cm3,
                    ciclo,
                    low=fila.low_fire_enabled,
                    high=fila.high_fire_enabled,
                )
            except FiringQuotationMathError as error:
                raise V2FiringQuotationInputInvalid(str(error)) from error
            comparacion.append(
                KilnComparison(
                    kiln_id=horno.id,
                    name=horno.name,
                    capacity_cm3=horno.capacity_volume_cm3,
                    active=horno.active,
                    selected=horno.id == fila.kiln_id,
                    quote=quote,
                )
            )
        return comparacion

    @staticmethod
    def _suggest(
        fila: V2FiringQuotation,
        comparacion: list[KilnComparison],
        elegido: ModeQuote | None,
    ) -> KilnSuggestion | None:
        """El horno activo mas barato en el MISMO modo, si no es el elegido."""
        if elegido is None or fila.total_volume_cm3 <= ZERO:
            return None
        candidatos: list[tuple[Decimal, int, KilnComparison]] = []
        for horno in comparacion:
            if horno.selected or not horno.active:
                continue
            modo = (
                horno.quote.shared
                if fila.firing_mode is V2FiringMode.SHARED
                else horno.quote.exclusive
            )
            if modo is not None and modo.commercial < elegido.commercial:
                candidatos.append((modo.commercial, horno.kiln_id, horno))
        if not candidatos:
            return None
        comercial, _id, mejor = min(candidatos, key=lambda c: (c[0], c[1]))
        return KilnSuggestion(
            kiln_id=mejor.kiln_id,
            name=mejor.name,
            commercial=comercial,
            savings=elegido.commercial - comercial,
        )

    async def _recalculate_glaze(self, fila: V2FiringQuotation) -> list[str]:
        if not fila.glaze_enabled:
            fila.glaze_material_cost = ZERO
            fila.glaze_cost_per_gram_snapshot = None
            fila.glaze_material_name_snapshot = None
            return []
        avisos: list[str] = []
        costo_por_gramo: Decimal | None
        manual = fila.glaze_manual_cost_per_gram
        # El Excel es explicito (hoja «Solo Quema», E10): el costo manual vale
        # SOLO si es mayor que cero; si no, se usa el del maestro. Un cero no es
        # un acuerdo de esmalte gratis, es un campo a medio llenar, y cobrar el
        # vidriado a cero por eso seria regalarlo.
        if (
            fila.glaze_cost_source is V2GlazeCostSource.MANUAL
            and manual is not None
            and (manual > ZERO)
        ):
            costo_por_gramo = manual
            fila.glaze_material_name_snapshot = None
        elif fila.glaze_cost_source is V2GlazeCostSource.MANUAL:
            avisos.append(WARN_GLAZE_MANUAL_COST_MISSING)
            material = await self._most_expensive_glaze()
            if material is None:
                avisos.append(WARN_GLAZE_NO_MATERIAL)
                costo_por_gramo = None
                fila.glaze_material_name_snapshot = None
            else:
                costo_por_gramo = material.effective_cost_per_unit
                fila.glaze_material_name_snapshot = material.product.name
        else:
            material = (
                await self._glaze_material(fila.glaze_material_id, elegido_ahora=False)
                if fila.glaze_material_id is not None
                else await self._most_expensive_glaze()
            )
            if material is None:
                avisos.append(WARN_GLAZE_NO_MATERIAL)
                costo_por_gramo = None
                fila.glaze_material_name_snapshot = None
            else:
                costo_por_gramo = material.effective_cost_per_unit
                fila.glaze_material_name_snapshot = material.product.name
        if fila.glaze_grams <= ZERO:
            avisos.append(WARN_GLAZE_WITHOUT_GRAMS)
        fila.glaze_cost_per_gram_snapshot = costo_por_gramo
        try:
            fila.glaze_material_cost = glaze_material_cost(
                fila.glaze_grams, costo_por_gramo or ZERO
            )
        except FiringQuotationMathError as error:
            raise V2FiringQuotationInputInvalid(str(error)) from error
        return avisos

    async def _recalculate_labor(self, fila: V2FiringQuotation) -> list[str]:
        """MO de vidriado: horas = cantidad / rendimiento x jornada; interno = 0."""
        if not fila.glaze_labor_enabled:
            fila.glaze_labor_hours = ZERO
            fila.glaze_labor_cost = ZERO
            return []
        trabajador = (
            await self._session.get(V2Worker, fila.glaze_labor_worker_id)
            if fila.glaze_labor_worker_id is not None
            else None
        )
        tecnica = (
            await self._session.get(V2Technique, fila.glaze_labor_technique_id)
            if fila.glaze_labor_technique_id is not None
            else None
        )
        if trabajador is None or tecnica is None:
            fila.glaze_labor_hours = ZERO
            fila.glaze_labor_cost = ZERO
            return [WARN_GLAZE_LABOR_INCOMPLETE]
        jornada = trabajador.workday_hours
        if jornada is None:
            jornada = (await self._settings.get()).workday_hours
        try:
            horas = quantize_hours(
                hours_required(
                    fila.glaze_labor_quantity, tecnica.default_capacity_per_workday, jornada
                )
            )
            tarifa = quantize_rate(hourly_rate(trabajador.daily_rate, jornada))
            costo = (
                ZERO
                if trabajador.worker_type is V2WorkerType.INTERNAL
                else quantize_money(labor_cost(horas, tarifa))
            )
        except LaborMathError as error:
            raise V2FiringQuotationInputInvalid(str(error)) from error
        fila.glaze_labor_worker_name_snapshot = trabajador.name
        fila.glaze_labor_worker_type_snapshot = trabajador.worker_type.value
        fila.glaze_labor_technique_name_snapshot = tecnica.name
        fila.glaze_labor_capacity_snapshot = tecnica.default_capacity_per_workday
        fila.glaze_labor_workday_hours_snapshot = jornada
        fila.glaze_labor_hourly_rate_snapshot = tarifa
        fila.glaze_labor_hours = horas
        fila.glaze_labor_cost = costo
        return []

    # ------------------------------------------------------------------
    # Emitir
    # ------------------------------------------------------------------
    async def preview(self, quotation_id: int) -> FiringQuotationPreview:
        fila = await self._get(quotation_id)
        estado = await self._state(fila)
        cliente = await self._issuance(fila)
        huella = commercial_fingerprint(document_payload(fila, estado.lines, cliente))
        bloqueos = (
            await self._blockers(fila, estado.lines)
            if fila.status is V2QuotationStatus.DRAFT
            else []
        )
        proyectada: date | None
        if fila.status is V2QuotationStatus.DRAFT and fila.validity_days_snapshot:
            proyectada, _ = compute_validity(await self.db_now(), fila.validity_days_snapshot)
        else:
            proyectada = fila.valid_until
        # La vista del cliente se queda con los avisos que le incumben. Se
        # construye un estado APARTE en vez de vaciarle los avisos al que vino:
        # hoy `_state()` fabrica uno nuevo en cada llamada y no habria dano, pero
        # esto deja de depender de eso —el dia que ese estado se comparta o se
        # cachee, mutarlo aqui borraria el aviso de venta bajo costo de la vista
        # interna, que es donde tiene que verse—. Lo noto Codex.
        para_el_cliente = replace(
            estado,
            warnings=[a for a in estado.warnings if a not in WARNINGS_INTERNOS],
        )
        return FiringQuotationPreview(
            state=para_el_cliente,
            can_confirm=fila.status is V2QuotationStatus.DRAFT and not bloqueos,
            blockers=bloqueos,
            fingerprint=huella,
            customer_name=cliente["name"],
            service_label=service_label(fila.low_fire_enabled, fila.high_fire_enabled),
            valid_until=proyectada,
        )

    async def confirm(
        self, quotation_id: int, expected_fingerprint: str, *, user: AuthenticatedUser
    ) -> tuple[V2FiringQuotation, bool]:
        """Emite y congela. Mismo orden que V2: doble clic, huella, bloqueos."""
        fila = await self._locked(quotation_id)
        if fila.status is V2QuotationStatus.CONFIRMED:
            if fila.commercial_fingerprint == expected_fingerprint:
                return fila, False
            raise V2FiringQuotationAlreadyIssuedError()
        if fila.status is not V2QuotationStatus.DRAFT:
            raise V2FiringQuotationNotConfirmableError()

        lineas = await self._lines(fila.id)
        with self._session.no_autoflush:
            await self._recalculate(fila, lineas)
        cliente = await self._issuance(fila)
        huella = commercial_fingerprint(document_payload(fila, lineas, cliente))
        if huella != expected_fingerprint:
            raise V2FiringQuotationChangedError()
        bloqueos = await self._blockers(fila, lineas)
        if bloqueos or not fila.validity_days_snapshot:
            raise V2FiringQuotationIncompleteError(details=[b.as_dict() for b in bloqueos])

        emitida_en = await self.db_now()
        valid_until, expires_at = compute_validity(emitida_en, fila.validity_days_snapshot)
        fila.issued_at = emitida_en
        fila.valid_until = valid_until
        fila.expires_at = expires_at
        fila.issued_by = user.id
        fila.issued_by_name = user.display_name
        fila.commercial_fingerprint = huella
        fila.customer_name_snapshot = cliente["name"]
        fila.customer_document_type_snapshot = cliente["document_type"]
        fila.customer_document_number_snapshot = cliente["document_number"]
        fila.customer_address_snapshot = cliente["address"]
        fila.customer_email_snapshot = cliente["email"]
        fila.customer_phone_snapshot = cliente["phone"]
        fila.status = V2QuotationStatus.CONFIRMED
        await self._session.flush()
        self._record(
            fila,
            user,
            EVENT_CONFIRMED,
            valid_until=valid_until.isoformat(),
            total_amount=str(fila.total_amount),
            fingerprint=huella,
        )
        return fila, True

    async def cancel(
        self, quotation_id: int, reason: str | None, *, user: AuthenticatedUser
    ) -> tuple[V2FiringQuotation, bool]:
        """Anula un borrador o una emitida. Idempotente; no borra nada."""
        fila = await self._locked(quotation_id)
        if fila.status is V2QuotationStatus.CANCELLED:
            return fila, False
        fila.status = V2QuotationStatus.CANCELLED
        fila.cancelled_at = await self.db_now()
        fila.cancelled_by = user.id
        fila.cancelled_by_name = user.display_name
        fila.cancel_reason = reason
        await self._session.flush()
        self._record(fila, user, EVENT_CANCELLED, was_issued=str(fila.issued_at is not None))
        return fila, True

    async def duplicate(
        self, quotation_id: int, *, user: AuthenticatedUser
    ) -> tuple[FiringQuotationState, bool, list[str]]:
        """Un borrador NUEVO con tarifas, IGV, TC y configuracion de HOY.

        Solo de una vencida o anulada. La original no se toca. Idempotente frente
        al doble clic: si ya hay un borrador abierto nacido de ella, se devuelve.
        """
        original = await self._locked(quotation_id)
        estado = self._effective(original, await self.db_now())
        if estado not in (V2EffectiveStatus.EXPIRED, V2EffectiveStatus.CANCELLED):
            raise V2FiringQuotationNotDuplicableError()
        abierta = await self._open_duplicate(original.id)
        if abierta is not None:
            return await self._state(abierta), False, []

        avisos: list[str] = []
        cliente_id = original.customer_id
        if cliente_id is not None and not await self._customer_usable(cliente_id):
            avisos.append(DUP_CUSTOMER_UNAVAILABLE)
            cliente_id = None
        try:
            async with self._session.begin_nested():
                nueva_estado = await self.create_draft(
                    {
                        "customer_id": cliente_id,
                        "name": original.name,
                        "notes": original.notes,
                        "client_notes": original.client_notes,
                        "currency_code": original.currency_code_snapshot,
                        "customer_kind": original.customer_kind,
                    },
                    user=user,
                )
                nueva = nueva_estado.quotation
                nueva.duplicated_from_id = original.id
                await self._session.flush()
        except IntegrityError:
            ganadora = await self._open_duplicate(original.id)
            if ganadora is None:
                raise
            return await self._state(ganadora), False, []

        # Las DECISIONES viajan; la economia (tarifas, IGV, TC, factor) es la de hoy.
        if original.kiln_id is not None:
            horno = await self._session.get(Kiln, original.kiln_id)
            if horno is not None and horno.active:
                nueva.kiln_id = horno.id
            else:
                avisos.append(DUP_KILN_UNAVAILABLE)
        nueva.firing_mode = original.firing_mode
        nueva.low_fire_enabled = original.low_fire_enabled
        nueva.high_fire_enabled = original.high_fire_enabled
        nueva.piece_separation_cm = original.piece_separation_cm
        nueva.glaze_enabled = original.glaze_enabled
        nueva.glaze_grams = original.glaze_grams
        nueva.glaze_cost_source = original.glaze_cost_source
        nueva.glaze_manual_cost_per_gram = original.glaze_manual_cost_per_gram
        if original.glaze_material_id is not None:
            if await self._glaze_material(original.glaze_material_id, elegido_ahora=False):
                nueva.glaze_material_id = original.glaze_material_id
            else:
                avisos.append(DUP_GLAZE_UNAVAILABLE)
        if original.glaze_labor_enabled:
            trabajador = (
                await self._session.get(V2Worker, original.glaze_labor_worker_id)
                if original.glaze_labor_worker_id is not None
                else None
            )
            tecnica = (
                await self._session.get(V2Technique, original.glaze_labor_technique_id)
                if original.glaze_labor_technique_id is not None
                else None
            )
            if trabajador and trabajador.active and tecnica and tecnica.active:
                nueva.glaze_labor_enabled = True
                nueva.glaze_labor_worker_id = trabajador.id
                nueva.glaze_labor_technique_id = tecnica.id
                nueva.glaze_labor_quantity = original.glaze_labor_quantity
            else:
                avisos.append(DUP_LABOR_UNAVAILABLE)
        for orden, linea in enumerate(await self._lines(original.id)):
            producto_id = linea.product_id
            if producto_id is not None and await self._session.get(Product, producto_id) is None:
                avisos.append(DUP_PRODUCT_UNAVAILABLE)
                producto_id = None
            self._session.add(
                V2FiringQuotationLine(
                    v2_firing_quotation_id=nueva.id,
                    sort_order=orden,
                    product_id=producto_id,
                    product_name_snapshot=linea.product_name_snapshot,
                    quantity=linea.quantity,
                    length_cm=linea.length_cm,
                    width_cm=linea.width_cm,
                    height_cm=linea.height_cm,
                )
            )
        await self._session.flush()
        await self._recalculate(nueva, await self._lines(nueva.id))
        await self._session.flush()
        self._record(original, user, EVENT_DUPLICATED, duplicate_id=str(nueva.id))
        return await self._state(nueva), True, sorted(set(avisos))

    # ------------------------------------------------------------------
    # Apoyo
    # ------------------------------------------------------------------
    async def db_now(self) -> datetime:
        instante = await self._session.scalar(select(func.clock_timestamp()))
        assert instante is not None
        return instante

    @staticmethod
    def _effective(fila: V2FiringQuotation, ahora: datetime) -> V2EffectiveStatus:
        return effective_status(
            status=fila.status.value,
            expires_at=fila.expires_at,
            has_production_handoff=False,
            now=ahora,
        )

    async def _get(self, quotation_id: int) -> V2FiringQuotation:
        fila = await self._session.get(V2FiringQuotation, quotation_id)
        if fila is None:
            raise V2FiringQuotationNotFoundError()
        return fila

    async def _locked(self, quotation_id: int) -> V2FiringQuotation:
        """La cotizacion bloqueada: serializa ediciones, emisiones y anulaciones."""
        fila = (
            await self._session.scalars(
                select(V2FiringQuotation)
                .where(V2FiringQuotation.id == quotation_id)
                .with_for_update()
            )
        ).one_or_none()
        if fila is None:
            raise V2FiringQuotationNotFoundError()
        return fila

    async def _draft(self, quotation_id: int) -> V2FiringQuotation:
        fila = await self._locked(quotation_id)
        if fila.status is not V2QuotationStatus.DRAFT:
            raise V2FiringQuotationNotEditableError()
        return fila

    async def _lines(self, quotation_id: int) -> list[V2FiringQuotationLine]:
        return list(
            (
                await self._session.scalars(
                    select(V2FiringQuotationLine)
                    .where(V2FiringQuotationLine.v2_firing_quotation_id == quotation_id)
                    .order_by(V2FiringQuotationLine.sort_order, V2FiringQuotationLine.id)
                )
            ).all()
        )

    async def _line(self, quotation_id: int, line_id: int) -> V2FiringQuotationLine:
        linea = await self._session.get(V2FiringQuotationLine, line_id)
        if linea is None or linea.v2_firing_quotation_id != quotation_id:
            raise V2FiringQuotationLineNotFoundError()
        return linea

    async def _open_duplicate(self, original_id: int) -> V2FiringQuotation | None:
        return await self._session.scalar(
            select(V2FiringQuotation).where(
                V2FiringQuotation.duplicated_from_id == original_id,
                V2FiringQuotation.status == V2QuotationStatus.DRAFT,
            )
        )

    async def _customer(self, customer_id: int | None) -> Partner | None:
        if customer_id is None:
            return None
        cliente = await self._session.get(Partner, customer_id)
        if cliente is None or not cliente.active or cliente.role not in CUSTOMER_ROLES:
            raise V2FiringQuotationInputInvalid(
                "El cliente no existe, esta dado de baja o no es cliente",
                code="V2_FQ_CUSTOMER_INVALID",
            )
        return cliente

    async def _customer_usable(self, customer_id: int) -> bool:
        cliente = await self._session.get(Partner, customer_id)
        return cliente is not None and cliente.active and cliente.role in CUSTOMER_ROLES

    async def _glaze_material(
        self, product_id: int, *, elegido_ahora: bool
    ) -> V2MaterialCost | None:
        """Un esmalte valorizado y activo. Elegirlo HOY sin serlo es un error."""
        material = await self._session.scalar(
            select(V2MaterialCost)
            .join(Product, Product.id == V2MaterialCost.product_id)
            .where(
                V2MaterialCost.product_id == product_id,
                V2MaterialCost.material_kind == V2MaterialKind.GLAZE,
                Product.active,
            )
            .options(joinedload(V2MaterialCost.product))
        )
        if material is None and elegido_ahora:
            raise V2FiringQuotationInputInvalid(
                "El esmalte no esta valorizado o esta dado de baja",
                code="V2_FQ_GLAZE_UNAVAILABLE",
            )
        return material

    async def _most_expensive_glaze(self) -> V2MaterialCost | None:
        """El esmalte ACTIVO mas caro por gramo: la referencia del Excel."""
        return await self._session.scalar(
            select(V2MaterialCost)
            .join(Product, Product.id == V2MaterialCost.product_id)
            .where(
                V2MaterialCost.material_kind == V2MaterialKind.GLAZE,
                Product.active,
                V2MaterialCost.effective_cost_per_unit > ZERO,
            )
            .options(joinedload(V2MaterialCost.product))
            .order_by(V2MaterialCost.effective_cost_per_unit.desc(), V2MaterialCost.id.desc())
            .limit(1)
        )

    async def _issuance(self, fila: V2FiringQuotation) -> dict[str, str | None]:
        """Lo que el documento dira del cliente: el maestro de hoy o lo congelado."""
        if fila.status is not V2QuotationStatus.DRAFT:
            return {
                "name": fila.customer_name_snapshot,
                "document_type": fila.customer_document_type_snapshot,
                "document_number": fila.customer_document_number_snapshot,
                "address": fila.customer_address_snapshot,
                "email": fila.customer_email_snapshot,
                "phone": fila.customer_phone_snapshot,
            }
        cliente = (
            await self._session.get(Partner, fila.customer_id)
            if fila.customer_id is not None
            else None
        )
        return {
            "name": cliente.name if cliente else fila.customer_name_snapshot,
            "document_type": (
                cliente.document_type.value if cliente and cliente.document_type else None
            ),
            "document_number": cliente.document_number if cliente else None,
            "address": cliente.address if cliente else None,
            "email": cliente.email if cliente else None,
            "phone": (cliente.phone or cliente.mobile) if cliente else None,
        }

    async def _blockers(
        self, fila: V2FiringQuotation, lineas: list[V2FiringQuotationLine]
    ) -> list[Blocker]:
        bloqueos: list[Blocker] = []
        if fila.customer_id is None or not await self._customer_usable(fila.customer_id):
            bloqueos.append(Blocker(BLOCK_NO_CUSTOMER))
        if not lineas:
            bloqueos.append(Blocker(BLOCK_NO_LINES))
        for linea in lineas:
            if not (linea.product_name_snapshot or "").strip():
                bloqueos.append(Blocker(BLOCK_LINE_WITHOUT_NAME, linea.id))
            if linea.quantity <= 0:
                bloqueos.append(Blocker(BLOCK_LINE_WITHOUT_QUANTITY, linea.id))
            if linea.unit_volume_cm3 <= ZERO:
                bloqueos.append(Blocker(BLOCK_LINE_WITHOUT_DIMENSIONS, linea.id))
        if fila.kiln_id is None:
            bloqueos.append(Blocker(BLOCK_NO_KILN))
        else:
            horno = await self._session.get(Kiln, fila.kiln_id)
            if horno is None or not horno.active:
                bloqueos.append(Blocker(BLOCK_KILN_INACTIVE))
        if not fila.low_fire_enabled and not fila.high_fire_enabled:
            bloqueos.append(Blocker(BLOCK_NO_CYCLE))
        elif fila.kiln_id is not None and lineas and _faltan_tarifas(fila):
            bloqueos.append(Blocker(BLOCK_RATES_MISSING))
        if fila.glaze_enabled and (
            fila.glaze_grams <= ZERO or fila.glaze_cost_per_gram_snapshot is None
        ):
            bloqueos.append(Blocker(BLOCK_GLAZE_INCOMPLETE))
        if fila.glaze_labor_enabled and (
            fila.glaze_labor_worker_id is None
            or fila.glaze_labor_technique_id is None
            or fila.glaze_labor_quantity <= ZERO
        ):
            bloqueos.append(Blocker(BLOCK_GLAZE_LABOR_INCOMPLETE))
        if not fila.validity_days_snapshot:
            bloqueos.append(Blocker(BLOCK_NO_VALIDITY))
        if fila.subtotal_amount <= ZERO:
            bloqueos.append(Blocker(BLOCK_ZERO_TOTAL))
        return bloqueos

    def _record(
        self, fila: V2FiringQuotation, user: AuthenticatedUser, event: str, **extra: str
    ) -> None:
        self._audit.record_action(
            entity_type=V2_FIRING_QUOTATION_ENTITY,
            entity_id=str(fila.id),
            action=AuditAction.CREATE if event == EVENT_CREATED else AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"event": event, "code": fila.code, **extra},
        )


__all__ = [
    "V2_FIRING_QUOTATION_ENTITY",
    "Blocker",
    "FiringQuotationPreview",
    "FiringQuotationState",
    "KilnComparison",
    "KilnSuggestion",
    "V2FiringQuotationService",
    "document_payload",
    "service_label",
]
