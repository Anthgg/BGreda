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

from app.models.production import ProductionOrderStatus, ProductionReadinessCode
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
    #: Solo para reintentos de red. La unicidad de verdad la impone el UNIQUE
    #: de `quotation_id` en la base.
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)

    @model_validator(mode="after")
    def _exactamente_un_origen(self) -> ProductionOrderCreateIn:
        """Uno de los dos, nunca los dos ni ninguno.

        Es la misma regla que el CHECK `exactly_one_origin` de la tabla, dicha
        aqui para que quien se equivoque reciba un 422 que explica el error y
        no un 500 con un mensaje de PostgreSQL.
        """
        if (self.quotation_id is None) == (self.prototype_id is None):
            raise ValueError("Indica una cotización o una muestra, y solo una de las dos.")
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


class ProductionOrderPage(BaseModel):
    items: list[ProductionOrderSummaryOut]
    total: int
    limit: int
    offset: int
