"""Fase 010P -- las reglas comerciales v2 del Cotizador V2 (tiempo, moldes, personal, espacio).

Funciones puras, sin base de datos ni sesion, como `quoter_v2_labor` y
`quoter_v2_pricing`: son las que deciden cuanto cuesta producir, y una formula
que solo vive dentro de un servicio asincrono no se puede fijar con una prueba.

Implementado en W1 (010P_PLAN_REV2). Las pruebas que fijan cada numero,
calculado a mano, estan en `tests/unit/test_quoter_v2_rules_010p.py`.

## Reglas (autoridad: 010P_PLAN_REV2 §1)

- ciclos = ceil(cantidad / moldes); minutos de la linea = ciclos x minutos por
  unidad; el pedido dura el MAXIMO de sus lineas, no la suma, porque productos
  distintos se trabajan en paralelo.
- El personal INTERNO no cuesta. Cada EXTERNO tiene DOS costos: el COMERCIAL
  (horas activas x jornal / jornada, lo que se imputa al cliente) y el REAL
  (jornales enteros: ceil(horas / jornada) x jornal, lo que paga el taller).
- El espacio se cobra por hora ACTIVA; el tiempo pasivo solo se sugiere.
- Los costos del pedido se calculan primero y luego se reparten por peso
  normalizado de minutos activos: nunca horas-de-linea x costo-hora sumadas.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.core.quoter_v2_pricing import allocate_by_weight
from app.models.quoter_v2 import V2ProductionType
from app.models.quoter_v2_labor import V2LaborAssignmentOrigin

#: Quien eligio al trabajador (DEFAULT / MANUAL). Vive en el modelo porque es
#: una columna; aqui se reexporta con el nombre que usan las reglas.
LaborAssignmentOrigin = V2LaborAssignmentOrigin

ZERO = Decimal(0)
MINUTES_PER_HOUR = Decimal(60)

#: Aviso: aceptar «Por mayor» sin trabajador externo por defecto configurado.
WARN_WHOLESALE_EXTERNAL_WORKER_MISSING = "V2_WHOLESALE_EXTERNAL_WORKER_MISSING"
#: Aviso: al aceptar «Por mayor» se conservaron asignaciones hechas a mano.
WARN_WHOLESALE_MANUAL_WORKERS_KEPT = "V2_WHOLESALE_MANUAL_WORKERS_KEPT"
#: Aviso: el externo por defecto no sabe la tecnica de algun proceso.
WARN_WHOLESALE_DEFAULT_WORKER_LACKS_TECHNIQUE = "V2_WHOLESALE_DEFAULT_WORKER_LACKS_TECHNIQUE"


class RulesMathError(ValueError):
    """Una entrada que las reglas 010P no pueden aceptar (negativa, jornada 0)."""


@dataclass(frozen=True)
class ExternalWorkerSnapshot:
    """Un trabajador EXTERNO asignado, con la tarifa congelada en la cotizacion.

    `workday_hours` es None cuando la persona no tiene jornada propia: entonces
    vale la de la cotizacion (F9).
    """

    worker_id: int
    daily_rate: Decimal
    workday_hours: Decimal | None


@dataclass(frozen=True)
class ExternalWorkerCost:
    worker_id: int
    daily_rate: Decimal
    workday_hours: Decimal
    hourly_equivalent: Decimal
    days_paid: int
    commercial_cost: Decimal
    real_cost: Decimal


@dataclass(frozen=True)
class ExternalLaborCost:
    """El personal externo del pedido: lo que se cobra y lo que se paga."""

    commercial: Decimal
    real: Decimal
    gap: Decimal
    per_worker: tuple[ExternalWorkerCost, ...]


@dataclass(frozen=True)
class ProcessAssignment:
    """Un proceso del pedido tal como esta ahora, para decidir «Por mayor»."""

    process_id: int
    technique_id: int
    worker_id: int | None
    origin: LaborAssignmentOrigin | None


@dataclass(frozen=True)
class DefaultWorker:
    """El trabajador por defecto de un tipo de pedido y lo que sabe hacer."""

    worker_id: int
    technique_ids: frozenset[int]


@dataclass(frozen=True)
class Reassignment:
    process_id: int
    worker_id: int


@dataclass(frozen=True)
class WholesaleLaborPlan:
    """Que cambiar en el personal al aceptar «Por mayor», y que avisar."""

    reassignments: tuple[Reassignment, ...]
    warnings: tuple[str, ...]


def _sin_negativos(nombre: str, valor: Decimal) -> None:
    if valor < ZERO:
        raise RulesMathError(f"{nombre} no puede ser negativo")


def production_cycles(quantity: int, mold_count: int) -> int:
    """ceil(cantidad / moldes). Un molde incompleto es un ciclo mas."""
    if mold_count < 1:
        raise ValueError("Hace falta al menos un molde")
    if quantity < 0:
        raise ValueError("La cantidad no puede ser negativa")
    # Division entera hacia arriba sin pasar por coma flotante.
    return -(-quantity // mold_count)


def line_active_minutes(
    quantity: int, mold_count: int, minutes_per_unit: Decimal | None
) -> Decimal | None:
    """ciclos x minutos por unidad; None si la linea aun no tiene tiempo."""
    if minutes_per_unit is None:
        return None
    _sin_negativos("El tiempo por unidad", minutes_per_unit)
    return Decimal(production_cycles(quantity, mold_count)) * minutes_per_unit


def order_active_minutes(line_minutes: Sequence[Decimal | None]) -> Decimal:
    """El MAXIMO de las lineas con tiempo: productos distintos van en paralelo.

    Sumarlas cobraria dos veces el mismo tiempo de taller. Las lineas sin
    tiempo no entran; sin ninguna, el pedido no tiene tiempo activo.
    """
    con_tiempo = [minutos for minutos in line_minutes if minutos is not None]
    return max(con_tiempo, default=ZERO)


def minutes_to_hours(minutes: Decimal) -> Decimal:
    return minutes / MINUTES_PER_HOUR


def external_labor_cost(
    active_hours: Decimal,
    workers: Sequence[ExternalWorkerSnapshot],
    default_workday_hours: Decimal,
) -> ExternalLaborCost:
    """Lo que se COBRA y lo que se PAGA por el personal externo del pedido.

    Comercial: horas activas x (jornal / jornada) de cada externo. Es lo que se
    imputa al cliente: las horas reales, no los jornales enteros.

    Real: ceil(horas activas / jornada) jornales x jornal de cada externo. Es
    lo que el taller desembolsa, porque un jornal empezado se paga entero.

    Cada externo con SU jornal y SU jornada; sin jornada propia, la de la
    cotizacion. Dos externos no reducen las horas: las trabajan los dos.
    """
    _sin_negativos("Las horas activas", active_hours)
    if default_workday_hours <= ZERO:
        raise RulesMathError("La jornada de la cotizacion tiene que ser positiva")
    filas: list[ExternalWorkerCost] = []
    for worker in workers:
        _sin_negativos("El jornal", worker.daily_rate)
        jornada = (
            worker.workday_hours
            if worker.workday_hours is not None and worker.workday_hours > ZERO
            else default_workday_hours
        )
        por_hora = worker.daily_rate / jornada
        jornales = math.ceil(active_hours / jornada) if active_hours > ZERO else 0
        filas.append(
            ExternalWorkerCost(
                worker_id=worker.worker_id,
                daily_rate=worker.daily_rate,
                workday_hours=jornada,
                hourly_equivalent=por_hora,
                days_paid=jornales,
                commercial_cost=active_hours * por_hora,
                real_cost=Decimal(jornales) * worker.daily_rate,
            )
        )
    comercial = sum((fila.commercial_cost for fila in filas), ZERO)
    real = sum((fila.real_cost for fila in filas), ZERO)
    return ExternalLaborCost(
        commercial=comercial, real=real, gap=real - comercial, per_worker=tuple(filas)
    )


def space_cost_per_hour(space_cost_per_day: Decimal, workday_hours: Decimal) -> Decimal:
    _sin_negativos("El costo de espacio por dia", space_cost_per_day)
    if workday_hours <= ZERO:
        raise RulesMathError("La jornada tiene que ser positiva")
    return space_cost_per_day / workday_hours


def space_cost(active_hours: Decimal, cost_per_hour: Decimal) -> Decimal:
    """Espacio por hora ACTIVA del pedido (el MAXIMO de sus lineas)."""
    _sin_negativos("Las horas activas", active_hours)
    _sin_negativos("El costo de espacio por hora", cost_per_hour)
    return active_hours * cost_per_hour


def passive_space_suggestion(passive_hours: Decimal, cost_per_hour: Decimal) -> Decimal:
    """Lo que costaria el tiempo pasivo si se considerara. Nunca entra en totales."""
    _sin_negativos("El tiempo pasivo", passive_hours)
    _sin_negativos("El costo de espacio por hora", cost_per_hour)
    return passive_hours * cost_per_hour


def administrative_cost(production_type: V2ProductionType, configured_cost: Decimal) -> Decimal:
    """RETAIL = 0; WHOLESALE = lo configurado. Decision del owner (010P, P7)."""
    if production_type is V2ProductionType.RETAIL:
        return ZERO
    return configured_cost


def wholesale_suggested(
    production_type: V2ProductionType,
    total_units: int,
    threshold: int | None,
    declined: bool,
) -> bool:
    """Solo RETAIL, solo si el TOTAL del pedido SUPERA el umbral y no se rechazo."""
    if production_type is not V2ProductionType.RETAIL or threshold is None or declined:
        return False
    return total_units > threshold


def allocate_by_active_minutes(
    total: Decimal,
    line_minutes: Sequence[Decimal],
    quantities: Sequence[Decimal],
) -> list[Decimal]:
    """Reparte un total del PEDIDO por peso normalizado de minutos activos.

    Primero se calcula el total (espacio, externo comercial, externo real) con
    el tiempo del pedido; despues se reparte. Nunca se calcula por linea con
    sus propias horas: en paralelo, eso cobraria dos veces el mismo tiempo.

    Reservas: sin minutos, por cantidad; sin cantidades, a partes iguales. La
    suma es exactamente el total (`allocate_by_weight`, resto mayor).
    """
    if len(line_minutes) != len(quantities):
        raise RulesMathError("Cada linea necesita sus minutos y su cantidad")
    if not line_minutes:
        return []
    if sum(line_minutes, ZERO) > ZERO:
        pesos = list(line_minutes)
    elif sum(quantities, ZERO) > ZERO:
        pesos = list(quantities)
    else:
        pesos = [Decimal(1) for _ in line_minutes]
    return allocate_by_weight(total, pesos)


def plan_wholesale_labor(
    assignments: Sequence[ProcessAssignment],
    external_default: DefaultWorker | None,
) -> WholesaleLaborPlan:
    """Que cambiar en el personal al aceptar «Por mayor» (decision 4).

    - Lo MANUAL no se toca nunca: lo eligio una persona. Se avisa.
    - Lo DEFAULT y lo sin asignar pasan al externo por defecto, solo si sabe la
      tecnica del proceso; si no la sabe, se deja como estaba y se avisa.
    - Sin externo por defecto no se reasigna nada y se avisa.
    """
    avisos: list[str] = []
    if any(fila.origin is LaborAssignmentOrigin.MANUAL for fila in assignments):
        avisos.append(WARN_WHOLESALE_MANUAL_WORKERS_KEPT)
    if external_default is None:
        return WholesaleLaborPlan(
            reassignments=(), warnings=(*avisos, WARN_WHOLESALE_EXTERNAL_WORKER_MISSING)
        )
    cambios: list[Reassignment] = []
    falta_tecnica = False
    for fila in assignments:
        if fila.origin is LaborAssignmentOrigin.MANUAL:
            continue
        if fila.worker_id == external_default.worker_id:
            continue
        if fila.technique_id not in external_default.technique_ids:
            falta_tecnica = True
            continue
        cambios.append(
            Reassignment(process_id=fila.process_id, worker_id=external_default.worker_id)
        )
    if falta_tecnica:
        avisos.append(WARN_WHOLESALE_DEFAULT_WORKER_LACKS_TECHNIQUE)
    return WholesaleLaborPlan(reassignments=tuple(cambios), warnings=tuple(avisos))
