"""Registro de auditoria.

Traduce el requisito de trazabilidad del Documento Funcional —expresado alli
como el *chatter* de Odoo— a un registro propio, campo a campo.

Ningun valor sensible entra aqui: la lista de exclusion se aplica por nombre de
campo antes de escribir, de modo que un descuido futuro no filtre un secreto al
historial.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import MAX_VALUE_LENGTH, REDACTED, AuditAction, AuditEvent

#: Fragmentos que, presentes en el nombre de un campo, impiden auditar su valor.
#: Se comparan en minusculas y como subcadena: cubre "password",
#: "supabase_secret_key", "access_token", "csrf_secret", "database_url"...
SENSITIVE_FRAGMENTS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "cookie",
        "credential",
        "database_url",
        "apikey",
        "api_key",
        "private",
        "authorization",
    }
)

#: Campos cuyo valor es un binario o una referencia interna: se audita que
#: cambiaron, no su contenido.
OPAQUE_FIELDS = frozenset({"logo", "logo_object_path", "signature"})


def is_sensitive(field: str) -> bool:
    """True si el valor de este campo nunca debe almacenarse en la auditoria."""
    lowered = field.lower()
    return any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS)


def normalize_value(field: str, value: Any) -> str | None:
    """Convierte un valor a su representacion auditable."""
    if is_sensitive(field):
        return REDACTED
    if value is None:
        return None
    if field.lower() in OPAQUE_FIELDS:
        # Basta con saber que hay contenido; el binario no se copia.
        return "(archivo)"
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    if len(text) > MAX_VALUE_LENGTH:
        return text[:MAX_VALUE_LENGTH] + "..."
    return text


class AuditRecorder:
    """Escribe eventos de auditoria en la sesion en curso.

    No hace commit: los eventos forman parte de la misma transaccion que el
    cambio auditado. Si la operacion falla, el historial tampoco miente.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def record_changes(
        self,
        *,
        entity_type: str,
        entity_id: str,
        changes: dict[str, tuple[Any, Any]],
        user_id: uuid.UUID | None,
        user_display_name: str | None,
        action: AuditAction = AuditAction.UPDATE,
    ) -> list[AuditEvent]:
        """Registra un evento por cada campo realmente modificado."""
        events: list[AuditEvent] = []
        for field, (old, new) in changes.items():
            event = AuditEvent(
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                field=field,
                old_value=normalize_value(field, old),
                new_value=normalize_value(field, new),
                user_id=user_id,
                user_display_name=user_display_name,
            )
            self._session.add(event)
            events.append(event)
        return events

    def record_action(
        self,
        *,
        entity_type: str,
        entity_id: str,
        action: AuditAction,
        user_id: uuid.UUID | None,
        user_display_name: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Registra un evento sin diferencia de campo (alta, baja, subida)."""
        event = AuditEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            user_id=user_id,
            user_display_name=user_display_name,
            event_metadata=metadata,
        )
        self._session.add(event)
        return event

    async def list_events(
        self,
        *,
        entity_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AuditEvent], int]:
        """Devuelve los eventos mas recientes y el total disponible."""
        conditions = []
        if entity_type:
            conditions.append(AuditEvent.entity_type == entity_type)

        total_stmt = select(func.count()).select_from(AuditEvent)
        items_stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        for condition in conditions:
            total_stmt = total_stmt.where(condition)
            items_stmt = items_stmt.where(condition)

        total = (await self._session.execute(total_stmt)).scalar_one()
        items = list(
            (await self._session.execute(items_stmt.limit(limit).offset(offset))).scalars().all()
        )
        return items, total


def diff_model(
    current: Any,
    incoming: dict[str, Any],
    fields: list[str],
) -> dict[str, tuple[Any, Any]]:
    """Compara el estado actual con los valores entrantes.

    Devuelve solo los campos que cambian de verdad, para que el historial no se
    llene de eventos vacios cuando alguien guarda sin editar nada.
    """
    changes: dict[str, tuple[Any, Any]] = {}
    for field in fields:
        old = getattr(current, field)
        new = incoming.get(field)
        if old != new:
            changes[field] = (old, new)
    return changes
