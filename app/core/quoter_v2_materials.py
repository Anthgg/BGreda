"""Aritmetica de materiales del Cotizador V2.

Fase 010C. Funciones puras: entran numeros, salen numeros. No hay sesion, ni
maestro, ni peticion. Todo lo que decide un costo de material vive aqui para
que se pueda comprobar con los ejemplos aprobados sin levantar nada.

Las tres reglas que gobiernan este modulo:

1. **el transporte es parte del material.** Comprar 100 kg por S/100 con S/30
   de transporte cuesta S/130, no S/100. Dividir por la cantidad el importe
   equivocado da un costo por gramo un 23 % bajo, y ese error se multiplica
   por cada gramo de cada pieza de cada cotizacion;

2. **el esmalte es el 15 % del peso de la pasta**, no el 1,5 % ni el 0,15 %.
   Se escribe como `Decimal("0.15")` y no como literal suelto en una formula
   para que solo haya un sitio donde pueda equivocarse;

3. **nada se redondea aqui.** El redondeo comercial se aplica una sola vez, al
   precio final, en 010F. Redondear cada componente por separado acumula un
   error que nadie sabe de donde salio.

Todo es `Decimal`. Un `float` binario no puede representar 0,1 exactamente, y
un costo por gramo del orden de 0,0013 pierde cifras justo donde importan.
"""

from __future__ import annotations

from decimal import Decimal

ZERO = Decimal(0)

#: Proporcion de esmalte sobre el peso de la pasta. Regla cerrada del negocio
#: y del Excel aprobado: 15 %.
GLAZE_WEIGHT_RATIO = Decimal("0.15")

#: Conversion de reserva cuando el material no declara la suya. NO es una
#: densidad universal —no existe tal cosa— sino una suposicion explicita para
#: no bloquear una cotizacion. Quien la use tiene que saber que la esta usando:
#: por eso `glaze_volume_ml` devuelve tambien si fue un fallback.
FALLBACK_ML_PER_GRAM = Decimal(1)


class MaterialMathError(ValueError):
    """Una entrada imposible, detectada antes de producir un numero falso."""


def acquisition_total_cost(purchase_cost: Decimal, transport_cost: Decimal) -> Decimal:
    """Lo que costo de verdad traer el material hasta el taller.

    El transporte no es un gasto aparte: sin el, el material no esta aqui.
    """
    if purchase_cost < ZERO or transport_cost < ZERO:
        raise MaterialMathError("Ni la compra ni el transporte pueden ser negativos")
    return purchase_cost + transport_cost


def cost_per_unit(
    purchase_cost: Decimal, transport_cost: Decimal, purchase_quantity: Decimal
) -> Decimal:
    """Costo por unidad base a partir de la adquisicion completa.

    Ejemplo aprobado: 100 kg por S/100 mas S/30 de transporte, expresados en
    gramos, son 130 / 100000 = S/0,0013 por gramo.

    Una cantidad de cero no da «coste infinito»: da un dato que no se puede
    usar, y decirlo aqui es mejor que propagar una division por cero hasta el
    precio.
    """
    if purchase_quantity <= ZERO:
        raise MaterialMathError("La cantidad adquirida tiene que ser mayor que cero")
    return acquisition_total_cost(purchase_cost, transport_cost) / purchase_quantity


def effective_cost_per_unit(derived: Decimal | None, override: Decimal | None) -> Decimal:
    """El costo con el que se COSTEA, que no siempre es el que se pago.

    Existe para el material regalado o recuperado: adquirirlo costo cero, pero
    valorizarlo a cero regalaria tambien el precio de venta de la pieza. El
    taller puede fijar un valor de costeo sin mentir sobre lo que pago: son
    dos hechos distintos y se guardan por separado.

    El override manda cuando existe, incluso si vale cero: alguien pudo
    decidir expresamente que este material no se valoriza.
    """
    if override is not None:
        if override < ZERO:
            raise MaterialMathError("El valor de costeo no puede ser negativo")
        return override
    return derived if derived is not None else ZERO


def body_total_weight(unit_weight: Decimal, quantity: int) -> Decimal:
    """Peso total de pasta de una linea: lo que lleva una pieza, por cuantas."""
    if unit_weight < ZERO:
        raise MaterialMathError("El peso por pieza no puede ser negativo")
    if quantity < 0:
        raise MaterialMathError("La cantidad no puede ser negativa")
    return unit_weight * Decimal(quantity)


def glaze_unit_weight(body_unit_weight: Decimal, *, requires_glaze: bool) -> Decimal:
    """Esmalte que lleva UNA pieza: el 15 % del peso de su pasta.

    Con el esmalte apagado devuelve cero, y cero significa cero: no hay peso,
    no hay costo y mas adelante tampoco habra tecnica de vidriado. Cobrar
    esmalte a quien lo apago seria cobrarle algo que dijo que no queria.
    """
    if not requires_glaze:
        return ZERO
    if body_unit_weight < ZERO:
        raise MaterialMathError("El peso por pieza no puede ser negativo")
    return body_unit_weight * GLAZE_WEIGHT_RATIO


def material_cost(total_weight: Decimal, cost_per_unit_value: Decimal) -> Decimal:
    """Peso por costo unitario. Sin redondear: eso es cosa de 010F."""
    if total_weight < ZERO:
        raise MaterialMathError("El peso no puede ser negativo")
    if cost_per_unit_value < ZERO:
        raise MaterialMathError("El costo unitario no puede ser negativo")
    return total_weight * cost_per_unit_value


def glaze_volume_ml(grams: Decimal, ml_per_gram: Decimal | None) -> tuple[Decimal, bool]:
    """Volumen equivalente y si se uso la conversion de reserva.

    Devuelve la pareja —valor y procedencia— a proposito. Un mililitraje
    calculado con 1:1 y otro calculado con la concentracion real del preparado
    se parecen demasiado como para distinguirlos despues, y quien los lea tiene
    derecho a saber cual de los dos esta mirando.
    """
    if grams < ZERO:
        raise MaterialMathError("Los gramos no pueden ser negativos")
    if ml_per_gram is None:
        return grams * FALLBACK_ML_PER_GRAM, True
    if ml_per_gram <= ZERO:
        # Una conversion de cero o negativa no es un dato pobre: es imposible.
        # Tomarla como valida daria un volumen cero o negativo con aspecto de
        # medida real.
        raise MaterialMathError("La conversion g/ml tiene que ser mayor que cero")
    return grams * ml_per_gram, False
