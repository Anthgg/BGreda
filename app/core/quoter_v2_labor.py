"""Fase 010D — la aritmetica de la mano de obra del Cotizador V2.

Funciones puras, sin base de datos y sin sesion. Viven aparte porque son las
que deciden cuanto cuesta producir, y una formula que solo existe dentro de un
servicio asincrono es una formula que nadie puede fijar con una prueba de una
linea.

## El principio de la fase

El costo NO sale de una tarifa por tecnica. Sale de:

    trabajador + jornada + tarifa + horas requeridas

«Torno = S/110» seria una constante escondida que deja de ser cierta en cuanto
cambia un jornal, y nadie se entera. Lo que se configura es el jornal de cada
persona y el rendimiento estandar de cada tecnica; el precio de una tarea es
una consecuencia de esos dos hechos.

## Los tres errores que estas funciones existen para impedir

Ninguno lanza una excepcion. Los tres producen un numero creible:

1. **cobrar jornadas completas donde hay horas sueltas.** Tres horas de torno
   no son un jornal: son tres horas. Con `ceil` cada tarea de una hora costaria
   un dia entero, y una cotizacion de cuatro tareas cortas se iria al triple;
2. **cobrar la misma jornada varias veces.** Si una persona hace torno, asas y
   vidriado para el mismo pedido, es UNA jornada repartida, no tres;
3. **invertir la division del rendimiento.** `50 piezas / 8 h` son 6,25 piezas
   por hora; multiplicar en vez de dividir da 400, y 75 piezas saldrian en
   once minutos.

## Por que no hay redondeo a dias aqui

Repartir diez horas en un dia largo o en dos dias es una DECISION de quien
planifica, no una consecuencia aritmetica. Este modulo calcula el minimo
imprescindible para poder proponerlo, y nada mas: quien decide es una persona.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.core.precision import QUANTITY_SCALE, UNIT_COST_SCALE

ZERO = Decimal(0)

#: Pasos de redondeo, uno por columna donde se guarda cada cosa.
_HOURS_STEP = Decimal(1).scaleb(-QUANTITY_SCALE)
_RATE_STEP = Decimal(1).scaleb(-UNIT_COST_SCALE)


def quantize_hours(value: Decimal) -> Decimal:
    """Horas a la escala con la que se guardan.

    Existe para que la fila pueda explicarse con sus propios numeros. Las horas
    salen de una division —`cantidad x jornada / capacidad`— que no siempre es
    exacta: una pieza con capacidad 3 da 2,666666666... y la columna guarda
    2,666667. Si el costo se calculara con el numero largo, la fila diria
    «2,666667 horas a S/15» junto a un importe que no es su producto, y nadie
    podria comprobar el cobro sin recalcularlo por fuera.

    Se redondea ANTES de multiplicar, no despues.
    """
    return value.quantize(_HOURS_STEP, rounding=ROUND_HALF_UP)


def quantize_rate(value: Decimal) -> Decimal:
    """Tarifa por hora a la escala con la que se congela.

    Mismo motivo: S/100 entre una jornada de 3 horas son 33,333333333333... y
    la columna guarda doce decimales. El costo tiene que salir de lo guardado.
    """
    return value.quantize(_RATE_STEP, rounding=ROUND_HALF_UP)


class LaborMathError(ValueError):
    """Una entrada con la que no se puede calcular mano de obra.

    Se distingue de un aviso a proposito. Un aviso deja seguir —un borrador a
    medias es legitimo— y esto no: son datos con los que cualquier resultado
    seria inventado.
    """


def hourly_rate(daily_rate: Decimal, workday_hours: Decimal) -> Decimal:
    """Tarifa por hora de un trabajador: su jornal entre su jornada.

    S/120 en 8 horas son S/15 la hora. S/110 en 8 horas, S/13,75.

    No se guarda en ninguna tabla. Guardarla obligaria a recordar recalcularla
    cada vez que cambia el jornal o la jornada, y el dia que alguien editara
    una de las dos por otra via la tarifa quedaria mintiendo sin error visible.
    Se deriva siempre, y lo unico que se congela es el resultado dentro de la
    linea de una cotizacion.
    """
    if daily_rate < ZERO:
        raise LaborMathError("El jornal no puede ser negativo")
    if workday_hours <= ZERO:
        raise LaborMathError("La jornada tiene que ser mayor que cero")
    return daily_rate / workday_hours


def units_per_hour(capacity_per_workday: Decimal, workday_hours: Decimal) -> Decimal:
    """Rendimiento por hora: lo que rinde una jornada, entre sus horas.

    50 piezas en 8 horas son 6,25 piezas por hora.

    Es un estandar CONFIGURADO, no una medicion. Que alguien haga hoy 70 piezas
    no sube este numero, y que haga 40 no lo baja: el sistema no aprende de la
    productividad de nadie. Cambiarlo es una decision de taller que se escribe
    a mano en el catalogo de tecnicas.
    """
    if capacity_per_workday <= ZERO:
        raise LaborMathError("El rendimiento estandar tiene que ser mayor que cero")
    if workday_hours <= ZERO:
        raise LaborMathError("La jornada tiene que ser mayor que cero")
    return capacity_per_workday / workday_hours


def hours_required(
    quantity: Decimal, capacity_per_workday: Decimal, workday_hours: Decimal
) -> Decimal:
    """Horas que pide una cantidad, al rendimiento estandar de la tecnica.

    75 piezas a 50 por jornada de 8 horas son 12 horas.

    Se escribe como `cantidad x jornada / capacidad` y no como
    `cantidad / (capacidad / jornada)` para no dividir dos veces: la division
    intermedia puede no ser exacta —50/7, por ejemplo— y arrastraria una cola
    de decimales a un resultado que deberia ser limpio.
    """
    if quantity < ZERO:
        raise LaborMathError("La cantidad no puede ser negativa")
    if capacity_per_workday <= ZERO:
        raise LaborMathError("El rendimiento estandar tiene que ser mayor que cero")
    if workday_hours <= ZERO:
        raise LaborMathError("La jornada tiene que ser mayor que cero")
    return quantity * workday_hours / capacity_per_workday


def labor_cost(hours: Decimal, rate_per_hour: Decimal) -> Decimal:
    """Lo que cuestan unas horas. Doce a S/13,75 son S/165.

    Horas, nunca jornadas: es la regla de la fase. Media jornada cuesta media
    jornada.
    """
    if hours < ZERO:
        raise LaborMathError("Las horas no pueden ser negativas")
    if rate_per_hour < ZERO:
        raise LaborMathError("La tarifa por hora no puede ser negativa")
    return hours * rate_per_hour


def exceeds_workday(assigned_hours: Decimal, workday_hours: Decimal) -> bool:
    """Si lo asignado a una persona no cabe en su jornada.

    Ocho horas exactas NO exceden: la comparacion es estricta. Un `>=` haria
    saltar el aviso en la jornada completa, que es justo el caso normal, y un
    aviso que salta siempre deja de leerse.
    """
    if workday_hours <= ZERO:
        raise LaborMathError("La jornada tiene que ser mayor que cero")
    return assigned_hours > workday_hours


def minimum_work_days(total_hours: Decimal, workday_hours: Decimal) -> int:
    """Cuantos dias harian falta si nadie alarga la jornada.

    Es una SUGERENCIA para poder plantear la decision, no el resultado. Diez
    horas con jornada de ocho caben en un dia largo —y entonces es un dia
    efectivo— o en dos dias —y entonces son dos—. Eso lo decide quien
    planifica, y lo que decida es lo que consumira 010F para cobrar el espacio.

    Aqui si se redondea hacia arriba, y es correcto: no existen dias de trabajo
    fraccionados. Lo que no puede redondearse son las HORAS, que es de donde
    sale el costo.
    """
    if workday_hours <= ZERO:
        raise LaborMathError("La jornada tiene que ser mayor que cero")
    if total_hours <= ZERO:
        return 0
    dias = total_hours / workday_hours
    entero = int(dias)
    return entero if dias == entero else entero + 1
