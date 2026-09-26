"""Fase 010P -- las reglas comerciales v2 del Cotizador V2 (tiempo, moldes, personal, espacio).

Funciones puras, sin base de datos ni sesion, como `quoter_v2_labor` y
`quoter_v2_pricing`: son las que deciden cuanto cuesta producir, y una formula
que solo vive dentro de un servicio asincrono no se puede fijar con una prueba.

**W0 (preparacion en rojo).** Este modulo declara solo la INTERFAZ aprobada en
010P_PLAN_REV2. Cada funcion levanta `NotImplementedError` hasta W1, y
`tests/unit/test_quoter_v2_rules_010p.py` esta en ROJO a proposito: fija los
numeros (F1-F9) antes de que exista el codigo que tiene que producirlos.

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

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.models.quoter_v2 import V2ProductionType

#: Aviso: aceptar «Por mayor» sin trabajador externo por defecto configurado.
WARN_WHOLESALE_EXTERNAL_WORKER_MISSING = "V2_WHOLESALE_EXTERNAL_WORKER_MISSING"
#: Aviso: al aceptar «Por mayor» se conservaron asignaciones hechas a mano.
WARN_WHOLESALE_MANUAL_WORKERS_KEPT = "V2_WHOLESALE_MANUAL_WORKERS_KEPT"
#: Aviso: el externo por defecto no sabe la tecnica de algun proceso.
WARN_WHOLESALE_DEFAULT_WORKER_LACKS_TECHNIQUE = "V2_WHOLESALE_DEFAULT_WORKER_LACKS_TECHNIQUE"


class LaborAssignmentOrigin(StrEnum):
    """Quien eligio al trabajador de un proceso (decision 4 de 010P).

    DEFAULT: lo puso el sistema con el trabajador por defecto del tipo de
    pedido. MANUAL: lo eligio una persona, y ningun automatismo lo pisa.
    """

    DEFAULT = "DEFAULT"
    MANUAL = "MANUAL"


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


def _w1() -> NotImplementedError:
    return NotImplementedError("010P W1: regla aprobada en 010P_PLAN_REV2, aun sin implementar")


def production_cycles(quantity: int, mold_count: int) -> int:
    """ceil(cantidad / moldes). Moldes ≥ 1."""
    raise _w1()


def line_active_minutes(
    quantity: int, mold_count: int, minutes_per_unit: Decimal | None
) -> Decimal | None:
    """ciclos x minutos por unidad; None si la linea aun no tiene tiempo."""
    raise _w1()


def order_active_minutes(line_minutes: Sequence[Decimal | None]) -> Decimal:
    """El MAXIMO de las lineas con tiempo (paralelismo); 0 si ninguna lo tiene."""
    raise _w1()


def minutes_to_hours(minutes: Decimal) -> Decimal:
    raise _w1()


def external_labor_cost(
    active_hours: Decimal,
    workers: Sequence[ExternalWorkerSnapshot],
    default_workday_hours: Decimal,
) -> ExternalLaborCost:
    """Comercial = horas x Σ(jornal/jornada); real = Σ ceil(horas/jornada) x jornal."""
    raise _w1()


def space_cost_per_hour(space_cost_per_day: Decimal, workday_hours: Decimal) -> Decimal:
    raise _w1()


def space_cost(active_hours: Decimal, cost_per_hour: Decimal) -> Decimal:
    raise _w1()


def passive_space_suggestion(passive_hours: Decimal, cost_per_hour: Decimal) -> Decimal:
    """Lo que costaria el tiempo pasivo si se considerara. Nunca entra en totales."""
    raise _w1()


def administrative_cost(production_type: V2ProductionType, configured_cost: Decimal) -> Decimal:
    """RETAIL = 0; WHOLESALE = lo configurado."""
    raise _w1()


def wholesale_suggested(
    production_type: V2ProductionType,
    total_units: int,
    threshold: int | None,
    declined: bool,
) -> bool:
    """Solo RETAIL, solo si el TOTAL de unidades supera el umbral y no se rechazo."""
    raise _w1()


def allocate_by_active_minutes(
    total: Decimal,
    line_minutes: Sequence[Decimal],
    quantities: Sequence[Decimal],
) -> list[Decimal]:
    """Reparte un total del pedido por peso normalizado de minutos activos.

    Reserva: por cantidad y, sin cantidades, a partes iguales (`_pesos`). La
    suma de lo repartido es exactamente el total (`allocate_by_weight`).
    """
    raise _w1()


def plan_wholesale_labor(
    assignments: Sequence[ProcessAssignment],
    external_default: DefaultWorker | None,
) -> WholesaleLaborPlan:
    """Aplica el externo por defecto a lo DEFAULT y a lo sin asignar; respeta lo MANUAL."""
    raise _w1()
