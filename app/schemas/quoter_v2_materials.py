"""Contrato publico de los materiales del Cotizador V2.

Dos superficies distintas y conviene no mezclarlas:

- la **valorizacion** de un material del maestro, que es politica de la casa y
  vale para todas las cotizaciones futuras;
- la **linea** de una cotizacion, que congela lo que uso ESA cotizacion.

Lo que el navegador manda son intenciones —que material, cuanto pesa una pieza,
si lleva esmalte— y lo que recibe son importes ya calculados. Ningun costo
viaja de ida salvo el override explicito, que es una decision, no un resultado.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.quoter_v2_materials import V2MaterialKind, V2MaterialOrigin

#: Tope holgado. Frena un cero de mas al teclear, no opina sobre el negocio.
MAX_MONEY = Decimal("10000000")
#: Un material se compra en gramos: mil toneladas son un error de captura.
MAX_QUANTITY = Decimal("1000000000")
#: Un costo por unidad se expresa con mucha escala —0,0013 el gramo— pero no
#: puede ser arbitrariamente grande.
MAX_UNIT_COST = Decimal("1000000")


class V2MaterialUpsertIn(BaseModel):
    """Los hechos de la adquisicion. El costo por unidad NO se manda: se deriva."""

    model_config = ConfigDict(extra="forbid")

    material_kind: V2MaterialKind
    origin: V2MaterialOrigin
    #: En la unidad base del producto, que sale del maestro. No se pide aqui
    #: para que un material que se lleva en gramos no acabe valorizado en kilos.
    purchase_quantity: Decimal = Field(gt=0, le=MAX_QUANTITY)
    purchase_cost: Decimal = Field(ge=0, le=MAX_MONEY)
    #: Parte del costo del material: sin transporte el material no esta aqui.
    transport_cost: Decimal = Field(default=Decimal(0), ge=0, le=MAX_MONEY)
    #: Valor de costeo cuando no coincide con lo pagado, tipicamente en
    #: material donado. Ausente = usar el derivado; cero = decision de no
    #: valorizarlo, que es distinto de no haber decidido.
    costing_override_per_unit: Decimal | None = Field(default=None, ge=0, le=MAX_UNIT_COST)
    #: Mililitros por gramo de este material. Ausente no significa 1: significa
    #: que no hay dato y habra que decir que se uso la conversion de reserva.
    ml_per_gram: Decimal | None = Field(default=None, gt=0, le=MAX_UNIT_COST)
    notes: str | None = Field(default=None, max_length=2000)


class V2MaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int
    product_name: str
    product_type: str
    uom_code: str | None
    active: bool

    material_kind: V2MaterialKind
    origin: V2MaterialOrigin
    purchase_quantity: Decimal
    purchase_cost: Decimal
    transport_cost: Decimal
    #: Compra mas transporte. Se devuelve calculado para que la pantalla no
    #: tenga que repetir la suma y pueda equivocarse.
    acquisition_total_cost: Decimal
    costing_override_per_unit: Decimal | None
    #: El costo con el que se cotiza. Lo deriva la base de datos.
    effective_cost_per_unit: Decimal
    ml_per_gram: Decimal | None
    notes: str | None

    #: Existencia total. Se muestra para avisar, NO para bloquear: un material
    #: sin stock se puede cotizar igual.
    stock: Decimal


class V2MaterialPage(BaseModel):
    items: list[V2MaterialOut]


class V2QuotationProductIn(BaseModel):
    """Una linea: que pieza, cuantas y de que esta hecha."""

    model_config = ConfigDict(extra="forbid")

    product_id: int | None = Field(default=None, ge=1)
    quantity: int = Field(default=0, ge=0, le=1_000_000)

    body_material_id: int | None = Field(default=None, ge=1)
    #: Lo que lleva UNA pieza, en la unidad base del material.
    body_unit_weight: Decimal | None = Field(default=None, ge=0, le=MAX_QUANTITY)
    #: Costear esta cotizacion con otro valor sin tocar el maestro. Es una
    #: decision de esta cotizacion y solo de esta.
    body_cost_per_unit_override: Decimal | None = Field(default=None, ge=0, le=MAX_UNIT_COST)

    #: Apagado por defecto. Encenderlo es una decision explicita.
    requires_glaze: bool = False
    #: Si se omite y el esmalte esta encendido, lo elige el sistema: el activo
    #: mas caro por gramo, como referencia de costeo.
    glaze_material_id: int | None = Field(default=None, ge=1)
    glaze_cost_per_unit_override: Decimal | None = Field(default=None, ge=0, le=MAX_UNIT_COST)


class V2QuotationProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sort_order: int
    product_id: int | None
    product_name: str | None
    quantity: int

    body_material_id: int | None
    body_material_name: str | None
    body_unit_weight: Decimal | None
    body_uom: str | None
    body_cost_per_unit: Decimal | None
    body_cost_is_override: bool
    body_total_weight: Decimal
    body_cost: Decimal

    requires_glaze: bool
    glaze_material_id: int | None
    glaze_material_name: str | None
    #: Si lo eligio el sistema. La pantalla lo usa para decir que es una
    #: referencia de costeo y no el esmalte final de produccion.
    glaze_is_reference: bool
    glaze_cost_per_unit: Decimal | None
    glaze_cost_is_override: bool
    glaze_percent: Decimal | None
    glaze_ml_per_gram: Decimal | None
    #: Si se uso la conversion de reserva 1 g = 1 ml.
    glaze_conversion_is_fallback: bool
    glaze_total_weight: Decimal
    glaze_volume_ml: Decimal
    glaze_cost: Decimal

    #: Lo que falta o conviene mirar. Avisos, no errores: un borrador a medias
    #: tiene que poder guardarse.
    warnings: list[str] = []


class V2QuotationProductsPage(BaseModel):
    items: list[V2QuotationProductOut]
    #: Suma de los materiales de todas las lineas. Sin redondear y sin factor:
    #: el precio lo construye 010F.
    materials_cost: Decimal
