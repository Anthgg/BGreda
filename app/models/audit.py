"""Auditoria propia de la aplicacion.

El Documento Funcional describe la trazabilidad con el *chatter* de Odoo. Esta
aplicacion no es Odoo: se traduce el requisito funcional —saber quien cambio
que, cuando y desde que valor— a un registro propio, plano y consultable.

Cada fila representa el cambio de **un campo**. Asi la consulta "quien toco el
IGV" es un filtro directo, sin analizar diferencias de documentos completos.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: Valor con el que se sustituye cualquier contenido que no deba almacenarse.
REDACTED = "[redactado]"

#: Longitud maxima que se conserva de un valor. Los textos largos —condiciones
#: generales, notas de pago— se truncan: la auditoria registra el cambio, no
#: sustituye a un control de versiones documental.
MAX_VALUE_LENGTH = 2000


class AuditAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class AuditEvent(Base):
    """Cambio registrado sobre una entidad de la aplicacion."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    #: Tipo logico de entidad ("company_settings", "document_sequence"...).
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    #: Identificador de la fila afectada, como texto para no atarse a un tipo.
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False)

    action: Mapped[AuditAction] = mapped_column(String(16), nullable=False)

    #: Campo modificado. Nulo cuando el evento describe la entidad completa.
    field: Mapped[str | None] = mapped_column(String(80))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)

    #: Autor del cambio. Nulo solo si lo origino un proceso interno.
    user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    #: Copia del nombre visible en el momento del cambio: si el perfil se
    #: renombra o desactiva, el historial sigue siendo legible.
    user_display_name: Mapped[str | None] = mapped_column(String(120))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    #: Contexto adicional no estructurado. Nunca contiene secretos.
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    __table_args__ = (
        CheckConstraint("action IN ('CREATE', 'UPDATE', 'DELETE')", name="action_allowed"),
        Index("ix_audit_events_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_audit_events_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuracion
        return f"<AuditEvent {self.entity_type}.{self.field} by={self.user_id}>"
