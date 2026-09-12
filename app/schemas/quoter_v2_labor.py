"""Contrato publico de la mano de obra del Cotizador V2.

Tres superficies distintas y conviene no mezclarlas:

- los **maestros** —trabajadores y tecnicas—, que son politica del taller;
- las **tareas** de una cotizacion, que congelan lo que uso ESA cotizacion;
- la **ilustracion**, que es una sola por cotizacion y no una tecnica mas.

Lo que el navegador manda son intenciones —quien, que tecnica, cuantas piezas—
y lo que recibe son horas y costos ya calculados. La tarifa por hora solo viaja
de ida cuando es un acuerdo explicito para esa cotizacion; el resto del tiempo
se deriva del jornal y la jornada, que es lo unico que se configura.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.quoter_v2_labor import V2WorkerType

#: Topes holgados. Frenan un cero de mas al teclear, no opinan sobre el negocio.
MAX_MONEY = Decimal("10000000")
MAX_QUANTITY = Decimal("1000000")
MAX_HOURS = Decimal("100000")
MAX_UNIT_COST = Decimal("1000000")
#: Un dia tiene 24 horas y eso no es negociable por configuracion.
MAX_WORKDAY_HOURS = Decimal(24)


# ---------------------------------------------------------------------------
# Trabajadores
# ---------------------------------------------------------------------------
class V2WorkerCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    worker_type: V2WorkerType
    #: Lo que cuesta un dia de esta persona. Cero es legitimo —una colaboracion
    #: sin costo declarado— pero negativo no significa nada.
    daily_rate: Decimal = Field(ge=0, le=MAX_MONEY)
    #: Jornada propia. Ausente significa «la del taller», no cero: copiar aqui
    #: la global permitiria que se separaran sin que nadie lo pidiera.
    workday_hours: Decimal | None = Field(default=None, gt=0, le=MAX_WORKDAY_HOURS)
    active: bool = True
    notes: str | None = Field(default=None, max_length=2000)


class V2WorkerUpdateIn(BaseModel):
    """Cambio parcial. Lo ausente se conserva; lo presente manda."""

    model_config = ConfigDict(extra="forbid")

    #: La version que el formulario leyo. Obligatoria: sin ella, dos
    #: administradores con la ficha abierta se pisan sin ver un conflicto.
    expected_version: int = Field(ge=1)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    worker_type: V2WorkerType | None = None
    daily_rate: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)
    workday_hours: Decimal | None = Field(default=None, gt=0, le=MAX_WORKDAY_HOURS)
    active: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


class V2WorkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    worker_type: V2WorkerType
    active: bool
    daily_rate: Decimal
    #: Lo que declara la ficha. NULL significa que usa la del taller.
    workday_hours: Decimal | None
    #: La que se acaba usando, ya resuelta. Se devuelve para que la pantalla no
    #: tenga que saber de donde sale.
    effective_workday_hours: Decimal
    #: `jornal / jornada`. Derivada, nunca guardada: un numero almacenado se
    #: desincronizaria en cuanto alguien editara el jornal por otra via.
    hourly_rate: Decimal
    notes: str | None
    version: int


class V2WorkerPage(BaseModel):
    items: list[V2WorkerOut]


# ---------------------------------------------------------------------------
# Tecnicas
# ---------------------------------------------------------------------------
class V2TechniqueCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    #: Lo que rinde UNA jornada. Estandar configurado, no medicion: el sistema
    #: no aprende de la productividad de nadie.
    default_capacity_per_workday: Decimal = Field(gt=0, le=MAX_QUANTITY)
    unit: str = Field(default="piezas", min_length=1, max_length=32)
    #: Si solo tiene sentido sobre una pieza esmaltada. Marca del catalogo, no
    #: una lista de nombres en el codigo.
    requires_glaze: bool = False
    active: bool = True
    notes: str | None = Field(default=None, max_length=2000)


class V2TechniqueUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)

    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    default_capacity_per_workday: Decimal | None = Field(default=None, gt=0, le=MAX_QUANTITY)
    unit: str | None = Field(default=None, min_length=1, max_length=32)
    requires_glaze: bool | None = None
    active: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


class V2TechniqueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    active: bool
    default_capacity_per_workday: Decimal
    unit: str
    requires_glaze: bool
    #: `capacidad / jornada`. Se devuelve calculado para que la pantalla no
    #: repita la division y pueda invertirla.
    units_per_hour: Decimal
    notes: str | None
    version: int


class V2TechniquePage(BaseModel):
    items: list[V2TechniqueOut]


# ---------------------------------------------------------------------------
# Tareas de una cotizacion
# ---------------------------------------------------------------------------
class V2LaborIn(BaseModel):
    """Una tarea: quien, que tecnica y cuanto.

    Los dos `*_override` son DECISIONES de esta cotizacion, no resultados. Por
    eso son lo unico que puede viajar de ida: las horas y el costo los calcula
    el backend.
    """

    model_config = ConfigDict(extra="forbid")

    #: El producto al que pertenece la tarea. Ausente es legitimo: el personal
    #: adicional puede apoyar al pedido entero.
    v2_quotation_product_id: int | None = Field(default=None, ge=1)
    worker_id: int | None = Field(default=None, ge=1)
    technique_id: int | None = Field(default=None, ge=1)
    quantity: Decimal | None = Field(default=None, ge=0, le=MAX_QUANTITY)

    #: Tarifa acordada para ESTA cotizacion, sin tocar el maestro. Presente y en
    #: nulo retira el acuerdo; ausente lo conserva.
    hourly_rate_override: Decimal | None = Field(default=None, ge=0, le=MAX_UNIT_COST)
    #: Horas acordadas para ESTE encargo. No cambian el rendimiento estandar:
    #: un jarron dificil no baja el estandar de manana.
    final_hours_override: Decimal | None = Field(default=None, ge=0, le=MAX_HOURS)

    #: Personal traido para el pedido. Suma costo y no resta plazo.
    is_additional_personnel: bool = False


class V2LaborOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sort_order: int
    v2_quotation_product_id: int | None

    worker_id: int
    worker_name: str
    worker_type: V2WorkerType
    daily_rate: Decimal
    workday_hours: Decimal
    hourly_rate: Decimal
    rate_overridden: bool

    technique_id: int
    technique_name: str
    technique_unit: str
    standard_capacity: Decimal

    quantity: Decimal
    #: Lo que sale del estandar.
    calculated_hours: Decimal
    #: Lo que se va a cobrar.
    final_hours: Decimal
    hours_overridden: bool
    is_additional_personnel: bool
    labor_cost: Decimal

    warnings: list[str] = []


class V2WorkerLoadOut(BaseModel):
    """Cuanto se le ha asignado a una persona en la cotizacion entera.

    Por cotizacion y no por producto: tres tareas de la misma persona en tres
    productos distintos son UNA jornada repartida, y mirarlas por separado haria
    creer que ninguna llega al limite.
    """

    worker_id: int
    worker_name: str
    workday_hours: Decimal
    assigned_hours: Decimal
    #: Si no cabe en su jornada. Es un AVISO: la solucion —dia largo, dos dias o
    #: mas gente— la elige una persona, y no hay recargo automatico.
    exceeds_workday: bool
    #: Dias que harian falta si nadie alargara la jornada. Sugerencia.
    minimum_days: int


class V2LaborPage(BaseModel):
    items: list[V2LaborOut]
    #: Suma de la mano de obra. Sin factor y sin redondear: el precio es 010F.
    labor_cost: Decimal
    workday_load: list[V2WorkerLoadOut]
    #: El minimo de dias segun las horas asignadas. NO es lo que se cobrara.
    suggested_work_days: int
    #: Lo que decidio quien planifica. NULL: todavia no se ha decidido.
    effective_work_days: int | None


# ---------------------------------------------------------------------------
# Planificacion e ilustracion
# ---------------------------------------------------------------------------
class V2PlanningIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Dias de taller que se van a usar. Es una decision: diez horas caben en un
    #: dia largo o en dos dias. 010F cobrara el espacio por este numero.
    effective_work_days: int | None = Field(default=None, ge=0, le=3650)


class V2IllustrationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    illustration_enabled: bool | None = None
    illustration_quantity: Decimal | None = Field(default=None, ge=0, le=MAX_QUANTITY)
    illustration_notes: str | None = Field(default=None, max_length=2000)
    #: Acuerdo para esta cotizacion. La configuracion global no se toca.
    illustration_hourly_rate_override: Decimal | None = Field(default=None, ge=0, le=MAX_UNIT_COST)


class V2IllustrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    quantity: Decimal
    notes: str | None
    #: Lo que valia ilustrar cuando se cotizo. Subir manana el jornal no
    #: reescribe este numero.
    daily_rate: Decimal | None
    workday_hours: Decimal | None
    capacity_per_workday: Decimal | None
    hourly_rate: Decimal | None
    hours: Decimal
    cost: Decimal
