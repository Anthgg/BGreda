"""Contrato publico del Cotizador V2.

Schemas **propios**. No se reexporta ni un campo de `app.schemas.quotations` ni
de `app.schemas.quotation_builder`, y no por gusto de duplicar: el contrato de
Legacy arrastra decenas de campos de costeo cuyo significado depende del motor
viejo. Compartirlo obligaria a que cada cambio de V2 pasara por la pantalla de
Legacy y al reves.

En 010A el contrato es deliberadamente corto: identidad, estado y las dos
decisiones de cabecera. Todo lo economico —materiales, quema, mano de obra,
factor comercial, IGV, moneda— entra con su fase y su snapshot.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.pricing_engine import PricingEngineVersion
from app.core.quoter_v2_lifecycle import V2EffectiveStatus
from app.models.quoter_v2 import (
    V2CustomerKind,
    V2ProductionHandoffStatus,
    V2ProductionType,
    V2QuotationStatus,
)

#: Las mismas cotas que la configuracion. Sin tope, un numero disparatado no da
#: un error claro: desborda la columna NUMERIC y el fallo llega desde la base,
#: sin decir que campo lo causo.
MAX_MONEY = Decimal("1000000")
MAX_FACTOR = Decimal("100")


def _blank_to_none(value: str | None) -> str | None:
    """Un campo vacio es ausencia, no una cadena vacia guardada para siempre."""
    if value is None:
        return None
    limpio = value.strip()
    return limpio or None


class V2QuotationCreateIn(BaseModel):
    """Alta de un borrador V2."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    customer_id: int | None = Field(default=None, ge=1)
    #: Por menor / por mayor. Lo decide la persona; el sistema jamas lo cambia,
    #: ni siquiera si la cantidad parece de por mayor. Ausente = lo que diga la
    #: configuracion.
    production_type: V2ProductionType | None = None
    #: A quien se cotiza, a efectos de tarifa de horno. Explicito: no se deduce
    #: del nombre del cliente.
    customer_kind: V2CustomerKind | None = None
    #: Fase 010B. Lo que el alta no diga, lo pone la configuracion. Lo que diga,
    #: manda y queda congelado en la cotizacion.
    currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    #: Solo tiene sentido en moneda extranjera. MANUAL, como en Legacy.
    exchange_rate: Decimal | None = Field(default=None, gt=0, le=MAX_MONEY)
    #: Nunca por debajo del minimo vigente; el servicio lo comprueba contra la
    #: configuracion y la base lo vuelve a exigir.
    commercial_factor: Decimal | None = Field(default=None, ge=2, le=MAX_FACTOR)
    #: Acotado aunque la columna sea TEXT: un campo libre sin tope es un campo
    #: por el que cabe cualquier cosa, y 4000 caracteres son mas que de sobra
    #: para una nota interna.
    notes: str | None = Field(default=None, max_length=4000)
    #: Fase 010H. Observaciones que SI salen en el PDF del cliente.
    client_notes: str | None = Field(default=None, max_length=2000)

    @field_validator("name", "notes", "client_notes")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class V2QuotationUpdateIn(BaseModel):
    """Cambio de la CABECERA de un borrador. Fase 010G.

    Existe porque el flujo de siete pasos deja volver atras: quien esta en el
    paso de la quema puede darse cuenta de que el cliente esta mal y regresar
    al primero. Sin esta ruta, ese cambio no tenia donde guardarse y la unica
    salida era empezar una cotizacion nueva.

    Semantica parcial, la misma de toda la familia: lo ausente se conserva y la
    presencia de una clave no es un cambio. NAVEGAR no es editar.

    Solo sobre borradores. Una cotizacion emitida ya comprometio un precio con
    un cliente concreto y en una moneda concreta.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    #: En nulo RETIRA el cliente: un borrador puede empezar sin el.
    customer_id: int | None = Field(default=None, ge=1)
    production_type: V2ProductionType | None = None
    customer_kind: V2CustomerKind | None = None
    currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    exchange_rate: Decimal | None = Field(default=None, gt=0, le=MAX_MONEY)
    notes: str | None = Field(default=None, max_length=4000)
    #: Fase 010H. Observaciones que SI salen en el PDF del cliente. `notes`
    #: sigue siendo interno.
    client_notes: str | None = Field(default=None, max_length=2000)

    @field_validator("name", "notes", "client_notes")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class V2ProductionHandoffOut(BaseModel):
    """Fase 010H. El puente de una cotizacion hacia produccion."""

    id: int
    v2_quotation_id: int
    status: V2ProductionHandoffStatus
    created_at: datetime
    created_by_name: str | None


class V2QuotationOut(BaseModel):
    """Una cotizacion V2 tal como la ve el frontend."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    #: Siempre `V2`. Viaja en la respuesta a proposito: el cliente no tiene que
    #: adivinar el motor mirando el prefijo del codigo.
    pricing_engine_version: PricingEngineVersion
    status: V2QuotationStatus
    production_type: V2ProductionType
    customer_id: int | None
    customer_name: str | None
    name: str | None
    notes: str | None

    #: Fase 010B. La copia congelada de la configuracion. Viaja en la respuesta
    #: para que la pantalla muestre con que numeros se armo ESTA cotizacion, y
    #: no los que la configuracion tenga hoy.
    customer_kind: V2CustomerKind | None
    tax_percent: Decimal | None
    currency_code: str | None
    currency_symbol: str | None
    exchange_rate: Decimal | None
    validity_days: int | None
    workday_hours: Decimal | None
    space_service_cost_per_day: Decimal | None
    administrative_cost: Decimal | None
    commercial_factor: Decimal | None
    commercial_factor_min: Decimal | None
    commercial_factor_max: Decimal | None
    low_fire_enabled: bool | None
    high_fire_enabled: bool | None
    settings_version: int | None

    created_at: datetime
    updated_at: datetime

    # ---- Fase 010H: ciclo de vida -------------------------------------
    client_notes: str | None = None
    #: Lo que la cotizacion ES hoy: `EXPIRED` y `READY_FOR_PRODUCTION` los
    #: calcula el backend con su reloj. La pantalla no decide si vencio.
    effective_status: V2EffectiveStatus
    issued_at: datetime | None = None
    #: Ultimo dia (calendario de Lima) en que la oferta vale.
    valid_until: date | None = None
    expires_at: datetime | None = None
    issued_by_name: str | None = None
    cancelled_at: datetime | None = None
    cancelled_by_name: str | None = None
    cancel_reason: str | None = None
    duplicated_from_id: int | None = None
    #: El borrador ya abierto a partir de esta, si lo hay: la pantalla ofrece
    #: ir a el en vez de invitar a duplicar otra vez.
    open_duplicate_id: int | None = None
    production_handoff: V2ProductionHandoffOut | None = None


class V2QuotationListItemOut(BaseModel):
    """Fila del listado. Sin desglose: nadie necesita el detalle para elegir."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    pricing_engine_version: PricingEngineVersion
    status: V2QuotationStatus
    production_type: V2ProductionType
    customer_name: str | None
    name: str | None
    created_at: datetime
    effective_status: V2EffectiveStatus
    valid_until: date | None = None


class V2QuotationPage(BaseModel):
    items: list[V2QuotationListItemOut]
    total: int


# ---------------------------------------------------------------------------
# Fase 010H: emision, cancelacion, duplicacion y puente a produccion
# ---------------------------------------------------------------------------
class V2BlockerOut(BaseModel):
    code: str
    line_id: int | None = None


class V2PreviewLineOut(BaseModel):
    """Una linea tal como la vera el cliente. Sin un solo costo interno."""

    id: int
    product_name: str | None
    quantity: int
    length_cm: Decimal | None
    width_cm: Decimal | None
    height_cm: Decimal | None
    client_observation: str | None
    unit_price: Decimal
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal


class V2ConfirmationPreviewOut(BaseModel):
    """El resumen que se revisa antes de emitir, con la huella que lo identifica."""

    quotation_id: int
    code: str
    status: V2QuotationStatus
    effective_status: V2EffectiveStatus
    can_confirm: bool
    blockers: list[V2BlockerOut]
    warnings: list[str]
    #: Hay que devolverla al confirmar. Si el documento cambio entre medias, la
    #: emision se rechaza con 409 en vez de congelar lo que nadie reviso.
    fingerprint: str
    customer_name: str | None
    name: str | None
    client_notes: str | None
    currency_code: str | None
    currency_symbol: str | None
    exchange_rate: Decimal | None
    tax_percent: Decimal | None
    commercial_factor: Decimal | None
    validity_days: int | None
    #: Si se emitiera ahora mismo. En una emitida, la que se congelo.
    valid_until: date | None
    subtotal_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    lines: list[V2PreviewLineOut]


class V2ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class V2CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class V2DuplicateWarningOut(BaseModel):
    code: str
    name: str | None = None


class V2DuplicateOut(BaseModel):
    quotation: V2QuotationOut
    #: Falso cuando ya habia un borrador abierto nacido de la misma: el doble
    #: clic no crea otro.
    created: bool
    warnings: list[V2DuplicateWarningOut]


class V2SendToProductionOut(BaseModel):
    handoff: V2ProductionHandoffOut
    created: bool


class V2HistoryEventOut(BaseModel):
    """Un hecho del ciclo de vida: quien, cuando y que."""

    event: str
    at: datetime
    user_name: str | None
    details: dict[str, str | None]
