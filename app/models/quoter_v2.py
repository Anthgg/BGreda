"""Cotizador V2: la cabecera del documento y nada mas.

Fase 010A. Esto es el esqueleto, no el motor. Aqui hay identidad —codigo,
estado, cliente, tipo de produccion— y la marca de motor que hace imposible
confundir este documento con uno del Cotizador historico. No hay una sola
cifra de costo, ni un snapshot economico, ni un factor comercial: eso llega en
010B y siguientes, y adelantarlo ahora significaria inventar un precio que
nadie aprobo.

**Tabla nueva, no una columna mas en `quotations`.** La cabecera Legacy lleva
mas de noventa columnas de costeo con formulas embebidas y snapshots atados al
motor viejo; colgar V2 de ella obligaria a tocar Legacy en cada fase de la
familia 010 y a compartir CHECKs que significan cosas distintas en cada motor.
Lo que si se comparte son los maestros de verdad —`partners` hoy, productos y
materiales despues—, porque un cliente es el mismo cliente cotice quien cotice.

El CHECK de `pricing_engine_version` no es decorativo: es lo que impide que un
INSERT a mano meta aqui una cotizacion Legacy. El CHECK espejo vive en
`quotations` y hace lo contrario.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.masters import Partner

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.pricing_engine import PRICING_ENGINE_VERSION_LENGTH, PricingEngineVersion
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType


class V2QuotationStatus(StrEnum):
    """Estado comercial de una cotizacion V2.

    Enum **propio**, no el de Legacy, aunque los tres valores coincidan hoy.
    Compartirlo ataria los dos motores: el dia que V2 anada `EXPIRED` —la
    vigencia es 010H— el cambio no puede aparecer de rebote en documentos
    historicos que nunca supieron vencer.
    """

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class V2ProductionType(StrEnum):
    """Por menor o por mayor. Lo elige la persona, nunca el sistema.

    Es la primera decision de una cotizacion V2 porque de ella cuelga el horno
    sugerido —chico para menor, grande para mayor—. Sugerido: en 010E el horno
    sera editable dentro de la cotizacion y superar la capacidad avisara y
    recomendara, pero jamas cambiara solo el tipo de produccion.

    En 010A el valor se guarda y se devuelve. No entra en ningun calculo,
    porque todavia no hay calculo.
    """

    RETAIL = "RETAIL"
    WHOLESALE = "WHOLESALE"


#: Tipo de produccion con el que nace una cotizacion V2 si nadie dice otra cosa.
DEFAULT_V2_PRODUCTION_TYPE = V2ProductionType.RETAIL


class V2Quotation(Base, TimestampMixin):
    """Cabecera de una cotizacion del Cotizador V2."""

    __tablename__ = "v2_quotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    #: `CTZ-V2-2026-000001`. Talonario propio: agotar cotizaciones Legacy no
    #: puede mover el contador de V2, y un codigo historico nunca podra
    #: confundirse con uno nuevo porque el prefijo no coincide.
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    #: Siempre 'V2'. El CHECK de la tabla no admite otra cosa.
    pricing_engine_version: Mapped[PricingEngineVersion] = mapped_column(
        StrEnumType(PricingEngineVersion, PRICING_ENGINE_VERSION_LENGTH),
        nullable=False,
        default=PricingEngineVersion.V2,
        server_default=text("'V2'"),
    )

    status: Mapped[V2QuotationStatus] = mapped_column(
        StrEnumType(V2QuotationStatus, 16),
        nullable=False,
        default=V2QuotationStatus.DRAFT,
        server_default=text("'DRAFT'"),
        index=True,
    )

    production_type: Mapped[V2ProductionType] = mapped_column(
        StrEnumType(V2ProductionType, 16),
        nullable=False,
        default=DEFAULT_V2_PRODUCTION_TYPE,
        server_default=text("'RETAIL'"),
    )

    #: Maestro compartido de verdad: un cliente es el mismo cliente cotice
    #: quien cotice. RESTRICT y no CASCADE: borrar un tercero no puede
    #: llevarse por delante un documento comercial.
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("partners.id", ondelete="RESTRICT"), index=True
    )
    #: El nombre con el que se emitio. Que el maestro cambie despues no
    #: reescribe lo que el cliente recibio.
    customer_name_snapshot: Mapped[str | None] = mapped_column(String(200))

    name: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by_name: Mapped[str | None] = mapped_column(String(200))

    customer: Mapped[Partner | None] = relationship("Partner", foreign_keys=[customer_id])

    __table_args__ = (
        CheckConstraint(
            f"pricing_engine_version = '{PricingEngineVersion.V2}'",
            name="engine_is_v2",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'CONFIRMED', 'CANCELLED')",
            name="status_allowed",
        ),
        CheckConstraint(
            "production_type IN ('RETAIL', 'WHOLESALE')",
            name="production_type_allowed",
        ),
        Index("ix_v2_quotations_created_at", "created_at"),
    )
