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

#: Fase 010P. Topes de entrada: sin ellos un error de tecleo llegaria al CHECK.
MAX_HOURLY_COST = Decimal("1000000")
MAX_PASSIVE_HOURS = Decimal("100000")


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
    #: Fase 010P. Costo de espacio por HORA acordado para ESTA cotizacion. Nulo
    #: retira el acuerdo y vuelve el congelado al crear.
    space_cost_per_hour_override: Decimal | None = Field(default=None, ge=0, le=MAX_HOURLY_COST)
    #: Secado, espera... Solo se SUGIERE en precio; nunca suma a un costo.
    passive_time_hours: Decimal | None = Field(default=None, ge=0, le=MAX_PASSIVE_HOURS)


class V2ExternalWorkerOut(BaseModel):
    """Un externo del pedido: su tarifa congelada, sus jornales y sus dos costos."""

    worker_id: int
    name: str | None = None
    daily_rate: Decimal
    workday_hours: Decimal
    hourly_equivalent: Decimal
    days_paid: int
    commercial_cost: Decimal
    real_cost: Decimal


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
    #: Fase 010P. Su parte del personal externo del PEDIDO, repartida por peso
    #: de minutos activos, y sus minutos activos.
    external_commercial_cost: Decimal = Decimal(0)
    external_real_cost: Decimal = Decimal(0)
    line_active_minutes: Decimal | None = None

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
    #: Los adicionales del Excel: empaque especial, molde, sello. Entran en las
    #: dos bases de costo, igual que la administracion.
    extras_cost: Decimal
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
    #: Fase 010J. El factor del precio objetivo (x3 por defecto).
    factor_target: Decimal | None = None
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

    # ---- Fase 010P: tiempo, personal externo, espacio y umbral -----------
    #: 1 = reglas anteriores a 010P (lo emitido conserva su historia); 2 = 010P.
    pricing_rules_version: int = 2
    #: El MAXIMO de los minutos activos de las lineas (productos en paralelo).
    active_production_minutes: Decimal = Decimal(0)
    active_production_hours: Decimal = Decimal(0)
    #: Lo que se imputa al cliente, lo que paga el taller y la diferencia. El
    #: margen y la ganancia se miden contra el costo REAL.
    commercial_external_labor_cost: Decimal = Decimal(0)
    real_external_labor_cost: Decimal = Decimal(0)
    labor_cost_gap: Decimal = Decimal(0)
    external_workers: list[V2ExternalWorkerOut] = []
    space_cost_per_hour_snapshot: Decimal | None = None
    space_cost_per_hour_override: Decimal | None = None
    effective_space_cost_per_hour: Decimal = Decimal(0)
    #: Tiempo pasivo informado y lo que costaria considerarlo. NO esta en
    #: ningun total: es una palanca para decidir el precio.
    passive_time_hours: Decimal = Decimal(0)
    passive_space_suggestion: Decimal = Decimal(0)
    #: Umbral por mayor congelado, unidades del pedido y si se sugiere.
    wholesale_threshold: int | None = None
    total_units: int = 0
    wholesale_suggested: bool = False
    wholesale_suggestion_declined: bool = False

    lines: list[V2PricingLineOut]
    #: Lo que conviene mirar. Avisos, nunca bloqueos: un borrador a medias
    #: tiene que poder guardarse.
    warnings: list[str] = []


class V2ReductionOut(BaseModel):
    """Una palanca para bajar el precio y el subtotal que dejaria. Fase 010J."""

    code: str
    applicable: bool
    cost_reduction: Decimal
    savings: Decimal
    estimated_subtotal: Decimal
    suggestion: str | None = None


class V2ReductionsOut(BaseModel):
    """Reducciones sugeridas. Estimaciones antes del redondeo; nada se aplica."""

    current_subtotal: Decimal
    commercial_factor: Decimal | None
    #: En moneda base (PEN), como los costos.
    currency_code: str
    items: list[V2ReductionOut]
    warnings: list[str] = []
