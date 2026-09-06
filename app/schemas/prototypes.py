"""Contratos de la API de prototipos.

Los importes no aparecen por ninguna parte. Cobrar un prototipo se decidira en
009K.1 y hoy no existe: lo unico que el dominio fisico necesita del dinero es
que la cotizacion de origen conste cobrada, y eso se lee de la cotizacion.

Los decimales viajan como texto, como en todo el proyecto: un flotante en JSON
convierte 0.1 + 0.2 en una cifra de inventario que no cuadra.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.prototypes import (
    PrototypeApproval,
    PrototypeMaterialRole,
    PrototypeMaterialStage,
    PrototypeStatus,
)
from app.services.prototypes import PrototypeReadinessCode


class PrototypeEvaluationCriterionIn(BaseModel):
    """Un criterio evaluado de la muestra.

    Es una LISTA porque en el cuaderno del taller lo es: la misma muestra se
    juzga por medidas, por acabado, por forma y por color, cada uno con su
    resultado. Aplanarlo a un solo criterio obligaria a elegir cual de los
    cuatro se guarda.
    """

    model_config = ConfigDict(extra="forbid")

    criterion: str = Field(min_length=1, max_length=120)
    #: Vocabulario del cuaderno: Pendiente / Conforme / No conforme /
    #: Requiere ajuste. No se valida contra un enum todavia porque el taller
    #: sigue anadiendo criterios y cerrarlo hoy seria adelantarse.
    result: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=2000)
    responsible: str | None = Field(default=None, max_length=120)
    requires_adjustment: bool | None = None
    new_sample: bool | None = None


class PrototypeTechnicalSpecificationsIn(BaseModel):
    """La ficha del taller, estructurada.

    Los nombres y las unidades salen del cuaderno real
    (`Control_Prototipos_Taller_Greda.xlsx`, hoja «Especificaciones»), no de lo
    que pareciera razonable: ahi el peso es «Peso estimado g» y las medidas son
    centimetros, asi que la unidad va en el nombre del campo y no hay un campo
    de unidad que alguien pueda contradecir.

    Todo es opcional. Una muestra se registra justamente para averiguar lo que
    todavia no se sabe, y exigir el color de algo que aun no se ha esmaltado
    dejaria sin registrar la muestra.
    """

    model_config = ConfigDict(extra="forbid")

    responsible: str | None = Field(default=None, max_length=120)
    priority: str | None = Field(default=None, max_length=40)

    width_cm: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    height_cm: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    length_cm: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    depth_cm: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    estimated_weight_g: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)

    technique: str | None = Field(default=None, max_length=120)
    finish: str | None = Field(default=None, max_length=120)
    mold: str | None = Field(default=None, max_length=120)
    color: str | None = Field(default=None, max_length=120)
    reference: str | None = Field(default=None, max_length=120)
    technical_notes: str | None = Field(default=None, max_length=2000)

    requires_new_sample: bool | None = None
    evaluation: list[PrototypeEvaluationCriterionIn] = Field(default_factory=list, max_length=50)


class PrototypeMaterialIn(BaseModel):
    """Un material que la muestra va a gastar. Elegido, nunca deducido."""

    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0)
    #: En la unidad base del material. La conversion no se inventa aqui: si el
    #: producto se lleva en gramos, la cantidad son gramos.
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    #: Fase 009K.1. Cuerpo, acabado u otro. `None` en lo que no lo declara, y
    #: ese hueco es el que impide precargar el material base sin adivinar.
    material_role: PrototypeMaterialRole | None = None
    #: En que etapa del trabajo se gasta. Independiente del rol: no se deduce
    #: uno del otro aunque en muchos casos coincidan.
    stage: PrototypeMaterialStage | None = None


class PrototypeCreateIn(BaseModel):
    """Alta de una muestra. Casi todo es opcional a proposito.

    El negocio admite registrar un prototipo de algo que todavia no esta en el
    catalogo y que aun no cuelga de ningun pedido: se prototipa justamente para
    decidir si merece la pena. Lo unico irrenunciable es como se llama.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(default=1, gt=0)
    quotation_id: int | None = Field(default=None, gt=0)
    product_id: int | None = Field(default=None, gt=0)
    stock_location_id: int | None = Field(default=None, gt=0)
    target_days: int | None = Field(default=None, gt=0)
    notes: str | None = None
    #: Fase 009K.1. La ficha del taller, estructurada. `notes` vuelve a ser
    #: solo observaciones humanas: el puente al Cotizador lee de aqui.
    technical_specifications: PrototypeTechnicalSpecificationsIn | None = None
    materials: list[PrototypeMaterialIn] = Field(default_factory=list, max_length=100)


class PrototypeUpdateIn(BaseModel):
    """Edicion. Solo mientras la muestra no ha gastado nada."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    quantity: int | None = Field(default=None, gt=0)
    quotation_id: int | None = Field(default=None, gt=0)
    product_id: int | None = Field(default=None, gt=0)
    stock_location_id: int | None = Field(default=None, gt=0)
    target_days: int | None = Field(default=None, gt=0)
    notes: str | None = None
    #: Fase 009K.1. La ficha del taller, estructurada. `notes` vuelve a ser
    #: solo observaciones humanas: el puente al Cotizador lee de aqui.
    technical_specifications: PrototypeTechnicalSpecificationsIn | None = None


class PrototypeMaterialsIn(BaseModel):
    """La lista COMPLETA de materiales, no un parche.

    Se reemplaza entera porque lo que importa es que el conjunto sea el que
    alguien decidio: un parcheo incremental deja la puerta abierta a que quede
    un material olvidado de una version anterior.
    """

    model_config = ConfigDict(extra="forbid")

    materials: list[PrototypeMaterialIn] = Field(max_length=100)


class PrototypeDecisionIn(BaseModel):
    """Aprobar o rechazar. La nota se anade a las observaciones."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


class PrototypeSuccessorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str | None = Field(default=None, max_length=2000)


class PrototypeMaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    sort_order: int
    product_name: str
    product_internal_reference: str
    #: Lo autorizado a gastar.
    #:
    #: Se sigue llamando `quantity` en el contrato publico A PROPOSITO. El
    #: frontend que hay desplegado lee ese nombre, y sigue vivo durante todo el
    #: despliegue de esta fase: base primero, backend despues, navegador al
    #: final. Romper el nombre para mejorarlo dejaria la pantalla en blanco
    #: durante esa ventana. `quantity_planned` viaja al lado, con el mismo
    #: valor, para que el cliente nuevo pueda usar el nombre que no miente.
    quantity: Decimal
    quantity_planned: Decimal
    #: Lo que de verdad salio del almacen. Nulo mientras no se ha arrancado, y
    #: nulo para siempre en lo anterior a 0022. Es la cifra de la que se deriva
    #: el material base de la cotizacion final: lo previsto no sirve, porque no
    #: es lo que la muestra aprobada consumio.
    #:
    #: **Solo lectura para el cliente.** La escribe el arranque a partir del
    #: movimiento de inventario; aceptarla desde fuera seria dejar que alguien
    #: declarara un consumo que el almacen no respalda.
    quantity_actual: Decimal | None = None
    uom_code: str
    #: Nulo en las lineas anteriores a 0022. Sin rol no se precarga material
    #: base: se le pide a una persona que lo declare.
    material_role: PrototypeMaterialRole | None = None
    #: En que etapa del trabajo se gasta. Eje distinto del rol.
    stage: PrototypeMaterialStage | None = None


class PrototypeIssueOut(BaseModel):
    """Un bloqueo concreto, en codigo. El texto lo pone el frontend."""

    code: PrototypeReadinessCode
    product_id: int | None = None
    product_name: str | None = None
    required_quantity: str | None = None
    available_quantity: str | None = None
    uom: str | None = None


class PrototypeReadinessOut(BaseModel):
    ready: bool
    issues: list[PrototypeIssueOut]


class PrototypeSummaryOut(BaseModel):
    id: int
    code: str
    name: str
    status: PrototypeStatus
    approval: PrototypeApproval
    quotation_id: int | None
    quotation_code: str | None
    product_id: int | None
    stock_location_id: int | None
    quantity: int
    target_days: int | None
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    decided_at: datetime | None
    supersedes_prototype_id: int | None
    material_count: int


class PrototypeOriginQuotationOut(BaseModel):
    """Una cotizacion nacida de esta muestra."""

    id: int
    code: str
    status: str


class PrototypeOut(PrototypeSummaryOut):
    notes: str | None
    #: Fase 009K.1. Nula en las muestras anteriores a 0022, y esa nulidad es la
    #: que impide precargar sus medidas: no se parsea `notes` para fingirla.
    technical_specifications: dict[str, Any] | None = None
    #: Fase 009K.1.1. La cotizacion de prototipo que autorizo esta muestra.
    #: Nula en las anteriores, que colgaban de una cotizacion de producto.
    prototype_quotation_id: int | None = None
    #: De que cotizaciones fue origen esta muestra, si de alguna. La activa
    #: —el borrador— es como mucho una; las demas son historia.
    origin_quotation_ids: list[int] = Field(default_factory=list)
    #: Las mismas, con lo que hace falta para pintarlas: una pantalla que solo
    #: recibe ids no puede escribir «CTZ-2026-000123» sin ir a buscarlas una
    #: por una, y el estado es lo que distingue el borrador vivo del historial.
    origin_quotations: list[PrototypeOriginQuotationOut] = Field(default_factory=list)
    #: Si la cotizacion de origen consta cobrada. Viaja para que la pantalla
    #: pueda decir POR QUE no se puede arrancar, no para decidirlo: la
    #: autoridad es el backend.
    quotation_payment_status: str | None
    materials: list[PrototypeMaterialOut]
    readiness: PrototypeReadinessOut


class PrototypePage(BaseModel):
    items: list[PrototypeSummaryOut]
    total: int
    limit: int
    offset: int
