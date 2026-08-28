"""Matematica del costo de quema.

Funciones puras: no tocan la base de datos ni la sesion. Lo que aqui se decide
sale de la hoja «Costo de quema» del documento funcional, celda a celda:

===========================  ==========================================
Hoja                         Aqui
===========================  ==========================================
``G = D*E*F*C``              :func:`line_volume`
``G / G19``                  participacion por volumen
``H = G/G19*U$8``            :func:`allocate_session_cost`
``L = G/$C$7*100``           :func:`physical_occupancy_percentage`
``T16:T25`` (tramos)         :func:`occupancy_bracket`
``Q = H + K``                :func:`FiringMath.base_cost`
``R = Q * V23``              :func:`FiringMath.allocated_cost`
===========================  ==========================================

Nada de ``float``: todo el camino es ``Decimal``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.core.errors import APIError

#: Los tramos de ocupacion avanzan de diez en diez. El documento los escribe
#: como "1-10%", "11-20%"... "91-100%", de modo que el tramo de un porcentaje
#: fisico es el multiplo de diez inmediatamente superior.
BRACKET_STEP = 10
MIN_BRACKET = 10
MAX_BRACKET = 100

#: Tramos validos que acepta la API. Fijados por §14 de la especificacion.
ALLOWED_BRACKETS: tuple[int, ...] = tuple(range(MIN_BRACKET, MAX_BRACKET + 1, BRACKET_STEP))


class FiringError(APIError):
    """Error base del modulo de quemas."""

    status_code = 422
    code = "FIRING_ERROR"
    message = "Error en la hoja de quema"


class KilnCapacityExceededError(FiringError):
    status_code = 409
    code = "KILN_CAPACITY_EXCEEDED"
    message = "El volumen supera la capacidad del horno"


class OccupancyFactorMissingError(FiringError):
    code = "OCCUPANCY_FACTOR_NOT_CONFIGURED"
    message = "El horno no tiene configurado un factor para este tramo de ocupacion"


class FiringRateMissingError(FiringError):
    code = "KILN_RATE_NOT_CONFIGURED"
    message = "El horno no tiene tarifa vigente para este tipo de quema"


class FiringDimensionError(FiringError):
    code = "FIRING_INVALID_DIMENSIONS"
    message = "Las dimensiones y la cantidad deben ser mayores que cero"


class FiringEmptyError(FiringError):
    code = "FIRING_EMPTY"
    message = "La hoja de quema necesita al menos una pieza y una sesion de horno"


#: Límite de escala para columnas NUMERIC(18,6) en la base de datos (menor a 10^12).
MAX_VOLUME_NUMERIC = Decimal("999999999999.999999")


class FiringVolumeOverflowError(FiringError):
    code = "FIRING_VOLUME_OVERFLOW"
    message = "El volumen de la pieza o de la hoja excede la precisión máxima permitida"


def line_volume(
    quantity: int,
    length_cm: Decimal,
    width_cm: Decimal,
    height_cm: Decimal,
) -> tuple[Decimal, Decimal]:
    """Volumen unitario y total de una linea, en cm3.

    Devuelve ``(unitario, total)`` donde ``total = cantidad * unitario``.
    """
    if quantity <= 0:
        raise FiringDimensionError("La cantidad debe ser un entero mayor que cero")
    if length_cm <= 0 or width_cm <= 0 or height_cm <= 0:
        raise FiringDimensionError("Largo, ancho y alto deben ser mayores que cero")

    unit = length_cm * width_cm * height_cm
    total = unit * Decimal(quantity)
    if unit > MAX_VOLUME_NUMERIC or total > MAX_VOLUME_NUMERIC:
        raise FiringVolumeOverflowError("El volumen calculado excede el límite máximo permitido")
    return unit, total


def physical_occupancy_percentage(volume_cm3: Decimal, capacity_cm3: Decimal) -> Decimal:
    """Ocupacion fisica exacta, en porcentaje.

    Es un dato distinto del tramo comercial: aqui no se redondea nada.
    """
    if capacity_cm3 <= 0:
        raise FiringError("La capacidad del horno debe ser mayor que cero")
    return volume_cm3 / capacity_cm3 * Decimal(100)


def occupancy_bracket(percentage: Decimal) -> int:
    """Tramo comercial de un porcentaje fisico.

    El documento define los tramos como intervalos cerrados de diez en diez
    (``1-10``, ``11-20``...), asi que el tramo es el multiplo de diez
    inmediatamente superior: 0.5 % y 5.4 % caen en 10; 76.235 % cae en 80.

    Una ocupacion superior al 100 % no tiene tramo: la tabla del negocio termina
    en ``91-100 %``. Quien llama decide si eso es un aviso o un bloqueo.
    """
    if percentage <= 0:
        return MIN_BRACKET
    # Techo entero de percentage/10, sin pasar por float.
    tens = int(percentage / Decimal(BRACKET_STEP))
    if Decimal(tens * BRACKET_STEP) < percentage:
        tens += 1
    bracket = max(1, tens) * BRACKET_STEP
    return min(bracket, MAX_BRACKET)


def resolve_factor(
    brackets: Sequence[tuple[int, int, Decimal]],
    bracket: int,
) -> Decimal:
    """Busca el multiplicador del tramo en la tabla de un horno.

    ``brackets`` es una secuencia de ``(min, max, factor)``.
    """
    for minimum, maximum, factor in brackets:
        if minimum <= bracket <= maximum:
            return factor
    raise OccupancyFactorMissingError(
        f"No hay factor configurado para el tramo de {bracket} % de ocupacion"
    )


def volume_share(line_volume_cm3: Decimal, total_volume_cm3: Decimal) -> Decimal:
    """Participacion de una linea en el volumen total de la hoja."""
    if total_volume_cm3 <= 0:
        raise FiringEmptyError()
    return line_volume_cm3 / total_volume_cm3


def allocate_session_cost(share: Decimal, rate: Decimal) -> Decimal:
    """Parte de la tarifa de una sesion que absorbe una linea."""
    return share * rate


def required_batches(assigned_volume_cm3: Decimal, capacity_cm3: Decimal) -> int:
    """Cuantas hornadas hacen falta para quemar un volumen (Fase 009C).

    Un horno no se llena "2.5 veces": si sobra media carga hay que encender
    el horno otra vez, asi que el resultado se redondea SIEMPRE hacia arriba.
    Capacidad exacta es una sola hornada (100/100 -> 1, no 2); un cm3 de mas
    ya obliga a la segunda (101/100 -> 2).

    Nada de float: el techo se calcula con division entera sobre enteros
    escalados, para que 3 * 0.1 no acabe siendo 0.30000000000000004.
    """
    if capacity_cm3 <= 0:
        raise FiringError("La capacidad del horno debe ser mayor que cero")
    if assigned_volume_cm3 <= 0:
        return 0
    # -(-a // b) es el techo exacto de a/b para enteros positivos. Ambos
    # Decimal se llevan al mismo exponente entero antes de dividir.
    volume_exponent = assigned_volume_cm3.as_tuple().exponent
    capacity_exponent = capacity_cm3.as_tuple().exponent
    assert isinstance(volume_exponent, int) and isinstance(capacity_exponent, int)
    scale = Decimal(1).scaleb(-min(volume_exponent, capacity_exponent, 0))
    return -(-int(assigned_volume_cm3 * scale) // int(capacity_cm3 * scale))


@dataclass(frozen=True)
class SessionInput:
    """Sesion de horno tal y como entra al calculo."""

    key: str
    kiln_id: int
    firing_type: str
    rate: Decimal
    capacity: Decimal


@dataclass(frozen=True)
class LineInput:
    """Pieza tal y como entra al calculo."""

    quantity: int
    length_cm: Decimal
    width_cm: Decimal
    height_cm: Decimal
    #: Claves de las sesiones que queman esta pieza (baja y alta).
    session_keys: tuple[str, ...]
    #: Horno cuya capacidad decide el tramo. Si es None se usa el del primer
    #: horno asignado a la linea.
    factor_kiln_id: int | None = None


@dataclass(frozen=True)
class LineResult:
    unit_volume_cm3: Decimal
    total_volume_cm3: Decimal
    share: Decimal
    factor_kiln_id: int
    occupancy_percentage: Decimal
    occupancy_bracket: int
    occupancy_factor: Decimal
    base_cost: Decimal
    allocated_cost: Decimal
    capacity_exceeded: bool


@dataclass(frozen=True)
class SessionResult:
    key: str
    kiln_id: int
    firing_type: str
    rate: Decimal
    capacity: Decimal
    assigned_volume_cm3: Decimal
    physical_occupancy_percentage: Decimal
    subtotal: Decimal
    capacity_exceeded: bool
    #: Hornadas necesarias para esta sesion (Fase 009C). Con
    #: ``multi_batch=False`` siempre es 1: es lo que ve una hoja de quema
    #: real, donde la hoja describe UNA hornada fisica.
    batches: int = 1


@dataclass(frozen=True)
class FiringMath:
    """Resultado completo del calculo de una hoja de quema."""

    total_volume_cm3: Decimal
    subtotal: Decimal
    total_cost: Decimal
    occupancy_percentage: Decimal
    occupancy_factor: Decimal
    lines: tuple[LineResult, ...]
    sessions: tuple[SessionResult, ...]
    capacity_exceeded: bool
    #: Suma de hornadas de todas las sesiones (Fase 009C).
    total_batches: int = 0


def compute_firing(
    sessions: Sequence[SessionInput],
    lines: Sequence[LineInput],
    factor_tables: Mapping[int, Sequence[tuple[int, int, Decimal]]],
    *,
    multi_batch: bool = False,
) -> FiringMath:
    """Calcula volumenes, reparto, ocupacion, factores y costos.

    ``factor_tables`` mapea ``kiln_id`` a su tabla de tramos. Es un argumento y
    no una consulta para que la funcion siga siendo pura y comprobable.

    ``multi_batch`` (Fase 009C) es **opt-in a proposito**. Una hoja de quema
    REAL describe una hornada fisica concreta: si no cabe, no cabe, y la
    hoja se corrige — no se le multiplica el costo por detras. El Cotizador,
    en cambio, planifica: ahi si tiene sentido decir "esto no entra en una
    sola hornada, hacen falta 3, y cuesta 3 veces". Dejarlo apagado por
    omision mantiene el costo de las hojas reales byte a byte igual.
    """
    if not sessions or not lines:
        raise FiringEmptyError()

    by_key = {session.key: session for session in sessions}
    for line in lines:
        for key in line.session_keys:
            if key not in by_key:
                raise FiringError(f"La pieza referencia una sesion inexistente: {key}")

    volumes: list[tuple[Decimal, Decimal]] = [
        line_volume(line.quantity, line.length_cm, line.width_cm, line.height_cm) for line in lines
    ]
    total_volume = sum((total for _, total in volumes), Decimal(0))
    if total_volume <= 0:
        raise FiringEmptyError()
    if total_volume > MAX_VOLUME_NUMERIC:
        raise FiringVolumeOverflowError(
            "El volumen total de la hoja excede el límite máximo permitido"
        )

    line_results: list[LineResult] = []
    session_volume: dict[str, Decimal] = {session.key: Decimal(0) for session in sessions}
    session_subtotal: dict[str, Decimal] = {session.key: Decimal(0) for session in sessions}
    any_capacity_exceeded = False

    # Primera pasada: cuanto volumen carga cada sesion. Hace falta ANTES de
    # repartir costos porque el numero de hornadas depende del volumen total
    # de la sesion (varias piezas comparten hornada) y multiplica su tarifa.
    for line, (_unit, total) in zip(lines, volumes, strict=True):
        for key in line.session_keys:
            session_volume[key] += total

    session_batches: dict[str, int] = {
        session.key: (
            max(1, required_batches(session_volume[session.key], session.capacity))
            if multi_batch
            else 1
        )
        for session in sessions
    }
    # La tarifa de la sesion es el costo de UNA hornada: con N hornadas el
    # horno se enciende N veces. Multiplicar aqui deja intacto el reparto
    # proporcional por volumen que ya hace allocate_session_cost.
    session_rate: dict[str, Decimal] = {
        session.key: session.rate * Decimal(session_batches[session.key]) for session in sessions
    }

    for line, (unit, total) in zip(lines, volumes, strict=True):
        share = volume_share(total, total_volume)

        base_cost = Decimal(0)
        for key in line.session_keys:
            portion = allocate_session_cost(share, session_rate[key])
            base_cost += portion
            session_subtotal[key] += portion

        # El horno que decide el tramo: el declarado o, si no se declara, el
        # primero al que se asigna la pieza.
        factor_kiln_id = line.factor_kiln_id
        if factor_kiln_id is None:
            if not line.session_keys:
                raise FiringError("La pieza no esta asignada a ninguna sesion de horno")
            factor_kiln_id = by_key[line.session_keys[0]].kiln_id

        capacity = next(
            (s.capacity for s in sessions if s.kiln_id == factor_kiln_id),
            None,
        )
        if capacity is None:
            raise FiringError(
                "El horno elegido para el factor no participa en ninguna sesion de la hoja"
            )

        occupancy = physical_occupancy_percentage(total, capacity)
        line_exceeded = occupancy > Decimal(100)
        any_capacity_exceeded = any_capacity_exceeded or line_exceeded

        bracket = occupancy_bracket(occupancy)
        factor = resolve_factor(factor_tables.get(factor_kiln_id, ()), bracket)

        line_results.append(
            LineResult(
                unit_volume_cm3=unit,
                total_volume_cm3=total,
                share=share,
                factor_kiln_id=factor_kiln_id,
                occupancy_percentage=occupancy,
                occupancy_bracket=bracket,
                occupancy_factor=factor,
                base_cost=base_cost,
                allocated_cost=base_cost * factor,
                capacity_exceeded=line_exceeded,
            )
        )

    session_results: list[SessionResult] = []
    max_occupancy = Decimal(0)
    for session in sessions:
        assigned = session_volume[session.key]
        occupancy = (
            physical_occupancy_percentage(assigned, session.capacity)
            if assigned > 0
            else Decimal(0)
        )
        max_occupancy = max(max_occupancy, occupancy)
        batches = session_batches[session.key]
        session_results.append(
            SessionResult(
                key=session.key,
                kiln_id=session.kiln_id,
                firing_type=session.firing_type,
                rate=session.rate,
                capacity=session.capacity,
                assigned_volume_cm3=assigned,
                physical_occupancy_percentage=occupancy,
                subtotal=session_subtotal[session.key],
                # Con multi_batch el exceso deja de ser un problema: se
                # resuelve con mas hornadas, no con una alerta.
                capacity_exceeded=(not multi_batch) and occupancy > Decimal(100),
                batches=batches,
            )
        )

    subtotal = sum((line.base_cost for line in line_results), Decimal(0))
    total_cost = sum((line.allocated_cost for line in line_results), Decimal(0))
    # Factor efectivo de la hoja. Cada linea tiene el suyo; este es el resumen
    # ponderado que aparece en la cabecera.
    effective_factor = total_cost / subtotal if subtotal > 0 else Decimal(1)

    return FiringMath(
        total_volume_cm3=total_volume,
        subtotal=subtotal,
        total_cost=total_cost,
        occupancy_percentage=max_occupancy,
        occupancy_factor=effective_factor,
        lines=tuple(line_results),
        sessions=tuple(session_results),
        capacity_exceeded=(not multi_batch) and any_capacity_exceeded,
        total_batches=sum(session_batches.values()),
    )
