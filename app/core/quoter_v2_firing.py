"""Fase 010E — la aritmetica de la quema del Cotizador V2.

Funciones puras, sin base de datos y sin sesion. Viven aparte por el mismo
motivo que las de 010D: son las que deciden cuanto cuesta encender el horno, y
una formula que solo existe dentro de un servicio asincrono es una formula que
nadie puede fijar con una prueba de una linea.

## El principio de la fase

**El horno se enciende entero.** Una hornada al 60 % cuesta lo mismo que una al
100 %: el gas se quema igual y la tarifa se cobra igual. De ahi la unica regla
que gobierna todo este modulo:

    hornadas = techo(volumen / capacidad)
    costo    = hornadas x tarifa_completa

## Lo que este modulo NO hace, y no es un olvido

**No existe el factor por ocupacion.** El motor historico multiplica el costo
de una linea por un factor que crece cuando la pieza ocupa poco horno —hasta
x3—. En V2 eso desaparecio: la ocupacion sirve para saber cuanto cabe, cuantas
hornadas hacen falta y como repartir el costo entre productos, jamas para
multiplicar un precio. Aqui no se importan `occupancy_bracket` ni
`resolve_factor`, y hay una prueba que lo comprueba leyendo el codigo.

**No prorratea.** Ni la tarifa ni el gas. La segunda hornada de una produccion
al 160 % va al 60 % de carga y cuesta una hornada COMPLETA.

**No decide por nadie.** Calcula recomendaciones —«esto cabria en un horno mas
chico»— y las devuelve como datos. Cambiar el horno o el tipo de produccion es
una decision humana.

## Lo que si se reutiliza

La geometria y el conteo de hornadas ya existian y estaban validados en
`app.core.firings`: volumen de una pieza, ocupacion fisica y techo exacto de
hornadas. Reescribirlos habria significado mantener dos versiones de la misma
division y descubrir tarde que no coinciden. Lo que NO se reutiliza de ese
modulo es su mitad comercial, que es justo la que esta fase elimina.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from app.core.firings import (
    line_volume,
    physical_occupancy_percentage,
    required_batches,
)
from app.core.precision import CALCULATION_SCALE, QUANTITY_SCALE

ZERO = Decimal(0)
HUNDRED = Decimal(100)

#: Pasos de redondeo, uno por columna donde se guarda cada cosa.
_VOLUME_STEP = Decimal(1).scaleb(-QUANTITY_SCALE)
_PERCENT_STEP = Decimal(1).scaleb(-QUANTITY_SCALE)
_ALLOCATION_STEP = Decimal(1).scaleb(-CALCULATION_SCALE)

#: Tope de hornadas que se detallan una a una en la respuesta. El calculo no se
#: limita —una produccion enorme sigue costando lo que cueste—; lo que se acota
#: es la lista de cargas por hornada, que es material de pantalla. Sin tope, un
#: volumen mal tecleado devolveria un millon de filas.
MAX_DETAILED_BATCHES = 50


class FiringMathError(ValueError):
    """Una entrada con la que no se puede calcular la quema.

    Se distingue de un aviso a proposito. Un aviso deja seguir —un borrador a
    medias es legitimo— y esto no: son datos con los que cualquier resultado
    seria inventado.
    """


def quantize_volume(value: Decimal) -> Decimal:
    """Volumen a la escala con la que se guarda."""
    return value.quantize(_VOLUME_STEP, rounding=ROUND_HALF_UP)


def quantize_percent(value: Decimal) -> Decimal:
    """Porcentaje de ocupacion a la escala con la que se guarda."""
    return value.quantize(_PERCENT_STEP, rounding=ROUND_HALF_UP)


def piece_volume(
    length_cm: Decimal | None, width_cm: Decimal | None, height_cm: Decimal | None
) -> Decimal:
    """Volumen de UNA pieza, en cm3. Cero si falta o no sirve alguna medida.

    Cero y no un error: un borrador a medio llenar es legitimo —se anade la
    linea, se elige la pasta y las medidas llegan despues— y reventar ahi
    dejaria la cotizacion sin poder guardarse. Quien llama avisa.
    """
    if length_cm is None or width_cm is None or height_cm is None:
        return ZERO
    if length_cm <= ZERO or width_cm <= ZERO or height_cm <= ZERO:
        return ZERO
    unitario, _total = line_volume(1, length_cm, width_cm, height_cm)
    return quantize_volume(unitario)


def total_volume(unit_volume_cm3: Decimal, quantity: int) -> Decimal:
    """Volumen de la linea entera."""
    if quantity <= 0 or unit_volume_cm3 <= ZERO:
        return ZERO
    return quantize_volume(unit_volume_cm3 * Decimal(quantity))


def occupancy_percent(volume_cm3: Decimal, capacity_cm3: Decimal) -> Decimal:
    """Que porcentaje del horno ocupa un volumen.

    Puede pasar de 100: eso es exactamente lo que hay que poder decir. Un 160 %
    no es un error, son dos hornadas.
    """
    if capacity_cm3 <= ZERO:
        raise FiringMathError("La capacidad del horno tiene que ser mayor que cero")
    if volume_cm3 <= ZERO:
        return ZERO
    return quantize_percent(physical_occupancy_percentage(volume_cm3, capacity_cm3))


def firing_count(volume_cm3: Decimal, capacity_cm3: Decimal) -> int:
    """Cuantas hornadas hacen falta. Techo exacto, nunca `float`.

    Se calcula sobre VOLUMENES y no sobre el porcentaje ya redondeado, que es
    la trampa de esta fase: una ocupacion de 100,0000004 % se guarda como
    100,000000 y, contada desde ahi, diria una sola hornada cuando el volumen
    no cabe. El porcentaje es para mirarlo; el conteo, para cobrarlo.

    Capacidad exacta es UNA hornada —100/100 da 1, no 2— y un cm3 de mas ya
    obliga a la segunda.
    """
    if capacity_cm3 <= ZERO:
        raise FiringMathError("La capacidad del horno tiene que ser mayor que cero")
    if volume_cm3 <= ZERO:
        return 0
    return required_batches(volume_cm3, capacity_cm3)


def batch_loads(occupancy: Decimal, count: int) -> tuple[Decimal, ...]:
    """Con cuanta carga va cada hornada, para poder verlo.

    La primera se llena, la segunda recibe lo que sobra. Es informacion de
    pantalla y nada mas: la ultima hornada, al 60 %, cuesta lo mismo que las
    demas. Si alguien intentara cobrar con estos numeros estaria prorrateando,
    que es justo lo que la fase prohibe.
    """
    if count <= 0:
        return ()
    cargas: list[Decimal] = []
    for indice in range(min(count, MAX_DETAILED_BATCHES)):
        restante = occupancy - Decimal(indice) * HUNDRED
        cargas.append(min(max(restante, ZERO), HUNDRED))
    return tuple(cargas)


def firing_cost(count: int, rate_per_batch: Decimal) -> Decimal:
    """Lo que cuestan N hornadas a tarifa completa.

    Sin prorrateo y sin descuento por hornada incompleta. Es la regla economica
    de la fase escrita como una multiplicacion.
    """
    if count < 0:
        raise FiringMathError("El numero de hornadas no puede ser negativo")
    if rate_per_batch < ZERO:
        raise FiringMathError("La tarifa no puede ser negativa")
    return Decimal(count) * rate_per_batch


def firing_difference(commercial_total: Decimal, gas_total: Decimal) -> Decimal:
    """Lo que deja la quema por si sola: lo que se cobra menos lo que cuesta.

    NO es el margen de la cotizacion. Es la diferencia de UN componente, y
    etiquetarla como ganancia final haria creer que ya estan descontados los
    materiales, la mano de obra y el resto.
    """
    return commercial_total - gas_total


def allocate_by_volume(total: Decimal, volumes: Sequence[Decimal]) -> list[Decimal]:
    """Reparte un costo entre lineas segun el volumen que ocupa cada una.

    La quema es GLOBAL: el horno se enciende para todo el pedido a la vez. Lo
    que cada producto absorbe es su participacion en el volumen total, no una
    hornada suya. Dos productos al 20 % y al 30 % comparten una hornada y se
    reparten su costo 40/60, no dos hornadas.

    **La suma de lo repartido es exactamente el total.** Las participaciones
    casi nunca son exactas —un tercio no cabe en dieciocho decimales— y
    redondear cada parte por su cuenta deja un sobrante de centesimas de
    centimo que no se cobraria a nadie. Se reparte por defecto y el resto se
    entrega, de a un paso, a quien mas perdio al truncar: es el metodo del
    resto mayor, y es reproducible.

    Sin volumen no hay reparto posible y se devuelven ceros. No es una perdida:
    sin volumen tampoco hay hornadas, y por tanto no hay nada que repartir.
    """
    if not volumes:
        return []
    suma = sum(volumes, ZERO)
    if suma <= ZERO or total == ZERO:
        return [ZERO for _ in volumes]

    crudos = [total * volumen / suma if volumen > ZERO else ZERO for volumen in volumes]
    partes = [valor.quantize(_ALLOCATION_STEP, rounding=ROUND_DOWN) for valor in crudos]

    objetivo = total.quantize(_ALLOCATION_STEP, rounding=ROUND_HALF_UP)
    faltante = objetivo - sum(partes, ZERO)
    pasos = int((faltante / _ALLOCATION_STEP).to_integral_value(rounding=ROUND_HALF_UP))
    if pasos > 0:
        # Quien mas parte perdio al truncar cobra primero. El desempate por
        # volumen y por posicion hace el resultado identico en cada ejecucion:
        # sin el, dos lineas iguales podrian intercambiarse el ultimo paso y la
        # cotizacion cambiaria sola al recalcularse.
        orden = sorted(
            range(len(partes)),
            key=lambda indice: (crudos[indice] - partes[indice], volumes[indice], -indice),
            reverse=True,
        )
        for indice in orden[:pasos]:
            partes[indice] += _ALLOCATION_STEP
    return partes


def volume_share_percent(line_volume_cm3: Decimal, total_volume_cm3: Decimal) -> Decimal:
    """Participacion de una linea en el volumen total, en porcentaje.

    Es la base del reparto y se devuelve para poder auditarlo: sin ella, una
    asignacion de S/180 sobre S/450 es un numero que nadie puede comprobar.
    """
    if total_volume_cm3 <= ZERO:
        return ZERO
    return quantize_percent(line_volume_cm3 / total_volume_cm3 * HUNDRED)


__all__ = [
    "MAX_DETAILED_BATCHES",
    "FiringMathError",
    "allocate_by_volume",
    "batch_loads",
    "firing_cost",
    "firing_count",
    "firing_difference",
    "occupancy_percent",
    "piece_volume",
    "quantize_percent",
    "quantize_volume",
    "total_volume",
    "volume_share_percent",
]
