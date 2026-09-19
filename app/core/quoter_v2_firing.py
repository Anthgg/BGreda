"""Fase 010E, reescrita en 010J — la aritmetica de la quema del Cotizador V2.

Funciones puras, sin base de datos y sin sesion. Viven aparte por el mismo
motivo que las de 010D: son las que deciden cuanto cuesta encender el horno, y
una formula que solo existe dentro de un servicio asincrono es una formula que
nadie puede fijar con una prueba de una linea.

## La regla (Excel final, hoja «Quema V2»)

010E cobraba cada hornada ENTERA. El Excel corregido del dueno distingue dos
modos, y el modo lo elige quien cotiza:

    ocupacion = volumen / capacidad x 100          (puede pasar de 100)
    hornadas  = techo(ocupacion / 100)              (fisicas: cuantas veces se enciende)

    COMPARTIDA (defecto)   carga = ocupacion / 100  (30 % -> 0,30 de hornada)
    EXCLUSIVA / URGENTE    carga = hornadas         (30 % -> 1 hornada entera)

    precio de quema = carga x tarifa completa del ciclo   (por cada ciclo encendido)
    gas             = carga x gas completo del ciclo

Baja y alta son independientes: cada una cobra su tarifa y su gas si esta
encendida. Mas de 100 % se muestra como hornada 1 al 100 % y hornada 2 con el
resto; en compartida esa segunda hornada se cobra por lo que ocupa.

El volumen de una pieza es su CAJA ENVOLVENTE con la separacion entre piezas
sumada a cada medida: (L+s)(A+s)(H+s), s = 3 cm por defecto, 0 = sin
separacion. No hay algoritmo de acomodo: rotar la pieza no cambia el volumen,
porque el producto no depende del orden de las medidas.

## Lo que este modulo NO hace, y no es un olvido

**No existe el factor por ocupacion.** El motor historico multiplica el costo
de una linea por un factor que crece cuando la pieza ocupa poco horno —hasta
x3—. En V2 eso no existe: la ocupacion decide la CARGA que se cobra, jamas
multiplica un precio. Aqui no se importan `occupancy_bracket` ni
`resolve_factor`, y hay una prueba que lo comprueba leyendo el codigo.

**No usa float.** Ni para la ocupacion ni para la carga: Decimal de punta a
punta, y el techo de hornadas sale de volumenes, no del porcentaje redondeado.

**No decide por nadie.** Compara hornos y sugiere el mas barato, pero cambiar
el horno o el modo es una decision humana.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from app.core.firings import (
    line_volume,
    physical_occupancy_percentage,
    required_batches,
)
from app.core.precision import QUANTITY_SCALE, UNIT_COST_SCALE
from app.core.quoter_v2_pricing import allocate_by_weight

ZERO = Decimal(0)
HUNDRED = Decimal(100)

#: Pasos de redondeo, uno por columna donde se guarda cada cosa.
_VOLUME_STEP = Decimal(1).scaleb(-QUANTITY_SCALE)
_PERCENT_STEP = Decimal(1).scaleb(-QUANTITY_SCALE)
#: La carga facturada se guarda con doce decimales: 85320/17000 = 5,0188235294...
#: y un redondeo a seis moveria el importe en milesimas de centimo.
_LOAD_STEP = Decimal(1).scaleb(-UNIT_COST_SCALE)

#: Los dos modos, como texto: el nucleo no depende del modelo.
SHARED = "SHARED"
EXCLUSIVE = "EXCLUSIVE"

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
    length_cm: Decimal | None,
    width_cm: Decimal | None,
    height_cm: Decimal | None,
    separation_cm: Decimal = ZERO,
) -> Decimal:
    """Volumen que ocupa UNA pieza en el horno, en cm3. Cero si falta una medida.

    Caja envolvente con la separacion sumada a cada medida: (L+s)(A+s)(H+s).
    Las medidas se comprueban ANTES de sumar la separacion: una linea sin alto
    no ocupa s al cubo, ocupa cero y se avisa.

    Cero y no un error: un borrador a medio llenar es legitimo —se anade la
    linea, se elige la pasta y las medidas llegan despues— y reventar ahi
    dejaria la cotizacion sin poder guardarse. Quien llama avisa.
    """
    if length_cm is None or width_cm is None or height_cm is None:
        return ZERO
    if length_cm <= ZERO or width_cm <= ZERO or height_cm <= ZERO:
        return ZERO
    if separation_cm < ZERO:
        raise FiringMathError("La separacion entre piezas no puede ser negativa")
    unitario, _total = line_volume(
        1, length_cm + separation_cm, width_cm + separation_cm, height_cm + separation_cm
    )
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

    La primera se llena, la segunda recibe lo que sobra:
    MAX(0, MIN(100, ocupacion - (n-1) x 100)), la misma formula del Excel. Es
    informacion de operacion: lo que se cobra sale de `billed_load`.
    """
    if count <= 0:
        return ()
    cargas: list[Decimal] = []
    for indice in range(min(count, MAX_DETAILED_BATCHES)):
        restante = occupancy - Decimal(indice) * HUNDRED
        cargas.append(min(max(restante, ZERO), HUNDRED))
    return tuple(cargas)


def billed_load(volume_cm3: Decimal, capacity_cm3: Decimal, mode: str) -> Decimal:
    """Cuantas hornadas se COBRAN. Fase 010J.

    COMPARTIDA: la fraccion exacta de horno que ocupa el pedido,
    volumen/capacidad (0,30 para un 30 %; 5,0188... para un 501,88 %).
    EXCLUSIVA: las hornadas enteras, el techo (1 para un 30 %; 6 para 501,88 %).

    Se calcula sobre VOLUMENES y no sobre el porcentaje ya redondeado, por el
    mismo motivo que `firing_count`.
    """
    if capacity_cm3 <= ZERO:
        raise FiringMathError("La capacidad del horno tiene que ser mayor que cero")
    if mode not in (SHARED, EXCLUSIVE):
        raise FiringMathError(f"Modo de quema desconocido: {mode}")
    if volume_cm3 <= ZERO:
        return ZERO
    if mode == EXCLUSIVE:
        return Decimal(firing_count(volume_cm3, capacity_cm3))
    return (volume_cm3 / capacity_cm3).quantize(_LOAD_STEP, rounding=ROUND_HALF_UP)


def firing_amount(load: Decimal, full_rate: Decimal) -> Decimal:
    """Carga facturada x tarifa (o gas) de una hornada completa.

    Es la unica multiplicacion de la quema, igual para el precio y para el gas:
    los dos se mueven con la misma carga, asi que la diferencia de la quema es
    siempre carga x (tarifa - gas).
    """
    if load < ZERO:
        raise FiringMathError("La carga facturada no puede ser negativa")
    if full_rate < ZERO:
        raise FiringMathError("La tarifa no puede ser negativa")
    return load * full_rate


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

    El reparto en si —resto mayor, suma exacta, desempate determinista— vive en
    `app.core.quoter_v2_pricing`: es el mismo de 010F y tener dos copias seria
    tener dos formas de perder un centimo.
    """
    return allocate_by_weight(total, volumes)


def volume_share_percent(line_volume_cm3: Decimal, total_volume_cm3: Decimal) -> Decimal:
    """Participacion de una linea en el volumen total, en porcentaje.

    Es la base del reparto y se devuelve para poder auditarlo: sin ella, una
    asignacion de S/180 sobre S/450 es un numero que nadie puede comprobar.
    """
    if total_volume_cm3 <= ZERO:
        return ZERO
    return quantize_percent(line_volume_cm3 / total_volume_cm3 * HUNDRED)


__all__ = [
    "EXCLUSIVE",
    "MAX_DETAILED_BATCHES",
    "SHARED",
    "FiringMathError",
    "allocate_by_volume",
    "batch_loads",
    "billed_load",
    "firing_amount",
    "firing_count",
    "firing_difference",
    "occupancy_percent",
    "piece_volume",
    "quantize_percent",
    "quantize_volume",
    "total_volume",
    "volume_share_percent",
]
