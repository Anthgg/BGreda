"""Contratos de la API de ordenes de produccion.

Los importes no aparecen por ninguna parte. Una orden de produccion es un papel
de taller: dice que fabricar, cuanto y con que material. El precio de venta, el
margen y el IGV son del documento comercial y no ayudan a nadie a esmaltar una
pieza; sacarlos al taller solo amplia quien puede ver el margen del cliente.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.production import ProductionOrderStatus, ProductionReadinessCode
from app.models.quotations import QuotationPaymentStatus


class ProductionOrderCreateIn(BaseModel):
    """Alta de la orden de una cotizacion confirmada."""

    model_config = ConfigDict(extra="forbid")

    quotation_id: int = Field(gt=0)
    #: Obligatoria y explicita. No se resuelve por defecto ni cuando solo hay
    #: una ubicacion: el dia que haya dos, el default silencioso descontaria
    #: del almacen equivocado sin que nadie lo notara.
    stock_location_id: int = Field(gt=0)
    #: Solo para reintentos de red. La unicidad de verdad la impone el UNIQUE
    #: de `quotation_id` en la base.
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)


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


class ProductionOrderLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    quotation_item_id: int
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
    quotation_id: int
    quotation_code: str
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
