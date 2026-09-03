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

from pydantic import BaseModel, ConfigDict, Field

from app.models.prototypes import PrototypeApproval, PrototypeStatus
from app.services.prototypes import PrototypeReadinessCode


class PrototypeMaterialIn(BaseModel):
    """Un material que la muestra va a gastar. Elegido, nunca deducido."""

    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0)
    #: En la unidad base del material. La conversion no se inventa aqui: si el
    #: producto se lleva en gramos, la cantidad son gramos.
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=6)


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
    quantity: Decimal
    uom_code: str


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


class PrototypeOut(PrototypeSummaryOut):
    notes: str | None
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
