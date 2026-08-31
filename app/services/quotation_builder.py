"""Orquestador transaccional del Cotizador multiproducto de Fase 005.11."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import APIError
from app.core.precision import QUANTITY_SCALE
from app.core.pricing import (
    BASE_CURRENCY,
    LinePricingInput,
    allocate_fixed_costs,
    price_line,
)
from app.models.audit import AuditAction
from app.models.firings import FiringType
from app.models.masters import Partner, Product
from app.models.quotations import (
    OtherCost,
    Quotation,
    QuotationItem,
    QuotationStatus,
    QuotationWorkflow,
)
from app.models.recipes import Recipe, RecipeStatus, RecipeVersion
from app.models.sequence import SequenceType
from app.models.settings import CommercialSettings
from app.schemas.auth import AuthenticatedUser
from app.schemas.firings import FiringIn, FiringLineIn, FiringSessionIn
from app.schemas.quotation_builder import (
    GlazePlanOut,
    GlazeSelectionItemIn,
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
from app.services.preparations import (
    GlazeChoice,
    GlazeEstimate,
    PreparationService,
    PreparationValidationError,
)
from app.services.quotation_pdf import QuotationPdfService
from app.services.quotations import FiringEstimateOverride, QuotationService
from app.services.sequences import SequenceService

ZERO = Decimal(0)
BUILDER_ENTITY = "quotation_builder"
PRODUCTION_DIMENSIONS = ("length", "width", "height")
ALL_DIMENSIONS = ("width", "height", "length", "depth")
HIDDEN_WARNING_CODES = {"DISCOUNT_RULE_BLOCKED_BY_SOURCE"}
#: Avisos que impiden dar una linea por completa.
#:
#: Vive en un solo sitio a proposito. Antes estaba duplicado entre `preview()`
#: y `_item_complete()`, y una lista que hay que recordar actualizar dos veces
#: acaba divergiendo: el preview daria la linea por incompleta y la lectura de
#: lo guardado por completa, o al reves.
BLOCKING_WARNING_CODES = frozenset(
    {
        "QUANTITY_REQUIRED",
        "PRODUCTION_DIMENSIONS_REQUIRED",
        "KILN_REQUIRED",
        "FIRING_REQUIRED",
        "FIRING_KILN_REQUIRED",
        "KILN_CAPACITY_EXCEEDED",
        "RECIPE_REQUIRED",
        "MATERIAL_GRAMS_PER_PIECE_REQUIRED",
        "CUSTOM_DIMENSIONS_NOT_ALLOWED_FOR_CONFIRMED_FIRING",
        # Fase 009D
        "GLAZE_PIECE_WEIGHT_REQUIRED",
        "GLAZE_ML_REQUIRES_PREPARATION",
    }
)
_DIMENSION_QUANTUM = Decimal(1).scaleb(-QUANTITY_SCALE)


#: Fase 009C: cada hornada ocupa el horno tres dias (enfriado incluido).
#: Regla secuencial simple: el sistema no modela actividades en paralelo
#: (calculated_days = suma de dias de tecnicas, sin scheduling), asi que las
#: hornadas se suman una tras otra en vez de inventar planificacion avanzada.
#: Fase 009C: la duracion de una hornada NO es una constante. Vive en
#: ``kilns.firing_days_per_batch`` porque depende del horno (el pequeno tarda 3
#: dias y el grande 4), y el negocio la edita desde Configuracion sin
#: desplegar. Este valor solo se usa como ultimo recurso cuando el preview no
#: trae la duracion —un borrador a medio llenar, sin horno todavia elegido—, y
#: nunca decide los dias de una cotizacion completa.
FALLBACK_DAYS_PER_BATCH = 3


def _selected_kilns(
    item: QuotationBuilderItemIn, fallback_kiln_id: int | None
) -> tuple[int | None, int | None]:
    """Hornos de quema baja y alta realmente seleccionados para una linea.

    Fase 009C: baja y alta son independientes, y la INTENCION manda sobre el
    dato. El flag decide si la quema existe; el id (o, en su defecto, el
    horno de cabecera) solo dice con que horno. Un `low_kiln_id` que quedo
    en el payload de una seleccion anterior NO resucita la quema: sin flag
    no hay quema, aunque el id siga ahi.

    `fallback_kiln_id` nunca inventa una quema que no se pidio; solo rellena
    el horno de una quema ya elegida.
    """
    low = (item.low_kiln_id or fallback_kiln_id) if item.low_kiln_selected else None
    high = (item.high_kiln_id or fallback_kiln_id) if item.high_kiln_selected else None
    return low, high


def _selected_routes(
    item: QuotationBuilderItemIn, fallback_kiln_id: int | None
) -> set[tuple[int, str]]:
    """Pares exactos (horno, tipo de quema) que esta linea usa.

    Emparejar por par —y no cruzando "hornos de la linea" con "tipos de la
    linea"— es lo que evita un producto cartesiano: una linea que usa
    (horno 1, BAJA) y (horno 2, ALTA) contaria tambien la sesion
    (horno 2, BAJA) de OTRA linea, inflando sus hornadas, sus dias y con
    ellos todos los gastos PER_DAY.
    """
    low, high = _selected_kilns(item, fallback_kiln_id)
    routes: set[tuple[int, str]] = set()
    if low is not None:
        routes.add((low, FiringType.LOW.value))
    if high is not None:
        routes.add((high, FiringType.HIGH.value))
    return routes


def _firings_without_kiln(item: QuotationBuilderItemIn, fallback_kiln_id: int | None) -> bool:
    """True si alguna quema pedida se quedo sin horno con el que hacerse.

    Un payload anterior a 009C puede traer `low_kiln_id` y ningun horno de
    cabecera: con el nuevo `high_kiln_selected=True` por omision, la quema
    alta queda pedida pero sin horno. Antes eso daba HIGH_KILN_REQUIRED;
    sin esta comprobacion la cotizacion saldria "valida" habiendose comido
    en silencio el costo y el plazo de esa quema.
    """
    low, high = _selected_kilns(item, fallback_kiln_id)
    return (item.low_kiln_selected and low is None) or (item.high_kiln_selected and high is None)


def _quantize_dimension(value: Decimal | None) -> Decimal | None:
    """Normaliza una dimension a la misma escala de la columna DB (6 decimales).

    Fase 009B: una dimension recien parseada del payload ("24", escala 0) y
    la misma dimension releida de products.width/QuotationItem.*_snapshot
    tras un roundtrip por Postgres ("24.000000", escala 6) son iguales en
    VALOR pero distintas en REPRESENTACION. _fingerprint() hashea texto ya
    stringificado (via los distintos "*_cm"/"*_snapshot" que arma
    firings.py), asi que sin esta normalizacion dos calculos con exactamente
    la misma dimension efectiva podian producir un fingerprint distinto
    segun de donde viniera el valor ese request en particular - disparando
    un QUOTATION_BUILDER_SOURCE_CHANGED espurio al confirmar.
    """
    if value is None:
        return None
    return value.quantize(_DIMENSION_QUANTUM)


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


class QuotationBuilderIncompleteError(APIError):
    status_code = 409
    code = "QUOTATION_BUILDER_INCOMPLETE"
    message = "Complete los datos obligatorios antes de confirmar"


def _fingerprint_default(value: object) -> str:
    # Un Decimal matematicamente igual puede llegar con distinta cantidad de
    # ceros de relleno segun su origen: recien parseado del payload del
    # usuario ("24") vs releido de una columna quantity_numeric() tras un
    # roundtrip por la base de datos ("24.000000"). str() sin normalizar
    # preserva esa diferencia de representacion y el fingerprint (que hashea
    # el string) los trataria como si hubieran cambiado, aunque el valor
    # efectivo sea identico. normalize() colapsa ambos a la misma forma
    # canonica antes de convertir a texto.
    if isinstance(value, Decimal):
        return str(value.normalize())
    return str(value)


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=_fingerprint_default)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: Fase 009F. La unica procedencia de tipo de cambio que existe hoy: lo teclea
#: una persona. La constante esta para que el dia que haya una fuente
#: automatica se distinga sin adivinar mirando fechas.
EXCHANGE_RATE_SOURCE = "MANUAL"

#: Simbolos de presentacion. `US$` y no `$` porque en Peru un `$` suelto se lee
#: como sol tan a menudo como como dolar, y aqui la diferencia es de 3,75 a 1.
CURRENCY_SYMBOLS = {"PEN": "S/", "USD": "US$"}


def _supported_currency(code: str | None) -> Literal["PEN", "USD"] | None:
    """Estrecha un codigo guardado a las monedas que 009F admite.

    Devuelve `None` —y no el codigo tal cual— cuando no es una de las dos:
    asi el borrador reconstruido cae al default en vez de arrastrar una moneda
    que el motor rechazaria despues con un error mucho menos claro.
    """
    return code if code in ("PEN", "USD") else None  # type: ignore[return-value]


def _resolve_currency(
    payload: QuotationBuilderDraftIn, settings: CommercialSettings
) -> tuple[str, Decimal | None]:
    """Decide en que moneda se emite y con que tasa.

    La cotizacion manda. Configuracion solo aporta el valor inicial cuando el
    borrador no dice nada, que es el caso de todo lo escrito antes de 009F.

    Una moneda de Configuracion que no sea PEN ni USD no puede emitirse: se
    cae a PEN en vez de propagar un codigo que el motor rechazaria mas
    adelante con un error mucho menos claro.
    """
    if payload.currency_code is not None:
        solicitada: str = payload.currency_code
    else:
        configurada = settings.currency_code or BASE_CURRENCY
        solicitada = configurada if configurada in CURRENCY_SYMBOLS else BASE_CURRENCY
    if solicitada == BASE_CURRENCY:
        return BASE_CURRENCY, None
    return solicitada, payload.exchange_rate


def _currency_symbol(currency_code: str, settings: CommercialSettings) -> str:
    """El simbolo con el que se presenta la moneda.

    `currency_code` es la autoridad semantica; el simbolo es presentacion. Se
    respeta el de Configuracion solo cuando coincide la moneda, porque un
    `S/` guardado alli no debe acabar encabezando importes en dolares.
    """
    if currency_code == (settings.currency_code or BASE_CURRENCY) and settings.currency_symbol:
        return settings.currency_symbol
    return CURRENCY_SYMBOLS.get(currency_code, currency_code)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _output_warnings(values: list[str]) -> list[str]:
    return [value for value in _unique(values) if value not in HIDDEN_WARNING_CODES]


def _snapshot_decimal(value: object) -> Decimal:
    if value is None:
        return ZERO
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return ZERO


def _confirmed_item_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if (
        snapshot.get("source") != "CONFIRMED_FIRING_LINE"
        or snapshot.get("occupancy_percentage") is not None
    ):
        return snapshot
    volume = _snapshot_decimal(snapshot.get("total_volume_cm3"))
    capacities = {
        _snapshot_decimal(session.get("capacity_snapshot"))
        for session in snapshot.get("sessions", [])
        if isinstance(session, dict) and session.get("capacity_snapshot") is not None
    }
    capacities.discard(ZERO)
    if not volume or not capacities:
        return snapshot
    enriched = dict(snapshot)
    enriched["occupancy_percentage"] = str(volume / min(capacities) * Decimal(100))
    return enriched


def _confirmed_production_summary(
    summary: dict[str, object], items: list[QuotationBuilderItemOut]
) -> dict[str, object]:
    """Completa las metricas de lineas confirmadas, incluso para snapshots antiguos."""
    confirmed = [
        item for item in items if item.production_snapshot.get("source") == "CONFIRMED_FIRING_LINE"
    ]
    if not confirmed:
        return summary

    total_volume = sum(
        (_snapshot_decimal(item.production_snapshot.get("total_volume_cm3")) for item in confirmed),
        ZERO,
    )
    base_total = sum(
        (
            _snapshot_decimal(
                item.production_snapshot.get("base_cost")
                or item.production_snapshot.get("subtotal")
            )
            for item in confirmed
        ),
        ZERO,
    )
    firing_total = sum(
        (
            _snapshot_decimal(
                item.production_snapshot.get("total_cost")
                or item.production_snapshot.get("allocated_cost")
                or item.firing_cost
            )
            for item in confirmed
        ),
        ZERO,
    )
    weighted_factor = sum(
        (
            _snapshot_decimal(item.production_snapshot.get("base_cost"))
            * _snapshot_decimal(item.production_snapshot.get("occupancy_factor"))
            for item in confirmed
        ),
        ZERO,
    )
    weighted_occupancy = sum(
        (
            _snapshot_decimal(item.production_snapshot.get("total_volume_cm3"))
            * _snapshot_decimal(item.production_snapshot.get("occupancy_percentage"))
            for item in confirmed
        ),
        ZERO,
    )
    weighted_volume = sum(
        (
            _snapshot_decimal(item.production_snapshot.get("total_volume_cm3"))
            for item in confirmed
            if item.production_snapshot.get("occupancy_percentage") is not None
        ),
        ZERO,
    )

    enriched = dict(summary)
    enriched.update(
        {
            "complete": True,
            "total_volume_cm3": str(total_volume),
            "subtotal": str(base_total),
            "total_cost": str(firing_total),
            "occupancy_factor": (
                str(weighted_factor / base_total)
                if base_total and weighted_factor
                else str(firing_total / base_total)
                if base_total
                else "0"
            ),
        }
    )
    if weighted_volume:
        enriched["occupancy_percentage"] = str(weighted_occupancy / weighted_volume)
    return enriched


def _stored_production_factor(plan: object) -> Decimal | None:
    """Factor de produccion guardado, o `None` si la linea es anterior a 009E."""
    if not isinstance(plan, dict):
        return None
    factor = _snapshot_decimal(plan.get("production_factor"))
    return factor if factor > ZERO else None


def _stored_rounding_step(plan: object) -> Literal["0.50", "1.00"] | None:
    """Normaliza el paso de redondeo guardado al literal del contrato.

    El snapshot pasa por `jsonable_encoder`, que puede dejar el Decimal como
    0.5 en vez de "0.50". Devolverlo tal cual haria fallar la validacion al
    reabrir el borrador, asi que se compara por VALOR y se devuelve la forma
    canonica.
    """
    if not isinstance(plan, dict):
        return None
    valor = _snapshot_decimal(plan.get("rounding_step"))
    if valor == Decimal("0.50"):
        return "0.50"
    if valor == Decimal("1.00"):
        return "1.00"
    return None


def _stored_commercial_plan(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Devuelve el plan comercial guardado, listo para el esquema de salida.

    Una cotizacion confirmada se LEE, no se recalcula: cambiar despues el IGV,
    el factor o los costos fijos no puede mover un precio ya comprometido.
    Los borradores anteriores a 009E no lo tienen; para ellos se devuelven los
    campos en cero y la linea se vuelve a costear al recalcular.
    """
    plan = snapshot.get("commercial_plan")
    if not isinstance(plan, dict):
        return {}
    campos = (
        "production_factor",
        "raw_net_unit_base",
        "technical_cost",
        "factored_cost",
        "fixed_cost_allocation",
        "commercial_base_cost",
        "commercial_base_unit_cost",
        "raw_net_unit",
        "raw_tax_unit",
        "raw_gross_unit",
        "rounding_step",
        "rounding_adjustment_unit",
        "final_gross_unit",
        "final_net_unit",
        "final_tax_unit",
        "line_total_net",
        "line_total_tax",
        "line_total_gross",
    )
    guardado: dict[str, Any] = {campo: _snapshot_decimal(plan.get(campo)) for campo in campos}
    # La moneda y su procedencia son texto, no importes: no pasan por
    # `_snapshot_decimal`. La tasa si, porque es un Decimal.
    moneda = plan.get("currency")
    if isinstance(moneda, str):
        guardado["currency_code_snapshot"] = moneda
    tasa = plan.get("exchange_rate")
    if tasa is not None:
        guardado["exchange_rate_snapshot"] = _snapshot_decimal(tasa)
    return guardado


def _stored_glaze_plan(snapshot: dict[str, Any]) -> GlazePlanOut | None:
    plan = snapshot.get("glaze_plan")
    if not isinstance(plan, dict):
        return None
    return GlazePlanOut.model_validate(plan)


def _glaze_inputs(snapshot: dict[str, Any]) -> list[GlazeSelectionItemIn]:
    """Recupera la eleccion de esmaltes guardada en el snapshot.

    Se lee `glazes_input` —lo que el usuario mando— y solo si falta se cae a
    las asignaciones ya resueltas, que es como quedaron guardados los
    borradores de antes de que existiera el plan. En ese caso el `share` se
    recupera del propio reparto: no hay nada mejor de donde sacarlo.
    """
    raw = snapshot.get("glazes_input")
    if not isinstance(raw, list):
        plan = snapshot.get("glaze_plan")
        raw = plan.get("allocations", []) if isinstance(plan, dict) else []
    selections: list[GlazeSelectionItemIn] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        preparation_id = entry.get("preparation_id")
        prepared_product_id = entry.get("prepared_product_id")
        if preparation_id is None and prepared_product_id is None:
            continue
        selections.append(
            GlazeSelectionItemIn(
                preparation_id=preparation_id,
                prepared_product_id=prepared_product_id,
                share=_snapshot_decimal(entry.get("share")) or Decimal(1),
            )
        )
    return selections


def _glaze_plan_snapshot(
    estimate: GlazeEstimate,
    *,
    unit: str,
    piece_weight_g: Decimal,
    default_applied: bool = False,
) -> dict[str, Any]:
    """Serializa el plan para `production_snapshot["glaze_plan"]`.

    Se guardan tanto la ENTRADA del usuario (`share`, `unit`, los ids) como los
    DERIVADOS congelados (`allocation_percent`, gramos, mililitros,
    concentracion y costo por mililitro). Los derivados no son redundancia: al
    confirmar, la cotizacion tiene que poder explicarse sola aunque despues
    cambien el porcentaje configurado, la receta o el lote.
    """
    return {
        "unit": unit,
        "default_applied": default_applied,
        "estimated_glaze_percent_snapshot": estimate.estimated_glaze_percent,
        "piece_weight_g_snapshot": piece_weight_g,
        "grams_per_piece": estimate.grams_per_piece,
        "total_estimated_solids_g": estimate.total_grams,
        "total_estimated_cost": (
            sum((a.cost for a in estimate.allocations if a.cost is not None), ZERO)
            if any(a.cost is not None for a in estimate.allocations)
            else None
        ),
        "allocations": [
            {
                "prepared_product_id": allocation.prepared_product.id,
                "prepared_product_internal_reference": (
                    allocation.prepared_product.internal_reference
                ),
                "prepared_product_name": allocation.prepared_product.name,
                "preparation_id": (allocation.preparation.id if allocation.preparation else None),
                "preparation_code": (
                    allocation.preparation.code if allocation.preparation else None
                ),
                "share": allocation.share,
                "allocation_percent": allocation.allocation_percent,
                "grams": allocation.grams,
                "millilitres": allocation.millilitres,
                "solids_g_per_ml_snapshot": allocation.solids_g_per_ml,
                "unit_cost_per_ml_snapshot": allocation.unit_cost_per_ml,
                "estimated_cost": allocation.cost,
            }
            for allocation in estimate.allocations
        ],
    }


class QuotationBuilderService:
    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRecorder,
        sequences: SequenceService,
        firings: FiringService,
        quotations: QuotationService,
        pdf: QuotationPdfService | None = None,
    ) -> None:
        self._session = session
        self._audit = audit
        self._sequences = sequences
        self._firings = firings
        self._quotations = quotations
        self._pdf = pdf or QuotationPdfService(session)
        # Fase 009D: el plan de esmaltes se calcula con el mismo motor que
        # la estimacion suelta. Duplicar la matematica aqui la haria
        # divergir en silencio en cuanto una de las dos cambiara.
        self._preparations = PreparationService(session, audit)

    async def _glaze_plan(
        self, item: QuotationBuilderItemIn, product: Product
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Resuelve el plan de esmaltes de una linea y sus avisos.

        El peso de la pieza sale de `Product.grammage`, no de un campo que el
        usuario teclee en el Cotizador: es el mismo dato del maestro con el que
        se calcula todo lo demas de la pieza, y tenerlo en dos sitios lo haria
        divergir.

        Los errores de eleccion se devuelven como AVISOS y no como excepciones:
        un borrador tiene que poder abrirse y arreglarse. El aviso impide
        completar, que es lo que de verdad hace falta bloquear.
        """
        glazes = list(item.glazes)
        default_applied = False
        if not glazes and not item.glaze_selection_touched:
            # Cotizacion preliminar: se propone el preparado mas caro. Pecar
            # por arriba es recuperable —si al final se usa uno mas barato, el
            # taller gana—; al reves se pierde dinero en un precio que ya se
            # comprometio. En cuanto el usuario toca la seleccion deja de
            # proponerse, incluso si la deja vacia a proposito.
            suggested = await self._preparations.most_expensive_preparation()
            if suggested is not None:
                glazes = [
                    GlazeSelectionItemIn(
                        preparation_id=suggested.id,
                        prepared_product_id=suggested.prepared_product_id,
                        share=Decimal(1),
                    )
                ]
                default_applied = True
        if not glazes:
            return None, []
        if item.quantity is None:
            # Ya lo bloquea QUANTITY_REQUIRED; anadir otro aviso solo repetiria
            # el mismo problema con dos nombres distintos.
            return None, []
        if product.grammage is None or product.grammage <= ZERO:
            # Sin peso de pieza no hay nada que estimar. Inventar un peso daria
            # un costo de esmalte creible y falso.
            #
            # Pero una SUGERENCIA no puede bloquear: si el aviso saltara
            # tambien cuando el esmalte lo propuso el sistema, cualquier
            # producto sin gramaje dejaria de poder cotizarse en cuanto
            # existiera un preparado en la base. El aviso es para quien pidio
            # el esmalte, no para quien no pidio nada.
            if default_applied:
                return None, []
            return None, ["GLAZE_PIECE_WEIGHT_REQUIRED"]

        choices = [
            GlazeChoice(
                share=glaze.share,
                preparation_id=glaze.preparation_id,
                prepared_product_id=glaze.prepared_product_id,
            )
            for glaze in glazes
        ]
        try:
            estimate = await self._preparations.estimate_glaze(
                piece_weight_g=product.grammage,
                quantity=item.quantity,
                glazes=choices,
                unit=item.glaze_unit,
            )
        except PreparationValidationError:
            if item.glaze_unit != "ml":
                raise
            # Pedir mililitros sin lote elegido no invalida el borrador: se
            # muestra el plan en gramos y se avisa de lo que falta.
            estimate = await self._preparations.estimate_glaze(
                piece_weight_g=product.grammage,
                quantity=item.quantity,
                glazes=choices,
                unit="g",
            )
            return (
                _glaze_plan_snapshot(
                    estimate,
                    unit="g",
                    piece_weight_g=product.grammage,
                    default_applied=default_applied,
                ),
                ["GLAZE_ML_REQUIRES_PREPARATION"],
            )
        return (
            _glaze_plan_snapshot(
                estimate,
                unit=item.glaze_unit,
                piece_weight_g=product.grammage,
                default_applied=default_applied,
            ),
            [],
        )

    async def _total_fixed_cost(self) -> Decimal:
        """Costos fijos de la cotizacion: se suman UNA vez, no por linea.

        Son los maestros de otros gastos activos. `calculation_type` ya no es
        autoridad de calculo en 009E: alquiler, servicios y administrativo son
        importes totales de la cotizacion, no importes por dia ni por pieza.
        Multiplicarlos por los dias del lote en cada linea cobraba el taller
        entero tantas veces como productos tuviera el pedido.
        """
        rows = (
            (await self._session.execute(select(OtherCost).where(OtherCost.active.is_(True))))
            .scalars()
            .all()
        )
        return sum((row.unit_price for row in rows), ZERO)

    @staticmethod
    def _apply_commercial_engine(
        items: list[QuotationBuilderItemOut],
        *,
        production_factor: Decimal,
        rounding_step: Decimal,
        total_fixed_cost: Decimal,
        currency_code: str = BASE_CURRENCY,
        exchange_rate: Decimal | None = None,
    ) -> list[QuotationBuilderItemOut]:
        """Aplica factor, reparto de fijos, margen, IGV y redondeo a cada linea.

        Las lineas sin cantidad todavia no se pueden costear, asi que no
        participan del reparto: darles peso repartiria costos fijos hacia una
        linea que aun no existe como tal.
        """
        priceable = [item for item in items if item.quantity and item.quantity > 0]
        if not priceable:
            return items

        factored = [item.technical_cost * production_factor for item in priceable]
        if sum(factored, ZERO) <= ZERO:
            # Sin base factorada no hay con que repartir. Se deja el reparto en
            # cero en vez de inventar partes iguales; la linea sigue viendose.
            allocations = [ZERO] * len(priceable)
        else:
            allocations = list(allocate_fixed_costs(factored, total_fixed_cost))

        priced: dict[int, QuotationBuilderItemOut] = {}
        for item, allocation in zip(priceable, allocations, strict=True):
            pricing = price_line(
                LinePricingInput(
                    quantity=cast(int, item.quantity),
                    technical_cost=item.technical_cost,
                    production_factor=production_factor,
                    fixed_cost_allocation=allocation,
                    markup_percent=item.markup_percent,
                    tax_percent=item.tax_percentage_snapshot,
                    rounding_step=rounding_step,
                    manual_net_unit=item.commercial_sale_unit_price_input,
                    currency=currency_code,
                    exchange_rate=exchange_rate,
                )
            )
            priced[item.sort_order] = item.model_copy(
                update={
                    "production_factor": pricing.production_factor,
                    "factored_cost": pricing.factored_cost,
                    "fixed_cost_allocation": pricing.fixed_cost_allocation,
                    "commercial_base_cost": pricing.commercial_base_cost,
                    "commercial_base_unit_cost": pricing.commercial_base_unit_cost,
                    "currency_code_snapshot": pricing.currency,
                    "exchange_rate_snapshot": pricing.exchange_rate,
                    "raw_net_unit_base": pricing.raw_net_unit_base,
                    "raw_net_unit": pricing.raw_net_unit,
                    "raw_tax_unit": pricing.raw_tax_unit,
                    "raw_gross_unit": pricing.raw_gross_unit,
                    "rounding_step": pricing.rounding_step,
                    "rounding_adjustment_unit": pricing.rounding_adjustment_unit,
                    "final_gross_unit": pricing.final_gross_unit,
                    "final_net_unit": pricing.final_net_unit,
                    "final_tax_unit": pricing.final_tax_unit,
                    "line_total_gross": pricing.line_total_gross,
                    "line_total_net": pricing.line_total_net,
                    "line_total_tax": pricing.line_total_tax,
                    # Los campos historicos siguen poblados con la MISMA
                    # semantica nueva, para que nada quede leyendo dos motores
                    # comerciales a la vez.
                    "commercial_sale_unit_price": pricing.final_net_unit,
                    # Una sola sugerencia. Dejar la del motor anterior seria
                    # ensenar dos precios distintos para la misma pieza.
                    "suggested_commercial_unit_price": pricing.final_net_unit,
                    "commercial_unit_price_with_tax": pricing.final_gross_unit,
                    "commercial_subtotal": pricing.line_total_net,
                    "tax_amount": pricing.line_total_tax,
                    "commercial_total": pricing.line_total_gross,
                    "space_cost": pricing.fixed_cost_allocation,
                }
            )
        return [priced.get(item.sort_order, item) for item in items]

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
        """Resuelve las dimensiones efectivas de un item.

        Fase 009B: una dimension enviada por la cotizacion SIEMPRE gana sobre
        el maestro (personalizacion por linea, sin mutar products.*); si no
        se envia nada para un campo, se usa el valor del maestro (puede ser
        None si el maestro tampoco lo tiene, lo que dispara el warning de
        PRODUCTION_DIMENSIONS_REQUIRED mas abajo). Antes de esta fase, un
        valor enviado que difiriera del maestro lanzaba PRODUCT_DIMENSION_CONFLICT
        y, al guardar, se escribia silenciosamente en el maestro compartido -
        ver docstring de dimensions_overridden en QuotationBuilderItemIn.
        """
        submitted = completion.model_dump(exclude_none=True)
        return {
            field: _quantize_dimension(submitted.get(field, getattr(product, field)))
            for field in ALL_DIMENSIONS
        }

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
        simulated = [entry for entry in resolved if entry[0].firing_line_id is None]
        if not resolved:
            warnings.append("ITEM_REQUIRED")
        for item, _product, dimensions, _recipe in simulated:
            if item.quantity is None:
                warnings.append("QUANTITY_REQUIRED")
            if any(dimensions[field] is None for field in PRODUCTION_DIMENSIONS):
                warnings.append("PRODUCTION_DIMENSIONS_REQUIRED")
            # Fase 009C: quema baja y alta son INDEPENDIENTES. Una pieza puede
            # necesitar solo baja, solo alta, o ambas. Lo unico obligatorio es
            # que haya al menos una — regla que ya vive en el dominio de quemas
            # (FiringService._build: "hay que indicar al menos un horno para la
            # quema baja o la alta"), aqui solo se refleja para avisar antes.
            # any(), no `not tupla`: _selected_kilns devuelve (low, high) y una
            # tupla de dos None sigue siendo truthy.
            if not any(_selected_kilns(item, kiln_id)):
                warnings.append("FIRING_REQUIRED")
            elif _firings_without_kiln(item, kiln_id):
                warnings.append("FIRING_KILN_REQUIRED")

        if warnings:
            summary = {"estimated": True, "complete": False, "warnings": _unique(warnings)}
            return summary, {}, {}, _unique(warnings)

        if not simulated:
            summary = {
                "estimated": False,
                "source": "CONFIRMED_FIRING_LINES",
                "complete": True,
                "warnings": [],
                "sessions": [],
                "lines": [],
            }
            return summary, {}, {}, []

        for item, _product, dimensions, _recipe in simulated:
            assert item.quantity is not None
            assert all(dimensions[field] is not None for field in PRODUCTION_DIMENSIONS)
        # Fase 009C: solo se abren sesiones para las quemas realmente
        # seleccionadas. Antes se creaban siempre baja Y alta, lo que obligaba
        # a pagar dos quemas aunque la pieza necesitara una sola.
        session_routes: list[tuple[int, FiringType]] = []
        for item, _product, _dimensions, _recipe in simulated:
            low_kiln_id, high_kiln_id = _selected_kilns(item, kiln_id)
            routes = [
                (low_kiln_id, FiringType.LOW) if low_kiln_id is not None else None,
                (high_kiln_id, FiringType.HIGH) if high_kiln_id is not None else None,
            ]
            for route in routes:
                if route is not None and route not in session_routes:
                    session_routes.append(route)
        # El horno del factor debe participar en LA HOJA (regla de
        # FiringService._build), no necesariamente en las sesiones de esta
        # linea: una pieza puede valorarse con la capacidad de otro horno de
        # la misma hoja. Si el elegido no llego a la hoja se deja en None y
        # el dominio usa el primer horno de la propia linea.
        sheet_kilns = {route[0] for route in session_routes}
        firing_payload = FiringIn(
            sessions=[
                FiringSessionIn(kiln_id=route[0], firing_type=route[1], sort_order=index)
                for index, route in enumerate(session_routes)
            ],
            lines=[
                FiringLineIn(
                    product_id=product.id,
                    description=product.name,
                    quantity=cast(int, item.quantity),
                    length_cm=cast(Decimal, dimensions["length"]),
                    width_cm=cast(Decimal, dimensions["width"]),
                    height_cm=cast(Decimal, dimensions["height"]),
                    low_kiln_id=_selected_kilns(item, kiln_id)[0],
                    high_kiln_id=_selected_kilns(item, kiln_id)[1],
                    factor_kiln_id=(
                        (item.factor_kiln_id or kiln_id)
                        if (item.factor_kiln_id or kiln_id) in sheet_kilns
                        else None
                    ),
                    sort_order=index,
                )
                for index, (item, product, dimensions, _recipe) in enumerate(simulated)
            ],
        )
        # multi_batch=True: el Cotizador planifica, asi que un volumen que no
        # entra en una hornada se resuelve con varias, no con una alerta.
        result = await self._firings.calculate(firing_payload, multi_batch=True)
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
        # El orden de la lista es la autoridad del builder. Normalizarlo evita
        # que dos items que omiten ``sort_order`` colisionen con la restriccion
        # unica (quotation_id, sort_order) al guardar el borrador.
        for position, (item, product, dimensions, recipe) in enumerate(resolved):
            recipe_id, version_id, version_fingerprint, auto_selected = recipe
            warnings: list[str] = []
            if item.quantity is None:
                warnings.append("QUANTITY_REQUIRED")
            missing_dimensions = [
                field for field in PRODUCTION_DIMENSIONS if dimensions[field] is None
            ]
            if missing_dimensions:
                warnings.append("PRODUCTION_DIMENSIONS_REQUIRED")
            # Una linea de quema confirmada es un hecho fisico ya ocurrido:
            # su volumen y su costo asignado se calcularon con las medidas
            # reales de esa hornada. Personalizar las medidas aqui no puede
            # cambiar ese costo (el item queda fuera de _simulate_production
            # y _firing_source usa unit_volume_cm3/allocated_cost historicos),
            # asi que aceptarlo produciria una cotizacion que ANUNCIA una
            # pieza mas grande cobrando la quema de la pequena. Se bloquea.
            if item.firing_line_id is not None and item.dimensions_overridden:
                warnings.append("CUSTOM_DIMENSIONS_NOT_ALLOWED_FOR_CONFIRMED_FIRING")
            # Fase 009C: baja y alta son independientes; solo se exige que
            # haya al menos una (misma regla que el dominio de quemas).
            if item.firing_line_id is None:
                if not any(_selected_kilns(item, payload.kiln_id)):
                    warnings.append("FIRING_REQUIRED")
                elif _firings_without_kiln(item, payload.kiln_id):
                    warnings.append("FIRING_KILN_REQUIRED")
            if item.materials_applied is None and (recipe_id is None or version_id is None):
                warnings.append("RECIPE_REQUIRED")
            if item.materials_applied is None and item.material_grams_per_piece is None:
                warnings.append("MATERIAL_GRAMS_PER_PIECE_REQUIRED")
            if "KILN_CAPACITY_EXCEEDED" in production_warnings:
                warnings.append("KILN_CAPACITY_EXCEEDED")

            # Fase 009D: el plan de esmaltes es una estimacion tecnica. No
            # entra en el costo (eso es 009E) ni descuenta inventario (009H).
            glaze_plan, glaze_warnings = await self._glaze_plan(item, product)
            warnings.extend(glaze_warnings)

            line_snapshot = production_lines.get(product.id, {})
            source_key = {
                "simulation": _fingerprint(production),
                "product_id": product.id,
                "line": line_snapshot,
            }
            # Fase 009C: los dias salen de las SESIONES que realmente queman
            # esta pieza (baja y/o alta), y cada sesion aporta
            # ``hornadas * dias_por_hornada de SU horno``.
            #
            # Se suma sesion a sesion, y no "total de hornadas x dias", porque
            # baja y alta pueden ir en hornos de duracion distinta: 27 hornadas
            # en el pequeno (3 dias) mas 3 en el grande (4 dias) son 81 + 12 =
            # 93 dias, no 30 x nada.
            item_routes = _selected_routes(item, payload.kiln_id)
            item_sessions = [
                session
                for session in production.get("sessions", [])
                if (session.get("kiln_id"), session.get("firing_type")) in item_routes
            ]
            item_plan = [
                {
                    "kiln_id": session.get("kiln_id"),
                    "kiln_code": session.get("kiln_code"),
                    "kiln_name": session.get("kiln_name"),
                    "firing_type": session.get("firing_type"),
                    "firing_days_per_batch": int(
                        session.get("days_per_batch", FALLBACK_DAYS_PER_BATCH)
                    ),
                    "required_batches": int(session.get("batches", 1)),
                    "calculated_firing_days": int(session.get("batches", 1))
                    * int(session.get("days_per_batch", FALLBACK_DAYS_PER_BATCH)),
                }
                for session in item_sessions
            ]
            item_batches = sum(entry["required_batches"] for entry in item_plan)
            item_days = sum(entry["calculated_firing_days"] for entry in item_plan)
            estimate = FiringEstimateOverride(
                cost=Decimal(str(line_snapshot.get("allocated_cost", "0"))),
                snapshot={
                    "estimated": True,
                    "kiln": kiln_snapshot,
                    "production": line_snapshot,
                    "batches": item_batches,
                    "firing_plan": item_plan,
                    "calculated_firing_days": item_days,
                }
                if line_snapshot
                else {"estimated": True},
                source_key=source_key,
                days=item_days,
            )

            calculation = None
            if item.quantity is not None:
                quotation_input = QuotationCalculateIn(
                    name=payload.name,
                    customer_id=payload.customer_id,
                    product_id=product.id,
                    quantity=item.quantity,
                    recipe_id=recipe_id,
                    recipe_version_id=version_id,
                    firing_line_id=item.firing_line_id,
                    materials_applied=item.materials_applied,
                    material_grams_per_piece=item.material_grams_per_piece,
                    techniques=item.techniques,
                    additionals=item.additionals,
                    days_adjustment=item.days_adjustment,
                    waiting_days=item.waiting_days,
                    # Fase 009E: los costos fijos son de la COTIZACION, no de
                    # la linea. Se pasa una lista vacia para que el motor
                    # tecnico no los sume aqui: antes cada linea cobraba el
                    # alquiler y el administrativo enteros, asi que anadir un
                    # segundo producto duplicaba el alquiler del taller.
                    other_costs=[],
                    markup_percent=item.markup_percent,
                    commercial_sale_unit_price=item.commercial_sale_unit_price,
                )
                calculation = (
                    await self._quotations.calculate(quotation_input)
                    if item.firing_line_id is not None
                    else await self._quotations.calculate_with_firing_estimate(
                        quotation_input, estimate
                    )
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
                        # Fase 009D: el plan de esmaltes NO entra en la
                        # huella. Todavia es una estimacion tecnica y no
                        # participa del costo comercial, asi que cambiar
                        # `estimated_glaze_percent` no debe bloquear con
                        # SOURCE_CHANGED una confirmacion que por lo demas
                        # sigue siendo valida. Se reevalua en 009E/009H,
                        # cuando el esmalte si sea autoridad de precio o de
                        # consumo fisico.
                        "input": item.model_dump(
                            mode="json",
                            exclude={"glazes", "glaze_unit", "glaze_selection_touched"},
                        ),
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
                    standard_width=product.width,
                    standard_height=product.height,
                    standard_length=product.length,
                    standard_depth=product.depth,
                    editable_dimensions=editable_dimensions,
                    dimensions_overridden=item.dimensions_overridden,
                    quantity=item.quantity,
                    recipe_id=recipe_id,
                    recipe_version_id=version_id,
                    recipe_version_fingerprint_snapshot=version_fingerprint,
                    recipe_auto_selected=auto_selected,
                    materials_applied_input=item.materials_applied,
                    material_grams_per_piece=item.material_grams_per_piece,
                    glaze_plan=GlazePlanOut.model_validate(glaze_plan) if glaze_plan else None,
                    glaze_unit=item.glaze_unit,
                    glaze_selection_touched=(item.glaze_selection_touched or bool(item.glazes)),
                    firing_id=calculation.firing_id if calculation else None,
                    firing_line_id=calculation.firing_line_id if calculation else None,
                    firing_code_snapshot=(
                        calculation.firing_code_snapshot if calculation else None
                    ),
                    kiln_id=item.factor_kiln_id or item.low_kiln_id or payload.kiln_id,
                    low_kiln_id=(
                        line_snapshot.get("low_kiln_id") if line_snapshot else item.low_kiln_id
                    )
                    or payload.kiln_id,
                    high_kiln_id=(
                        line_snapshot.get("high_kiln_id") if line_snapshot else item.high_kiln_id
                    )
                    or payload.kiln_id,
                    low_kiln_selected=item.low_kiln_selected,
                    high_kiln_selected=item.high_kiln_selected,
                    factor_kiln_id=(
                        line_snapshot.get("factor_kiln_id")
                        if line_snapshot
                        else item.factor_kiln_id
                    )
                    or payload.kiln_id,
                    production_snapshot={
                        **(
                            {
                                "source": "CONFIRMED_FIRING_LINE",
                                **calculation.firing_snapshot,
                            }
                            if calculation and item.firing_line_id is not None
                            else line_snapshot
                        ),
                        "materials_applied_input": item.materials_applied,
                        "commercial_sale_unit_price_input": item.commercial_sale_unit_price,
                        "dimensions_overridden": item.dimensions_overridden,
                        # La INTENCION se guarda tal cual el usuario la
                        # expreso. Derivarla despues de la simulacion la
                        # perderia en un borrador incompleto: ahi la
                        # simulacion sale temprano y no deja low/high_kiln_id,
                        # asi que duplicarlo apagaria ambas quemas.
                        "low_kiln_selected": item.low_kiln_selected,
                        "high_kiln_selected": item.high_kiln_selected,
                        "low_kiln_id_input": item.low_kiln_id,
                        "high_kiln_id_input": item.high_kiln_id,
                        "firing_batches": item_batches,
                        # Requisito 8: al confirmar hay que poder explicar los
                        # dias sin volver a consultar la configuracion, porque
                        # el horno puede cambiar de duracion despues.
                        "firing_plan": item_plan,
                        "calculated_firing_days": item_days,
                        # Clave con espacio de nombres propio, como firing_plan:
                        # dos planes legibles en vez de una sopa de claves
                        # sueltas junto a base_cost y occupancy_factor.
                        **({"glaze_plan": glaze_plan} if glaze_plan is not None else {}),
                        "glaze_unit": item.glaze_unit,
                        # Se guarda la INTENCION de haber elegido, no solo lo
                        # elegido: una lista vacia con la bandera puesta
                        # significa "no quiero esmalte", y sin ella el
                        # sugerido volveria en el siguiente recalculo.
                        "glaze_selection_touched": (
                            item.glaze_selection_touched or bool(item.glazes)
                        ),
                        "glazes_input": [
                            {
                                "preparation_id": glaze.preparation_id,
                                "prepared_product_id": glaze.prepared_product_id,
                                "share": glaze.share,
                            }
                            for glaze in item.glazes
                        ],
                    },
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
                    technical_cost=(
                        calculation.materials_applied
                        + calculation.firing_cost
                        + calculation.labor_cost
                        if calculation
                        else ZERO
                    ),
                    final_unit_cost=calculation.final_unit_cost if calculation else ZERO,
                    final_total_cost=calculation.final_total_cost if calculation else ZERO,
                    markup_percent=item.markup_percent,
                    calculated_sale_unit_price=(
                        calculation.calculated_sale_unit_price if calculation else ZERO
                    ),
                    suggested_commercial_unit_price=(
                        calculation.suggested_commercial_unit_price if calculation else ZERO
                    ),
                    commercial_sale_unit_price_input=item.commercial_sale_unit_price,
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
                    commercial_unit_price_with_tax=(
                        calculation.commercial_unit_price_with_tax if calculation else ZERO
                    ),
                    commercial_total=(calculation.commercial_total if calculation else ZERO),
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
                    warnings=_output_warnings(warnings),
                    complete=not BLOCKING_WARNING_CODES.intersection(warnings),
                    sort_order=position,
                )
            )

        # ------------------------------------------------------------------
        # Fase 009E: motor comercial. Va DESPUES del bucle porque el reparto
        # de costos fijos necesita ver todas las lineas: el peso de cada una
        # es su costo factorado sobre el total factorado de la cotizacion.
        # ------------------------------------------------------------------
        # Fase 009E: la politica sale de la configuracion comercial, no de una
        # constante del codigo. El override por cotizacion gana al default; el
        # paso de redondeo es politica de la empresa y no se sobreescribe desde
        # una cotizacion, para que nadie se salte Configuracion pieza a pieza.
        production_factor = payload.production_factor or settings.production_factor_default
        rounding_step = settings.rounding_step
        # Fase 009F: la moneda es intencion de la cotizacion. Configuracion
        # sigue dando el valor inicial, pero deja de ser la autoridad: dos
        # cotizaciones del mismo dia pueden emitirse en monedas distintas.
        currency_code, exchange_rate = _resolve_currency(payload, settings)
        total_fixed_cost = await self._total_fixed_cost()
        item_outputs = self._apply_commercial_engine(
            item_outputs,
            production_factor=production_factor,
            rounding_step=rounding_step,
            total_fixed_cost=total_fixed_cost,
            currency_code=currency_code,
            exchange_rate=exchange_rate,
        )

        subtotal = sum((item.line_total_net for item in item_outputs), ZERO)
        tax_amount = sum((item.line_total_tax for item in item_outputs), ZERO)
        # No se vuelve a redondear: el total es la suma de las lineas que el
        # propio documento enumera.
        total = sum((item.line_total_gross for item in item_outputs), ZERO)
        if production.get("source") == "CONFIRMED_FIRING_LINES":
            production = _confirmed_production_summary(production, item_outputs)
            firing_total = sum((item.firing_cost for item in item_outputs), ZERO)
            firing_tax_percentage = settings.tax_percent or ZERO
            firing_tax_amount = firing_total * firing_tax_percentage / Decimal(100)
            production.update(
                {
                    "total_cost": firing_total,
                    "tax_percentage": firing_tax_percentage,
                    "tax_amount": firing_tax_amount,
                    "total_with_tax": firing_total + firing_tax_amount,
                    "currency_code": settings.currency_code or "PEN",
                    "currency_symbol": settings.currency_symbol or "S/",
                }
            )
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
                # Se hashean los VALORES comerciales que el calculo usa de
                # verdad, no `version`/`updated_at` de la fila.
                #
                # Con la version, cualquier edicion de la configuracion
                # invalidaba todos los borradores: cambiar el texto de las
                # condiciones de pago, o los datos del banco, dejaba
                # cotizaciones correctas sin poder confirmarse con
                # SOURCE_CHANGED. Con 009D eso ademas contradice la regla
                # explicita de que tocar `estimated_glaze_percent` no puede
                # bloquear una confirmacion mientras el esmalte no sea
                # autoridad de precio.
                #
                # Estos cuatro son los unicos campos de la configuracion que
                # entran en el importe (ver los usos de `settings.` mas abajo);
                # cambiar cualquiera de ellos SIGUE invalidando la huella, que
                # es justo lo que debe hacer.
                "settings": [
                    settings.tax_percent,
                    settings.default_quotation_factor,
                    settings.currency_code,
                    settings.currency_symbol,
                ],
                # Fase 009E: la politica EFECTIVA, no la version de la fila.
                # Cambiar el factor por defecto o el paso de redondeo si altera
                # el precio y debe invalidar el borrador; editar el banco o las
                # condiciones de pago no.
                # Fase 009F: la moneda y la tasa cambian el precio, asi que
                # entran en la huella. Un borrador guardado a 3,75 no puede
                # confirmarse en silencio con otra tasa.
                "pricing": [
                    production_factor,
                    rounding_step,
                    total_fixed_cost,
                    currency_code,
                    exchange_rate,
                ],
                "production": production,
                "items": [item.source_fingerprint for item in item_outputs],
            }
        )
        if len(tax_rates) == 1:
            header_tax_percentage = next(iter(tax_rates))
        elif subtotal:
            # Una cotizacion puede combinar productos gravados y exentos. El
            # encabezado conserva una tasa efectiva representativa mientras
            # cada item mantiene su tasa exacta y el origen queda como MIXED.
            header_tax_percentage = (tax_amount * Decimal(100) / subtotal).quantize(
                Decimal("0.000001")
            )
        else:
            header_tax_percentage = ZERO

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
            quotation_net_total=subtotal,
            quotation_tax_total=tax_amount,
            quotation_gross_total=total,
            production_factor=production_factor,
            rounding_step=rounding_step,
            total_fixed_cost=total_fixed_cost,
            tax_percentage_snapshot=header_tax_percentage,
            tax_rate_source_snapshot=(
                next(iter(tax_sources))
                if len(tax_rates) == 1 and len(tax_sources) == 1
                else "MIXED"
            ),
            tax_amount=tax_amount,
            total_with_tax=total,
            currency_code_snapshot=currency_code,
            currency_symbol_snapshot=_currency_symbol(currency_code, settings),
            exchange_rate_snapshot=exchange_rate,
            exchange_rate_source_snapshot=EXCHANGE_RATE_SOURCE if exchange_rate else None,
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
        row.firing_snapshot = jsonable_encoder(preview.production_summary)
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
        row.exchange_rate_snapshot = preview.exchange_rate_snapshot
        row.exchange_rate_source_snapshot = preview.exchange_rate_source_snapshot
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
                    production_snapshot=jsonable_encoder(
                        {
                            **item.production_snapshot,
                            # Fase 009E. Politica y resultado comercial de la
                            # linea. Al confirmar queda congelado: cambiar
                            # despues el IGV, el factor o los costos fijos no
                            # puede mover una cotizacion ya cerrada.
                            "commercial_plan": {
                                # Fase 009F. La moneda y la tasa se copian aqui
                                # como TRAZA, no como autoridad: la autoridad
                                # sigue siendo la cabecera, y sigue habiendo una
                                # sola tasa por cotizacion. Sin esta copia, una
                                # linea confirmada no puede explicar de donde
                                # salio su precio sin reconstruirlo.
                                "currency": item.currency_code_snapshot,
                                "exchange_rate": item.exchange_rate_snapshot,
                                "exchange_rate_source": (
                                    EXCHANGE_RATE_SOURCE
                                    if item.exchange_rate_snapshot is not None
                                    else None
                                ),
                                "production_factor": item.production_factor,
                                "rounding_step": item.rounding_step,
                                "rounding_mode": "CEILING",
                                "technical_cost": item.technical_cost,
                                "factored_cost": item.factored_cost,
                                "fixed_cost_allocation": item.fixed_cost_allocation,
                                "commercial_base_cost": item.commercial_base_cost,
                                "commercial_base_unit_cost": item.commercial_base_unit_cost,
                                "markup_percent": item.markup_percent,
                                "tax_percent": item.tax_percentage_snapshot,
                                # El neto ANTES de convertir, siempre en PEN.
                                # Es lo que permite explicar el precio en
                                # dolares sin rehacer la cuenta.
                                "raw_net_unit_base": item.raw_net_unit_base,
                                "raw_net_unit": item.raw_net_unit,
                                "raw_tax_unit": item.raw_tax_unit,
                                "raw_gross_unit": item.raw_gross_unit,
                                "rounding_adjustment_unit": item.rounding_adjustment_unit,
                                "final_gross_unit": item.final_gross_unit,
                                "final_net_unit": item.final_net_unit,
                                "final_tax_unit": item.final_tax_unit,
                                "quantity": item.quantity,
                                "line_total_net": item.line_total_net,
                                "line_total_tax": item.line_total_tax,
                                "line_total_gross": item.line_total_gross,
                            },
                        }
                    ),
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
        # Fase 009G. La vigencia se congela aqui y solo aqui: es el unico
        # instante en que existe la cotizacion confirmada y todavia se sabe que
        # decia la configuracion. Leerla al generar el PDF hacia que cambiar el
        # ajuste moviera la vigencia de documentos ya entregados.
        row.validity_days_snapshot = (
            await self._quotations.commercial_settings()
        ).quote_validity_days
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
            # Fase 009E: la politica comercial vuelve como ENTRADA. Sin esto,
            # confirmar recalcularia con el factor por defecto y el override
            # guardado se perderia justo en el momento de congelarlo.
            production_factor=next(
                (
                    factor
                    for item in row.items
                    if (
                        factor := _stored_production_factor(
                            item.production_snapshot.get("commercial_plan")
                        )
                    )
                ),
                None,
            ),
            # Fase 009F: la moneda y la tasa tambien vuelven como ENTRADA.
            #
            # Al CONFIRMAR importa porque el recalculo de deriva tiene que
            # usar la tasa guardada: si volviera al default, la huella no
            # coincidiria y confirmar una cotizacion en dolares seria
            # imposible. Al DUPLICAR importa porque el borrador nuevo hereda
            # la intencion —misma moneda, misma tasa— y desde ahi es editable.
            currency_code=_supported_currency(row.currency_code_snapshot),
            exchange_rate=row.exchange_rate_snapshot,
            items=[
                QuotationBuilderItemIn(
                    product_id=item.product_id,
                    quantity=item.quantity,
                    # _to_input reproduce fielmente el ultimo estado guardado
                    # (para duplicate() y para la re-verificacion de deriva
                    # de confirm()) — siempre se re-envian las dimensiones
                    # congeladas de la linea, esten o no personalizadas. El
                    # "refresco" de modo estandar contra un maestro que
                    # cambio (ver seccion "Cambio del maestro en Draft" del
                    # spec) ocurre solo cuando el usuario mismo vuelve a
                    # guardar el borrador desde el wizard enviando
                    # `dimensions` vacio — no aqui.
                    dimensions=ProductDimensionCompletionIn(
                        width=item.product_width_snapshot,
                        height=item.product_height_snapshot,
                        length=item.product_length_snapshot,
                        depth=item.product_depth_snapshot,
                    ),
                    dimensions_overridden=bool(
                        item.production_snapshot.get("dimensions_overridden")
                    ),
                    recipe_id=item.recipe_id,
                    recipe_version_id=item.recipe_version_id,
                    materials_applied=item.production_snapshot.get("materials_applied_input"),
                    material_grams_per_piece=item.material_grams_per_piece,
                    firing_line_id=(
                        item.production_snapshot.get("firing_line_id")
                        if item.production_snapshot.get("source") == "CONFIRMED_FIRING_LINE"
                        else None
                    ),
                    low_kiln_id=item.production_snapshot.get(
                        "low_kiln_id_input", item.production_snapshot.get("low_kiln_id")
                    ),
                    high_kiln_id=item.production_snapshot.get(
                        "high_kiln_id_input", item.production_snapshot.get("high_kiln_id")
                    ),
                    # Fase 009C: se lee la INTENCION guardada, no se deduce de
                    # si quedo un horno. Deducirla rompe el borrador
                    # incompleto: ahi la simulacion sale temprano y no deja
                    # low/high_kiln_id, asi que duplicarlo apagaria ambas
                    # quemas y lo dejaria con FIRING_REQUIRED. Los borradores
                    # anteriores a 009C no tienen el flag: para ellos se cae a
                    # la presencia del horno, que es como se guardaban.
                    low_kiln_selected=bool(
                        item.production_snapshot.get(
                            "low_kiln_selected",
                            item.production_snapshot.get("low_kiln_id") is not None,
                        )
                    ),
                    high_kiln_selected=bool(
                        item.production_snapshot.get(
                            "high_kiln_selected",
                            item.production_snapshot.get("high_kiln_id") is not None,
                        )
                    ),
                    factor_kiln_id=item.production_snapshot.get("factor_kiln_id") or item.kiln_id,
                    # Fase 009D: se reconstruyen las ENTRADAS del usuario, no
                    # los derivados. Los gramos y mililitros guardados son el
                    # resultado de un calculo, no una eleccion: reinyectarlos
                    # como entrada los congelaria en un borrador que deberia
                    # recalcularse con la configuracion vigente.
                    glazes=_glaze_inputs(item.production_snapshot),
                    glaze_unit=(
                        item.production_snapshot.get("glaze_unit", "g")
                        if item.production_snapshot.get("glaze_unit") in ("g", "ml")
                        else "g"
                    ),
                    glaze_selection_touched=bool(
                        item.production_snapshot.get("glaze_selection_touched", False)
                    ),
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
                    commercial_sale_unit_price=item.production_snapshot.get(
                        "commercial_sale_unit_price_input",
                        item.commercial_sale_unit_price,
                    ),
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
        )
        firing_complete = bool(
            item.kiln_id or item.production_snapshot.get("source") == "CONFIRMED_FIRING_LINE"
        )
        materials_complete = bool(
            item.production_snapshot.get("materials_applied_input") is not None
            or (
                item.recipe_id is not None
                and item.recipe_version_id is not None
                and item.material_grams_per_piece is not None
            )
        )
        return (
            all(value is not None for value in required)
            and firing_complete
            and materials_complete
            and not BLOCKING_WARNING_CODES.intersection(item.calculation_warnings)
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
                # item.product es lazy="joined": el maestro VIGENTE viaja sin
                # consulta extra, para que la UI pueda restaurar el estandar
                # al desactivar "Personalizar medidas".
                standard_width=item.product.width if item.product else None,
                standard_height=item.product.height if item.product else None,
                standard_length=item.product.length if item.product else None,
                standard_depth=item.product.depth if item.product else None,
                # Que una medida sea editable depende del MAESTRO, no de lo
                # que esta linea guardo. Mirar el snapshot hacia que una medida
                # propia de la cotizacion pareciera venir del maestro: la UI la
                # protegia, dejaba de enviarla, y el recalculo caia al maestro
                # -que no la tiene- y pedia las medidas otra vez. El borrador
                # se completaba en pantalla y se rompia al reabrirlo.
                #
                # Es el mismo criterio que usa `preview`, y tiene que serlo:
                # dos reglas para lo mismo se separan en cuanto una cambia.
                editable_dimensions=(
                    []
                    if row.status is not QuotationStatus.DRAFT
                    else [
                        field
                        for field in ALL_DIMENSIONS
                        if item.product is None or getattr(item.product, field) is None
                    ]
                ),
                dimensions_overridden=bool(item.production_snapshot.get("dimensions_overridden")),
                quantity=item.quantity,
                recipe_id=item.recipe_id,
                recipe_version_id=item.recipe_version_id,
                recipe_version_fingerprint_snapshot=(item.recipe_version_fingerprint_snapshot),
                materials_applied_input=item.production_snapshot.get("materials_applied_input"),
                material_grams_per_piece=item.material_grams_per_piece,
                firing_id=item.production_snapshot.get("firing_id"),
                firing_line_id=item.production_snapshot.get("firing_line_id"),
                firing_code_snapshot=item.production_snapshot.get("firing_code"),
                kiln_id=item.kiln_id,
                low_kiln_id=item.production_snapshot.get("low_kiln_id") or item.kiln_id,
                high_kiln_id=item.production_snapshot.get("high_kiln_id") or item.kiln_id,
                low_kiln_selected=bool(
                    item.production_snapshot.get(
                        "low_kiln_selected",
                        item.production_snapshot.get("low_kiln_id") is not None,
                    )
                ),
                high_kiln_selected=bool(
                    item.production_snapshot.get(
                        "high_kiln_selected",
                        item.production_snapshot.get("high_kiln_id") is not None,
                    )
                ),
                factor_kiln_id=item.production_snapshot.get("factor_kiln_id") or item.kiln_id,
                production_snapshot=_confirmed_item_snapshot(item.production_snapshot),
                # CONFIRMED lee el snapshot guardado y no vuelve a calcular:
                # cambiar despues el porcentaje, la receta o el lote no puede
                # mover una cotizacion ya cerrada.
                **_stored_commercial_plan(item.production_snapshot),
                glaze_plan=_stored_glaze_plan(item.production_snapshot),
                glaze_unit=str(item.production_snapshot.get("glaze_unit", "g")),
                glaze_selection_touched=bool(
                    item.production_snapshot.get("glaze_selection_touched", False)
                ),
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
                commercial_sale_unit_price_input=item.production_snapshot.get(
                    "commercial_sale_unit_price_input",
                    item.commercial_sale_unit_price,
                ),
                commercial_sale_unit_price=item.commercial_sale_unit_price,
                effective_profit_unit=item.effective_profit_unit,
                effective_profit_total=item.effective_profit_total,
                effective_markup_percent=item.effective_markup_percent,
                commercial_subtotal=item.commercial_subtotal,
                commercial_unit_price_with_tax=(
                    (item.commercial_subtotal + item.tax_amount) / Decimal(item.quantity)
                    if item.quantity
                    else ZERO
                ),
                commercial_total=item.commercial_subtotal + item.tax_amount,
                tax_percentage_snapshot=item.tax_percentage_snapshot,
                tax_rate_source_snapshot=item.tax_rate_source_snapshot,
                tax_amount=item.tax_amount,
                source_fingerprint=item.source_fingerprint,
                warnings=_output_warnings(item.calculation_warnings),
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
            production_summary=_confirmed_production_summary(row.firing_snapshot, item_outputs),
            items=item_outputs,
            item_count=len(item_outputs),
            commercial_subtotal=row.commercial_subtotal,
            quotation_net_total=sum((item.line_total_net for item in item_outputs), ZERO),
            quotation_tax_total=sum((item.line_total_tax for item in item_outputs), ZERO),
            quotation_gross_total=sum((item.line_total_gross for item in item_outputs), ZERO),
            production_factor=next(
                (item.production_factor for item in item_outputs if item.production_factor),
                ZERO,
            ),
            rounding_step=next(
                (item.rounding_step for item in item_outputs if item.rounding_step), ZERO
            ),
            tax_percentage_snapshot=row.tax_percentage_snapshot,
            tax_rate_source_snapshot=row.tax_rate_source_snapshot,
            tax_amount=row.tax_amount,
            total_with_tax=row.total_with_tax,
            currency_code_snapshot=row.currency_code_snapshot,
            currency_symbol_snapshot=row.currency_symbol_snapshot,
            exchange_rate_snapshot=row.exchange_rate_snapshot,
            exchange_rate_source_snapshot=row.exchange_rate_source_snapshot,
            warnings=_output_warnings(row.calculation_warnings),
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

    async def render_pdf_preview(self, payload: QuotationBuilderDraftIn) -> tuple[bytes, str]:
        """Genera la previsualizacion comercial en PDF de un borrador en memoria sin persistir."""
        calculated = await self.preview(payload)
        if not calculated.items or not calculated.complete:
            raise QuotationBuilderIncompleteError(
                "La cotización debe estar completa para generar la vista previa"
            )

        customer: Partner | None = None
        if calculated.customer_id:
            customer = await self._session.get(Partner, calculated.customer_id)

        return await self._pdf.render_draft_pdf(calculated, customer=customer)

    async def get_pdf_preview(self, quotation_id: int) -> tuple[bytes, str]:
        """Genera la previsualizacion comercial en PDF de un borrador guardado por ID."""
        quotation = await self._get(quotation_id)
        if quotation.status is not QuotationStatus.DRAFT:
            return await self._pdf.get_quotation_pdf(quotation_id)

        calculated = await self.get(quotation_id)
        if not calculated.items or not calculated.complete:
            raise QuotationBuilderIncompleteError(
                "La cotización debe estar completa para generar la vista previa"
            )

        customer: Partner | None = None
        if calculated.customer_id:
            customer = await self._session.get(Partner, calculated.customer_id)

        return await self._pdf.render_draft_pdf(calculated, customer=customer)
