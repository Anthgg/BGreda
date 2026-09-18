"""Contratos de la API de ordenes de produccion.

Los importes no aparecen por ninguna parte. Una orden de produccion es un papel
de taller: dice que fabricar, cuanto y con que material. El precio de venta, el
margen y el IGV son del documento comercial y no ayudan a nadie a esmaltar una
pieza; sacarlos al taller solo amplia quien puede ver el margen del cliente.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.firings import FiringType
from app.models.production import (
    ProductionConsumptionKind,
    ProductionNoteKind,
    ProductionOrderStatus,
    ProductionReadinessCode,
)
from app.models.quotations import QuotationPaymentStatus


class ProductionOrderCreateIn(BaseModel):
    """Alta de una orden. De una cotizacion confirmada, o de una muestra."""

    model_config = ConfigDict(extra="forbid")

    quotation_id: int | None = Field(default=None, gt=0)
    #: Fase 009K.4. El otro origen posible. Se usa para las ITERACIONES: una
    #: muestra sucesora no tiene cotizacion de prototipo propia que cobrar, y
    #: sin esta puerta no podria fabricarse por el camino unico.
    prototype_id: int | None = Field(default=None, gt=0)
    #: Obligatoria y explicita. No se resuelve por defecto ni cuando solo hay
    #: una ubicacion: el dia que haya dos, el default silencioso descontaria
    #: del almacen equivocado sin que nadie lo notara.
    stock_location_id: int = Field(gt=0)
    #: Fase 010I. El tercer origen: una cotizacion V2 ya enviada a produccion.
    #: Se pide por la cotizacion porque es lo que el taller conoce; el servicio
    #: resuelve su puente y cuelga la orden de el.
    v2_quotation_id: int | None = Field(default=None, gt=0)
    #: Solo para reintentos de red. La unicidad de verdad la impone el UNIQUE
    #: del origen en la base (`quotation_id`, `prototype_id` o `v2_handoff_id`).
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)

    @model_validator(mode="after")
    def _exactamente_un_origen(self) -> ProductionOrderCreateIn:
        """Uno de los tres, nunca dos ni ninguno.

        Es la misma regla que el CHECK `exactly_one_origin` de la tabla, dicha
        aqui para que quien se equivoque reciba un 422 que explica el error y
        no un 500 con un mensaje de PostgreSQL.
        """
        origenes = (self.quotation_id, self.prototype_id, self.v2_quotation_id)
        if sum(origen is not None for origen in origenes) != 1:
            raise ValueError(
                "Indica una cotización, una cotización V2 o una muestra, y solo una de ellas."
            )
        return self


class ReadinessIssueOut(BaseModel):
    """Un bloqueo concreto, en codigo. El texto lo pone el frontend."""

    code: ProductionReadinessCode
    production_order_line_id: int | None = None
    quotation_item_id: int | None = None
    prepared_product_id: int | None = None
    prepared_product_name: str | None = None
    #: Decimales como texto, como en todo el proyecto.
    required_quantity: str | None = None
    available_quantity: str | None = None
    uom: str | None = None


class ProductionReadinessOut(BaseModel):
    ready: bool
    issues: list[ReadinessIssueOut]


class ProductionOrderOrigin(StrEnum):
    """De donde nace una orden de produccion. Fase 009K.4.

    Lo dice el BACKEND y viaja explicito. La alternativa —que el navegador lo
    deduzca de que campo venga relleno— convierte una regla del dominio en una
    heuristica de pantalla, y el dia que se anada un tercer origen habra dos
    sitios donde arreglarlo.
    """

    QUOTATION = "QUOTATION"
    PROTOTYPE = "PROTOTYPE"
    #: Fase 010I. Ese tercer origen: una cotizacion del Cotizador V2, que entra
    #: por su puente de 010H.
    V2_QUOTATION = "V2_QUOTATION"


class ProductionOrderLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    #: Nulo en la linea de una orden de MUESTRA (Fase 009K.4): no copia ningun
    #: item de cotizacion porque no hay cotizacion de la que copiar.
    quotation_item_id: int | None
    sort_order: int
    product_id: int
    product_name: str
    product_internal_reference: str
    quantity: int | None
    width: Decimal | None
    height: Decimal | None
    length: Decimal | None
    depth: Decimal | None
    recipe_id: int | None
    recipe_version_id: int | None
    material_grams_per_piece: Decimal | None
    prepared_product_id: int | None
    prepared_product_name: str | None
    prepared_product_internal_reference: str | None
    #: Lo que pide la receta, en gramos. La conversion a la unidad del saldo la
    #: hace el motor de disponibilidad, porque depende del maestro de unidades.
    required_material_quantity: Decimal | None
    required_material_uom: str | None


class ProductionOrderSummaryOut(BaseModel):
    id: int
    code: str
    status: ProductionOrderStatus
    #: Fase 009K.4. El origen, dicho por el backend.
    origin_type: ProductionOrderOrigin = ProductionOrderOrigin.QUOTATION
    #: Nulos cuando la orden nace de una muestra.
    quotation_id: int | None = None
    quotation_code: str | None = None
    #: Nulos cuando la orden nace de una cotizacion.
    prototype_id: int | None = None
    prototype_code: str | None = None
    #: La cotizacion de prototipo de la que salio la muestra, si la hubo. Es
    #: el documento que el taller reconoce, y por eso viaja al lado del PRT.
    prototype_quotation_id: int | None = None
    prototype_quotation_code: str | None = None
    #: Fase 010I. Nulos salvo cuando la orden nace de una cotizacion V2. Son
    #: campos PROPIOS y no `quotation_id` reutilizado: el id de una V2 y el de
    #: una Legacy son espacios distintos, y compartir el campo haria que un
    #: enlace llevara a la cotizacion equivocada.
    v2_quotation_id: int | None = None
    v2_quotation_code: str | None = None
    stock_location_id: int
    stock_location_name: str
    line_count: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None


class ProductionOrderOut(ProductionOrderSummaryOut):
    #: Identificador opaco del QR. Ni el id ni el codigo: un token secuencial
    #: dejaria recorrer ordenes ajenas cambiando un digito.
    qr_token: str
    quotation_customer_name: str | None
    #: Fase 009H.1. Si la cotizacion de origen consta cobrada. Viaja para que
    #: la pantalla pueda decir POR QUE no se puede arrancar, no para que lo
    #: decida: la autoridad es el guardia del backend, que rechaza el arranque
    #: aunque el boton llegara a aparecer.
    #:
    #: Nulo significa «no consta», que es un tercer caso y no «impagada». Para
    #: arrancar hace falta PAID; el nulo tambien bloquea.
    quotation_payment_status: QuotationPaymentStatus | None
    lines: list[ProductionOrderLineOut]
    readiness: ProductionReadinessOut
    #: Fase 010I, decision D3. Las clases de material que la cotizacion V2 de
    #: esta orden planifico como inventariables y que aun no tienen ningun
    #: consumo real. Mientras no este vacia, la orden no puede FINALIZAR. Viaja
    #: para que la pantalla diga que falta; quien decide es `complete`.
    #: Siempre vacia fuera de las ordenes V2.
    pending_consumption_kinds: list[ProductionConsumptionKind] = Field(default_factory=list)


class ProductionOrderPage(BaseModel):
    items: list[ProductionOrderSummaryOut]
    total: int
    limit: int
    offset: int


class ProductionConsumptionCreateIn(BaseModel):
    """Registrar material REAL gastado en una orden V2. Fase 010I.

    La cantidad va en la unidad base del material —la del saldo— y SIEMPRE en
    positivo: registrar un consumo es descontar, y el signo lo pone el
    movimiento. Un cero no es un consumo.
    """

    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0)
    #: Opcional: por defecto el almacen de la orden. Cada consumo dice el suyo
    #: porque no se asume un almacen unico.
    stock_location_id: int | None = Field(default=None, gt=0)
    quantity: Decimal = Field(gt=0)
    kind: ProductionConsumptionKind
    #: La pieza a la que se imputa. Nulo = consumo de la orden entera.
    v2_quotation_product_id: int | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=500)
    #: OBLIGATORIA. La genera el cliente al abrir el formulario de consumo y la
    #: reutiliza en cada reintento de ESE consumo: es lo que impide que un doble
    #: clic o un corte de red descuenten dos veces.
    idempotency_key: str = Field(min_length=8, max_length=64)


class ProductionConsumptionOut(BaseModel):
    """Un consumo real, tal como lo ve el taller.

    Sin el costo: igual que el resto de esta API, una orden de produccion es un
    papel de taller y no ensena importes. El costo por unidad se guarda solo
    para comparar despues lo real con lo cotizado.
    """

    id: int
    production_order_id: int
    v2_quotation_product_id: int | None
    product_id: int
    product_name: str
    product_internal_reference: str
    stock_location_id: int
    stock_location_name: str
    kind: ProductionConsumptionKind
    quantity: Decimal
    uom_code: str
    #: Saldo del material en ese almacen justo despues de este consumo.
    balance_after: Decimal
    stock_movement_id: int
    note: str | None
    created_by_name: str | None
    created_at: datetime


class ProductionConsumptionPage(BaseModel):
    items: list[ProductionConsumptionOut]
    total: int


class ProductionNoteCreateIn(BaseModel):
    """Anadir una nota o una quema al seguimiento de la orden. Fase 010I, D4.

    NOTE: solo texto. FIRING_NOTE: horno y tipo de quema obligatorios, texto
    opcional. `occurred_at` es cuando paso, y es OBLIGATORIA: la pantalla la
    propone en «ahora» y la manda siempre. Si fuera opcional, un reintento sin
    fecha no podria distinguirse de una nota fechada en otro momento
    (hallazgo de Copilot en el bloque C).
    """

    model_config = ConfigDict(extra="forbid")

    kind: ProductionNoteKind
    body: str | None = Field(default=None, max_length=2000)
    kiln_id: int | None = Field(default=None, gt=0)
    firing_type: FiringType | None = None
    occurred_at: datetime
    #: OBLIGATORIA, como en el consumo: un doble clic no debe dejar dos notas.
    idempotency_key: str = Field(min_length=8, max_length=64)

    @model_validator(mode="after")
    def _campos_de_su_clase(self) -> ProductionNoteCreateIn:
        texto = (self.body or "").strip()
        self.body = texto or None
        if self.kind is ProductionNoteKind.NOTE:
            if self.body is None:
                raise ValueError("Una nota necesita texto")
            if self.kiln_id is not None or self.firing_type is not None:
                raise ValueError("Una nota no lleva horno ni tipo de quema")
        elif self.kiln_id is None or self.firing_type is None:
            raise ValueError("Una quema necesita horno y tipo de quema")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at debe llevar zona horaria")
        return self


class ProductionNoteOut(BaseModel):
    id: int
    production_order_id: int
    kind: ProductionNoteKind
    body: str | None
    kiln_id: int | None
    kiln_name: str | None
    firing_type: FiringType | None
    occurred_at: datetime
    created_by_name: str | None
    created_at: datetime


class ProductionTimelineEventType(StrEnum):
    #: Un cambio de estado: INICIO, EN PROCESO, FINALIZADO o Anulada.
    STATUS = "STATUS"
    CONSUMPTION = "CONSUMPTION"
    NOTE = "NOTE"
    FIRING_NOTE = "FIRING_NOTE"


class ProductionTimelineEventOut(BaseModel):
    """Un hecho del seguimiento. Solo viaja el detalle de su tipo."""

    type: ProductionTimelineEventType
    occurred_at: datetime
    actor_name: str | None
    #: Solo en STATUS: el estado al que se llego.
    status: ProductionOrderStatus | None = None
    consumption: ProductionConsumptionOut | None = None
    note: ProductionNoteOut | None = None


class ProductionTimelineOut(BaseModel):
    """El seguimiento de la orden, del hecho mas antiguo al mas reciente."""

    items: list[ProductionTimelineEventOut]
