"""Precio del Cotizador V2: de los costos al total que ve el cliente.

Fase 010F. Es el ultimo eslabon: aqui no se mide, ni se pesa, ni se enciende
nada. Se toma lo que 010C, 010D y 010E dejaron congelado y se convierte en un
precio.

## Las dos bases, y por que son dos

**Costo real** es lo que sale del bolsillo: el GAS que de verdad se quema.
**Costo de produccion** es la base comercial: la TARIFA que el taller cobra por
encender el horno. Los demas componentes son los mismos. Intercambiarlos no
rompe nada visible —los dos son numeros creibles— y deja el margen invertido.

## Tres bases de reparto, no una

Los productos son independientes en lo suyo y comparten lo del taller. Cada
costo general se reparte por lo que de verdad lo consume:

- la **quema**, por VOLUMEN. Ya venia repartida desde 010E: el horno se llena
  de sitio, no de tiempo ni de dinero;
- el **espacio**, por HORAS de trabajo. El taller se ocupa por tiempo;
- lo **general** —administracion, ilustracion y el personal que apoya al pedido
  entero sin producto asignado—, por COSTO DIRECTO. Acompana al dinero.

Cuando una base no existe —ninguna linea tiene horas, por ejemplo— se reparte
por cantidad de piezas antes que dejar el costo sin repartir. Es lo que hace el
modelo aprobado, y la alternativa seria perder el importe.

## El orden de las operaciones es la fase entera

    costo directo + generales repartidos = costo de produccion asignado
                                         x factor global
                                         = precio de la linea
                                         / cantidad = unitario
                                         redondeado hacia arriba
                                         x cantidad = subtotal
                                         + IGV      = total

**El factor es UNO por cotizacion.** No hay un x3 para la jarra y un x2.5 para
el vaso: eso no es negociar, es emitir dos documentos con la misma cabecera.

**El IGV va al final.** No es ingreso del taller: multiplicarlo por el factor
cobraria al cliente el impuesto triplicado.

**El subtotal se RECONSTRUYE** sumando las lineas ya redondeadas. Tomar el
precio global anterior al redondeo daria un total que no coincide con los
unitarios que el cliente esta leyendo, y el documento no cuadraria al sumarlo
a mano.

## Una divergencia consciente con el Excel de referencia

El modelo en Excel lleva la ilustracion por PRODUCTO. En el sistema es una sola
por cotizacion: lo fijo la regla aprobada de 010D —«la ilustracion es una sola
por cotizacion y no una tecnica mas»— y esa regla manda sobre el Excel. Aqui se
trata, por tanto, como un costo general y se reparte por costo directo. Los
totales de la cotizacion coinciden con el Excel; lo que cambia es a que linea
se le carga cada parte.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.quoter_v2_config import DEFAULT_WORKDAY_HOURS
from app.core.quoter_v2_pricing import (
    PricingMathError,
    allocate_by_weight,
    apply_factor,
    ceil_to_step,
    persisted_margin_percent,
    quantize_money,
    tax_amount,
    to_base_currency,
    unit_price,
    within_factor_range,
)
from app.core.quoter_v2_rules_010p import (
    ExternalLaborCost,
    ExternalWorkerSnapshot,
    RulesMathError,
    allocate_by_active_minutes,
    external_labor_cost,
    line_active_minutes,
    minutes_to_hours,
    order_active_minutes,
    space_cost,
    space_cost_per_hour,
    wholesale_suggested,
)
from app.models.audit import AuditAction
from app.models.quoter_v2 import V2Quotation, V2QuotationProduct, V2QuotationStatus
from app.models.quoter_v2_labor import (
    V2QuotationLabor,
    V2QuotationWorker,
    V2Worker,
    V2WorkerType,
)
from app.models.quoter_v2_processes import V2QuotationExtra
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder
from app.services.quoter_v2_locks import lock_quotation_for_recalculation

ZERO = Decimal(0)

#: Entidad de auditoria propia. Mover el factor comercial de una cotizacion no
#: es lo mismo que cambiarle un material o un horno.
V2_PRICING_ENTITY = "v2_quotation_pricing"

#: Avisos. Ninguno bloquea: un borrador a medias tiene que poder guardarse.
WARN_NO_FACTOR = "V2_PRICING_FACTOR_NOT_SET"
WARN_NO_TAX = "V2_PRICING_TAX_NOT_SET"
WARN_NO_ROUNDING = "V2_PRICING_ROUNDING_NOT_SET"
WARN_NO_LINES = "V2_PRICING_NO_LINES"
WARN_NO_COST = "V2_PRICING_NO_COST"
WARN_WORK_DAYS_NOT_SET = "V2_PRICING_WORK_DAYS_NOT_SET"
WARN_LINE_WITHOUT_QUANTITY = "V2_PRICING_LINE_WITHOUT_QUANTITY"
WARN_SELLING_BELOW_COST = "V2_PRICING_SELLING_BELOW_REAL_COST"
# Fase 010P.
WARN_LINE_TIME_MISSING = "V2_PRICING_LINE_TIME_MISSING"
WARN_PASSIVE_TIME_AVAILABLE = "V2_PRICING_PASSIVE_TIME_AVAILABLE"
WARN_WHOLESALE_THRESHOLD_EXCEEDED = "V2_WHOLESALE_THRESHOLD_EXCEEDED"
#: Desde esta version de reglas el costo sale del tiempo del pedido (010P).
PRICING_RULES_V2 = 2
EMPTY_EXTERNAL_LABOR = ExternalLaborCost(commercial=ZERO, real=ZERO, gap=ZERO, per_worker=())


class V2PricingQuotationNotFoundError(APIError):
    status_code = 404
    code = "V2_PRICING_QUOTATION_NOT_FOUND"
    message = "La cotizacion V2 no existe"


class V2PricingNotEditableError(APIError):
    status_code = 409
    code = "V2_PRICING_QUOTATION_NOT_EDITABLE"
    message = "La cotizacion ya no es un borrador y su precio no puede cambiarse"


class V2FactorOutOfRangeError(APIError):
    """Un factor fuera del rango que ESTA cotizacion congelo.

    El suelo de x2 es una regla cerrada del negocio, no una preferencia: por
    debajo no se vende sin una autorizacion que todavia no existe.
    """

    status_code = 422
    code = "V2_PRICING_FACTOR_OUT_OF_RANGE"
    message = "El factor comercial esta fuera del rango permitido"


class V2PricingInputInvalid(APIError):
    status_code = 422
    code = "V2_PRICING_INPUT_INVALID"
    message = "Los datos no permiten calcular un precio"


@dataclass(frozen=True)
class PricingState:
    """Todo lo que hay que saber del precio de una cotizacion."""

    quotation: V2Quotation
    lines: list[V2QuotationProduct]
    warnings: list[str]
    #: Fase 010P. El personal externo persona por persona (comercial y real).
    external_labor: ExternalLaborCost = EMPTY_EXTERNAL_LABOR
    #: Unidades del pedido ENTERO y si se sugiere pasar a por mayor.
    total_units: int = 0
    wholesale_suggested: bool = False
    #: Nombres de los externos, para presentar el desglose.
    worker_names: dict[int, str] = field(default_factory=dict)


async def _lines_of(session: AsyncSession, quotation_id: int) -> list[V2QuotationProduct]:
    consulta = (
        select(V2QuotationProduct)
        .where(V2QuotationProduct.v2_quotation_id == quotation_id)
        .order_by(V2QuotationProduct.sort_order, V2QuotationProduct.id)
    )
    return list((await session.scalars(consulta)).all())


async def _labor_by_line(
    session: AsyncSession, quotation_id: int
) -> tuple[dict[int, Decimal], dict[int, Decimal], Decimal]:
    """Costo y horas de mano de obra por linea, y el costo TOTAL.

    El total incluye las tareas SIN producto asignado —el personal que apoya al
    pedido entero, que 010D permite explicitamente—. Esas no caben en ninguna
    linea, asi que se reparten despues como costo general: dejarlas fuera haria
    que la suma de las lineas no llegara al costo de la cotizacion.
    """
    filas = (
        await session.execute(
            select(
                V2QuotationLabor.v2_quotation_product_id,
                func.sum(V2QuotationLabor.labor_cost),
                func.sum(V2QuotationLabor.final_hours),
            )
            .where(V2QuotationLabor.v2_quotation_id == quotation_id)
            .group_by(V2QuotationLabor.v2_quotation_product_id)
        )
    ).all()

    costos: dict[int, Decimal] = {}
    horas: dict[int, Decimal] = {}
    total = ZERO
    for product_id, costo, hora in filas:
        costo = Decimal(costo or 0)
        total += costo
        if product_id is None:
            continue
        costos[int(product_id)] = costo
        horas[int(product_id)] = Decimal(hora or 0)
    return costos, horas, total


async def _extras_by_line(
    session: AsyncSession, quotation_id: int
) -> tuple[dict[int, Decimal], Decimal, Decimal]:
    """Adicionales por linea, los del pedido entero, y el total.

    Un adicional puede colgar de una pieza —un molde para esa taza— o del pedido
    —el empaque de toda la entrega—. Los primeros van al costo directo de su
    linea; los segundos se reparten como lo general, igual que la
    administracion. El total es lo que el resumen muestra como «adicionales».
    """
    filas = (
        await session.execute(
            select(
                V2QuotationExtra.v2_quotation_product_id,
                func.sum(V2QuotationExtra.total_cost),
            )
            .where(V2QuotationExtra.v2_quotation_id == quotation_id)
            .group_by(V2QuotationExtra.v2_quotation_product_id)
        )
    ).all()

    por_linea: dict[int, Decimal] = {}
    generales = ZERO
    total = ZERO
    for product_id, costo in filas:
        costo = Decimal(costo or 0)
        total += costo
        if product_id is None:
            generales += costo
            continue
        por_linea[int(product_id)] = costo
    return por_linea, generales, total


def _pesos(preferidos: list[Decimal], cantidades: list[Decimal]) -> list[Decimal]:
    """La base con la que repartir un costo general, con sus dos reservas.

    Se prefiere la base propia —horas para el espacio, costo directo para lo
    general—. Si no existe, la cantidad de piezas. Y si tampoco hay piezas, a
    PARTES IGUALES.

    La tercera reserva no es un detalle: sin ella, una cotizacion cuyas lineas
    estan todas a cantidad cero —un borrador recien empezado con los dias ya
    decididos— dejaba el espacio y la administracion en la cabecera sin llegar
    a ninguna linea, y la suma de lo repartido dejaba de ser el total. El
    importe es real y es de la cotizacion: sin una base que prefiera a una
    linea sobre otra, repartirlo por igual es lo menos arbitrario que se puede
    hacer, y lo unico que conserva la integridad.
    """
    if sum(preferidos, ZERO) > ZERO:
        return preferidos
    if sum(cantidades, ZERO) > ZERO:
        return cantidades
    return [Decimal(1) for _ in preferidos]


def _reset_pricing(quotation: V2Quotation, lines: list[V2QuotationProduct]) -> None:
    """Deja el precio en cero. Sin factor no hay precio que valga."""
    for campo in (
        "price_min",
        "price_target",
        "negotiated_price",
        "subtotal_amount",
        "tax_amount",
        "total_amount",
        "rounding_adjustment",
        "estimated_profit",
        "effective_margin_percent",
    ):
        setattr(quotation, campo, ZERO)
    for linea in lines:
        for campo in (
            "line_price",
            "unit_price_raw",
            "unit_price",
            "line_subtotal",
            "line_tax",
            "line_total",
            "allocated_profit",
        ):
            setattr(linea, campo, ZERO)


async def refresh_pricing(session: AsyncSession, quotation: V2Quotation) -> list[str]:
    """Recalcula el precio entero de una cotizacion y lo deja escrito.

    Se llama al tocar el precio y tambien al tocar cualquier cosa que entre en
    el: una linea, una tarea o el horno. Un precio que no se recalculara al
    anadir una pieza seria un precio de otra cotizacion.

    **Solo escribe sobre borradores.** Una cotizacion emitida conserva lo que
    congelo. Devuelve avisos, nunca excepciones, para lo que no impide
    calcular.
    """
    # `no_autoflush` por el mismo motivo que en 010E: entre poner las
    # cantidades y poner los importes hay consultas, y un flush intermedio
    # escribiria una fila que no cumple sus propios CHECK —una linea sin piezas
    # con subtotal, por ejemplo—. El flush explicito del final ya ve el estado
    # coherente.
    with session.no_autoflush:
        lineas = await _lines_of(session, quotation.id)
        if quotation.status is not V2QuotationStatus.DRAFT:
            return []
        return await _recalculate(session, quotation, lineas)


async def _costs_v1(
    session: AsyncSession, quotation: V2Quotation, lineas: list[V2QuotationProduct]
) -> list[str]:
    """Costos con las reglas ANTERIORES a 010P (`pricing_rules_version = 1`).

    Solo lo emitido conserva la version 1, y lo emitido no se recalcula: se
    mantiene para que un borrador v1 que quedara en alguna base siga
    explicandose con las reglas con las que nacio.
    """
    avisos: list[str] = []

    # ---- 1. Lo que cuesta cada cosa ----------------------------------
    costos_mo, horas_mo, mano_de_obra_total = await _labor_by_line(session, quotation.id)

    ilustracion = quotation.illustration_cost
    dias = quotation.effective_work_days
    if dias is None:
        # El espacio se cobra por dias EFECTIVOS de taller, y eso lo decide una
        # persona (010D). Sin decision no se inventa un numero: se avisa y el
        # espacio no entra todavia.
        avisos.append(WARN_WORK_DAYS_NOT_SET)
    espacio = Decimal(dias or 0) * (quotation.space_service_cost_per_day_snapshot or ZERO)
    administracion = quotation.administrative_cost_snapshot or ZERO

    quema_comercial = quotation.firing_commercial_total
    gas_real = quotation.firing_gas_total

    # Los adicionales del Excel (hoja «Cotizador V2», B25): empaque especial,
    # molde, sello. Suman al Costo de Produccion Y al Costo Real, porque son
    # dinero que sale de verdad. Los que cuelgan de una pieza van a su costo
    # directo; los del pedido entero, a lo general, como la administracion.
    extras_por_linea, extras_generales, extras_total = await _extras_by_line(session, quotation.id)

    materiales_total = ZERO
    for linea in lineas:
        directo = linea.body_cost + linea.glaze_cost
        materiales_total += directo
        # Fase 010J. La ilustracion de ESTE producto es costo directo suyo
        # (hoja «Productos», Z = materiales + MO + ilustracion). La que no se
        # asigno a ningun producto sigue yendo a lo general.
        linea.direct_cost = (
            directo
            + costos_mo.get(linea.id, ZERO)
            + extras_por_linea.get(linea.id, ZERO)
            + (linea.illustration_cost or ZERO)
        )

    directo_total = sum((linea.direct_cost for linea in lineas), ZERO)
    # Lo que no cabe en ninguna linea: la administracion, la ilustracion —que
    # es una sola por cotizacion— y el personal que apoya al pedido entero.
    mano_de_obra_sin_asignar = mano_de_obra_total - sum(costos_mo.values(), ZERO)
    general_total = administracion + ilustracion + mano_de_obra_sin_asignar + extras_generales

    quotation.materials_cost_total = materiales_total
    quotation.labor_cost_total = mano_de_obra_total
    quotation.extras_cost_total = extras_total
    quotation.space_cost = espacio
    quotation.direct_cost_total = directo_total
    quotation.production_cost_total = directo_total + quema_comercial + espacio + general_total
    quotation.real_cost_total = directo_total + gas_real + espacio + general_total

    # ---- 2. Repartir lo general entre las lineas ----------------------
    # El espacio por HORAS y lo general por COSTO DIRECTO, con las bases de
    # reserva que hacen falta para que NUNCA se pierda un importe.
    cantidades = [Decimal(linea.quantity) for linea in lineas]
    pesos_horas = _pesos([horas_mo.get(linea.id, ZERO) for linea in lineas], cantidades)
    pesos_directo = _pesos([linea.direct_cost for linea in lineas], cantidades)

    espacios = allocate_by_weight(espacio, pesos_horas)
    generales = allocate_by_weight(general_total, pesos_directo)
    for linea, espacio_linea, general_linea in zip(lineas, espacios, generales, strict=True):
        linea.allocated_space_cost = espacio_linea
        linea.allocated_general_cost = general_linea
        # La quema ya venia repartida por volumen desde 010E. Reutilizarla, y
        # no volver a repartirla aqui, es lo que impide que dos fases den dos
        # numeros distintos para lo mismo.
        linea.allocated_production_cost = (
            linea.direct_cost + linea.firing_commercial_cost + espacio_linea + general_linea
        )
        linea.allocated_real_cost = (
            linea.direct_cost + linea.firing_gas_cost + espacio_linea + general_linea
        )

    if not lineas:
        avisos.append(WARN_NO_LINES)
    if quotation.production_cost_total <= ZERO:
        avisos.append(WARN_NO_COST)

    return avisos


async def external_workers_of(
    session: AsyncSession, quotation: V2Quotation
) -> list[ExternalWorkerSnapshot]:
    """Los EXTERNOS DISTINTOS asignados hoy a la cotizacion, con su tarifa congelada.

    Una persona en tres procesos es UNA persona: cuenta una vez, con UN jornal.
    La tarifa sale de `v2_quotation_workers` (congelada en su primera
    asignacion); una tarea sin esa fila —anterior a 0043— usa el congelado de
    su tarea mas reciente. Quien ya no tiene tareas no cuenta.
    """
    tareas = (
        await session.execute(
            select(
                V2QuotationLabor.worker_id,
                V2QuotationLabor.worker_type_snapshot,
                V2QuotationLabor.daily_rate_snapshot,
                V2QuotationLabor.workday_hours_snapshot,
            )
            .where(V2QuotationLabor.v2_quotation_id == quotation.id)
            .order_by(V2QuotationLabor.id.desc())
        )
    ).all()
    congelados = {
        fila.worker_id: fila
        for fila in (
            await session.scalars(
                select(V2QuotationWorker).where(V2QuotationWorker.v2_quotation_id == quotation.id)
            )
        ).all()
    }
    externos: dict[int, ExternalWorkerSnapshot] = {}
    for worker_id, tipo, jornal, jornada in tareas:
        if worker_id in externos:
            continue
        fila = congelados.get(worker_id)
        if fila is not None:
            tipo, jornal, jornada = (
                fila.worker_type_snapshot,
                fila.daily_rate_snapshot,
                fila.workday_hours_snapshot,
            )
        if tipo is not V2WorkerType.EXTERNAL:
            continue
        externos[worker_id] = ExternalWorkerSnapshot(
            worker_id=worker_id, daily_rate=jornal, workday_hours=jornada
        )
    return [externos[clave] for clave in sorted(externos)]


def effective_space_cost_per_hour(quotation: V2Quotation) -> Decimal:
    """El costo de espacio por hora de ESTA cotizacion: el acuerdo o el congelado."""
    if quotation.space_cost_per_hour_override is not None:
        return quotation.space_cost_per_hour_override
    if quotation.space_cost_per_hour_snapshot is not None:
        return quotation.space_cost_per_hour_snapshot
    # Una cotizacion sin el congelado por hora (anterior a 0042) lo deriva de
    # lo que si congelo: su costo por dia y su jornada.
    dia = quotation.space_service_cost_per_day_snapshot
    jornada = quotation.workday_hours_snapshot
    if dia is None or jornada is None or jornada <= ZERO:
        return ZERO
    return space_cost_per_hour(dia, jornada)


def workday_hours_of(quotation: V2Quotation) -> Decimal:
    jornada = quotation.workday_hours_snapshot
    return jornada if jornada is not None and jornada > ZERO else DEFAULT_WORKDAY_HOURS


def total_units_of(lineas: list[V2QuotationProduct]) -> int:
    """Unidades del pedido ENTERO: lo que mira el umbral por mayor (P4)."""
    return sum((linea.quantity for linea in lineas), 0)


async def _costs_v2(
    session: AsyncSession, quotation: V2Quotation, lineas: list[V2QuotationProduct]
) -> list[str]:
    """Costos con las reglas de 010P (`pricing_rules_version = 2`).

    1. Tiempo: cada linea, ceil(cantidad/moldes) x minutos por unidad; el
       pedido, el MAXIMO de sus lineas (productos distintos van en paralelo).
    2. Personal: el interno no cuesta; cada externo DISTINTO cuesta horas x
       jornal/jornada al cliente (comercial) y jornales enteros al taller
       (real). La tarea ya no lleva costo propio: el costo es del pedido.
    3. Espacio: horas ACTIVAS x costo por hora. El tiempo pasivo solo se sugiere.
    4. Primero los totales del pedido; despues se reparten por peso normalizado
       de minutos activos. Nunca horas-de-linea x costo-hora sumadas.
    """
    avisos: list[str] = []

    # ---- 1. Tiempo -------------------------------------------------------
    for linea in lineas:
        linea.line_active_minutes = line_active_minutes(
            linea.quantity, linea.mold_count or 1, linea.production_time_per_unit_minutes
        )
    if any(
        linea.quantity > 0 and linea.production_time_per_unit_minutes is None for linea in lineas
    ):
        avisos.append(WARN_LINE_TIME_MISSING)
    minutos = order_active_minutes([linea.line_active_minutes for linea in lineas])
    quotation.active_production_minutes = minutos
    horas = minutes_to_hours(minutos)

    # ---- 2. Personal externo -----------------------------------------------
    try:
        externo = external_labor_cost(
            horas, await external_workers_of(session, quotation), workday_hours_of(quotation)
        )
    except RulesMathError as error:
        raise V2PricingInputInvalid(str(error)) from error
    quotation.commercial_external_labor_cost = externo.commercial
    quotation.real_external_labor_cost = externo.real
    # «Mano de obra» del precio es lo que se imputa al cliente.
    quotation.labor_cost_total = externo.commercial

    # ---- 3. Espacio, administracion, ilustracion, adicionales ---------------
    espacio = space_cost(horas, effective_space_cost_per_hour(quotation))
    if quotation.passive_time_hours > ZERO:
        avisos.append(WARN_PASSIVE_TIME_AVAILABLE)
    administracion = quotation.administrative_cost_snapshot or ZERO
    ilustracion = quotation.illustration_cost
    quema_comercial = quotation.firing_commercial_total
    gas_real = quotation.firing_gas_total
    extras_por_linea, extras_generales, extras_total = await _extras_by_line(session, quotation.id)

    materiales_total = ZERO
    for linea in lineas:
        directo = linea.body_cost + linea.glaze_cost
        materiales_total += directo
        linea.direct_cost = (
            directo + extras_por_linea.get(linea.id, ZERO) + (linea.illustration_cost or ZERO)
        )
    directo_total = sum((linea.direct_cost for linea in lineas), ZERO)
    general_total = administracion + ilustracion + extras_generales

    quotation.materials_cost_total = materiales_total
    quotation.extras_cost_total = extras_total
    quotation.space_cost = espacio
    quotation.direct_cost_total = directo_total
    quotation.production_cost_total = (
        directo_total + quema_comercial + espacio + general_total + externo.commercial
    )
    quotation.real_cost_total = directo_total + gas_real + espacio + general_total + externo.real

    # ---- 4. Repartir los totales del pedido ---------------------------------
    cantidades = [Decimal(linea.quantity) for linea in lineas]
    minutos_linea = [linea.line_active_minutes or ZERO for linea in lineas]
    espacios = allocate_by_active_minutes(espacio, minutos_linea, cantidades)
    comerciales = allocate_by_active_minutes(externo.commercial, minutos_linea, cantidades)
    reales = allocate_by_active_minutes(externo.real, minutos_linea, cantidades)
    generales = allocate_by_weight(
        general_total, _pesos([linea.direct_cost for linea in lineas], cantidades)
    )
    for linea, espacio_l, general_l, comercial_l, real_l in zip(
        lineas, espacios, generales, comerciales, reales, strict=True
    ):
        linea.allocated_space_cost = espacio_l
        linea.allocated_general_cost = general_l
        linea.allocated_external_commercial_cost = comercial_l
        linea.allocated_external_real_cost = real_l
        linea.allocated_production_cost = (
            linea.direct_cost + linea.firing_commercial_cost + espacio_l + general_l + comercial_l
        )
        linea.allocated_real_cost = (
            linea.direct_cost + linea.firing_gas_cost + espacio_l + general_l + real_l
        )

    if not lineas:
        avisos.append(WARN_NO_LINES)
    if quotation.production_cost_total <= ZERO:
        avisos.append(WARN_NO_COST)
    if wholesale_suggested(
        quotation.production_type,
        total_units_of(lineas),
        quotation.wholesale_threshold_snapshot,
        declined=quotation.wholesale_suggestion_declined_at is not None,
    ):
        avisos.append(WARN_WHOLESALE_THRESHOLD_EXCEEDED)
    return avisos


async def _recalculate(
    session: AsyncSession, quotation: V2Quotation, lineas: list[V2QuotationProduct]
) -> list[str]:
    avisos: list[str] = []

    if quotation.pricing_rules_version >= PRICING_RULES_V2:
        avisos += await _costs_v2(session, quotation, lineas)
    else:
        avisos += await _costs_v1(session, quotation, lineas)

    # ---- 3. Del costo al precio --------------------------------------
    factor = quotation.commercial_factor
    if factor is None:
        _reset_pricing(quotation, lineas)
        await session.flush()
        return [*avisos, WARN_NO_FACTOR]

    minimo = quotation.commercial_factor_min_snapshot or factor
    # Fase 010J. El precio objetivo es costo x factor OBJETIVO (x3), como en el
    # Excel; no el maximo del rango. Las filas de antes de 0038 congelaron su
    # maximo como objetivo y conservan ese numero.
    objetivo = (
        quotation.commercial_factor_target_snapshot
        or quotation.commercial_factor_max_snapshot
        or factor
    )
    try:
        quotation.price_min = apply_factor(quotation.production_cost_total, minimo)
        quotation.price_target = apply_factor(quotation.production_cost_total, objetivo)
        quotation.negotiated_price = apply_factor(quotation.production_cost_total, factor)
    except PricingMathError as error:
        raise V2PricingInputInvalid(str(error)) from error

    impuesto = quotation.tax_percent_snapshot
    if impuesto is None:
        avisos.append(WARN_NO_TAX)
        impuesto = ZERO
    paso = quotation.rounding_step_snapshot
    if paso is None or paso <= ZERO:
        avisos.append(WARN_NO_ROUNDING)
        paso = None
    #: NULL cuando la cotizacion esta en moneda base: no hay nada que convertir
    #: y un 1 guardado ahi seria un tipo de cambio inventado (010B).
    cambio = quotation.exchange_rate_snapshot

    subtotal = ZERO
    impuesto_total = ZERO
    total = ZERO
    sin_cantidad = False
    try:
        for linea in lineas:
            linea.line_price = apply_factor(linea.allocated_production_cost, factor)
            crudo = unit_price(linea.line_price, linea.quantity, cambio)
            linea.unit_price_raw = crudo
            linea.unit_price = quantize_money(crudo) if paso is None else ceil_to_step(crudo, paso)
            # Reconstruido desde el unitario redondeado: es la unica forma de
            # que el documento cuadre cuando alguien lo sume a mano.
            linea.line_subtotal = linea.unit_price * Decimal(linea.quantity)
            linea.line_tax = tax_amount(linea.line_subtotal, impuesto)
            linea.line_total = linea.line_subtotal + linea.line_tax
            linea.allocated_profit = (
                to_base_currency(linea.line_subtotal, cambio) - linea.allocated_real_cost
            )
            subtotal += linea.line_subtotal
            impuesto_total += linea.line_tax
            total += linea.line_total
            if linea.quantity <= 0:
                sin_cantidad = True
    except PricingMathError as error:
        raise V2PricingInputInvalid(str(error)) from error

    if sin_cantidad:
        avisos.append(WARN_LINE_WITHOUT_QUANTITY)

    quotation.subtotal_amount = subtotal
    quotation.tax_amount = impuesto_total
    quotation.total_amount = total

    # ---- 4. Lo que deja ----------------------------------------------
    subtotal_base = to_base_currency(subtotal, cambio)
    # Cuanto anadio el redondeo respecto al precio que se buscaba. Sin este
    # numero nadie sabe por que el subtotal no es exactamente costo x factor.
    quotation.rounding_adjustment = subtotal_base - quotation.negotiated_price
    # Contra el costo REAL y sin el IGV: el impuesto no es ingreso del taller.
    quotation.estimated_profit = subtotal_base - quotation.real_cost_total
    try:
        quotation.effective_margin_percent = persisted_margin_percent(
            quotation.estimated_profit, subtotal_base
        )
    except PricingMathError as error:
        raise V2PricingInputInvalid(str(error)) from error
    if subtotal_base > ZERO and quotation.estimated_profit < ZERO:
        avisos.append(WARN_SELLING_BELOW_COST)

    await session.flush()
    return avisos


class V2PricingService:
    """El resultado economico de una cotizacion V2."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._audit = audit

    async def pricing_state(self, quotation_id: int) -> PricingState:
        """El precio de una cotizacion, listo para presentarlo.

        Recalcula si es borrador —las lineas, las tareas o el horno pueden
        haber cambiado por otra via— y se limita a leer si ya esta emitida. Que
        recalcule no lo convierte en una escritura encubierta: la ruta de
        lectura no confirma la transaccion.
        """
        quotation = await self.quotation(quotation_id)
        avisos = await refresh_pricing(self._session, quotation)
        lineas = await _lines_of(self._session, quotation_id)
        externo = EMPTY_EXTERNAL_LABOR
        if quotation.pricing_rules_version >= PRICING_RULES_V2:
            # El desglose persona por persona se deriva de lo congelado: es una
            # lectura, no un recalculo. Una emitida ensena el de su emision.
            externo = external_labor_cost(
                minutes_to_hours(quotation.active_production_minutes),
                await external_workers_of(self._session, quotation),
                workday_hours_of(quotation),
            )
        unidades = total_units_of(lineas)
        ids = [fila.worker_id for fila in externo.per_worker]
        nombres: dict[int, str] = {}
        if ids:
            filas = await self._session.execute(
                select(V2Worker.id, V2Worker.name).where(V2Worker.id.in_(ids))
            )
            for worker_id, nombre in filas.all():
                nombres[worker_id] = nombre
        return PricingState(
            quotation=quotation,
            lines=lineas,
            warnings=avisos,
            external_labor=externo,
            worker_names=nombres,
            total_units=unidades,
            wholesale_suggested=quotation.status is V2QuotationStatus.DRAFT
            and wholesale_suggested(
                quotation.production_type,
                unidades,
                quotation.wholesale_threshold_snapshot,
                declined=quotation.wholesale_suggestion_declined_at is not None,
            ),
        )

    async def set_pricing(
        self, quotation_id: int, data: dict[str, Any], *, user: AuthenticatedUser
    ) -> PricingState:
        """Cambia el factor comercial de la cotizacion.

        Es lo UNICO que se puede decidir aqui: el resto son consecuencias. Un
        precio unitario tecleado a mano dejaria de poder explicarse desde los
        costos, que es justo lo que esta fase construye.
        """
        quotation = await self._draft(quotation_id)

        if "commercial_factor" in data:
            factor = data["commercial_factor"]
            if factor is None:
                # En el resto de la familia un nulo explicito RETIRA un acuerdo.
                # Aqui no hay nada que retirar: sin factor no hay precio, y una
                # cotizacion no puede quedarse sin el. Se dice en vez de
                # ignorarlo en silencio, que dejaria al usuario creyendo que
                # cambio algo.
                raise V2PricingInputInvalid(
                    "El factor comercial no puede retirarse: sin el no hay precio",
                    code="V2_PRICING_FACTOR_REQUIRED",
                )
            self._apply_factor(quotation, factor)
        # Fase 010P. El costo de espacio por hora puede acordarse para ESTA
        # cotizacion (nulo retira el acuerdo y vuelve el congelado), y el tiempo
        # pasivo se informa para sugerirlo en precio: nunca suma a un costo.
        if "space_cost_per_hour_override" in data:
            acuerdo = data["space_cost_per_hour_override"]
            if acuerdo is not None and acuerdo < ZERO:
                raise V2PricingInputInvalid("El costo de espacio por hora no puede ser negativo")
            quotation.space_cost_per_hour_override = acuerdo
        if "passive_time_hours" in data:
            pasivo = data["passive_time_hours"]
            pasivo = ZERO if pasivo is None else pasivo
            if pasivo < ZERO:
                raise V2PricingInputInvalid("El tiempo pasivo no puede ser negativo")
            quotation.passive_time_hours = pasivo

        estado = await self.pricing_state(quotation_id)
        await self._session.flush()

        self._audit.record_action(
            entity_type=V2_PRICING_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "commercial_factor": str(quotation.commercial_factor),
                "space_cost_per_hour_override": str(quotation.space_cost_per_hour_override),
                "passive_time_hours": str(quotation.passive_time_hours),
                "production_cost_total": str(quotation.production_cost_total),
                "subtotal_amount": str(quotation.subtotal_amount),
                "total_amount": str(quotation.total_amount),
            },
        )
        return estado

    @staticmethod
    def _apply_factor(quotation: V2Quotation, factor: Decimal) -> None:
        """Comprueba el factor contra el rango que la cotizacion congelo.

        Se comprueba aqui, con un mensaje que dice entre que limites puede
        moverse, en vez de dejar que reviente el CHECK de la base y devuelva un
        500 sin explicacion.
        """
        minimo = quotation.commercial_factor_min_snapshot
        maximo = quotation.commercial_factor_max_snapshot
        if minimo is None or maximo is None:
            raise V2PricingInputInvalid(
                "La cotizacion no congelo los limites del factor comercial",
                code="V2_PRICING_FACTOR_RANGE_MISSING",
            )
        if not within_factor_range(factor, minimo, maximo):
            raise V2FactorOutOfRangeError(
                f"El factor comercial tiene que estar entre x{minimo} y x{maximo}"
            )
        quotation.commercial_factor = factor

    async def quotation(self, quotation_id: int) -> V2Quotation:
        """La cotizacion para LEER su precio, sin exigir que sea borrador.

        Con la cabecera BLOQUEADA (Fase 010P): leer un borrador lo recalcula, y
        recalcular escribe lineas. Tomar la cabecera primero, como las
        escrituras, deja un solo orden de bloqueo (`quoter_v2_locks`).
        """
        quotation = await lock_quotation_for_recalculation(self._session, quotation_id)
        if quotation is None:
            raise V2PricingQuotationNotFoundError()
        return quotation

    async def _draft(self, quotation_id: int) -> V2Quotation:
        """La cotizacion, bloqueada, si todavia admite cambios de precio.

        Mismo criterio y mismo motivo que en 010C, 010D y 010E: leer el estado
        sin bloquear deja una ventana entre la comprobacion y el guardado por
        la que una emision simultanea colaria otro factor en una cotizacion ya
        comprometida.
        """
        quotation = (
            await self._session.scalars(
                select(V2Quotation).where(V2Quotation.id == quotation_id).with_for_update()
            )
        ).one_or_none()
        if quotation is None:
            raise V2PricingQuotationNotFoundError()
        if quotation.status is not V2QuotationStatus.DRAFT:
            raise V2PricingNotEditableError()
        return quotation


__all__ = [
    "PricingState",
    "V2FactorOutOfRangeError",
    "V2PricingInputInvalid",
    "V2PricingNotEditableError",
    "V2PricingQuotationNotFoundError",
    "V2PricingService",
    "refresh_pricing",
]
