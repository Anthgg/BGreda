"""Contrato publico del motor economico del Cotizador V2.

Lo unico que el navegador puede DECIDIR aqui es el factor comercial. Todo lo
demas son consecuencias: los costos vienen de 010C a 010E, y el precio sale de
aplicarles ese factor. Aceptar un precio unitario tecleado a mano lo
desconectaria de los costos que lo explican, que es justo lo que esta fase
construye.

## Los numeros no se mezclan

Cuatro salidas distintas y cuatro campos distintos:

- **costo real** — lo que sale del bolsillo, con el GAS que se quema;
- **costo de produccion** — la base comercial, con la TARIFA de quema;
- **precio minimo** y **precio objetivo** — el suelo y el techo que la
  cotizacion congelo;
- **precio negociado** — el del factor elegido hoy.

Esconderlas dentro de un unico total haria imposible saber si una venta esta
por encima del suelo, que es la pregunta que el taller hace siempre.

## Nada de esto va al PDF del cliente

El documento comercial lleva producto, cantidad, medidas, precio unitario,
subtotal, IGV y total. El costo, el gas, el factor y la ganancia son internos,
y el PDF es de 010H.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class V2PricingIn(BaseModel):
    """La unica decision de esta superficie: el factor comercial.

    Semantica parcial: lo ausente se conserva. Es GLOBAL por cotizacion —no hay
    un factor por producto— y tiene que caber en el rango que la cotizacion
    congelo, que el backend comprueba aunque la pantalla ya lo haga.
    """

    model_config = ConfigDict(extra="forbid")

    #: Factor, no porcentaje: 3 significa x3.
    #:
    #: El limite de aqui es solo una barrera contra un cero de mas al teclear, y
    #: por eso es absurdamente alto. El rango REAL lo impone el snapshot de la
    #: cotizacion y lo valida el servicio: el suelo de x2 es una regla cerrada
    #: del negocio, pero el techo NO —x3 es el valor por defecto y la casa puede
    #: subirlo desde Configuracion—. Poner aqui un 10 habria capado en silencio
    #: un maximo que el propio taller acabara de habilitar.
    commercial_factor: Decimal | None = Field(default=None, gt=0, le=1000)


class V2PricingLineOut(BaseModel):
    """Lo que a un producto le toca del costo y del precio."""

    line_id: int
    product_name: str | None
    quantity: int

    #: Lo que cuesta la pieza por si misma: materiales mas su mano de obra.
    direct_cost: Decimal
    #: Lo que absorbe de lo que es de la cotizacion entera. Tres bases
    #: distintas: la quema por volumen, el espacio por horas y lo general por
    #: costo directo.
    firing_cost: Decimal
    gas_cost: Decimal
    space_cost: Decimal
    general_cost: Decimal
    #: Las dos bases de la linea. Una lleva la tarifa de quema; la otra, el gas.
    production_cost: Decimal
    real_cost: Decimal

    #: `costo de produccion asignado x factor`, en moneda base.
    line_price: Decimal
    #: Por pieza, en la moneda de la cotizacion. El crudo viaja junto al
    #: redondeado para poder explicar el salto.
    unit_price_raw: Decimal
    unit_price: Decimal
    #: Reconstruidos desde el unitario redondeado: es lo que hace que el
    #: documento cuadre al sumarlo a mano.
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal
    #: Subtotal en moneda base menos costo real. Puede ser negativo.
    profit: Decimal


class V2PricingOut(BaseModel):
    """El resultado economico completo de una cotizacion."""

    model_config = ConfigDict(from_attributes=True)

    # ---- Lo que cuesta ---------------------------------------------------
    materials_cost: Decimal
    labor_cost: Decimal
    illustration_cost: Decimal
    space_cost: Decimal
    administration_cost: Decimal
    #: Lo que de verdad se quema frente a lo que se cobra por encender.
    gas_cost: Decimal
    firing_commercial_cost: Decimal
    firing_difference: Decimal

    #: Materiales + mano de obra, sumados por linea. Los generales van aparte.
    direct_cost: Decimal
    #: Las DOS bases. La diferencia entre ellas es la de la quema.
    real_cost: Decimal
    production_cost: Decimal

    # ---- Lo que se cobra -------------------------------------------------
    commercial_factor: Decimal | None
    factor_min: Decimal | None
    factor_max: Decimal | None
    #: Costo de produccion por el suelo, por el techo y por el factor de hoy.
    price_min: Decimal
    price_target: Decimal
    negotiated_price: Decimal

    #: Ya en la moneda de la cotizacion. El subtotal se RECONSTRUYE sumando las
    #: lineas redondeadas, no se toma del precio negociado.
    currency_code: str | None
    exchange_rate: Decimal | None
    tax_percent: Decimal | None
    rounding_step: Decimal | None
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    #: Lo que el redondeo anadio sobre el precio negociado. Sin este numero
    #: nadie puede explicar por que el subtotal no es costo x factor.
    rounding_adjustment: Decimal

    # ---- Lo que deja -----------------------------------------------------
    #: Precio comercial SIN IGV menos costo REAL. El impuesto no es ingreso del
    #: taller. Puede ser negativo, y entonces hay que poder verlo.
    estimated_profit: Decimal
    #: Sobre el precio, no sobre el costo.
    effective_margin_percent: Decimal

    lines: list[V2PricingLineOut]
    #: Lo que conviene mirar. Avisos, nunca bloqueos: un borrador a medias
    #: tiene que poder guardarse.
    warnings: list[str] = []
