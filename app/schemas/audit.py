"""Contratos del historial de auditoria."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.audit import AuditAction


class AuditEventOut(BaseModel):
    id: int
    entity_type: str
    entity_id: str
    action: AuditAction
    field: str | None
    old_value: str | None
    new_value: str | None
    user_id: uuid.UUID | None
    user_display_name: str | None
    created_at: datetime


class AuditEventPage(BaseModel):
    """Pagina de historial. Sin cursores complejos: la Fase 2 solo necesita
    mostrar los cambios recientes de configuracion."""

    items: list[AuditEventOut] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
