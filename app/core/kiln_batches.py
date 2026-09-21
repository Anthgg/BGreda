"""Aritmetica pura de la planificacion de hornadas. Fase 010L.

Sin base de datos y sin sesion: lo que decide si algo cabe, cuanto queda libre,
que hornadas se sugieren y si una alta puede arrancar. Aqui se prueban las
reglas; el servicio las aplica bajo bloqueo y la base las garantiza al final.

## Lo que esto NO promete

Todo es VOLUMETRICO. «Cabe» significa que el volumen de las piezas, con su
separacion, no pasa de la capacidad util del horno. No significa que las piezas
se puedan acomodar fisicamente: una pieza muy alta puede no entrar aunque sobre
volumen. Eso lo resuelve el mapa de 010M. Por eso los textos dicen «capacidad
estimada disponible» y nunca «caben».
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal(0)
HUNDRED = Decimal(100)
PERCENT_QUANTUM = Decimal("0.000001")


class KilnBatchMathError(ValueError):
    """Una entrada imposible: capacidad nula, cantidad negativa..."""


def _percent(valor: Decimal) -> Decimal:
    return valor.quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_UP)


def occupancy_percent(assigned_cm3: Decimal, capacity_cm3: Decimal) -> Decimal:
    """Que porcentaje de la hornada esta ocupado. Nunca pasa de 100 en una hornada valida."""
    if capacity_cm3 <= ZERO:
        raise KilnBatchMathError("La capacidad de la hornada tiene que ser mayor que cero")
    if assigned_cm3 < ZERO:
        raise KilnBatchMathError("El volumen asignado no puede ser negativo")
    return _percent(assigned_cm3 / capacity_cm3 * HUNDRED)


def available_cm3(assigned_cm3: Decimal, capacity_cm3: Decimal) -> Decimal:
    """Lo que queda libre. Cero si esta llena; nunca negativo."""
    return max(capacity_cm3 - assigned_cm3, ZERO)


def available_percent(assigned_cm3: Decimal, capacity_cm3: Decimal) -> Decimal:
    return _percent(HUNDRED - occupancy_percent(assigned_cm3, capacity_cm3))


def fits(assigned_cm3: Decimal, capacity_cm3: Decimal, extra_cm3: Decimal) -> bool:
    """Si `extra_cm3` entra sin pasar del 100 %. Sin tolerancia: el negocio no definio ninguna."""
    if extra_cm3 < ZERO:
        raise KilnBatchMathError("No se puede asignar un volumen negativo")
    return assigned_cm3 + extra_cm3 <= capacity_cm3


def assignment_volume(quantity: int, unit_volume_cm3: Decimal) -> Decimal:
    """Volumen de `quantity` piezas. Es exactamente el que exige el CHECK de la base."""
    if quantity <= 0:
        raise KilnBatchMathError("Una asignacion tiene que llevar al menos una pieza")
    if unit_volume_cm3 <= ZERO:
        raise KilnBatchMathError("Una pieza sin medidas no ocupa horno y no se puede asignar")
    return Decimal(quantity) * unit_volume_cm3


def max_pieces_that_fit(free_cm3: Decimal, unit_volume_cm3: Decimal) -> int:
    """Cuantas piezas enteras de ese volumen caben en lo libre."""
    if unit_volume_cm3 <= ZERO:
        return 0
    return int(free_cm3 // unit_volume_cm3)


# ---------------------------------------------------------------------------
# Sugerencias
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BatchCandidate:
    """Lo que el motor de sugerencias necesita saber de una hornada."""

    batch_id: int
    kiln_id: int
    kiln_name: str
    firing_type: str
    scheduled_date: date
    editable: bool
    exclusive: bool
    capacity_cm3: Decimal
    assigned_cm3: Decimal


@dataclass(frozen=True)
class Requirement:
    """Lo que falta planificar de un pedido, para UN ciclo."""

    firing_type: str
    #: Volumen de todas las piezas que faltan en este ciclo.
    remaining_cm3: Decimal
    #: Volumen de la pieza mas pequena que falta: si no cabe ni esa, la
    #: hornada no sirve para nada de este pedido.
    smallest_piece_cm3: Decimal
    exclusive: bool


@dataclass(frozen=True)
class Suggestion:
    batch_id: int
    kiln_id: int
    kiln_name: str
    scheduled_date: date
    occupancy_percent: Decimal
    available_percent: Decimal
    available_cm3: Decimal
    #: Si admite TODO lo que falta de este ciclo.
    covers_all: bool
    #: Cuanto de lo que falta cubriria, en porcentaje del pedido pendiente.
    covered_percent: Decimal


def suggest(
    candidates: Iterable[BatchCandidate], requirement: Requirement, today: date
) -> list[Suggestion]:
    """Hornadas compatibles, en el orden en que conviene mirarlas. NUNCA asigna.

    Compatible es TODO a la vez:

    - mismo ciclo: baja y alta no se mezclan;
    - editable (planificada), y con fecha de hoy en adelante —hoy de Lima, que
      lo decide quien llama—;
    - no reservada en exclusiva por otro pedido; y si el pedido es exclusivo,
      solo hornadas VACIAS: uno exclusivo no se agrupa con nadie;
    - con sitio para, al menos, la pieza mas pequena que falta.

    Orden: primero las que admiten todo lo que falta; luego la fecha mas
    cercana; luego el ajuste mas justo —menos hueco sobrante—; y el horno como
    desempate estable. Mismo input, mismo orden: se puede probar y auditar.
    """
    if requirement.remaining_cm3 <= ZERO:
        return []
    salida: list[tuple[tuple[bool, date, Decimal, int, int], Suggestion]] = []
    for c in candidates:
        if c.firing_type != requirement.firing_type or not c.editable:
            continue
        if c.scheduled_date < today or c.exclusive:
            continue
        if requirement.exclusive and c.assigned_cm3 > ZERO:
            continue
        libre = available_cm3(c.assigned_cm3, c.capacity_cm3)
        if libre < requirement.smallest_piece_cm3 or libre <= ZERO:
            continue
        cubre_todo = libre >= requirement.remaining_cm3
        cubierto = min(libre, requirement.remaining_cm3)
        sobrante = libre - cubierto
        sugerencia = Suggestion(
            batch_id=c.batch_id,
            kiln_id=c.kiln_id,
            kiln_name=c.kiln_name,
            scheduled_date=c.scheduled_date,
            occupancy_percent=occupancy_percent(c.assigned_cm3, c.capacity_cm3),
            available_percent=available_percent(c.assigned_cm3, c.capacity_cm3),
            available_cm3=libre,
            covers_all=cubre_todo,
            covered_percent=_percent(cubierto / requirement.remaining_cm3 * HUNDRED),
        )
        clave = (not cubre_todo, c.scheduled_date, sobrante, c.kiln_id, c.batch_id)
        salida.append((clave, sugerencia))
    salida.sort(key=lambda par: par[0])
    return [s for _, s in salida]


# ---------------------------------------------------------------------------
# Baja antes que alta
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LineCycles:
    """Una linea de un pedido y lo que necesita de cada ciclo."""

    line_key: str
    needs_low: bool
    needs_high: bool


def high_start_violations(
    lines: Sequence[LineCycles],
    high_in_batch: dict[str, int],
    high_already_fired: dict[str, int],
    low_completed: dict[str, int],
) -> list[str]:
    """Lineas cuya alta no puede arrancar porque no pasaron por la baja.

    En ceramica una pieza que necesita bizcocho no puede ir a alta cruda: la
    humedad y los gases la hacen estallar y dañan a las vecinas de hornada. La
    regla se evalua POR LINEA Y CANTIDAD: si una linea se partio en dos bajas,
    en alta solo pueden arrancar tantas piezas como ya completaron la suya.

    - `high_in_batch`: piezas de cada linea en ESTA hornada alta;
    - `high_already_fired`: piezas de la linea en otras altas ya arrancadas o
      completadas —cuentan contra lo bizcochado—;
    - `low_completed`: piezas de la linea en bajas COMPLETADAS.

    Una linea que solo necesita alta —una pieza que el cliente trae ya
    bizcochada— no se bloquea nunca.
    """
    fallos: list[str] = []
    for linea in lines:
        if not (linea.needs_low and linea.needs_high):
            continue
        en_esta = high_in_batch.get(linea.line_key, 0)
        if en_esta <= 0:
            continue
        ya = high_already_fired.get(linea.line_key, 0)
        bizcochadas = low_completed.get(linea.line_key, 0)
        if ya + en_esta > bizcochadas:
            fallos.append(linea.line_key)
    return fallos


__all__ = [
    "BatchCandidate",
    "KilnBatchMathError",
    "LineCycles",
    "Requirement",
    "Suggestion",
    "assignment_volume",
    "available_cm3",
    "available_percent",
    "fits",
    "high_start_violations",
    "max_pieces_that_fit",
    "occupancy_percent",
    "suggest",
]
