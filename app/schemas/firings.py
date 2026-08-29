"""Contratos de hornos, tarifas y hojas de quema.

Los importes, volumenes y dimensiones son ``Decimal``: Pydantic los serializa
como texto en JSON, de modo que el navegador nunca ve un ``float`` de un dato
de negocio.

Una linea no referencia sesiones por identificador —al crear la hoja todavia no
existen— sino por el **horno** que hace su quema baja y el que hace su quema
alta. Es tambien como lo escribe el documento funcional («T° baja elegida»,
«T° alta elegida»), y no es ambiguo porque una hoja tiene como maximo una sesion
por combinacion de horno y temperatura.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.firings import ALLOWED_BRACKETS
from app.models.firings import FiringStatus, FiringType

_TAG_PATTERN = re.compile(r"<\s*/?\s*[a-zA-Z]")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _plain_text(value: str) -> str:
    """Rechaza HTML y caracteres de control."""
    if _TAG_PATTERN.search(value):
        raise ValueError("El texto no admite etiquetas HTML")
    if _CONTROL_CHARS.search(value):
        raise ValueError("El texto no admite caracteres de control")
    return value


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


#: Dimensiones en centimetros. El limite superior evita que un error de tecleo
#: genere un volumen que desborde la escala de la columna.
Dimension = Annotated[Decimal, Field(gt=Decimal(0), le=Decimal(100_000))]
Money = Annotated[Decimal, Field(ge=Decimal(0))]


# ---------------------------------------------------------------------------
# Tarifas
# ---------------------------------------------------------------------------
class KilnRateIn(_In):
    firing_type: FiringType
    rate: Money
    #: Desde cuando rige. Por omision, hoy.
    valid_from: date | None = None


class KilnRateOut(_Out):
    id: int
    kiln_id: int
    firing_type: FiringType
    rate: Decimal
    valid_from: date
    valid_to: date | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Factores de ocupacion
# ---------------------------------------------------------------------------
class KilnOccupancyFactorIn(_In):
    min_percentage: Annotated[int, Field(ge=1, le=100)]
    max_percentage: Annotated[int, Field(ge=1, le=100)]
    factor: Annotated[Decimal, Field(gt=Decimal(0), le=Decimal(100))]

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.max_percentage < self.min_percentage:
            raise ValueError("max_percentage debe ser mayor o igual a min_percentage")
        return self


class KilnOccupancyFactorOut(_Out):
    id: int
    kiln_id: int
    min_percentage: int
    max_percentage: int
    factor: Decimal


# ---------------------------------------------------------------------------
# Hornos
# ---------------------------------------------------------------------------
class KilnCreate(_In):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    capacity_volume_cm3: Annotated[Decimal, Field(gt=Decimal(0))]
    #: Fase 009C: dias que el horno queda ocupado por cada hornada. El 3 por
    #: omision es la duracion historica, no una regla: el horno grande son 4.
    firing_days_per_batch: Annotated[int, Field(ge=1)] = 3
    active: bool = True
    notes: Annotated[str | None, Field(max_length=1000)] = None
    occupancy_factors: list[KilnOccupancyFactorIn] | None = None

    @field_validator("name", "notes")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        return _plain_text(value) if value else value


class KilnUpdate(_In):
    """Actualizacion parcial. El codigo nunca se edita: lo genera el backend."""

    name: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    capacity_volume_cm3: Annotated[Decimal | None, Field(gt=Decimal(0))] = None
    firing_days_per_batch: Annotated[int | None, Field(ge=1)] = None
    active: bool | None = None
    notes: Annotated[str | None, Field(max_length=1000)] = None

    @field_validator("name", "notes")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        return _plain_text(value) if value else value


class KilnOut(_Out):
    id: int
    code: str
    name: str
    capacity_volume_cm3: Decimal
    firing_days_per_batch: int
    active: bool
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    #: Tarifas vigentes por tipo de quema, ya resueltas.
    current_low_rate: Decimal | None = None
    current_high_rate: Decimal | None = None
    occupancy_factors: list[KilnOccupancyFactorOut] = []


class KilnPage(_Out):
    items: list[KilnOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Hojas de quema: entrada
# ---------------------------------------------------------------------------
class FiringSessionIn(_In):
    kiln_id: int
    firing_type: FiringType
    sort_order: int = 0


class FiringLineIn(_In):
    product_id: int | None = None
    description: Annotated[str, Field(min_length=1, max_length=200)]
    quantity: Annotated[int, Field(gt=0)]
    length_cm: Dimension
    width_cm: Dimension
    height_cm: Dimension
    #: Horno que hace la quema baja de esta pieza. Debe existir como sesion.
    low_kiln_id: int | None = None
    #: Horno que hace la quema alta de esta pieza.
    high_kiln_id: int | None = None
    #: Horno cuya capacidad decide el tramo de ocupacion. Por omision, el de la
    #: primera sesion asignada a la linea.
    factor_kiln_id: int | None = None
    notes: Annotated[str | None, Field(max_length=1000)] = None
    sort_order: int = 0

    @field_validator("description", "notes")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        return _plain_text(value) if value else value


class FiringIn(_In):
    """Cuerpo de creacion y de edicion de una hoja en borrador."""

    scheduled_date: date | None = None
    firing_date: date | None = None
    notes: Annotated[str | None, Field(max_length=2000)] = None
    sessions: list[FiringSessionIn]
    lines: list[FiringLineIn]

    @field_validator("notes")
    @classmethod
    def _validate_notes(cls, value: str | None) -> str | None:
        return _plain_text(value) if value else None


class FiringCalculateIn(FiringIn):
    """Entrada del simulador.

    Mismo cuerpo que una hoja, porque simular es exactamente «cuanto costaria
    esta hoja». No inserta, no actualiza, no consume correlativo y no genera
    ningun movimiento de inventario.
    """


# ---------------------------------------------------------------------------
# Hojas de quema: salida
# ---------------------------------------------------------------------------
class FiringSessionOut(_Out):
    id: int | None = None
    kiln_id: int
    kiln_code: str
    kiln_name: str
    firing_type: FiringType
    rate_snapshot: Decimal
    capacity_snapshot: Decimal
    #: Volumen de las piezas asignadas a esta sesion.
    assigned_volume_cm3: Decimal
    #: Ocupacion fisica exacta de la sesion, informativa.
    physical_occupancy_percentage: Decimal
    subtotal: Decimal
    capacity_exceeded: bool = False
    #: Hornadas necesarias para esta sesion (Fase 009C). En una hoja de quema
    #: real siempre es 1: la hoja describe UNA hornada fisica.
    batches: int = 1
    #: Duracion de UNA hornada en este horno, copiada de
    #: ``kilns.firing_days_per_batch``. Viaja al frontend para que la interfaz
    #: explique el numero en vez de volver a calcularlo por su cuenta.
    days_per_batch: int = 0
    #: ``batches * days_per_batch``: dias que ocupa esta sesion.
    days: int = 0
    sort_order: int = 0


class FiringLineOut(_Out):
    id: int | None = None
    product_id: int | None = None
    product_internal_reference: str | None = None
    description: str
    quantity: int
    length_cm: Decimal
    width_cm: Decimal
    height_cm: Decimal
    unit_volume_cm3: Decimal
    total_volume_cm3: Decimal
    low_kiln_id: int | None = None
    high_kiln_id: int | None = None
    factor_kiln_id: int | None = None
    #: Participacion de la linea en el volumen total de la hoja.
    volume_share: Decimal
    #: Ocupacion fisica exacta contra la capacidad del horno del factor.
    occupancy_percentage: Decimal
    #: Tramo comercial en decenas. Los valores posibles son ``ALLOWED_BRACKETS``.
    occupancy_bracket: int
    occupancy_factor: Decimal
    base_cost: Decimal
    allocated_cost: Decimal
    capacity_exceeded: bool = False
    notes: str | None = None
    sort_order: int = 0


class FiringCalculateOut(_Out):
    """Resultado del simulador. Ningun dato se ha persistido."""

    total_volume_cm3: Decimal
    subtotal: Decimal
    total_cost: Decimal
    tax_percentage: Decimal = Decimal(0)
    tax_amount: Decimal = Decimal(0)
    total_with_tax: Decimal = Decimal(0)
    currency_code: str = "PEN"
    currency_symbol: str = "S/"
    occupancy_percentage: Decimal
    occupancy_factor: Decimal
    capacity_exceeded: bool
    #: Suma de hornadas de todas las sesiones (Fase 009C).
    total_batches: int = 0
    #: Suma de los dias de cada sesion, cada una con la duracion de SU horno.
    total_days: int = 0
    sessions: list[FiringSessionOut]
    lines: list[FiringLineOut]


class FiringOut(_Out):
    id: int
    code: str
    status: FiringStatus
    scheduled_date: date | None = None
    firing_date: date | None = None
    notes: str | None = None
    total_volume_cm3: Decimal
    occupancy_percentage: Decimal
    occupancy_factor: Decimal
    subtotal: Decimal
    total_cost: Decimal
    tax_percentage: Decimal = Decimal(0)
    tax_amount: Decimal = Decimal(0)
    total_with_tax: Decimal = Decimal(0)
    currency_code: str = "PEN"
    currency_symbol: str = "S/"
    created_by_id: uuid.UUID | None = None
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    sessions: list[FiringSessionOut] = []
    lines: list[FiringLineOut] = []


class FiringSummaryOut(_Out):
    """Fila del listado. No arrastra sesiones ni piezas."""

    id: int
    code: str
    status: FiringStatus
    scheduled_date: date | None = None
    firing_date: date | None = None
    total_volume_cm3: Decimal
    total_cost: Decimal
    line_count: int
    session_count: int
    created_at: datetime


class ConfirmedFiringLineOut(_Out):
    """Linea de una quema confirmada, tal y como la elige una cotizacion.

    Es una vista plana a proposito: el selector necesita el codigo de la hoja,
    la fecha y el costo ya repartido, y no tiene por que arrastrar las sesiones
    ni el resto de piezas de la quema.
    """

    id: int
    firing_id: int
    firing_code: str
    firing_date: date | None = None
    product_id: int | None = None
    product_internal_reference: str | None = None
    description: str
    quantity: int
    total_volume_cm3: Decimal
    allocated_cost: Decimal


class ConfirmedFiringLinePage(_Out):
    items: list[ConfirmedFiringLineOut]
    total: int
    limit: int
    offset: int


class FiringPage(_Out):
    items: list[FiringSummaryOut]
    total: int
    limit: int
    offset: int


#: Se reexporta para que la interfaz ofrezca exactamente los tramos validos.
__all__ = [
    "ALLOWED_BRACKETS",
    "ConfirmedFiringLineOut",
    "ConfirmedFiringLinePage",
    "FiringCalculateIn",
    "FiringCalculateOut",
    "FiringIn",
    "FiringLineIn",
    "FiringLineOut",
    "FiringOut",
    "FiringPage",
    "FiringSessionIn",
    "FiringSessionOut",
    "FiringSummaryOut",
    "KilnCreate",
    "KilnOccupancyFactorIn",
    "KilnOccupancyFactorOut",
    "KilnOut",
    "KilnPage",
    "KilnRateIn",
    "KilnRateOut",
    "KilnUpdate",
]
