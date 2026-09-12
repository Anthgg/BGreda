"""Fase 010F — la aritmetica economica final del Cotizador V2.

Funciones puras, sin base de datos y sin sesion. Aqui se decide el PRECIO, asi
que es el modulo donde un error no revienta: cobra de menos durante meses.

## El orden importa, y es este

    costo directo por producto
      + costos generales repartidos (quema, espacio, administracion)
      = COSTO DE PRODUCCION
      x factor global
      = precio de la linea
      / cantidad            -> precio unitario
      redondeado al escalon -> precio unitario comercial
      x cantidad            -> subtotal de la linea
      + IGV                 -> total

Cada paso depende del anterior y **ninguno se puede adelantar**. Las tres
inversiones que este modulo existe para impedir:

1. **redondear antes de tiempo.** Redondear un componente interno arrastra el
   error por toda la cadena y multiplica por tres lo que sobra;
2. **aplicar el IGV antes del factor.** El IGV no es ingreso del taller:
   multiplicarlo por tres seria cobrar al cliente el impuesto triplicado;
3. **aplicar el factor por producto.** El factor es UNO por cotizacion. Dos
   factores distintos dentro del mismo documento no son una negociacion: son
   dos precios que nadie puede explicar juntos.

## Dos costos, no uno

**Costo real** es lo que de verdad sale del bolsillo: el gas que se quema.
**Costo de produccion** es la base comercial: la tarifa de quema que el taller
cobra por encender. Se calculan con los mismos componentes salvo ese, y su
diferencia es lo que la quema aporta. Confundirlos invierte el margen entero.

## Por que el redondeo va hacia arriba y su propia funcion

`ceil_to_step` repite a proposito lo que hace `app.core.pricing.ceil_to_step`
del motor historico. No se importa porque `app.core.pricing` es Legacy y el
dominio V2 no puede tocarlo —la prueba de aislamiento lo comprueba—, y porque
aquella version arrastra ademas una tabla de escalones admitidos y una politica
de reparto de costos fijos que son del otro motor. Lo que se comparte es la
regla del negocio, no el codigo.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal

from app.core.precision import CALCULATION_SCALE, MONEY_SCALE

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)

#: Pasos de redondeo, uno por columna donde se guarda cada cosa.
_MONEY_STEP = Decimal(1).scaleb(-MONEY_SCALE)
_ALLOCATION_STEP = Decimal(1).scaleb(-CALCULATION_SCALE)


class PricingMathError(ValueError):
    """Una entrada con la que no se puede calcular un precio.

    Se distingue de un aviso a proposito. Un aviso deja seguir —un borrador a
    medias es legitimo— y esto no: son datos con los que cualquier precio
    seria inventado.
    """


def quantize_money(value: Decimal) -> Decimal:
    """Un importe a la escala con la que se guarda y se ensena."""
    return value.quantize(_MONEY_STEP, rounding=ROUND_HALF_UP)


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Redondea SIEMPRE hacia arriba al multiplo de `step`.

    8,30 con paso 0,50 sube a 8,50; 8,90 sube a 9,00. Un valor que ya es
    multiplo exacto no se mueve: 8,50 sigue siendo 8,50.

    Hacia arriba y no al mas cercano porque es el precio que se le dice al
    cliente: redondear hacia abajo regala la diferencia en cada unidad, y
    multiplicada por la cantidad deja de ser calderilla.

    El cociente se lleva a entero con `ROUND_CEILING` sobre `Decimal` y no con
    `math.ceil` sobre un flotante: 142,50 / 0,50 en coma flotante da
    285,00000000000006, y el techo de eso es 286 —un escalon entero de mas—.
    """
    if value < ZERO:
        raise PricingMathError("No se redondea un importe negativo")
    if step <= ZERO:
        raise PricingMathError("El paso de redondeo tiene que ser mayor que cero")
    escalones = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return quantize_money(escalones * step)


def share(part: Decimal, total: Decimal) -> Decimal:
    """Participacion de una parte en un total. Cero si no hay total.

    Sin total no hay participacion posible, y devolver cero es mas honesto que
    inventar un reparto igualitario que nadie pidio: quien llama decide si usa
    otra base.
    """
    if total <= ZERO:
        return ZERO
    return part / total


def allocate_by_weight(total: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """Reparte un importe entre lineas segun un peso, sin perder un centimo.

    **La suma de lo repartido es exactamente el total.** Las participaciones
    casi nunca son exactas —un tercio no cabe en dieciocho decimales— y
    redondear cada parte por su cuenta deja un sobrante que no se cobraria a
    nadie. Se reparte por defecto y el resto se entrega, de a un paso, a quien
    mas perdio al truncar: es el metodo del resto mayor.

    El desempate es determinista —por peso y por posicion— para que dos
    ejecuciones den el mismo resultado. Sin el, dos lineas iguales podrian
    intercambiarse el ultimo paso y la cotizacion cambiaria sola al
    recalcularse.

    Sin peso no hay reparto: se devuelven ceros y quien llama decide si
    corresponde otra base. Es la misma regla que 010E aplico a la quema.
    """
    if not weights:
        return []
    suma = sum(weights, ZERO)
    if suma <= ZERO or total == ZERO:
        return [ZERO for _ in weights]

    crudos = [total * peso / suma if peso > ZERO else ZERO for peso in weights]
    partes = [valor.quantize(_ALLOCATION_STEP, rounding=ROUND_DOWN) for valor in crudos]

    objetivo = total.quantize(_ALLOCATION_STEP, rounding=ROUND_HALF_UP)
    faltante = objetivo - sum(partes, ZERO)
    pasos = int((faltante / _ALLOCATION_STEP).to_integral_value(rounding=ROUND_HALF_UP))
    if pasos > 0:
        orden = sorted(
            range(len(partes)),
            key=lambda indice: (crudos[indice] - partes[indice], weights[indice], -indice),
            reverse=True,
        )
        for indice in orden[:pasos]:
            partes[indice] += _ALLOCATION_STEP
    return partes


def apply_factor(cost: Decimal, factor: Decimal) -> Decimal:
    """El precio comercial de un costo: costo x factor.

    Es un MULTIPLICADOR y no un porcentaje anadido encima. x2 sobre 1.000 son
    2.000, no 3.000. La confusion entre «x2» y «+200 %» es facil de cometer y
    duplica el precio sin que ninguna cuenta parezca rara.
    """
    if cost < ZERO:
        raise PricingMathError("El costo no puede ser negativo")
    if factor <= ZERO:
        raise PricingMathError("El factor tiene que ser mayor que cero")
    return cost * factor


def unit_price(line_price: Decimal, quantity: int, exchange_rate: Decimal | None) -> Decimal:
    """Precio de UNA pieza, ya en la moneda de la cotizacion.

    El costo se calcula siempre en la moneda base; si la cotizacion esta en
    otra, aqui —y solo aqui— se convierte. Convertir antes obligaria a
    convertir cada componente y a acertar siempre; convertir despues del
    redondeo daria un precio unitario que no es multiplo del escalon.

    Cantidad cero da precio cero. Una linea sin piezas no tiene precio
    unitario, y dividir entre cero para averiguarlo seria peor.
    """
    if quantity <= 0:
        return ZERO
    if line_price < ZERO:
        raise PricingMathError("El precio de la linea no puede ser negativo")
    unitario = line_price / Decimal(quantity)
    if exchange_rate is None:
        return unitario
    if exchange_rate <= ZERO:
        raise PricingMathError("El tipo de cambio tiene que ser mayor que cero")
    return unitario / exchange_rate


def tax_amount(subtotal: Decimal, tax_percent: Decimal) -> Decimal:
    """El IGV de un subtotal. El porcentaje viaja como 18, no como 0,18.

    Se aplica SOBRE el subtotal ya redondeado y reconstruido desde los precios
    unitarios, que es lo que el cliente ve sumado. Calcularlo sobre un total
    anterior al redondeo daria un impuesto que no cuadra con las lineas del
    documento.
    """
    if subtotal < ZERO:
        raise PricingMathError("El subtotal no puede ser negativo")
    if tax_percent < ZERO:
        raise PricingMathError("El impuesto no puede ser negativo")
    return subtotal * tax_percent / HUNDRED


def to_base_currency(amount: Decimal, exchange_rate: Decimal | None) -> Decimal:
    """Un importe en la moneda de la cotizacion, llevado a la moneda base.

    Hace falta para comparar un precio en dolares contra un costo en soles. Si
    no hay tipo de cambio es que la cotizacion ya esta en la moneda base y no
    hay nada que convertir: devolver el importe tal cual es la respuesta, no un
    caso sin cubrir.
    """
    if exchange_rate is None:
        return amount
    if exchange_rate <= ZERO:
        raise PricingMathError("El tipo de cambio tiene que ser mayor que cero")
    return amount * exchange_rate


def margin_percent(profit: Decimal, price: Decimal) -> Decimal:
    """Que porcentaje del precio es ganancia. Cero si no hay precio.

    Sobre el PRECIO y no sobre el costo: son dos numeros distintos y el
    negocio habla del primero. Un margen del 75 % sobre precio es un recargo
    del 300 % sobre costo, y confundirlos hace irreconocible la cifra.
    """
    if price <= ZERO:
        return ZERO
    return profit / price * HUNDRED


def within_factor_range(factor: Decimal, minimum: Decimal, maximum: Decimal) -> bool:
    """Si el factor cabe en el rango que la cotizacion congelo.

    Los limites viajan con la cotizacion: una emitida a x2 cuando el minimo era
    x2 sigue siendo valida aunque hoy el minimo sea otro.
    """
    return minimum <= factor <= maximum


__all__ = [
    "PricingMathError",
    "allocate_by_weight",
    "apply_factor",
    "ceil_to_step",
    "margin_percent",
    "quantize_money",
    "share",
    "tax_amount",
    "to_base_currency",
    "unit_price",
    "within_factor_range",
]
