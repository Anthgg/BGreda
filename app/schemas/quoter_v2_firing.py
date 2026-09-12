"""Contrato publico de la quema del Cotizador V2.

Lo que el navegador manda son DECISIONES —que horno, si hay baja, si hay alta,
a quien se cotiza y que importes se pactaron— y lo que recibe son hornadas y
costos ya calculados. La ocupacion, el numero de hornadas y el reparto entre
productos no viajan de ida: son consecuencias, y aceptarlas del cliente seria
dejar que la pantalla decidiera cuantas veces se enciende el horno.

Dos numeros que nunca se mezclan y por eso viajan separados:

- **gas real** — lo que cuesta encender. Es COSTO.
- **tarifa de quema** — lo que se cobra por encender. Es PRECIO, y depende de
  si el cliente es externo o alumno.

Su diferencia es la ganancia propia de la quema. Si los dos compartieran campo,
esa diferencia dejaria de poder calcularse.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.quoter_v2 import V2CustomerKind

#: Topes holgados. Frenan un cero de mas al teclear, no opinan sobre el negocio.
MAX_MONEY = Decimal("10000000")


class V2FiringIn(BaseModel):
    """Las decisiones de quema de una cotizacion.

    Semantica de PATCH: lo ausente se conserva, lo presente manda. Y la regla
    que 010C y 010D aprendieron a base de borrar acuerdos: **la presencia de
    una clave no es un cambio**. Reenviar el mismo `kiln_id` no reevalua nada.
    """

    model_config = ConfigDict(extra="forbid")

    #: El horno de ESTA cotizacion. Cambiarlo no toca la configuracion global
    #: ni el tipo de produccion. En nulo lo retira: la cotizacion se queda sin
    #: quema hasta que alguien elija otro.
    kiln_id: int | None = Field(default=None, ge=1)
    #: A quien se cotiza, a efectos de TARIFA. No cambia el gas: un alumno y un
    #: externo queman el mismo gas.
    customer_kind: V2CustomerKind | None = None
    #: Cada una se puede apagar. Debe poder existir solo baja, solo alta o
    #: ambas: no hay una regla rigida y no se inventa aqui.
    low_fire_enabled: bool | None = None
    high_fire_enabled: bool | None = None

    #: Los cuatro importes por hornada, pactados DENTRO de esta cotizacion. El
    #: maestro no se toca: CTZ-001 puede usar 40 mientras el maestro dice 35 y
    #: CTZ-002 sigue con 35.
    #:
    #: Presente con valor: manda. Presente en nulo: retira el acuerdo. Ausente:
    #: conserva lo que hubiera. Un cero explicito es un valor —un horno
    #: prestado con el gas incluido— y no una ausencia.
    gas_cost_low_override: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)
    gas_cost_high_override: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)
    commercial_rate_low_override: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)
    commercial_rate_high_override: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)


class V2KilnOptionOut(BaseModel):
    """Un horno del taller y lo que pasaria si se eligiera este."""

    kiln_id: int
    code: str
    name: str
    capacity_cm3: Decimal
    active: bool
    #: Lo que ocuparia la carga actual en ESTE horno. Puede pasar de 100.
    occupancy_percent: Decimal
    #: Y cuantas hornadas pediria. Es la informacion con la que se decide.
    firing_count: int
    #: Si tiene tarifas V2 configuradas. Sin ellas no se puede costear, y la
    #: pantalla tiene que poder decirlo antes de que alguien lo elija.
    has_rates: bool


class V2FiringLineOut(BaseModel):
    """Lo que a un producto le toca de la quema."""

    line_id: int
    product_name: str | None
    quantity: int
    total_volume_cm3: Decimal
    #: Cuanto horno ocupa esta linea. Informacion, no multiplicador.
    occupancy_percent: Decimal
    #: Su participacion en el volumen total: la base del reparto.
    volume_share_percent: Decimal
    commercial_cost: Decimal
    gas_cost: Decimal


class V2FiringOut(BaseModel):
    """La quema completa de una cotizacion."""

    model_config = ConfigDict(from_attributes=True)

    production_type: str
    customer_kind: V2CustomerKind | None

    kiln_id: int | None
    kiln_name: str | None
    #: La capacidad CONGELADA. Remedir el horno manana no cambia las hornadas
    #: que esta cotizacion dice tener.
    kiln_capacity_cm3: Decimal | None

    total_volume_cm3: Decimal
    occupancy_percent: Decimal
    firing_count: int
    low_fire_enabled: bool
    high_fire_enabled: bool
    low_fire_count: int
    high_fire_count: int
    #: Con cuanta carga va cada hornada. Es informacion de pantalla: la ultima,
    #: al 60 %, cuesta exactamente lo mismo que las demas.
    batch_loads: list[Decimal]

    #: Lo que cuesta encender, por hornada. COSTO.
    gas_cost_low: Decimal | None
    gas_cost_high: Decimal | None
    gas_low_is_override: bool
    gas_high_is_override: bool
    #: Lo que se cobra, por hornada. PRECIO.
    commercial_rate_low: Decimal | None
    commercial_rate_high: Decimal | None
    commercial_low_is_override: bool
    commercial_high_is_override: bool

    gas_total: Decimal
    commercial_total: Decimal
    #: `commercial_total - gas_total`. La ganancia propia de la quema, no el
    #: margen de la cotizacion: aqui no estan descontados ni los materiales ni
    #: la mano de obra.
    difference: Decimal

    #: El horno que el sistema recomendaria. RECOMIENDA: no se aplica solo.
    recommended_kiln_id: int | None
    kilns: list[V2KilnOptionOut]
    lines: list[V2FiringLineOut]

    #: Lo que conviene mirar. Avisos y recomendaciones, nunca acciones
    #: automaticas: que una produccion por menor no quepa en su horno se dice,
    #: no se resuelve convirtiendola en por mayor.
    warnings: list[str] = []
