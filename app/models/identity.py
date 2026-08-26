"""Cache y metricas de la consulta de identidad (DNI/RUC).

Dos tablas deliberadamente pequenas: la cache evita repetir un documento ya
consultado dentro del TTL, y las metricas son observabilidad interna, no el
balance oficial de cuota de ningun proveedor (ese lo tiene el proveedor).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.identity import IdentityDocumentType, ProviderName
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType

#: Se reexportan para que quien importe el modelo no tenga que saber que el
#: enum en realidad vive en app.core.identity (la unica fuente: el servicio,
#: los proveedores y la persistencia deben compartir la misma clase, o una
#: comparacion `is` entre dos StrEnum "iguales pero distintos" fallaria en
#: silencio).
__all__ = [
    "IdentityDocumentType",
    "IdentityLookupAuditEvent",
    "IdentityLookupCache",
    "IdentityLookupDailyStat",
    "IdentityLookupProviderMetric",
    "ProviderName",
]


class IdentityLookupCache(Base, TimestampMixin):
    """Ultima respuesta normalizada de un documento, mientras no expire.

    Se indexa por ``document_hash`` y no por el documento en claro: no hace
    falta conservar una copia legible del DNI o el RUC para poder invalidarla
    o volver a consultarla, porque quien pide un refresco ya lo conoce.
    """

    __tablename__ = "identity_lookup_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_type: Mapped[IdentityDocumentType] = mapped_column(
        StrEnumType(IdentityDocumentType, 8), nullable=False
    )
    document_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[ProviderName] = mapped_column(StrEnumType(ProviderName, 16), nullable=False)
    #: Solo los campos normalizados del contrato publico (ver
    #: ``app.schemas.identity``), nunca la respuesta cruda del proveedor.
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Cuando caduca. Una fila caducada no se borra automaticamente pero deja
    #: de servirse; la limpieza es un job de mantenimiento aparte, no logica
    #: de negocio.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("document_type", "document_hash", name="uq_identity_lookup_cache_hash"),
        Index("ix_identity_lookup_cache_expires_at", "expires_at"),
    )


class IdentityLookupProviderMetric(Base, TimestampMixin):
    """Contadores diarios por proveedor. Observabilidad, no facturacion."""

    __tablename__ = "identity_lookup_provider_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[ProviderName] = mapped_column(StrEnumType(ProviderName, 16), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    success: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    not_found: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rate_limited: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    timeouts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    provider_errors: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        UniqueConstraint("provider", "event_date", name="uq_identity_provider_metrics_day"),
        CheckConstraint("requests >= 0", name="requests_not_negative"),
    )


class IdentityLookupDailyStat(Base, TimestampMixin):
    """Cache hits y usos de fallback del dia, sin desglosar por proveedor.

    Decolecta solo se llama como secundario en esta arquitectura (nunca en
    paralelo con el primario), asi que sus propias ``requests`` en
    :class:`IdentityLookupProviderMetric` ya son el conteo de fallback; esta
    fila guarda ademas los aciertos de cache, que no pertenecen a ningun
    proveedor.
    """

    __tablename__ = "identity_lookup_daily_stats"

    event_date: Mapped[date] = mapped_column(Date, primary_key=True)
    cache_hits: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    fallback_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class IdentityLookupAuditEvent(Base):
    """Rastro minimo de quien consulto que, sin el documento en claro.

    Guardar cambios al tercero sigue la auditoria normal de
    ``app.services.audit``; esta tabla es solo para la consulta en si, que no
    necesita entrar al historial general con un DNI completo.
    """

    __tablename__ = "identity_lookup_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_type: Mapped[IdentityDocumentType] = mapped_column(
        StrEnumType(IdentityDocumentType, 8), nullable=False
    )
    #: Enmascarado por app.core.identity.mask_document antes de llegar aqui.
    masked_document: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[ProviderName | None] = mapped_column(
        StrEnumType(ProviderName, 16), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    cache_hit: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("ix_identity_lookup_audit_events_created_at", "created_at"),)
