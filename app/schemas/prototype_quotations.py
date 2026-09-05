"""Contrato HTTP del Cotizador de Prototipos.

El navegador manda INTENCION —que pieza, cuantas, cuantos dias, que material— y
recibe IMPORTES. Nunca al reves: no hay ningun campo de entrada que acepte un
subtotal, un IGV, un total, un costo unitario ni un plazo, porque aceptarlo
seria dejar que la pantalla fije el precio.

Por el mismo motivo la unidad de medida solo viaja de salida. La manda el
catalogo del material, y admitirla como entrada permitiria cotizar kilos de
algo que se lleva en gramos.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.firings import FiringType
from app.models.prototype_quotations import (
    PrototypeQuotationPaymentStatus,
    PrototypeQuotationStatus,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrototypeQuotationMaterialIn(_Strict):
    """Un material previsto. Sin unidad y sin costo: los dos los pone el backend."""

    product_id: int = Field(gt=0)
    #: Por UNA muestra. Es lo que la persona teclea; el total lo multiplica el
    #: backend por la cantidad de muestras.
    quantity_per_prototype: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    #: El barro de la pieza, frente a un esmalte o un consumible cualquiera.
    is_body_material: bool = False


class PrototypeQuotationDraftIn(_Strict):
    """Lo que se puede editar mientras la cotizacion es un borrador."""

    customer_id: int | None = Field(default=None, gt=0)
    product_id: int | None = Field(default=None, gt=0)
    description: str = Field(min_length=1, max_length=200)
    quantity: int = Field(default=1, gt=0)

    width_cm: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    length_cm: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    height_cm: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    depth_cm: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)

    technical_specifications: dict[str, Any] | None = None
    notes: str | None = None

    design_days: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=6)
    #: Nulo NO es cero: significa «cobra lo que cobre la casa». Mandar aqui el
    #: valor por defecto convertiria una herencia en un precio pactado.
    design_rate_override: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=6
    )
    artist_days: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=6)
    artist_rate_override: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=6
    )

    mold_maker_partner_id: int | None = Field(default=None, gt=0)
    #: Precio FIJO del matricero. Sus dias alargan el plazo y no lo multiplican.
    mold_maker_price_override: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=6
    )
    mold_maker_days: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=6)

    kiln_id: int | None = Field(default=None, gt=0)
    #: Sin valor por defecto a proposito: elegir BAJA en silencio cotizaria a
    #: una tarifa que nadie escogio.
    firing_type: FiringType | None = None
    firing_batches: int = Field(default=0, ge=0)

    drying_days: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=6)
    adjustment_days: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=6)
    fixed_cost_override: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=6)

    materials: list[PrototypeQuotationMaterialIn] = Field(default_factory=list, max_length=50)


class PrototypeQuotationUpdateIn(PrototypeQuotationDraftIn):
    """Edicion con bloqueo optimista.

    Sin `expected_updated_at` dos personas editando a la vez se pisarian y la
    segunda no se enteraria de haber borrado el trabajo de la primera.
    """

    expected_updated_at: datetime | None = None


class PrototypeQuotationMaterialOut(BaseModel):
    id: int | None = None
    product_id: int
    product_name: str
    quantity_per_prototype: Decimal
    total_quantity: Decimal
    #: Del catalogo. Viaja de SALIDA porque la pantalla la muestra, no la elige.
    uom_code: str
    unit_cost: Decimal
    cost: Decimal
    is_body_material: bool = False


class PrototypeCostBreakdownOut(BaseModel):
    """El desglose INTERNO. Lo ve quien cotiza; no sale en el PDF del cliente."""

    design_cost: Decimal
    artist_cost: Decimal
    mold_maker_cost: Decimal
    materials_cost: Decimal
    firing_cost: Decimal
    fixed_cost: Decimal
    base_cost: Decimal

    #: Antes del escalon comercial. Explica de donde viene el ajuste.
    raw_tax: Decimal
    raw_gross_total: Decimal

    commercial_net_total: Decimal
    tax_percent: Decimal
    commercial_tax_total: Decimal
    commercial_gross_total: Decimal
    total_per_prototype: Decimal

    #: Con que politica se cerro el numero. Sin el origen, un «0.50» no explica
    #: nada dentro de dos anos.
    rounding_step: Decimal
    rounding_source: str | None = None

    #: Las tarifas realmente usadas: la de la casa o la pactada.
    design_rate: Decimal
    artist_rate: Decimal
    mold_maker_price: Decimal
    firing_rate: Decimal
    firing_days_per_batch: int

    design_days: Decimal
    artist_days: Decimal
    mold_maker_days: Decimal
    drying_days: Decimal
    firing_days: int
    adjustment_days: Decimal
    estimated_days: Decimal
    target_date: date | None = None

    materials: list[PrototypeQuotationMaterialOut] = Field(default_factory=list)


class PrototypeQuotationOut(BaseModel):
    """La cotizacion completa, para la pantalla de quien cotiza."""

    id: int
    code: str | None
    status: PrototypeQuotationStatus
    payment_status: PrototypeQuotationPaymentStatus
    paid_at: datetime | None = None
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None

    customer_id: int | None = None
    customer_name: str | None = None
    product_id: int | None = None
    description: str
    quantity: int

    width_cm: Decimal | None = None
    length_cm: Decimal | None = None
    height_cm: Decimal | None = None
    depth_cm: Decimal | None = None
    technical_specifications: dict[str, Any] | None = None
    notes: str | None = None

    design_days: Decimal
    design_rate_override: Decimal | None = None
    artist_days: Decimal
    artist_rate_override: Decimal | None = None
    mold_maker_partner_id: int | None = None
    mold_maker_price_override: Decimal | None = None
    mold_maker_days: Decimal
    kiln_id: int | None = None
    firing_type: FiringType | None = None
    firing_batches: int
    drying_days: Decimal
    adjustment_days: Decimal
    fixed_cost_override: Decimal | None = None

    currency_code: str | None = None
    currency_symbol: str | None = None
    exchange_rate: Decimal | None = None

    costing: PrototypeCostBreakdownOut | None = None

    #: La muestra fisica que nacio al cobrar, si ya se cobro.
    prototype_id: int | None = None
    prototype_code: str | None = None

    updated_at: datetime | None = None


class PrototypeQuotationListItemOut(BaseModel):
    """Fila del listado. Sin desglose: nadie necesita el costeo entero para elegir."""

    id: int
    code: str | None
    status: PrototypeQuotationStatus
    payment_status: PrototypeQuotationPaymentStatus
    customer_name: str | None = None
    description: str
    quantity: int
    commercial_gross_total: Decimal | None = None
    estimated_days: Decimal | None = None
    confirmed_at: datetime | None = None


class PrototypeQuotationPage(BaseModel):
    items: list[PrototypeQuotationListItemOut]
    total: int
